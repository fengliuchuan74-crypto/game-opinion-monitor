"""Bulk collection controls, date budgets and progress semantics; no network."""
from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from collection_ui import (collection_busy, collection_totals, log_card,
                           progress_card, scan_pages)
from collectors.base import RawReview
from modules.monitoring import configure_target, target_state
from modules.review_store import init_db, save_reviews
from modules.storage import database
from workbench_ui import dashboard_update_signature


class BulkCollectionUiTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path(__file__).resolve().parents[1]/'.test-tmp'/('bulk-ui-'+uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.folder)
        self.db=self.folder/'reviews.sqlite3'
        init_db(self.db)

    def form(self):
        source=f'''from pathlib import Path
from collection_ui import collection_request_form
collection_request_form(Path({str(self.db)!r}),{{'app_id':'111','country':'cn','app_name':'测试游戏'}},{{}})
'''
        entry=self.folder/'collection_form.py'
        entry.write_text(source,encoding='utf-8')
        return AppTest.from_file(str(entry),default_timeout=15).run()

    def test_scan_budget_is_bounded_and_rounds_up(self):
        self.assertEqual(scan_pages(5000),100)
        self.assertEqual(scan_pages(10000),200)
        self.assertEqual(scan_pages(51),2)
        self.assertEqual(scan_pages(1_000_000),20000)
        for count in (0,49,1_000_001):
            with self.assertRaises(ValueError): scan_pages(count)

    def test_paused_and_waiting_jobs_are_not_replaced_by_new_requests(self):
        for status in ('采集中','等待续采','等待重试','已暂停','排队中'):
            self.assertTrue(collection_busy({'active_status':status}))
        self.assertTrue(collection_busy({'requested':1}))
        self.assertFalse(collection_busy({'active_status':'成功','lease_until':'2020-01-01T00:00:00Z'}))

    def test_totals_are_entire_selected_game_region_and_dates_normalize(self):
        rows=[('a','cn','2025-01-01T00:00:00+08:00'),
              ('b','cn','2026-09-01T00:00:00Z'),('c','us','2024-01-01T00:00:00Z')]
        save_reviews(self.db,[RawReview(platform='App Store',external_id=identity,
            app_id='111',country=country,date=stamp,author='测试',title='',content='评论',rating=4)
            for identity,country,stamp in rows])
        result=collection_totals(self.db,'111','cn')
        self.assertEqual(result['total'],2)
        self.assertEqual(result['oldest_date'],'2024-12-31 16:00:00')
        self.assertEqual(result['newest_date'],'2026-09-01 00:00:00')

    def test_progress_separates_scanned_and_inserted_without_completeness_claim(self):
        run={'status':'采集中','fetched':80,'inserted':50,
            'request_json':json.dumps({'mode':'history','pages':200}),
            'result_json':json.dumps({'scanned_count':300,'oldest_date':'2026-08-01T00:00:00Z','newest_date':'2026-09-01T00:00:00Z'})}
        markup=progress_card(run)
        self.assertIn('历史回溯',markup)
        self.assertIn('本次已读取 <b>300</b>',markup)
        self.assertIn('新增入库 <b>50</b>',markup)
        self.assertIn('2026-08-01 08:00',markup)
        self.assertNotIn('100%',markup)
        self.assertIn('最多扫描约 10,000 条',markup)
        pending=progress_card(run,{'active_control':'pause'})
        self.assertIn('正在暂停',pending)
        self.assertIn('等待当前页保存完成',pending)
        queued=progress_card(run,{'requested':1})
        self.assertIn('排队中',queued)
        self.assertIn('本次已读取 <b>0</b>',queued)

    def test_date_backfill_distinguishes_scanned_window_matches_and_new_records(self):
        run={'status':'成功','fetched':173,'inserted':16,'duplicates':250,
            'request_json':json.dumps({'mode':'date_range','pages':200,
                'start_at':'2026-09-17T00:00:00+08:00','end_at':'2026-09-24T00:00:00+08:00'}),
            'result_json':json.dumps({'scanned_count':750,'matched_count':173,
                'oldest_date':'2026-09-16T00:00:00Z','newest_date':'2026-09-24T00:00:00Z'})}
        markup=progress_card(run)
        self.assertIn('本次已读取 <b>750</b>',markup)
        self.assertIn('新增入库 <b>16</b>',markup)
        self.assertIn('目标范围内去重评论 · <b>173 条</b>',markup)
        self.assertIn('本轮扫描到的评论时间 · 2026-09-16 08:00 — 2026-09-24 08:00',markup)
        self.assertIn('目标日期范围 · 2026-09-17 00:00 — 2026-09-24 00:00',markup)
        self.assertIn('结束时间不含',markup)
        self.assertNotIn('250',markup)

    def test_live_total_panel_reads_new_count_and_time_from_database(self):
        entry=self.folder/'live_totals.py'
        entry.write_text(f'''from pathlib import Path
from collection_ui import collection_live_totals
collection_live_totals(Path({str(self.db)!r}),{{'app_id':'111','country':'cn'}})
''',encoding='utf-8')
        app=AppTest.from_file(str(entry),default_timeout=15).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('累计已采集</span><strong>0' in item.value for item in app.markdown))
        save_reviews(self.db,[RawReview(platform='App Store',external_id=identity,app_id='111',country='cn',
            date=stamp,author='测试',title='',content='评论',rating=4)
            for identity,stamp in [('one','2026-09-01T00:00:00Z'),('two','2026-09-24T00:00:00Z')]])
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('累计已采集</span><strong>2' in item.value for item in app.markdown))
        self.assertTrue(any('2026-09-01 08:00 — 2026-09-24 08:00' in item.value for item in app.markdown))

    def test_history_and_status_are_visible_in_colored_journal(self):
        markup=log_card({'status':'已暂停','fetched':25,'started_at':'2026-09-01T00:00:00Z',
            'request_json':json.dumps({'mode':'history'})})
        self.assertIn('collection-log paused',markup)
        self.assertIn('历史回溯',markup)
        self.assertIn('已暂停',markup)
        self.assertNotIn('<script>',log_card({'status':'<script>bad</script>'}))

    def test_latest_and_history_forms_submit_their_default_large_budgets(self):
        app=self.form()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox[0].value,'5,000 条')
        self.assertEqual(len(app.date_input),0)
        with patch('collection_ui.request_collection',return_value=True) as request:
            next(button for button in app.button if button.label=='按以上设置开始采集').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(request.call_args.kwargs,{'mode':'latest','pages':100})
        app.radio[0].set_value('历史回溯').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox[0].value,'10,000 条')
        with patch('collection_ui.request_collection',return_value=True) as request:
            next(button for button in app.button if button.label=='按以上设置开始采集').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(request.call_args.kwargs,{'mode':'history','pages':200})

    def test_custom_million_budget_and_incomplete_date_range(self):
        app=self.form()
        app.radio[0].set_value('按固定日期补采').run()
        app.selectbox[0].set_value('自定义数量').run()
        app.number_input[0].set_value(1_000_000).run()
        app.date_input[0].set_value((date(2026,1,1),)).run()
        with patch('collection_ui.request_collection',return_value=True) as request:
            next(button for button in app.button if button.label=='按以上设置开始采集').click().run()
            self.assertFalse(app.exception)
            request.assert_not_called()
            self.assertTrue(any('起始日期' in error.value for error in app.error))
        app.date_input[0].set_value((date(2026,1,1),date(2026,1,2))).run()
        with patch('collection_ui.request_collection',return_value=True) as request:
            next(button for button in app.button if button.label=='按以上设置开始采集').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(request.call_args.kwargs['pages'],20000)
            self.assertEqual(request.call_args.kwargs['start_at'],'2026-01-01T00:00:00+08:00')
            self.assertEqual(request.call_args.kwargs['end_at'],'2026-01-03T00:00:00+08:00')

    def test_active_page_writes_do_not_rebuild_dashboard_but_completion_does(self):
        configure_target(self.db,'111','cn')
        with database(self.db) as connection:
            run_id=connection.execute('INSERT INTO collection_runs(app_id,country,started_at,status) VALUES(?,?,?,?)',
                ('111','cn',datetime.now(timezone.utc).isoformat(),'采集中')).lastrowid
        initial=dashboard_update_signature(self.db,'111','cn')
        save_reviews(self.db,[RawReview(platform='App Store',external_id='new',app_id='111',country='cn',
            date='2026-09-01T00:00:00Z',author='测试',title='',content='评论',rating=5)])
        with database(self.db) as connection:
            connection.execute('UPDATE collection_runs SET fetched=50,inserted=50,pages=1 WHERE id=?',(run_id,))
        self.assertEqual(initial,dashboard_update_signature(self.db,'111','cn'))
        with database(self.db) as connection:
            connection.execute("UPDATE collection_runs SET status='成功' WHERE id=?",(run_id,))
        self.assertNotEqual(initial,dashboard_update_signature(self.db,'111','cn'))

    def test_progress_controls_follow_pause_resume_cancel_without_removing_counts(self):
        state={'active_status':'采集中','active_run_id':7}
        run={'id':7,'status':'采集中','fetched':300,'inserted':250,
            'request_json':json.dumps({'mode':'history'})}
        entry=self.folder/'collection_progress.py'
        entry.write_text(f'''from pathlib import Path
from collection_ui import collection_live_progress
collection_live_progress(Path({str(self.db)!r}),{{'app_id':'111','country':'cn'}})
''',encoding='utf-8')
        def update(*args,action):
            state['active_status']={'pause':'已暂停','resume':'等待续采','cancel':'已取消'}[action]
            run['status']=state['active_status']
            return True
        with patch('collection_ui.target_state',side_effect=lambda *args:state), \
             patch('collection_ui.collection_progress',side_effect=lambda *args:run), \
             patch('collection_ui.control_collection',side_effect=update) as control:
            app=AppTest.from_file(str(entry),default_timeout=15).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.button(key='collection_live_111_cn_pause').disabled)
            self.assertTrue(app.button(key='collection_live_111_cn_resume').disabled)
            app.button(key='collection_live_111_cn_pause').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key='collection_live_111_cn_pause').disabled)
            self.assertFalse(app.button(key='collection_live_111_cn_resume').disabled)
            app.button(key='collection_live_111_cn_resume').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(control.call_args.kwargs,{'action':'resume'})
            app.button(key='collection_live_111_cn_cancel').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key='collection_live_111_cn_cancel').disabled)
            self.assertTrue(any('新增入库 <b>250</b>' in item.value for item in app.markdown))

    def test_whole_app_keeps_totals_and_charts_while_bulk_settings_skip_analysis(self):
        import workbench_ui
        now=datetime.now(timezone.utc)
        save_reviews(self.db,[RawReview(platform='App Store',external_id=identity,app_id='111',country='cn',
            date=(now-age).isoformat(),author='隔离验收',title='测试评论',content='更新之后闪退无法登录',rating=1)
            for identity,age in [('current',timedelta(hours=1)),('previous',timedelta(days=8)),('old',timedelta(days=400))]])
        configure_target(self.db,'111','cn',pages=100)
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=40)
        with patch.dict(os.environ,{'APPSTORE_DISABLE_WORKER':'1','APPSTORE_DISABLE_ICON_FETCH':'1'}), \
             patch.multiple(workbench_ui,DB=self.db,DATA=self.folder,OUTPUT=self.folder/'outputs'), \
             patch('workbench_ui.read_reviews',wraps=workbench_ui.read_reviews) as read, \
             patch('workbench_ui.analyzed',wraps=workbench_ui.analyzed) as analyze, \
             patch('requests.sessions.Session.request',side_effect=AssertionError('UI验收禁止联网')):
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key='agent_111_cn_mode').value,'规则初筛')
            self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'1')
            self.assertEqual(len(app.get('plotly_chart')),10)
            self.assertTrue(any('累计已采集</span><strong>3' in item.value for item in app.markdown))
            self.assertTrue(read.call_args.kwargs.get('start_at'))
            self.assertTrue(read.call_args.kwargs.get('end_at'))
            self.assertLessEqual((now-datetime.fromisoformat(read.call_args.kwargs['start_at'])).days,14)
            analyze.reset_mock()
            app.sidebar.radio(key='workspace_page').set_value('采集与设置').run()
            self.assertFalse(app.exception)
            analyze.assert_not_called()
            self.assertEqual(len(app.get('plotly_chart')),0)
            self.assertEqual(app.radio(key='collect_111_cn_mode').options,
                ['最新评论 / 衔接上次采集','按固定日期补采','历史回溯'])
            self.assertEqual(app.selectbox(key='collect_111_cn_latest_budget').value,'5,000 条')
            app.radio(key='collect_111_cn_mode').set_value('按固定日期补采').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.date_input(key='collect_111_cn_dates').value),2)
            self.assertEqual(app.selectbox(key='collect_111_cn_date_range_budget').value,'10,000 条')
            app.radio(key='collect_111_cn_mode').set_value('历史回溯').run()
            app.selectbox(key='collect_111_cn_history_budget').set_value('自定义数量').run()
            app.number_input(key='collect_111_cn_history_count').set_value(1_000_000).run()
            next(button for button in app.button if button.label=='按以上设置开始采集').click().run()
            self.assertFalse(app.exception)
            analyze.assert_not_called()
            queued=target_state(self.db,'111','cn')
            self.assertEqual(json.loads(queued['request_json'])['pages'],20000)
            self.assertEqual(json.loads(queued['request_json'])['mode'],'history')
            self.assertTrue(queued['requested'])
            self.assertTrue(app.button(key='collection_live_111_cn_pause').disabled)
            self.assertFalse(app.button(key='collection_live_111_cn_cancel').disabled)
            app.sidebar.radio(key='workspace_page').set_value('舆情概览').run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.get('plotly_chart')),10)
            self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'1')
            app.button(key='collection_live_111_cn_cancel').click().run()
            self.assertFalse(app.exception)
            self.assertFalse(target_state(self.db,'111','cn')['requested'])
            self.assertEqual(collection_totals(self.db,'111','cn')['total'],3)


if __name__=='__main__': unittest.main()
