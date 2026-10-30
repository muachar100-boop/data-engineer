# Q3.1 第一阶段：原始检查与基础清洗

依据本地 README Q3.1、Q1代码和三份分析文档，并重新读取实际 CSV。只生成本报告和日志；所有明细保留在返回对象中。尚未实现完整 Pipeline。

## 字段类型与缺失

| 原始字段 | 读取类型 | 标准类型 | 原始缺失数 |
| --- | --- | --- | --- |
| Invoice | string | string | 0 |
| StockCode | string | string | 0 |
| Description | string | string | 4382 |
| Quantity | string | float64 | 0 |
| Price | string | float64 | 0 |
| InvoiceDate | string | datetime64[us] | 0 |
| Customer ID | string | string | 243007 |
| Country | string | string | 0 |

输入以文本保真；空串和空白转缺失，数值显式解析，日期严格按秒格式解析。保留原始 DataFrame 和 source_row；客户 ID 去数字后的 .0，商品只去首尾空白，另设大写分类键。描述不填充、不按描述合并商品。

## 清洗规则及业务权衡

- 正向销售：纯数字发票、已识别商品、数量和单价大于0、核心字段有效。保留匿名客户和统计极值。金额是数量×价格，不代表已支付或最终净收入。
- 取消记录：所有 C 发票独立保留，包括重复、匿名、费用及正数量例外；本阶段不匹配、不取绝对值、不扣减正单。不能将每条 C 行直接解释成实物退货。
- A 发票、B 代码及 Adjust bad debt 单列坏账。费用、折扣、手工调整、礼券、样品、测试不混入商品销售；礼券可能涉及递延收入，不能当实物商品收入。
- 非C负数量包含库存盘点、损坏等线索，隔离待核查，不凭负号认定退货。零价可能为赠品、库存调整或缺价，保留审计但不进付费销售。负价格不取绝对值。
- 整行重复不等于重复导入：缺少可靠明细键，默认保留并标记，另给去重敏感性。此决定可能高估销售，需来源系统确认后再定稿。不会按 Invoice+StockCode 粗暴去重。
- 代码格式不能替代业务含义：PADS、DCGSSBOY/GIRL 和有商品描述证据的 DCGS/SP 代码作为商品例外；未知代码隔离。新代码需补充证据后再纳入。
- 极端数量与价格仅标记，不截尾、不缩尾；80995 和74215 均有随后反向冲销证据，不能仅删负行，也不能未经确认认定正单录错。后续净销售需结合退货匹配解释。
- 匿名记录用于 BI，不补造客户，不参与 RFM。Q1 曾建议退款后净 RFM；Q3 明确要求基于正向真实销售，应先遵守题目，净 RFM 可另作扩展，不能静默改口径。
- 退货匹配后续需处理时间先后、重复候选、缺客户、部分/多次退货和数量占用；不得将未知客户互相匹配。当前保留全部候选和来源行号。

## 实际统计

| 指标 | 结果 |
| --- | --- |
| 原始行数 | 1067371 |
| 原始带符号金额 | 19287250.568 |
| 整行重复多余行 | 34335 |
| 重复组全部成员 | 67242 |
| Quantity负数 | 22950 |
| Quantity零 | 0 |
| Price负数 | 5 |
| Price零 | 6202 |
| 非C负数量 | 3457 |
| C中正数量 | 1 |
| C中客户缺失 | 750 |
| C中重复多余行 | 390 |
| Quantity IQR提示 | 116489 |
| Price IQR提示 | 68105 |
| 核心字段无效 | 0 |
| 正向销售行数 | 1037009 |
| 正向销售金额 | 20110037.467 |
| 销售中匿名行数 | 234358 |
| 销售中匿名金额 | 2675572.72 |
| 有客户销售行数（后续RFM候选） | 802651 |
| 有客户销售人数 | 5852 |
| 销售中描述缺失 | 0 |
| 若去重销售行数 | 1003345 |
| 若去重销售金额 | 19643718.087 |
| 去重销售金额减少 | 466319.38 |
| 实际运行秒数（报告写入前） | 2.285168 |

## 互斥记录分类（按取消→坏账→无效→未知→非商品→数量价格顺序）

| record_class | rows | amount |
| --- | --- | --- |
| bad_debt | 6 | -147614.08 |
| cancellation | 19494 | -1526667.86 |
| non_product | 4697 | 851351.491 |
| nonpositive_quantity | 3433 | 0 |
| real_sale | 1037009 | 20110037.467 |
| unknown_stock | 36 | 143.55 |
| zero_price | 2696 | 0 |

分类行数加总等于原始行数；例如非商品中的零价格已归入非商品，不能把各异常标志重复相加当作删除数量。

## 特殊代码清单

| code_key | stock_role | rows | amount |
| --- | --- | --- | --- |
| ADJUST | manual_adjustment | 67 | 6835.24 |
| ADJUST2 | manual_adjustment | 3 | 731.05 |
| AMAZONFEE | platform_fee | 43 | -260763.58 |
| B | bad_debt | 6 | -147614.08 |
| BANK CHARGES | bank_fee | 102 | -35562.629 |
| C2 | postage | 282 | 13386 |
| C3 | unknown | 1 | 0 |
| CRUK | commission | 16 | -7933.43 |
| D | discount | 177 | -13484.54 |
| DCGS0003 | product | 14 | 32.59 |
| DCGS0004 | product | 5 | 67.94 |
| DCGS0006 | unknown | 1 | 0 |
| DCGS0016 | unknown | 1 | 0 |
| DCGS0027 | unknown | 1 | 0 |
| DCGS0036 | unknown | 1 | 0 |
| DCGS0037 | product | 2 | 12.72 |
| DCGS0039 | unknown | 1 | 0 |
| DCGS0041 | product | 1 | 5.06 |
| DCGS0044 | product | 1 | 2.57 |
| DCGS0053 | unknown | 1 | 0 |
| DCGS0055 | unknown | 1 | 0 |
| DCGS0056 | unknown | 1 | 0 |
| DCGS0057 | unknown | 1 | 0 |
| DCGS0058 | product | 31 | 34.82 |
| DCGS0059 | unknown | 1 | 0 |
| DCGS0060 | unknown | 1 | 0 |
| DCGS0062 | product | 2 | 2.51 |
| DCGS0066N | product | 4 | 17.3 |
| DCGS0066P | unknown | 1 | 0 |
| DCGS0067 | unknown | 1 | 0 |
| DCGS0068 | unknown | 3 | 17.12 |
| DCGS0069 | unknown | 6 | 80.31 |
| DCGS0070 | unknown | 3 | 25.44 |
| DCGS0071 | unknown | 1 | 0 |
| DCGS0072 | unknown | 4 | 20.68 |
| DCGS0073 | unknown | 1 | 0 |
| DCGS0074 | unknown | 1 | 0 |
| DCGS0075 | product | 1 | 12.72 |
| DCGS0076 | product | 15 | 292.09 |
| DCGSLBOY | unknown | 1 | 0 |
| DCGSLGIRL | unknown | 1 | 0 |
| DCGSSBOY | product | 23 | 251.35 |
| DCGSSGIRL | product | 25 | 295.63 |
| DOT | postage | 1446 | 322647.47 |
| GIFT | unknown | 1 | 0 |
| GIFT_0001_10 | voucher | 16 | 126.21 |
| GIFT_0001_20 | voucher | 29 | 471.06 |
| GIFT_0001_30 | voucher | 29 | 610.09 |
| GIFT_0001_40 | voucher | 7 | 166.09 |
| GIFT_0001_50 | voucher | 8 | 253.59 |
| GIFT_0001_60 | voucher | 2 | 0 |
| GIFT_0001_70 | voucher | 3 | 59.57 |
| GIFT_0001_80 | voucher | 4 | 0 |
| GIFT_0001_90 | voucher | 2 | 0 |
| M | manual_adjustment | 1426 | -82781.27 |
| PADS | product | 19 | -36.583 |
| POST | postage | 2122 | 112341 |
| S | sample | 104 | -6065.8 |
| SP1002 | product | 3 | 14.75 |
| TEST001 | test | 15 | 202.5 |
| TEST002 | test | 2 | 1 |

## 极端数量证据（保留原符号）

| source_row | invoice_no | stock_code | description | quantity | unit_price | invoice_datetime | customer_id | country | record_class |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 587081 | 541431 | 23166 | MEDIUM CERAMIC TOP STORAGE JAR | 74215 | 1.04 | 2011-01-18 10:01:00 | 12346 | United Kingdom | real_sale |
| 587086 | C541433 | 23166 | MEDIUM CERAMIC TOP STORAGE JAR | -74215 | 1.04 | 2011-01-18 10:17:00 | 12346 | United Kingdom | cancellation |
| 1065883 | 581483 | 23843 | PAPER CRAFT , LITTLE BIRDIE | 80995 | 2.08 | 2011-12-09 09:15:00 | 16446 | United Kingdom | real_sale |
| 1065884 | C581484 | 23843 | PAPER CRAFT , LITTLE BIRDIE | -80995 | 2.08 | 2011-12-09 09:27:00 | 16446 | United Kingdom | cancellation |

## 待确认与边界

尚未确认重复来源、极端交易是否录错、零价格含义及未知代码。销售筛选是可复现的业务规则，不是逐单履约证明。退货匹配前正单含后来取消的购买，因此当前金额不能作为净销售。RFM、returns_log、三个 Parquet 及 run_all 接入均未实现。本阶段速度不代表完整 Pipeline 的速度。
