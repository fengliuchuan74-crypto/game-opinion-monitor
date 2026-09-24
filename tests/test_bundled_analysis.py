"""The published Agent dashboard is readable independently of local app state."""
from __future__ import annotations

from contextlib import ExitStack
import gzip
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from modules.bundled_analysis import load_bundled_analysis
from modules.dashboard import sentiments
from modules.version_attribution import attribute_versions


BUNDLE = Path(__file__).resolve().parents[1] / 'bundled_data'


class BundledAnalysisTests(unittest.TestCase):
    def setUp(self):
        guards = ExitStack()
        self.addCleanup(guards.close)
        failure = AssertionError('Historical bundle view must not access a local DB, model configuration or network')
        for name in ('sqlite3.connect', 'requests.sessions.Session.request', 'socket.create_connection',
                     'modules.codex_runner.run_codex', 'modules.codex_runner.configured_model',
                     'modules.codex_runner.configured_reasoning_effort',
                     'modules.codex_runner.available_reasoning_efforts',
                     'modules.agent_analysis._settings', 'modules.version_history.get_version_history'):
            guards.enter_context(patch(name, side_effect=failure))

    def test_actual_bundle_has_complete_fixed_dashboard_and_all_report_evidence(self):
        result = load_bundled_analysis(BUNDLE)
        view, profile = result['view'], result['profile']
        report, current = view['agent_report'], view['current']
        published = json.loads((BUNDLE / 'agent-report.json').read_text(encoding='utf-8'))
        self.assertEqual(result['metadata']['review_count'], 4076)
        self.assertEqual(result['metadata']['agent_result_count'], 495)
        self.assertEqual((profile['app_id'], profile['country']), ('1618911882', 'cn'))
        self.assertEqual(view['analysis_mode'], 'Agent分析')
        self.assertEqual(view['metrics']['total'], 495)
        self.assertEqual(len(current), 495)
        self.assertTrue(current['agent_analyzed'].all())
        self.assertTrue(current['analysis_source'].eq('Agent').all())
        self.assertTrue(current['agent_model'].eq(published['model']).all())
        self.assertEqual(len(report['findings']), 8)
        self.assertEqual(len(report['evidence']), 87)
        self.assertEqual(report['findings'], published['findings'])
        self.assertEqual(report['evidence'], published['evidence'])
        self.assertEqual(view['start'], pd.Timestamp(published['coverage']['start_at']))
        self.assertEqual(view['end'], pd.Timestamp(published['coverage']['end_at']))
        self.assertTrue(current['date'].ge(view['start']).all())
        self.assertTrue(current['date'].lt(view['end']).all())
        distribution = sentiments(current).set_index('情绪')['评论数'].to_dict()
        self.assertEqual(distribution, {'好评': 27, '中评': 7, '差评': 438, '待复核': 23})
        self.assertFalse(report['stale'])
        self.assertTrue(report['from_bundle'])
        self.assertTrue(view['from_bundle'])
        self.assertTrue(view['bundled_history'])
        self.assertEqual(report['bundled_snapshot_id'], result['metadata']['snapshot_id'])
        self.assertEqual((result['icon']['app_id'], result['icon']['country']), ('1618911882', 'cn'))
        self.assertTrue(result['icon']['data_uri'].startswith('data:image/png;base64,'))
        self.assertEqual(result['regions']['地区'].tolist(), ['CN'])
        self.assertEqual(int(result['regions'].iloc[0]['评论数']), 495)
        self.assertEqual(int(result['regions'].iloc[0]['差评']), 438)

    def test_unanalysed_previous_period_does_not_leak_rule_judgments(self):
        previous = load_bundled_analysis(BUNDLE)['view']['previous']
        self.assertGreater(len(previous), 0)
        self.assertFalse(previous['agent_analyzed'].any())
        self.assertTrue(previous['analysis_source'].eq('待分析').all())
        self.assertTrue(previous['sentiment_label'].eq('待复核').all())
        self.assertTrue(previous['sentiment_score'].isna().all())
        self.assertTrue(previous['issue_category'].eq('未归类/待复核').all())
        self.assertTrue(previous['needs_review'].all())
        self.assertTrue(previous['agent_reason'].eq('').all())
        self.assertIn('rule_sentiment_label', previous)
        self.assertTrue(previous['rating'].between(1, 5).all())

    def test_version_attribution_uses_only_published_history(self):
        result = load_bundled_analysis(BUNDLE)
        payload = json.loads(gzip.decompress((BUNDLE / 'snapshot.json.gz').read_bytes()))
        history = payload['caches']['app_versions']['1618911882_cn.json']
        current = result['view']['current']
        expected = attribute_versions(current, history)
        for name in ('source_version', 'inferred_version', 'version_basis', 'version_display', 'version_reason'):
            pd.testing.assert_series_equal(current[name], expected[name])
        self.assertTrue(current['version_basis'].isin(['source', 'date_inferred']).any())

    def test_reading_again_does_not_reuse_mutated_in_memory_dashboard(self):
        first = load_bundled_analysis(BUNDLE)
        first['view']['agent_report']['findings'].clear()
        first['view']['current'].loc[:, 'sentiment_label'] = '中评'
        second = load_bundled_analysis(BUNDLE)
        self.assertEqual(len(second['view']['agent_report']['findings']), 8)
        self.assertEqual(int(second['view']['current']['sentiment_label'].eq('差评').sum()), 438)

    def test_invalid_report_digest_is_rejected_with_clear_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ('manifest.json', 'snapshot.json.gz', 'agent-report.json'):
                shutil.copyfile(BUNDLE / name, folder / name)
            (folder / 'agent-report.json').write_bytes(b'{}')
            with self.assertRaisesRegex(ValueError, '随附 Agent 完整报告校验失败'):
                load_bundled_analysis(folder)

    def test_missing_bundle_is_reported_as_unavailable_not_a_database_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, '随附历史分析文件缺失或格式无效'):
                load_bundled_analysis(Path(temporary))


if __name__ == '__main__':
    unittest.main()
