import unittest
from copy import deepcopy

import pandas as pd

from modules.version_attribution import attribute_versions, review_version_text


class VersionAttributionTests(unittest.TestCase):
    def setUp(self):
        self.history = {'app_id': '111', 'country': 'cn', 'checked_at': '2026-09-24T10:00:00Z',
                        'releases': [{'version': '5.0', 'released_at': '2026-09-10T10:00:00Z', 'precision': 'timestamp'},
                                     {'version': '6.0', 'released_at': '2026-09-20T10:00:00Z', 'precision': 'timestamp'}]}

    def classify(self, dates, **extra):
        data = pd.DataFrame([{'app_id': '111', 'country': 'cn', 'topic': '', 'date': date, **extra}
                             for date in dates])
        return attribute_versions(data, self.history)

    def test_timestamp_intervals_are_left_closed_and_right_open(self):
        rows = self.classify(['2026-09-10T09:59:59Z', '2026-09-10T10:00:00Z',
                              '2026-09-20T09:59:59Z', '2026-09-20T10:00:00Z'])
        self.assertEqual(rows.inferred_version.tolist(), ['', '5.0', '5.0', '6.0'])

    def test_day_precision_blocks_entire_release_day_in_release_timezone(self):
        self.history['releases'][1].update(released_at='2026-09-20T18:00:00+08:00', precision='day')
        rows = self.classify(['2026-09-19T15:59:59Z', '2026-09-19T16:00:00Z',
                              '2026-09-20T15:59:59Z', '2026-09-20T16:00:00Z'])
        self.assertEqual(rows.inferred_version.tolist(), ['5.0', '', '', '6.0'])

    def test_source_always_wins_even_if_history_region_or_date_is_invalid(self):
        for topic in ['version:4.9', 'VER：4.9', '版本: 4.9']:
            with self.subTest(topic=topic):
                row = self.classify([None], topic=topic, country='us').iloc[0]
                self.assertEqual(row.source_version, '4.9')
                self.assertEqual(row.inferred_version, '')
                self.assertEqual(row.version_basis, 'source')
                self.assertEqual(row.version_display, '4.9')
                self.assertEqual(review_version_text(row), '版本 4.9（来源提供）')

    def test_inference_never_changes_input_topic_hash_or_index(self):
        data = pd.DataFrame([{'app_id': '111', 'country': 'cn', 'topic': '',
                              'date': '2026-09-22T00:00:00Z', 'content_hash': 'original'}], index=[92])
        before = data.copy(deep=True)
        result = attribute_versions(data, self.history)
        pd.testing.assert_frame_equal(data, before)
        pd.testing.assert_frame_equal(result[data.columns], before)
        self.assertEqual(result.iloc[0].version_basis, 'date_inferred')
        self.assertEqual(result.iloc[0].version_display, '6.0')
        self.assertEqual(review_version_text(result.iloc[0]), '版本 6.0（按日期推定）')

    def test_region_and_app_must_match(self):
        for extra in [{'country': 'us'}, {'app_id': '222'}, {'app_id': None}, {'country': None}]:
            with self.subTest(extra=extra):
                row = self.classify(['2026-09-22T00:00:00Z'], **extra).iloc[0]
                self.assertEqual(row.version_basis, 'unknown')
                self.assertEqual(row.inferred_version, '')

    def test_no_inference_after_observation_and_future_releases_are_ignored(self):
        self.history['releases'].append({'version': '7.0', 'released_at': '2026-09-25T00:00:00Z', 'precision': 'day'})
        rows = self.classify(['2026-09-24T10:00:00Z', '2026-09-24T10:00:01Z', '2026-09-25T12:00:00Z'])
        self.assertEqual(rows.inferred_version.tolist(), ['6.0', '', ''])

    def test_missing_or_naive_comment_dates_are_unknown(self):
        rows = self.classify([None, pd.NaT, '2026-09-22T00:00:00', 'not-a-date'])
        self.assertEqual(rows.version_basis.tolist(), ['unknown'] * 4)

    def test_missing_naive_or_conflicting_history_refuses_inference(self):
        variants = []
        for stamp in [None, '2026-09-20T00:00:00']:
            history = deepcopy(self.history)
            history['releases'][1]['released_at'] = stamp
            variants.append(history)
        for stamp in [None, '2026-09-24T10:00:00']:
            history = deepcopy(self.history)
            history['checked_at'] = stamp
            variants.append(history)
        conflict = deepcopy(self.history)
        conflict['releases'].append({'version': '7.0', 'released_at': '2026-09-20T18:00:00+08:00', 'precision': 'timestamp'})
        variants.append(conflict)
        mixed_precision = deepcopy(conflict)
        mixed_precision['releases'][-1]['precision'] = 'day'
        variants.append(mixed_precision)
        for history in variants:
            with self.subTest(history=history):
                self.history = history
                self.assertEqual(self.classify(['2026-09-22T00:00:00Z']).iloc[0].version_basis, 'unknown')

    def test_duplicate_same_version_and_time_is_safe_and_history_not_mutated(self):
        self.history['releases'].append(dict(self.history['releases'][1]))
        before = deepcopy(self.history)
        self.assertEqual(self.classify(['2026-09-22T00:00:00Z']).iloc[0].inferred_version, '6.0')
        self.assertEqual(self.history, before)

    def test_source_is_retained_with_invalid_history(self):
        self.history['releases'][0]['released_at'] = None
        row = self.classify(['2026-09-22T00:00:00Z'], topic='version:3.0').iloc[0]
        self.assertEqual(row.version_basis, 'source')
        self.assertEqual(row.source_version, '3.0')

    def test_legacy_labels_empty_data_and_nullable_topic(self):
        self.assertEqual(review_version_text({'topic': 'version:6.0.0'}), '版本 6.0.0（来源提供）')
        self.assertEqual(review_version_text({'topic': pd.NA}), '版本未能确定')
        self.assertEqual(review_version_text({}), '版本未能确定')
        empty = attribute_versions(pd.DataFrame(), self.history)
        self.assertTrue(empty.empty)
        self.assertIn('version_display', empty.columns)
