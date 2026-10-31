"""Q4 正式抽取：规则分流 + gpt-4o-mini 批量抽取 + 引句修复 + gpt-4.1-mini 单条兜底。

单文件、可独立运行：只依赖同目录的 prompt_template.txt 与原始数据 data/olist/olist_order_reviews_dataset.csv。
规则、Schema 与校验、请求体、计价都写在本文件中，与开发阶段验证过的实现逐字节等价
（规则输出、全部 2,780 个请求体、由缓存重建的 extracted_issues.json 均已离线比对一致）。
在 $3 预算内选用 gpt-4o-mini：独立样本上四字段匹配比 gpt-4.1-mini 少 2–4 条（n=30），见 accuracy_evaluation.md。

流程：
1. 读取评分 ≤ 3 的全部评价（22,754 行）；规则处理空文本、泛化短语和整字段完全匹配的短语（8,610 行，不调用模型）。
2. 其余按（标题, 正文, 评分）去重为独立输入（13,898 个），每批 5 条发给 gpt-4o-mini，8 个线程并发。
3. 逐条校验 Schema 与原文连续引句；仅大小写不同则修复，否则单条用 gpt-4.1-mini 兜底，兜底仍失败记入死信。
4. 传输错误（429/5xx/超时）最多重试 2 次，仍失败留给续跑；预算或 40 分钟时限触及时停止发新请求。
5. 结果逐条写入 full_run/，--assemble 按 review_id 展开、逐条再校验，写出 extracted_issues.json 与 full_run/report.json。

用法（项目根目录）：
    python q4_llm_extraction/extract_reviews.py               # 离线：分流统计、请求计划与预估，不读 Key
    python q4_llm_extraction/extract_reviews.py --live        # 付费全量（可中断后续跑，已完成批次不重发）
    python q4_llm_extraction/extract_reviews.py --assemble    # 离线：由结果缓存生成 extracted_issues.json
    python q4_llm_extraction/extract_reviews.py --cost        # 离线：由 full_run/events.jsonl 重算成本分项
API Key 从环境变量 OPENAI_API_KEY 或仓库根目录 .env 读取，从不打印。
"""
import argparse
import csv
import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

B = Path(__file__).resolve().parent
ROOT = B.parent
REVIEWS_CSV = ROOT / 'data/olist/olist_order_reviews_dataset.csv'
PROMPT_FILE = B / 'prompt_template.txt'
# 冻结的 v8 system prompt（prompt_template.txt 的 SYSTEM 段）的 SHA256，防止提交的 Prompt 与运行的不一致
PROMPT_SHA256 = 'a7b37981d54e8bfcfb78ff4e3461378ef5c540f8ce2058bab2a4e3a1760f13a3'
STATE = B / 'full_run'
OUTPUT = B / 'extracted_issues.json'

# 价格核对日期 2026-11-01（每百万 token，美元）；费用按 usage 估算，以 OpenAI 账单为准
PRIMARY = dict(model='gpt-4o-mini-2024-07-18', input_rate=0.15, cached_input_rate=0.075, output_rate=0.60)
FALLBACK = dict(model='gpt-4.1-mini-2025-04-14', input_rate=0.40, cached_input_rate=0.10, output_rate=1.60)
MAX_OUT_PER_REVIEW = 500
BATCH = 5
WORKERS = 8
PROJECT_BUDGET_USD = 3.0
# 开发与验证阶段（v2–v10、换模型小试）全部已承诺费用，含失败请求的保留预留；明细见 cost_report.md
DEV_COMMITMENT_USD = 0.30133195
STOP_SUBMIT_SECONDS = 37 * 60  # 40 分钟时限，留 3 分钟给在途请求与兜底
TRANSPORT_RETRIES = 2

# ---------------------------------------------------------------- 输出 Schema

CATEGORIES = ['delivery_late', 'wrong_product', 'quality_defect', 'missing_item', 'seller_communication', 'packaging', 'other']
SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Q4 review extraction v2',
    'type': 'object',
    'properties': {
        'review_id': {'type': 'string', 'minLength': 1},
        'issues': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'category': {'type': 'string', 'enum': CATEGORIES},
                'severity': {'type': 'string', 'enum': ['low', 'medium', 'high']},
                'evidence_quote': {'type': 'string', 'minLength': 1},
                'evidence_quote_en': {'type': 'string', 'minLength': 1}},
            'required': ['category', 'severity', 'evidence_quote', 'evidence_quote_en'],
            'additionalProperties': False}},
        'sentiment_score': {'type': 'number', 'minimum': -1, 'maximum': 1},
        'actionable_for_seller': {'type': 'boolean'},
        'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1}},
    'required': ['review_id', 'issues', 'sentiment_score', 'actionable_for_seller', 'confidence'],
    'additionalProperties': False,
}


def batch_schema():
    """一次请求多条评论：{"results": [{"sample_id", "result"}]}。"""
    item = {'type': 'object', 'properties': {'sample_id': {'type': 'string'},
                                             'result': {k: v for k, v in SCHEMA.items() if k not in ['$schema', 'title']}},
            'required': ['sample_id', 'result'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {'results': {'type': 'array', 'items': item}}, 'required': ['results'], 'additionalProperties': False}


def strict_loads(text):
    """拒绝重复键和 NaN/Infinity。"""
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError('重复JSON键:' + k)
            d[k] = v
        return d

    def invalid(value):
        raise ValueError('非法JSON常数:' + value)
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def schema_errors(value, schema=SCHEMA, path='$'):
    """本项目 Schema 用到的严格子集：type / enum / required / additionalProperties / minLength / 数值范围。"""
    errors = []
    typ = schema['type']
    valid = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
             'string': lambda: isinstance(value, str), 'number': lambda: type(value) in (int, float) and math.isfinite(value),
             'boolean': lambda: type(value) is bool}[typ]()
    if not valid:
        return [path + ':类型错误，需要' + typ]
    if 'enum' in schema and value not in schema['enum']:
        errors.append(path + ':枚举值错误')
    if typ == 'object':
        errors += [path + ':缺少字段 ' + k for k in schema['required'] if k not in value]
        errors += [path + ':多余字段 ' + k for k in value if k not in schema['properties']]
        for k, v in value.items():
            if k in schema['properties']:
                errors += schema_errors(v, schema['properties'][k], path + '.' + k)
    elif typ == 'array':
        for i, v in enumerate(value):
            errors += schema_errors(v, schema['items'], f'{path}[{i}]')
    elif typ == 'string':
        if len(value) < schema.get('minLength', 0) or ('minLength' in schema and not value.strip()):
            errors.append(path + ':空字符串')
    elif typ == 'number':
        if value < schema.get('minimum', float('-inf')) or value > schema.get('maximum', float('inf')):
            errors.append(path + ':超出范围')
    return errors


def validate(value, sample):
    """Schema + review_id 一致 + 每条证据是原标题或原正文的连续子串 + 无重复问题。翻译语义不能由程序证明。"""
    errors = schema_errors(value)
    if not errors:
        if value['review_id'] != sample['review_id']:
            errors.append('review_id与输入不一致')
        seen = set()
        for i, issue in enumerate(value['issues']):
            quote = issue['evidence_quote']
            if not any(quote in sample[k] for k in ['raw_title', 'raw_message']):
                errors.append(f'issues[{i}]:证据不是原标题或正文连续子串')
            key = (issue['category'], quote)
            if key in seen:
                errors.append(f'issues[{i}]:重复问题')
            seen.add(key)
    return {'valid': not errors, 'errors': errors}

# ---------------------------------------------------------------- 读取与规则分流


def normalize(value):
    # 不删除否定词、标点、数字；原文独立保存
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value)).strip().casefold()


def match_form(value):
    value = ''.join(c for c in unicodedata.normalize('NFD', value) if unicodedata.category(c) != 'Mn')
    return value.strip(' .!?,;:\"\'\t\r\n')


def load_records(path=REVIEWS_CSV):
    """评分 ≤ 3 的全部评价，按原文件顺序。"""
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        columns = next(reader)
        rows = [dict(zip(columns, r)) for r in reader]
    return [dict(review_id=r['review_id'], order_id=r['order_id'], review_score=int(r['review_score']),
                 raw_title=r['review_comment_title'], raw_message=r['review_comment_message'])
            for r in rows if r['review_score'] in {'1', '2', '3'}]


# 整字段完全匹配（标题、正文只允许一个为空或二者是同一短语）；情绪值为 5 档：-1, -0.5, 0, 0.5, 1。
# 加强语气与明确负面情绪 → -1；普通负面 → -0.5；ok/bom/gostei/recomendo → 0.5；muito bom/ótimo/excelente → 1。
GENERIC = {
    'ruim': -.5, 'muito ruim': -1, 'pessimo': -1, 'pessima': -1, 'horrivel': -1,
    'bom': .5, 'boa': .5, 'muito bom': 1, 'muito boa': 1, 'excelente': 1, 'otimo': 1, 'otima': 1,
    'recomendo': .5, 'recomendo o produto': .5, 'nao recomendo': -.5, 'gostei': .5, 'nao gostei': -.5,
    'estou bem chateada': -1, 'estou bem chateado': -1, 'ok': .5, 'regular': 0,
}
EXACT = {
    'produto errado': ('wrong_product', 'medium', 'Wrong product', -.5, True),
    'recebi o produto errado': ('wrong_product', 'medium', 'I received the wrong product', -.5, True),
    'entrega atrasada': ('delivery_late', 'medium', 'Late delivery', -.5, True),
    'atraso na entrega': ('delivery_late', 'medium', 'Delivery delay', -.5, True),
    'veio com defeito': ('quality_defect', 'medium', 'It arrived defective', -.5, True),
    'produto com defeito': ('quality_defect', 'medium', 'Defective product', -.5, True),
    'pessimo atendimento': ('seller_communication', 'low', 'Terrible service', -1, True),
    'produto de pessima qualidade': ('other', 'low', 'Product of terrible quality', -1, False),
    'pessima qualidade': ('other', 'low', 'Terrible quality', -1, False),
    'nao recebi o produto': ('other', 'medium', 'I have not received the product', -.5, True),
    'ainda nao recebi o produto': ('other', 'medium', 'I still have not received the product', -.5, True),
}


def rule_extract(sample):
    """能由规则确定时返回输出，否则返回 None（交给模型）。不使用评分推断问题。"""
    title, body = sample['raw_title'], sample['raw_message']
    parts = [s for s in (title, body) if normalize(s)]
    if not any(c.isalnum() for c in title + ' ' + body):
        return {'review_id': sample['review_id'], 'issues': [], 'sentiment_score': 0, 'actionable_for_seller': False, 'confidence': 0}
    forms = {match_form(normalize(s)) for s in parts}
    if len(forms) != 1:
        return None  # 标题与正文不同语境，不能只看一个字段
    form = next(iter(forms))
    out = {'review_id': sample['review_id'], 'issues': [], 'sentiment_score': 0, 'actionable_for_seller': False, 'confidence': .9}
    if form in GENERIC:
        out['sentiment_score'] = GENERIC[form]
        return out
    if form not in EXACT:
        return None
    category, severity, english, sentiment, actionable = EXACT[form]
    out.update(issues=[{'category': category, 'severity': severity, 'evidence_quote': parts[0], 'evidence_quote_en': english}],
               sentiment_score=sentiment, actionable_for_seller=actionable)
    return out


def key_of(r):
    return hashlib.sha256(json.dumps([r['raw_title'], r['raw_message'], r['review_score']], ensure_ascii=False).encode()).hexdigest()


def split(records):
    """返回 (规则输出 {行号: 输出}, 模型独立输入 [样本])。"""
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

# ---------------------------------------------------------------- 请求、计价与后处理


def system_prompt():
    """prompt_template.txt 的 SYSTEM 段，即冻结的 v8 Prompt。"""
    text = PROMPT_FILE.read_text().split('=== SYSTEM ===', 1)[1].split('=== END SYSTEM ===', 1)[0].strip() + '\n'
    if hashlib.sha256(text.encode()).hexdigest() != PROMPT_SHA256:
        raise SystemExit('prompt_template.txt 的 SYSTEM 段与冻结的 v8 Prompt 不一致')
    return text


def request_body(batch, arm):
    """评论内容由 JSON 序列化放进 user 消息，不做字符串拼接；Structured Outputs 严格模式。"""
    reviews = [dict(sample_id=s['sample_id'], review_id=s['review_id'], review_score=s['review_score'],
                    title=s['raw_title'], message=s['raw_message']) for s in batch]
    return dict(model=arm['model'], temperature=0, max_completion_tokens=MAX_OUT_PER_REVIEW * len(batch) + 50,
                messages=[{'role': 'system', 'content': system_prompt()},
                          {'role': 'user', 'content': json.dumps({'reviews': reviews}, ensure_ascii=False)}],
                response_format={'type': 'json_schema', 'json_schema': {'name': 'review_batch', 'strict': True, 'schema': batch_schema()}})


def reserve(body, arm):
    """保守预留：UTF-8 字节数 + 2048 作为输入 token 上界，加最大输出。"""
    upper_in = len(json.dumps(body, ensure_ascii=False).encode()) + 2048
    return (upper_in * arm['input_rate'] + body['max_completion_tokens'] * arm['output_rate']) / 1e6


def usage_cost(usage, arm):
    pin = usage.get('prompt_tokens', 0)
    cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0
    pout = usage.get('completion_tokens', 0)
    return ((pin - cached) * arm['input_rate'] + cached * arm['cached_input_rate'] + pout * arm['output_rate']) / 1e6


def env_key():
    key = os.environ.get('OPENAI_API_KEY')
    if key:
        return key
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith('OPENAI_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise RuntimeError('未找到 OPENAI_API_KEY（环境变量或项目根目录 .env）；不会打印Key')


def http_transport(key):
    def send(body):
        req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=json.dumps(body).encode(),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors='replace')[:2000]
            raise RuntimeError('HTTP %s: %s' % (e.code, detail)) from None
    return send


def parse(raw):
    choice = raw['choices'][0]
    if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
        raise ValueError('拒绝或截断：finish_reason=%s' % choice.get('finish_reason'))
    return strict_loads(choice['message']['content'])


def repair_quote(issue, sample):
    """引句不是原文子串、但标题或正文中只有唯一一处仅大小写不同的匹配时，替换为原文写法。返回 (issue, 修复记录或 None)。"""
    q = issue['evidence_quote']
    if not q or any(q in sample[k] for k in ('raw_title', 'raw_message')):
        return issue, None
    hits = [(k, i) for k in ('raw_title', 'raw_message') for i in range(len(sample[k]) - len(q) + 1)
            if sample[k][i:i + len(q)].lower() == q.lower()]
    if len(hits) != 1:
        return issue, None
    k, i = hits[0]
    fixed = sample[k][i:i + len(q)]
    return dict(issue, evidence_quote=fixed), dict(field=k, before=q, after=fixed)


def process(data, batch):
    """逐条后处理。返回 (通过的输出, 修复记录, 需兜底的 sample_id 及原因)；同批通过的评论直接保留。"""
    by = {s['sample_id']: s for s in batch}
    if schema_errors(data, batch_schema()):
        return {}, [], {sid: ['batch_schema'] for sid in by}
    outputs, repairs, fallback, seen = {}, [], {}, set()
    for row in data['results']:
        sid = row['sample_id']
        if sid not in by or sid in seen:
            continue
        seen.add(sid)
        result = dict(row['result'], issues=[])
        for n, issue in enumerate(row['result']['issues']):
            fixed, rep = repair_quote(issue, by[sid])
            result['issues'].append(fixed)
            if rep:
                repairs.append(dict(sample_id=sid, issue_index=n, **rep))
        errors = validate(result, by[sid])['errors']
        if errors:
            cross = [o for o in by if o != sid and any(i['evidence_quote'] in by[o]['raw_title'] + by[o]['raw_message']
                                                       for i in result['issues'] if i['evidence_quote'])]
            fallback[sid] = errors + (['quote_from_other_review:%s' % ','.join(cross)] if cross else [])
        else:
            outputs[sid] = result
    for sid in by:
        if sid not in seen:
            fallback[sid] = ['missing_sample_id']
    return outputs, repairs, fallback

# ---------------------------------------------------------------- 运行（预算、时限、续跑）


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


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
        self.started = json.loads(meta.read_text())['first_request_at'] if meta.exists() else None
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
                atomic_json(self.state / 'meta.json', dict(first_request_at=self.started))
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
        fee = reserve(body, arm)
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
            cost = usage_cost(usage, arm) if usage else (0.0 if raw else fee)
            with self.lock:
                self.held -= fee
                self.spent += cost
                self.append('events.jsonl', dict(kind=kind, model=arm['model'], sample_ids=sample_ids, attempt=attempt, error=err,
                                                 usage=usage, cost_usd=cost, seconds=round(self.clock() - t0, 2), at=self.clock()))
            if raw is not None:
                try:
                    return parse(raw)
                except ValueError as e:
                    self.append('events.jsonl', dict(kind=kind, sample_ids=sample_ids, error='parse: %s' % e, cost_usd=0.0, at=self.clock()))
                    return None
            if not any(code in (err or '') for code in ('HTTP 429', 'HTTP 5', 'Timeout', 'timed out', 'URLError')):
                return None
            time.sleep(2 * 4 ** attempt)
        return None

    def run_batch(self, batch):
        data = self.call('primary', request_body(batch, PRIMARY), PRIMARY, [s['sample_id'] for s in batch])
        if data is None:
            # 传输失败（含重试后仍 429）或已停止：留给续跑，不转 gpt-4.1-mini 单条兜底，避免限流时成本放大
            return
        out, repairs, fb = process(data, batch)
        by = {s['sample_id']: s for s in batch}
        for sid, o in out.items():
            self.save(by[sid], o, PRIMARY['model'], [r for r in repairs if r['sample_id'] == sid])
        for sid, reasons in fb.items():
            s = by[sid]
            d = self.call('fallback', request_body([s], FALLBACK), FALLBACK, [sid])
            if d is None and self.stopped:
                return
            o2, rep2, fb2 = process(d, [s]) if d is not None else ({}, [], {sid: ['request_failed']})
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
        budget_left = PROJECT_BUDGET_USD - DEV_COMMITMENT_USD
    run = Run(state, transport, budget_left)
    todo = [[s for s in b if s['key'] not in run.done and s['key'] not in run.dead] for b in batches(inputs)]
    todo = [b for b in todo if b]
    with ThreadPoolExecutor(workers) as pool:
        for n, f in enumerate([pool.submit(run.run_batch, b) for b in todo], 1):
            f.result()
            if n % 100 == 0 or n == len(todo):
                print('进度 %d/%d 批；完成 %d 条，死信 %d；已花费 $%.4f；%.1f 分钟' % (
                    n, len(todo), len(run.done), len(run.dead), run.spent, run.elapsed() / 60), flush=True)
    return run

# ---------------------------------------------------------------- 组装输出


def assemble(records, rules, inputs, state=STATE, output=None):
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
    atomic_json(output or (OUTPUT if state == STATE else state / 'extracted_issues.json'), out)
    events = read_jsonl(state / 'events.jsonl')
    meta = state / 'meta.json'
    first = json.loads(meta.read_text())['first_request_at'] if meta.exists() else None
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
    atomic_json(state / 'report.json', report)
    return out, report


def assemble_offline():
    """离线：由 full_run/ 结果缓存重建 extracted_issues.json（不读 Key、不发请求）；供 run_all.py 调用。"""
    system_prompt()
    if not (STATE / 'results.jsonl').exists():
        raise FileNotFoundError('没有 full_run/results.jsonl：先运行 extract_reviews.py --live')
    records = load_records()
    rules, inputs = split(records)
    _, report = assemble(records, rules, inputs)
    return {k: v for k, v in report.items() if not k.endswith('examples')}


def cost_summary(state=STATE):
    """离线：由 full_run/events.jsonl（逐请求 usage）重算 cost_report.md 中的全量费用分项。"""
    events = read_jsonl(state / 'events.jsonl')
    if not events:
        raise FileNotFoundError('没有 full_run/events.jsonl')
    rows = {}
    for e in events:
        k = 'transport_error' if e.get('error') and not e.get('usage') else e['kind']
        r = rows.setdefault(k, dict(requests=0, reviews=0, input_tokens=0, cached_tokens=0, output_tokens=0, cost_usd=0.0))
        u = e.get('usage') or {}
        r['requests'] += 1
        r['reviews'] += len(e.get('sample_ids', [])) if u else 0
        r['input_tokens'] += u.get('prompt_tokens', 0)
        r['cached_tokens'] += (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0) or 0
        r['output_tokens'] += u.get('completion_tokens', 0)
        r['cost_usd'] += e['cost_usd']
    for k, r in rows.items():
        r['cost_usd'] = round(r['cost_usd'], 4)
        if r['reviews']:
            r['per_review_usd'] = round(r['cost_usd'] / r['reviews'], 7)
            r['input_tokens_per_review'] = round(r['input_tokens'] / r['reviews'], 1)
            r['output_tokens_per_review'] = round(r['output_tokens'] / r['reviews'], 1)
    p = rows['primary']
    p['cost_without_cache_usd'] = round((p['input_tokens'] * PRIMARY['input_rate'] + p['output_tokens'] * PRIMARY['output_rate']) / 1e6, 4)
    total = round(sum(e['cost_usd'] for e in events), 4)
    first = json.loads((state / 'meta.json').read_text())['first_request_at']
    return dict(by_kind=rows, run_cost_usd=total, dev_commitment_usd=DEV_COMMITMENT_USD,
                total_with_dev_usd=round(total + DEV_COMMITMENT_USD, 4),
                per_low_score_review_usd=round(total / 22754, 7), per_model_input_usd=round(total / 13898, 7),
                wall_minutes=round((max(e['at'] for e in events) - first) / 60, 1),
                note='按公开单价与 usage 估算，不是账单核实；以 OpenAI 账单为准')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--assemble', action='store_true')
    ap.add_argument('--cost', action='store_true', help='离线：由 full_run/events.jsonl 重算成本分项')
    ap.add_argument('--rerun', action='store_true', help='确认在没有 full_run/ 缓存时重新付费跑全量')
    a = ap.parse_args()
    if a.cost:
        print(json.dumps(cost_summary(), ensure_ascii=False, indent=2))
        return
    if a.assemble:
        print(json.dumps(assemble_offline(), ensure_ascii=False, indent=2))
        return
    system_prompt()
    records = load_records()
    rules, inputs = split(records)
    done = {x['key'] for x in read_jsonl(STATE / 'results.jsonl')}
    todo = [s for s in inputs if s['key'] not in done]
    upper = sum(reserve(request_body(b, PRIMARY), PRIMARY) for b in batches(todo))
    print(json.dumps(dict(rows=len(records), rule_rows=len(rules), model_unique_inputs=len(inputs), remaining_inputs=len(todo),
                          requests=-(-len(todo) // BATCH), workers=WORKERS, dev_commitment_usd=DEV_COMMITMENT_USD,
                          budget_left_usd=round(PROJECT_BUDGET_USD - DEV_COMMITMENT_USD, 4), reserved_upper_usd=round(upper, 2),
                          estimate='约 $1.56 主流程 + $0.33 兜底（含 10% 重试），8 并发约 28 分钟；2026-11-01 实际 $1.42、28.0 分钟'),
                     ensure_ascii=False, indent=2))
    if not a.live:
        print('离线模式：未读取Key、未发送请求。加 --live 付费运行全量。')
        return
    if OUTPUT.exists() and not done and not a.rerun:
        # 已有提交的抽取结果但没有本地缓存（例如新克隆）：再跑一次会重复付费并超出 $3
        raise SystemExit('extracted_issues.json 已存在但没有 full_run/ 缓存：全量已于 2026-11-01 完成（$1.42）。'
                         '重新付费运行需显式加 --rerun，且预算只按开发阶段扣除，请先确认账单余额。')
    run = execute(inputs, http_transport(env_key()))
    print('停止原因：%s；已花费（本次全量）$%.4f；用时 %.1f 分钟' % (run.stopped or '全部完成', run.spent, run.elapsed() / 60))
    _, report = assemble(records, rules, inputs)
    print(json.dumps({k: v for k, v in report.items() if not k.endswith('examples')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
