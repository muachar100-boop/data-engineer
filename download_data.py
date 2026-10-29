"""下载笔试所需数据，并整理到 README 指定的位置。"""

import logging
import shutil
from pathlib import Path

import kagglehub


# 根据脚本的位置确定项目根目录，避免受终端当前目录影响
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"

RETAIL_DATASET = "mashlyn/online-retail-ii-uci"
OLIST_DATASET = "olistbr/brazilian-ecommerce"

OLIST_FILES = (
    "olist_customers_dataset.csv",
    "olist_geolocation_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "product_category_name_translation.csv",
)

logger = logging.getLogger(__name__)


def file_ready(path: Path) -> bool:
    """文件存在且非空时，视为已经准备好。"""
    return path.is_file() and path.stat().st_size > 0


def find_csv(folder: Path, filename: str) -> Path:
    """查找下载目录中的 CSV，兼容文件名大小写和嵌套目录。"""
    matches = [
        path
        for path in folder.rglob("*")
        if path.is_file() and path.name.lower() == filename.lower()
    ]

    if len(matches) != 1:
        raise FileNotFoundError(
            f"在 {folder} 中寻找 {filename}，找到 {len(matches)} 个匹配文件，"
            "请检查下载内容。"
        )

    if not file_ready(matches[0]):
        raise ValueError(f"下载的文件为空：{matches[0]}")

    return matches[0]


def copy_csv(source: Path, target: Path) -> None:
    """先复制到临时文件，复制完成后再替换目标文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")

    try:
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    finally:
        # 如果复制中断，清理本次留下的临时文件
        temporary.unlink(missing_ok=True)

    logger.info("已保存：%s", target.relative_to(PROJECT_ROOT))


def prepare_retail() -> Path:
    """准备 Online Retail II，并统一目标文件名。"""
    target = DATA_DIR / "online_retail_ii.csv"

    if file_ready(target):
        logger.info("文件已存在，跳过：%s", target.relative_to(PROJECT_ROOT))
        return target

    # 如果此前手动放入了大写 II 的文件，优先使用本地文件
    local_matches = [
        path
        for path in DATA_DIR.rglob("*")
        if path.is_file()
        and path.name.lower() == target.name
        and file_ready(path)
    ]

    if len(local_matches) == 1:
        source = local_matches[0]
        logger.info("使用已有的本地 Retail 文件")
    else:
        logger.info("准备下载 Online Retail II……")
        cache_dir = Path(kagglehub.dataset_download(RETAIL_DATASET))
        source = find_csv(cache_dir, target.name)

    copy_csv(source, target)
    return target


def prepare_olist() -> Path:
    """准备 Olist 的 9 张表，只复制缺失或为空的文件。"""
    target_dir = DATA_DIR / "olist"
    target_dir.mkdir(parents=True, exist_ok=True)

    missing_files = [
        name for name in OLIST_FILES
        if not file_ready(target_dir / name)
    ]

    if not missing_files:
        logger.info("Olist 的 9 个文件均已存在，跳过下载")
        return target_dir

    logger.info("Olist 还缺 %d 个文件，准备获取数据……", len(missing_files))

    # kagglehub 会利用自己的本地缓存，不强制重新下载
    cache_dir = Path(kagglehub.dataset_download(OLIST_DATASET))

    for filename in missing_files:
        source = find_csv(cache_dir, filename)
        copy_csv(source, target_dir / filename)

    return target_dir


def download_all() -> dict[str, Path]:
    """供命令行或后续 run_all.py 调用的统一入口。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    retail_path = prepare_retail()
    olist_dir = prepare_olist()

    expected_files = [
        retail_path,
        *(olist_dir / name for name in OLIST_FILES),
    ]

    invalid_files = [
        str(path) for path in expected_files
        if not file_ready(path)
    ]

    if invalid_files:
        raise RuntimeError(f"数据准备未完成：{invalid_files}")

    logger.info("数据准备完成，10 个 CSV 均已就位")

    return {
        "retail": retail_path,
        "olist": olist_dir,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(message)s",
    )

    try:
        download_all()
    except Exception:
        logger.exception("数据准备失败，请根据上面的错误信息检查")
        raise