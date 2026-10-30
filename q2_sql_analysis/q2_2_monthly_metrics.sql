-- Q2.2：平台真实成交分析（MySQL 8.0+）
-- 月份按下单时间归属；取消率以百分数输出，例如 2.35 表示 2.35%。
-- 原始层字段为文本：空日期转换为 NULL，金额显式转换为 DECIMAL。
-- 先把订单明细汇总到订单粒度，避免一对多连接放大订单数和平均送达时长。
WITH item_totals AS (
    SELECT
        order_id,
        SUM(
            CAST(NULLIF(price, '') AS DECIMAL(18, 2))
            + CAST(NULLIF(freight_value, '') AS DECIMAL(18, 2))
        ) AS order_gmv
    FROM order_items
    GROUP BY order_id
),
order_base AS (
    SELECT
        order_id,
        order_status,
        STR_TO_DATE(
            NULLIF(order_purchase_timestamp, ''), '%Y-%m-%d %H:%i:%s'
        ) AS purchased_at,
        STR_TO_DATE(
            NULLIF(order_delivered_customer_date, ''), '%Y-%m-%d %H:%i:%s'
        ) AS delivered_at
    FROM orders
)
SELECT
    DATE_FORMAT(o.purchased_at, '%Y-%m') AS month,
    COUNT(*) AS total_orders,
    SUM(CASE WHEN o.order_status = 'delivered' THEN 1 ELSE 0 END) AS delivered_orders,
    SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END) AS canceled_orders,
    ROUND(
        100.0 * SUM(CASE WHEN o.order_status = 'canceled' THEN 1 ELSE 0 END)
        / COUNT(*),
        2
    ) AS cancel_rate,
    ROUND(
        SUM(CASE
            WHEN o.order_status = 'delivered' THEN COALESCE(i.order_gmv, 0)
            ELSE 0
        END),
        2
    ) AS delivered_gmv,
    ROUND(
        AVG(CASE
            WHEN o.order_status = 'delivered'
                 AND o.delivered_at IS NOT NULL
                 AND o.purchased_at IS NOT NULL
            THEN TIMESTAMPDIFF(SECOND, o.purchased_at, o.delivered_at) / 86400.0
            ELSE NULL
        END),
        2
    ) AS avg_delivery_days
FROM order_base AS o
LEFT JOIN item_totals AS i ON i.order_id = o.order_id
GROUP BY DATE_FORMAT(o.purchased_at, '%Y-%m')
ORDER BY month;
