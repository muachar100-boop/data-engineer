"""整个笔试项目的一键运行入口，目前接入数据准备。"""

import logging

from download_data import download_all


def main():
    paths = download_all()

    # 后续在这里接入 Q1、Q2、Q3 等模块
    logging.info("Retail 文件：%s", paths["retail"])
    logging.info("Olist 目录：%s", paths["olist"])


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(message)s",
    )
    main()