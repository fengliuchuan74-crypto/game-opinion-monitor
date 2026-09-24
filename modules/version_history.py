"""Cache publicly documented App Store release times, separate from review versions.

A release interval describes the version available in the store at a comment's
date. It never proves which version that reviewer installed or reviewed.
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests


SUCCESS_TTL = timedelta(hours=6)
FAILURE_TTL = timedelta(minutes=5)
TIMEOUT = (3, 8)
LOOKUP_URL = 'https://itunes.apple.com/lookup'
MONTHS = {name: index for index, name in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}


class SourceMismatch(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _release_date(value):
    text = str(value or '').strip()
    stamp = _stamp(text)
    if stamp:
        return stamp.isoformat(), 'timestamp'
    # Parse the observed JS date serialization without depending on OS locale.
    matched = re.fullmatch(
        r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([A-Z][a-z]{2}) (\d{1,2}) (\d{4}) '
        r'(\d{2}):(\d{2}):(\d{2}) GMT([+-])(\d{2})(\d{2})(?: \([^)]*\))?', text)
    if matched:
        month, day, year, hour, minute, second, sign, offset_hour, offset_minute = matched.groups()
        if month not in MONTHS or int(offset_hour) > 23 or int(offset_minute) > 59:
            raise ValueError('版本发布日期时区无效')
        offset = timedelta(hours=int(offset_hour), minutes=int(offset_minute))
        if sign == '-':
            offset = -offset
        value = datetime(int(year), MONTHS[month], int(day), int(hour), int(minute),
                         int(second), tzinfo=timezone(offset))
        return value.astimezone(timezone.utc).isoformat(), 'timestamp'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        # Midnight is a calendar-day marker, not an inferred exact release time.
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).isoformat(), 'day'
    raise ValueError('版本发布日期缺失或无法可靠解析')


def _official_app_url(value, app_id, country):
    try:
        p = urlparse(str(value))
        return (p.scheme == 'https' and p.hostname == 'apps.apple.com'
                and not p.username and not p.password and p.port in (None, 443)
                and re.fullmatch(rf'/{country}/app/(?:[^/]+/)?id{app_id}/?', p.path) is not None)
    except (TypeError, ValueError):
        return False


def _official_source(value, app_id, country):
    if _official_app_url(value, app_id, country):
        return True
    try:
        p = urlparse(str(value))
        q = parse_qs(p.query)
        return (p.scheme == 'https' and p.hostname == 'itunes.apple.com' and p.path == '/lookup'
                and not p.username and not p.password and p.port in (None, 443)
                and q.get('id') == [app_id] and q.get('country') == [country])
    except (TypeError, ValueError):
        return False


def _version(value):
    text = str(value or '').strip()
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}', text):
        raise ValueError('版本号缺失或格式无效')
    return text


class _EmbeddedData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.matches = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('id') == 'serialized-server-data':
            self.matches += 1
            self.active = attrs.get('type') == 'application/json'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def _page_releases(html, app_id, country, page_url, current):
    parser = _EmbeddedData()
    parser.feed(html)
    if parser.matches != 1 or not parser.parts:
        raise ValueError('公开页面未提供可解析的版本历史')
    parsed = json.loads(''.join(parser.parts))
    data = parsed['data'][0]['data']
    if (not _official_app_url(data.get('canonicalURL'), app_id, country)
            or str(data.get('lockup', {}).get('adamId')) != app_id):
        raise SourceMismatch('公开版本页面的游戏或地区与请求不符')
    fields = data.get('pageMetrics', {}).get('pageFields', {})
    if (fields.get('pageId') is not None and str(fields['pageId']) != app_id
            or fields.get('storeFront') is not None and str(fields['storeFront']).lower() != country):
        raise SourceMismatch('公开版本页面身份校验失败')
    history = data['shelfMapping']['mostRecentVersion']['seeAllAction']['pageData']
    history_id = history.get('pageMetrics', {}).get('pageFields', {}).get('pageId')
    if history_id is not None and str(history_id) != app_id:
        raise SourceMismatch('版本历史属于其他游戏')
    items = history['shelves'][0]['items']
    if not isinstance(items, list) or not items or len(items) > 5000:
        raise ValueError('公开版本历史列表缺失或大小异常')
    releases = {}
    for item in items:
        version = _version(item['primarySubtitle'])
        released_at, precision = _release_date(item['secondarySubtitle'])
        release = dict(version=version, released_at=released_at, precision=precision, source_url=page_url)
        if version in releases and releases[version] != release:
            raise ValueError('同一版本出现冲突的发布日期')
        releases[version] = release
    history_current = releases.get(current['version'])
    if not history_current:
        raise ValueError('公开历史尚未包含 lookup 当前版本')
    same_time = _stamp(history_current['released_at']) == _stamp(current['released_at'])
    same_day = history_current['released_at'][:10] == current['released_at'][:10]
    if not same_time and not (history_current['precision'] == 'day' and same_day):
        raise ValueError('公开历史与 lookup 当前版本发布时间不一致')
    releases[current['version']] = current
    latest = _stamp(current['released_at'])
    if any(_stamp(release['released_at']) > latest for release in releases.values()):
        raise ValueError('公开历史出现晚于 lookup 当前版本的记录，需等待来源同步')
    # Only this response's observed releases are used; no old snapshots are merged.
    return sorted(releases.values(), key=lambda row: row['released_at'], reverse=True)


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_cache(path, app_id, country):
    value = _read_json(path)
    if not value or value.get('app_id') != app_id or value.get('country') != country:
        return None
    if not _stamp(value.get('checked_at')) or not _official_app_url(value.get('source_url'), app_id, country):
        return None
    releases = value.get('releases')
    if not isinstance(releases, list) or not releases or len(releases) > 5000:
        return None
    try:
        if any(not _stamp(item['released_at']) or item['precision'] not in ('timestamp', 'day')
               or not _official_source(item['source_url'], app_id, country) or not _version(item['version'])
               for item in releases):
            return None
        if value['current_version'] not in {row['version'] for row in releases}:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    value['history_complete'] = False
    return value


def _empty(app_id, country):
    return dict(app_id=app_id, country=country, checked_at=None, releases=[], current_version='',
                error='尚无本地版本历史；请联网同步版本资料。', source_url='', history_complete=False)


def get_version_history(app_id, country, cache_dir, *, allow_fetch=False, force=False, session=None):
    """Read offline by default; optionally fetch lookup and its official app page.

    Cache success lasts six hours. Failures have an independent five-minute retry
    record, so an unsuccessful refresh cannot extend the old snapshot's date.
    ``precision='day'`` is explicitly a calendar day, never an exact timestamp.
    """
    app_id, country = str(app_id).strip(), str(country).strip().lower()
    result = _empty(app_id, country)
    if not re.fullmatch(r'[0-9]+', app_id) or not re.fullmatch(r'[a-z]{2}', country):
        result['error'] = 'App ID 或地区代码无效。'
        return result
    folder = Path(cache_dir)
    cache_path = folder / f'{app_id}_{country}.json'
    retry_path = folder / f'{app_id}_{country}.retry.json'
    cached = _load_cache(cache_path, app_id, country)
    attempt = _read_json(retry_path) or {}
    if attempt.get('app_id') != app_id or attempt.get('country') != country:
        attempt = {}
    now = _now()
    checked = _stamp(cached.get('checked_at')) if cached else None
    attempted = _stamp(attempt.get('attempted_at'))
    result = copy.deepcopy(cached) if cached else result
    failed_after_success = bool(attempt.get('error')) and attempted and (checked is None or attempted >= checked)
    if failed_after_success:
        result['error'] = attempt.get('error') or result.get('error', '')
    if not allow_fetch:
        return result
    if not force and not failed_after_success and cached and not cached.get('error') and timedelta(0) <= now - checked < SUCCESS_TTL:
        return result
    if not force and failed_after_success and timedelta(0) <= now - attempted < FAILURE_TTL:
        return result

    transport = session or requests.Session()
    owned = session is None
    fallback = None
    lookup_url = LOOKUP_URL + '?' + urlencode({'id': app_id, 'country': country, 'entity': 'software'})
    try:
        response = transport.get(lookup_url, timeout=TIMEOUT, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Apple 版本资料限流，请稍后重试。' if response.status_code == 429
                             else f'Apple lookup 返回 HTTP {response.status_code}')
        data = response.json()
        items = data.get('results', []) if isinstance(data, dict) else []
        if not isinstance(items, list) or len(items) != 1 or str(items[0].get('trackId')) != app_id:
            raise SourceMismatch('lookup 返回的游戏身份与请求不符')
        app = items[0]
        if not _official_app_url(app.get('trackViewUrl'), app_id, country):
            raise SourceMismatch('lookup 返回的官方链接地区或游戏不匹配')
        page_url = urlunparse(urlparse(app['trackViewUrl'])._replace(query='', fragment=''))
        version = _version(app.get('version'))
        released_at = _stamp(app.get('currentVersionReleaseDate'))
        if not released_at or released_at > now:
            raise ValueError('lookup 当前版本没有可靠的已发布时刻')
        current = dict(version=version, released_at=released_at.isoformat(),
                       precision='timestamp', source_url=lookup_url)
        fallback = dict(app_id=app_id, country=country, checked_at=now.isoformat(), releases=[current],
                        current_version=version, error='', source_url=page_url, history_complete=False)
        response = transport.get(page_url, timeout=TIMEOUT, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('Apple 版本历史限流，请稍后重试。' if response.status_code == 429
                             else f'Apple 版本页面返回 HTTP {response.status_code}')
        final_url = getattr(response, 'url', page_url) or page_url
        if not _official_app_url(final_url, app_id, country):
            raise SourceMismatch('公开版本页面跳转到其他游戏或地区')
        html = response.content.decode('utf-8-sig')
        releases = _page_releases(html, app_id, country, page_url, current)
        result = dict(fallback, releases=releases)
        try:
            _atomic_json(cache_path, result)
            _atomic_json(retry_path, dict(app_id=app_id, country=country,
                                        attempted_at=now.isoformat(), error=''))
        except OSError as exc:
            result['error'] = '版本资料已读取，但本地缓存保存失败：' + str(exc)
        return result
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        error = str(exc) or type(exc).__name__
        # Identity mismatches never produce a successful cache, even if lookup had
        # valid partial information. Existing verified data remains untouched.
        if cached:
            result = copy.deepcopy(cached)
        elif isinstance(exc, SourceMismatch):
            result = _empty(app_id, country)
        else:
            result = copy.deepcopy(fallback or _empty(app_id, country))
        result['error'] = '版本资料同步未完成：' + error
        try:
            if not cached and fallback and not isinstance(exc, SourceMismatch):
                result['error'] += '；当前版本发布时间已保留，历史版本暂不可用。'
                _atomic_json(cache_path, result)
            _atomic_json(retry_path, dict(app_id=app_id, country=country,
                                        attempted_at=now.isoformat(), error=result['error']))
        except OSError:
            result['error'] += '；本地重试记录未能保存。'
        return result
    finally:
        if owned:
            transport.close()
