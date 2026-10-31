"""Q4 正式抽取：规则分流 + gpt-4o-mini（v8 Prompt）批量抽取 + 引句修复 + gpt-4.1-mini 单条兜底。

配置与独立验证 W01–W30 / X01–X30 上的候选完全相同（versions/v8 Prompt、每批 5 条、run_v8 的请求体与后处理）。
验证结论与取舍见 versions/v10/v10_plan.json：在 $3 预算内选用 gpt-4o-mini；它的四字段匹配比 v6+gpt-4.1-mini 少 2–4 条（n=30）。

流程：
1. 读取评分 ≤ 3 的全部评价（22,754 行），rules_v8 处理空文本、泛化短语和完整短语（8,610 行，不调用模型）。
2. 其余按（标题, 正文, 评分）去重为独立输入，每批 5 条发给 gpt-4o-mini，8 个线程并发。
3. 逐条校验 Schema 与原文连续引句；仅大小写不同则修复，否则单条用 gpt-4.1-mini 兜底，兜底仍失败记入死信。
4. 传输错误（429/5xx/超时）最多重试 2 次；预算或 40 分钟时限触及时停止发新请求，已完成结果保留，可续跑。
5. --assemble 把结果按 review_id 展开、逐条再校验，写出 extracted_issues.json 与 full_run/report.json。

用法（项目根目录，gekko-de 环境）：
    python q4_llm_extraction/extract_reviews.py               # 离线：分流统计、请求计划与预估，不读 Key
    python q4_llm_extraction/extract_reviews.py --live        # 付费全量（可中断后续跑，已完成批次不重发）
    python q4_llm_extraction/extract_reviews.py --assemble    # 离线：由结果缓存生成 extracted_issues.json
"""
import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

B = Path(__file__).resolve().parent
sys.path[:0] = [str(B / 'versions/v10'), str(B / 'versions/v8'), str(B / 'round3_cost_optimization'), str(B / 'model_trial'), str(B), str(B.parent)]
import model_trial as mt  # noqa: E402
import run_v8  # noqa: E402

STATE = B / 'full_run'
OUTPUT = B / 'extracted_issues.json'
PRIMARY = mt.ARMS['gpt-4o-mini']
FALLBACK = run_v8.FALLBACK
BATCH = 5
WORKERS = 8
PROJECT_BUDGET_USD = 3.0
DEADLINE_SECONDS = 40 * 60
STOP_SUBMIT_SECONDS = 37 * 60  # 留 3 分钟给在途请求与兜底
TRANSPORT_RETRIES = 2


def history_usd():
    """开发阶段全部已承诺费用（含失败请求的保留预留），作为全量预算的已用部分。"""
    import run_v10
    return mt.prior_commitment() + run_v10.committed()


def load_records():
    from analyze import records
    return records()


def key_of(r):
    return hashlib.sha256(json.dumps([r['raw_title'], r['raw_message'], r['review_score']], ensure_ascii=False).encode()).hexdigest()


def split(records):
    """返回 (规则输出 {行号: 输出}, 模型独立输入 [样本])。"""
    from rules_v8 import rule_extract
    rules, unique = {}, {}
    for i, r in enumerate(records):
        out = rule_extract(r)
        if out is not None:
            rules[i] = out
        else:
            unique.setdefault(key_of(r), r)
    inputs = [dict(sample_id='R%05d' % n, key=k, review_id=r['review_id'], review_score=r['review_score'],
                   raw_title=r['raw_title'], raw_message=r['raw_message']) for n, (k, r) in enumerate(sorted(unique.items()))]
    return rules, inputs


def batches(inputs):
    return [inputs[i:i + BATCH] for i in range(0, len(inputs), BATCH)]


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class Run:
    """线程安全的预算/时限/落盘状态。results.jsonl 每个独立输入一行，events.jsonl 每次请求一行。"""

    def __init__(self, state, transport, budget_left, clock=time.time):
        self.state, self.transport, self.clock = state, transport, clock
        state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.done = {x['key'] for x in read_jsonl(state / 'results.jsonl')}
        self.dead = {x['key'] for x in read_jsonl(state / 'deadletter.jsonl')}
        self.spent = sum(e['cost_usd'] for e in read_jsonl(state / 'events.jsonl'))
        self.budget_left, self.held = budget_left, 0.0
        meta = state / 'meta.json'
        self.started = mt.load_json(meta)['first_request_at'] if meta.exists() else None
        self.stopped = None

    def append(self, name, obj):
        with (self.state / name).open('a') as f:
            f.write(json.dumps(obj, ensure_ascii=False) + '\n')

    def elapsed(self):
        return self.clock() - self.started if self.started is not None else 0.0

    def reserve(self, fee):
        with self.lock:
            if self.started is None:
                self.started = self.clock()
                mt.atomic_json(self.state / 'meta.json', dict(first_request_at=self.started))
            if self.elapsed() > STOP_SUBMIT_SECONDS:
                self.stopped = self.stopped or 'deadline'
                return False
            if self.spent + self.held + fee > self.budget_left:
                self.stopped = self.stopped or 'budget'
                return False
            self.held += fee
            return True

    def call(self, kind, body, arm, sample_ids):
        """带传输重试的单次付费调用；返回解析后的 JSON，未发送或失败返回 None。"""
        fee = mt.reserve(body, arm)
        for attempt in range(TRANSPORT_RETRIES + 1):
            if not self.reserve(fee):
                return None
            t0, raw, err = self.clock(), None, None
            try:
                raw = self.transport(body)
            except Exception as e:  # noqa: BLE001 — 记录后决定是否重试
                err = '%s: %s' % (type(e).__name__, str(e)[:500])
            usage = (raw or {}).get('usage', {})
            # 无 usage 的失败按全额预留计入已花费，不假设免费
            cost = mt.usage_cost(usage, arm) if usage else (0.0 if raw else fee)
            with self.lock:
                self.held -= fee
                self.spent += cost
                self.append('events.jsonl', dict(kind=kind, model=arm['model'], sample_ids=sample_ids, attempt=attempt, error=err,
                                                 usage=usage, cost_usd=cost, seconds=round(self.clock() - t0, 2), at=self.clock()))
            if raw is not None:
                try:
                    return run_v8.parse(raw)
                except ValueError as e:
                    self.append('events.jsonl', dict(kind=kind, sample_ids=sample_ids, error='parse: %s' % e, cost_usd=0.0, at=self.clock()))
                    return None
            if not any(code in (err or '') for code in ('HTTP 429', 'HTTP 5', 'Timeout', 'timed out', 'URLError')):
                return None
            time.sleep(2 * 4 ** attempt)
        return None

    def run_batch(self, batch):
        data = self.call('primary', run_v8.v7_body(batch, PRIMARY, 'current'), PRIMARY, [s['sample_id'] for s in batch])
        if data is None:
            # 传输失败（含重试后仍 429）或已停止：留给续跑，不转 gpt-4.1-mini 单条兜底，避免限流时成本放大
            return
        out, repairs, fb = run_v8.process(data, batch)
        by = {s['sample_id']: s for s in batch}
        for sid, o in out.items():
            self.save(by[sid], o, PRIMARY['model'], [r for r in repairs if r['sample_id'] == sid])
        for sid, reasons in fb.items():
            s = by[sid]
            d = self.call('fallback', run_v8.v7_body([s], FALLBACK, 'current'), FALLBACK, [sid])
            if d is None and self.stopped:
                return
            o2, rep2, fb2 = run_v8.process(d, [s]) if d is not None else ({}, [], {sid: ['request_failed']})
            if sid in o2:
                self.save(s, o2[sid], FALLBACK['model'], rep2, reasons)
            else:
                with self.lock:
                    self.dead.add(s['key'])
                    self.append('deadletter.jsonl', dict(key=s['key'], sample_id=sid, primary_reasons=reasons, fallback_reasons=fb2.get(sid)))

    def save(self, s, out, model, repairs, fallback_reasons=None):
        with self.lock:
            self.done.add(s['key'])
            self.append('results.jsonl', dict(key=s['key'], sample_id=s['sample_id'], model=model, result=out,
                                              repairs=repairs, fallback_reasons=fallback_reasons))


def execute(inputs, transport, state=STATE, budget_left=None, workers=WORKERS):
    if budget_left is None:
        budget_left = PROJECT_BUDGET_USD - history_usd()
    run = Run(state, transport, budget_left)
    todo = [b for b in batches(inputs) if any(s['key'] not in run.done and s['key'] not in run.dead for s in b)]
    todo = [[s for s in b if s['key'] not in run.done and s['key'] not in run.dead] for b in todo]
    with ThreadPoolExecutor(workers) as pool:
        for n, f in enumerate([pool.submit(run.run_batch, b) for b in todo], 1):
            f.result()
            if n % 100 == 0 or n == len(todo):
                print('进度 %d/%d 批；完成 %d 条，死信 %d；已花费 $%.4f；%.1f 分钟' % (
                    n, len(todo), len(run.done), len(run.dead), run.spent, run.elapsed() / 60), flush=True)
    return run


def assemble(records, rules, inputs, state=STATE):
    from validate_output import validate
    results = {x['key']: x for x in read_jsonl(state / 'results.jsonl')}
    dead = {x['key'] for x in read_jsonl(state / 'deadletter.jsonl')}
    out, seen, missing, invalid, models = [], set(), [], [], {}
    for i, r in enumerate(records):
        if i in rules:
            o, model = rules[i], 'rules_v8'
        else:
            x = results.get(key_of(r))
            if x is None:
                missing.append(dict(review_id=r['review_id'], reason='deadletter' if key_of(r) in dead else 'not_processed'))
                continue
            o, model = dict(x['result'], review_id=r['review_id']), x['model']
        ident = (r['review_id'], r['raw_title'], r['raw_message'])
        if ident in seen:  # 同一 review_id 关联多个订单：内容相同只输出一次
            continue
        v = validate(o, r)
        if not v['valid']:
            invalid.append(dict(review_id=r['review_id'], errors=v['errors']))
            continue
        seen.add(ident)
        models[model] = models.get(model, 0) + 1
        out.append(o)
    mt.atomic_json(OUTPUT if state == STATE else state / 'extracted_issues.json', out)
    events = read_jsonl(state / 'events.jsonl')
    meta = state / 'meta.json'
    first = mt.load_json(meta)['first_request_at'] if meta.exists() else None
    report = dict(rows=len(records), rule_rows=len(rules), model_unique_inputs=len(inputs), output_records=len(out), by_source=models,
                  missing=len(missing), invalid=len(invalid), missing_examples=missing[:20], invalid_examples=invalid[:20],
                  requests=len(events), primary_requests=sum(e['kind'] == 'primary' for e in events),
                  fallback_requests=sum(e['kind'] == 'fallback' for e in events),
                  transport_errors=sum(1 for e in events if e.get('error')),
                  input_tokens=sum(e.get('usage', {}).get('prompt_tokens', 0) for e in events),
                  cached_tokens=sum((e.get('usage', {}).get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0 for e in events),
                  output_tokens=sum(e.get('usage', {}).get('completion_tokens', 0) for e in events),
                  run_cost_usd=round(sum(e['cost_usd'] for e in events), 6),
                  wall_minutes=round((max(e['at'] for e in events) - first) / 60, 1) if events and first else None,
                  note='费用按公开单价与 usage 估算，不是账单核实；以 OpenAI 账单为准')
    mt.atomic_json(state / 'report.json', report)
    return out, report


def check_prompts():
    """冻结的 v8 Prompt 未改动，且提交用的根目录 prompt_template.txt 与它逐字一致。"""
    from prepare_v8 import verify as verify_v8
    verify_v8()
    root = (B / 'prompt_template.txt').read_text().split('=== SYSTEM ===', 1)[1].split('=== END SYSTEM ===', 1)[0].strip()
    if root != (B / 'versions/v8/prompt_template.txt').read_text().strip():
        raise SystemExit('根目录 prompt_template.txt 与 versions/v8/prompt_template.txt 不一致')


def assemble_offline():
    """离线：由 full_run/ 结果缓存重建 extracted_issues.json（不读 Key、不发请求）；供 run_all.py 调用。"""
    check_prompts()
    if not (STATE / 'results.jsonl').exists():
        raise FileNotFoundError('没有 full_run/results.jsonl：先运行 extract_reviews.py --live')
    records = load_records()
    rules, inputs = split(records)
    _, report = assemble(records, rules, inputs)
    return {k: v for k, v in report.items() if not k.endswith('examples')}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--assemble', action='store_true')
    a = ap.parse_args()
    if a.assemble:
        print(json.dumps(assemble_offline(), ensure_ascii=False, indent=2))
        return
    check_prompts()
    records = load_records()
    rules, inputs = split(records)
    hist = history_usd()
    done = {x['key'] for x in read_jsonl(STATE / 'results.jsonl')}
    todo = [s for s in inputs if s['key'] not in done]
    upper = sum(mt.reserve(run_v8.v7_body(b, PRIMARY, 'current'), PRIMARY) for b in batches(todo))
    print(json.dumps(dict(rows=len(records), rule_rows=len(rules), model_unique_inputs=len(inputs), remaining_inputs=len(todo),
                          requests=-(-len(todo) // BATCH), workers=WORKERS, history_commitment_usd=round(hist, 4),
                          budget_left_usd=round(PROJECT_BUDGET_USD - hist, 4), reserved_upper_usd=round(upper, 2),
                          estimate_from_x_holdout='约 $1.56 主流程 + $0.33 兜底（含 10% 重试），8 并发约 28 分钟'),
                     ensure_ascii=False, indent=2))
    if not a.live:
        print('离线模式：未读取Key、未发送请求。加 --live 付费运行全量。')
        return
    run = execute(inputs, mt.http_transport(mt.env_key()))
    print('停止原因：%s；已花费（本次全量）$%.4f；用时 %.1f 分钟' % (run.stopped or '全部完成', run.spent, run.elapsed() / 60))
    _, report = assemble(records, rules, inputs)
    print(json.dumps({k: v for k, v in report.items() if not k.endswith('examples')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
