"""Q5 成本与容量估算的数据支撑：从 Olist 原始 CSV 计算设计文档里用到的比例。

只读 data/olist/ 下 3 张表，纯 pandas 向量化计算，不连 MySQL、不调 API。
输出：q5_system_design/baseline_stats.json（供文档引用）并打印中文摘要。

口径说明（面试可能追问）：
- 有文本：review_comment_message 去首尾空白后非空。
- 延迟送达：沿用 Q2 口径，delivered 订单 order_delivered_customer_date > order_estimated_delivery_date；
  另给"按天比较"口径，因为预计送达日期只精确到天（时间都是 00:00:00）。
- 卖家发货超时：order_delivered_carrier_date > 该订单最早的 shipping_limit_date。
- 状态事件数：Olist 只有最终状态，没有状态变更日志，用"非空里程碑时间戳个数"近似
  （下单/审核/交承运商/送达），这是下限估计，真实系统里 invoiced/processing 等中间状态会更多。
- LLM 单条成本：引用 Q4 v6 实测（平均输入 323.67、输出 115.50 token，gpt-4.1-mini 冻结费率）。
"""

import json
import logging
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OLIST = ROOT / "data" / "olist"
OUT = Path(__file__).resolve().parent / "baseline_stats.json"

# Q4 v6 实测，见 q4_llm_extraction/cost_report.md
Q4_IN_TOKENS, Q4_OUT_TOKENS = 323.67, 115.50
PRICE_IN, PRICE_OUT = 0.40, 1.60  # USD / 百万 token，gpt-4.1-mini
SHORT_WORDS = 3  # ≤3 个词的短评视为可走规则（如 "ruim"、"não recebi"）


def load():
    reviews = pd.read_csv(OLIST / "olist_order_reviews_dataset.csv")
    orders = pd.read_csv(
        OLIST / "olist_orders_dataset.csv",
        parse_dates=["order_purchase_timestamp", "order_approved_at",
                     "order_delivered_carrier_date", "order_delivered_customer_date",
                     "order_estimated_delivery_date"],
    )
    items = pd.read_csv(OLIST / "olist_order_items_dataset.csv", parse_dates=["shipping_limit_date"])
    return reviews, orders, items


def review_stats(reviews: pd.DataFrame, n_orders: int) -> dict:
    text = reviews["review_comment_message"].fillna("").str.strip()
    has_text = text.ne("")
    low = reviews["review_score"].le(3)
    very_low = reviews["review_score"].le(2)
    low_text = text[low & has_text]
    norm = low_text.str.lower().str.replace(r"\s+", " ", regex=True)
    words = norm.str.split().str.len()
    n = len(reviews)
    return {
        "reviews": n,
        "reviews_per_order": round(n / n_orders, 4),
        "has_text_rate": round(has_text.mean(), 4),
        "low_score_rate(<=3)": round(low.mean(), 4),
        "low_score_and_text_rate": round((low & has_text).mean(), 4),
        "very_low_score_and_text_rate(<=2)": round((very_low & has_text).mean(), 4),
        "low_text_count": int(len(low_text)),
        "low_text_short_share(<=3词)": round(words.le(SHORT_WORDS).mean(), 4),
        "low_text_exact_dup_share": round(norm.duplicated().mean(), 4),
        "low_text_need_llm_rate_of_all_reviews": round(
            (low & has_text).sum() * (1 - words.le(SHORT_WORDS).mean()) / n, 4),
    }


def delivery_stats(orders: pd.DataFrame, items: pd.DataFrame) -> dict:
    d = orders[orders["order_status"].eq("delivered") & orders["order_delivered_customer_date"].notna()]
    late_ts = d["order_delivered_customer_date"] > d["order_estimated_delivery_date"]
    late_day = d["order_delivered_customer_date"].dt.normalize() > d["order_estimated_delivery_date"]
    est_midnight = d["order_estimated_delivery_date"].dt.time.astype(str).eq("00:00:00").mean()

    limit = items.groupby("order_id")["shipping_limit_date"].min()
    o = orders.set_index("order_id").join(limit, how="inner")
    o = o[o["order_delivered_carrier_date"].notna()]
    handoff_late = (o["order_delivered_carrier_date"] > o["shipping_limit_date"]).mean()

    # 未送达却已超预计日期（数据集截止时的"存量异常"）
    snapshot = orders["order_purchase_timestamp"].max()
    open_ = orders[orders["order_delivered_customer_date"].isna()
                   & ~orders["order_status"].isin(["canceled", "unavailable"])]
    open_overdue = (open_["order_estimated_delivery_date"] < snapshot).sum()
    return {
        "delivered_orders": int(len(d)),
        "late_rate_timestamp(Q2口径)": round(late_ts.mean(), 4),
        "late_rate_by_day": round(late_day.mean(), 4),
        "estimated_date_is_midnight_share": round(est_midnight, 4),
        "seller_handoff_late_rate": round(handoff_late, 4),
        "open_orders_overdue_at_snapshot": int(open_overdue),
        "status_distribution": orders["order_status"].value_counts().to_dict(),
    }


def volume_stats(orders: pd.DataFrame) -> dict:
    ts = orders["order_purchase_timestamp"]
    milestones = orders[["order_purchase_timestamp", "order_approved_at",
                         "order_delivered_carrier_date", "order_delivered_customer_date"]].notna().sum(axis=1)
    # 只看订单量稳定期（2017-06 ~ 2018-08），避开冷启动和数据集尾部截断
    stable = ts[(ts >= "2017-06-01") & (ts < "2018-09-01")]
    daily = stable.dt.floor("D").value_counts()
    hourly = stable.dt.floor("h").value_counts()
    hours = pd.date_range(stable.min().floor("h"), stable.max().floor("h"), freq="h")
    hourly = hourly.reindex(hours, fill_value=0)
    return {
        "orders": int(len(orders)),
        "avg_milestone_events_per_order": round(milestones.mean(), 3),
        "stable_period_avg_orders_per_day": round(daily.mean(), 1),
        "peak_day_over_avg_day": round(daily.max() / daily.mean(), 2),
        "peak_day": str(daily.idxmax().date()),
        "peak_hour_over_avg_hour": round(hourly.max() / hourly.mean(), 2),
        "hour_of_day_share_max": round(ts.dt.hour.value_counts(normalize=True).max(), 4),
    }


def cost_projection(r: dict, daily_orders: int = 1_000_000) -> dict:
    per_call = (Q4_IN_TOKENS * PRICE_IN + Q4_OUT_TOKENS * PRICE_OUT) / 1e6
    reviews_day = daily_orders * r["reviews_per_order"]
    steps = {
        "A_全部评价过LLM": reviews_day,
        "B_只处理有文本": reviews_day * r["has_text_rate"],
        "C_只处理<=3星且有文本": reviews_day * r["low_score_and_text_rate"],
        "D_再剔除<=3词短评": reviews_day * r["low_text_need_llm_rate_of_all_reviews"],
        "E_再去重(精确重复)": reviews_day * r["low_text_need_llm_rate_of_all_reviews"]
                               * (1 - r["low_text_exact_dup_share"]),
    }
    return {
        "per_call_usd": round(per_call, 8),
        "daily_orders": daily_orders,
        "tiers": {k: {"calls_per_day": round(v), "usd_per_day": round(v * per_call, 2),
                      "usd_per_year": round(v * per_call * 365)} for k, v in steps.items()},
    }


def main():
    reviews, orders, items = load()
    r = review_stats(reviews, len(orders))
    stats = {
        "reviews": r,
        "delivery": delivery_stats(orders, items),
        "volume": volume_stats(orders),
        "llm_cost_1m_per_day": cost_projection(r),
    }
    OUT.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("Q5 基线统计已写入 %s", OUT)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    main()
