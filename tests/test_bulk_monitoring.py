"""Durability and task ownership checks for long App Store collection jobs."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors.app_store import AppStoreCollector
from collectors.base import CollectionResult, RawReview
from modules import monitoring
from modules.review_store import init_db, read_reviews, save_reviews
from modules.storage import database


class BulkMonitoringTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder=Path(temp.name)
        self.db=self.folder/'reviews.sqlite3'
        init_db(self.db)
        monitoring.configure_target(self.db,'111','cn',False,30,3)
        self.agent=patch('modules.agent_analysis.maybe_queue_analysis').start()
        self.alerts=patch('modules.alerts.publish_alerts').start()
        self.versions=patch('modules.monitoring.get_version_history').start()
        self.addCleanup(patch.stopall)

    def review(self,identity='first',**changes):
        values=dict(platform='App Store',external_id=identity,app_id='111',country='cn',
            date='2026-09-22T01:00:00+00:00',author='测试玩家',title='',content='更新后闪退',rating=1)
        values.update(changes)
        return RawReview(**values)

    def request(self,mode='history',pages=20000,**options):
        self.assertTrue(monitoring.request_collection(self.db,'111','cn',mode=mode,pages=pages,**options))
        job=monitoring.claim_job(self.db,'111','cn')
        self.assertIsNotNone(job)
        return job

    def perform(self,job,effect):
        collector=AppStoreCollector(log_dir=self.folder/'logs')
        with patch('modules.monitoring.collect_batch',side_effect=effect) as collect:
            value=monitoring.perform_job(self.db,job,self.folder/'logs',collector)
        self.assertTrue(collect.called)
        return value

    def page(self,on_page,rows,offset=50,**extra):
        dates=[r.date for r in rows if r.date]
        meta=dict(checkpoint={'offset':offset,'pages':offset//50},scanned_count=len(rows),
            sequence_verified=True,boundary_reached=False,
            oldest_date=min(dates) if dates else None,newest_date=max(dates) if dates else None)
        meta.update(extra)
        return on_page(rows,meta)

    def result(self,offset=50,done=False,retryable=False,**extra):
        metadata=dict(checkpoint={'offset':offset,'pages':offset//50},done=done,
            retryable=retryable,retry_after_seconds=30 if retryable else 0,
            sequence_verified=True,boundary_reached=done)
        metadata.update(extra)
        return CollectionResult(platform='App Store',requested=1,metadata=metadata,
            stop_reason='已读完公开可见分页' if done else '批次完成')

    def latest(self):
        return monitoring.recent_runs(self.db,'111','cn')[0]

    def test_large_request_snapshot_and_active_job_cannot_be_replaced(self):
        job=self.request()
        self.assertEqual(job['mode'],'history')
        self.assertEqual(job['pages'],20000)
        self.assertTrue(job['lease_token'])
        self.assertFalse(monitoring.request_collection(self.db,'111','cn',mode='latest',pages=1))
        self.assertIsNone(monitoring.claim_job(self.db,'111','cn'))
        for invalid in [0,20001]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                monitoring.request_collection(self.db,'111','us',mode='history',pages=invalid)

    def test_page_is_persisted_before_batch_returns_and_resume_reuses_same_run(self):
        job=self.request()
        def first(collector,app_id,country,**kwargs):
            self.assertEqual((app_id,country),('111','cn'))
            self.assertEqual(kwargs['max_pages'],20000)
            self.page(kwargs['on_page'],[self.review()])
            self.assertEqual(len(read_reviews(self.db)),1)
            checkpoint=json.loads(self.latest()['checkpoint_json'])
            self.assertEqual(checkpoint['offset'],50)
            return self.result()
        self.perform(job,first)
        first_run=self.latest()
        self.assertIsNone(first_run['finished_at'])
        self.agent.assert_not_called()
        resumed=monitoring.claim_job(self.db,'111','cn')
        self.assertEqual(resumed['run_id'],job['run_id'])
        self.assertEqual(resumed['checkpoint']['offset'],50)
        self.assertNotEqual(resumed['lease_token'],job['lease_token'])
        def last(collector,app_id,country,**kwargs):
            self.assertEqual(kwargs['checkpoint']['offset'],50)
            self.page(kwargs['on_page'],[self.review('second')],offset=100)
            return self.result(offset=100,done=True)
        self.perform(resumed,last)
        final=self.latest()
        self.assertEqual(final['id'],job['run_id'])
        self.assertEqual(final['inserted'],2)
        self.assertIsNotNone(final['finished_at'])
        self.assertEqual(len(read_reviews(self.db)),2)
        self.agent.assert_called_once_with(self.db,'111','cn')
        progress=monitoring.collection_progress(self.db,'111','cn')
        self.assertEqual(progress['id'],job['run_id'])
        self.assertEqual(progress['inserted'],2)
        self.assertIsInstance(progress['checkpoint_json'],str)

    def test_pause_keeps_committed_page_resume_only_starts_at_saved_checkpoint(self):
        job=self.request()
        def first(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            monitoring.control_collection(self.db,'111','cn','pause')
            self.assertTrue(kwargs['should_stop']())
            return self.result()
        self.perform(job,first)
        self.assertEqual(self.latest()['status'],'已暂停')
        self.assertIsNone(monitoring.claim_job(self.db,'111','cn'))
        self.assertEqual(len(read_reviews(self.db)),1)
        self.agent.assert_not_called()
        monitoring.control_collection(self.db,'111','cn','resume')
        resumed=monitoring.claim_job(self.db,'111','cn')
        self.assertEqual(resumed['run_id'],job['run_id'])
        self.assertEqual(resumed['checkpoint']['offset'],50)

    def test_cancel_waiting_job_never_requeues_and_preserves_reviews(self):
        job=self.request()
        def first(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            return self.result()
        self.perform(job,first)
        monitoring.control_collection(self.db,'111','cn','cancel')
        self.assertEqual(self.latest()['status'],'已取消')
        self.assertIsNone(monitoring.claim_job(self.db,'111','cn'))
        self.assertEqual(len(read_reviews(self.db)),1)
        self.agent.assert_not_called()

    def test_transport_failure_retains_last_committed_checkpoint_without_watermark_advance(self):
        marker='2026-09-20T01:00:00+00:00'
        with database(self.db) as connection:
            connection.execute('UPDATE monitor_targets SET last_review_at=?',(marker,))
        job=self.request(mode='latest')
        def failure(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            raise RuntimeError('模拟第二页连接中断')
        self.perform(job,failure)
        self.assertEqual(len(read_reviews(self.db)),1)
        self.assertEqual(json.loads(self.latest()['checkpoint_json'])['offset'],50)
        self.assertEqual(monitoring.target_state(self.db,'111','cn')['last_review_at'],marker)
        self.agent.assert_not_called()

    def test_invalid_save_rolls_back_page_and_does_not_advance_checkpoint(self):
        job=self.request()
        def invalid(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            self.page(kwargs['on_page'],[self.review('valid-next'),self.review('empty',content='')],offset=100)
            return self.result(offset=100,done=True)
        self.perform(job,invalid)
        self.assertEqual(read_reviews(self.db)['external_id'].tolist(),['first'])
        self.assertEqual(json.loads(self.latest()['checkpoint_json'])['offset'],50)
        self.agent.assert_not_called()

    def test_history_completion_cannot_advance_latest_monitor_watermark(self):
        marker='2026-09-20T01:00:00+00:00'
        with database(self.db) as connection:
            connection.execute('UPDATE monitor_targets SET last_review_at=?',(marker,))
        job=self.request()
        def complete(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            return self.result(done=True)
        self.perform(job,complete)
        self.assertEqual(monitoring.target_state(self.db,'111','cn')['last_review_at'],marker)

    def test_expired_worker_cannot_write_after_reclaim_changes_lease_token(self):
        original=self.request()
        with database(self.db) as connection:
            connection.execute("UPDATE monitor_targets SET lease_until='2000-01-01T00:00:00+00:00' WHERE app_id='111' AND country='cn'")
        reclaimed=monitoring.claim_job(self.db,'111','cn')
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed['run_id'],original['run_id'])
        self.assertNotEqual(reclaimed['lease_token'],original['lease_token'])
        def stale(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review('stale')])
            return self.result(done=True)
        try:
            self.perform(original,stale)
        except RuntimeError:
            pass  # A stale lease may be rejected before or inside the callback.
        self.assertEqual(len(read_reviews(self.db)),0)
        self.assertEqual(monitoring.target_state(self.db,'111','cn')['lease_token'],reclaimed['lease_token'])
        self.assertIsNone(self.latest()['finished_at'])
        self.agent.assert_not_called()

    def test_retryable_response_preserves_checkpoint_and_waits_until_retry_time(self):
        job=self.request()
        def limited(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            return self.result(retryable=True)
        self.perform(job,limited)
        self.assertIsNone(self.latest()['finished_at'])
        self.assertGreaterEqual(self.latest()['retry_count'],1)
        self.assertEqual(json.loads(self.latest()['checkpoint_json'])['offset'],50)
        self.assertIsNone(monitoring.claim_job(self.db,'111','cn'))
        with database(self.db) as connection:
            connection.execute("UPDATE collection_runs SET available_at='2000-01-01T00:00:00+00:00'")
        resumed=monitoring.claim_job(self.db,'111','cn')
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed['checkpoint']['offset'],50)
        self.agent.assert_not_called()

    def test_replayed_checkpoint_deduplicates_without_duplicate_records_or_agent_runs(self):
        job=self.request()
        def first(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            return self.result()
        self.perform(job,first)
        resumed=monitoring.claim_job(self.db,'111','cn')
        def replay(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review(),self.review('second')],offset=100)
            return self.result(offset=100,done=True)
        self.perform(resumed,replay)
        self.assertEqual(len(read_reviews(self.db)),2)
        self.assertEqual(self.latest()['inserted'],2)
        self.assertEqual(self.latest()['duplicates'],1)
        self.agent.assert_called_once()

    def test_successful_replay_does_not_reset_repeated_forward_page_failures(self):
        job=self.request()
        checkpoint={'next_page':21,'matched_count':1}
        def replay_then_failure(collector,app_id,country,**kwargs):
            # Resume rereads earlier pages to find newly inserted reviews. These
            # successful replays must not hide a consistently failing page 21.
            self.page(kwargs['on_page'],[self.review()],checkpoint=checkpoint,matched_count=1)
            return self.result(checkpoint=checkpoint,retryable=True)
        for attempt in range(1,7):
            self.perform(job,replay_then_failure)
            run=self.latest()
            self.assertEqual(run['retry_count'],attempt)
            self.assertEqual(json.loads(run['checkpoint_json'])['next_page'],21)
            if attempt<6:
                self.assertEqual(run['status'],'等待重试')
                with database(self.db) as connection:
                    connection.execute("UPDATE collection_runs SET available_at='2000-01-01T00:00:00+00:00'")
                job=monitoring.claim_job(self.db,'111','cn')
                self.assertIsNotNone(job)
        self.assertEqual(self.latest()['status'],'已暂停')
        self.assertIsNone(monitoring.claim_job(self.db,'111','cn'))
        self.assertEqual(len(read_reviews(self.db)),1)
        self.agent.assert_not_called()

    def test_page_with_wrong_binding_cannot_write_into_another_game(self):
        job=self.request()
        def wrong_game(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review('other',app_id='222')])
            return self.result(done=True)
        self.perform(job,wrong_game)
        self.assertEqual(len(read_reviews(self.db)),0)
        self.assertNotEqual(self.latest()['status'],'成功')
        self.agent.assert_not_called()

    def test_external_transaction_rollback_reverts_page_rows(self):
        with self.assertRaisesRegex(RuntimeError,'撤回整页'):
            with database(self.db) as connection:
                connection.execute('BEGIN IMMEDIATE')
                result=save_reviews(self.db,[self.review()],connection=connection)
                self.assertEqual(result.inserted,1)
                raise RuntimeError('撤回整页')
        self.assertEqual(len(read_reviews(self.db)),0)

    def test_sql_time_window_and_latest_limit_compare_timezones_correctly(self):
        save_reviews(self.db,[self.review('earlier',date='2026-09-22T09:00:00+08:00'),
                              self.review('later',date='2026-09-22T02:00:00Z'),
                              self.review('outside',date='2026-09-23T00:00:00Z')])
        rows=read_reviews(self.db,app_id='111',country='cn',start_at='2026-09-22T01:00:00Z',
                          end_at='2026-09-23T00:00:00Z',limit=1)
        self.assertEqual(rows['external_id'].tolist(),['later'])

    def test_coverage_calendar_can_aggregate_without_loading_review_bodies(self):
        from datetime import date
        from modules.collection_coverage import coverage_calendar
        save_reviews(self.db,[self.review('before',date='2026-09-21T15:59:59Z'),
                              self.review('after',date='2026-09-21T16:00:00Z')])
        rows=coverage_calendar(self.db,'111','cn',None,date(2026,9,21),date(2026,9,22))
        self.assertEqual(rows['本地评论数'].tolist(),[1,1])

    def test_completed_real_collection_refreshes_versions_outside_database_transaction(self):
        def refresh(app_id,country,cache_dir,**kwargs):
            self.assertEqual((app_id,country,cache_dir),('111','cn',self.folder/'app_versions'))
            self.assertEqual(kwargs,{'allow_fetch':True})
            # A separate writer can acquire the database immediately: network
            # fetching must not occur while a collection transaction is open.
            with database(self.db) as connection:
                connection.execute('PRAGMA busy_timeout=0')
                connection.execute('BEGIN IMMEDIATE')
            self.assertEqual(len(read_reviews(self.db)),1)
            self.assertIn(self.latest()['status'],('成功','部分成功'))
        self.versions.side_effect=refresh
        for sequence_verified,status in [(True,'成功'),(False,'部分成功')]:
            with self.subTest(status=status):
                self.assertTrue(monitoring.request_collection(self.db,'111','cn'))
                def complete(collector,app_id,country,**kwargs):
                    self.page(kwargs['on_page'],[self.review()])
                    return self.result(done=True,sequence_verified=sequence_verified)
                with patch('modules.monitoring.collect_batch',side_effect=complete):
                    monitoring.run_due(self.db,self.folder/'logs')
                self.assertEqual(self.latest()['status'],status)
        self.assertEqual(self.versions.call_count,2)

    def test_failed_collection_does_not_fetch_versions(self):
        self.assertTrue(monitoring.request_collection(self.db,'111','cn'))
        failed=self.result(done=True)
        failed.errors.append('公开接口请求失败')
        with patch('modules.monitoring.collect_batch',return_value=failed):
            monitoring.run_due(self.db,self.folder/'logs')
        self.assertEqual(self.latest()['status'],'失败')
        self.versions.assert_not_called()

    def test_completed_collection_skips_versions_during_global_rate_limit(self):
        self.assertTrue(monitoring.request_collection(self.db,'111','cn'))
        def complete(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            with database(self.db) as connection:
                connection.execute("INSERT OR REPLACE INTO runtime_state(key,value) VALUES('collection_not_before','2999-01-01T00:00:00+00:00')")
            return self.result(done=True)
        with patch('modules.monitoring.collect_batch',side_effect=complete):
            monitoring.run_due(self.db,self.folder/'logs')
        self.assertEqual(self.latest()['status'],'成功')
        self.versions.assert_not_called()

    def test_injected_collectors_and_stopped_workers_do_not_fetch_versions(self):
        for stopped in (False,True):
            with self.subTest(stopped=stopped):
                self.assertTrue(monitoring.request_collection(self.db,'111','cn'))
                stop=threading.Event()
                def complete(collector,app_id,country,**kwargs):
                    self.page(kwargs['on_page'],[self.review()])
                    if stopped: stop.set()
                    return self.result(done=True)
                with patch('modules.monitoring.collect_batch',side_effect=complete):
                    monitoring.run_due(self.db,self.folder/'logs',stop_event=stop,
                        factory=None if stopped else lambda: AppStoreCollector(log_dir=self.folder/'logs'))
                self.assertEqual(self.latest()['status'],'成功')
        self.versions.assert_not_called()

    def test_version_refresh_failure_preserves_committed_reviews_and_success(self):
        self.assertTrue(monitoring.request_collection(self.db,'111','cn'))
        self.versions.side_effect=RuntimeError('版本历史暂时不可用')
        def complete(collector,app_id,country,**kwargs):
            self.page(kwargs['on_page'],[self.review()])
            return self.result(done=True)
        with patch('modules.monitoring.collect_batch',side_effect=complete), self.assertLogs(level='ERROR') as logs:
            monitoring.run_due(self.db,self.folder/'logs')
        self.assertIn('optional version history',' '.join(logs.output))
        self.assertEqual(self.latest()['status'],'成功')
        self.assertEqual(read_reviews(self.db)['external_id'].tolist(),['first'])
        self.agent.assert_called_once()
