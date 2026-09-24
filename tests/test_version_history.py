import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from modules import version_history as history


APP_URL = 'https://apps.apple.com/cn/app/example/id111'
NOW = datetime(2026, 9, 24, 6, tzinfo=timezone.utc)


def lookup(app_id=111, country='cn', version='2.0'):
    return {'results': [{'trackId': app_id, 'trackViewUrl': f'https://apps.apple.com/{country}/app/example/id{app_id}?uo=4',
        'version': version, 'currentVersionReleaseDate': '2026-09-01T12:00:00Z'}]}


def page(app_id='111', country='cn', items=None):
    if items is None:
        items = [{'primarySubtitle': '2.0', 'secondarySubtitle': 'Tue Sep 01 2026 12:00:00 GMT+0000 (Coordinated Universal Time)'},
                 {'primarySubtitle': '1.0', 'secondarySubtitle': 'Wed Jul 01 2026 08:30:20 GMT+0800 (China Standard Time)'}]
    value = {'data': [{'data': {'canonicalURL': f'https://apps.apple.com/{country}/app/example/id{app_id}',
        'lockup': {'adamId': app_id}, 'pageMetrics': {'pageFields': {'pageId': app_id, 'storeFront': country}},
        'shelfMapping': {'mostRecentVersion': {'seeAllAction': {'pageData': {
            'isIncomplete': False, 'shelves': [{'items': items}],
            'pageMetrics': {'pageFields': {'pageId': app_id}}}}}}}}]}
    return '<meta charset="utf-8"><script id="serialized-server-data" type="application/json">' + json.dumps(value) + '</script>'


def response(data=None, *, html='', status=200, url=APP_URL):
    return SimpleNamespace(status_code=status, url=url, content=html.encode('utf-8'),
                           json=lambda: copy.deepcopy(data))


class VersionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.files = {}
        self.session = Mock()
        self.clock = patch.object(history, '_now', return_value=NOW)
        self.read = patch.object(history, '_read_json', side_effect=lambda p: copy.deepcopy(self.files.get(str(p))))
        self.write = patch.object(history, '_atomic_json', side_effect=lambda p, value: self.files.__setitem__(str(p), copy.deepcopy(value)))
        for helper in (self.clock, self.read, self.write):
            helper.start()
            self.addCleanup(helper.stop)
        self.cache_path = str(Path('cache') / '111_cn.json')

    def get(self, **kwargs):
        return history.get_version_history('111', 'cn', Path('cache'), session=self.session, **kwargs)

    def successful(self, **kwargs):
        self.session.get.side_effect = [response(lookup()), response(html=page())]
        return self.get(allow_fetch=True, **kwargs)

    def test_offline_default_never_requests_and_no_cache_has_no_false_checked_time(self):
        result = self.get()
        self.session.get.assert_not_called()
        self.assertIsNone(result['checked_at'])
        self.assertEqual(result['releases'], [])

    def test_official_history_binding_utc_seconds_and_two_requests(self):
        result = self.successful()
        self.assertEqual([row['version'] for row in result['releases']], ['2.0', '1.0'])
        self.assertEqual(result['releases'][1]['released_at'], '2026-07-01T00:30:20+00:00')
        self.assertEqual(result['releases'][0]['precision'], 'timestamp')
        self.assertFalse(result['history_complete'])
        self.assertEqual(result['error'], '')
        self.assertEqual(self.session.get.call_count, 2)
        for call in self.session.get.call_args_list:
            self.assertEqual(call.kwargs['timeout'], (3, 8))
            self.assertFalse(call.kwargs['allow_redirects'])
        self.session.get.reset_mock()
        self.assertEqual(self.get()['releases'], result['releases'])
        self.session.get.assert_not_called()

    def test_lookup_other_id_or_country_never_requests_page_or_writes_success_cache(self):
        for payload in (lookup(222), lookup(country='us')):
            with self.subTest(payload=payload):
                self.files.clear()
                self.session.get.reset_mock()
                self.session.get.side_effect = [response(payload)]
                result = self.get(allow_fetch=True)
                self.assertEqual(self.session.get.call_count, 1)
                self.assertNotIn(self.cache_path, self.files)
                self.assertEqual(result['releases'], [])

    def test_page_wrong_country_or_lockup_id_cannot_poison_cache(self):
        for html in (page(country='us'), page(app_id='222'),
                     page().replace('"adamId": "111"', '"adamId": "222"')):
            with self.subTest(html=html[:100]):
                self.files.clear()
                self.session.get.side_effect = [response(lookup()), response(html=html)]
                result = self.get(allow_fetch=True)
                self.assertNotIn(self.cache_path, self.files)
                self.assertEqual(result['releases'], [])
                self.assertTrue(result['error'])

    def test_unavailable_history_retains_only_verified_lookup_current_version(self):
        self.session.get.side_effect = [response(lookup()), response(html='<html>游戏页面</html>')]
        result = self.get(allow_fetch=True)
        self.assertEqual([row['version'] for row in result['releases']], ['2.0'])
        self.assertEqual(result['releases'][0]['source_url'].split('?')[0], history.LOOKUP_URL)
        self.assertIn('历史版本暂不可用', result['error'])
        self.assertIn(self.cache_path, self.files)

    def test_failed_refresh_preserves_old_success_snapshot_and_checks_after_five_minutes(self):
        old = self.successful()
        old_checked = old['checked_at']
        with patch.object(history, '_now', return_value=NOW + timedelta(hours=7)):
            self.session.get.side_effect = requests.Timeout('连接超时')
            failed = self.get(allow_fetch=True)
            self.assertEqual(failed['checked_at'], old_checked)
            self.assertEqual(failed['releases'], old['releases'])
            self.assertEqual(self.files[self.cache_path]['checked_at'], old_checked)
            self.session.get.reset_mock()
            self.get(allow_fetch=True)
            self.session.get.assert_not_called()
        with patch.object(history, '_now', return_value=NOW + timedelta(hours=7, minutes=6)):
            self.session.get.side_effect = [response(lookup()), response(html=page())]
            again = self.get(allow_fetch=True)
            self.assertNotEqual(again['checked_at'], old_checked)

    def test_page_failure_preserves_old_history_without_merging_new_current_version(self):
        old = self.successful()
        self.session.get.side_effect = [response(lookup(version='3.0')), response(status=503)]
        failed = self.get(allow_fetch=True, force=True)
        self.assertEqual(failed['releases'], old['releases'])
        self.assertEqual(failed['checked_at'], old['checked_at'])
        self.assertEqual(self.files[self.cache_path]['releases'], old['releases'])

    def test_lookup_429_stops_without_second_request(self):
        self.session.get.side_effect = [response(status=429)]
        result = self.get(allow_fetch=True)
        self.assertEqual(self.session.get.call_count, 1)
        self.assertIn('限流', result['error'])
        self.assertNotIn(self.cache_path, self.files)

    def test_fresh_success_uses_six_hour_cache_and_force_can_refresh(self):
        self.successful()
        self.session.get.reset_mock()
        self.get(allow_fetch=True)
        self.session.get.assert_not_called()
        self.session.get.side_effect = [response(lookup()), response(html=page())]
        self.get(allow_fetch=True, force=True)
        self.assertEqual(self.session.get.call_count, 2)

    def test_failed_forced_refresh_retries_after_five_minutes_and_success_clears_error(self):
        self.successful()
        self.session.get.side_effect = requests.Timeout('暂时超时')
        self.get(allow_fetch=True, force=True)
        with patch.object(history, '_now', return_value=NOW + timedelta(minutes=6)):
            self.session.get.reset_mock()
            self.session.get.side_effect = [response(lookup()), response(html=page())]
            result = self.get(allow_fetch=True)
            self.assertEqual(self.session.get.call_count, 2)
            self.assertEqual(result['error'], '')
            self.assertEqual(self.get()['error'], '')

    def test_cache_write_failure_returns_verified_data_without_crashing(self):
        with patch.object(history, '_atomic_json', side_effect=OSError('只读目录')):
            result = self.successful()
        self.assertEqual(len(result['releases']), 2)
        self.assertIn('缓存保存失败', result['error'])

    def test_day_precision_is_retained_without_inventing_exact_release_time(self):
        items = [{'primarySubtitle': '2.0', 'secondarySubtitle': '2026-09-01'},
                 {'primarySubtitle': '1.0', 'secondarySubtitle': '2026-07-01'}]
        self.session.get.side_effect = [response(lookup()), response(html=page(items=items))]
        result = self.get(allow_fetch=True)
        self.assertEqual(result['releases'][0]['precision'], 'timestamp')
        self.assertEqual(result['releases'][1]['precision'], 'day')
        self.assertEqual(result['releases'][1]['released_at'], '2026-07-01T00:00:00+00:00')

    def test_invalid_history_date_falls_back_instead_of_silently_bridging_a_gap(self):
        items = [{'primarySubtitle': '2.0', 'secondarySubtitle': '2026-09-01T12:00:00Z'},
                 {'primarySubtitle': '1.5', 'secondarySubtitle': '未知日期'},
                 {'primarySubtitle': '1.0', 'secondarySubtitle': '2026-07-01'}]
        self.session.get.side_effect = [response(lookup()), response(html=page(items=items))]
        result = self.get(allow_fetch=True)
        self.assertEqual([r['version'] for r in result['releases']], ['2.0'])
        self.assertTrue(result['error'])

    def test_cache_mismatched_identity_is_ignored_and_parameters_cannot_escape_folder(self):
        cached = self.successful()
        self.files[self.cache_path]['country'] = 'us'
        self.session.get.reset_mock()
        self.assertEqual(self.get()['releases'], [])
        result = history.get_version_history('../111', 'cn', Path('cache'), allow_fetch=True, session=self.session)
        self.assertTrue(result['error'])
        self.session.get.assert_not_called()

    def test_real_saved_page_fixture_has_sixteen_releases_and_current_matches_lookup(self):
        folder = Path(__file__).resolve().parents[2] / 'appstore_validation' / 'version_timeline_probe'
        if not (folder / 'app_page.html').exists():
            self.skipTest('本机只读探测证据未随项目发布')
        payload = json.loads((folder / 'lookup.json').read_text(encoding='utf-8'))
        html = (folder / 'app_page.html').read_text(encoding='utf-8')
        app_url = payload['results'][0]['trackViewUrl'].split('?')[0]
        self.session.get.side_effect = [response(payload), response(html=html, url=app_url)]
        result = history.get_version_history('1618911882', 'cn', Path('cache'),
            allow_fetch=True, session=self.session)
        self.assertEqual(len(result['releases']), 16)
        self.assertEqual(result['releases'][0]['version'], '6.0.0')
        self.assertEqual(result['releases'][0]['released_at'], '2026-07-08T21:06:32+00:00')
        self.assertEqual(result['releases'][-1]['version'], '0.8.0')


if __name__ == '__main__':
    unittest.main()
