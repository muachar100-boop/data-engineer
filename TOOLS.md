# TOOLS.md - 工具与软件使用清单

> 只列对解题有实质帮助、且在仓库代码或文档里能找到使用痕迹的工具。
> 版本号取自本机实际运行环境（conda 环境 `gekko-de`，Python 3.11.18），2026-11-01 用 `python run_all.py` 全量跑通核对。
> 标【待本人补充】的地方是仓库里看不出来、需要本人确认的信息。

---

## 🤖 AI 工具

### 工具 1：Claude（对话 + Claude Code）

- **版本/型号**：Claude Code 桌面版（Code 标签页）；对话阶段的具体型号【待本人补充】
- **解决了什么问题**：
  - 拆解题目、起草代码骨架和文档初稿，之后由我对照真实数据逐条核对、改写
  - Q4 人工标注：W、X 两批共 60 条标签先由 Claude 依据原文、v8 情绪量表和 v6 类别规则给出建议，再由我审核确认（`accuracy_evaluation.md` 的“局限”第 1 条已如实说明，这些标签不是完全独立的盲标）
  - 整理收尾：检查 Q1–Q5 能否复现、补齐 `run_all.py` 的模块接入、核对文档里的数字与引用文件
- **使用场景**：每道题都用过；完整记录见 `AI_LOG.md`
- **替代方案**：查官方文档 + Stack Overflow 手写；Q4 标注改为两人独立盲标再计算一致性（Cohen's kappa），代价是时间成本高很多

### 工具 2：OpenAI API（gpt-4o-mini / gpt-4.1-mini）——作为 Q4 的生产组件，而不是写代码的助手

- **版本/型号**：主模型 `gpt-4o-mini-2024-07-18`，兜底 `gpt-4.1-mini-2025-04-14`（Chat Completions + Structured Outputs 严格模式）
- **解决了什么问题**：对 13,898 个规则处理不了的独立差评输入做结构化抽取；全量 28.0 分钟、估算 $1.42（含开发阶段合计约 $1.72，预算 $3）
- **使用场景**：`extract_reviews.py --live`；每批 5 条、8 线程并发、temperature=0；引句不是原文连续子串时单条交给 gpt-4.1-mini 重抽
- **API Key 配置**：只有付费运行 `extract_reviews.py --live` 才需要。在仓库根目录 `.env` 中写 `OPENAI_API_KEY=sk-...`（也可用同名环境变量），程序读取但从不打印；`.env` 已在 `.gitignore` 中。`python run_all.py` 和 Q4 单元测试都是离线的，不需要 Key；本项目没有调用 Anthropic API，不需要 `ANTHROPIC_API_KEY`
- **替代方案**：全量用 gpt-4.1-mini（质量更好，但全量约 $4.4，超预算）；或本地小模型分类（见下方“放弃的方案”）

---

## 💻 IDE / 开发环境

### VS Code

- **解决了什么问题**：写 Python / SQL / Markdown，统一用一个编辑器
- **使用场景**：通过 conda 扩展选择 `gekko-de` 解释器（`.vscode/settings.json` 里配置了 conda 为默认环境管理器）；SQLTools 扩展连接本地 MySQL（见下方数据库工具）
- **替代方案**：PyCharm；Jupyter Notebook 适合 Q1 边看边探索，但最终产物要求可一键复现，所以统一写成 `.py` 脚本

### Anaconda（conda 环境 `gekko-de`）

- **解决了什么问题**：隔离笔试依赖，避免和本机其他项目（如 PyTorch 环境）的 pandas/numpy 版本冲突
- **使用场景**：`conda activate gekko-de` 后运行所有脚本；`python run_all.py` 在这个环境里跑通
- **替代方案**：`python -m venv .venv` + `pip install -r requirements.txt`；或 uv

---

## 🐍 Python 库

### 数据处理

| 库 | 版本 | 用途 | 替代方案 |
|----|------|------|----------|
| pandas | 3.1.0 | Q1 统计报告、Q3 ETL 全流程（向量化清洗、RFM 聚合、退货匹配）、Q5 基线统计 | Polars（更快，但团队主栈是 pandas）；DuckDB 直接 SQL |
| numpy | 2.4.6 | Q3 向量化条件计算、固定随机种子 42 | 纯 pandas 方法 |
| pyarrow | 26.0.0 | Q3 三个 parquet 的写出与回读校验 | fastparquet |
| matplotlib | 3.11.2 | Q1 生成 `customer_country_distribution.png`（有/无 Customer ID 的国家分布对比） | seaborn；或只在报告里给表格 |

> 注：pandas 3.x 写出的 datetime 列是 `datetime64[us]`，pandas 2.x 是 `[ns]`；值完全一致，但跨版本比较 parquet 时 `DataFrame.equals` 会因为 dtype 返回 False。

### 数据库 / SQL（强制使用 MySQL 8.0+）

| 库 | 版本 | 用途 | 替代方案 |
|----|------|------|----------|
| PyMySQL | 1.2.3 | `load_olist.py` 用游标 `executemany` 分批写入 9 张表；`SSCursor` 流式回读做全字段指纹校验；`run_all.py` 执行 Q2 SQL 并把结果摘要写进日志 | mysqlclient（需编译）；mysql-connector-python |
| cryptography | 50.0.2 | MySQL 8 默认 `caching_sha2_password` 认证需要 | 把账号改成 `mysql_native_password`（不推荐，降低安全性） |
| python-dotenv | 1.2.4 | 从根目录 `.env` 读 MySQL 连接信息，密码不进代码和 Git | 直接读环境变量 |

> 没有用 `pandas.to_sql` + SQLAlchemy 灌数据：`to_sql` 会自动推断类型（空字符串变 NULL、`'00123'` 变数字等），而我要求原始层与 CSV **逐字节一致**，所以所有列先按 `LONGTEXT` 原样导入，类型转换放到 SQL 里显式 `CAST` / `STR_TO_DATE`。

### LLM / API

| 库 | 版本 | 用途 | 替代方案 |
|----|------|------|----------|
| urllib.request（标准库） | — | Q4 直接调用 `https://api.openai.com/v1/chat/completions` | openai 官方 SDK |

> 列出标准库是因为这是一个**刻意的选择**：不用 SDK，请求体完全由代码拼出，可以离线把全部 2,780 个请求体和开发阶段的版本逐字节比对；429/5xx/超时的重试与预算预留逻辑也都在自己手里，测试时用测试桩替换传输层即可。

### 数据下载

| 库 | 版本 | 用途 | 替代方案 |
|----|------|------|----------|
| kagglehub | 1.0.3 | `download_data.py` 下载两个 Kaggle 数据集，利用本地缓存避免重复下载，并把 `online_retail_II.csv` 统一重命名为 `online_retail_ii.csv` | Kaggle CLI；手动下载解压 |

---

## 🛠️ 数据库工具

### MySQL Community Server 8.4.13（本机原生安装，未用 Docker）

- **解决了什么问题**：Q2 全部 SQL（题目强制 MySQL 8.0+）；`load_olist.py` 启动时会拒绝 MariaDB 和 8.0 以下版本
- **使用场景**：Olist 9 张表导入 `.env` 指定的库；Q2.1–Q2.3 的 `.sql` 由 `run_all.py` 执行，行数和前 5 行写进 `run_all_*.log`
- **替代方案**：`docker run -d --name mysql8 -e MYSQL_ROOT_PASSWORD=xxx -p 3306:3306 mysql:8`

### VS Code SQLTools 扩展（MySQL 驱动）

- **解决了什么问题**：在编辑器里直接写、跑 SQL，看结果表
- **使用场景**：连接名 `olist-local`（localhost:3306，库 `olist`，每次询问密码）；调试 Q2 各题 CTE、对比 Q2.4 原查询与优化查询
- **替代方案**：DBeaver、MySQL Workbench、命令行 `mysql`

---

## 🔍 调试与分析工具

- **unittest（标准库）**：`q4_llm_extraction/test_extract_reviews.py` 共 13 个离线测试，用测试桩代替 API（不联网、不读 Key），覆盖批量解析、引句修复、兜底、死信、重试、续跑不重复付费等路径；`run_all.py` 每次都会跑
- **assert 断言**：Q3 `validate_datasets` 对三个输出做断言（销售金额为正、客户唯一、recency 非负、退货匹配键/时间/同价、累计退货数量不超过原单、RFM 金额与销售对账），并把 parquet 回读后与内存表完整比较
- **EXPLAIN ANALYZE**：Q2.4 的验证方法（见 `q2_4_query_optimization.md` 第 3 节）；原查询与优化查询的实测耗时【待本人补充到 REFLECTION.md 对比表】
- **SHA256 指纹**：Q2 导入前后对 CSV 和 MySQL 表各算一次“行数 + 每行哈希之和”的无序指纹，证明没有丢行、改值；Q3 运行前后校验原始 CSV 未被改动；Q4 校验 Prompt 与冻结版本一致

---

## 🌐 在线资源 / 文档

- **MySQL 8.4 Reference Manual – Range Optimization**（https://dev.mysql.com/doc/refman/8.4/en/range-optimization.html）：Q2.4 判断联合索引“等值列在前、范围列在后”以及范围列之后的列不能继续缩小扫描区间
- **MySQL 8.0 Reference Manual – CREATE INDEX**（https://dev.mysql.com/doc/refman/8.0/en/create-index.html）：LONGTEXT 列必须指定前缀长度才能建索引
- **OpenAI API 文档（Structured Outputs、定价页）**：Q4 严格 JSON Schema 输出和成本估算；单价于 2026-11-01 核对
- **Kaggle 数据集页面**：确认两个数据集的文件名与字段（Online Retail II 实际字段是 `Invoice` / `Price` / `Customer ID`，与题目里的 `InvoiceNo` / `UnitPrice` 不同）

---

## 📌 关键工具决策反思

### 你做过的最重要的工具选型决策是？为什么？

**Q4 主模型选 gpt-4o-mini，而不是质量更好的 gpt-4.1-mini。**
独立样本上 gpt-4.1-mini 的四字段匹配更好（X 批 17/30 vs 13/30），但全量约 $4.4，超出 $3 预算；把 gpt-4.1-mini 压到极限也要约 $3.2。最终方案是 gpt-4o-mini 批量主跑 + 引句校验失败时用 gpt-4.1-mini 单条兜底，实际约 $1.72。代价写在 `accuracy_evaluation.md` 里：按事先登记的非劣效门槛，这个方案**没有通过**，是在预算约束下明知风险的取舍。

**Q2 原始层全部用 LONGTEXT 原样导入，而不是 `to_sql` 自动建表。**
这样原始层可以和 CSV 做全字段指纹比对，空字符串、前导零都不会被悄悄改掉；代价是每条查询都要显式转换类型，且没有索引，Q2.3 在本机要跑约 105 秒。Q2.4 文档里给了分析层改成 `CHAR(32)` / `DATETIME` / `DECIMAL` 并加主键的建议。

### 你尝试过但**放弃**的工具/方案？

- **Q4 用冻结的预训练 BERT（均值池化）+ 逻辑回归 / Ridge 头做分类**：在 `bert_pilot/` 做过小试（依赖 torch、transformers、scikit-learn、openpyxl）。放弃原因与小试结果【待本人补充】；另外它只能做分类，`evidence_quote_en` 翻译仍需要 LLM。
- **Q4 全量用 gpt-4.1-mini**：质量更好，但超预算（见上）。
- **Q4 批大小 10 条的紧凑批量**：在 `round3_cost_optimization/batch10_compact_pilot/` 试过，最终用 5 条/批。放弃原因【待本人补充】。
- **Q2 用 `pandas.to_sql` 灌数据**：会自动推断类型、改写原值，无法证明原始层与 CSV 一致，改为 PyMySQL 原样写入。

### 如果再做一次，你会换用什么工具？

- **从一开始就锁定精确版本**：`requirements.txt` 已删掉模板里没用到的包（sqlalchemy、openai、anthropic、langdetect、pydantic、pytest、tqdm、loguru、seaborn、ucimlrepo），但仍是宽松下限；会改成 `pip freeze` 出的精确版本，或者用 uv 锁文件。
- **Q2 导入后建一层带类型和主键的分析表**，而不是每条查询都在 LONGTEXT 上 `CAST`；Q2.3 这种多表聚合能从分钟级降下来。
- **Q4 实验脚本从一开始就纳入 Git**：`versions/`、`bert_pilot/` 等实验目录现在只剩缓存文件，`accuracy_evaluation.md` 里的复现命令已经跑不了。【其余待本人补充】
