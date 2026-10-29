"""Q1.1：只读质量探索；原始 CSV 不删除、不填补、不覆盖。"""

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


def main(input_path=None, output_dir=None):
    source = Path(input_path) if input_path else ROOT / "data/online_retail_ii.csv"
    output = Path(output_dir) if output_dir else Path(__file__).resolve().parent
    raw, typed = load_data(source)
    if raw.empty:
        raise ValueError("输入文件没有数据行，无法生成分布报告")
    absent = set(FIELD_MAP.values()) - set(raw.columns)
    if absent:
        raise ValueError(f"实际列名为 {raw.columns.tolist()}；缺少所需字段：{sorted(absent)}")
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
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="原始 CSV 路径")
    parser.add_argument("--output-dir", type=Path, help="报告输出目录")
    args = parser.parse_args()
    main(args.input, args.output_dir)
