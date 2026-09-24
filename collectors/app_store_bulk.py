"""Bounded, resumable traversal of Apple's publicly readable review pages.

The persistence callback is the commit boundary: rows and their checkpoint must be
saved together before it returns. Only page-sized review bodies live in memory.
The numeric page ceiling is a user budget, never a promise of historical access.
"""
from __future__ import annotations

import copy
import hashlib
import time
from datetime import datetime, timezone

from .app_store_public import public_context, public_page
from .app_store_window import read_page, timestamp, valid_next
from .base import CollectionResult


MAX_PAGES = 20_000
FINGERPRINT_WINDOW = 256
QUALITY_ISSUE_LIMIT = 40
DATE_ISSUE_CODES = {'missing_date', 'date_order_within_page', 'date_order_between_pages', 'future_date'}


def _identity(row):
    return str(row.external_id) if row.external_id else hashlib.sha256(
        f'{row.author}|{row.date}|{row.title}|{row.content}'.encode()).hexdigest()


def _quality_issue(state, trace_item, code, count, message, **evidence):
    """Keep a small, durable explanation instead of an unexplained false flag."""
    state['sequence_verified'] = False
    if code in DATE_ISSUE_CODES:
        state['chronology_verified'] = False
    issue = {'page': trace_item['page'], 'code': code, 'count': count,
             'message': message, **evidence}
    trace_item.setdefault('quality_issues', []).append(copy.deepcopy(issue))
    issues = state.setdefault('quality_issues', [])
    if any(old.get('page') == issue['page'] and old.get('code') == code for old in issues):
        return
    issues.append(issue)
    state['quality_issue_count'] = state.get('quality_issue_count', 0) + 1
    del issues[:-QUALITY_ISSUE_LIMIT]
    state['quality_issues_truncated'] = max(0, state['quality_issue_count'] - len(issues))


def _coverage(state):
    state['coverage_start'] = state['coverage_end'] = None
    oldest, end = timestamp(state.get('oldest_date')), timestamp(state.get('requested_end'))
    start = timestamp(state.get('requested_start'))
    if not state.get('sequence_verified'):
        state['coverage_status'] = '分页衔接、重复或时间顺序待确认，不能认定连续覆盖'
    elif state.get('boundary_reached'):
        state['coverage_status'] = '已遍历至起始日期之前（公开源可见范围）'
    elif start:
        state['coverage_status'] = '尚未遍历到起始日期'
    elif oldest:
        state['coverage_status'] = '已建立本轮可见范围；更早历史未验证'
    else:
        state['coverage_status'] = '公开源未返回有效评论，未建立覆盖记录'
    if state.get('sequence_verified') and oldest and end:
        covered = max(oldest, start) if start else oldest
        if covered < end:
            state['coverage_start'], state['coverage_end'] = covered.isoformat(), end.isoformat()


def _metadata(state, trace, done=False, retryable=False):
    snapshot = copy.deepcopy(state)
    _coverage(snapshot)
    snapshot['source_exhausted'] = bool(snapshot.get('_source_exhausted'))
    if snapshot['source_exhausted']:
        snapshot['source_exhausted_note'] = '仅表示当前公开列表返回末页，不保证全部历史可见'
    result = {k: v for k, v in snapshot.items() if not k.startswith('_')}
    result.update(checkpoint=snapshot, page_trace=copy.deepcopy(trace), done=done, retryable=retryable)
    return result


def _rss_next(payload, app_id, country, page):
    links = payload.get('feed', {}).get('link', [])
    advertised = next((item['attributes'].get('href', '') for item in links
        if isinstance(item, dict) and isinstance(item.get('attributes'), dict)
        and item['attributes'].get('rel') == 'next'), '') if isinstance(links, list) else ''
    if advertised and not valid_next(advertised, app_id, country, page + 1):
        return '', '分页链接不符合当前游戏、地区和下一页页码'
    return advertised, ''


def collect_batch(collector, app_id, country, *, start_at=None, end_at=None,
                  max_pages=2000, page_budget=20, checkpoint=None, on_page=None,
                  should_stop=None, delay_seconds=1.0):
    """Stream one work slice; resume ``metadata['checkpoint']`` until ``done``.

    ``scanned_count`` counts forward page rows; ``matched_count`` counts matching
    rows excluding known overlap and all replay pages. Replay rows still reach
    ``on_page`` so the database can update/deduplicate them. The database's inserted
    count, rather than either traversal counter, is the exact new-comment count.
    A slice's ``requested`` includes replay pages, excluding context discovery.
    """
    result = CollectionResult(platform=collector.platform)
    now = timestamp(result.started_at)
    app_id, country = str(app_id).strip(), str(country).strip().lower()
    trace = []
    try:
        limit = max(1, min(MAX_PAGES, int(max_pages)))
        budget = max(1, min(200, int(page_budget)))
        delay = max(0.0, min(30.0, float(delay_seconds)))
    except (TypeError, ValueError, OverflowError):
        limit, budget, delay = 0, 0, 0
    previous = copy.deepcopy(checkpoint) if isinstance(checkpoint, dict) else {}
    start = timestamp(previous.get('requested_start', start_at)) if previous.get('requested_start', start_at) else None
    raw_end = previous.get('requested_end', end_at)
    end = timestamp(raw_end) if raw_end else now
    if end:
        end = min(end, now)
    state = previous or {
        'version': 1, 'app_id': app_id, 'country': country, 'next_page': 1,
        'requested_start': start.isoformat() if start else None,
        'requested_end': end.isoformat() if end else None,
        'page_limit': limit, 'source_kind': '', 'scanned_count': 0,
        'matched_count': 0, 'request_attempts': 0, 'undated_count': 0,
        'outside_range_count': 0, 'overlap_count': 0, 'replay_read_count': 0,
        'boundary_reached': False, 'sequence_verified': True,
        'chronology_verified': True, 'target_date_observed': False, '_consecutive_old_pages': 0,
        'quality_issues': [], 'quality_issue_count': 0,
        'oldest_date': None, 'newest_date': None,
        'oldest_matched_date': None, 'newest_matched_date': None, '_anchors': [],
        '_fingerprints': {}, '_context': None, '_advertised': '',
        'counting_note': '累计匹配数不含断点回扫页；实际新增按数据库评论 ID 去重统计',
    }
    # Older checkpoints only had scanned extrema. Never reuse those as a saved
    # comment watermark when the requested date window excluded their rows.
    state.setdefault('oldest_matched_date', None)
    state.setdefault('newest_matched_date', None)
    state.setdefault('chronology_verified', not any(issue.get('code') in DATE_ISSUE_CODES
        for issue in state.get('quality_issues', [])))
    state.setdefault('target_date_observed', False)

    def finish(reason, *, done=False, retryable=False):
        result.stop_reason = reason
        result.metadata = _metadata(state, trace, done, retryable)
        for issue in state.get('quality_issues', []):
            message = issue.get('message')
            if message and message not in result.warnings and message not in result.errors:
                result.warnings.append(message)
        if not state.get('sequence_verified') and not state.get('quality_issues'):
            result.warnings.append('此前采集已标记分页连续性待确认；旧断点未记录具体异常位置。')
        result.finished_at = datetime.now(timezone.utc).isoformat()
        return result

    if (not limit or not app_id.isascii() or not app_id.isdigit()
            or len(country) != 2 or not country.isascii() or not country.isalpha()
            or not end or ((start_at or state.get('requested_start')) and not start)
            or (start and start >= end)
            or state.get('app_id') != app_id or state.get('country') != country):
        result.errors.append('App ID、地区、批量设置或日期范围无效；日期须包含时区且起始早于结束。')
        return finish('参数无效', done=True)
    if on_page is None:
        result.errors.append('大规模采集必须提供逐页持久化回调，避免丢失已读取的评论。')
        return finish('缺少持久化回调', done=True)
    if should_stop and should_stop():
        return finish('已暂停')
    frontier = max(1, int(state.get('next_page', 1)))
    limit = min(limit, int(state.get('page_limit', limit)))
    state['page_limit'] = limit
    if state.get('boundary_reached') or state.get('_source_exhausted'):
        return finish('已越过起始日期' if state.get('boundary_reached') else '公开源末页', done=True)
    if state.get('_target_date_stop'):
        return finish('已读到起始日期之前，连续性待确认', done=True)
    if start and (state.get('_date_review_required') or not state.get('chronology_verified')):
        return finish('评论日期异常，需复核后继续', done=True)
    if frontier > limit:
        return finish('页数上限', done=True)

    # Source selection happens once. Never switch RSS into a deep public cursor.
    initial_attempts = []
    context = state.get('_context')
    if not state.get('source_kind'):
        context, error = public_context(collector, app_id, country, state, initial_attempts)
        if any(item.get('http_status') == 429 for item in initial_attempts):
            result.errors.append(error)
            trace.append({'page': frontier, 'attempts': initial_attempts, 'read_count': 0, 'matched_count': 0})
            return finish('公开源限流', retryable=True)
        if context:
            state.update(source_kind='public_review_list', _context=context,
                         source_total_reported=context.get('total_reported'))
        else:
            result.warnings.append(error + '；仅在第 1 页降级为 RSS，RSS 最多读取 10 页。')
            state.update(source_kind='public_rss', source_page_limit=10)
    if state['source_kind'] == 'public_review_list' and not context:
        # A malformed old checkpoint cannot turn an established deep cursor into RSS.
        result.errors.append('断点缺少公开列表上下文，请重新建立采集任务。')
        state['sequence_verified'] = False
        return finish('断点无效', done=True)
    if state['source_kind'] == 'public_rss' and frontier > 10:
        return finish('RSS 页数上限', done=True)

    resume = frontier > 1
    first_page = max(1, frontier - 2) if resume else 1
    # Tiny budgets must still leave room to advance after the two replay pages.
    budget = max(budget, frontier - first_page + 1)
    anchors = copy.deepcopy(state.get('_anchors', []))
    original_ids = {identity for anchor in anchors for identity in anchor.get('ids', [])}
    old_tail = next((anchor.get('ids', [])[-1] for anchor in reversed(anchors)
        if anchor.get('page') == frontier - 1 and anchor.get('ids')), None)
    seam_found = not resume
    prior_page_oldest = None
    replay_ids = set()
    live_fingerprints = set()
    state.pop('retry_after_seconds', None)

    for page in range(first_page, limit + 1):
        if result.requested >= budget:
            return finish('分批保存，继续采集')
        if should_stop and should_stop():
            return finish('已暂停')
        if state['source_kind'] == 'public_rss' and page > 10:
            return finish('RSS 页数上限', done=True)
        result.requested += 1
        attempts = initial_attempts if result.requested == 1 else []
        initial_attempts = []
        public = state['source_kind'] == 'public_review_list'
        if public:
            payload, rows, has_more, error = public_page(
                collector, app_id, country, page, context, state, attempts)
            rate_limited = any(item.get('http_status') == 429 for item in attempts)
            empty_list = isinstance(payload.get('feed'), dict) and payload['feed'].get('entry') == []
            known_empty = page > 1 or context.get('total_reported') == 0
            if not rows and empty_list and known_empty and not rate_limited:
                # A valid, source-identified empty list after earlier pages is its visible end.
                error = ''
                has_more = False
            elif not rows and page == 1 and not resume and not rate_limited:
                result.warnings.append(error + '；改用同地区 RSS，最多读取 10 页。')
                state.update(source_kind='public_rss', source_page_limit=10, _context=None)
                state.pop('source_total_reported', None)
                public = False
        if not public:
            payload, rows, rss_attempts, error = read_page(
                collector, app_id, country, page, state.get('_advertised', '') if not resume else '', state)
            attempts.extend(rss_attempts)
            has_more = False
        item = {'page': page, 'attempts': attempts, 'read_count': len(rows),
                'matched_count': 0, 'replay': page < frontier}
        trace.append(item)
        if any(attempt.get('http_status') == 429 for attempt in attempts):
            result.errors.append(f'第 {page} 页限流，断点保留，稍后重试。')
            state.setdefault('retry_after_seconds', 60)
            return finish('公开源限流', retryable=True)
        if error:
            result.errors.append(f'第 {page} 页：{error}；已保留此前保存的数据。')
            if error == '公开源页码上限':
                return finish('RSS 页数上限', done=True)
            return finish('读取暂时失败', retryable=True)

        candidate = copy.deepcopy(state)
        is_replay = page < frontier
        identities = [_identity(row) for row in rows]
        dates = [timestamp(row.date) for row in rows]
        known = [value for value in dates if value is not None]
        if old_tail and old_tail in identities:
            seam_found = True
        if resume and page >= frontier and not seam_found:
            _quality_issue(candidate, item, 'resume_anchor_missing', 1,
                f'第 {page} 页：回扫未找到旧末页尾部评论 ID，断点衔接待确认。',
                expected_tail_id=old_tail)
            candidate['resume_status'] = '回扫未找到旧末页尾部评论 ID，衔接待确认'
        elif resume and seam_found:
            candidate['resume_status'] = '回扫已找到旧末页尾部评论 ID'
        missing_dates = len(rows) - len(known)
        within_page_inversions = sum(a < b for a, b in zip(known, known[1:]))
        future_dates = sum(d > now for d in known)
        if missing_dates:
            _quality_issue(candidate, item, 'missing_date', missing_dates,
                f'第 {page} 页：{missing_dates} 条评论缺少有效日期，无法确认完整时间覆盖。')
        if within_page_inversions:
            _quality_issue(candidate, item, 'date_order_within_page', within_page_inversions,
                f'第 {page} 页：出现 {within_page_inversions} 处评论时间逆序，公开列表并非严格按日期递减。')
        if future_dates:
            _quality_issue(candidate, item, 'future_date', future_dates,
                f'第 {page} 页：{future_dates} 条评论日期晚于本批采集时间，日期可靠性待确认。')
        if prior_page_oldest and known and max(known) > prior_page_oldest:
            _quality_issue(candidate, item, 'date_order_between_pages', 1,
                f'第 {page - 1} → {page} 页：后一页出现更新的评论时间，分页顺序可能发生漂移。',
                previous_oldest=prior_page_oldest.isoformat(), newest_date=max(known).isoformat())
        if known:
            prior_page_oldest = min(known)
            item.update(oldest_date=min(known).isoformat(), newest_date=max(known).isoformat())
        fingerprint = hashlib.sha256('\n'.join(sorted(identities)).encode()).hexdigest()
        recent_fingerprints = candidate.setdefault('_fingerprints', {})
        if rows and fingerprint in live_fingerprints:
            _quality_issue(candidate, item, 'repeated_page', len(rows),
                f'第 {page} 页重复返回本批已读页面，已停止以免重复循环。')
            state = candidate
            result.errors.append(f'第 {page} 页重复返回本批已读页面，已停止以免重复循环。')
            return finish('重复页面', done=True)
        if rows and not is_replay and fingerprint in recent_fingerprints:
            original_page = int(recent_fingerprints[fingerprint])
            # Offset drift may carry an old anchor page forward once, before its tail is found.
            expected_drift = resume and original_page >= frontier - 2 and not (set(identities) & replay_ids)
            if not expected_drift:
                _quality_issue(candidate, item, 'repeated_page', len(rows),
                    f'第 {page} 页重复返回第 {original_page} 页内容，已停止以免重复循环。',
                    original_page=original_page)
                state = candidate
                result.errors.append(f'第 {page} 页重复返回已读页面，已停止以免重复循环。')
                return finish('重复页面', done=True)
        if rows:
            live_fingerprints.add(fingerprint)
        local_ids = set()
        matched = []
        matched_dates = []
        new_matches = 0
        duplicate_within_page = []
        unexpected_overlap = []
        recent_ids = {identity for anchor in state.get('_anchors', []) for identity in anchor.get('ids', [])}
        for row, date, identity in zip(rows, dates, identities):
            if identity in local_ids:
                duplicate_within_page.append(identity)
                continue
            local_ids.add(identity)
            duplicate = identity in recent_ids or identity in replay_ids or (resume and identity in original_ids)
            if not is_replay and duplicate:
                candidate['overlap_count'] += 1
                expected_resume_overlap = resume and (identity in original_ids or identity in replay_ids)
                if not expected_resume_overlap:
                    unexpected_overlap.append(identity)
            if date and date < end and (start is None or date >= start):
                matched.append(row)
                matched_dates.append(date)
                if not is_replay and not duplicate:
                    new_matches += 1
            elif not is_replay:
                candidate['outside_range_count'] += 1
        if duplicate_within_page:
            _quality_issue(candidate, item, 'duplicate_id_within_page', len(duplicate_within_page),
                f'第 {page} 页：同一页重复返回 {len(duplicate_within_page)} 条评论 ID，已去重保存。',
                review_ids=duplicate_within_page[:3])
        if unexpected_overlap:
            _quality_issue(candidate, item, 'unexpected_page_overlap', len(unexpected_overlap),
                f'第 {page} 页：与此前页面重叠 {len(unexpected_overlap)} 条评论，已按 ID 去重；公开分页可能变化，连续覆盖待确认。',
                review_ids=unexpected_overlap[:3])
        if matched_dates:
            # These are the dates actually handed to the sink, including replayed
            # rows. Scanned extrema may contain excluded rows beyond frozen end.
            oldest_match = timestamp(candidate.get('oldest_matched_date'))
            newest_match = timestamp(candidate.get('newest_matched_date'))
            candidate['oldest_matched_date'] = min([min(matched_dates)] + ([oldest_match] if oldest_match else [])).isoformat()
            candidate['newest_matched_date'] = max([max(matched_dates)] + ([newest_match] if newest_match else [])).isoformat()
        if is_replay:
            candidate['replay_read_count'] += len(rows)
            replay_ids.update(identities)
        else:
            candidate['scanned_count'] += len(rows)
            candidate['matched_count'] += new_matches
            candidate['undated_count'] += len(rows) - len(known)
            item['matched_count'] = new_matches
            if known:
                oldest, newest = timestamp(candidate.get('oldest_date')), timestamp(candidate.get('newest_date'))
                candidate['oldest_date'] = min([min(known)] + ([oldest] if oldest else [])).isoformat()
                candidate['newest_date'] = max([max(known)] + ([newest] if newest else [])).isoformat()
            candidate['_anchors'] = (candidate.get('_anchors', []) + [{'page': page, 'ids': identities}])[-2:]
            if rows:
                recent_fingerprints[fingerprint] = page
                while len(recent_fingerprints) > FINGERPRINT_WINDOW:
                    del recent_fingerprints[next(iter(recent_fingerprints))]
            candidate['next_page'] = page + 1
        if not public:
            advertised, link_error = _rss_next(payload, app_id, country, page)
            candidate['_advertised'] = advertised
            has_more = bool(advertised)
            if link_error:
                _quality_issue(candidate, item, 'invalid_next_page', 1,
                    f'第 {page} 页：{link_error}。')
                result.errors.append(link_error)
        else:
            link_error = ''
        reached = bool(start and known and min(known) < start and candidate['sequence_verified'] and not is_replay)
        if reached:
            candidate['boundary_reached'] = True
        if start and not is_replay:
            if known and min(known) < start:
                candidate['target_date_observed'] = True
            all_older = bool(rows and len(known) == len(rows) and max(known) < start
                             and candidate['chronology_verified'])
            candidate['_consecutive_old_pages'] = candidate.get('_consecutive_old_pages', 0) + 1 if all_older else 0
        date_review = bool(start and not candidate['chronology_verified'])
        target_date_stop = bool(start and not candidate['sequence_verified']
            and candidate['chronology_verified'] and candidate.get('_consecutive_old_pages', 0) >= 2)
        if date_review:
            candidate['_date_review_required'] = True
            candidate['date_stop_note'] = '评论日期缺失或顺序异常，已保存本页有效范围内评论；需复核后重新采集。'
        elif target_date_stop:
            candidate['_target_date_stop'] = True
            candidate['date_stop_note'] = '连续两页的有效日期均早于目标起点，已停止继续回溯；分页质量异常仍待确认，不宣称完整覆盖。'
        source_end = not has_more and not is_replay
        if source_end:
            candidate['_source_exhausted'] = True
        capped = page >= limit or (not public and page >= 10)
        done = reached or date_review or target_date_stop or source_end or bool(link_error) or capped
        try:
            # The database stores candidate.next_page atomically with this page's rows.
            on_page(matched, _metadata(candidate, trace, done=done))
        except Exception as exc:
            result.errors.append(f'第 {page} 页保存失败（{type(exc).__name__}），游标未前进。')
            return finish('保存暂时失败', retryable=True)
        state = candidate
        if reached:
            return finish('已越过起始日期', done=True)
        if date_review:
            result.warnings.append(candidate['date_stop_note'])
            return finish('评论日期异常，需复核后继续', done=True)
        if target_date_stop:
            result.warnings.append(candidate['date_stop_note'])
            return finish('已读到起始日期之前，连续性待确认', done=True)
        if link_error:
            return finish('分页异常', done=True)
        if source_end:
            return finish('公开源末页', done=True)
        if capped:
            return finish('RSS 页数上限' if not public and page >= 10 else '页数上限', done=True)
        if delay and result.requested < budget:
            time.sleep(delay)
    return finish('页数上限', done=True)
