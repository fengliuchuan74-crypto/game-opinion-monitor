"""Read-only version attribution; date inference never becomes source metadata."""
from __future__ import annotations

import re

import pandas as pd


def _text(value) -> str:
    if value is None or pd.isna(value):
        return ''
    return str(value).strip()


def _source_version(topic) -> str:
    match = re.search(r'(?:version|ver|版本)\s*[:：]\s*([^\s,，;；]+)', _text(topic), re.I)
    return match.group(1) if match else ''


def _timestamp(value):
    try:
        if not _text(value):
            return None
        stamp = pd.Timestamp(value)
        return stamp if pd.notna(stamp) and stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _history(history):
    if not isinstance(history, dict):
        return None, [], '暂无可用版本历史'
    checked = _timestamp(history.get('checked_at'))
    if checked is None:
        return None, [], '版本历史观测时间缺失或没有时区'
    if not isinstance(history.get('releases'), list):
        return checked, [], '暂无可用版本历史'
    releases, exact_versions = {}, {}
    for release in history['releases']:
        if not isinstance(release, dict):
            return checked, [], '版本历史记录格式无效'
        stamp = _timestamp(release.get('released_at'))
        if stamp is None:
            return checked, [], '版本发布时间缺失或没有时区'
        if stamp > checked:
            continue
        version = _text(release.get('version'))
        precision = _text(release.get('precision'))
        if not version or precision not in ('timestamp', 'day'):
            return checked, [], '版本号或发布时间精度无效'
        exact = stamp.tz_convert('UTC')
        if exact in exact_versions and exact_versions[exact] != version:
            return checked, [], '同一发布时间存在冲突版本'
        exact_versions[exact] = version
        # A day-precision release blocks its entire local calendar day.
        start = stamp.normalize() if precision == 'day' else stamp
        day_end = start + pd.DateOffset(days=1) if precision == 'day' else None
        key = start.tz_convert('UTC')
        existing = releases.get(key)
        if existing and existing['version'] != version:
            return checked, [], '同一发布时间存在冲突版本'
        if existing and existing['day_end'] is not None:
            if day_end is not None:
                existing['day_end'] = max(existing['day_end'], day_end)
            continue
        releases[key] = {'version': version, 'start': start, 'day_end': day_end}
    return checked, sorted(releases.values(), key=lambda item: item['start']), ''


def review_version_text(row) -> str:
    """Return a UI label, also accepting legacy rows containing only topic."""
    source = _source_version(row.get('topic')) or _text(row.get('source_version'))
    if source:
        return f'版本 {source}（来源提供）'
    inferred = _text(row.get('inferred_version'))
    if _text(row.get('version_basis')) == 'date_inferred' and inferred:
        return f'版本 {inferred}（按日期推定）'
    return '版本未能确定'


def attribute_versions(data: pd.DataFrame, history: dict) -> pd.DataFrame:
    """Append display-only attribution fields to an independent dataframe copy."""
    result = data.copy(deep=True)
    checked, releases, history_error = _history(history)
    identity = history if isinstance(history, dict) else {}
    fields = {key: [] for key in ('source_version', 'inferred_version', 'version_basis',
                                  'version_display', 'version_reason')}
    for _, row in result.iterrows():
        source = _source_version(row.get('topic'))
        inferred, basis, reason = '', 'unknown', ''
        if source:
            basis, reason = 'source', '评论来源直接提供版本'
        elif (not _text(identity.get('app_id')) or not _text(identity.get('country'))
              or _text(row.get('app_id')) != _text(identity.get('app_id'))
              or _text(row.get('country')).lower() != _text(identity.get('country')).lower()):
            reason = '没有匹配该应用及地区的版本历史'
        elif history_error:
            reason = history_error
        else:
            date = _timestamp(row.get('date'))
            if date is None:
                reason = '评论日期缺失或没有时区'
            elif checked is None or date > checked:
                reason = '评论时间晚于版本历史观测时间'
            elif not releases or date < releases[0]['start']:
                reason = '评论早于已知版本历史或暂无可用记录'
            elif any(item['day_end'] is not None and item['start'] <= date < item['day_end']
                     for item in releases):
                reason = '发布日仅有日期精度，无法确定当天评论版本'
            else:
                selected = next(item for item in reversed(releases) if item['start'] <= date)
                inferred, basis = selected['version'], 'date_inferred'
                reason = '按同应用同地区的发布时间区间推定，未由评论来源证实'
        fields['source_version'].append(source)
        fields['inferred_version'].append(inferred)
        fields['version_basis'].append(basis)
        fields['version_display'].append(source or inferred or '版本未知')
        fields['version_reason'].append(reason)
    for key, values in fields.items():
        result[key] = values
    return result
