"""Q3.1：复用基础清洗，生成销售、客户特征和取消退款明细。

独立运行：python q3_etl_pipeline/pipeline.py
原始数据只读；输出保留来源行号，匹配过程采用确定性排序。
"""
import argparse
import csv
import logging
import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
FIELD_MAP = {
    'Invoice': 'invoice_no', 'StockCode': 'stock_code',
    'Description': 'description', 'Quantity': 'quantity',
    'Price': 'unit_price', 'InvoiceDate': 'invoice_datetime',
    'Customer ID': 'customer_id', 'Country': 'country',
}
# 按 Q1 业务证据覆盖格式规则；纯字母代码也可能是真商品。
STOCK_TYPES = {
    'POST': 'freight', 'DOT': 'freight', 'C2': 'freight',
    'AMAZONFEE': 'platform_fee', 'BANK CHARGES': 'bank_fee',
    'CRUK': 'commission', 'D': 'discount', 'M': 'manual_adjustment',
    'ADJUST': 'manual_adjustment', 'ADJUST2': 'manual_adjustment',
    'B': 'bad_debt', 'S': 'sample', 'TEST001': 'test', 'TEST002': 'test',
    'PADS': 'product', 'DCGSSBOY': 'product', 'DCGSSGIRL': 'product',
}

EXCLUDED_CODES = frozenset({
    'C3', 'DCGS0006', 'DCGS0016', 'DCGS0027', 'DCGS0036', 'DCGS0039',
    'DCGS0053', 'DCGS0055', 'DCGS0056', 'DCGS0057', 'DCGS0059', 'DCGS0060',
    'DCGS0066P', 'DCGS0070', 'DCGS0071', 'DCGS0072', 'DCGS0074',
    'DCGSLBOY', 'DCGSLGIRL', 'GIFT',
})


def stage(func):
    """统一记录每步耗时和异常；异常不吞掉，不继续产出半成品。"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        start = time.perf_counter()
        LOGGER.info('开始 %s', func.__name__)
        try:
            result = func(*args, **kwargs)
        except Exception:
            LOGGER.exception('失败 %s', func.__name__)
            raise
        LOGGER.info('完成 %s，耗时 %.2f 秒', func.__name__, time.perf_counter() - start)
        return result
    return wrapped


@dataclass
class CleaningResult:
    raw: pd.DataFrame
    classified: pd.DataFrame
    sales: pd.DataFrame
    summary: pd.DataFrame

    @property
    def analysis_records(self):
        """指定代码与零价从分析层排除，classified 仍完整保留审计。"""
        return self.classified.loc[~self.classified['excluded_from_analysis']].copy()

    @property
    def cancellations(self):
        return self.analysis_records.loc[lambda d: d['record_class'].eq('cancellation_refund')].copy()

    @property
    def ebay(self):
        return self.analysis_records.loc[lambda d: d['stock_type'].eq('ebay')].copy()


@stage
def load_data(path: Path) -> pd.DataFrame:
    """保真文本读取；不把 NA/NULL 字面文本静默转为缺失。"""
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        header = next(csv.reader(handle))
    if len(header) != len(set(header)) or set(header) != set(FIELD_MAP):
        raise ValueError(f'字段不符合当前数据契约：{header}')
    raw = pd.read_csv(path, dtype='string', keep_default_na=False, encoding='utf-8-sig')
    if raw.empty:
        raise ValueError('输入无数据')
    return raw


@stage
def standardize_fields(raw: pd.DataFrame) -> pd.DataFrame:
    """在副本上清首尾空白；保留原始表及字段映射，不填补业务缺失。"""
    df = raw.rename(columns=FIELD_MAP).copy()
    for col in df.columns:
        df[col] = df[col].str.strip().replace('', pd.NA)
    df.insert(0, 'source_row', np.arange(1, len(df) + 1))
    # 保留前导零；只去掉数字 ID 导出时附带的 .0 后缀。
    df['customer_id'] = df['customer_id'].str.replace(r'^([0-9]+)\.0+$', r'\1', regex=True)
    for col in ('quantity', 'unit_price'):
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')
    df['invoice_datetime'] = pd.to_datetime(df['invoice_datetime'], format='%Y-%m-%d %H:%M:%S', errors='coerce')
    df['total_amount'] = df['quantity'] * df['unit_price']
    df['invalid_core'] = (
        df[['invoice_no', 'stock_code', 'invoice_datetime']].isna().any(axis=1)
        | ~np.isfinite(df['quantity']) | ~np.isfinite(df['unit_price'])
        | ~np.isfinite(df['total_amount']) | df['quantity'].mod(1).ne(0)
    )
    df['invalid_customer_id'] = df['customer_id'].notna() & ~df['customer_id'].str.fullmatch(r'[0-9]+', na=False)
    df['duplicate_extra'] = raw.duplicated(keep='first')
    df['duplicate_member'] = raw.duplicated(keep=False)
    # 证据已显示 74215/80995 对冲；阈值仅供复核，不判定错误。
    df['extreme_quantity_review'] = df['quantity'].abs().ge(70000)
    return df


@stage
def classify_records(df: pd.DataFrame) -> pd.DataFrame:
    """商品性质与交易事件分开分类，避免退款中的费用被误算商品。"""
    df = df.copy()
    key = df['stock_code'].str.upper()
    kind = key.map(STOCK_TYPES).astype('string')
    kind = kind.mask(key.str.fullmatch(r'GIFT_[0-9_]+', na=False), 'voucher')
    regular = key.str.fullmatch(r'[0-9]+[A-Z]*', na=False)
    df['stock_type'] = kind.fillna(pd.Series(np.where(regular, 'product', 'unknown'), index=df.index, dtype='string'))
    unknown = df['stock_type'].eq('unknown')
    ebay = df['description'].str.contains('ebay', case=False, regex=False, na=False)
    # 用户指定：剩余未知代码按描述含 ebay 与否分流，缺描述仍不填补。
    df.loc[unknown, 'stock_type'] = pd.Series(
        np.where(ebay, 'ebay', 'product'), index=df.index, dtype='string')[unknown]
    df.loc[key.isin(EXCLUDED_CODES), 'stock_type'] = 'excluded_stock'
    df['excluded_from_analysis'] = key.isin(EXCLUDED_CODES) | df['unit_price'].eq(0)
    inv = df['invoice_no']
    df['is_cancellation'] = inv.str.startswith('C', na=False)
    debt = inv.str.startswith('A', na=False) | key.eq('B').fillna(False) | df['description'].str.casefold().eq('adjust bad debt').fillna(False)
    conditions = [key.isin(EXCLUDED_CODES), df['unit_price'].eq(0), df['invalid_core'], debt,
                  df['is_cancellation'] & df['quantity'].ge(0),
                  (df['is_cancellation'] | key.eq('PADS').fillna(False)) & df['quantity'].lt(0) & df['unit_price'].gt(0),
                  df['unit_price'].lt(0), df['quantity'].lt(0),
                  df['quantity'].eq(0) | df['unit_price'].eq(0),
                  ~inv.str.fullmatch(r'[0-9]+', na=False)]
    df['record_class'] = pd.Series(np.select(
        [c.to_numpy(dtype=bool, na_value=False) for c in conditions],
        ['excluded_stock', 'zero_price', 'invalid', 'bad_debt', 'c_positive_review', 'cancellation_refund',
         'negative_price_review', 'negative_quantity_review', 'zero_value_review', 'invoice_review'],
        default='positive'), index=df.index, dtype='string')
    df.loc[df['record_class'].eq('positive'), 'record_class'] = df.loc[df['record_class'].eq('positive'), 'stock_type'].map(
        {'product': 'real_sale', 'ebay': 'ebay'}).fillna('non_product')
    return df


@stage
def filter_real_sales(classified: pd.DataFrame) -> pd.DataFrame:
    """正向商品销售候选；含匿名、重复及极值，不等于退款后净销售。"""
    return classified.loc[classified['record_class'].eq('real_sale')].copy()


@stage
def summarize_records(classified: pd.DataFrame) -> pd.DataFrame:
    return classified.groupby(['record_class', 'stock_type'], dropna=False, sort=True).agg(
        rows=('source_row', 'size'), amount=('total_amount', 'sum'),
        anonymous=('customer_id', lambda s: s.isna().sum()),
    ).reset_index()


@stage
def validate_output(raw, classified, sales, summary):
    """行与金额守恒、来源可追溯、销售资格断言。"""
    assert len(raw) == len(classified) == summary['rows'].sum()
    assert classified['source_row'].is_unique
    assert classified['source_row'].between(1, len(raw)).all()
    assert np.isclose(summary['amount'].sum(), classified['total_amount'].sum(), atol=1e-6, rtol=0)
    active = classified.loc[~classified.excluded_from_analysis]
    assert active.unit_price.ne(0).all()
    assert not active.stock_code.str.upper().isin(EXCLUDED_CODES).any()
    pads = active.stock_code.str.upper().eq('PADS') & active.quantity.lt(0) & active.unit_price.gt(0) & ~active.invalid_core
    assert active.loc[pads, 'record_class'].eq('cancellation_refund').all()
    assert sales['quantity'].gt(0).all() and sales['unit_price'].gt(0).all()
    assert sales['total_amount'].gt(0).all()
    assert sales['invoice_no'].str.fullmatch(r'[0-9]+').all()
    assert sales['stock_type'].eq('product').all()
    assert sales['invoice_datetime'].notna().all()
    assert np.allclose(sales['total_amount'], sales['quantity'] * sales['unit_price'])
    assert str(sales['customer_id'].dtype) == 'string'
    assert pd.api.types.is_datetime64_any_dtype(sales['invoice_datetime'])


def markdown_table(frame):
    """只遍历小型汇总表，不逐行处理全量明细。"""
    rows = ['| ' + ' | '.join(frame.columns) + ' |', '| ' + ' | '.join(['---'] * len(frame.columns)) + ' |']
    rows.extend('| ' + ' | '.join(str(v).replace('|', '\\|') for v in row) + ' |' for row in frame.itertuples(index=False, name=None))
    return '\n'.join(rows)


@stage
def write_report(result: CleaningResult, path: Path):
    raw, df, sales = result.raw, result.classified, result.sales
    active = result.analysis_records
    metrics = {
        '原始 / 分析层行数': f'{len(raw):,} / {len(active):,}',
        '排除行数（代码与零价去重计数）': int(df.excluded_from_analysis.sum()),
        '指定代码行数（可与零价重叠）': int(df.stock_type.eq('excluded_stock').sum()),
        '零价行数（可与指定代码重叠）': int(df.unit_price.eq(0).sum()),
        '正向商品销售行数': len(sales),
        '正向商品计算金额': f'{sales.total_amount.sum():.6f}',
        '取消退款行数（含负数量PADS）': len(result.cancellations),
        '负数量PADS进入取消行数': int((result.cancellations.stock_code.str.upper().eq('PADS') & result.cancellations.quantity.lt(0)).sum()),
        'ebay分析行数': len(result.ebay),
        '原始整行重复多余行（保留）': int(df.duplicate_extra.sum()),
        '销售重复多余行（保留）': int(sales.duplicate_extra.sum()),
        '极端数量行 / 销售极端数量行（保留）': f'{df.extreme_quantity_review.sum()} / {sales.extreme_quantity_review.sum()}',
        '销售匿名行数': int(sales.customer_id.isna().sum()),
        '原始 / 销售有客户数': f'{df.customer_id.nunique()} / {sales.customer_id.nunique()}',
        '核心字段无效 / 客户ID无效行数': f'{df.invalid_core.sum()} / {df.invalid_customer_id.sum()}',
        '原始带符号金额': f'{df.total_amount.sum():.6f}',
        '排除记录带符号金额': f'{df.loc[df.excluded_from_analysis, "total_amount"].sum():.6f}',
        '分析层带符号金额': f'{active.total_amount.sum():.6f}',
    }
    text = """# Q3.1 基础清洗验证报告

## 清洗规则

- 原始CSV只读；原始文本及完整审计明细保留，通过source_row关联。派生字段去首尾空白、规范类型，缺失不填补。
- 重复记录保留；极端大额保留。数量绝对值≥70000仅作复核标记，不作为删除依据。
- 指定代码及所有零价记录从分析层排除，审计层保留；二者可能重叠。
- 其余未知代码：Description不区分大小写包含ebay时归为ebay，否则归为product；描述缺失也按此规则归为product，不补描述。ebay单列，不进入商品销售。
- 负数量PADS归入取消退款；C负数量正价格同样归入取消退款，保留原符号。C正数量单列待查，其他非C负数量待查。
- 销售筛选：数字发票、product、正数量、正价格、核心字段有效；保留匿名客户。费用、礼券、测试、坏账等单列。
- 金额=数量×单价，未扣匹配退款，不代表实付或最终净收入。本阶段不实现RFM、退款匹配、Parquet。

排除代码：""" + ', '.join(sorted(EXCLUDED_CODES)) + '。\n\n## 实际结果\n\n'
    text += markdown_table(pd.DataFrame({'指标': list(metrics), '值': list(metrics.values())}))
    summary = result.summary.rename(columns={
        'record_class': '记录分类', 'stock_type': '代码类型', 'rows': '行数',
        'amount': '带符号金额', 'anonymous': '匿名行数'})
    summary['带符号金额'] = summary['带符号金额'].map(lambda value: f'{value:.6f}')
    text += '\n\n## 分类对账（含排除记录）\n\n' + markdown_table(summary)
    text += """\n\n## 验证与后续

已校验行数及金额守恒、来源行唯一性、销售正值和字段类型，并验证分析层无零价或指定排除代码。待处理退款匹配、部分退货与累计数量占用；未知代码按描述分类属于当前业务规则，后续仍可结合商品主数据复核。
"""
    Path(path).write_text(text, encoding='utf-8')


@stage
def run_basic_cleaning(path: Path) -> CleaningResult:
    raw = load_data(path)
    classified = classify_records(standardize_fields(raw))
    sales = filter_real_sales(classified)
    summary = summarize_records(classified)
    validate_output(raw, classified, sales, summary)
    return CleaningResult(raw, classified, sales, summary)


# 题目要求字段在前，审计扩展字段在后。
FACT_COLUMNS = [
    'invoice_no', 'stock_code', 'description', 'quantity', 'unit_price',
    'total_amount', 'invoice_datetime', 'customer_id', 'country',
]
AUDIT_COLUMNS = ['source_row', 'duplicate_extra', 'duplicate_member', 'extreme_quantity_review']


@dataclass
class PipelineResult:
    cleaning: CleaningResult
    sales_facts: pd.DataFrame
    customer_features: pd.DataFrame
    returns_log: pd.DataFrame
    elapsed_seconds: float = 0.0
    price_review: dict | None = None


@stage
def build_sales_facts(cleaning: CleaningResult) -> pd.DataFrame:
    """复用唯一的销售筛选规则，保留重复与极端交易。"""
    return cleaning.sales[FACT_COLUMNS + AUDIT_COLUMNS].reset_index(drop=True).copy()


@stage
def match_returns(classified: pd.DataFrame, sales: pd.DataFrame, require_same_price: bool = True) -> pd.DataFrame:
    """只对C记录匹配，销售端不做全量笛卡尔连接。

    以客户+商品建立索引，仅循环约两万条取消记录（不循环百万销售行）。
    顺序：退款时间、来源行；候选：历史时间倒序，同时间来源行最小优先。
    先要求同价（仅容忍1e-9浮点误差，不按分舍入），再按等量优先、最近选择。
    require_same_price=False仅用于复算旧规则审计，不用于正式输出或RFM。
    不跨多条销售拆分；原单不足时保留未匹配，禁止过量退款。
    """
    LOGGER.info('匹配模式：%s', '正式同价规则' if require_same_price else '旧规则复算，仅作价格差异审计')
    returns = classified.loc[classified.is_cancellation].sort_values(
        ['invoice_datetime', 'source_row'], kind='stable', na_position='last').copy()
    returns = returns.reset_index(drop=True)
    sales_ordered = sales.sort_values(
        ['invoice_datetime', 'source_row'], ascending=[True, False], kind='stable').reset_index(drop=True)
    valid_sales = sales_ordered.customer_id.notna()
    groups = sales_ordered.loc[valid_sales].groupby(['customer_id', 'stock_code'], sort=False).groups
    # groups的索引就是sales_ordered的行号，组内时间已升序。
    groups = {key: np.asarray(index, dtype=np.int64) for key, index in groups.items()}
    times = sales_ordered.invoice_datetime.to_numpy(dtype='datetime64[ns]')
    quantities = sales_ordered.quantity.to_numpy()
    remaining = quantities.copy()
    n = len(returns)
    matched_ids = pd.array([pd.NA] * n, dtype='Int64')
    invoices = pd.array([pd.NA] * n, dtype='string')
    statuses = np.full(n, 'unmatched', dtype=object)
    methods = np.full(n, 'none', dtype=object)
    candidates = np.zeros(n, dtype=np.int64)
    matched_price = np.full(n, np.nan)
    matched_date = np.full(n, np.datetime64('NaT'), dtype='datetime64[ns]')
    prices = sales_ordered.unit_price.to_numpy()
    source_ids = sales_ordered.source_row.to_numpy()
    source_invoices = sales_ordered.invoice_no.to_numpy()
    # 费用、测试等C记录仍留日志，但不匹配到商品销售。
    columns = ['source_row', 'customer_id', 'stock_code', 'quantity', 'unit_price',
               'invoice_datetime', 'stock_type', 'invalid_core', 'excluded_from_analysis']
    for i, row in enumerate(returns[columns].itertuples(index=False, name=None)):
        source, customer, stock, qty, price, date, stock_type, invalid, excluded = row
        if excluded:
            statuses[i] = 'excluded_by_cleaning'
        elif invalid:
            statuses[i] = 'invalid_core'
        elif qty >= 0:
            statuses[i] = 'non_negative_c_review'
        elif price <= 0:
            statuses[i] = 'non_positive_price'
        elif stock_type != 'product':
            statuses[i] = 'non_product'
        elif pd.isna(customer):
            statuses[i] = 'missing_customer'
        else:
            indices = groups.get((customer, stock))
            if indices is None:
                statuses[i] = 'no_customer_product_sale'
                continue
            # 严格早于退款；同一时间戳不能证明先后，不匹配。
            stop = np.searchsorted(times[indices], date.to_datetime64(), side='left')
            history = indices[:stop]
            if not len(history):
                statuses[i] = 'no_earlier_sale'
                continue
            needed = -qty
            available = history[remaining[history] >= needed]
            if not len(available):
                statuses[i] = 'insufficient_remaining_quantity'
                continue
            if require_same_price:
                available = available[np.isclose(prices[available], price, atol=1e-9, rtol=0)]
                if not len(available):
                    statuses[i] = 'no_same_price_sale'
                    continue
            exact = available[quantities[available] == needed]
            pool = exact if len(exact) else available
            if not len(pool):
                statuses[i] = 'insufficient_remaining_quantity'
                continue
            chosen = pool[-1]
            candidates[i] = len(pool)
            methods[i] = 'exact_quantity' if len(exact) else 'partial_quantity'
            statuses[i] = 'matched'
            matched_ids[i] = source_ids[chosen]
            invoices[i] = source_invoices[chosen]
            matched_price[i] = prices[chosen]
            matched_date[i] = times[chosen]
            remaining[chosen] -= needed
    returns['matched_original_invoice'] = invoices
    returns['matched_source_row'] = matched_ids
    returns['match_status'] = pd.array(statuses, dtype='string')
    returns['match_method'] = pd.array(methods, dtype='string')
    returns['candidate_count'] = candidates
    returns['matched_original_unit_price'] = matched_price
    returns['matched_original_datetime'] = matched_date
    returns['price_mismatch'] = returns.match_status.eq('matched') & ~np.isclose(
        returns.unit_price, returns.matched_original_unit_price, atol=1e-9, rtol=0)
    LOGGER.info('C记录 %d，匹配 %d；状态统计 %s', n,
                int(returns.match_status.eq('matched').sum()), returns.match_status.value_counts().to_dict())
    # 文件日志记录每条未匹配的来源与原因；控制台只显示汇总。
    for source, invoice, status in returns.loc[returns.match_status.ne('matched'),
            ['source_row', 'invoice_no', 'match_status']].itertuples(index=False, name=None):
        LOGGER.debug('未匹配 source_row=%s invoice=%s reason=%s', source, invoice, status)
    return returns[FACT_COLUMNS + ['matched_original_invoice'] + AUDIT_COLUMNS + [
        'stock_type', 'record_class', 'excluded_from_analysis', 'matched_source_row',
        'match_status', 'match_method', 'candidate_count', 'matched_original_unit_price',
        'matched_original_datetime', 'price_mismatch']]


@stage
def compute_rfm(sales: pd.DataFrame, returns: pd.DataFrame, reference_date) -> pd.DataFrame:
    """正式RFM基于全部有客户的正向真实销售；退款仅作独立审计，不改变RFM。"""
    known = sales.loc[sales.customer_id.notna()].copy()
    matched = returns.loc[returns.match_status.eq('matched')]
    usage = matched.assign(returned_quantity=-matched.quantity).groupby('matched_source_row').returned_quantity.sum()
    known['remaining_quantity'] = known.quantity - known.source_row.map(usage).fillna(0)
    effective = known.copy()  # 正向口径包含之后全退或部分退货的原销售
    features = effective.groupby('customer_id', sort=True).agg(
        frequency=('invoice_no', 'nunique'), first_purchase_date=('invoice_datetime', 'min'),
        last_purchase_date=('invoice_datetime', 'max'), unique_products=('stock_code', 'nunique'),
        remaining_quantity=('remaining_quantity', 'sum'))
    gross = known.groupby('customer_id').agg(
        gross_frequency=('invoice_no', 'nunique'), gross_monetary=('total_amount', 'sum'),
        gross_first_purchase_date=('invoice_datetime', 'min'), gross_last_purchase_date=('invoice_datetime', 'max'),
        gross_unique_products=('stock_code', 'nunique'), duplicate_sales_rows=('duplicate_extra', 'sum'),
        extreme_sales_rows=('extreme_quantity_review', 'sum'))
    features = features.join(gross)
    refunds = matched.assign(refund_amount=-matched.total_amount).groupby('customer_id').agg(
        matched_refund_amount=('refund_amount', 'sum'), return_count=('invoice_no', 'nunique'),
        ambiguous_return_rows=('candidate_count', lambda x: x.gt(1).sum()))
    features = features.join(refunds)
    for col in refunds.columns:
        features[col] = features[col].fillna(0)
    features['monetary'] = features.gross_monetary
    features['net_monetary'] = features.monetary - features.matched_refund_amount
    features['fully_returned'] = features.remaining_quantity.eq(0)
    features['recency_days'] = (pd.Timestamp(reference_date).normalize() - features.last_purchase_date.dt.normalize()).dt.days
    countries = effective.dropna(subset=['country']).groupby(['customer_id', 'country']).invoice_no.nunique().rename('orders').reset_index()
    countries = countries.sort_values(['customer_id', 'orders', 'country'], ascending=[True, False, True], kind='stable')
    features['country'] = countries.drop_duplicates('customer_id').set_index('customer_id').country
    features['country_count'] = effective.groupby('customer_id').country.nunique()
    features['is_one_time_buyer'] = features.frequency.eq(1)
    un = returns.loc[returns.match_status.ne('matched') & returns.quantity.lt(0)
        & returns.unit_price.gt(0) & returns.stock_type.eq('product') & ~returns.excluded_from_analysis]
    unmatched = un.assign(refund_amount=-un.total_amount).groupby('customer_id').agg(
        unmatched_refund_amount=('refund_amount', 'sum'), unmatched_return_rows=('source_row', 'size'))
    features = features.join(unmatched)
    features[['unmatched_refund_amount', 'unmatched_return_rows']] = features[['unmatched_refund_amount', 'unmatched_return_rows']].fillna(0)
    # 大额已由用户确认真实，保留统计标记，但不因此将客户判为异常。
    features['has_anomaly'] = (features.duplicate_sales_rows.gt(0) | features.ambiguous_return_rows.gt(0)
        | features.unmatched_return_rows.gt(0) | features.country_count.gt(1))
    for col in ['return_count', 'ambiguous_return_rows', 'unmatched_return_rows']:
        features[col] = features[col].astype('int64')
    features = features.reset_index()
    required = ['customer_id', 'recency_days', 'frequency', 'monetary', 'first_purchase_date',
                'last_purchase_date', 'unique_products', 'country', 'is_one_time_buyer']
    return features[required + [c for c in features.columns if c not in required]]


@stage
def audit_price_changes(previous: pd.DataFrame, current: pd.DataFrame) -> dict:
    """复核旧规则价格不同的全部记录，新旧匹配各自独立计算数量占用。"""
    old = previous.loc[previous.price_mismatch, ['source_row', 'matched_source_row']]
    checked = old.merge(current[['source_row', 'matched_source_row', 'match_status']],
        on='source_row', suffixes=('_old', '_new'), validate='one_to_one')
    alternatives = checked.match_status.eq('matched')
    assert checked.loc[alternatives, 'matched_source_row_old'].ne(checked.loc[alternatives, 'matched_source_row_new']).all()
    stats = {'previous_price_mismatch': len(old), 'alternative_matched': int(alternatives.sum()),
             'now_null': int((~alternatives).sum())}
    for row in checked.itertuples(index=False, name=None):
        LOGGER.debug('旧价格差异复核 source_row=%s old_sale=%s new_sale=%s status=%s', *row)
    LOGGER.info('旧价格差异记录复核：%s', stats)
    return stats


@stage
def validate_datasets(result: PipelineResult):
    sales, features, returns = result.sales_facts, result.customer_features, result.returns_log
    assert len(sales) == len(result.cleaning.sales)
    assert sales.source_row.is_unique and sales.quantity.gt(0).all()
    assert sales.total_amount.gt(0).all() and np.isfinite(sales.total_amount).all()
    assert np.allclose(sales.total_amount, sales.quantity * sales.unit_price)
    for df in (sales, returns):
        assert all(c in df.columns for c in FACT_COLUMNS)
        assert all(str(df[c].dtype) == 'string' for c in ['invoice_no', 'stock_code', 'customer_id'])
        assert pd.api.types.is_datetime64_any_dtype(df.invoice_datetime)
        assert pd.api.types.is_float_dtype(df.total_amount)
    assert features.customer_id.is_unique and features.customer_id.notna().all()
    matched = returns.loc[returns.match_status.eq('matched')]
    known = sales.loc[sales.customer_id.notna()].copy()
    used = matched.assign(used=-matched.quantity).groupby('matched_source_row').used.sum()
    known['remaining'] = known.quantity - known.source_row.map(used).fillna(0)
    effective = known  # 正式RFM不按剩余数量筛除购买
    assert set(features.customer_id) == set(effective.customer_id)
    assert features.recency_days.ge(0).all() and features.frequency.ge(1).all()
    assert features.monetary.gt(0).all() and features.unique_products.ge(1).all()
    assert features.is_one_time_buyer.equals(features.frequency.eq(1))
    assert pd.api.types.is_bool_dtype(features.is_one_time_buyer)
    assert features.frequency.eq(features.gross_frequency).all()
    assert features.unique_products.eq(features.gross_unique_products).all()
    assert np.isclose(features.monetary.sum(), known.total_amount.sum(), atol=1e-6, rtol=0)
    assert np.allclose(features.monetary, features.gross_monetary, atol=1e-6, rtol=0)
    assert np.allclose(features.net_monetary, features.monetary - features.matched_refund_amount, atol=1e-6, rtol=0)
    by_customer = effective.groupby('customer_id').agg(
        frequency=('invoice_no', 'nunique'), unique_products=('stock_code', 'nunique'),
        first_purchase_date=('invoice_datetime', 'min'), last_purchase_date=('invoice_datetime', 'max'))
    pd.testing.assert_frame_equal(features.set_index('customer_id')[by_customer.columns], by_customer, check_dtype=False)
    expected = result.cleaning.classified.loc[result.cleaning.classified.is_cancellation, 'source_row']
    assert set(returns.source_row) == set(expected)
    assert returns.source_row.is_unique and returns.invoice_no.str.startswith('C').all()
    matched = returns.loc[returns.match_status.eq('matched')]
    assert returns.loc[returns.match_status.ne('matched'), 'matched_original_invoice'].isna().all()
    assert matched.matched_source_row.notna().all()
    assert matched.quantity.lt(0).all() and matched.total_amount.lt(0).all()
    joined = matched.merge(sales, left_on='matched_source_row', right_on='source_row', suffixes=('_return', '_sale'), validate='many_to_one')
    assert joined.customer_id_return.eq(joined.customer_id_sale).all()
    assert joined.stock_code_return.eq(joined.stock_code_sale).all()
    assert joined.invoice_datetime_sale.lt(joined.invoice_datetime_return).all()
    assert np.isclose(joined.unit_price_sale, joined.unit_price_return, atol=1e-9, rtol=0).all()
    assert not returns.price_mismatch.any()
    exact = joined.match_method.eq('exact_quantity')
    assert joined.loc[exact, 'quantity_sale'].eq(-joined.loc[exact, 'quantity_return']).all()
    usage = matched.assign(used=-matched.quantity).groupby('matched_source_row').used.sum()
    assert usage.le(sales.set_index('source_row').quantity.reindex(usage.index)).all()
    assert np.isclose(features.matched_refund_amount.sum(), -matched.total_amount.sum(), atol=1e-6, rtol=0)
    assert returns.loc[returns.match_status.ne('matched'), 'matched_source_row'].isna().all()
    # 按日核对：标准化金额必须等于各互斥分类之和。
    classified = result.cleaning.classified
    daily = classified.groupby([classified.invoice_datetime.dt.normalize(), 'record_class']).total_amount.sum().groupby(level=0).sum()
    raw_daily = classified.groupby(classified.invoice_datetime.dt.normalize()).total_amount.sum()
    assert np.allclose(daily.reindex(raw_daily.index), raw_daily, atol=1e-6, rtol=0)


@stage
def export_parquet(result: PipelineResult, output_dir: Path):
    """先写临时目录并回读校验，全部通过后替换正式文件。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    datasets = {'sales_facts': result.sales_facts, 'customer_features': result.customer_features, 'returns_log': result.returns_log}
    with tempfile.TemporaryDirectory(prefix='.q3-', dir=output_dir) as tmp:
        for name, df in datasets.items():
            file = Path(tmp) / f'{name}.parquet'
            df.to_parquet(file, index=False, engine='pyarrow', compression='snappy')
            loaded = pd.read_parquet(file, engine='pyarrow')
            pd.testing.assert_frame_equal(df, loaded)
            LOGGER.info('Parquet回读通过 %s，%d行，%d字节', name, len(df), file.stat().st_size)
        for name in datasets:
            os.replace(Path(tmp) / f'{name}.parquet', output_dir / f'{name}.parquet')


@stage
def write_pipeline_report(result: PipelineResult, path: Path, input_hash: str):
    sales, features, returns = result.sales_facts, result.customer_features, result.returns_log
    df = result.cleaning.classified
    matched = returns.match_status.eq('matched')
    eligible = returns.quantity.lt(0) & returns.unit_price.gt(0) & returns.stock_type.eq('product') & returns.customer_id.notna() & ~returns.excluded_from_analysis & returns.invoice_datetime.notna()
    gross_customers = sales.customer_id.nunique()
    metrics = {
        '原始 / 分析层行数': f'{len(df):,} / {(~df.excluded_from_analysis).sum():,}',
        '指定代码 / 零价 / 合并排除行数': f'{df.stock_type.eq("excluded_stock").sum()} / {df.unit_price.eq(0).sum()} / {df.excluded_from_analysis.sum()}',
        'sales_facts行数 / 正向金额': f'{len(sales):,} / {sales.total_amount.sum():.6f}',
        '销售匿名行数 / 原始重复多余行': f'{sales.customer_id.isna().sum()} / {df.duplicate_extra.sum()}',
        '大额数量标记行数 / 销售行数（已确认真实，保留）': f'{df.extreme_quantity_review.sum()} / {sales.extreme_quantity_review.sum()}',
        '原始有客户数 / 有正向销售客户数 / RFM有效客户数': f'{df.customer_id.nunique()} / {gross_customers} / {len(features)}',
        '全退客户数（仍保留正向RFM）': int(features.fully_returned.sum()),
        'R基准日': df.invoice_datetime.max().date(),
        'R范围 / F范围': f'{features.recency_days.min()}–{features.recency_days.max()} / {features.frequency.min()}–{features.frequency.max()}',
        'M范围 / 商品数范围': f'{features.monetary.min():.3f}–{features.monetary.max():.3f} / {features.unique_products.min()}–{features.unique_products.max()}',
        '有客户正向金额（含全退客户）': f'{sales.loc[sales.customer_id.notna(), "total_amount"].sum():.6f}',
        '已匹配退款金额（含全退客户）': f'{-returns.loc[matched, "total_amount"].sum():.6f}',
        '正式RFM金额M合计（正向，不扣退款）': f'{features.monetary.sum():.6f}',
        '退款后金额合计（net_monetary，仅审计）': f'{features.net_monetary.sum():.6f}',
        '一次正向购买客户数': int(features.is_one_time_buyer.sum()),
        'returns_log行数 / 带符号金额': f'{len(returns):,} / {returns.total_amount.sum():.6f}',
        '匹配行数 / 全部C记录成功率': f'{matched.sum()} / {matched.mean():.2%}',
        '有客户商品负数量退款资格行数 / 成功率': f'{eligible.sum()} / {matched.sum() / eligible.sum():.2%}' if eligible.any() else '0 / 不适用',
        '精确数量 / 部分退货匹配行数': f'{returns.match_method.eq("exact_quantity").sum()} / {returns.match_method.eq("partial_quantity").sum()}',
        '多候选匹配行数 / 已匹配价格不同行数': f'{returns.candidate_count.gt(1).sum()} / {returns.price_mismatch.sum()}',
        'C记录重复多余行（保留）': int(returns.duplicate_extra.sum()),
        '总耗时（含清洗、匹配、断言、导出及回读）': f'{result.elapsed_seconds:.2f}秒',
    }
    text = """# Q3.1 ETL 验证报告

## 已确认规则

- 原始CSV只读，派生字段规范类型、清理首尾空白，缺失不填补。source_row关联原始数据行（从1开始，不含表头）；原始与分类审计明细保留在返回对象中。
- 重复保留；极端大额已确认真实，保留。数量绝对值≥70000仅作标记，不作为错误或删除依据。
- 指定代码与零价从分析层排除，审计层保留；其余未知代码按描述是否含ebay（不区分大小写）分为ebay或product，不含ebay及缺描述均归product。费用、礼券、测试、坏账不混入商品销售。
- sales_facts仍为数字发票、product、正数量和正价格的销售明细，包含匿名，不从中删除后来被退款的原销售。金额=数量×单价，不代表实付。
- returns_log保留全部C记录。正数量C、非商品和未匹配记录的匹配字段为NULL，不扣减客户金额。负数量非C的PADS保留在基础取消明细，不进入仅限C的退货输出。

排除代码：""" + ', '.join(sorted(EXCLUDED_CODES)) + '。\n\n## 实际结果\n\n'
    text += markdown_table(pd.DataFrame({'指标': list(metrics), '结果': list(metrics.values())}))
    text += """\n\n## 退货匹配

1. 退款按时间、source_row顺序处理。候选须同客户、同商品、单价相同、销售严格早于退款且余量足够。单价仅容忍1e-9浮点误差，不四舍五入到两位小数。
2. 先选原始数量等于退款绝对数量的最近销售；无等量候选才选余量足够的最近销售，标记partial_quantity。同时间选source_row最小者；candidate_count记录该优先级下候选数。
3. 每次匹配扣减可退余量，允许买10个分两次各退5个；累计不能超退。不跨销售行拼单，找不到合适销售则NULL，不占用余量。
4. 原候选价格不同时，会从其他同价候选重新选择；没有同价且余量足够的销售，状态为no_same_price_sale并给NULL。重复来源行仍分别处理，不能证明业务上没有重复退款。

## 旧价格差异记录复核

"""
    review = result.price_review or {}
    text += f'复算旧规则的 {review.get("previous_price_mismatch", 0)} 条价格差异记录：{review.get("alternative_matched", 0)} 条找到其他同价销售，{review.get("now_null", 0)} 条在新规则下未匹配，匹配字段为NULL。新旧规则各自按时间重新分配余量，其他记录也可能受占用变化影响。\n'
    text += """\n## 正式RFM口径

- 正式recency_days、frequency、monetary、购买日期、商品数及主要国家均基于有客户的正向真实销售，与README一致；匿名不进入客户表。
- R以数据集最大日期减最后正向购买日期（日历日）；F按正向独立Invoice计；M为正向销售金额之和，不扣退款。全退和部分退货不改变上述字段，全退客户仍保留。
- 主要国家按正向购买的独立Invoice数最多选取，平手按国家名称排序；is_one_time_buyer表示只有一次正向购买。gross_*作为兼容审计字段，与相应正向字段一致。
- 匹配退款额、退货次数、未匹配退款额、remaining_quantity、fully_returned和net_monetary仅供独立退货审计，不用于正式RFM。net_monetary=monetary－已匹配退款金额，未匹配退款不扣减。

## 匹配状态及原因

"""
    statuses = returns.groupby('match_status').agg(rows=('source_row', 'size'), amount=('total_amount', 'sum')).reset_index()
    statuses['说明'] = statuses.match_status.map({
        'matched': '已匹配', 'insufficient_remaining_quantity': '历史销售余量不足（不跨行拼单）',
        'no_same_price_sale': '无同价且余量足够的历史销售',
        'missing_customer': '缺少客户ID', 'no_customer_product_sale': '没有该客户商品的有效销售',
        'no_earlier_sale': '没有严格早于退款的销售', 'non_negative_c_review': 'C正数量，匹配NULL',
        'non_product': '非商品，不匹配商品销售', 'excluded_by_cleaning': '清洗排除',
        'invalid_core': '关键字段无效', 'non_positive_price': '单价非正'})
    text += markdown_table(statuses.rename(columns={'match_status': '状态', 'rows': '行数', 'amount': '带符号金额'}))
    nulls = []
    for name, frame in [('sales_facts', sales), ('customer_features', features), ('returns_log', returns)]:
        for col in frame.columns:
            if frame[col].isna().any():
                nulls.append({'数据集': name, '字段': col, '空值率': f'{frame[col].isna().mean():.4%}'})
    text += '\n\n## 输出空值率（仅列非零，其余均为0）\n\n' + markdown_table(pd.DataFrame(nulls, columns=['数据集', '字段', '空值率']))
    text += """\n\n## 校验与限制

已断言销售正值、客户唯一性与有效客户覆盖、R非负、匹配键/时间/同价、累计数量、未匹配NULL、正向RFM金额与销售对账、审计净金额及按日分类金额对账。三份Parquet逐表回读与内存表完整比较。全表清洗使用向量化，仅约两万条退款使用有状态循环；固定排序和种子42，结果可复现。pipeline_log.txt记录分步耗时、未匹配明细及旧价格差异逐行复核。

匿名不能可靠匹配或做RFM；同时间戳和跨销售行拼单退款不自动匹配。多候选只是规则推断，重复保留仍可能放大金额和可退数量。正式M为正向交易额，不代表退款后净消费；审计净金额不扣未匹配退款，仍可能偏高。极端大额按已确认真实处理。

README所需文件和字段、模块化、日志/异常、断言及性能要求均实现。按本次确认，正式RFM恢复README的正向口径；部分退货仍是等量匹配的已接受扩展。Q1、Q2代码不变。

输入CSV SHA256：`""" + input_hash + '`。\n'
    Path(path).write_text(text, encoding='utf-8')


def configure_logging(log_path: Path):
    """独立运行和run_all共用同一完整文件日志，避免重复添加。"""
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    for handler in list(LOGGER.handlers):
        handler.close()
        LOGGER.removeHandler(handler)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    file = logging.FileHandler(log_path, mode='w', encoding='utf-8')
    file.setLevel(logging.DEBUG)
    for handler in (stream, file):
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)


@stage
def run_pipeline(input_path=ROOT / 'data/online_retail_ii.csv', output_dir=None, report_path=None):
    start = time.perf_counter()
    np.random.seed(42)
    input_path = Path(input_path)
    module_dir = Path(__file__).parent
    output_dir = Path(output_dir) if output_dir is not None else module_dir / 'outputs'
    report_path = Path(report_path) if report_path is not None else module_dir / 'validation_report.md'
    input_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()
    cleaning = run_basic_cleaning(input_path)
    sales = build_sales_facts(cleaning)
    # 旧规则仅作对照审计，正式输出严格要求同价。
    previous = match_returns(cleaning.classified, sales, require_same_price=False)
    returns = match_returns(cleaning.classified, sales)
    price_review = audit_price_changes(previous, returns)
    del previous
    features = compute_rfm(sales, returns, cleaning.classified.invoice_datetime.max())
    result = PipelineResult(cleaning, sales, features, returns, price_review=price_review)
    validate_datasets(result)
    export_parquet(result, output_dir)
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == input_hash, '原始文件发生变化'
    result.elapsed_seconds = time.perf_counter() - start
    write_pipeline_report(result, report_path, input_hash)
    main_elapsed = result.elapsed_seconds
    result.elapsed_seconds = time.perf_counter() - start
    report_text = report_path.read_text(encoding='utf-8').replace(
        '总耗时（含清洗、匹配、断言、导出及回读）', '总耗时（含清洗、匹配、校验、导出、回读与报告）').replace(
        f'{main_elapsed:.2f}秒', f'{result.elapsed_seconds:.2f}秒')
    report_path.write_text(report_text, encoding='utf-8')
    LOGGER.info('Pipeline完成，主流程 %.2f秒；含报告总耗时 %.2f秒', main_elapsed, result.elapsed_seconds)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'data/online_retail_ii.csv')
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).parent / 'outputs')
    parser.add_argument('--report', type=Path, default=Path(__file__).parent / 'validation_report.md')
    args = parser.parse_args()
    configure_logging(Path(__file__).parent / 'pipeline_log.txt')
    run_pipeline(args.input, args.output_dir, args.report)


if __name__ == '__main__':
    main()
