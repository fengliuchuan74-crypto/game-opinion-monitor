"""Explain traversed public-feed intervals without treating local samples as complete history."""
import json
from datetime import timedelta

import pandas as pd

from .operations import LOCAL_TZ
from .storage import database


def run_details(run):
    try:
        data=json.loads(run.get('result_json') or '{}')
        return data if isinstance(data,dict) else {}
    except (TypeError,ValueError): return {}


def run_request(run):
    try:
        data=json.loads(run.get('request_json') or '{}')
        return data if isinstance(data,dict) else {}
    except (TypeError,ValueError): return {}


def coverage_calendar(db_path,app_id,country,raw,start,end):
    start=pd.Timestamp(start,tz=LOCAL_TZ)
    end=min(pd.Timestamp(end,tz=LOCAL_TZ)+pd.Timedelta(days=1),pd.Timestamp.now(tz=LOCAL_TZ))
    if raw is not None:
        dates=pd.to_datetime(raw.get('date',pd.Series(dtype=str)),utc=True,errors='coerce',format='mixed').dt.tz_convert(LOCAL_TZ)
        counts=dates.dt.strftime('%Y-%m-%d').value_counts()
    else:
        # The coverage calendar needs daily counts, never historical review bodies.
        with database(db_path) as connection:
            rows=connection.execute('''SELECT date(date,'+8 hours') AS day,COUNT(*) AS n FROM review_records
              WHERE app_id=? AND country=? AND julianday(date)>=julianday(?) AND julianday(date)<julianday(?) GROUP BY day''',
              (str(app_id),country,start.isoformat(),end.isoformat())).fetchall()
        counts={row['day']:row['n'] for row in rows}
    with database(db_path) as connection:
        runs=connection.execute("SELECT result_json,finished_at FROM collection_runs WHERE app_id=? AND country=? AND finished_at IS NOT NULL",(app_id,country)).fetchall()
    intervals=[]
    for run in runs:
        meta=run_details(dict(run))
        if not meta.get('sequence_verified') or not meta.get('coverage_start') or not meta.get('coverage_end'): continue
        try:
            left=pd.Timestamp(meta['coverage_start']).tz_convert(LOCAL_TZ)
            right=pd.Timestamp(meta['coverage_end']).tz_convert(LOCAL_TZ)
            if left<right: intervals.append((left,right))
        except (TypeError,ValueError): continue
    intervals.sort()
    output=[]; day=start
    while day<end:
        stop=min(day+timedelta(days=1),end)
        cursor=day; touched=False
        for left,right in intervals:
            if right<=day or left>=stop: continue
            touched=True
            if left<=cursor: cursor=max(cursor,min(right,stop))
        state='公开源时段已遍历' if cursor>=stop else '仅部分时段已遍历' if touched else '尚无遍历记录'
        output.append({'日期（北京时间）':day.strftime('%Y-%m-%d'),
            '本地评论数':int(counts.get(day.strftime('%Y-%m-%d'),0)),
            '公开源检查范围':state,'截至':stop.strftime('%H:%M') if stop<day+timedelta(days=1) else '24:00'})
        day+=timedelta(days=1)
    return pd.DataFrame(output)
