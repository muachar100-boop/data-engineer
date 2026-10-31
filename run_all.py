"""整个笔试项目的一键运行入口，接入数据准备、Q3 ETL、Olist 原始 CSV 导入和 Q5 基线统计。"""

import logging
from pathlib import Path

from download_data import download_all
from q2_sql_analysis.load_olist import load_olist
from q3_etl_pipeline.pipeline import configure_logging, run_pipeline
from q5_system_design.q5_baseline_stats import main as q5_baseline_stats


def main():
    paths = download_all()

    configure_logging(Path(__file__).resolve().parent / "q3_etl_pipeline/pipeline_log.txt")
    run_pipeline(paths["retail"])

    load_olist(paths["olist"])

    # Q5：为设计文档计算容量与 LLM 成本基线；失败不影响其他模块
    try:
        q5_baseline_stats()
    except Exception:
        logging.exception("Q5 基线统计失败，已跳过")

    # Q2.1–Q2.4 暂未接入
    logging.info("Retail 文件：%s", paths["retail"])
    logging.info("Olist 目录：%s", paths["olist"])


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(message)s",
    )
    main()