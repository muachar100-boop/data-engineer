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
    """依据当前数据中的描述提出业务解释；未知代码保留不确定性。"""
    meanings = {
        "ADJUST": "人工调整（描述含操作者及时间），非普通商品销售",
        "ADJUST2": "人工调整（描述含操作者及时间），非普通商品销售",
        "AMAZONFEE": "Amazon 平台费用", "B": "坏账调整",
        "BANK CHARGES": "银行费用", "C2": "运输/运费（CARRIAGE）",
        "CRUK": "CRUK 佣金；机构及具体收费规则无法仅凭记录确定",
        "D": "折扣", "DOT": "线上渠道邮费（DOTCOM POSTAGE）",
        "M": "手工记录；需逐笔判断用途", "m": "手工记录；需逐笔判断用途",
        "PADS": "描述为配套垫片，可能是商品，不能仅因代码格式排除",
        "POST": "邮费", "S": "样品；销售归属需业务确认",
        "TEST001": "描述明确为测试商品，需核查是否测试交易",
        "TEST002": "描述明确为测试商品，需核查是否测试交易",
        "47503J ": "末尾空格造成格式不匹配，描述为园艺工具，可能仍是商品",
    }
    if code in meanings:
        return meanings[code]
    if code.startswith("gift_"):
        return "礼券相关编码；有面额描述时支持此推断，空描述记录需核查；售券不宜直接等同商品收入"
    if code.startswith("DCGS") or code == "SP1002":
        return "特殊商品/渠道编码候选；结合本行描述核查，不能仅按格式判为非商品"
    return "描述证据不足，业务含义待核查"


def q12_invoice(data):
    """步骤一：完整统计非纯数字发票的前导非数字部分及记录特征。"""
    unusual = data.loc[~data["Invoice"].str.fullmatch(r"[0-9]+")].copy()
    unusual["前缀"] = unusual["Invoice"].str.extract(r"^([^0-9]+)", expand=False).fillna("无前导非数字字符")
    summary = unusual.groupby("前缀").agg(
        行数=("Invoice", "size"), 不同发票数=("Invoice", "nunique"),
        Quantity_min=("Quantity", "min"), Quantity_max=("Quantity", "max"),
        Price_min=("Price", "min"), Price_max=("Price", "max"),
        净金额=("Amount", "sum"),
        负数量行数=("Quantity", lambda s: s.lt(0).sum()),
        正数量行数=("Quantity", lambda s: s.gt(0).sum()),
    ).reset_index()
    sections = ["## 1. Invoice 非纯数字前缀", table(summary),
                "出现次数同时给出明细行数和不同发票数，避免把订单与明细混淆。提取规则为前导连续非数字字符；无前导前缀的异常格式也会单独保留。"]
    for prefix, group in unusual.groupby("前缀"):
        descriptions = group["Description"].value_counts().head(10).rename_axis("Description").reset_index(name="行数")
        sections += [f"### 前缀 {prefix}：描述 TOP 10", table(descriptions),
                     "记录示例：", table(group.head(6))]
        exceptions = group.loc[group["Quantity"].ge(0)] if prefix == "C" else group
        if prefix == "C" and len(exceptions):
            sections += ["C 前缀中数量非负的例外（全部）：", table(exceptions)]
    sections += [
        "**解读与销售口径**：C 前缀以负数量为主，结合取消发票标记及描述，支持取消/退货/冲销推断，但数量正值例外需逐笔核查。"
        "A 前缀的描述为 Adjust bad debt，支持坏账调整解释；不能将其当作商品销售或普通退货。",
        "商品正向销售额应统计经确认的销售，C 记录单列为取消/退货，并在核实对应关系后用于销售净额；A 记录单列为财务调整，不混入商品销售额。"
        "原始带符号金额总和仅是交易记录净额，不自动等于营业收入。不能对负金额取绝对值后计入销售。",
        "支持代码：`q12_invoice()`；核心为 `Invoice.str.fullmatch(r'[0-9]+')`、`str.extract(r'^([^0-9]+)')` 和按前缀分组汇总。",
    ]
    return sections


def q12_stock(data):
    """步骤二：格式筛选不是清洗规则，展示所有代码和全部不同描述。"""
    mask = ~data["StockCode"].str.fullmatch(r"[0-9]+[A-Za-z]*")
    selected = data.loc[mask]
    summary = selected.groupby("StockCode", sort=True).agg(
        行数=("Invoice", "size"), 净金额=("Amount", "sum"),
        绝对金额和=("AbsAmount", "sum"),
        描述=("Description", lambda s: repr(s.drop_duplicates().tolist())),
    ).reset_index()
    summary["占全部行(%)"] = summary["行数"] / len(data) * 100
    summary["占全表净金额(%)"] = summary["净金额"] / data["Amount"].sum() * 100
    summary["占全表绝对金额(%)"] = summary["绝对金额和"] / data["AbsAmount"].sum() * 100
    summary["可能业务含义"] = summary["StockCode"].map(code_meaning)
    summary["StockCode"] = summary["StockCode"].map(repr)
    overall = pd.DataFrame([{
        "代码数量": selected["StockCode"].nunique(), "行数": len(selected),
        "行数占比(%)": mask.mean() * 100, "净金额": selected["Amount"].sum(),
        "净金额占比(%)": selected["Amount"].sum() / data["Amount"].sum() * 100,
        "绝对金额占比(%)": selected["AbsAmount"].sum() / data["AbsAmount"].sum() * 100,
    }])
    assert summary["行数"].sum() == len(selected)
    assert np.isclose(summary["净金额"].sum(), selected["Amount"].sum())
    return ["## 2. StockCode 格式异常与业务含义",
            "规则：纯数字 `[0-9]+` 或数字后跟一个/多个英文字母 `[0-9]+[A-Za-z]+` 为常规格式，"
            "合并写作 `[0-9]+[A-Za-z]*` 并使用 fullmatch。保留空格和大小写，空值也会落入待核查集合。",
            "### 整体占比", table(overall), "### 每种代码（全部不同描述）", table(summary),
            "**结论**：POST、DOT、C2 的描述支持运费解释；AMAZONFEE、BANK CHARGES、CRUK 支持费用/佣金解释；"
            "D 为折扣，B 为坏账调整，M/m 为手工记录。DCGS、SP1002、PADS 等可含实际商品，不能统称为非商品。"
            "礼券、样品、测试记录及空描述代码应独立核查；金额贡献为负说明存在净扣减，不代表计算错误。",
            "支持代码：`q12_stock()`、`code_meaning()`。金额占比的分母均为全表对应金额，不是筛选集合内部金额。"]


def q12_extremes(data):
    """步骤三：读取题目指定数量并补充实际最大/最小值，展示关联证据。"""
    requested = data.loc[data["Quantity"].isin([80995, -80995])]
    extrema = data.loc[data["Quantity"].isin([data["Quantity"].min(), data["Quantity"].max()])]
    sections = ["## 3. 极端数量与负价格",
                f"实际 Quantity 最小值为 {data['Quantity'].min()}，最大值为 {data['Quantity'].max()}。",
                "### Quantity 为 80995 或 -80995 的完整记录", table(requested),
                "### 实际最小/最大数量记录", table(extrema)]
    positive = requested.loc[requested["Quantity"].gt(0)]
    negative = requested.loc[requested["Quantity"].lt(0)]
    linked = positive.merge(negative, on=["Customer ID", "StockCode", "Price"], suffixes=("_正", "_负"))
    if len(linked):
        linked["间隔分钟"] = (pd.to_datetime(linked["InvoiceDate_负"]) - pd.to_datetime(linked["InvoiceDate_正"])).dt.total_seconds() / 60
        linked["合计金额"] = linked["Amount_正"] + linked["Amount_负"]
        sections += ["### 同客户、同商品、同单价的关联候选", table(linked)]
    negatives = data.loc[data["Price"].lt(0)]
    sections += ["### 负价格记录（全部）", table(negatives),
                 "### 与负价格相同 StockCode 的所有记录（检查正向调整）",
                 table(data.loc[data["StockCode"].isin(negatives["StockCode"].unique())]),
                 "**解读**：两条数量极值属于同客户、同商品、同单价，且负向发票带 C 前缀；结合时间间隔和合计金额，"
                 "强烈支持原交易被取消/冲销的解释。无法仅凭 CSV 判断最初巨量下单是否录入错误，因此不能将其当作已实现的巨额销售，也不能直接删除审计证据。",
                 "负价格记录共同出现 A 前缀、StockCode B、Description 为 Adjust bad debt、Quantity 为 1、Customer ID 缺失，国家为 United Kingdom。"
                 "同代码还存在正价格调整，说明不是简单的商品单价为负；支持财务坏账调整解释，具体会计方向仍需账务规则确认。",
                 "**处理判断**：保留全部原始记录并标记候选关联；商品销售指标与坏账调整分开，取消金额按核实后的净额口径处理。"
                 "只有来源系统确认错误且可追溯时才考虑下游纠错，当前不删除。",
                 "支持代码：`q12_extremes()`；使用 `isin`、同客户/商品/价格的 `merge` 和时间差验证关联，没有将相反数量自动当作确定匹配。"]
    return sections


def q12_customers(data):
    """步骤四：在完整记录上比较匿名与已识别群体；客单价按发票聚合。"""
    groups = data.assign(客户组=np.where(missing_mask(data["Customer ID"]), "Customer ID缺失", "Customer ID非缺失"))
    summary = groups.groupby("客户组").agg(
        行数=("Invoice", "size"), 发票数=("Invoice", "nunique"), 净金额=("Amount", "sum"),
        绝对金额和=("AbsAmount", "sum"), 平均每行金额=("Amount", "mean"))
    summary["行数占比(%)"] = summary["行数"] / len(data) * 100
    summary["净金额占比(%)"] = summary["净金额"] / data["Amount"].sum() * 100
    summary["绝对金额占比(%)"] = summary["绝对金额和"] / data["AbsAmount"].sum() * 100
    invoices = groups.groupby(["客户组", "Invoice"])["Amount"].sum()
    summary["原始净额每发票均值"] = invoices.groupby(level=0).mean()
    summary["原始净额每发票中位数"] = invoices.groupby(level=0).median()
    mixed = groups.groupby("Invoice")["客户组"].nunique().gt(1).sum()
    # 辅助观察口径，不生成清洗数据：纯数字发票、正数量、正价格。
    sale_mask = groups["Invoice"].str.fullmatch(r"[0-9]+") & groups["Quantity"].gt(0) & groups["Price"].gt(0)
    sale = groups.loc[sale_mask]
    sale_invoices = sale.groupby(["客户组", "Invoice"])["Amount"].sum()
    sales_summary = sale.groupby("客户组").agg(行数=("Invoice", "size"), 发票数=("Invoice", "nunique"), 正向金额=("Amount", "sum"))
    sales_summary["正向每发票均值"] = sale_invoices.groupby(level=0).mean()
    sales_summary["正向每发票中位数"] = sale_invoices.groupby(level=0).median()
    countries = groups.groupby(["客户组", "Country"]).agg(行数=("Invoice", "size"), 净金额=("Amount", "sum")).reset_index()
    countries["组内行数占比(%)"] = countries["行数"] / countries["客户组"].map(summary["行数"]) * 100
    countries["组内净金额占比(%)"] = countries["净金额"] / countries["客户组"].map(summary["净金额"]) * 100
    observations = []
    for label in summary.index:
        row = summary.loc[label]
        uk = countries.loc[(countries["客户组"] == label) & countries["Country"].eq("United Kingdom"), "组内行数占比(%)"].sum()
        observations.append(
            f"{label}：行数占比 {row['行数占比(%)']:.4f}%，净金额占比 {row['净金额占比(%)']:.4f}%，"
            f"英国记录占组内 {uk:.4f}%；正向每发票均值 {sales_summary.loc[label, '正向每发票均值']:.2f}、"
            f"中位数 {sales_summary.loc[label, '正向每发票中位数']:.2f}。"
        )
    sections = ["## 4. Customer ID 缺失与非缺失群体比较", table(summary.reset_index()),
                f"同一 Invoice 同时出现在两个群体的数量：{mixed}。若存在混合发票，按组得到的是发票片段，不能当成互斥订单。",
                "**客单价口径**：匿名群体无法按客户聚合，因此使用每张发票金额的均值及中位数。原始净额口径保留取消、调整、费用和重复记录，"
                "称为每发票净额，不能直接等同正常购物订单客单价。平均每行金额另列，不称为客单价。",
                "### 正向交易观察口径", table(sales_summary.reset_index()),
                "辅助口径仅选纯数字 Invoice、Quantity > 0、Price > 0，以比较正向每发票金额；这是分析视图，未删除原始行，"
                "也未排除全部特殊代码、未匹配取消、未去重，所以不是最终真实销售指标。",
                "### 国家分布（全部国家）", table(countries),
                "### 高频商品 TOP 10（正向观察口径，按出现明细行数排名）",
                "按 StockCode 汇总，不按描述拆分；相同次数按代码排序。排名未排除特殊代码，因此展示的是高频代码候选，描述用于人工区分商品与费用。"]
    sections += ["### 数据支持的群体差异", *observations,
                 "本次匿名组的正向每发票均值高于已识别组，中位数却低于已识别组，显示均值可能受到较大金额发票影响；"
                 "匿名组英国集中度也更高。因此不能从较低的平均每行金额直接得出匿名客户客单价更低的结论。"]
    for label, group in sale.groupby("客户组"):
        top = group.groupby("StockCode").agg(
            行数=("Invoice", "size"), 不同发票数=("Invoice", "nunique"),
            数量合计=("Quantity", "sum"), 正向金额=("Amount", "sum"),
            描述示例=("Description", lambda s: repr(s.drop_duplicates().head(3).tolist())),
        ).reset_index().sort_values(["行数", "StockCode"], ascending=[False, True]).head(10)
        sections += [f"#### {label}", table(top)]
        conventional = group.loc[group["StockCode"].str.fullmatch(r"[0-9]+[A-Za-z]*")]
        product_top = conventional.groupby("StockCode").agg(
            行数=("Invoice", "size"), 不同发票数=("Invoice", "nunique"),
            描述示例=("Description", lambda s: repr(s.drop_duplicates().head(3).tolist())),
        ).reset_index().sort_values(["行数", "StockCode"], ascending=[False, True]).head(10)
        sections += ["常规格式商品代码候选 TOP 10（辅助视图，可能遗漏特殊编码的真实商品）：", table(product_top)]
    assert summary["行数"].sum() == len(data)
    assert np.isclose(summary["净金额"].sum(), data["Amount"].sum())
    sections += ["**本次高频差异**：匿名组原始代码排名首位为 DOT（DOTCOM POSTAGE），提示其可能包含线上渠道相关的匿名记录；"
                 "常规商品候选中匿名组以 85099B 为首，已识别组以 85123A 为首，且两组都出现 22423、85099B、21212、47566 等代码，商品偏好存在重叠。",
                 "**群体解释边界**：比较上表的组内国家占比、正向每发票均值/中位数和高频代码，才能判断群体差异。"
                 "匿名记录可能来自未登记客户、特定渠道或历史系统缺录；这只是候选解释。CSV 缺少渠道、注册状态及缺失原因字段，"
                 "不能把缺失 ID 直接解释为游客、散客或低价值客户。应保留其对交易金额的贡献，但不伪造客户 ID 进行 RFM。",
                 "支持代码：`q12_customers()`；使用 `missing_mask`、按客户组/Invoice 聚合金额、按组/Country 统计及按组/StockCode 排名。"]
    return sections


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
    for analyze in (q12_invoice, q12_stock, q12_extremes, q12_customers):
        sections.extend(analyze(data))
    sections += ["## 复现方式与代码定位",
                 "```bash\nconda activate gekko-de\npython q1_data_quality/q1_code.py --only-q12\n```",
                 "不加 --only-q12 时重现 Q1.1 和 Q1.2。支持代码在同目录 q1_code.py 中，函数名称与上述四步一一对应；不调用 df.describe()。",
                 "### 核心支持代码（data 是原始数据的分析副本）",
                 "```python\n# 1. 非纯数字发票和前缀\nunusual = data.loc[~data['Invoice'].str.fullmatch(r'[0-9]+')].copy()\nunusual['prefix'] = unusual['Invoice'].str.extract(r'^([^0-9]+)', expand=False).fillna('无前导非数字字符')\nprint(unusual.groupby('prefix')['Invoice'].agg(['size', 'nunique']))\n\n# 2. 不符合常规格式的商品代码与金额占比\nmask = ~data['StockCode'].str.fullmatch(r'[0-9]+[A-Za-z]*')\nprint(data.loc[mask].groupby('StockCode')['Amount'].agg(['size', 'sum']))\nprint(data.loc[mask, 'Amount'].sum() / data['Amount'].sum())\n\n# 3. 数量极值、负价格记录\nprint(data.loc[data['Quantity'].isin([80995, -80995])])\nprint(data.loc[data['Price'].lt(0)])\n\n# 4. 客户缺失占比、每发票净额\nmissing = missing_mask(data['Customer ID'])\nprint(missing.mean(), data.loc[missing, 'Amount'].sum() / data['Amount'].sum())\nprint(data.assign(missing=missing).groupby(['missing', 'Invoice'])['Amount'].sum().groupby(level=0).agg(['mean', 'median']))\n```",
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
