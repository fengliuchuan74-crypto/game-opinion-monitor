from __future__ import annotations

import unittest
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench_ui import _browser_card, _browser_status
from collectors.base import RawReview
from modules.operations import LOCAL_TZ
from modules.review_store import save_reviews
from streamlit.testing.v1 import AppTest


class ReviewBrowserTests(unittest.TestCase):
    def test_agent_status_keeps_pending_review_distinct(self):
        rows = pd.DataFrame([
            {'needs_review': False, 'agent_analyzed': True, 'manual_reviewed': False,
             'issue_categories': '服务器/网络问题', 'issue_category': '服务器/网络问题'},
            {'needs_review': True, 'agent_analyzed': True, 'manual_reviewed': False,
             'issue_categories': 'Bug/闪退问题', 'issue_category': 'Bug/闪退问题'},
            {'needs_review': True, 'agent_analyzed': False, 'manual_reviewed': False,
             'issue_categories': '', 'issue_category': '未归类/待复核'},
            {'needs_review': True, 'agent_analyzed': True, 'manual_reviewed': True,
             'issue_categories': '内容/玩法反馈', 'issue_category': '内容/玩法反馈'},
        ])
        result = _browser_status(rows, 'Agent分析')
        self.assertEqual(result['_browser_status'].tolist(), ['已分析', '待复核', '待分析', '人工复核'])

    def test_card_escapes_review_text_and_shows_agent_evidence(self):
        row = {
            '_browser_status': '待复核', 'sentiment_label': '差评', 'rating': 1,
            'issue_categories': '服务器/网络问题', 'issue_category': '服务器/网络问题',
            'content': '<script>alert("x")</script>', 'title': '<b>坏标题</b>',
            'date': pd.Timestamp('2026-09-24T08:00:00Z'), 'review_id': 7,
            'author': '玩家', 'topic': 'version:1.2', 'agent_analyzed': True,
            'agent_reason': '网络错误', 'agent_demand': '修复登录',
            'agent_quote': '<原文依据>',
        }
        markup = _browser_card(row)
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;alert', markup)
        self.assertIn('&lt;b&gt;坏标题&lt;/b&gt;', markup)
        self.assertIn('玩家诉求', markup)
        self.assertIn('修复登录', markup)
        self.assertIn('&lt;原文依据&gt;', markup)
        self.assertIn('版本 1.2', markup)
        row['topic']=''
        self.assertIn('版本未能确定', _browser_card(row))
        self.assertNotIn('版本未知', _browser_card(row))


class ReviewBrowserQueryTests(unittest.TestCase):
    """Actual UI/database checks: only the selected scope reaches analysis."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Path(temp.name) / 'reviews.sqlite3'
        self.today = datetime.now(LOCAL_TZ).date()
        self.prefix = 'browser_111_cn'
        rows = []
        for age in [*range(10), 29, 30, 40]:
            stamp = pd.Timestamp(self.today - timedelta(days=age), tz=LOCAL_TZ) + pd.Timedelta(hours=12)
            rows.append(RawReview(platform='App Store', external_id=f'r{age}',
                app_id='111', country='cn', date=stamp.isoformat(), author='测试玩家',
                title=f'第 {age} 天', content=f'第 {age} 天更新后闪退，无法进入游戏', rating=1,
                topic='version:6.0.0' if age==0 else ''))
        # SQL totals and limits must remain scoped to this game and market.
        rows.append(RawReview(platform='App Store', external_id='other', app_id='222',
            country='cn', date=rows[0].date, author='玩家', title='其他游戏', content='其他游戏评论', rating=3))
        save_reviews(self.db, rows)

    def app(self, state=None):
        script = f'''
from pathlib import Path
from unittest.mock import patch
import streamlit as st
import workbench_ui as ui
from modules.operations import analyze_reviews
from modules.review_store import read_reviews

def record_read(db, **options):
    st.session_state['sql_options'] = options
    return read_reviews(db, **options)

def record_analysis(raw, revision, path):
    st.session_state['analysis_ids'] = raw['external_id'].tolist()
    result = analyze_reviews(raw, Path(path))
    if not result.empty:
        result['agent_analyzed'] = ~result['external_id'].eq('r0')
        result['needs_review'] = result['external_id'].eq('r1')
    return result

with patch.object(ui, 'DB', Path({str(self.db)!r})), patch.object(ui, 'get_version_history', side_effect=lambda *args:st.session_state.get('version_history_fixture',{{}})), patch.object(ui, 'read_reviews', side_effect=record_read), patch.object(ui, 'analyzed', side_effect=record_analysis):
    ui.render_review_browser({{'app_id':'111', 'country':'cn', 'app_name':'测试游戏'}}, st.session_state.pop('request', None))
'''
        app = AppTest.from_string(script, default_timeout=20)
        for key, value in (state or {}).items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception, [e.message for e in app.exception])
        return app

    def assert_total(self, app, total=13):
        self.assertTrue(any(f'当前库共 {total:,} 条' in value.value for value in app.markdown))

    def test_rolling_ranges_filter_before_analysis_and_keep_database_total(self):
        app = self.app()
        self.assertEqual(set(app.session_state['analysis_ids']), {f'r{i}' for i in range(7)})
        self.assertIn('start_at', app.session_state['sql_options'])
        self.assertIn('end_at', app.session_state['sql_options'])
        self.assert_total(app)
        self.assertTrue(any('版本 6.0.0' in item.value for item in app.markdown))
        self.assertTrue(any('版本未能确定' in item.value for item in app.markdown))
        app.selectbox(key=self.prefix+'_range').set_value('近 30 天').run()
        self.assertFalse(app.exception)
        self.assertEqual(set(app.session_state['analysis_ids']), {f'r{i}' for i in [*range(10),29]})
        self.assert_total(app)

    def test_latest_n_limits_sql_and_retains_full_count_maximum(self):
        app = self.app({self.prefix+'_range':'最新 N 条', self.prefix+'_count':3})
        self.assertEqual(app.session_state['analysis_ids'], ['r0','r1','r2'])
        self.assertEqual(app.session_state['sql_options']['limit'], 3)
        self.assertEqual(app.number_input(key=self.prefix+'_count').max, 13)
        self.assert_total(app)

    def test_custom_range_and_dashboard_range_use_local_calendar_boundaries(self):
        start, last = self.today-timedelta(days=9), self.today-timedelta(days=8)
        app = self.app({self.prefix+'_range':'自定义日期', self.prefix+'_dates':(start,last)})
        self.assertEqual(set(app.session_state['analysis_ids']), {'r8','r9'})
        self.assertEqual(pd.Timestamp(app.session_state['sql_options']['end_at']).date(), last+timedelta(days=1))
        end = pd.Timestamp(self.today-timedelta(days=7), tz=LOCAL_TZ)
        app.session_state['request'] = {'mode':'Agent分析','status':'待分析',
            'start':pd.Timestamp(start, tz=LOCAL_TZ).isoformat(), 'end':end.isoformat()}
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key=self.prefix+'_range').value, '看板统计范围')
        self.assertEqual(set(app.session_state['analysis_ids']), {'r8','r9'})
        self.assert_total(app)

    def test_incomplete_custom_range_does_not_fall_back_to_all_reviews(self):
        app = self.app({self.prefix+'_range':'自定义日期', self.prefix+'_dates':(self.today,)})
        self.assertNotIn('analysis_ids', app.session_state)
        self.assertNotIn('sql_options', app.session_state)
        self.assertTrue(any('完整的开始日期' in item.value for item in app.info))

    def test_all_collected_is_complete_and_pagination_search_and_table_still_work(self):
        app = self.app({self.prefix+'_range':'全部已采集'})
        self.assertEqual(len(app.session_state['analysis_ids']), 13)
        self.assertNotIn('limit', app.session_state['sql_options'])
        self.assertNotIn('start_at', app.session_state['sql_options'])
        self.assertEqual(app.number_input(key=self.prefix+'_page').max, 2)
        app.number_input(key=self.prefix+'_page').set_value(2).run()
        self.assertFalse(app.exception)
        self.assertTrue(any('第 40 天' in item.value for item in app.markdown))
        app.text_input(key=self.prefix+'_query').set_value('第 40 天').run()
        self.assertEqual(app.number_input(key=self.prefix+'_page').value, 1)
        app.radio(key=self.prefix+'_display').set_value('彩色表格').run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(app.dataframe[0].value['评论版本'].tolist(), ['版本未能确定'])
        self.assert_total(app)

    def test_status_filter_uses_current_scope_and_keeps_agent_pending_separate(self):
        app = self.app({self.prefix+'_range':'最新 N 条',self.prefix+'_count':3,
            self.prefix+'_mode':'Agent分析'})
        self.assertFalse(any(button.key == self.prefix+'_quick_review' for button in app.button))
        app.selectbox(key=self.prefix+'_status').set_value('待复核').run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key=self.prefix+'_status').value, '待复核')
        self.assertEqual(app.selectbox(key=self.prefix+'_range').value, '最新 N 条')
        cards = [item.value for item in app.markdown if '<article' in item.value]
        self.assertEqual(len(cards), 1)
        self.assertIn('第 1 天', cards[0])
        app.selectbox(key=self.prefix+'_status').set_value('待分析').run()
        self.assertFalse(app.exception)
        cards = [item.value for item in app.markdown if '<article' in item.value]
        self.assertEqual(len(cards), 1)
        self.assertIn('第 0 天', cards[0])
        self.assertIn('Agent 尚未分析', cards[0])

    def test_version_filters_separate_source_from_date_inference(self):
        stamp=lambda days:pd.Timestamp(self.today-timedelta(days=days),tz=LOCAL_TZ).isoformat()
        history={'app_id':'111','country':'cn','checked_at':stamp(-1),'releases':[
            {'version':'6.0.0','released_at':stamp(20),'precision':'timestamp'},
            {'version':'7.0.0','released_at':stamp(3),'precision':'timestamp'}]}
        app=self.app({self.prefix+'_range':'全部已采集','version_history_fixture':history})
        self.assertFalse(app.exception)
        app.selectbox(key=self.prefix+'_version_basis').set_value('来源提供').run()
        cards=[item.value for item in app.markdown if '<article' in item.value]
        self.assertEqual(len(cards),1)
        self.assertIn('版本 6.0.0（来源提供）',cards[0])
        app.selectbox(key=self.prefix+'_version_basis').set_value('按日期推定').run()
        app.selectbox(key=self.prefix+'_version').set_value('7.0.0').run()
        cards=[item.value for item in app.markdown if '<article' in item.value]
        self.assertEqual(len(cards),3)
        self.assertTrue(all('版本 7.0.0（按日期推定）' in card for card in cards))
        app.radio(key=self.prefix+'_display').set_value('彩色表格').run()
        self.assertEqual(app.dataframe[0].value['评论版本'].tolist(),['版本 7.0.0（按日期推定）']*3)


if __name__ == '__main__':
    unittest.main()
