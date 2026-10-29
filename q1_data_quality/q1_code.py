"""Q1.1/Q1.2：只读质量探索；原始 CSV 不删除、不填补、不覆盖。"""

import argparse
import csv
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
# 基于本次实际运行结果整理的人工观察，不将业务假设直接判为数据错误。
SUSPICIOUS_FINDINGS = """## 可疑现象总结（待进一步核查）

1. **字段缺失**：Description 存在缺失；Customer ID 大量缺失，缺失比例约为 22.77%，可能影响客户识别和客户维度分析。
2. **商品代码与描述的对应关系**：从商品识别语义出发，StockCode 与 Description 的对应关系值得核查；实际同时存在一个 StockCode 对应多个 Description、一个 Description 对应多个 StockCode 的情况。是否应严格一一对应，仍需结合描述变更、格式差异及业务规则确认。
3. **数量负值及对称极值**：Quantity 存在大量负数，最小值与最大值的绝对值相等，需核查是否涉及取消、退货或冲销等业务记录。
4. **大额负价格**：Price 存在 5 条绝对值较大的负数记录，需核查对应记录的业务含义。
5. **零价格**：Price 存在较多为 0 的记录，偏离普通付费商品交易的价格预期；需核查是否涉及赠品、调整记录或价格缺录，暂不直接判定为错误。
6. **极大的正价格**：Price 的最大正向值远大于第三四分位数（Q3），可能是特殊记录，需结合商品代码和描述核查。
7. **商品代码含空白**：StockCode 存在 1 条首尾含空白的非缺失记录，原始值为 '47503J '，末尾空格可能影响匹配、分组及唯一值统计。
8. **整行完全重复**：存在大量按全部原始字段比较完全相同的记录，需要校对是否为重复导入或缺少明细标识导致的无法区分，暂不直接删除。
9. **跨年连续无记录区间**：2009-12-24 至 2010-01-03、2010-12-24 至 2011-01-03 均连续 11 天无记录，可能与节假日停业有关，也需排查数据采集或导出中断的可能。

以上为基于当前数据的人工观察和待验证假设；原始数据保留，不据此直接删除或填补。输入数据变化后，应重新核对本总结中的具体数量和日期。
"""
# 题目中的业务名称 -> 当前下载版本的原始列名。只用于解释，不重命名数据。
FIELD_MAP = {
    "InvoiceNo": "Invoice", "StockCode": "StockCode",
    "Description": "Description", "Quantity": "Quantity",
    "InvoiceDate": "InvoiceDate", "UnitPrice": "Price",
    "CustomerID / Customer ID": "Customer ID", "Country": "Country",
}
DTYPES = {
    "Invoice": str, "StockCode": str, "Description": str,
    "Quantity": "int64", "InvoiceDate": str, "Price": "float64",
    "Customer ID": str, "Country": str,
}


def table(frame: pd.DataFrame) -> str:
    """生成 Markdown 表格，不额外依赖 tabulate；转义换行及竖线。"""
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "\\n").replace("\r", "\\r")
    rows = ["| " + " | ".join(map(cell, frame.columns)) + " |",
            "| " + " | ".join(["---"] * len(frame.columns)) + " |"]
    rows.extend("| " + " | ".join(map(cell, row)) + " |"
                for row in frame.itertuples(index=False, name=None))
    return "\n".join(rows)


def load_data(path: Path):
    """保留原始文本视图，另按指定类型读取分析视图。"""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    if len(header) != len(set(header)):
        raise ValueError(f"CSV 存在重复列名，请先人工检查：{header}")
    # 禁止默认把 NA、NULL 等文本静默视作缺失；空字段单独统计。
    raw = pd.read_csv(path, dtype="string", keep_default_na=False)
    typed = pd.read_csv(path, dtype=DTYPES, keep_default_na=False)
    return raw, typed


def missing_mask(series: pd.Series) -> pd.Series:
    """缺失口径为空字段或仅含空白字符；不改变原值。"""
    return series.str.strip().eq("")


def profile_columns(raw, typed):
    rows = []
    for name in raw.columns:
        series = raw[name]
        missing = missing_mask(series)
        observed = series[~missing]
        repeated = int(observed.duplicated().sum())
        rows.append({
            "列名": name, "指定类型dtype": str(typed[name].dtype),
            "原始文本dtype": str(series.dtype),
            "空字段数": int(series.eq("").sum()),
            "仅空白数": int((series.ne("") & missing).sum()),
            "缺失比例(%)": round(missing.mean() * 100, 6),
            "非缺失唯一值数": observed.nunique(),
            "非缺失重复多余行数": repeated,
            "非缺失重复比例(%)": round(repeated / len(observed) * 100, 6) if len(observed) else None,
        })
    return pd.DataFrame(rows)


def stock_description_profile(raw):
    """统计双向对应数量；缺失不参与对应关系，文本不去空格或改大小写。"""
    valid = ~missing_mask(raw["StockCode"]) & ~missing_mask(raw["Description"])
    pairs = raw.loc[valid, ["StockCode", "Description"]].drop_duplicates()
    per_stock = pairs.groupby("StockCode", sort=False)["Description"].nunique()
    per_description = pairs.groupby("Description", sort=False)["StockCode"].nunique()
    summary = pd.DataFrame([{
        "两列均非缺失行数": int(valid.sum()),
        "至少一列缺失行数": int((~valid).sum()),
        "不同对应组合数": len(pairs),
        "StockCode数量": len(per_stock),
        "Description数量": len(per_description),
        "对应多个Description的StockCode数": int(per_stock.gt(1).sum()),
        "对应多个StockCode的Description数": int(per_description.gt(1).sum()),
    }])
    distribution = per_stock.value_counts().sort_index().rename_axis(
        "每个StockCode对应的不同Description数量"
    ).reset_index(name="StockCode数量")
    # 优先展示多描述代码的对应示例，按原始数据中的出现顺序选取五个组合。
    examples = pairs[pairs["StockCode"].isin(per_stock[per_stock.gt(1)].index)].head(5).copy()
    if examples.empty:
        examples = pairs.head(5).copy()
    examples["该StockCode对应的Description总数"] = examples["StockCode"].map(per_stock)
    for column in ("StockCode", "Description"):
        examples[column] = examples[column].map(repr)
    return summary, distribution, examples


def numeric_checks(raw):
    """仅检查 Quantity、Price 的数值可解析性、分布和取值。"""
    summaries, checks = [], []
    for name in ("Quantity", "Price"):
        series = raw[name]
        missing = missing_mask(series)
        parsed = pd.to_numeric(series.mask(missing), errors="coerce")
        finite = parsed.notna() & np.isfinite(parsed)
        values = parsed[finite]
        masks = {
            "非空但无法解析为数值": ~missing & parsed.isna(),
            "非有限值(inf/-inf)": parsed.notna() & ~finite,
            "负值（仅标记，业务含义待分析）": finite & parsed.lt(0),
            "零值（仅标记）": finite & parsed.eq(0),
        }
        if name == "Quantity":
            masks["非整数值"] = finite & parsed.mod(1).ne(0)
        if name in ("Quantity", "Price"):
            q1, q3 = values.quantile([0.25, 0.75])
            iqr = q3 - q1
            masks["超出1.5倍IQR（统计提示，非错误判定）"] = finite & (
                parsed.lt(q1 - 1.5 * iqr) | parsed.gt(q3 + 1.5 * iqr))
            summaries.append({
                "列名": name, "有限数值数": len(values),
                "min": values.min(), "max": values.max(),
                "mean": values.mean(), "std(ddof=1)": values.std(ddof=1),
                "max-min": values.max() - values.min(),
                "Q1": q1, "median": values.median(), "Q3": q3,
            })
        for rule, mask in masks.items():
            checks.append({"列名": name, "检查规则": rule, "记录数": int(mask.sum()),
                           "占全部行(%)": round(mask.mean() * 100, 6),
                           "原始值示例": repr(series[mask].head(5).tolist())})
    return pd.DataFrame(summaries), pd.DataFrame(checks)


def key_checks(raw):
    """候选键不是已声明的主键；唯一性必须同时满足无缺失、无重复。"""
    candidates = [[name] for name in raw.columns]
    candidates += [["Invoice", "StockCode"], ["Invoice", "StockCode", "InvoiceDate"], list(raw.columns)]
    rows = []
    for columns in candidates:
        missing = pd.concat([missing_mask(raw[c]) for c in columns], axis=1).any(axis=1)
        extra = int(raw.duplicated(subset=columns).sum())
        members = int(raw.duplicated(subset=columns, keep=False).sum())
        rows.append({"候选键/列": " + ".join(columns), "含缺失行数": int(missing.sum()),
                     "重复多余行数": extra, "重复组涉及行数": members,
                     "重复多余行比例(%)": round(extra / len(raw) * 100, 6) if len(raw) else None,
                     "满足无缺失且唯一": bool(not missing.any() and extra == 0)})
    return pd.DataFrame(rows)


def date_profile(raw):
    series = raw["InvoiceDate"]
    missing = missing_mask(series)
    # 使用已观察到的明确格式，避免日/月歧义；失败只在派生视图中成为 NaT。
    parsed = pd.to_datetime(series.mask(missing), format="%Y-%m-%d %H:%M:%S", errors="coerce")
    valid = parsed.dropna()
    daily = valid.dt.normalize().value_counts().sort_index()
    monthly = valid.dt.to_period("M").value_counts().sort_index()
    if len(valid):
        daily = daily.reindex(pd.date_range(valid.min().normalize(), valid.max().normalize(), freq="D"), fill_value=0)
        monthly = monthly.reindex(pd.period_range(valid.min().to_period("M"), valid.max().to_period("M"), freq="M"), fill_value=0)
    summary = pd.DataFrame([{
        "最早日期": valid.min(), "最晚日期": valid.max(),
        "有效日期行数": len(valid), "缺失日期数": int(missing.sum()),
        "非空解析失败数": int((~missing & parsed.isna()).sum()),
        "解析失败示例": repr(series[~missing & parsed.isna()].head(5).tolist()),
    }])
    daily = daily.rename_axis("日期").reset_index(name="记录数")
    monthly = monthly.rename_axis("月份").reset_index(name="记录数")
    assert daily["记录数"].sum() == len(valid)
    assert monthly["记录数"].sum() == len(valid)
    assert len(valid) + missing.sum() + (~missing & parsed.isna()).sum() == len(raw)
    return summary, daily, monthly


def code_meaning(code):
    """按人工核查结论标注业务解释，不改变原始值。"""
    meanings = {
        "ADJUST": "人工调整", "ADJUST2": "人工调整", "AMAZONFEE": "Amazon平台费用",
        "B": "销售坏账调整", "BANK CHARGES": "银行费用", "CRUK": "CRUK佣金",
        "D": "折扣", "DOT": "线上渠道邮费", "M": "手工记录", "m": "手工记录",
        "POST": "邮费", "C2": "运输/运费（CARRIAGE）",
        "TEST001": "测试商品，需核查是否测试交易", "TEST002": "测试商品，需核查是否测试交易",
        "S": "样品", "PADS": "商品", "DCGSSBOY": "商品",
        "DCGSSGIRL": "商品", "C3": "缺失值，可能为记录出错",
        "DCGSLBOY": "缺失值，可能为记录出错", "DCGSLGIRL": "缺失值，可能为记录出错",
        "GIFT": "缺失值，可能为记录出错",
        "gift_0001_60": "缺失值，记录出错（人工待核查判断）",
        "gift_0001_90": "缺失值，记录出错（人工待核查判断）",
        "47503J ": "商品代码末尾空格，描述为园艺工具",
    }
    if code in meanings:
        return meanings[code]
    if code.startswith("gift_0001_"):
        return "礼物券"
    return "业务含义待核查"


def q12_invoice(data):
    """步骤一：精简展示前缀次数、描述证据和需要核查的正值。"""
    unusual = data.loc[~data["Invoice"].str.fullmatch(r"[0-9]+")].copy()
    unusual["前缀"] = unusual["Invoice"].str.extract(r"^([^0-9]+)", expand=False).fillna("无前导非数字字符")
    summary = unusual.groupby("前缀").agg(出现次数=("Invoice", "size")).reset_index()
    summary["业务含义"] = summary["前缀"].map({"A": "销售坏账记录（财务调整）", "C": "取消交易并退款的记录（1条正值待确认）"}).fillna("待核查")
    sections = ["## 1. Invoice 非纯数字前缀", table(summary),
                "出现次数按交易明细行计：A 出现6次，描述均为 Adjust bad debt；C 出现19494次，其中19493条 Quantity 为负，1条为正。C 的取消及退款含义是业务解释，实际资金退款状态仍需来源系统确认。"]
    cols = ["Invoice", "StockCode", "Description", "Quantity", "Price", "Amount"]
    sections += ["A 前缀的记录证据：", table(unusual.loc[unusual["前缀"].eq("A"), cols]),
                 "C 前缀中正数量的例外（后续确认）：", table(unusual.loc[unusual["前缀"].eq("C") & unusual["Quantity"].gt(0), cols]),
                 "**销售总额口径**：A 是财务调整，不应该算入销售总额；C 应按原始带符号金额如实加入销售净额计算，通常作为退款扣减，不能取绝对值累加。1条正值保留原符号并单列待确认。",
                 "支持代码：`q12_invoice()`，以 fullmatch 筛出非纯数字 Invoice，再提取前缀并统计行数。"]
    return sections


def stock_selection(data):
    """去掉仅含字母数字的混合代码，保留人工指定的五种例外。"""
    codes = data["StockCode"]
    conventional = codes.str.fullmatch(r"[0-9]+[A-Za-z]*")
    mixed = codes.str.fullmatch(r"[A-Za-z0-9]+") & codes.str.contains(r"[A-Za-z]") & codes.str.contains(r"[0-9]")
    return (~conventional & ~mixed) | codes.isin(["C3", "C2", "ADJUST2", "TEST001", "TEST002"])


def q12_stock(data):
    """步骤二：过滤仅作用于展示集合，按剩余集合重新计算整体占比。"""
    selected = data.loc[stock_selection(data)]
    summary = selected.groupby("StockCode", sort=True).agg(
        行数=("Invoice", "size"), 净金额=("Amount", "sum"),
        描述=("Description", lambda x: repr(x.drop_duplicates().tolist())),
    ).reset_index()
    summary["行数占比(%)"] = summary["行数"] / len(data) * 100
    summary["净金额占比(%)"] = summary["净金额"] / data["Amount"].sum() * 100
    summary["业务解释"] = summary["StockCode"].map(code_meaning)
    summary["StockCode"] = summary["StockCode"].map(repr)
    overall = pd.DataFrame([{"代码数": selected["StockCode"].nunique(), "行数": len(selected),
        "行数占比(%)": len(selected) / len(data) * 100, "净金额": selected["Amount"].sum(),
        "净金额占比(%)": selected["Amount"].sum() / data["Amount"].sum() * 100}])
    assert summary["行数"].sum() == len(selected)
    return ["## 2. StockCode 代码与业务解释",
            "展示集合去掉纯数字、数字后接字母，以及仅由字母和数字组成的混合代码；保留纯字母、下划线、空格等特殊格式。C3、C2、ADJUST2、TEST001、TEST002 按人工指定名单作为例外保留。过滤只影响分析展示，不删除原始记录。整体占比已按当前展示集合重算，分母仍为全表。",
            table(overall), table(summary),
            "表中“缺失值”指 Description 缺失，不是 StockCode 缺失。“记录出错”属于人工待核查判断，尚未依据该标签删除记录。PADS、DCGSSBOY、DCGSSGIRL 按商品解释；gift_0001_数字按礼物券解释，其中60、90单列为缺失描述记录。",
            "支持代码：`stock_selection()`、`q12_stock()`、`code_meaning()`。"]


def q12_extremes(data):
    """步骤三：保留完整极值和负价格证据，关联只作文字说明。"""
    requested = data.loc[data["Quantity"].isin([80995, -80995])]
    cols = ["数据行序号", "Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country", "Amount"]
    sections = ["## 3. 可疑的极端值", "### 数量极值完整记录", table(requested[cols])]
    positive = requested.loc[requested["Quantity"].gt(0)]
    negative = requested.loc[requested["Quantity"].lt(0)]
    linked = positive.merge(negative, on=["Customer ID", "StockCode", "Price"], suffixes=("_正", "_负"))
    for row in linked.itertuples(index=False):
        minutes = (pd.Timestamp(row.InvoiceDate_负) - pd.Timestamp(row.InvoiceDate_正)).total_seconds() / 60
        sections.append(f"关联证据：同客户、同商品、同单价，时间间隔 {minutes:g} 分钟，合计金额 {row.Amount_正 + row.Amount_负:.2f}，支持取消/冲销解释；初始大额下单是否录入错误仍需确认。")
    sections += ["### 负价格完整记录", table(data.loc[data["Price"].lt(0), cols]),
                 "负价格记录均为 A 前缀、StockCode B、Description 为 Adjust bad debt、Quantity 为1、Customer ID缺失、Country为United Kingdom，因此属于销售坏账财务调整。",
                 "**极端值处理建议**：C 类异常极值在确认确为错误交易及其取消冲销后，可从下游有效销售明细中删除对应的正负交易对；确认前保留。不能仅删除负向 C 行而保留原正向销售，否则会高估销售额。正常退款 C 记录仍按第1节如实参与净额计算。A 类坏账财务调整不可删除，应保留用于财务审计，并与商品销售额分开统计。本次不执行任何删除。",
                 "支持代码：`q12_extremes()`，使用 isin、merge 和时间差核对关联，不再输出关联宽表或同代码调整表。"]
    return sections


def country_pies(countries, output):
    """两组国家明细行占比分别绘制饼图，前五国家以外合并为 Other。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(1, 2, figsize=(15, 5))
    country_colors = {name: plt.get_cmap("tab20")(index % 20)
                      for index, name in enumerate(countries.index)}
    country_colors["Other"] = "#aaaaaa"
    labels = ["Customer ID missing", "Customer ID present"]
    for axis, (group, column), title in zip(axes, countries.items(), labels):
        shares = column[column.gt(0)].sort_values(ascending=False)
        top = shares.head(5).copy()
        if len(shares) > 5:
            top.loc["Other"] = shares.iloc[5:].sum()
        wedges, _, _ = axis.pie(top, startangle=90, autopct=lambda v: f"{v:.2f}%" if v >= 5 else "", colors=[country_colors[name] for name in top.index])
        axis.legend(wedges, [f"{name}: {value:.2f}%" for name, value in top.items()], loc="center left", bbox_to_anchor=(1, 0.5), frameon=False, fontsize=9)
        axis.set_title(title)
    figure.suptitle("Country distribution by transaction rows")
    figure.tight_layout(rect=(0, 0, 1, 0.94))
    output.mkdir(parents=True, exist_ok=True)
    figure.savefig(output / "customer_country_distribution.png", dpi=160, bbox_inches="tight")
    plt.close(figure)


def q12_customers(data, output):
    """步骤四：仅保留三项总体占比和三个群体对比表。"""
    labels = ["Customer ID缺失", "Customer ID非缺失"]
    groups = data.assign(客户组=np.where(missing_mask(data["Customer ID"]), labels[0], labels[1]))
    totals = groups.groupby("客户组").agg(行数=("Invoice", "size"), 总金额=("AbsAmount", "sum"), 净金额=("Amount", "sum"))
    summary = pd.DataFrame({
        "总行数占比(%)": totals["行数"] / len(data) * 100,
        "总金额占比(%)": totals["总金额"] / data["AbsAmount"].sum() * 100,
        "净金额占比(%)": totals["净金额"] / data["Amount"].sum() * 100,
    }).reindex(labels)
    countries = pd.crosstab(groups["Country"], groups["客户组"]).reindex(columns=labels)
    countries = countries.div(countries.sum(axis=0), axis=1) * 100
    country_pies(countries, output)
    invoices = groups.groupby(["客户组", "Invoice"])["Amount"].sum()
    average = invoices.groupby(level=0).mean().reindex(labels).rename("客单价（原始净额发票均值）").reset_index()
    tops = []
    for label in labels:
        subset = groups.loc[groups["客户组"].eq(label)]
        top = subset.groupby("StockCode").agg(行数=("Invoice", "size"), 描述=("Description", lambda x: repr(x.drop_duplicates().head(2).tolist()))).reset_index().sort_values(["行数", "StockCode"], ascending=[False, True]).head(10).reset_index(drop=True)
        top.columns = [f"{label}-{c}" for c in top.columns]
        tops.append(top)
    comparison = pd.concat(tops, axis=1)
    comparison.insert(0, "排名", range(1, len(comparison) + 1))
    assert totals["行数"].sum() == len(data)
    assert np.isclose(totals["净金额"].sum(), data["Amount"].sum())
    assert np.allclose(summary.sum(), 100)
    return ["## 4. Customer ID 缺失与非缺失对比",
            "### 总体占比", table(summary.reset_index()),
            "总金额定义为逐行 |Quantity × Price| 的合计，表示抵消前金额规模；净金额为带符号 Quantity × Price 合计。两种金额占比均以全表同口径金额为分母。保留取消、财务调整及重复记录。",
            "### 表1：国家分布占比（组内明细行占比，%）", table(countries.reset_index()),
            "![国家分布占比对比](customer_country_distribution.png)",
            "饼图分别展示两组前五国家，其余合并为 Other；完整国家比例见上表。",
            "### 表2：客单价对比", table(average),
            "客单价 = 每组原始净金额 / 每组不同 Invoice 数。包含取消及调整发票，是原始净额发票均值，不是按客户计算的均值。",
            "### 表3：高频商品/代码 TOP 10 对比", table(comparison),
            "使用全部原始行，按 StockCode 出现明细行数排名，不筛选正向交易、不去重。保留邮费等非商品代码供对照，不将其误称为商品；相同频次按代码排序。",
            "**解读**：匿名组英国集中度更高；原始净额发票均值低于非缺失组。高频代码及描述显示商品与渠道相关记录的差异，但不能只凭ID缺失判断是散客或低价值客户。匿名记录可能与未登记客户、线上渠道或历史缺录有关，仍需渠道信息确认。",
            "支持代码：`q12_customers()`、`country_pies()`，使用 groupby、crosstab 与按组发票金额均值计算。"]


def write_q12(raw, typed, output):
    """只向新分析视图加入派生列，按四个问题写出可复现证据。"""
    data = typed.copy()
    data.insert(0, "数据行序号", raw.index + 1)
    data["Amount"] = data["Quantity"] * data["Price"]
    data["AbsAmount"] = data["Amount"].abs()
    assert np.isfinite(data["Amount"]).all(), "金额存在非有限值，请先检查输入"
    if np.isclose(data["Amount"].sum(), 0) or data["AbsAmount"].sum() == 0:
        raise ValueError("金额分母为零，不能计算金额占比")
    sections = ["# Q1.2 揭示数据的真实结构",
                "本报告由 q1_code.py 的 Q1.2 函数生成。实际字段为 Invoice、Price、Customer ID；保留所有原始行、空值、空格及重复记录。"
                "Amount = Quantity × Price，未换汇；数据未标明逐笔币种，不将计算结果直接认定为经过财务确认的收入。",
                f"全表 {len(data):,} 行；带符号净金额合计 {data['Amount'].sum():,.6f}；逐行金额绝对值合计 {data['AbsAmount'].sum():,.6f}。",
                "净金额占比 = 子集带符号金额 / 全表带符号金额；负占比表示净扣减。另列绝对金额占比用于观察抵消前规模，"
                "其分子是逐行绝对值之和，不是子集净额的绝对值。比例不能视为已清洗的销售额贡献。"]
    for analyze in (q12_invoice, q12_stock, q12_extremes):
        sections.extend(analyze(data))
    sections.extend(q12_customers(data, output))
    sections += ["## 复现方式与代码定位",
                 "```bash\nconda activate gekko-de\npython q1_data_quality/q1_code.py --only-q12\n```",
                 "不加 --only-q12 时重现 Q1.1 和 Q1.2。支持代码在同目录 q1_code.py 中，函数名称与上述四步一一对应；不调用 df.describe()。",
                 "### 核心支持代码（data 是原始数据的分析副本）",
                 "```python\n# 1. 非纯数字发票和前缀\nunusual = data.loc[~data['Invoice'].str.fullmatch(r'[0-9]+')].copy()\nunusual['prefix'] = unusual['Invoice'].str.extract(r'^([^0-9]+)', expand=False).fillna('无前导非数字字符')\nprint(unusual.groupby('prefix')['Invoice'].agg(['size', 'nunique']))\n\n# 2. 不符合常规格式的商品代码与金额占比\nmask = stock_selection(data)\nprint(data.loc[mask].groupby('StockCode')['Amount'].agg(['size', 'sum']))\nprint(data.loc[mask, 'Amount'].sum() / data['Amount'].sum())\n\n# 3. 数量极值、负价格记录\nprint(data.loc[data['Quantity'].isin([80995, -80995])])\nprint(data.loc[data['Price'].lt(0)])\n\n# 4. 客户缺失占比、每发票净额\nmissing = missing_mask(data['Customer ID'])\nprint(missing.mean(), data.loc[missing, 'Amount'].sum() / data['Amount'].sum())\nprint(data.assign(missing=missing).groupby(['missing', 'Invoice'])['Amount'].sum().groupby(level=0).mean())\n```",
                 "业务含义为结合当前描述与数值的推断，不是来源系统已验证的标签。输入数据变化后，应重新审阅文字解读。"]
    output.mkdir(parents=True, exist_ok=True)
    report = output / "findings.md"
    report.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"Q1.2 分析报告：{report.resolve()}")
    return report


def main(input_path=None, output_dir=None, only_q12=False):
    source = Path(input_path) if input_path else ROOT / "data/online_retail_ii.csv"
    output = Path(output_dir) if output_dir else Path(__file__).resolve().parent
    raw, typed = load_data(source)
    if raw.empty:
        raise ValueError("输入文件没有数据行，无法生成分布报告")
    absent = set(FIELD_MAP.values()) - set(raw.columns)
    if absent:
        raise ValueError(f"实际列名为 {raw.columns.tolist()}；缺少所需字段：{sorted(absent)}")
    if only_q12:
        return write_q12(raw, typed, output)
    sections = ["# Q1.1 数据质量统计", SUSPICIOUS_FINDINGS,
                f"输入：`{source.resolve()}`；实际数据行数：{len(raw):,}；列数：{len(raw.columns)}。",
                "## 1. 实际字段与题目名称",
                table(pd.DataFrame(FIELD_MAP.items(), columns=["题目业务名称", "实际字段名"])),
                f"全部实际列名：`{raw.columns.tolist()}`。保留原始名称，不做重命名。",
                "## 2. 前五行（原始文本）", table(raw.head(5)),
                "## 3. 类型、缺失和单列重复", table(profile_columns(raw, typed)),
                "按指定类型读取：Quantity 为 int64，Price 为 float64，其余列为 str；InvoiceDate 只在时间统计的派生视图中解析为日期。"
                "缺失定义：空字段或仅空白字符，保留原值；NA/NULL 等字面文本不自动判为缺失。"
                "单列重复比例分母为该列非缺失行数；重复不等于数据错误。Customer ID 按字符串标识符处理。"]
    correspondence, distribution, examples = stock_description_profile(raw)
    sections += ["### StockCode 与 Description 对应关系",
                 "仅统计两列均非缺失的记录；按原始文本精确比较，保留首尾空格和大小写。对应数量指不同值数量，不是交易行数。",
                 table(correspondence), table(distribution),
                 "五条不同对应组合示例（优先展示对应多个描述的商品代码）：", table(examples)]
    numeric, checks = numeric_checks(raw)
    sections += ["## 4. 数值分布", table(numeric),
                 "统计排除缺失、无法解析及非有限值，原表不变。std 使用样本标准差（ddof=1）。Customer ID 是标识符，不计算金额式分布。",
                 "## 5. 类型可解析性及取值检查", table(checks)]
    sections.append("## 6. 字符串/标识符唯一值及 TOP 10")
    for name in raw.columns:
        if name in ("Quantity", "Price", "InvoiceDate"):
            continue
        values = raw.loc[~missing_mask(raw[name]), name]
        top = values.value_counts().head(10).rename_axis("原始值").reset_index(name="记录数")
        # repr 显示首尾空格，避免 Markdown 渲染掩盖原始差异。
        top["原始值"] = top["原始值"].map(repr)
        sections += [f"### {name}：非缺失唯一值 {values.nunique():,} 个", table(top)]
        sections.append(f"首尾含空白的非缺失记录数：{int((values != values.str.strip()).sum())}。")
        if name == "StockCode":
            whitespace_rows = raw.loc[values.index[values.ne(values.str.strip())]].copy()
            if not whitespace_rows.empty:
                whitespace_rows.insert(0, "数据行序号(从1开始，不含表头)", whitespace_rows.index + 1)
                # 显示完整原始记录，repr 让空白字符在报告中可见。
                for column in raw.columns:
                    whitespace_rows[column] = whitespace_rows[column].map(repr)
                sections += ["StockCode 首尾含空白的非缺失记录（完整内容）：", table(whitespace_rows)]
    sections += ["## 7. 重复记录与候选主键", table(key_checks(raw)),
                 "README 未声明交易明细主键。全列重复按原始文本精确比较；多余行数使用 keep='first'，涉及行数使用 keep=False。"
                 "候选组合仅用于检验，不能因发票号重复就删除明细，也不新增行号冒充业务主键。"]
    dates, daily, monthly = date_profile(raw)
    sections += ["## 8. 时间范围和记录数量", table(dates),
                 "按有效 InvoiceDate 统计交易明细行数（不是订单数），不推断时区；连续日期/月中无记录的区间计为 0。首尾月份可能不完整。",
                 "### 每月记录数", table(monthly), "### 每日记录数", table(daily),
                 "## 9. 后续核查说明",
                 "以上为统计和规则标记，不自动认定负值、极端值、重复记录或字母代码是错误。"
                 "报告开头已列出当前观察到的九项可疑现象，相关业务解释仍需进一步核查。"]
    output.mkdir(parents=True, exist_ok=True)
    report = output / "data_quality_report.md"
    report.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"实际列名：{raw.columns.tolist()}")
    print(f"实际行数：{len(raw):,}；列数：{len(raw.columns)}")
    print(raw.head(5).to_string(index=False))
    print(dates.to_string(index=False))
    print(f"完整统计报告：{report.resolve()}")
    write_q12(raw, typed, output)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="原始 CSV 路径")
    parser.add_argument("--output-dir", type=Path, help="报告输出目录")
    parser.add_argument("--only-q12", action="store_true", help="仅生成 Q1.2，保留现有 Q1.1 报告")
    args = parser.parse_args()
    main(args.input, args.output_dir, args.only_q12)
