"""First-start data import must be offline, atomic, repeatable and preserve local work."""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from collectors.base import RawReview
from modules.bundled_snapshot import TABLES, ensure_bundled_snapshot, snapshot_info
from modules.operations import analyze_reviews
from modules.review_store import init_db, read_reviews, save_reviews, upsert_game_profile
from modules.storage import database


class BundledSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.sqlite3'
        self.target = self.root / 'local' / 'reviews.sqlite3'
        self.folder = self.root / 'bundle'
        self.folder.mkdir()
        init_db(self.source)
        upsert_game_profile(self.source, app_id='111', country='cn', app_name='快照测试游戏')
        save_reviews(self.source, [RawReview(platform='App Store', app_id='111', country='cn',
            external_id='public-test-1', data_source='app_store_public_feed',
            date='2026-09-20T00:00:00Z', author='TestUser', title='测试', content='更新后闪退，无法登录', rating=1)])
        with database(self.source) as connection:
            self.payload = {'format_version': 1, 'tables': {
                table: [dict(row) for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}")]
                for table, columns in TABLES.items()
            }, 'caches': {}}
        self.metadata = dict(format_version=1, snapshot_id='fixture-20260924', created_at='2026-09-24T00:00:00Z',
            review_count=1, game_count=1, agent_result_count=0, preferred_app_id='111', preferred_country='cn')
        self.write_bundle()

    def write_bundle(self):
        packed = gzip.compress(json.dumps(self.payload, ensure_ascii=False).encode('utf-8'), mtime=0)
        (self.folder / 'snapshot.json.gz').write_bytes(packed)
        self.metadata['sha256'] = hashlib.sha256(packed).hexdigest()
        (self.folder / 'manifest.json').write_text(json.dumps(self.metadata), encoding='utf-8')

    def test_new_install_shows_real_rule_analysis_without_network_or_model(self):
        failure = AssertionError('Snapshot browsing must not invoke network or models')
        with patch('requests.sessions.Session.request', side_effect=failure), \
                patch('socket.create_connection', side_effect=failure), \
                patch('modules.codex_runner.run_codex', side_effect=failure):
            metadata = ensure_bundled_snapshot(self.target, self.folder)
            data = analyze_reviews(read_reviews(self.target), self.target)
        self.assertEqual(metadata['review_count'], 1)
        self.assertEqual(data.iloc[0].sentiment_label, '差评')
        self.assertIn('Bug/闪退问题', data.iloc[0].issue_categories)
        self.assertEqual(snapshot_info(self.target)['snapshot_id'], 'fixture-20260924')
        with database(self.target) as connection:
            for table in ('monitor_targets', 'agent_runs', 'collection_runs', 'runtime_state'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_settings WHERE auto_enabled=1').fetchone()[0], 0)

    def test_repeated_start_or_new_release_does_not_overwrite_local_comments(self):
        ensure_bundled_snapshot(self.target, self.folder)
        with database(self.target) as connection:
            connection.execute("UPDATE review_records SET content='本地保留内容'")
        self.payload['tables']['review_records'][0]['content'] = '新版随附快照'
        self.metadata['snapshot_id'] = 'newer'
        self.write_bundle()
        ensure_bundled_snapshot(self.target, self.folder)
        self.assertEqual(len(read_reviews(self.target)), 1)
        self.assertEqual(read_reviews(self.target).iloc[0].content, '本地保留内容')
        self.assertEqual(snapshot_info(self.target)['snapshot_id'], 'fixture-20260924')

    def test_existing_user_data_or_configuration_is_not_seeded(self):
        for kind in ('review', 'profile', 'queued_job'):
            target = self.root / kind / 'reviews.sqlite3'
            init_db(target)
            with database(target) as connection:
                if kind == 'review':
                    connection.execute("INSERT INTO review_records(dedupe_key,content_hash,platform,data_source,collected_at,content) VALUES('local','hash','App Store','import','2026-09-01','用户数据')")
                elif kind == 'profile':
                    connection.execute("INSERT INTO game_profiles(app_id,country,app_name,created_at,updated_at) VALUES('999','us','本地游戏','now','now')")
                else:
                    connection.execute("INSERT INTO monitor_targets(app_id,country,requested) VALUES('999','us',1)")
            self.assertEqual(ensure_bundled_snapshot(target, self.folder), {})
            self.assertEqual(snapshot_info(target), {})
            with database(target) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_records WHERE external_id='public-test-1'").fetchone()[0], 0)

    def test_corrupt_or_invalid_snapshot_leaves_no_partial_import(self):
        (self.folder / 'snapshot.json.gz').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            ensure_bundled_snapshot(self.target, self.folder)
        self.assertTrue(read_reviews(self.target).empty)
        self.assertEqual(snapshot_info(self.target), {})
        self.payload['tables']['review_records'].append(dict(self.payload['tables']['review_records'][0], id=2))
        self.metadata['review_count'] = 2
        self.write_bundle()
        with self.assertRaises(sqlite3.IntegrityError):  # Profile and reviews roll back together.
            ensure_bundled_snapshot(self.target, self.folder)
        with database(self.target) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM game_profiles').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM review_records').fetchone()[0], 0)
        self.assertEqual(snapshot_info(self.target), {})

    def test_parallel_initializers_import_once_and_keep_deletions(self):
        init_db(self.target)
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: ensure_bundled_snapshot(self.target, self.folder), range(2)))
        self.assertEqual([row['review_count'] for row in rows], [1, 1])
        self.assertEqual(len(read_reviews(self.target)), 1)
        with database(self.target) as connection:
            connection.execute('DELETE FROM review_records')
        ensure_bundled_snapshot(self.target, self.folder)
        self.assertTrue(read_reviews(self.target).empty)

    def test_interrupted_cache_write_rolls_back_and_next_start_recovers(self):
        icon = dict(app_id='111', country='cn', data_uri='data:image/png;base64,fixture', next_check=0)
        self.payload['caches'] = {'app_icons': {'111_cn.json': icon}}
        self.write_bundle()
        def interrupted_dump(value, handle, **kwargs):
            handle.write('{"partial":')
            raise OSError('simulated disk interruption')
        with patch('modules.bundled_snapshot.json.dump', side_effect=interrupted_dump):
            with self.assertRaisesRegex(OSError, 'simulated disk interruption'):
                ensure_bundled_snapshot(self.target, self.folder)
        self.assertTrue(read_reviews(self.target).empty)
        self.assertEqual(snapshot_info(self.target), {})
        target = self.target.parent / 'app_icons' / '111_cn.json'
        self.assertFalse(target.exists())
        ensure_bundled_snapshot(self.target, self.folder)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), icon)
        self.assertEqual(len(read_reviews(self.target)), 1)

    def test_saved_agent_results_ignore_new_machine_effort_limits_but_new_jobs_validate(self):
        from modules.agent_analysis import PROMPT_VERSION, analysis_status, configure_analysis, request_analysis
        review = self.payload['tables']['review_records'][0]
        result = dict(review_id=review['id'], content_hash=review['content_hash'],
            sentiment_label='中评', issue_category='内容/玩法反馈', issue_categories=['内容/玩法反馈'],
            needs_review=False, reason='随附历史解读', demand='修复登录', target='游戏', evidence_quote=review['content'])
        self.payload['tables']['agent_review_results'] = [dict(review_id=review['id'],
            content_hash=review['content_hash'], model='fixture-model', reasoning_effort='medium',
            prompt_version=PROMPT_VERSION, result_json=json.dumps(result), actual_model='fixture-model',
            run_id=6, analyzed_at='2026-09-24T00:00:00Z')]
        self.payload['tables']['agent_settings'] = [dict(app_id='111', country='cn', model='fixture-model',
            reasoning_effort='medium', updated_at='2026-09-24T00:00:00Z')]
        self.metadata['agent_result_count'] = 1
        self.write_bundle()
        ensure_bundled_snapshot(self.target, self.folder)
        with patch('modules.codex_runner.available_reasoning_efforts', return_value=('low',)):
            data = analyze_reviews(read_reviews(self.target), self.target)
            status = analysis_status(self.target, '111', 'cn', '2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z')
            self.assertTrue(data.iloc[0].agent_analyzed)
            self.assertEqual(data.iloc[0].agent_reason, '随附历史解读')
            self.assertEqual(status['analyzed'], 1)
            with self.assertRaisesRegex(ValueError, '推理强度'):
                configure_analysis(self.target, '111', 'cn', model='fixture-model', reasoning_effort='medium')
            with self.assertRaisesRegex(ValueError, '推理强度'):
                request_analysis(self.target, '111', 'cn', model='fixture-model', reasoning_effort='medium',
                    start_at='2026-09-01T00:00:00Z', end_at='2026-10-01T00:00:00Z')

    def test_explicit_empty_install_and_missing_bundle_remain_empty(self):
        with patch.dict('os.environ', {'APPSTORE_SKIP_BUNDLED_SNAPSHOT': '1'}):
            self.assertEqual(ensure_bundled_snapshot(self.target, self.folder), {})
        self.assertFalse(self.target.exists())
        self.assertEqual(ensure_bundled_snapshot(self.target, self.root / 'missing'), {})
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
