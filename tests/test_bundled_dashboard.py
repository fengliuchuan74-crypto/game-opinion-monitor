"""Bundled review defaults remain offline and respect subsequent user choices."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import workbench_ui
from collectors.base import RawReview
from modules.review_store import save_reviews, upsert_game_profile
from modules.storage import database


class BundledDashboardTests(unittest.TestCase):
    def setUp(self):
        self.folder=Path(__file__).resolve().parents[1]/'.test-tmp'/('bundled-ui-'+uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.folder)
        self.db=self.folder/'reviews.sqlite3'
        save_reviews(self.db,[RawReview(platform='App Store',external_id=f'{app_id}-{number}',
            app_id=app_id,country='cn',date='2024-01-01T01:00:00Z',author='验收玩家',
            title='体验',content='更新后闪退' if number%2 else '剧情很好玩',rating=1 if number%2 else 5)
            for app_id,total in [('111',3),('222',2)] for number in range(total)])
        upsert_game_profile(self.db,app_id='111',country='cn',app_name='较多评论游戏')
        upsert_game_profile(self.db,app_id='222',country='cn',app_name='快照首选游戏')
        self.info={'snapshot_id':'synthetic-ui-test','created_at':'2024-01-02T00:00:00Z',
            'review_count':5,'preferred_app_id':'222','preferred_country':'cn',
            'earliest_review_at':'2024-01-01T01:00:00Z','latest_review_at':'2024-01-01T01:00:00Z'}
        self.primary='快照首选游戏 · cn (222)'
        self.other='较多评论游戏 · cn (111)'

    def app(self,*,marked=True,state=None,stored_agent=False):
        if marked:
            with database(self.db) as connection:
                connection.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES('bundled_snapshot',?)",
                    (json.dumps(self.info,ensure_ascii=False),))
        guards=ExitStack()
        self.addCleanup(guards.close)
        guards.enter_context(patch.dict(os.environ,{'APPSTORE_DISABLE_WORKER':'1','APPSTORE_DISABLE_ICON_FETCH':'1'}))
        guards.enter_context(patch.multiple(workbench_ui,DB=self.db,DATA=self.folder,OUTPUT=self.folder/'outputs'))
        # Bootstrap is separately tested against its real bundle. These tests use
        # a small, marked synthetic database to exercise only the page defaults.
        guards.enter_context(patch.object(workbench_ui,'ensure_bundled_snapshot'))
        guards.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('Snapshot browsing must stay offline')))
        self.runtime=guards.enter_context(patch('agent_ui.check_runtime',side_effect=AssertionError('Rule browsing must not check Codex')))
        if stored_agent:
            analyze=workbench_ui.analyzed
            def with_stored_results(raw,revision,path):
                return analyze(raw,revision,path).assign(agent_analyzed=True)
            guards.enter_context(patch.object(workbench_ui,'analyzed',side_effect=with_stored_results))
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=45)
        for key,value in (state or {}).items(): app.session_state[key]=value
        app.run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        return app

    def test_snapshot_opens_preferred_game_all_history_and_rules_even_with_agent_results(self):
        app=self.app(stored_agent=True)
        self.assertEqual(app.selectbox(key='current_game').value,self.primary)
        self.assertEqual(app.selectbox(key='statistics_window').value,'全部已采集历史')
        self.assertEqual(app.radio(key='agent_222_cn_mode').value,'规则初筛')
        self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'2')
        self.assertEqual(len(app.get('plotly_chart')),10)
        self.assertTrue(any('随附真实 App Store 评论快照' in item.value and '初始全库 5 条' in item.value
            and '不代表实时评论' in item.value for item in app.caption))
        self.runtime.assert_not_called()

    def test_user_selection_and_next_game_request_take_precedence(self):
        app=self.app(state={'next_game':self.other})
        self.assertEqual(app.selectbox(key='current_game').value,self.other)
        app.selectbox(key='statistics_window').set_value('近 30 天').run()
        app.selectbox(key='current_game').set_value(self.primary).run()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='statistics_window').value,'近 30 天')
        self.assertEqual(app.selectbox(key='current_game').value,self.primary)
        app.session_state['next_game']=self.other
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='current_game').value,self.other)
        self.assertEqual(app.selectbox(key='statistics_window').value,'近 30 天')

    def test_existing_unmarked_database_keeps_previous_defaults(self):
        app=self.app(marked=False)
        self.assertEqual(app.selectbox(key='current_game').value,self.other)
        self.assertEqual(app.selectbox(key='statistics_window').value,'近 7 天')
        self.assertFalse(any('随附真实 App Store 评论快照' in item.value for item in app.caption))
        self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'0')

    def test_history_report_requires_fixed_filename_hash_and_matching_profile(self):
        bundle=self.folder/'bundled_data'
        bundle.mkdir()
        body='# 隔离历史报告\n\n合成测试说明。'
        (bundle/'agent-report.md').write_bytes(body.encode('utf-8'))
        summary={'filename':'agent-report.md','app_id':'222','country':'cn',
            'start_at':'2024-01-01T00:00:00Z','end_at':'2024-01-02T00:00:00Z',
            'total':2,'analyzed':2,'sha256':hashlib.sha256(body.encode()).hexdigest()}
        info={**self.info,'report_summary':summary}
        profile={'app_id':'222','country':'cn'}
        with patch.object(workbench_ui,'ROOT',self.folder):
            self.assertEqual(workbench_ui._bundled_report(info,profile)[1],body)
            self.assertIsNone(workbench_ui._bundled_report(info,{'app_id':'111','country':'cn'}))
            self.assertIsNone(workbench_ui._bundled_report(info,{'app_id':'222','country':'us'}))
            for changed in [{'filename':'../agent-report.md'},{'sha256':'0'*64},{'analyzed':1},
                    {'start_at':'2024-01-01'},{'end_at':'2023-01-01T00:00:00Z'}]:
                with self.subTest(changed=changed):
                    self.assertIsNone(workbench_ui._bundled_report({**info,'report_summary':{**summary,**changed}},profile))
            (bundle/'agent-report.md').unlink()
            self.assertIsNone(workbench_ui._bundled_report(info,profile))

    def test_history_report_stays_collapsed_and_explains_its_independent_window(self):
        bundle=self.folder/'bundled_data'
        bundle.mkdir()
        body='# 隔离历史报告\n\n<script>test()</script>'
        (bundle/'agent-report.md').write_bytes(body.encode('utf-8'))
        info={**self.info,'report_summary':{'filename':'agent-report.md','app_id':'222','country':'cn',
            'start_at':'2024-01-01T00:00:00Z','end_at':'2024-01-02T00:00:00Z',
            'total':2,'analyzed':2,'sha256':hashlib.sha256(body.encode()).hexdigest()}}
        entry=self.folder/'report_preview.py'
        entry.write_text(f'''from pathlib import Path
from unittest.mock import patch
import workbench_ui as ui
with patch.object(ui,'ROOT',Path({str(self.folder)!r})):
    ui.snapshot_caption({info!r},{{'app_id':'222','country':'cn'}})
''',encoding='utf-8')
        with patch('requests.sessions.Session.request',side_effect=AssertionError('Historical report must stay offline')):
            app=AppTest.from_file(str(entry),default_timeout=15).run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        self.assertEqual(app.expander[0].label,'查看随附 Agent 历史报告 · 2 条')
        self.assertFalse(app.expander[0].proto.expanded)
        self.assertFalse(app.markdown[0].proto.allow_html)
        self.assertEqual(len(app.get('download_button')),1)
        self.assertTrue(any('2024-01-01 08:00' in item.value and '不随当前看板筛选变化' in item.value
            and '不会启动新的 Agent' in item.value for item in app.caption))


if __name__=='__main__': unittest.main()
