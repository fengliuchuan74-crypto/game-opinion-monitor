"""Published report upgrades must preserve local data and never start Agent work."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from collectors.base import RawReview
from modules.agent_analysis import latest_report
from modules.bundled_reports import restore_completed_report
from modules.bundled_snapshot import TABLES, ensure_bundled_snapshot, snapshot_info
from modules.review_store import init_db, save_reviews
from modules.storage import database


BUNDLE = Path(__file__).resolve().parents[1] / 'bundled_data'


class BundledReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.metadata = json.loads((BUNDLE / 'manifest.json').read_text(encoding='utf-8'))
        cls.summary = cls.metadata['report_summary']
        cls.published = json.loads((BUNDLE / 'agent-report.json').read_text(encoding='utf-8'))
        cls.old_metadata = json.loads(json.dumps(cls.metadata))
        cls.old_metadata['report_summary'].pop('json_filename')
        cls.old_metadata['report_summary'].pop('json_sha256')
        payload = json.loads(gzip.decompress((BUNDLE / 'snapshot.json.gz').read_bytes()))
        cls.fixture = cls.root / 'old-install.sqlite3'
        init_db(cls.fixture)
        with database(cls.fixture) as connection:
            for table, columns in TABLES.items():
                connection.executemany(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [[row[key] for key in columns] for row in payload['tables'][table]])
            connection.execute("INSERT INTO store_meta(key,value) VALUES('bundled_snapshot',?)",
                               (json.dumps(cls.old_metadata, ensure_ascii=False),))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.target = self.work / 'reviews.sqlite3'
        with database(self.fixture) as source:
            destination = sqlite3.connect(self.target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        failure = AssertionError('Reading the bundled report must be fully offline')
        for name in ('requests.sessions.Session.request', 'socket.create_connection',
                     'modules.codex_runner.run_codex'):
            guard = patch(name, side_effect=failure)
            guard.start()
            self.addCleanup(guard.stop)

    def content_digests(self, target=None):
        with database(target or self.target) as connection:
            return {table: hashlib.sha256(json.dumps(
                [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')],
                ensure_ascii=False).encode('utf-8')).hexdigest()
                for table in ('review_records', 'agent_review_results', 'agent_settings', 'game_profiles')}

    def report(self, target=None):
        return latest_report(target or self.target, self.summary['app_id'], self.summary['country'],
                             self.summary['start_at'], self.summary['end_at'])

    def assert_no_work_scheduled(self, target=None):
        with database(target or self.target) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running')").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM agent_settings WHERE auto_enabled=1').fetchone()[0], 0)
            for table in ('monitor_targets', 'collection_runs', 'runtime_state'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def assert_no_report_inserted(self):
        with database(self.target) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM store_meta WHERE key LIKE 'bundled_report:%'").fetchone()[0], 0)
        self.assert_no_work_scheduled()

    def changed_bundle(self):
        folder = self.work / 'damaged-bundle'
        folder.mkdir()
        for name in ('manifest.json', 'snapshot.json.gz', 'agent-report.json'):
            shutil.copyfile(BUNDLE / name, folder / name)
        return folder

    def write_changed_report(self, folder, report):
        body = json.dumps(report, ensure_ascii=False).encode('utf-8')
        (folder / 'agent-report.json').write_bytes(body)
        metadata = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        metadata['report_summary']['json_sha256'] = hashlib.sha256(body).hexdigest()
        (folder / 'manifest.json').write_text(json.dumps(metadata), encoding='utf-8')

    def scoped_review(self):
        with database(self.target) as connection:
            row = connection.execute('''SELECT * FROM review_records
                WHERE app_id=? AND country=? AND julianday(date)>=julianday(?)
                  AND julianday(date)<julianday(?) LIMIT 1''',
                (self.summary['app_id'], self.summary['country'],
                 self.summary['start_at'], self.summary['end_at'])).fetchone()
            values = dict(row)
        return RawReview(**{field.name: values[field.name] for field in fields(RawReview)
                            if field.name in values and field.name != 'raw_json'})

    def test_old_install_restores_complete_report_without_touching_data_or_running_agent(self):
        before = self.content_digests()
        with database(self.target) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM review_records').fetchone()[0], 4076)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_review_results').fetchone()[0], 495)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0], 0)
        metadata = ensure_bundled_snapshot(self.target, BUNDLE)
        # A saved historical report remains readable even when this computer only offers low.
        with patch('modules.codex_runner.available_reasoning_efforts', return_value=('low',)):
            report = self.report()
        self.assertIsNotNone(report)
        self.assertTrue(report['summary'])
        self.assertEqual(len(report['findings']), 8)
        self.assertEqual(report['findings'], self.published['findings'])
        self.assertEqual(report['evidence'], self.published['evidence'])
        self.assertEqual(report['coverage']['total'], 495)
        self.assertEqual(report['coverage']['analyzed'], 495)
        self.assertFalse(report['stale'])
        self.assertEqual(metadata['bundled_report_run_id'], report['run_id'])
        self.assertEqual(metadata['report_summary']['json_sha256'], self.summary['json_sha256'])
        self.assertEqual(report['source_run_id'], self.published['run_id'])
        self.assertEqual(self.content_digests(), before)
        self.assert_no_work_scheduled()
        with database(self.target) as connection:
            run = dict(connection.execute('SELECT * FROM agent_runs').fetchone())
        self.assertEqual(run['status'], 'completed')
        self.assertEqual((run['total'], run['selected'], run['processed'], run['initial_processed']),
                         (495, 495, 495, 495))
        self.assertEqual(run['snapshot_json'], '[]')
        self.assertEqual(run['finished_at'], self.published['generated_at'])

    def test_fresh_install_contains_the_same_readable_completed_report(self):
        target = self.work / 'fresh' / 'reviews.sqlite3'
        metadata = ensure_bundled_snapshot(target, BUNDLE)
        report = self.report(target)
        self.assertEqual(metadata['review_count'], 4076)
        self.assertEqual(report['findings'], self.published['findings'])
        self.assertFalse(report['stale'])
        self.assertEqual(self.content_digests(target), self.content_digests())
        self.assert_no_work_scheduled(target)

    def test_repeated_and_parallel_upgrades_insert_only_one_report(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            metadata = list(pool.map(lambda _: ensure_bundled_snapshot(self.target, BUNDLE), range(2)))
        again = ensure_bundled_snapshot(self.target, BUNDLE)
        self.assertEqual(metadata[0]['bundled_report_run_id'], metadata[1]['bundled_report_run_id'])
        self.assertEqual(again['bundled_report_run_id'], metadata[0]['bundled_report_run_id'])
        with database(self.target) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM store_meta WHERE key LIKE 'bundled_report:%'").fetchone()[0], 1)
        self.assert_no_work_scheduled()

    def test_existing_run_id_six_is_preserved_and_report_gets_a_new_id(self):
        with database(self.target) as connection:
            connection.execute('''INSERT INTO agent_runs
                (id,app_id,country,start_at,end_at,model,reasoning_effort,prompt_version,scope_hash,
                 status,stage,total,selected,processed,max_reviews,snapshot_json,created_at,report_json)
                VALUES(6,'local-game','us','2026-01-01','2026-01-02','local-model','low','local-v1',
                 'local-hash','completed','本地已有报告',1,1,1,10,'[]','2026-01-02','{"local":true}')''')
            before = dict(connection.execute('SELECT * FROM agent_runs WHERE id=6').fetchone())
        ensure_bundled_snapshot(self.target, BUNDLE)
        report = self.report()
        self.assertNotEqual(report['run_id'], 6)
        self.assertEqual(report['source_run_id'], 6)
        with database(self.target) as connection:
            self.assertEqual(dict(connection.execute('SELECT * FROM agent_runs WHERE id=6').fetchone()), before)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0], 2)
        self.assert_no_work_scheduled()

    def test_different_snapshot_is_not_given_this_report(self):
        other = dict(self.old_metadata, snapshot_id='another-published-snapshot')
        with database(self.target) as connection:
            connection.execute("UPDATE store_meta SET value=? WHERE key='bundled_snapshot'",
                               (json.dumps(other),))
        before = self.content_digests()
        self.assertEqual(ensure_bundled_snapshot(self.target, BUNDLE), other)
        # Caller metadata alone is not sufficient: the actual DB provenance is checked too.
        restore_completed_report(self.target, BUNDLE, self.old_metadata)
        self.assertEqual(snapshot_info(self.target), other)
        self.assertEqual(self.content_digests(), before)
        self.assert_no_report_inserted()

    def test_ordinary_user_database_is_not_given_bundled_report(self):
        with database(self.target) as connection:
            connection.execute("DELETE FROM store_meta WHERE key='bundled_snapshot'")
        before = self.content_digests()
        self.assertEqual(ensure_bundled_snapshot(self.target, BUNDLE), {})
        restore_completed_report(self.target, BUNDLE, self.old_metadata)
        self.assertEqual(snapshot_info(self.target), {})
        self.assertEqual(self.content_digests(), before)
        self.assert_no_report_inserted()

    def test_missing_local_analysis_does_not_create_a_false_completed_run(self):
        with database(self.target) as connection:
            connection.execute('DELETE FROM agent_review_results WHERE rowid IN '
                               '(SELECT rowid FROM agent_review_results LIMIT 1)')
        before = self.content_digests()
        self.assertEqual(ensure_bundled_snapshot(self.target, BUNDLE), self.old_metadata)
        self.assertIsNone(self.report())
        self.assertEqual(self.content_digests(), before)
        self.assert_no_report_inserted()

    def test_review_changed_before_upgrade_does_not_gain_a_false_completed_run(self):
        review = self.scoped_review()
        review.content += '\n更新：升级以前已经修改。'
        self.assertEqual(save_reviews(self.target, [review]).updated, 1)
        before = self.content_digests()
        self.assertEqual(ensure_bundled_snapshot(self.target, BUNDLE), self.old_metadata)
        self.assertIsNone(self.report())
        self.assertEqual(self.content_digests(), before)
        self.assert_no_report_inserted()

    def test_active_job_is_preserved_and_upgrade_waits_until_it_is_terminal(self):
        with database(self.target) as connection:
            connection.execute('''INSERT INTO agent_runs
                (id,app_id,country,start_at,end_at,model,reasoning_effort,prompt_version,scope_hash,
                 status,stage,total,selected,processed,max_reviews,snapshot_json,created_at)
                VALUES(6,?,?,?,?,?,?,'local-v1','local-hash','running','现有任务',1,1,0,10,'[]',
                 '2026-09-24T01:00:00+00:00')''',
                (self.summary['app_id'], self.summary['country'], self.summary['start_at'],
                 self.summary['end_at'], self.summary['model'], 'medium'))
            active_before = dict(connection.execute('SELECT * FROM agent_runs WHERE id=6').fetchone())
        self.assertEqual(ensure_bundled_snapshot(self.target, BUNDLE), self.old_metadata)
        self.assertIsNone(self.report())
        with database(self.target) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0], 1)
            self.assertEqual(dict(connection.execute('SELECT * FROM agent_runs WHERE id=6').fetchone()),
                             active_before)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM store_meta WHERE key LIKE 'bundled_report:%'").fetchone()[0], 0)
            connection.execute("UPDATE agent_runs SET status='failed' WHERE id=6")
            terminal_before = dict(connection.execute('SELECT * FROM agent_runs WHERE id=6').fetchone())
        ensure_bundled_snapshot(self.target, BUNDLE)
        report = self.report()
        self.assertIsNotNone(report)
        self.assertNotEqual(report['run_id'], 6)
        with database(self.target) as connection:
            self.assertEqual(dict(connection.execute('SELECT * FROM agent_runs WHERE id=6').fetchone()),
                             terminal_before)
        self.assert_no_work_scheduled()

    def test_changed_review_marks_restored_report_stale(self):
        ensure_bundled_snapshot(self.target, BUNDLE)
        review = self.scoped_review()
        review.content += '\n更新：此评论已由测试修改。'
        self.assertEqual(save_reviews(self.target, [review]).updated, 1)
        report = self.report()
        self.assertTrue(report['stale'])
        self.assertIn('评论或人工复核已变化', report['stale_reason'])
        self.assertEqual(report['findings'], self.published['findings'])
        self.assert_no_work_scheduled()

    def test_added_review_marks_restored_report_stale(self):
        ensure_bundled_snapshot(self.target, BUNDLE)
        review = self.scoped_review()
        review.external_id = 'bundled-report-new-local-review'
        review.content = '本地新增评论：更新之后登录异常，请修复。'
        self.assertEqual(save_reviews(self.target, [review]).inserted, 1)
        self.assertTrue(self.report()['stale'])
        self.assert_no_work_scheduled()

    def test_corrupt_hash_or_json_never_inserts_a_run(self):
        folder = self.changed_bundle()
        before = self.content_digests()
        (folder / 'agent-report.json').write_bytes(b'{broken')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            ensure_bundled_snapshot(self.target, folder)
        metadata = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        metadata['report_summary']['json_sha256'] = hashlib.sha256(b'{broken').hexdigest()
        (folder / 'manifest.json').write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            ensure_bundled_snapshot(self.target, folder)
        self.assertEqual(self.content_digests(), before)
        self.assertEqual(snapshot_info(self.target), self.old_metadata)
        self.assert_no_report_inserted()

    def test_tampered_scope_analysis_or_evidence_is_rejected_even_with_matching_file_hash(self):
        folder = self.changed_bundle()
        before = self.content_digests()
        for kind in ('scope_hash', 'analysis_hash', 'evidence'):
            with self.subTest(kind=kind):
                report = json.loads(json.dumps(self.published))
                if kind == 'evidence':
                    report['evidence'][0]['content'] = 'This is not the published review text.'
                else:
                    report['coverage'][kind] = '0' * 64
                self.write_changed_report(folder, report)
                with self.assertRaises(ValueError):
                    ensure_bundled_snapshot(self.target, folder)
                self.assertEqual(self.content_digests(), before)
                self.assertEqual(snapshot_info(self.target), self.old_metadata)
                self.assert_no_report_inserted()


if __name__ == '__main__':
    unittest.main()
