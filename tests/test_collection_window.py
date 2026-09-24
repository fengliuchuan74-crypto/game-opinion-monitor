import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from collectors.app_store import AppStoreCollector
from collectors.app_store_window import valid_next
from collectors.base import CollectionResult, RawReview
from modules.collection_coverage import coverage_calendar, run_details, run_request
from modules.monitoring import claim_job, configure_target, perform_job, recent_runs, request_collection, target_state
from modules.review_store import init_db, read_reviews, save_reviews
from modules.storage import database


def payload(rows, next_page=None):
    entries=[{'id':{'label':identity}, 'updated':{'label':stamp},
        'content':{'label':'更新后闪退'},'im:rating':{'label':'1'}} for identity,stamp in rows]
    links=[] if next_page is None else [{'attributes':{'rel':'next',
        'href':f'https://itunes.apple.com/cn/rss/customerreviews/page={next_page}/id=111/sortby=mostrecent/xml?l=en&urlDesc=x'}}]
    return {'feed':{'entry':entries,'link':links,'updated':None}}


def response(value,status=200):
    result=Mock(status_code=status,text=json.dumps(value))
    result.json.return_value=value
    return result


class CollectionWindowTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.folder=Path(folder.name); self.db=self.folder/'reviews.sqlite3'
        init_db(self.db)

    def collect(self,pages,**options):
        session=Mock()
        session.get.side_effect=[response(value) for value in pages]
        result=AppStoreCollector(self.folder,session=session).collect('111','cn',
            max_pages=options.pop('max_pages',10),delay_seconds=0,follow_feed_links=True,**options)
        return result,session

    def review(self,identity='a',stamp='2026-09-20T10:00:00Z'):
        return RawReview(platform='App Store',external_id=identity,date=stamp,
            author='test',title='',content='闪退',rating=1,app_id='111',country='cn')

    def test_fixed_dates_include_china_midnight_exclude_end_and_stop_before_start(self):
        first=payload([('outside-end','2026-09-20T16:00:00Z'),('last-second','2026-09-20T15:59:59Z')],2)
        second=payload([('start','2026-09-19T16:00:00Z'),('older','2026-09-19T15:59:59Z')],3)
        result,session=self.collect([first,second],start_at='2026-09-20T00:00:00+08:00',end_at='2026-09-21T00:00:00+08:00')
        self.assertEqual([r.external_id for r in result.reviews],['last-second','start'])
        self.assertEqual(result.metadata['scanned_count'],4)
        self.assertEqual(result.metadata['outside_range_count'],2)
        self.assertTrue(result.metadata['boundary_reached'])
        self.assertEqual(result.metadata['coverage_start'],'2026-09-19T16:00:00+00:00')
        self.assertEqual(session.get.call_count,2)

    def test_json_page_preferred_over_broken_advertised_xml(self):
        result,session=self.collect([payload([('a','2026-09-20T10:00:00Z')],2),payload([('b','2026-09-19T10:00:00Z')])])
        self.assertEqual(result.fetched_count,2)
        self.assertTrue(all('/json?l=en' in call.args[0] for call in session.get.call_args_list))
        self.assertTrue(result.metadata['sequence_verified'])

    def test_empty_first_url_recovers_without_changing_binding(self):
        result,session=self.collect([{'feed':{}},payload([('a','2026-09-20T10:00:00Z')])])
        self.assertEqual(result.fetched_count,1)
        self.assertEqual(result.metadata['request_attempts'],2)
        self.assertEqual(result.requested,1)
        self.assertTrue(all('/cn/' in c.args[0] and '/id=111/' in c.args[0] for c in session.get.call_args_list))

    def test_empty_middle_page_preserves_prior_rows_but_does_not_skip_gap(self):
        result,session=self.collect([payload([('a','2026-09-20T10:00:00Z')],2)]+[{'feed':{}}]*5,
            start_at='2026-09-01T00:00:00Z')
        self.assertEqual(result.fetched_count,1)
        self.assertEqual(result.requested,2)
        self.assertFalse(result.metadata['boundary_reached'])
        self.assertEqual(result.stop_reason,'空页未恢复')
        self.assertFalse(any('page=3' in c.args[0] for c in session.get.call_args_list))

    def test_budget_reports_partial_interval_without_claiming_start(self):
        result,_=self.collect([payload([('a','2026-09-20T10:00:00Z')],2)],max_pages=1,
            start_at='2026-09-01T00:00:00Z',end_at='2026-09-21T00:00:00Z')
        self.assertEqual(result.stop_reason,'页数上限')
        self.assertFalse(result.metadata['boundary_reached'])
        self.assertEqual(result.metadata['coverage_start'],'2026-09-20T10:00:00+00:00')

    def test_repeated_page_is_not_continuous_or_double_stored(self):
        result,_=self.collect([payload([('a','2026-09-20T10:00:00Z')],2),payload([('a','2026-09-20T10:00:00Z')],3)])
        self.assertEqual(result.fetched_count,1)
        self.assertFalse(result.metadata['sequence_verified'])
        self.assertIsNone(result.metadata['coverage_start'])
        self.assertEqual(result.stop_reason,'重复页面')

    def test_out_of_order_or_unknown_dates_never_certify_boundary(self):
        for rows in [[('a','2026-09-20T10:00:00Z'),('b','2026-09-21T10:00:00Z')],
                     [('a','2026-09-20T10:00:00Z'),('b',None)]]:
            with self.subTest(rows=rows):
                result,_=self.collect([payload(rows)],start_at='2026-09-21T00:00:00Z')
                self.assertFalse(result.metadata['sequence_verified'])
                self.assertFalse(result.metadata['boundary_reached'])
                self.assertIsNone(result.metadata['coverage_start'])

    def test_midnight_equal_boundary_requires_next_page(self):
        result,session=self.collect([payload([('a','2026-09-20T00:00:00Z')],2),payload([('b','2026-09-19T23:59:59Z')])],
            start_at='2026-09-20T00:00:00Z')
        self.assertEqual(session.get.call_count,2)
        self.assertEqual(result.fetched_count,1)
        self.assertTrue(result.metadata['boundary_reached'])

    def test_untrusted_next_pages_and_ports_rejected(self):
        root='https://itunes.apple.com/cn/rss/customerreviews/page=2/id=111/sortby=mostrecent/xml'
        self.assertTrue(valid_next(root,'111','cn',2))
        for url in [root.replace('/cn/','/us/'),root.replace('111','222'),root.replace('page=2','page=3'),
                    root.replace('.com/','.com:8000/'),root.replace('.com/','.com:bad/'),root.replace('https:','http:')]:
            self.assertFalse(valid_next(url,'111','cn',2))

    def test_rate_limit_does_not_trigger_compatibility_requests(self):
        session=Mock(); session.get.return_value=response({},429)
        result=AppStoreCollector(self.folder,session=session).collect('111',follow_feed_links=True)
        self.assertEqual(session.get.call_count,1)
        self.assertIn('限流',result.errors[0])

    def test_invalid_and_future_ranges_never_request(self):
        for start,end in [('2026-09-20','2026-09-21'),('2099-01-01T00:00:00Z','2099-02-01T00:00:00Z')]:
            result,session=self.collect([],start_at=start,end_at=end)
            self.assertTrue(result.errors); session.get.assert_not_called()

    def test_queued_request_is_snapshot_and_duplicate_click_cannot_replace_it(self):
        configure_target(self.db,'111','cn',False,30,3)
        self.assertTrue(request_collection(self.db,'111','cn',mode='date_range',pages=8,
            start_at='2026-09-01T00:00:00Z',end_at='2026-09-21T00:00:00Z'))
        self.assertFalse(request_collection(self.db,'111','cn',pages=1))
        configure_target(self.db,'111','cn',False,30,2)
        job=claim_job(self.db,'111','cn')
        self.assertEqual(job['pages'],8); self.assertEqual(job['mode'],'date_range')
        self.assertFalse(request_collection(self.db,'111','cn',pages=1))
        self.assertEqual(run_request(recent_runs(self.db,'111','cn')[0])['pages'],8)

    def complete(self,mode='latest',partial=False):
        configure_target(self.db,'111','cn',False,15,10)
        with database(self.db) as connection:
            connection.execute('UPDATE monitor_targets SET last_review_at=?',('2026-09-19T10:00:00+00:00',))
        kwargs={'mode':mode,'pages':10}
        if mode=='date_range': kwargs.update(start_at='2026-09-01T00:00:00Z',end_at='2026-09-21T00:00:00Z')
        request_collection(self.db,'111','cn',**kwargs)
        job=claim_job(self.db,'111','cn')
        if mode=='latest': self.assertEqual(job['start_at'],'2026-09-18T10:00:00+00:00')
        collector=Mock(); collector.collect.return_value=CollectionResult(platform='App Store',reviews=[self.review()],
            metadata={'sequence_verified':True,'boundary_reached':not partial},stop_reason='页数上限' if partial else '已越过起始日期')
        with patch('modules.alerts.publish_alerts'):
            status=perform_job(self.db,job,self.folder,collector)
        return status,target_state(self.db,'111','cn')

    def test_continuous_checkpoint_advances_only_after_reaching_overlap_start(self):
        status,state=self.complete()
        self.assertEqual(status,'成功')
        self.assertEqual(state['last_review_at'],'2026-09-20T10:00:00+00:00')

    def test_gap_keeps_checkpoint_without_slowing_healthy_polling(self):
        status,state=self.complete(partial=True)
        self.assertEqual(status,'部分成功')
        self.assertEqual(state['last_review_at'],'2026-09-19T10:00:00+00:00')
        self.assertEqual(state['failures'],0)
        self.assertIn('连续性缺口',recent_runs(self.db,'111','cn')[0]['detail'])

    def test_date_backfill_cannot_advance_continuous_checkpoint(self):
        status,state=self.complete(mode='date_range')
        self.assertEqual(status,'成功')
        self.assertEqual(state['last_review_at'],'2026-09-19T10:00:00+00:00')

    def test_schema_four_upgrade_preserves_data_and_backs_up_old_schema(self):
        save_reviews(self.db,[self.review()])
        configure_target(self.db,'111','cn',False,30,3)
        with database(self.db) as connection:
            for table,column in [('monitor_targets','request_json'),('monitor_targets','last_review_at'),
                                 ('collection_runs','request_json'),('collection_runs','result_json')]:
                connection.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
            connection.execute("UPDATE store_meta SET value='4' WHERE key='schema_version'")
        init_db(self.db)
        self.assertEqual(len(read_reviews(self.db)),1)
        self.assertEqual(target_state(self.db,'111','cn')['pages'],3)
        backup=next((self.folder/'backups').glob('*.sqlite3'))
        with database(backup) as connection:
            self.assertEqual(connection.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()[0],'4')

    def test_coverage_merges_intervals_and_does_not_infer_coverage_from_counts(self):
        intervals=[('2026-09-18T16:00:00Z','2026-09-19T04:00:00Z'),
                   ('2026-09-19T04:00:00Z','2026-09-19T16:00:00Z'),
                   ('2026-09-20T01:00:00Z','2026-09-20T05:00:00Z')]
        with database(self.db) as connection:
            for start,end in intervals:
                connection.execute('INSERT INTO collection_runs(app_id,country,started_at,finished_at,status,result_json) VALUES(?,?,?,?,?,?)',
                    ('111','cn',end,end,'成功',json.dumps({'sequence_verified':True,'coverage_start':start,'coverage_end':end})))
        raw=pd.DataFrame({'date':['2026-09-21T00:00:00Z']})
        calendar=coverage_calendar(self.db,'111','cn',raw,date(2026,9,19),date(2026,9,21))
        self.assertEqual(calendar['公开源检查范围'].tolist(),['公开源时段已遍历','仅部分时段已遍历','尚无遍历记录'])
        self.assertEqual(calendar['本地评论数'].tolist(),[0,0,1])

    def test_malformed_metadata_is_not_a_coverage_claim(self):
        for value in ['[]','null','bad']:
            self.assertEqual(run_details({'result_json':value}),{})
            self.assertEqual(run_request({'request_json':value}),{})
