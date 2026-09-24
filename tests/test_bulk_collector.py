import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from collectors.app_store import AppStoreCollector
from collectors.app_store_bulk import collect_batch
from collectors.app_store_public import ROWS_URL
from collectors.base import CollectionResult, RawReview


def rows(count=1500):
    origin = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    return [RawReview(platform='App Store', external_id=str(index + 1),
        date=(origin - timedelta(minutes=index)).isoformat(), author='玩家',
        title='体验反馈', content=f'评论 {index + 1}', rating=3, app_id='111', country='cn')
        for index in range(count)]


class BulkCollectorTests(unittest.TestCase):
    def setUp(self):
        self.collector = SimpleNamespace(platform='App Store', timeout=1)
        self.data = rows()
        self.saved = []
        self.requests = []
        self.fail_page = None
        self.repeat_page = None
        self.context = patch('collectors.app_store_bulk.public_context', return_value=(
            {'headers': {}, 'total_reported': len(self.data)}, ''))
        self.page = patch('collectors.app_store_bulk.public_page', side_effect=self.read)
        self.context.start()
        self.page.start()
        self.addCleanup(self.context.stop)
        self.addCleanup(self.page.stop)

    def read(self, collector, app_id, country, page, context, meta, attempts):
        self.requests.append(page)
        meta['request_attempts'] += 1
        attempts.append({'http_status': 200, 'count': 0})
        if page == self.fail_page:
            attempts[-1] = {'error': 'Timeout', 'count': 0}
            return {}, [], False, '公开列表读取失败：Timeout'
        wanted = page - 1 if page == self.repeat_page else page
        selected = self.data[(wanted - 1) * 50:wanted * 50]
        attempts[-1]['count'] = len(selected)
        return {'feed': {'entry': [{'id': row.external_id} for row in selected]}}, selected, len(selected) == 50, ''

    def collect(self, **kwargs):
        def save(page_rows, metadata):
            self.saved.append(([row.external_id for row in page_rows], copy.deepcopy(metadata)))
        options = {'max_pages': 2000, 'page_budget': 20, 'delay_seconds': 0, 'on_page': save}
        options.update(kwargs)
        return collect_batch(self.collector, '111', 'cn', **options)

    def test_public_traversal_continues_beyond_ten_pages_and_streams_without_bodies_in_result(self):
        first = self.collect(max_pages=25)
        self.assertEqual(self.requests, list(range(1, 21)))
        self.assertEqual(first.reviews, [])
        self.assertFalse(first.metadata['done'])
        self.assertEqual(first.metadata['matched_count'], 1000)
        self.assertEqual(first.metadata['checkpoint']['next_page'], 21)
        self.assertEqual(len(self.saved), 20)
        self.assertEqual(self.saved[-1][1]['checkpoint']['next_page'], 21)
        second = self.collect(max_pages=25, checkpoint=first.metadata['checkpoint'])
        self.assertEqual(self.requests[-7:], list(range(19, 26)))
        self.assertEqual(second.requested, 7)
        self.assertTrue(second.metadata['done'])
        self.assertEqual(second.stop_reason, '页数上限')
        self.assertEqual(second.metadata['matched_count'], 1250)
        self.assertEqual(second.metadata['replay_read_count'], 100)
        self.assertTrue(second.metadata['sequence_verified'])

    def test_transient_error_preserves_failed_page_and_committed_counts(self):
        self.fail_page = 3
        first = self.collect()
        self.assertFalse(first.metadata['done'])
        self.assertTrue(first.metadata['retryable'])
        self.assertEqual(first.metadata['checkpoint']['next_page'], 3)
        self.assertEqual(first.metadata['matched_count'], 100)
        self.assertEqual(len(self.saved), 2)
        self.fail_page = None
        second = self.collect(max_pages=4, checkpoint=first.metadata['checkpoint'])
        self.assertEqual(self.requests[3:], [1, 2, 3, 4])
        self.assertEqual(second.metadata['matched_count'], 200)
        self.assertTrue(second.metadata['sequence_verified'])

    def test_storage_failure_does_not_advance_or_lose_page(self):
        def save(page_rows, meta):
            if meta['checkpoint']['next_page'] == 3:
                raise OSError('database busy')
            self.saved.append(page_rows)
        result = self.collect(on_page=save)
        self.assertEqual(result.metadata['checkpoint']['next_page'], 2)
        self.assertEqual(result.metadata['matched_count'], 50)
        self.assertTrue(result.metadata['retryable'])

    def test_date_boundaries_filter_exactly_and_stop_only_after_verified_start(self):
        start = self.data[74].date
        end = self.data[10].date
        result = self.collect(start_at=start, end_at=end)
        self.assertEqual(self.requests, [1, 2])
        self.assertEqual(result.metadata['matched_count'], 64)
        self.assertTrue(result.metadata['boundary_reached'])
        self.assertEqual(result.metadata['coverage_start'], start)
        self.assertEqual(result.metadata['coverage_end'], end)
        self.assertEqual(self.saved[0][0][0], '12')
        self.assertEqual(self.saved[1][0][-1], '75')

    def test_repeated_page_terminates_without_continuity_claim(self):
        self.repeat_page = 3
        result = self.collect()
        self.assertEqual(result.stop_reason, '重复页面')
        self.assertTrue(result.metadata['done'])
        self.assertFalse(result.metadata['sequence_verified'])
        self.assertIsNone(result.metadata['coverage_start'])
        self.assertEqual(result.metadata['matched_count'], 100)

    def test_resume_rescans_two_pages_and_handles_small_offset_drift(self):
        first = self.collect(max_pages=23)
        inserted = copy.deepcopy(self.data[:10])
        for index, row in enumerate(inserted):
            row.external_id = f'new-{index}'
        self.data = inserted + self.data
        second = self.collect(max_pages=23, checkpoint=first.metadata['checkpoint'])
        self.assertTrue(second.metadata['sequence_verified'])
        self.assertIn('找到旧末页', second.metadata['resume_status'])
        self.assertEqual(second.metadata['matched_count'], 1140)
        self.assertEqual(second.metadata['overlap_count'], 10)
        self.assertEqual(second.metadata['quality_issues'], [])

    def test_large_offset_drift_without_old_tail_never_certifies_coverage(self):
        first = self.collect(max_pages=23)
        changed = copy.deepcopy(self.data)
        for row in changed:
            row.external_id = 'changed-' + row.external_id
        self.data = changed
        second = self.collect(max_pages=23, checkpoint=first.metadata['checkpoint'])
        self.assertFalse(second.metadata['sequence_verified'])
        self.assertIsNone(second.metadata['coverage_start'])
        self.assertIn('未找到', second.metadata['resume_status'])

    def test_unknown_or_disordered_dates_do_not_claim_boundary(self):
        for bad in [None, self.data[0].date]:
            with self.subTest(date=bad):
                self.data = rows(100)
                self.data[55].date = bad
                result = self.collect(start_at=self.data[60].date, max_pages=2)
                self.assertFalse(result.metadata['sequence_verified'])
                self.assertFalse(result.metadata['boundary_reached'])
                self.assertIsNone(result.metadata['coverage_start'])

    def test_verified_empty_deep_page_is_visible_end(self):
        self.data = rows(100)
        result = self.collect()
        self.assertEqual(result.stop_reason, '公开源末页')
        self.assertEqual(result.requested, 3)
        self.assertTrue(result.metadata['done'])
        self.assertTrue(result.metadata['source_exhausted'])
        self.assertTrue(result.metadata['checkpoint']['source_exhausted'])
        self.assertTrue(result.metadata['sequence_verified'])
        self.assertEqual(result.metadata['matched_count'], 100)

    def test_pause_preserves_cursor_and_small_slices_still_make_progress(self):
        first = self.collect(page_budget=1)
        self.assertEqual(first.metadata['checkpoint']['next_page'], 2)
        paused = self.collect(checkpoint=first.metadata['checkpoint'], should_stop=lambda: True)
        self.assertEqual(paused.requested, 0)
        self.assertEqual(paused.metadata['checkpoint']['next_page'], 2)
        second = self.collect(page_budget=1, checkpoint=first.metadata['checkpoint'])
        self.assertGreater(second.metadata['checkpoint']['next_page'], 2)

    def test_rss_fallback_has_explicit_ten_page_limit(self):
        self.context.stop()
        def rss(collector, app_id, country, page, advertised, meta):
            meta['request_attempts'] += 1
            self.requests.append(page)
            payload = {'feed': {'link': [{'attributes': {'rel': 'next', 'href':
                f'https://itunes.apple.com/cn/rss/customerreviews/page={page + 1}/id=111/sortby=mostrecent/json'}}]}}
            return payload, self.data[(page - 1) * 50:page * 50], [{'http_status': 200}], ''
        with patch('collectors.app_store_bulk.public_context', return_value=(None, '公开列表不可用')), \
                patch('collectors.app_store_bulk.read_page', side_effect=rss):
            result = self.collect()
        self.assertEqual(self.requests, list(range(1, 11)))
        self.assertEqual(result.stop_reason, 'RSS 页数上限')
        self.assertEqual(result.metadata['source_page_limit'], 10)
        self.assertEqual(result.metadata['source_kind'], 'public_rss')
        self.assertTrue(result.metadata['done'])

    def test_429_never_switches_source_or_advances_cursor(self):
        def limited(collector, app_id, country, page, context, meta, attempts):
            meta['request_attempts'] += 1
            meta['retry_after_seconds'] = 120
            attempts.append({'http_status': 429})
            return {}, [], False, '公开源限流'
        with patch('collectors.app_store_bulk.public_page', side_effect=limited), \
                patch('collectors.app_store_bulk.read_page') as rss:
            result = self.collect()
        rss.assert_not_called()
        self.assertFalse(result.metadata['done'])
        self.assertTrue(result.metadata['retryable'])
        self.assertEqual(result.metadata['retry_after_seconds'], 120)
        self.assertEqual(result.metadata['checkpoint']['next_page'], 1)

    def test_deep_page_failure_cannot_fallback_to_rss_or_skip_a_page(self):
        self.fail_page = 12
        with patch('collectors.app_store_bulk.read_page') as rss:
            result = self.collect()
        rss.assert_not_called()
        self.assertEqual(result.metadata['checkpoint']['next_page'], 12)
        self.assertEqual(result.metadata['matched_count'], 550)
        self.assertEqual(self.requests, list(range(1, 13)))

    def test_checkpoint_json_size_is_bounded_and_does_not_embed_review_text(self):
        import json
        self.data = rows(350 * 50)
        result = self.collect(max_pages=320, page_budget=200)
        result = self.collect(max_pages=320, page_budget=200, checkpoint=result.metadata['checkpoint'])
        checkpoint = result.metadata['checkpoint']
        self.assertTrue(result.metadata['done'])
        self.assertEqual(result.metadata['matched_count'], 16_000)
        self.assertLessEqual(len(checkpoint['_fingerprints']), 256)
        self.assertLessEqual(sum(len(anchor['ids']) for anchor in checkpoint['_anchors']), 100)
        encoded = json.dumps(checkpoint, ensure_ascii=False)
        self.assertNotIn('体验反馈', encoded)
        self.assertLess(len(encoded), 25_000)

    def test_context_429_preserves_retry_after_without_fallback(self):
        self.context.stop()
        response = Mock(status_code=429, headers={'Retry-After': '180'})
        collector = AppStoreCollector.__new__(AppStoreCollector)
        collector.session = Mock()
        collector.session.get.return_value = response
        collector.timeout = 1
        with patch('collectors.app_store_bulk.read_page') as rss:
            result = collect_batch(collector, '111', 'cn', on_page=Mock())
        rss.assert_not_called()
        self.assertEqual(collector.session.get.call_count, 1)
        self.assertEqual(result.metadata['retry_after_seconds'], 180)
        self.assertTrue(result.metadata['retryable'])

    def test_invalid_dates_or_missing_sink_make_no_requests(self):
        result = self.collect(start_at='2026-09-20', end_at='2026-09-21')
        self.assertEqual(self.requests, [])
        self.assertTrue(result.errors)
        result = self.collect(on_page=None)
        self.assertEqual(self.requests, [])
        self.assertTrue(result.errors)

    def test_matched_dates_exclude_scanned_rows_after_requested_end(self):
        result = self.collect(end_at=self.data[10].date, max_pages=2)
        self.assertEqual(result.metadata['newest_date'], self.data[0].date)
        self.assertEqual(result.metadata['newest_matched_date'], self.data[11].date)
        self.assertEqual(result.metadata['oldest_matched_date'], self.data[99].date)
        result = self.collect(end_at='2020-01-01T00:00:00Z', max_pages=2)
        self.assertIsNone(result.metadata['newest_matched_date'])
        self.assertEqual(result.metadata['matched_count'], 0)

    def test_future_end_is_frozen_at_first_slice_even_after_resume(self):
        first_now = '2026-09-20T12:00:01+00:00'
        with patch('collectors.app_store_bulk.CollectionResult', side_effect=lambda **kwargs:
                CollectionResult(**kwargs, started_at=first_now)):
            first = self.collect(page_budget=1, end_at='2099-01-01T00:00:00Z')
        self.assertEqual(first.metadata['requested_end'], first_now)
        newer = copy.deepcopy(self.data[0])
        newer.external_id = 'new-after-start'
        newer.date = '2026-09-20T12:01:00+00:00'
        self.data.insert(0, newer)
        self.saved.clear()
        with patch('collectors.app_store_bulk.CollectionResult', side_effect=lambda **kwargs:
                CollectionResult(**kwargs, started_at='2026-09-20T13:00:00+00:00')):
            second = self.collect(page_budget=3, checkpoint=first.metadata['checkpoint'])
        self.assertEqual(second.metadata['requested_end'], first_now)
        self.assertEqual(second.metadata['newest_matched_date'], '2026-09-20T12:00:00+00:00')
        self.assertTrue(second.metadata['sequence_verified'])
        self.assertNotIn('new-after-start', [identity for page, _ in self.saved for identity in page])

    def test_unexpected_source_overlap_records_page_count_and_survives_resume(self):
        # Repeat the previous page's last row at the next page's first position,
        # matching the boundary-overlap pattern seen in the isolated live run.
        self.data[150] = copy.deepcopy(self.data[149])
        self.data[550] = copy.deepcopy(self.data[549])
        first = self.collect(max_pages=25)
        self.assertEqual(first.metadata['matched_count'], 998)
        self.assertEqual(first.metadata['overlap_count'], 2)
        issues = first.metadata['quality_issues']
        self.assertEqual([(item['page'], item['count'], item['code']) for item in issues],
            [(4, 1, 'unexpected_page_overlap'), (12, 1, 'unexpected_page_overlap')])
        second = self.collect(max_pages=25, checkpoint=first.metadata['checkpoint'])
        self.assertEqual(second.metadata['quality_issues'], issues)
        self.assertEqual(second.metadata['replay_read_count'], 100)
        self.assertEqual(second.metadata['overlap_count'], 2)
        self.assertTrue(any('第 4 页' in item for item in second.warnings))
        self.assertTrue(any('第 12 页' in item for item in second.warnings))
        self.assertFalse(second.metadata['sequence_verified'])

    def test_new_unexpected_overlap_after_resume_is_still_reported(self):
        first = self.collect(max_pages=25)
        self.data[1050] = copy.deepcopy(self.data[1049])
        second = self.collect(max_pages=25, checkpoint=first.metadata['checkpoint'])
        self.assertFalse(second.metadata['sequence_verified'])
        self.assertEqual(second.metadata['quality_issues'][0]['page'], 22)
        self.assertEqual(second.metadata['quality_issues'][0]['count'], 1)

    def test_quality_diagnostics_are_bounded_and_include_date_problem_kind(self):
        self.data = rows(50 * 60)
        for index in range(0, len(self.data), 50):
            self.data[index].date = None
        result = self.collect(max_pages=60, page_budget=100)
        self.assertEqual(result.metadata['quality_issue_count'], 60)
        self.assertEqual(result.metadata['quality_issues_truncated'], 20)
        self.assertEqual(len(result.metadata['quality_issues']), 40)
        self.assertTrue(all(issue['code'] == 'missing_date' and issue['count'] == 1
            for issue in result.metadata['quality_issues']))
        self.assertFalse(result.metadata['sequence_verified'])

    def test_date_range_stops_after_two_older_pages_despite_source_overlap(self):
        self.data[150] = copy.deepcopy(self.data[149])
        start = self.data[220].date
        result = self.collect(start_at=start, max_pages=20_000)
        self.assertEqual(self.requests, list(range(1, 8)))
        self.assertTrue(result.metadata['done'])
        self.assertTrue(result.metadata['target_date_observed'])
        self.assertTrue(result.metadata['chronology_verified'])
        self.assertFalse(result.metadata['boundary_reached'])
        self.assertFalse(result.metadata['sequence_verified'])
        self.assertEqual(result.stop_reason, '已读到起始日期之前，连续性待确认')
        self.assertEqual(result.metadata['matched_count'], 220)
        self.assertTrue(any('连续两页' in warning for warning in result.warnings))
        self.assertTrue(all(row['done'] for _, row in self.saved[-1:]))

    def test_date_stop_counter_ignores_replay_pages_and_survives_batches(self):
        self.data[150] = copy.deepcopy(self.data[149])
        start = self.data[920].date
        first = self.collect(start_at=start)
        self.assertFalse(first.metadata['done'])
        self.assertEqual(first.metadata['checkpoint']['_consecutive_old_pages'], 1)
        second = self.collect(start_at=start, checkpoint=first.metadata['checkpoint'])
        self.assertEqual(self.requests[-3:], [19, 20, 21])
        self.assertTrue(second.metadata['done'])
        self.assertFalse(second.metadata['boundary_reached'])
        self.assertEqual(second.stop_reason, '已读到起始日期之前，连续性待确认')
        self.assertEqual(second.metadata['checkpoint']['next_page'], 22)

    def test_date_range_with_unknown_or_disordered_dates_stops_for_review(self):
        for bad_date in [None, self.data[0].date]:
            with self.subTest(date=bad_date):
                self.data = rows()
                self.requests.clear()
                self.data[60].date = bad_date
                result = self.collect(start_at=self.data[120].date, max_pages=20_000)
                self.assertEqual(self.requests, [1, 2])
                self.assertTrue(result.metadata['done'])
                self.assertFalse(result.metadata['chronology_verified'])
                self.assertFalse(result.metadata['boundary_reached'])
                self.assertEqual(result.stop_reason, '评论日期异常，需复核后继续')
                self.assertTrue(any('需复核' in warning for warning in result.warnings))


class BulkTransportTests(unittest.TestCase):
    def test_raw_http_payloads_traverse_1100_comments_and_resume_without_gaps(self):
        all_rows = rows(1100)
        collector = AppStoreCollector.__new__(AppStoreCollector)
        collector.timeout = 1
        collector.session = Mock()

        def response(value):
            result = Mock(status_code=200, headers={})
            result.json.return_value = value
            return result

        def get(url, *, params=None, **kwargs):
            if url != ROWS_URL:
                return response({'adamId': 111,
                    'writeUserReviewUrl': 'https://userpub.itunes.apple.com/writeUserReview?cc=cn',
                    'kindId': 11, 'userReviewsRowUrl': ROWS_URL,
                    'userReviewsSortOptions': [{'sortId': 4}], 'totalNumberOfReviews': len(all_rows)})
            self.assertEqual(params['id'], '111')
            self.assertEqual(params['cc'], 'cn')
            self.assertEqual(params['sort'], '4')
            selected = all_rows[params['startIndex']:params['endIndex']]
            return response({'userReviewList': [{'userReviewId': item.external_id,
                'body': item.content, 'date': item.date, 'name': item.author, 'rating': item.rating,
                'title': item.title, 'viewUsersUserReviewsUrl': 'https://itunes.apple.com/cn/reviews?userProfileId=1'}
                for item in selected]})

        collector.session.get.side_effect = get
        stored = {}

        def sink(page_rows, meta):
            stored.update({row.external_id: row for row in page_rows})

        first = collect_batch(collector, '111', 'cn', delay_seconds=0, on_page=sink)
        self.assertFalse(first.metadata['done'])
        second = collect_batch(collector, '111', 'cn', delay_seconds=0,
            on_page=sink, checkpoint=first.metadata['checkpoint'])
        self.assertTrue(second.metadata['done'])
        self.assertTrue(second.metadata['source_exhausted'])
        self.assertTrue(second.metadata['sequence_verified'])
        self.assertEqual(second.metadata['matched_count'], 1100)
        self.assertEqual(second.metadata['request_attempts'], 26)
        self.assertEqual(set(stored), {row.external_id for row in all_rows})
        self.assertTrue(all(row.data_source == 'app_store_public_review_list' for row in stored.values()))
        requests = [call.kwargs['params']['startIndex'] for call in collector.session.get.call_args_list
            if call.args[0] == ROWS_URL]
        self.assertEqual(requests, list(range(0, 1000, 50)) + list(range(900, 1150, 50)))

    def test_transport_does_not_implicitly_retry_429_with_retry_after(self):
        collector = AppStoreCollector(log_dir=Mock())
        self.addCleanup(collector.session.close)
        retry = collector.session.get_adapter('https://itunes.apple.com').max_retries
        self.assertFalse(retry.is_retry('GET', 429, has_retry_after=True))
        self.assertFalse(retry.is_retry('GET', 429, has_retry_after=False))
        self.assertTrue(retry.is_retry('GET', 503, has_retry_after=True))


if __name__ == '__main__':
    unittest.main()
