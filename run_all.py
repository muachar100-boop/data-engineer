"""整个笔试项目的一键运行入口：数据下载 → Q1 → Q2 → Q3 → Q4（离线）→ Q5。

设计原则：
- 每个模块独立 try/except，某个模块失败只记录并继续，最后打印汇总表；有失败时退出码为 1。
- 数据下载失败时直接停止：后面所有模块都依赖原始 CSV。
- Q2 需要本机 MySQL 8.0+ 与根目录 .env（MYSQL_HOST/PORT/USER/PASSWORD/DATABASE）。
- Q4 只做离线步骤，绝不调用付费 API：有 full_run/ 缓存时重建 extracted_issues.json，
  没有缓存时只校验已提交的 extracted_issues.json，并跑离线单元测试。付费全量见 extract_reviews.py --live。

用法（项目根目录）：
    python run_all.py                    # 全部模块
    python run_all.py --only q1 q3       # 只跑指定模块（数据下载总会先执行）
    python run_all.py --skip q2          # 跳过指定模块，例如本机没有 MySQL 时
"""

import argparse
import collections
import json
import logging
import re
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ("q1", "q2", "q3", "q4", "q5")
Q2_SQL_FILES = ("q2_1_translation_gap", "q2_2_monthly_metrics", "q2_3_top_sellers")

logger = logging.getLogger("run_all")


def sql_statements(text: str) -> list[str]:
    """把 .sql 文件拆成可逐条执行的语句：去掉块注释和整行 -- 注释，再按分号切分。

    只适用于本仓库的 SQL：字符串字面量里没有分号、-- 或 /*。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in text.split(";") if s.strip()]


def run_q1(paths):
    from q1_data_quality.q1_code import main as q1_main

    report = q1_main(paths["retail"])
    return f"已生成 {report.relative_to(ROOT)}、findings.md、customer_country_distribution.png"


def run_q2(paths):
    import pymysql
    from dotenv import dotenv_values

    from q2_sql_analysis.load_olist import load_olist

    # 导入是幂等的：表已存在且全字段指纹与 CSV 一致时跳过写入
    load_olist(paths["olist"])

    config = dotenv_values(ROOT / ".env")
    connection = pymysql.connect(
        host=config["MYSQL_HOST"], port=int(config["MYSQL_PORT"]),
        user=config["MYSQL_USER"], password=config["MYSQL_PASSWORD"],
        database=config["MYSQL_DATABASE"], charset="utf8mb4",
    )
    summary = []
    try:
        with connection.cursor() as cursor:
            for name in Q2_SQL_FILES:
                sql_path = ROOT / "q2_sql_analysis" / f"{name}.sql"
                statements = sql_statements(sql_path.read_text(encoding="utf-8"))
                if len(statements) != 1:
                    raise ValueError(f"{sql_path.name} 应只含 1 条查询，实际 {len(statements)} 条")
                start = time.perf_counter()
                cursor.execute(statements[0])
                rows = cursor.fetchall()
                elapsed = time.perf_counter() - start
                # 只把列名和前 5 行写进日志，便于和文档里的数字核对；不另存结果文件
                logger.info("%s：%d 行，%.2f 秒；列：%s", name, len(rows), elapsed,
                            [d[0] for d in cursor.description])
                for row in rows[:5]:
                    logger.info("    %s", tuple(str(v) for v in row))
                summary.append(f"{name} {len(rows)} 行")
    finally:
        connection.close()
    return "；".join(summary)


def run_q3(paths):
    from q3_etl_pipeline.pipeline import configure_logging, run_pipeline

    configure_logging(ROOT / "q3_etl_pipeline" / "pipeline_log.txt")
    result = run_pipeline(paths["retail"])
    return (f"sales_facts {len(result.sales_facts):,} 行、customer_features {len(result.customer_features):,} 行、"
            f"returns_log {len(result.returns_log):,} 行，耗时 {result.elapsed_seconds:.1f} 秒")


def check_q4_output(er) -> str:
    """没有 full_run/ 缓存时：逐条复核已提交的 extracted_issues.json（Schema + 原文连续引句）。"""
    records = er.load_records()
    by_id = collections.defaultdict(list)
    for r in records:
        by_id[r["review_id"]].append(r)
    output = json.loads(er.OUTPUT.read_text(encoding="utf-8"))
    # 同一 review_id 可能对应多个订单行，任一行通过即可
    invalid = [o.get("review_id") for o in output
               if not any(er.validate(o, r)["valid"] for r in by_id.get(o.get("review_id"), []))]
    if invalid:
        raise AssertionError(f"extracted_issues.json 有 {len(invalid)} 条未通过校验，例如 {invalid[:5]}")
    missing = len(set(by_id) - {o["review_id"] for o in output})
    return (f"无 full_run/ 缓存，已校验提交的 extracted_issues.json：{len(output):,} 条全部通过，"
            f"{missing} 个 review_id 无输出（死信）")


def run_q4(paths):
    from q4_llm_extraction import extract_reviews as er

    if (er.STATE / "results.jsonl").exists():
        report = er.assemble_offline()
        message = f"由 full_run/ 缓存重建 extracted_issues.json：{report['output_records']:,} 条"
    else:
        message = check_q4_output(er)

    # 离线单元测试：测试桩代替 API，不联网、不读 Key
    suite = unittest.defaultTestLoader.discover(str(ROOT / "q4_llm_extraction"), pattern="test_extract_reviews.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise AssertionError(f"Q4 单元测试失败：{len(result.failures)} 个失败，{len(result.errors)} 个错误")
    return f"{message}；单元测试 {result.testsRun} 个通过"


def run_q5(paths):
    from q5_system_design.q5_baseline_stats import OUT, main as q5_main

    q5_main()
    return f"已写入 {OUT.relative_to(ROOT)}"


STEPS = {"q1": run_q1, "q2": run_q2, "q3": run_q3, "q4": run_q4, "q5": run_q5}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="+", choices=MODULES, help="只运行这些模块")
    parser.add_argument("--skip", nargs="+", choices=MODULES, default=[], help="跳过这些模块")
    args = parser.parse_args(argv)
    selected = [m for m in (args.only or MODULES) if m not in args.skip]

    log_path = ROOT / f"run_all_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")],
    )
    logger.info("运行模块：%s；日志：%s", " ".join(selected), log_path.name)

    from download_data import download_all

    try:
        paths = download_all()
    except Exception:
        logger.exception("数据准备失败，后续模块都依赖原始 CSV，停止运行")
        return 1

    results = []
    for name in selected:
        logger.info("========== %s 开始 ==========", name.upper())
        start = time.perf_counter()
        try:
            detail = STEPS[name](paths)
            status = "成功"
        except Exception as error:
            logger.exception("%s 失败，已跳过，继续后面的模块", name.upper())
            detail = f"{type(error).__name__}: {error}"
            status = "失败"
        results.append((name, status, time.perf_counter() - start, detail))

    logger.info("========== 汇总 ==========")
    for name, status, elapsed, detail in results:
        logger.info("%s | %s | %6.1f 秒 | %s", name.upper(), status, elapsed, detail)
    failed = [name for name, status, *_ in results if status == "失败"]
    if failed:
        logger.error("失败模块：%s（详见 %s）", " ".join(failed).upper(), log_path.name)
        return 1
    logger.info("全部模块运行成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
