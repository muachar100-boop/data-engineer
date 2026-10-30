-- Q2.1：找出没有有效英文对照的商品类别，并统计商品数。
-- MySQL 8.0+；在已导入 Olist 的数据库中执行。
-- 原始层保留空字符串：英文为 NULL、空字符串或纯空格均视为无有效翻译。
-- 保留商品类别为空的分组，便于运行后区分类别缺失与翻译缺失。
SELECT
    p.product_category_name,
    COUNT(*) AS product_count
FROM products AS p
WHERE NOT EXISTS (
    SELECT 1
    FROM product_category_name_translation AS t
    WHERE t.product_category_name = p.product_category_name
      AND NULLIF(TRIM(t.product_category_name_english), '') IS NOT NULL
)
GROUP BY p.product_category_name
ORDER BY product_count DESC, p.product_category_name;

/*
Q2.1 进阶：缺失翻译的处理方案及利弊

运行结果：
- 类别名为空：610 个商品，属于商品类别缺失，无法直接补翻译。
- portateis_cozinha_e_preparadores_de_alimentos：10 个商品，缺少英文对照。
- pc_gamer：3 个商品，缺少英文对照。
合计 623 个商品；严格区分为 2 个具名类别缺少翻译，以及 610 个商品缺少类别信息。

方案 1：人工核实并补齐映射
做法：请熟悉葡萄牙语或业务分类的人确认两个具名类别的英文名，维护补充映射表。
      对类别为空的商品，先从来源系统补回类别，再翻译；无法恢复时保留缺失。
优点：准确性高，命名统一，适合当前仅两个具名类别需要补译的情况。
缺点：需要人工和业务支持；空白类别可能无法追溯，类别增多后维护成本会上升。

方案 2：自动生成候选，人工审核
做法：使用机器翻译或 LLM 生成英文候选，经审核后写入补充映射表，并记录来源。
      空白类别只有在有商品描述等额外信息时，才考虑自动分类，不能凭空推断。
优点：减少人工翻译工作，适合持续新增类别。
缺点：可能误译、分类不准确或命名不一致；仍需要审核，且依赖可用的商品信息。

方案 3：暂时保留缺失，设置展示兜底
做法：有类别名但无英文时显示葡萄牙语原名，并标记 translation_missing；
      类别为空时显示 Unknown，并标记 category_missing。
优点：实现简单，保留全部商品，报表可以继续使用，同时明确暴露数据问题。
缺点：没有真正补齐翻译；展示语言不统一，Unknown 无法支持细分类别分析。

建议：先用方案 3 保证报表完整，再用方案 1 补齐两个具名类别的翻译。
      对 610 个空白类别单独追查来源；无法恢复时保留 Unknown，不随意归入现有类别。
      保留原始表，通过补充映射表或分析视图提供修正字段，并分别统计类别缺失和
      翻译缺失，以验证修复效果。以上为处理建议，本文件不执行数据修复。
*/
