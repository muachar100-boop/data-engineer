"""离线重算 accuracy_evaluation.md 中 W、X 两批独立样本的全部结果。不联网、不读 Key，只用本目录的数据。

数据（每批一个子目录）：
- samples.json               抽样得到的 30 条模型路由评论
- human_labels.csv           人工标签（运行模型之前封存；哈希见 sealed_labels_manifest.json）
- decision_rule.json         运行之前登记的判定规则
- outputs.json               各组模型的最终输出（已通过引句校验）；X 批另存对照组 X28 未通过校验的原始结果

用法（项目根目录）：
    python q4_llm_extraction/evaluation/evaluate.py           # 打印各批与合并结果
    python q4_llm_extraction/evaluation/evaluate.py --check   # 并断言与 accuracy_evaluation.md 中的数字一致
"""
import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from extract_reviews import validate  # noqa: E402

RANK = {'low': 0, 'medium': 1, 'high': 2}
CANDIDATE, CONTROL = 'gpt-4o-mini', 'v6-gpt-4.1-mini'
X28_RAW = 'v6-gpt-4.1-mini_X28_raw_failed_quote_check'


def to_level(x):
    """登记的情绪评分口径：|x|<0.15→0；0.15≤|x|<0.65→±0.5；|x|≥0.65→±1（5 档值映射到自身）。"""
    a = abs(x)
    if a < 0.15:
        return 0
    return (1 if a >= 0.65 else 0.5) * (1 if x > 0 else -1)


def load(batch):
    d = HERE / batch
    sealed = json.loads((d / 'sealed_labels_manifest.json').read_text())
    labels = (d / 'human_labels.csv').read_bytes()
    assert hashlib.sha256(labels).hexdigest() == sealed['labels_sha256'], batch + '：标签与封存哈希不一致'
    with (d / 'human_labels.csv').open(encoding='utf-8-sig', newline='') as f:
        gold = {r['sample_id']: r for r in csv.DictReader(f)}
    samples = {s['sample_id']: s for s in json.loads((d / 'samples.json').read_text())}
    return gold, samples, json.loads((d / 'outputs.json').read_text())


def main_fields(result):
    sev = {}
    for i in result['issues']:
        c = i['category']
        if c not in sev or RANK[i['severity']] > RANK[sev[c]]:
            sev[c] = i['severity']
    return sorted(sev), sev


def compare(result, gold):
    cats, sev = main_fields(result)
    exp = json.loads(gold['expected_categories_json'])
    r = dict(expected=exp, actual=cats,
             category=set(exp) == set(cats),
             category_severity=json.loads(gold['expected_severity_json']) == sev,
             sentiment=to_level(result['sentiment_score']) == float(gold['expected_sentiment_level']),
             actionable=result['actionable_for_seller'] == (gold['expected_actionable_for_seller'] == 'true'))
    r['four_fields'] = r['category_severity'] and r['category'] and r['sentiment'] and r['actionable']
    return r


def metrics(out, gold):
    rows = [compare(out[s], gold[s]) for s in sorted(gold)]
    tp = sum(len(set(r['expected']) & set(r['actual'])) for r in rows)
    fp = sum(len(set(r['actual']) - set(r['expected'])) for r in rows)
    fn = sum(len(set(r['expected']) - set(r['actual'])) for r in rows)
    m = {k: sum(r[k] for r in rows) for k in ('category', 'category_severity', 'sentiment', 'actionable', 'four_fields')}
    m['n'] = len(rows)
    m['micro_f1'] = round(2 * tp / (2 * tp + fp + fn), 4) if 2 * tp + fp + fn else None
    return m


def wilson(k, n, z=1.96):
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / 2 / n) / d, z * math.sqrt(p * (1 - p) / n + z * z / 4 / n / n) / d
    return round(100 * (c - h)), round(100 * (c + h))


def evaluate():
    res = {}
    for batch in ('W', 'X'):
        gold, samples, data = load(batch)
        arms = dict(data['arms'])
        for arm, out in arms.items():
            for sid, o in out.items():  # 提交的输出都必须仍能通过引句与 Schema 校验
                v = validate(o, samples[sid])
                assert v['valid'], (batch, arm, sid, v['errors'])
        if batch == 'X':
            # 对照组 X28 缺失：默认用未通过引句校验的原始结果比较四字段（与文档主表一致），另报两种处理
            raw = data[X28_RAW]
            res['X_x28'] = {
                'a_both_drop_X28': {a: metrics({k: v for k, v in arms[a].items() if k != 'X28'},
                                               {k: v for k, v in gold.items() if k != 'X28'}) for a in arms},
                'c_control_X28_all_wrong': {CONTROL: metrics(dict(arms[CONTROL], X28=dict(
                    issues=[], sentiment_score=9, actionable_for_seller=None)), gold)}}
            arms[CONTROL] = dict(arms[CONTROL], X28=raw)
        res[batch] = {a: metrics(o, gold) for a, o in arms.items()}
    pooled = {}
    for a in (CANDIDATE, CONTROL):
        pooled[a] = {k: res['W'][a][k] + res['X'][a][k] for k in ('category', 'category_severity', 'sentiment', 'actionable', 'four_fields', 'n')}
        pooled[a]['wilson_95'] = {k: wilson(pooled[a][k], pooled[a]['n']) for k in ('category', 'sentiment', 'actionable', 'four_fields')}
    res['pooled'] = pooled
    return res


EXPECTED = {  # accuracy_evaluation.md 中引用的数字
    ('W', 'gpt-4o-mini'): dict(category=14, category_severity=13, sentiment=23, actionable=30, four_fields=10, micro_f1=0.6849),
    ('W', 'v8-gpt-4.1-mini'): dict(category=18, category_severity=18, sentiment=23, actionable=30, four_fields=15, micro_f1=0.7429),
    ('W', 'v6-gpt-4.1-mini'): dict(category=16, category_severity=16, sentiment=24, actionable=29, four_fields=12, micro_f1=0.7059),
    ('X', 'gpt-4o-mini'): dict(category=20, category_severity=18, sentiment=24, actionable=26, four_fields=13, micro_f1=0.7797),
    ('X', 'v6-gpt-4.1-mini'): dict(category=21, category_severity=20, sentiment=24, actionable=25, four_fields=17, micro_f1=0.8),
    ('pooled', 'gpt-4o-mini'): dict(category=34, category_severity=31, sentiment=47, actionable=56, four_fields=23),
    ('pooled', 'v6-gpt-4.1-mini'): dict(category=37, category_severity=36, sentiment=48, actionable=54, four_fields=29),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    res = evaluate()
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.check:
        bad = [(k, f, res[k[0]][k[1]][f], v) for k, exp in EXPECTED.items() for f, v in exp.items() if res[k[0]][k[1]][f] != v]
        assert not bad, bad
        x = res['X_x28']
        assert (x['a_both_drop_X28'][CANDIDATE]['four_fields'], x['a_both_drop_X28'][CONTROL]['four_fields']) == (12, 17)
        assert x['c_control_X28_all_wrong'][CONTROL]['four_fields'] == 17
        print('CHECK PASS：与 accuracy_evaluation.md 的数字一致')


if __name__ == '__main__':
    main()
