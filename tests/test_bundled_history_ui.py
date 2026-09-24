"""Reproduce viewing the shipped report on a different, already used computer."""
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import workbench_ui as ui
from collectors.base import RawReview
from modules.review_store import save_reviews, upsert_game_profile
from modules.storage import database


ROOT = Path(__file__).resolve().parents[1]


class LaterDate(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2027, 2, 1, 12, tzinfo=tz)


class BundledHistoryUITests(unittest.TestCase):
    def test_independent_workspace_opens_report_when_local_database_only_has_another_game(self):
        folder = ROOT / '.test-tmp' / ('other-game-history-' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        db = folder / 'reviews.sqlite3'
        save_reviews(db, [RawReview(platform='App Store', external_id='other-game-local', app_id='999999999',
            country='us', date='2027-02-01T00:00:00Z', author='LocalPlayer', title='Local review',
            content='This review belongs to another game.', rating=4)])
        upsert_game_profile(db, app_id='999999999', country='us', app_name='Only local game')

        def persisted():
            with database(db) as connection:
                return {table: hashlib.sha256(json.dumps([tuple(row) for row in
                    connection.execute(f'SELECT * FROM {table} ORDER BY rowid')], ensure_ascii=False).encode()).hexdigest()
                    for table in ('game_profiles', 'review_records', 'agent_review_results', 'agent_runs',
                                  'agent_settings', 'monitor_targets', 'collection_runs', 'store_meta')}

        before = persisted()
        published = json.loads((ROOT / 'bundled_data' / 'agent-report.json').read_text(encoding='utf-8'))
        with ExitStack() as guards:
            guards.enter_context(patch.dict(os.environ, APPSTORE_DISABLE_WORKER='1', APPSTORE_DISABLE_ICON_FETCH='1'))
            guards.enter_context(patch.multiple(ui, DB=db, DATA=folder, OUTPUT=folder/'outputs', datetime=LaterDate))
            denied = AssertionError('Independent history must not call network, Codex or local analysis')
            for name in ('requests.sessions.Session.request', 'socket.create_connection',
                         'modules.codex_runner.run_codex', 'agent_ui.check_runtime',
                         'workbench_ui.analyzed', 'workbench_ui.latest_report', 'workbench_ui.analysis_panel'):
                guards.enter_context(patch(name, side_effect=denied))
            app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=60)
            app.session_state['workspace_page'] = '监控总览'
            app.run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            self.assertEqual(app.selectbox(key='current_game').options,
                             ['Only local game · us (999999999)', '＋ 添加游戏'])
            self.assertFalse(any(item.key == 'bundled_agent_report_open' for item in app.button))
            self.assertIn('随附历史分析', app.radio(key='workspace_page').options)
            app.radio(key='workspace_page').set_value('随附历史分析').run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            self.assertEqual(app.selectbox(key='current_game').value, 'Only local game · us (999999999)')
            self.assertTrue(app.selectbox(key='statistics_window').disabled)
            self.assertTrue(app.session_state['scope'].startswith('bundled_history:'))
            self.assertEqual(app.session_state['cutoff'], published['coverage']['end_at'])
            self.assertEqual(next(item.value for item in app.metric if item.label == '评论总数'), '495')
            self.assertEqual(len(app.get('plotly_chart')), 10)
            self.assertTrue(any('495/495' in item.value and '8 项' in item.value for item in app.success))
            rendered = '\n'.join(item.value for item in app.markdown)
            self.assertIn(published['summary'], rendered)
            for finding in published['findings']:
                self.assertIn(finding['title'], rendered)
            self.assertTrue(any(item.label == '导出这份历史分析' for item in app.expander))
            next(item for item in app.button if item.label == '生成本轮交付包').click().run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            delivery = app.session_state['delivery']
            self.assertEqual(delivery['meta']['app_id'], '1618911882')
            self.assertEqual(delivery['meta']['agent']['coverage']['analyzed'], 495)
            self.assertIn(published['summary'], delivery['md'])
            self.assertFalse(any(item.label == '备份完整数据库' for item in app.button))
            # The same independent route also wins over the Add Game selection.
            app.selectbox(key='current_game').set_value('＋ 添加游戏').run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            self.assertEqual(next(item.value for item in app.metric if item.label == '评论总数'), '495')
            self.assertEqual(before, persisted())

    def test_real_history_works_with_different_data_model_date_and_running_local_job(self):
        folder = ROOT / '.test-tmp' / ('independent-history-' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, folder)
        db = folder / 'reviews.sqlite3'
        save_reviews(db, [RawReview(platform='App Store', external_id='local-only', app_id='1618911882',
            country='cn', date='2027-02-01T00:00:00Z', author='本地玩家', title='本地新增评论',
            content='这条本地评论不属于随附报告。', rating=4)])
        upsert_game_profile(db, app_id='1618911882', country='cn', app_name='恋与深空')
        with database(db) as connection:
            connection.execute("INSERT INTO agent_settings(app_id,country,model,reasoning_effort,updated_at) "
                "VALUES('1618911882','cn','different-local-model','high','2027-02-01T00:00:00Z')")
            connection.execute("""INSERT INTO agent_runs(app_id,country,start_at,end_at,model,
                reasoning_effort,prompt_version,scope_hash,status,stage,total,selected,max_reviews,
                snapshot_json,created_at) VALUES('1618911882','cn','2027-02-01T00:00:00Z',
                '2027-02-01T01:00:00Z','different-local-model','high','local-version','local-hash',
                'running','本地任务',1,1,1,'[]','2027-02-01T00:00:00Z')""")

        def persisted():
            with database(db) as connection:
                return {table: hashlib.sha256(json.dumps([tuple(row) for row in
                    connection.execute(f'SELECT * FROM {table} ORDER BY rowid')], ensure_ascii=False).encode()).hexdigest()
                    for table in ('review_records', 'agent_review_results', 'agent_runs', 'agent_settings',
                                  'collection_runs', 'store_meta')}

        before = persisted()
        published = json.loads((ROOT / 'bundled_data' / 'agent-report.json').read_text(encoding='utf-8'))
        with ExitStack() as guards:
            guards.enter_context(patch.dict(os.environ, APPSTORE_DISABLE_WORKER='1', APPSTORE_DISABLE_ICON_FETCH='1'))
            guards.enter_context(patch.multiple(ui, DB=db, DATA=folder, OUTPUT=folder/'outputs', datetime=LaterDate))
            denied = AssertionError('Reading history must not call a model, network, or local analysis view')
            for name in ('requests.sessions.Session.request', 'socket.create_connection',
                         'modules.codex_runner.run_codex', 'agent_ui.check_runtime',
                         'workbench_ui.analyzed', 'workbench_ui.latest_report', 'workbench_ui.analysis_panel'):
                guards.enter_context(patch(name, side_effect=denied))
            app = AppTest.from_file(str(ROOT / 'app.py'), default_timeout=60)
            app.session_state['workspace_page'] = '监控总览'
            app.session_state['agent_1618911882_cn_model'] = 'different-local-model'
            app.session_state['agent_1618911882_cn_reasoning'] = 'high'
            app.run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            app.button(key='bundled_agent_report_open').click().run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            self.assertEqual(app.selectbox(key='statistics_window').value, ui.BUNDLED_REPORT_WINDOW)
            self.assertEqual(next(item.value for item in app.metric if item.label == '评论总数'), '495')
            self.assertEqual(len(app.get('plotly_chart')), 10)
            self.assertTrue(any('495/495' in item.value and '8 项' in item.value for item in app.success))
            self.assertTrue(any('2.3.3' in item.value for item in app.caption))
            rendered = '\n'.join(item.value for item in app.markdown)
            self.assertIn(published['summary'], rendered)
            for finding in published['findings']:
                self.assertIn(finding['title'], rendered)
            self.assertNotIn('当前区间尚无完成', '\n'.join(item.value for item in app.info))
            self.assertEqual(app.session_state['agent_1618911882_cn_model'], 'different-local-model')
            self.assertEqual(app.session_state['agent_1618911882_cn_reasoning'], 'high')
            app.radio(key='workspace_page').set_value('数据与导出').run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            next(item for item in app.button if item.label == '生成本轮交付包').click().run()
            self.assertFalse(app.exception, [item.message for item in app.exception])
            delivery = app.session_state['delivery']
            self.assertIn(published['summary'], delivery['md'])
            self.assertEqual(delivery['meta']['agent']['coverage']['analyzed'], 495)
            self.assertEqual(delivery['meta']['agent']['model'], published['model'])
            self.assertFalse(any(item.label == '备份完整数据库' for item in app.button))
            self.assertEqual(before, persisted())


if __name__ == '__main__':
    unittest.main()
