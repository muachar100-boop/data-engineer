"""将 Olist CSV 原样导入 MySQL 8；不清洗、不去重、不覆盖已有表。"""

import csv
import hashlib
import itertools
import json
import logging
from pathlib import Path
import re
import uuid

import pymysql
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    'customers': 'olist_customers_dataset.csv',
    'geolocation': 'olist_geolocation_dataset.csv',
    'order_items': 'olist_order_items_dataset.csv',
    'order_payments': 'olist_order_payments_dataset.csv',
    'order_reviews': 'olist_order_reviews_dataset.csv',
    'orders': 'olist_orders_dataset.csv',
    'products': 'olist_products_dataset.csv',
    'sellers': 'olist_sellers_dataset.csv',
    'product_category_name_translation': 'product_category_name_translation.csv',
}
LOG = logging.getLogger(__name__)


def identifier(value):
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]*', value):
        raise ValueError(f'无效 SQL 标识符：{value!r}')
    return f'`{value}`'


def fingerprint(rows, width):
    """无序多重集指纹：计数 + 每行 SHA256 的模 2^256 和，保留重复次数。"""
    count = total = 0
    for row in rows:
        if len(row) != width or any(not isinstance(v, str) for v in row):
            raise ValueError('行宽不匹配或存在非字符串字段；拒绝隐式转换')
        payload = json.dumps(list(row), ensure_ascii=False, separators=(',', ':'))
        total = (total + int.from_bytes(hashlib.sha256(payload.encode()).digest(), 'big')) % (1 << 256)
        count += 1
    return {'rows': count, 'content_sha256_sum': f'{total:064x}'}


def source_info(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f, strict=True)
        columns = next(reader)
        if not columns or len(set(columns)) != len(columns):
            raise ValueError(f'CSV 表头无效：{path}')
        for column in columns:
            identifier(column)
        result = fingerprint(reader, len(columns))
    with path.open('rb') as f:
        result['file_sha256'] = hashlib.file_digest(f, 'sha256').hexdigest()
    return columns, result


def database_info(connection, table, columns):
    with connection.cursor() as cursor:
        cursor.execute(f'SHOW COLUMNS FROM {identifier(table)}')
        if [r[0] for r in cursor.fetchall()] != columns:
            raise ValueError(f'{table} 的列与 CSV 不一致；保留已有表并停止')
    with connection.cursor(pymysql.cursors.SSCursor) as cursor:
        cursor.execute(f'SELECT {", ".join(map(identifier, columns))} FROM {identifier(table)}')
        return fingerprint(cursor, len(columns))


def load_olist(data_dir=None):
    data_dir = Path(data_dir) if data_dir else ROOT / 'data' / 'olist'
    config = dotenv_values(ROOT / '.env')
    required = ('MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE')
    if any(config.get(k) is None for k in required):
        raise ValueError('.env 缺少 MYSQL_HOST/PORT/USER/PASSWORD/DATABASE 配置')
    connection = pymysql.connect(
        host=config['MYSQL_HOST'], port=int(config['MYSQL_PORT']),
        user=config['MYSQL_USER'], password=config['MYSQL_PASSWORD'],
        database=config['MYSQL_DATABASE'], charset='utf8mb4', autocommit=False,
    )
    stages = []
    locked = False
    report = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT VERSION()')
            version = cursor.fetchone()[0]
            if 'mariadb' in version.lower() or int(version.split('.')[0]) < 8:
                raise RuntimeError('要求 MySQL 8.0+')
            cursor.execute("SELECT GET_LOCK('olist_csv_import', 0)")
            locked = cursor.fetchone()[0] == 1
            if not locked:
                raise RuntimeError('另一个 Olist 导入任务正在运行')
            cursor.execute('SHOW FULL TABLES')
            existing = {row[0] for row in cursor.fetchall()}
        LOG.info('MySQL %s；已有表：%s', version, sorted(existing))
        # 所有输入、已有表先校验，再创建任何暂存表。
        sources = {}
        for table, filename in TABLES.items():
            columns, source = source_info(data_dir / filename)
            sources[table] = (columns, source)
            if table in existing:
                actual = database_info(connection, table, columns)
                if any(actual[k] != source[k] for k in actual):
                    raise ValueError(f'{table} 与 CSV 内容不一致；不覆盖、不追加，停止导入')
            report[table] = {**source, 'status': 'verified_existing' if table in existing else 'imported'}
        for table, filename in TABLES.items():
            if table in existing:
                LOG.info('%s 已有且内容一致，跳过写入', table)
                continue
            columns, source = sources[table]
            stage = '_olist_stage_' + uuid.uuid4().hex
            with connection.cursor() as cursor:
                fields = ', '.join(f'{identifier(c)} LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL' for c in columns)
                cursor.execute(f'CREATE TABLE {identifier(stage)} ({fields}) ENGINE=InnoDB')
                stages.append((stage, table))
                sql = f'INSERT INTO {identifier(stage)} VALUES ({", ".join(["%s"] * len(columns))})'
                with (data_dir / filename).open(encoding='utf-8-sig', newline='') as f:
                    reader = csv.reader(f, strict=True)
                    next(reader)
                    while batch := list(itertools.islice(reader, 2000)):
                        cursor.executemany(sql, batch)
                        connection.commit()
            actual = database_info(connection, stage, columns)
            if any(actual[k] != source[k] for k in actual):
                raise RuntimeError(f'{table} 导入完整性校验失败')
            LOG.info('%s：%s 行，全字段指纹一致', table, actual['rows'])
        # 防止导入过程中源文件改变。
        for table, filename in TABLES.items():
            if source_info(data_dir / filename) != sources[table]:
                raise RuntimeError(f'导入期间源文件发生变化：{filename}')
        if stages:
            with connection.cursor() as cursor:
                cursor.execute('RENAME TABLE ' + ', '.join(f'{identifier(s)} TO {identifier(t)}' for s, t in stages))
            stages.clear()
        output = ROOT / 'q2_sql_analysis' / 'import_report.json'
        temporary = output.with_suffix('.json.tmp')
        temporary.write_text(json.dumps({'mysql_version': version, 'tables': report}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(output)
        LOG.info('9 张表验证通过；报告：%s', output)
        return report
    finally:
        try:
            connection.rollback()
            with connection.cursor() as cursor:
                for stage, _ in stages:
                    cursor.execute(f'DROP TABLE IF EXISTS {identifier(stage)}')
                if locked:
                    cursor.execute("SELECT RELEASE_LOCK('olist_csv_import')")
        finally:
            connection.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s | %(message)s')
    load_olist()
