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

import pandas as pd
import streamlit as st
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

    def app(self,*,marked=True,state=None,stored_agent=False,bundled_report=None):
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
        if bundled_report:
            guards.enter_context(patch.object(workbench_ui,'_bundled_report',side_effect=lambda info,profile:
                (bundled_report,'合成历史报告预览') if profile['app_id']=='222' and profile['country']=='cn' else None))
            self.report={'run_id':42,'summary':'已完成的完整结构化结论',
                'coverage':{**bundled_report,'pending':0},
                'positive':['保留已验证的剧情体验优势'],
                'findings':[{'title':'核实更新后闪退','category':'闪退/卡顿','owner':'客户端团队',
                    'observation':'有玩家报告更新后闪退','hypothesis':'需验证设备与版本条件',
                    'validation':'复现并回访反馈用户','actions':['核对设备与运行日志'],'evidence_ids':[]}]}
            self.report_views=[]
            def panel(db,profile,view):
                prefix=f"agent_{profile['app_id']}_{profile['country']}"
                mode_key=prefix+'_mode'
                if mode_key not in st.session_state:
                    st.session_state[mode_key]=st.session_state.get(prefix+'_preference','规则初筛')
                mode=st.radio('看板分析方式',['Agent分析','规则初筛'],key=mode_key)
                st.session_state[prefix+'_preference']=mode
                self.report_views.append((profile['app_id'],view['start'],view['end']))
                matching=(profile['app_id']=='222' and mode=='Agent分析'
                    and view['start']==pd.Timestamp(bundled_report['start_at'])
                    and view['end']==pd.Timestamp(bundled_report['end_at']))
                return mode,self.report if matching else None,{}
            guards.enter_context(patch.object(workbench_ui,'analysis_panel',side_effect=panel))
            self.latest_report=guards.enter_context(patch.object(workbench_ui,'latest_report',return_value=self.report))
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

    def test_open_completed_report_selects_exact_window_and_renders_dashboard_without_analysis(self):
        summary={'app_id':'222','country':'cn','start_at':'2024-01-01T01:00:00Z',
            'end_at':'2024-01-02T03:47:23.123456Z','total':2,'analyzed':2,'model':'synthetic-model'}
        app=self.app(stored_agent=True,bundled_report=summary,state={
            'workspace_page':'数据与导出','agent_222_cn_model':'user-chosen-model','agent_222_cn_reasoning':'high'})
        app.button(key='bundled_agent_report_open').click().run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        self.assertEqual(app.radio(key='workspace_page').value,'舆情概览')
        self.assertEqual(app.radio(key='agent_222_cn_mode').value,'Agent分析')
        self.assertEqual(app.session_state['agent_222_cn_preference'],'Agent分析')
        self.assertEqual(app.session_state['agent_222_cn_model'],'user-chosen-model')
        self.assertEqual(app.session_state['agent_222_cn_reasoning'],'high')
        self.assertEqual(app.selectbox(key='statistics_window').value,workbench_ui.BUNDLED_REPORT_WINDOW)
        self.assertEqual(self.report_views[-1],('222',pd.Timestamp(summary['start_at']),pd.Timestamp(summary['end_at'])))
        self.assertTrue(any('当前区间 2024-01-01 09:00 — 2024-01-02 11:47' in item.value for item in app.caption))
        self.assertEqual(pd.Timestamp(app.session_state['cutoff']),pd.Timestamp(summary['end_at']))
        self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'2')
        self.assertEqual(len(app.get('plotly_chart')),10)
        self.assertTrue(any('已完成的完整结构化结论' in item.value for item in app.markdown))
        self.assertTrue(any('核实更新后闪退' in item.value for item in app.markdown))
        self.assertFalse(any('当前区间尚无完成' in item.value for item in app.info))
        self.assertTrue(next(item for item in app.button if item.label=='更新统计至当前时间').disabled)
        app.radio(key='workspace_page').set_value('数据与导出').run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        arguments=self.latest_report.call_args.args
        self.assertEqual(arguments[1:3],('222','cn'))
        self.assertEqual(arguments[3:5],(pd.Timestamp(summary['start_at']),pd.Timestamp(summary['end_at'])))
        self.runtime.assert_not_called()

    def test_changing_game_safely_leaves_completed_report_window(self):
        summary={'app_id':'222','country':'cn','start_at':'2024-01-01T00:00:00+08:00',
            'end_at':'2024-01-02T11:47:23+08:00','total':2,'analyzed':2,'model':'synthetic-model'}
        app=self.app(bundled_report=summary)
        self.assertIn(workbench_ui.BUNDLED_REPORT_WINDOW,app.selectbox(key='statistics_window').options)
        app.button(key='bundled_agent_report_open').click().run()
        app.selectbox(key='current_game').set_value(self.other).run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        self.assertEqual(app.selectbox(key='statistics_window').value,'全部已采集历史')
        self.assertNotIn(workbench_ui.BUNDLED_REPORT_WINDOW,app.selectbox(key='statistics_window').options)
        self.assertEqual(app.radio(key='agent_111_cn_mode').value,'规则初筛')
        self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'3')
        self.assertFalse(any(item.key=='bundled_agent_report_open' for item in app.button))


if __name__=='__main__': unittest.main()
