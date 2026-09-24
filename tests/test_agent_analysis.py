import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from collectors.base import RawReview
from modules.agent_analysis import (
    _claim, _process_next, analysis_status, analysis_revision, cancel_analysis,
    configure_analysis, latest_report, recent_analysis_runs, request_analysis,
    maybe_queue_analysis, LOCAL_TZ,
    start_analysis_worker, stop_analysis_worker,
)
from modules.operations import review_override
from modules.review_store import init_db, read_reviews, save_reviews
from modules.storage import database


START = '2026-09-22T00:00:00+08:00'
END = '2026-09-23T00:00:00+08:00'


class FixtureRunner:
    def __init__(self):
        self.calls = []
        self.corrupt_quote = False
        self.corrupt_report = False
        self.fail_batch = None
        self.on_call = None

    def __call__(self, prompt, schema, directory, **kwargs):
        properties = schema['properties']
        if 'reviews' in properties:
            rows = json.loads((directory/'reviews.json').read_text(encoding='utf-8'))
            self.calls.append(('reviews',len(rows)))
            batch_no = sum(kind=='reviews' for kind,_ in self.calls)
            if self.fail_batch and batch_no>=self.fail_batch:
                raise RuntimeError('连接暂时失败')
            output = {'reviews':[dict(review_id=r['review_id'],content_hash=r['content_hash'],
                sentiment_label='差评',issue_category='Bug/闪退问题',issue_categories=['Bug/闪退问题'],
                needs_review=False,reason='玩家描述启动失败',demand='希望恢复启动',target='游戏稳定性',
                evidence_quote='这不是原文' if self.corrupt_quote else r['content']) for r in rows]}
            # All data is passed through the prompt: the runtime needs no file tools.
            assert rows[0]['content'] in prompt
        elif 'requests' in properties:
            context = json.loads((directory/'report_context.json').read_text(encoding='utf-8'))
            self.calls.append(('requests',len(context['evidence'])))
            category = context['evidence'][0]['analysis']['issue_category']
            output = {'requests':[dict(category=category,review_ids=[context['evidence'][0]['review_id']],
                                      question='核查同类问题是否还有其他反馈')]}
        else:
            context = json.loads((directory/'report_context.json').read_text(encoding='utf-8'))
            self.calls.append(('report',len(context['evidence'])))
            evidence = context['evidence'][0]
            output = dict(summary='玩家反馈需要核实启动稳定性。',findings=[dict(title='启动反馈',
                category=evidence['analysis']['issue_category'],observation='评论描述启动失败',
                hypothesis='原因需要研发验证',actions=['核对机型并复现'],owner='QA',validation='复现用例通过',
                evidence_ids=[999999] if self.corrupt_report else [evidence['review_id']])],
                positive=[],limitations=['仅分析当前已采集评论'])
        if self.on_call:
            self.on_call(properties)
        return dict(output=output,model='test-model',usage={'input_tokens':10,'output_tokens':5})


class AgentAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.db = self.folder/'reviews.sqlite3'
        init_db(self.db)
        self.runner = FixtureRunner()

    def seed(self, count=1, **changes):
        values = dict(platform='App Store',app_id='111',country='cn',date='2026-09-22T01:00:00+00:00',
                      author='测试者',title='启动问题',content='更新后闪退，无法进入游戏',rating=1)
        values.update(changes)
        save_reviews(self.db,[RawReview(external_id=str(i),**values) for i in range(count)])

    def request(self, budget=200, **kwargs):
        values = dict(start_at=START,end_at=END,max_reviews=budget,model='test-model')
        values.update(kwargs)
        return request_analysis(self.db,'111','cn',**values)

    def process(self):
        self.assertTrue(_process_next(self.db,self.folder/'agent',runner=self.runner))

    def status(self):
        return analysis_status(self.db,'111','cn',START,END)

    def test_complete_run_persists_validated_results_stats_evidence_and_trace(self):
        self.seed(3)
        revision = analysis_revision(self.db)
        run_id, created = self.request()
        self.assertTrue(created)
        self.process()
        status = self.status()
        self.assertEqual((status['total'],status['analyzed'],status['pending']),(3,3,0))
        self.assertEqual(status['latest_run']['status'],'completed')
        self.assertGreater(analysis_revision(self.db),revision)
        report = latest_report(self.db,'111','cn',START,END)
        self.assertFalse(report['stale'])
        self.assertEqual(report['run_id'],run_id)
        self.assertEqual(report['stats']['categories'],{'Bug/闪退问题':3})
        self.assertEqual(report['investigations'][0]['current']['matching'],3)
        self.assertEqual(report['evidence'][0]['content'],'更新后闪退，无法进入游戏')
        self.assertEqual(self.request(),(run_id,False))

    def test_budget_is_truthful_and_subsequent_run_reuses_cache(self):
        self.seed(15)
        self.request(budget=10)
        self.process()
        self.assertEqual(self.status()['pending'],5)
        report = latest_report(self.db,'111','cn',START,END)
        self.assertEqual(report['coverage']['pending'],5)
        self.assertIn('仍有 5 条待分析',report['limitations'][0])
        self.request(budget=10)
        self.process()
        self.assertEqual([size for kind,size in self.runner.calls if kind=='reviews'],[10,5])
        self.assertEqual(self.status()['pending'],0)

    def test_all_pending_uses_multiple_small_batches_and_reuses_cache(self):
        self.seed(75)
        self.request(budget=10)
        self.process()
        configure_analysis(self.db,'111','cn',auto_enabled=True,max_reviews=120,model='test-model')
        self.runner = FixtureRunner()
        self.request(budget=None)
        queued = self.status()['latest_run']
        self.assertTrue(queued['all_reviews'])
        self.assertEqual(queued['selected'],65)
        self.assertEqual(queued['max_reviews'],0)
        self.assertEqual(self.status()['settings']['max_reviews'],120)
        self.assertTrue(self.status()['settings']['auto_enabled'])
        self.process()
        self.assertEqual([size for kind,size in self.runner.calls if kind=='reviews'],[15,15,15,15,5])
        self.assertEqual(self.status()['analyzed'],75)
        self.assertEqual(self.status()['pending'],0)
        report = latest_report(self.db,'111','cn',START,END)
        self.assertTrue(report['coverage']['all_reviews'])
        self.assertEqual(report['coverage']['snapshot_total'],75)

    def test_all_pending_over_one_thousand_is_not_truncated_or_saved_as_auto_budget(self):
        self.seed(1105)
        run_id,_ = self.request(budget=None)
        self.assertEqual(self.status()['latest_run']['selected'],1105)
        with database(self.db) as connection:
            row = connection.execute('SELECT snapshot_json FROM agent_runs WHERE id=?',(run_id,)).fetchone()
        self.assertEqual(len(json.loads(row[0])),1105)
        self.assertEqual(self.status()['settings']['max_reviews'],200)
        self.assertFalse(self.status()['settings']['auto_enabled'])

    def test_all_pending_is_not_limited_by_previous_failed_twenty_review_run(self):
        self.seed(65)
        self.runner.corrupt_report = True
        self.request(budget=20)
        self.process()
        self.assertEqual(self.status()['analyzed'],20)
        self.assertEqual(self.status()['latest_run']['status'],'failed')
        self.runner = FixtureRunner()
        self.request(budget=None)
        self.assertEqual(self.status()['latest_run']['selected'],45)
        self.process()
        self.assertEqual([size for kind,size in self.runner.calls if kind=='reviews'],[15,15,15])
        self.assertEqual(self.status()['pending'],0)

    def test_all_pending_failure_retry_reuses_completed_batches(self):
        self.seed(65)
        self.runner.fail_batch = 2
        self.request(budget=None)
        self.process()
        self.assertEqual(self.status()['analyzed'],15)
        self.runner = FixtureRunner()
        self.request(budget=None)
        self.assertEqual(self.status()['latest_run']['selected'],50)
        self.process()
        self.assertEqual([size for kind,size in self.runner.calls if kind=='reviews'],[15,15,15,5])
        self.assertEqual(self.status()['pending'],0)

    def test_all_pending_snapshot_is_frozen_and_later_arrivals_remain_visible(self):
        self.seed(65)
        self.request(budget=None)
        self.seed(66)
        self.process()
        self.assertEqual([size for kind,size in self.runner.calls if kind=='reviews'],[15,15,15,15,5])
        self.assertEqual(self.status()['analyzed'],65)
        self.assertEqual(self.status()['pending'],1)
        report = latest_report(self.db,'111','cn',START,END)
        self.assertEqual(report['coverage']['snapshot_total'],65)
        self.assertEqual(report['coverage']['total'],66)
        self.assertEqual(report['coverage']['pending'],1)

    def test_active_queue_deduplicates_even_with_moving_cutoff(self):
        self.seed()
        first = self.request()
        second = self.request(end_at='2026-09-23T00:01:00+08:00')
        self.assertEqual(second,(first[0],False))
        self.assertEqual(len(recent_analysis_runs(self.db,'111','cn')),1)

    def test_invalid_quote_never_enters_cache(self):
        self.seed()
        self.runner.corrupt_quote = True
        self.request()
        self.process()
        status = self.status()
        self.assertEqual(status['analyzed'],0)
        self.assertEqual(status['latest_run']['status'],'failed')
        self.assertIn('原文匹配',status['latest_run']['error'])

    def test_invalid_report_id_rejected_but_completed_classification_reused(self):
        self.seed()
        self.runner.corrupt_report = True
        self.request()
        self.process()
        self.assertEqual(self.status()['latest_run']['status'],'failed')
        self.assertEqual(self.status()['latest_run']['usage']['input_tokens'],40)
        self.assertIsNone(latest_report(self.db,'111','cn',START,END))
        self.assertEqual(self.status()['analyzed'],1)
        self.runner.corrupt_report = False
        self.request()
        self.process()
        self.assertEqual(sum(kind=='reviews' for kind,_ in self.runner.calls),1)
        self.assertEqual(self.status()['latest_run']['status'],'completed')

    def test_invalid_report_gets_one_bounded_correction_and_records_both_calls(self):
        self.seed()
        self.runner.corrupt_report = True
        def repair(properties):
            if 'summary' in properties:
                self.runner.corrupt_report = False
        self.runner.on_call = repair
        self.request()
        self.process()
        self.assertEqual(self.status()['latest_run']['status'],'completed')
        self.assertEqual(sum(kind=='report' for kind,_ in self.runner.calls),2)
        self.assertEqual(self.status()['latest_run']['usage']['input_tokens'],40)

    def test_wrong_quote_repair_must_pass_original_text_validation(self):
        self.seed()
        self.runner.corrupt_quote = True
        def repair(properties):
            if 'reviews' in properties:
                self.runner.corrupt_quote = False
        self.runner.on_call = repair
        self.request()
        self.process()
        self.assertEqual(self.status()['latest_run']['status'],'completed')
        self.assertEqual(sum(kind=='reviews' for kind,_ in self.runner.calls),2)

    def test_only_bad_quote_is_repaired_and_other_twenty_nine_are_already_saved(self):
        self.seed(30)
        base = self.runner
        bad_id = None
        correction_seen = False
        observed_sizes = []
        def targeted(prompt,schema,directory,**kwargs):
            nonlocal bad_id, correction_seen
            response = base(prompt,schema,directory,**kwargs)
            if 'reviews' in schema['properties']:
                records = response['output']['reviews']
                observed_sizes.append(len(records))
                if bad_id is None:
                    response['model'] = 'original-response-model'
                    bad_id = records[0]['review_id']
                    records[0]['evidence_quote'] = '这是对启动问题的概括，原文未这么写'
                elif directory.name == 'correction':
                    correction_seen = True
                    response['model'] = 'correction-response-model'
                    self.assertEqual([r['review_id'] for r in records],[bad_id])
                    self.assertIn(f'review_id={bad_id}',prompt)
                    self.assertIn('不要总结',prompt)
                    self.assertEqual(self.status()['analyzed'],14)
            return response
        self.runner = targeted
        self.request(budget=None)
        self.process()
        self.assertEqual(observed_sizes,[15,1,15])
        self.assertEqual(self.status()['analyzed'],30)
        self.assertEqual(self.status()['latest_run']['status'],'completed')
        self.assertEqual(self.status()['latest_run']['usage']['input_tokens'],50)
        with database(self.db) as connection:
            provenance = dict(connection.execute('SELECT actual_model,COUNT(*) FROM agent_review_results GROUP BY actual_model'))
        self.assertEqual(provenance,{'original-response-model':14,'correction-response-model':1,'test-model':15})

    def test_failed_targeted_repair_keeps_valid_subset_and_retry_only_analyzes_one(self):
        self.seed(30)
        base = self.runner
        seen = []
        def still_bad(prompt,schema,directory,**kwargs):
            response = base(prompt,schema,directory,**kwargs)
            if 'reviews' in schema['properties']:
                seen.append(len(response['output']['reviews']))
                response['output']['reviews'][0]['evidence_quote'] = '引用被改写了'
            return response
        self.runner = still_bad
        self.request(budget=None)
        self.process()
        self.assertEqual(seen,[15,1])
        self.assertEqual(self.status()['latest_run']['status'],'failed')
        self.assertEqual(self.status()['analyzed'],14)
        self.assertEqual(self.status()['latest_run']['usage']['input_tokens'],20)
        self.runner = FixtureRunner()
        self.request(budget=None)
        self.assertEqual(self.status()['latest_run']['selected'],16)
        self.process()
        self.assertEqual([size for kind,size in self.runner.calls if kind=='reviews'],[15,1])
        self.assertEqual(self.status()['analyzed'],30)

    def test_cancel_during_targeted_repair_preserves_completed_subset(self):
        self.seed(30)
        base = self.runner
        run_id,_ = self.request(budget=None)
        def cancel_repair(prompt,schema,directory,**kwargs):
            response = base(prompt,schema,directory,**kwargs)
            if 'reviews' in schema['properties']:
                if len(response['output']['reviews'])==15:
                    response['output']['reviews'][0]['evidence_quote'] = '引用被改写了'
                else:
                    cancel_analysis(self.db,run_id)
            return response
        self.runner = cancel_repair
        self.process()
        self.assertEqual(self.status()['latest_run']['status'],'cancelled')
        self.assertEqual(self.status()['analyzed'],14)

    def test_failed_second_batch_preserves_first_batch(self):
        self.seed(35)
        self.runner.fail_batch = 2
        self.request()
        self.process()
        self.assertEqual(self.status()['analyzed'],15)
        self.assertEqual(self.status()['latest_run']['status'],'failed')
        retry = FixtureRunner()
        self.runner = retry
        self.request()
        self.process()
        self.assertEqual([size for kind,size in retry.calls if kind=='reviews'],[15,5])
        self.assertEqual(self.status()['analyzed'],35)

    def test_queued_and_running_cancellation(self):
        self.seed()
        first,_ = self.request()
        cancel_analysis(self.db,first)
        self.assertFalse(_process_next(self.db,self.folder/'agent',runner=self.runner))
        self.assertEqual(self.status()['latest_run']['status'],'cancelled')
        second,_ = self.request()
        self.runner.on_call = lambda _:cancel_analysis(self.db,second)
        self.process()
        self.assertEqual(self.status()['latest_run']['status'],'cancelled')
        self.assertEqual(self.status()['analyzed'],0)

    def test_lease_recovery_and_live_lease_exclusion(self):
        self.seed()
        run_id,_ = self.request()
        claimed = _claim(self.db)
        self.assertEqual(claimed['id'],run_id)
        self.assertIsNone(_claim(self.db))
        with database(self.db) as connection:
            connection.execute("UPDATE agent_runs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",(run_id,))
        self.process()
        self.assertEqual(self.status()['latest_run']['status'],'completed')
        self.assertEqual(self.status()['latest_run']['attempts'],2)

    def test_original_changed_during_call_never_publishes_stale_result(self):
        self.seed()
        self.request()
        def mutate(properties):
            if 'reviews' in properties:
                self.seed(content='现在已经修复了',rating=5,date='2026-09-22T02:00:00+00:00')
        self.runner.on_call = mutate
        self.process()
        self.assertEqual(self.status()['analyzed'],0)
        self.assertEqual(self.status()['latest_run']['status'],'failed')

    def test_investigation_scope_never_includes_other_game_country_or_window(self):
        self.seed(2)
        self.seed(4,app_id='222')
        self.seed(3,country='us')
        # One earlier review in the same game/region is in the equal prior window.
        save_reviews(self.db,[RawReview(platform='App Store',app_id='111',country='cn',external_id='older',author='测试者',title='',
            date='2026-09-21T01:00:00+00:00',content='更新后闪退',rating=1)])
        self.request()
        self.process()
        report = latest_report(self.db,'111','cn',START,END)
        result = report['investigations'][0]
        self.assertEqual(result['current']['total'],2)
        self.assertEqual(result['previous']['total'],1)
        self.assertEqual(result['previous']['classified'],0)
        self.assertEqual(result['previous']['matching'],0)
        self.assertTrue(all(r['app_id']=='111' and r['country']=='cn' for r in report['evidence']))

    def test_report_stale_after_human_review_and_rebuilt_with_human_result(self):
        self.seed()
        self.request()
        self.process()
        row = read_reviews(self.db).iloc[0]
        review_override(self.db,int(row.review_id),row.content_hash,'好评','正向口碑/泛好评','QA','这里是在肯定修复')
        self.assertTrue(latest_report(self.db,'111','cn',START,END)['stale'])
        self.request()
        self.process()
        report = latest_report(self.db,'111','cn',START,END)
        self.assertFalse(report['stale'])
        self.assertEqual(report['stats']['sentiments'],{'好评':1})
        self.assertEqual(report['stats']['manual_reviewed'],1)
        analysis = report['evidence'][0]['analysis']
        self.assertNotEqual(analysis['demand'],'希望恢复启动')
        self.assertEqual(analysis['analysis_source'],'人工复核')

    def test_model_change_invalidates_status_and_report(self):
        self.seed()
        self.request()
        self.process()
        configure_analysis(self.db,'111','cn',model='different-model')
        self.assertEqual(self.status()['analyzed'],0)
        self.assertIsNone(latest_report(self.db,'111','cn',START,END))

    def test_reasoning_effort_is_saved_and_separates_cache(self):
        self.seed()
        configure_analysis(self.db,'111','cn',model='test-model',reasoning_effort='high')
        self.request(reasoning_effort='high')
        self.process()
        self.assertEqual(self.status()['settings']['reasoning_effort'],'high')
        # Switching effort must not silently display the high-effort result.
        configure_analysis(self.db,'111','cn',model='test-model',reasoning_effort='low')
        self.assertEqual(self.status()['analyzed'],0)
        self.assertEqual(self.status()['pending'],1)
        self.request(reasoning_effort='low')
        self.process()
        with database(self.db) as connection:
            efforts = {row[0] for row in connection.execute('SELECT DISTINCT reasoning_effort FROM agent_review_results')}
        self.assertEqual(efforts,{'high','low'})

    def test_timeout_splits_a_large_batch_instead_of_repeating_same_prompt(self):
        self.seed(8)
        self.request(budget=None)
        base = FixtureRunner()
        calls = []
        def runner(prompt, schema, directory, **kwargs):
            if 'reviews' in schema['properties']:
                rows = json.loads((Path(directory)/'reviews.json').read_text(encoding='utf8'))
                calls.append(len(rows))
                if len(calls) == 1:
                    raise RuntimeError('本批 Agent 分析超时；已完成结果保留，可以重试继续。')
            return base(prompt, schema, directory, **kwargs)
        self.runner = runner
        self.process()
        self.assertEqual(calls,[8,4,4])
        self.assertEqual(self.status()['pending'],0)
        self.assertEqual(self.status()['latest_run']['status'],'completed')

    def test_same_day_later_cutoff_reuses_report_but_new_data_marks_stale(self):
        self.seed()
        self.request(end_at='2026-09-22T18:00:00+08:00')
        self.process()
        later = '2026-09-22T19:00:00+08:00'
        self.assertFalse(latest_report(self.db,'111','cn',START,later)['stale'])
        save_reviews(self.db,[RawReview(platform='App Store',app_id='111',country='cn',external_id='new',author='测试者',title='',
            date='2026-09-22T18:30:00+08:00',content='更新后无法进入游戏',rating=1)])
        self.assertTrue(latest_report(self.db,'111','cn',START,later)['stale'])

    def test_old_report_stales_when_new_classifications_saved_then_report_fails(self):
        self.seed(25)
        self.request(budget=10)
        self.process()
        first = latest_report(self.db,'111','cn',START,END)
        self.assertEqual(first['coverage']['analyzed'],10)
        self.runner.corrupt_report = True
        self.request(budget=10)
        self.process()
        self.assertEqual(self.status()['analyzed'],20)
        old = latest_report(self.db,'111','cn',START,END)
        self.assertTrue(old['stale'])
        self.assertIn('新的单条分析',old['stale_reason'])
        self.runner.corrupt_report = False
        self.request(budget=10)
        self.assertEqual(self.status()['latest_run']['selected'],0)
        self.process()
        self.assertEqual(self.status()['analyzed'],20)

    def test_auto_can_retry_summary_only_but_respects_cancellation_and_unchanged_report(self):
        self.seed()
        self.runner.corrupt_report = True
        self.request()
        self.process()
        configure_analysis(self.db,'111','cn',auto_enabled=True,days=1,model='test-model')
        with patch('modules.agent_analysis.datetime') as clock:
            clock.now.return_value = datetime(2026,9,22,19,tzinfo=LOCAL_TZ)
            queued = maybe_queue_analysis(self.db,'111','cn')
        self.assertTrue(queued[1])
        self.assertEqual(self.status()['latest_run']['selected'],0)
        cancel_analysis(self.db,queued[0])
        with patch('modules.agent_analysis.datetime') as clock:
            clock.now.return_value = datetime(2026,9,22,19,tzinfo=LOCAL_TZ)
            self.assertIsNone(maybe_queue_analysis(self.db,'111','cn'))

        self.runner.corrupt_report = False
        self.request(end_at='2026-09-22T19:00:00+08:00')
        self.process()
        with patch('modules.agent_analysis.datetime') as clock:
            clock.now.return_value = datetime(2026,9,22,20,tzinfo=LOCAL_TZ)
            self.assertIsNone(maybe_queue_analysis(self.db,'111','cn'))

    def test_auto_empty_scope_does_not_raise_or_queue(self):
        self.seed(date='2026-09-20T12:00:00+08:00')
        configure_analysis(self.db,'111','cn',auto_enabled=True,days=1,model='test-model')
        with patch('modules.agent_analysis.datetime') as clock:
            clock.now.return_value = datetime(2026,9,22,19,tzinfo=LOCAL_TZ)
            self.assertIsNone(maybe_queue_analysis(self.db,'111','cn'))

    def test_cancelled_partial_run_does_not_restart_on_duplicate_collection(self):
        self.seed(15)
        run_id,_ = self.request(budget=10)
        cancel_analysis(self.db,run_id)
        configure_analysis(self.db,'111','cn',auto_enabled=True,days=1,model='test-model')
        with patch('modules.agent_analysis.datetime') as clock:
            clock.now.return_value = datetime(2026,9,22,19,tzinfo=LOCAL_TZ)
            self.assertIsNone(maybe_queue_analysis(self.db,'111','cn'))
        self.seed(15,content='更新后还是闪退，反馈内容发生变化')
        with patch('modules.agent_analysis.datetime') as clock:
            clock.now.return_value = datetime(2026,9,22,19,tzinfo=LOCAL_TZ)
            self.assertTrue(maybe_queue_analysis(self.db,'111','cn')[1])

    def test_stop_worker_waits_until_runner_observes_cancellation(self):
        self.seed()
        self.request()
        entered = threading.Event()
        def blocking_runner(*args, **kwargs):
            entered.set()
            tick = threading.Event()
            while not kwargs['cancelled']():
                tick.wait(0.02)
            raise RuntimeError('runner cancelled')
        with patch('modules.codex_runner.run_codex',blocking_runner):
            start_analysis_worker(self.db,self.folder/'agent')
            self.addCleanup(stop_analysis_worker,self.db)
            self.assertTrue(entered.wait(3))
            self.assertTrue(stop_analysis_worker(self.db,timeout=3))
        self.assertEqual(self.status()['latest_run']['status'],'cancelled')
        self.assertTrue(stop_analysis_worker(self.db,timeout=0))


if __name__=='__main__':
    unittest.main()
