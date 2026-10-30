# Q2.4 - 慢查询优化分析

## 原始 SQL

```sql
SELECT 
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(oi.price) AS revenue,
    AVG(r.review_score) AS avg_score
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.customer_id
LEFT JOIN order_items oi ON o.order_id = oi.order_id
LEFT JOIN order_reviews r ON o.order_id = r.order_id
WHERE o.order_status = 'delivered'
  AND o.order_purchase_timestamp >= '2017-01-01'
  AND LOWER(c.customer_city) LIKE '%sao%'
GROUP BY c.customer_state
ORDER BY revenue DESC;
```

---

## 第 1 问：性能问题及原因

1. **两个一对多表直接连接，产生行数乘积。** 一个订单有 m 条明细和 n 条评价时，连接后产生 m×n 行。增加的中间行消耗连接、聚合、排序及临时表资源，也导致金额重复累计。
2. **城市条件难以使用普通 B-tree 索引进行范围定位。** `LOWER(customer_city)` 对字段逐行计算；即使建立小写字段的函数索引，`LIKE '%sao%'` 的前导通配符仍没有可定位的固定前缀。不能把它擅自改成 `LIKE 'sao%'` 或城市等值匹配，因为筛选含义会变化。全文检索通常是词项检索，也不是任意子串匹配的直接替代。
3. **过滤和连接需要合适的访问路径。** 无状态/时间联合索引时可能扫描大量订单；无连接键索引时可能增加扫描、哈希构建或重复查找成本。状态单列通常选择性较低，建议等值状态在前、时间范围在后。实际收益取决于筛选比例，范围过宽时全扫描也可能是合理选择。
4. **`COUNT(DISTINCT)` 在放大后的中间结果上去重。** 它能修正订单数，但不能修正收入或评分，还增加去重开销。先保证最终每个订单一行，再用 `COUNT(*)`。
5. **大中间结果的聚合及排序可能使用临时表并落盘。** 应优先减少进入最终聚合的行数，而不是只增加内存。`ORDER BY SUM(...)` 是动态聚合结果排序，给原始 price 加索引不能直接消除它；州的数量很少，最终排序本身未必是主要瓶颈，应看执行计划。

当前原始层所有字段为 LONGTEXT，没有主键或索引，且金额需要显式转换。规模扩大后，建议在独立分析层将 ID 定义为 CHAR(32)、时间定义为 DATETIME、金额定义为 DECIMAL、州定义为 CHAR(2)，校验唯一性后为 orders.order_id 和 customers.customer_id 设置主键。不修改原始 CSV 或为了优化而直接覆盖原始表。

## 第 2 问：优化 SQL 与索引

可运行查询见 `q2_4_optimized.sql`：

```sql
-- Q2.4：MySQL 8.0+，只读查询；先筛选，再将两个一对多表分别压到订单粒度。
-- 保持原题筛选条件：2017-01-01 起（包含 2018 年），城市包含 sao。
-- revenue 保持原题商品金额口径，不含运费；avg_score 为每订单最新评价的平均分。
WITH filtered_orders AS (
    SELECT o.order_id, c.customer_state
    FROM orders AS o
    INNER JOIN customers AS c ON c.customer_id = o.customer_id
    WHERE o.order_status = 'delivered'
      AND o.order_purchase_timestamp >= '2017-01-01'
      AND LOWER(c.customer_city) LIKE '%sao%'
),
item_totals AS (
    SELECT
        oi.order_id,
        SUM(CAST(NULLIF(oi.price, '') AS DECIMAL(18, 2))) AS order_revenue
    FROM order_items AS oi
    INNER JOIN filtered_orders AS f ON f.order_id = oi.order_id
    GROUP BY oi.order_id
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
    INNER JOIN filtered_orders AS f ON f.order_id = r.order_id
),
review_dedup AS (
    SELECT order_id, review_score
    FROM review_ranked
    WHERE rn = 1
)
SELECT
    f.customer_state,
    COUNT(*) AS orders,
    SUM(i.order_revenue) AS revenue,
    ROUND(AVG(r.review_score), 2) AS avg_score
FROM filtered_orders AS f
LEFT JOIN item_totals AS i ON i.order_id = f.order_id
LEFT JOIN review_dedup AS r ON r.order_id = f.order_id
GROUP BY f.customer_state
ORDER BY revenue DESC, f.customer_state;
```

执行步骤：

1. 先筛选满足订单状态、下单时间及城市条件的订单。原查询在 WHERE 中要求城市匹配，会排除 customers 未匹配的行，因此这里改为 INNER JOIN，保持筛选含义；不能把所有 LEFT JOIN 都随意改成 INNER JOIN。
2. 仅针对候选订单汇总商品明细，使每个订单一行。
3. 仅针对候选订单为评价排序，每个订单保留最新答复的一条，规则与 Q2.3 一致；无评价订单仍保留，NULL 不参与平均评分。
4. 在订单粒度连接并按州汇总，避免多商品订单重复加权评分。

CTE 表达逻辑阶段，不保证 MySQL 一定按书写顺序执行或物化；实际合并、物化、扫描方式需通过 EXPLAIN ANALYZE 验证。该查询依赖 orders.order_id 和 customers.customer_id 唯一，验证后才能安全用 COUNT(*)。

可选索引见 `q2_4_indexes.sql`，当前 LONGTEXT 原始表使用前缀索引：

```sql
-- 可选索引建议，需手动执行；本文件不是查询运行的前置强制步骤。
-- 先执行 SHOW INDEX FROM orders/customers/order_items/order_reviews（分别执行）。
-- 不重复创建等价索引。当前原始层使用 LONGTEXT，必须指定索引前缀。
-- 假定 ID 为固定 32 字符、状态 <=16 字符、时间为固定 19 字符。
CREATE INDEX idx_q24_orders_status_purchase
    ON orders (order_status(16), order_purchase_timestamp(19));
CREATE INDEX idx_q24_customers_id ON customers (customer_id(32));
CREATE INDEX idx_q24_items_order ON order_items (order_id(32));
CREATE INDEX idx_q24_reviews_order ON order_reviews (order_id(32));

-- 更新统计信息，随后用 EXPLAIN ANALYZE 检查实际访问路径。
ANALYZE TABLE orders, customers, order_items, order_reviews;
```

- orders 的联合索引支持状态等值加时间范围筛选；范围列后的列一般不能继续缩小连续扫描区间，不应声称再拼上 ID 就一定有同等过滤收益。
- customers.customer_id 支持订单查找客户；items/reviews.order_id 支持按候选订单访问关联数据。优化器也可能选择哈希连接或全扫描，并非建索引后必然使用。
- 不给城市列增加一个无助于当前子串搜索的普通索引。若大规模频繁查询同样的城市条件，可在分析层维护规范城市及匹配标记/城市集合；需保证与原条件语义一致并维护更新。
- 索引占磁盘、增加写入成本；先 SHOW INDEX 检查是否已有等价索引。LONGTEXT 必须指定前缀；改为合理字段类型后可以使用全列索引。前缀索引仍需检查完整字段，不等同于覆盖索引或唯一约束。

### 实测方法（待用户运行）

分别在原查询和优化查询前加 `EXPLAIN ANALYZE`，语法为 `EXPLAIN ANALYZE WITH ... SELECT ...`；它会实际执行查询。对比相同数据库、相同数据量、相近缓存状态，重复测试，并记录根节点总耗时、各节点 actual rows × loops、连接方式、索引使用和临时表情况。不要把计划估算 rows 当成实际扫描行数，完整 SQL 的实际扫描行数可结合 Performance Schema 的 ROWS_EXAMINED。

| 指标 | 原查询 | 优化查询 |
|---|---|---|
| 执行时间 | 待实测 | 待实测 |
| 实际扫描行数 | 待实测 | 待实测 |
| 连接中间结果行数 | 待实测 | 待实测 |

本地原始规模的结果不能直接证明 100 倍数据量下的性能；不预填加速倍数。

## 第 3 问：潜在统计错误

某个订单的商品价格为 100 和 50，有两条评价。原连接产生四行，价格分别为 100、100、50、50，因此 SUM(price)=300，而正确商品金额为 150。COUNT(DISTINCT order_id) 只修正订单计数，不能修正这个乘法放大。不能改成 SUM(DISTINCT price)：不同商品甚至不同订单可能价格相同，会被错误去重。

只预汇总商品后再直接连接多条评价，也会重复订单总额；必须把两个一对多关系分别压到订单粒度，或分别聚合最终指标后再关联。原 AVG(review_score) 同时会受商品条数和多次评价影响，不能视为每订单一票的平均评分；优化查询明确使用每订单最新评价。

另外，price 仅为商品金额，不含 freight_value，不是买家含运费的订单总额，也不能直接认定为扣退款、佣金后的净收入。本题优化查询保持原 price 口径；如果业务要求与 Q2.2 一致的含运费 GMV，将 item_totals 中金额改为：

```sql
SUM(
    CAST(NULLIF(oi.price, '') AS DECIMAL(18, 2))
    + CAST(NULLIF(oi.freight_value, '') AS DECIMAL(18, 2))
) AS order_revenue
```

保持筛选时间为 2017-01-01 起，而非仅 2017 年。由于同时修正统计错误，优化前后 revenue 和 avg_score 可能不同，不能以与原错误查询完全相等作为验收标准。应要求同一州 orders 一致，并独立核对修正后的商品金额和去重评分。

## 参考

- [MySQL 8.4：范围优化](https://dev.mysql.com/doc/refman/8.4/en/range-optimization.html)
- [MySQL 8.0：CREATE INDEX 与前缀索引](https://dev.mysql.com/doc/refman/8.0/en/create-index.html)
