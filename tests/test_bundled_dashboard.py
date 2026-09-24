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
from streamlit.testing.v1 import AppTest

import workbench_ui
from collectors.base import RawReview
from modules.review_store import read_reviews, save_reviews, upsert_game_profile
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
        guards.enter_context(patch.object(workbench_ui,'published_bundle_metadata',return_value=self.info if marked else {}))
        guards.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('Snapshot browsing must stay offline')))
        self.runtime=guards.enter_context(patch('agent_ui.check_runtime',side_effect=AssertionError('Rule browsing must not check Codex')))
        if bundled_report:
            guards.enter_context(patch.object(workbench_ui,'_bundled_report',side_effect=lambda info,profile:
                (bundled_report,'合成历史报告预览') if profile['app_id']=='222' and profile['country']=='cn' else None))
            self.report={'run_id':42,'summary':'已完成的完整结构化结论','model':bundled_report['model'],
                'reasoning_effort':'medium','from_bundle':True,
                'coverage':{**bundled_report,'pending':0},
                'positive':['保留已验证的剧情体验优势'],
                'findings':[{'title':'核实更新后闪退','category':'闪退/卡顿','owner':'客户端团队',
                    'observation':'有玩家报告更新后闪退','hypothesis':'需验证设备与版本条件',
                    'validation':'复现并回访反馈用户','actions':['核对设备与运行日志'],'evidence_ids':[]}]}
            profile={'app_id':'222','country':'cn','app_name':'快照首选游戏'}
            raw=read_reviews(self.db,app_id='222',country='cn')
            data=workbench_ui.analyze_reviews(raw,None).assign(agent_analyzed=True,analysis_source='Agent')
            data=workbench_ui.attribute_versions(data,{})
            start=pd.Timestamp(bundled_report['start_at']).tz_convert(workbench_ui.LOCAL_TZ)
            end=pd.Timestamp(bundled_report['end_at']).tz_convert(workbench_ui.LOCAL_TZ)
            view=workbench_ui.snapshot(data,start=start,end=end,now=end)
            view.update(analysis_mode='Agent分析',agent_report=self.report,from_bundle=True,undated=0,future=0)
            regions=workbench_ui.region_comparison(data,'222',['cn'],start,end)
            self.bundle={'profile':profile,'view':view,'regions':regions}
            self.bundle_loader=guards.enter_context(patch.object(workbench_ui,'bundled_dashboard',return_value=self.bundle))
            self.analysis_panel=guards.enter_context(patch.object(workbench_ui,'analysis_panel',wraps=workbench_ui.analysis_panel))
            self.latest_report=guards.enter_context(patch.object(workbench_ui,'latest_report',
                side_effect=AssertionError('Historical dashboard must not select reports from the local database')))
            self.export_tools=guards.enter_context(patch.object(workbench_ui,'data_tools',wraps=workbench_ui.data_tools))
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
        self.assertTrue(any('随附真实 App Store 评论快照' in item.value and '随附全库 5 条' in item.value
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
        # The bundle was fixed before this local record arrived. Its dashboard
        # and exported view must continue to contain exactly the published rows.
        save_reviews(self.db,[RawReview(platform='App Store',external_id='local-new-222',app_id='222',
            country='cn',date='2024-01-01T02:00:00Z',author='本地玩家',title='新增体验',content='本地新采集的评论',rating=3)])
        self.analysis_panel.reset_mock()
        self.export_tools.reset_mock()
        app.button(key='bundled_agent_report_open').click().run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        self.assertEqual(app.radio(key='workspace_page').value,'舆情概览')
        self.assertFalse(any(item.key=='agent_222_cn_mode' for item in app.radio))
        self.assertEqual(app.session_state['agent_222_cn_preference'],'Agent分析')
        self.assertEqual(app.session_state['agent_222_cn_model'],'user-chosen-model')
        self.assertEqual(app.session_state['agent_222_cn_reasoning'],'high')
        self.assertEqual(app.selectbox(key='statistics_window').value,workbench_ui.BUNDLED_REPORT_WINDOW)
        self.bundle_loader.assert_called()
        self.analysis_panel.assert_not_called()
        self.latest_report.assert_not_called()
        self.assertTrue(any('历史快照范围（北京时间）：2024-01-01 09:00 — 2024-01-02 11:47' in item.value for item in app.caption))
        self.assertTrue(any('报告模型：synthetic-model · medium' in item.value for item in app.caption))
        self.assertEqual(pd.Timestamp(app.session_state['cutoff']),pd.Timestamp(summary['end_at']))
        self.assertEqual(next(item.value for item in app.metric if item.label=='评论总数'),'2')
        self.assertEqual(len(app.get('plotly_chart')),10)
        self.assertTrue(any('已完成的完整结构化结论' in item.value for item in app.markdown))
        self.assertTrue(any('核实更新后闪退' in item.value for item in app.markdown))
        self.assertTrue(any('已载入随附完整 Agent 分析 · 2/2 条' in item.value for item in app.success))
        self.assertFalse(any('当前区间尚无完成' in item.value for item in app.info))
        self.assertTrue(next(item for item in app.button if item.label=='更新统计至当前时间').disabled)
        app.radio(key='workspace_page').set_value('数据与导出').run()
        self.assertFalse(app.exception,[item.message for item in app.exception])
        arguments=self.export_tools.call_args.args
        self.assertEqual(arguments[0],self.bundle['profile'])
        self.assertIs(arguments[1],self.bundle['view'])
        self.assertEqual(arguments[1]['start'],pd.Timestamp(summary['start_at']))
        self.assertEqual(arguments[1]['end'],pd.Timestamp(summary['end_at']))
        self.assertEqual(len(arguments[1]['current']),2)
        self.assertEqual(len(read_reviews(self.db,app_id='222',country='cn')),3)
        self.latest_report.assert_not_called()
        self.analysis_panel.assert_not_called()
        self.assertEqual(app.session_state['agent_222_cn_model'],'user-chosen-model')
        self.assertEqual(app.session_state['agent_222_cn_reasoning'],'high')
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
