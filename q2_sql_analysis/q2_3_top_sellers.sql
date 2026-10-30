-- Q2.3：2017 年 GMV TOP 20 卖家（MySQL 8.0+）
-- 年份按下单时间；GMV 延续 Q2.2，仅统计 delivered，含商品金额和运费。
-- 订单数、独立客户数、取消率和评价范围：卖家参与的全部 2017 年订单。
-- 独立客户按 customer_unique_id；同一订单的评分归属该订单参与的每个卖家。
-- 延迟率：已送达且实际/预计时间齐全的订单中，实际送达时间 > 预计时间的比例。
-- 两个 rate 字段均为百分数；无有效评价或无有效送达样本时返回 NULL。
-- 评价保留答复时间最新的一条，创建时间、review_id 和评分用于进一步打破并列。
WITH orders_2017 AS (
    SELECT
        order_id,
        customer_id,
        order_status,
        STR_TO_DATE(NULLIF(order_delivered_customer_date, ''),
                    '%Y-%m-%d %H:%i:%s') AS delivered_at,
        STR_TO_DATE(NULLIF(order_estimated_delivery_date, ''),
                    '%Y-%m-%d %H:%i:%s') AS estimated_at
    FROM orders
    WHERE order_purchase_timestamp >= '2017-01-01 00:00:00'
      AND order_purchase_timestamp < '2018-01-01 00:00:00'
),
seller_orders AS (
    -- 一个卖家在一个订单中的全部商品先汇总，避免重复计数及平均值加权。
    SELECT
        i.seller_id,
        i.order_id,
        SUM(CAST(NULLIF(i.price, '') AS DECIMAL(18, 2))
          + CAST(NULLIF(i.freight_value, '') AS DECIMAL(18, 2))) AS seller_order_gmv
    FROM order_items AS i
    INNER JOIN orders_2017 AS o ON o.order_id = i.order_id
    GROUP BY i.seller_id, i.order_id
),
review_ranked AS (
    SELECT
        r.order_id,
        CAST(NULLIF(r.review_score, '') AS UNSIGNED) AS review_score,
        ROW_NUMBER() OVER (
            PARTITION BY r.order_id
            ORDER BY NULLIF(r.review_answer_timestamp, '') DESC,
                     NULLIF(r.review_creation_date, '') DESC,
                     r.review_id DESC,
                     CAST(NULLIF(r.review_score, '') AS UNSIGNED) DESC
        ) AS rn
    FROM order_reviews AS r
    INNER JOIN orders_2017 AS o ON o.order_id = r.order_id
),
review_dedup AS (
    SELECT order_id, review_score
    FROM review_ranked
    WHERE rn = 1
),
seller_metrics AS (
    SELECT
        so.seller_id,
        s.seller_state,
        SUM(CASE WHEN o.order_status = 'delivered'
                 THEN so.seller_order_gmv ELSE 0 END) AS gmv_2017,
        COUNT(*) AS order_count,
        COUNT(DISTINCT NULLIF(c.customer_unique_id, '')) AS unique_customers,
        AVG(r.review_score) AS avg_review_score,
        100.0 * AVG(CASE
            WHEN o.order_status = 'delivered'
                 AND o.delivered_at IS NOT NULL
                 AND o.estimated_at IS NOT NULL
            THEN CASE WHEN o.delivered_at > o.estimated_at THEN 1 ELSE 0 END
            ELSE NULL
        END) AS late_delivery_rate,
        100.0 * SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END)
              / COUNT(*) AS cancel_rate
    FROM seller_orders AS so
    INNER JOIN orders_2017 AS o ON o.order_id = so.order_id
    LEFT JOIN sellers AS s ON s.seller_id = so.seller_id
    LEFT JOIN customers AS c ON c.customer_id = o.customer_id
    LEFT JOIN review_dedup AS r ON r.order_id = so.order_id
    GROUP BY so.seller_id, s.seller_state
),
seller_ranked AS (
    -- 在全部卖家中先算州内排名，再取全国 TOP 20；按未舍入的 GMV 排名。
    SELECT
        seller_metrics.*,
        RANK() OVER (
            PARTITION BY seller_state ORDER BY gmv_2017 DESC
        ) AS state_rank
    FROM seller_metrics
)
SELECT
    seller_id,
    seller_state,
    ROUND(gmv_2017, 2) AS gmv_2017,
    order_count,
    unique_customers,
    ROUND(avg_review_score, 2) AS avg_review_score,
    ROUND(late_delivery_rate, 2) AS late_delivery_rate,
    ROUND(cancel_rate, 2) AS cancel_rate,
    state_rank
FROM seller_ranked
ORDER BY gmv_2017 DESC, seller_id
LIMIT 20;
