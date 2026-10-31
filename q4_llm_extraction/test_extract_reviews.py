"""extract_reviews 离线测试：测试桩代替 API，不联网、不读 Key；用真实分流结果的前 40 个独立输入。

运行（项目根目录）：python q4_llm_extraction/test_extract_reviews.py
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_reviews as er  # noqa: E402

RECORDS = er.load_records()
RULES, INPUTS = er.split(RECORDS)


def stub(bad=(), fail_first=0, fallback_bad=False, http_error=None):
    calls, state = [], dict(n=0)

    def send(body):
        calls.append(body)
        state['n'] += 1
        if state['n'] <= fail_first:
            raise RuntimeError(http_error or 'HTTP 429: rate limit')
        reviews = json.loads(body['messages'][1]['content'])['reviews']
        single = len(reviews) == 1 and body['model'] == er.FALLBACK['model']
        results = [dict(sample_id=r['sample_id'], result=dict(
            review_id=r['review_id'],
            issues=[dict(category='other', severity='medium', evidence_quote='zzz inexistente', evidence_quote_en='x')]
            if r['sample_id'] in bad and (fallback_bad or not single) else [],
            sentiment_score=-0.5, actionable_for_seller=False, confidence=0.9)) for r in reviews]
        return dict(model=body['model'], usage=dict(prompt_tokens=1500, completion_tokens=500, prompt_tokens_details=dict(cached_tokens=0)),
                    choices=[dict(finish_reason='stop', message=dict(content=json.dumps(dict(results=results), ensure_ascii=False)))])
    send.calls = calls
    return send


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inputs = INPUTS[:40]
        er.time.sleep = lambda s: None  # 重试退避不真等

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_rules(self):
        s = lambda t, m='': dict(review_id='r', raw_title=t, raw_message=m)
        self.assertEqual(er.rule_extract(s('', '')), dict(review_id='r', issues=[], sentiment_score=0, actionable_for_seller=False, confidence=0))
        self.assertEqual(er.rule_extract(s('', 'Péssimo!'))['sentiment_score'], -1)
        self.assertEqual(er.rule_extract(s('Ruim', 'ruim'))['sentiment_score'], -.5)
        o = er.rule_extract(s('', 'Entrega atrasada!'))
        self.assertEqual((o['issues'][0]['category'], o['issues'][0]['evidence_quote'], o['sentiment_score']),
                         ('delivery_late', 'Entrega atrasada!', -.5))
        self.assertIsNone(er.rule_extract(s('', 'Não veio com defeito')))  # 不做关键词包含匹配
        self.assertIsNone(er.rule_extract(s('Ruim', 'Produto errado')))   # 标题正文语境不同交给模型

    def test_validate_and_repair(self):
        sample = dict(review_id='r', raw_title='', raw_message='Falta de respeito com o consumidor, falta de respeito é pouco')
        good = dict(review_id='r', issues=[dict(category='other', severity='low', evidence_quote='Falta de respeito com o consumidor',
                                                evidence_quote_en='x')], sentiment_score=-1, actionable_for_seller=False, confidence=.9)
        self.assertTrue(er.validate(good, sample)['valid'])
        bad = json.loads(json.dumps(good)); bad['issues'][0]['evidence_quote'] = 'falta de respeito com o consumidor'
        self.assertFalse(er.validate(bad, sample)['valid'])
        fixed, rep = er.repair_quote(bad['issues'][0], sample)
        self.assertEqual(fixed['evidence_quote'], 'Falta de respeito com o consumidor')
        self.assertIsNone(er.repair_quote(dict(evidence_quote='FALTA DE RESPEITO'), sample)[1])  # 多处匹配不猜
        self.assertIn('$.sentiment_score:超出范围', er.schema_errors(dict(good, sentiment_score=2)))
        with self.assertRaises(ValueError):
            er.strict_loads('{"a": 1, "a": 2}')

    def test_prompt_is_frozen_v8(self):
        body = er.request_body(INPUTS[:5], er.PRIMARY)
        self.assertEqual(body['messages'][0]['content'], er.system_prompt())
        self.assertEqual([r['sample_id'] for r in json.loads(body['messages'][1]['content'])['reviews']], [s['sample_id'] for s in INPUTS[:5]])
        orig = er.PROMPT_FILE
        tmp = self.tmp / 'p.txt'
        tmp.write_text(orig.read_text().replace('Return only', 'Return'))
        er.PROMPT_FILE = tmp
        try:
            with self.assertRaises(SystemExit):
                er.system_prompt()
        finally:
            er.PROMPT_FILE = orig

    def test_live_refuses_without_cache(self):
        old_state, old_out, old_argv = er.STATE, er.OUTPUT, sys.argv
        er.STATE, er.OUTPUT = self.tmp / 'full_run', self.tmp / 'extracted_issues.json'
        er.OUTPUT.write_text('[]')
        er.env_key = lambda: (_ for _ in ()).throw(AssertionError('不应读取 Key'))
        sys.argv = ['extract_reviews.py', '--live']
        try:
            with self.assertRaises(SystemExit) as cm:
                er.main()
            self.assertIn('--rerun', str(cm.exception))
        finally:
            er.STATE, er.OUTPUT, sys.argv = old_state, old_out, old_argv

    @unittest.skipUnless((er.STATE / 'events.jsonl').exists(), '没有 full_run/events.jsonl')
    def test_cost_summary_matches_run_report(self):
        c = er.cost_summary()
        report = json.loads((er.STATE / 'report.json').read_text())
        self.assertEqual(round(c['run_cost_usd'], 4), round(report['run_cost_usd'], 4))
        self.assertEqual(c['by_kind']['primary']['requests'] + c['by_kind']['fallback']['requests']
                         + c['by_kind']['transport_error']['requests'], report['requests'])
        self.assertEqual(c['by_kind']['primary']['reviews'], report['model_unique_inputs'])

    def test_split_counts(self):
        self.assertEqual(len(RECORDS), 22754)
        self.assertEqual(len(RULES), 8610)
        self.assertEqual(len({s['key'] for s in INPUTS}), len(INPUTS))
        self.assertEqual(len(INPUTS), 13898)

    def test_full_flow_resume_and_fallback(self):
        s = stub(bad={self.inputs[2]['sample_id']})
        run = er.execute(self.inputs, s, self.tmp, budget_left=1.0, workers=4)
        self.assertEqual(len(run.done), 40)
        self.assertEqual(len(s.calls), 8 + 1)
        res = {x['sample_id']: x for x in er.read_jsonl(self.tmp / 'results.jsonl')}
        self.assertEqual(res[self.inputs[2]['sample_id']]['model'], er.FALLBACK['model'])
        s2 = stub()
        er.execute(self.inputs, s2, self.tmp, budget_left=1.0)
        self.assertEqual(len(s2.calls), 0, '续跑不应重发已完成的输入')

    def test_deadletter_when_fallback_fails(self):
        sid = self.inputs[0]['sample_id']
        er.execute(self.inputs[:5], stub(bad={sid}, fallback_bad=True), self.tmp, budget_left=1.0)
        dead = er.read_jsonl(self.tmp / 'deadletter.jsonl')
        self.assertEqual([d['sample_id'] for d in dead], [sid])

    def test_transport_retry_then_success(self):
        s = stub(fail_first=2)
        run = er.execute(self.inputs[:5], s, self.tmp, budget_left=1.0)
        self.assertEqual(len(run.done), 5)
        self.assertEqual(len(s.calls), 3)

    def test_failed_request_left_for_resume_not_fallback(self):
        s = stub(fail_first=1, http_error='HTTP 400: bad request')
        run = er.execute(self.inputs[:5], s, self.tmp, budget_left=1.0)
        self.assertEqual((len(run.done), len(s.calls)), (0, 1))
        s2 = stub()
        run = er.execute(self.inputs[:5], s2, self.tmp, budget_left=1.0)
        self.assertEqual((len(run.done), [c['model'] for c in s2.calls]), (5, [er.PRIMARY['model']]))

    def test_retries_exhausted_no_fallback_storm(self):
        s = stub(fail_first=99)
        run = er.execute(self.inputs[:10], s, self.tmp, budget_left=1.0)
        self.assertEqual(len(run.done), 0)
        self.assertTrue(all(c['model'] == er.PRIMARY['model'] for c in s.calls))
        self.assertEqual(len(s.calls), 2 * (er.TRANSPORT_RETRIES + 1))

    def test_budget_stop(self):
        run = er.execute(self.inputs, stub(), self.tmp, budget_left=0.004, workers=1)
        self.assertEqual(run.stopped, 'budget')
        self.assertLess(len(run.done), 40)
        events = er.read_jsonl(self.tmp / 'events.jsonl')
        self.assertLessEqual(sum(e['cost_usd'] for e in events), 0.004)

    def test_deadline_stop(self):
        t = [0.0]
        orig = er.Run.__init__

        def init(self_, *a, **k):
            orig(self_, *a, clock=lambda: t[0], **k)
        er.Run.__init__ = init
        try:
            def slow(body):
                t[0] += er.STOP_SUBMIT_SECONDS / 3
                return stub()(body)
            run = er.execute(self.inputs, slow, self.tmp, budget_left=1.0, workers=1)
        finally:
            er.Run.__init__ = orig
        self.assertEqual(run.stopped, 'deadline')
        self.assertLess(len(run.done), 40)

    def test_assemble_fans_out_and_validates(self):
        er.execute(self.inputs, stub(), self.tmp, budget_left=1.0)
        keys = {s['key'] for s in self.inputs}
        sub = [r for r in RECORDS if er.key_of(r) in keys][:60] + [RECORDS[i] for i in list(RULES)[:20]]
        rules = {i: RULES[RECORDS.index(r)] for i, r in enumerate(sub) if RECORDS.index(r) in RULES}
        out, report = er.assemble(sub, rules, self.inputs, self.tmp)
        self.assertEqual(report['invalid'], 0)
        self.assertEqual(report['missing'], 0)
        by_rid = {r['review_id'] for r in sub}
        self.assertTrue({o['review_id'] for o in out} <= by_rid)
        self.assertEqual(report['by_source']['rules_v8'], len({(r['review_id'], r['raw_title'], r['raw_message']) for i, r in enumerate(sub) if i in rules}))


if __name__ == '__main__':
    unittest.main(verbosity=2)
