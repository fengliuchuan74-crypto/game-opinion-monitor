import hashlib
import json
from datetime import datetime,timedelta
from .operations import analyze_reviews,snapshot,issue_plans,LOCAL_TZ
from .review_store import read_reviews
from .storage import database,utc_now


def publish_alerts(db_path,app_id,country,now=None):
    cutoff=now or datetime.now(LOCAL_TZ)
    # Alerts inspect the current week and its comparison, never the entire archive.
    start=(cutoff-timedelta(days=14)).isoformat()
    data=analyze_reviews(read_reviews(db_path,app_id=app_id,country=country,start_at=start,end_at=cutoff.isoformat()),db_path)
    view=snapshot(data,now=now)
    count=0
    for plan in issue_plans(view):
        if plan['priority']!='P1': continue
        payload={'category':plan['category'],'count':plan['count'],'trigger':plan['trigger'],
                 'evidence':[{k:item[k] for k in ['review_id','content_hash']} for item in plan['evidence']]}
        fingerprint=hashlib.sha256((app_id+country+json.dumps(payload,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
        with database(db_path) as connection:
            cursor=connection.execute('INSERT OR IGNORE INTO alerts(app_id,country,created_at,title,detail,fingerprint) VALUES(?,?,?,?,?,?)',
                (app_id,country,utc_now(),f"{plan['category']} · {plan['count']} 条待核查",json.dumps(payload,ensure_ascii=False),fingerprint))
            count+=cursor.rowcount
    return count


def unread_alerts(db_path):
    with database(db_path) as connection:
        return [dict(row) for row in connection.execute('''SELECT a.*,g.app_name FROM alerts a
            LEFT JOIN game_profiles g ON a.app_id=g.app_id AND a.country=g.country
            WHERE acknowledged_at IS NULL ORDER BY id DESC LIMIT 100''')]


def acknowledge(db_path,alert_id):
    with database(db_path) as connection:
        connection.execute('UPDATE alerts SET acknowledged_at=? WHERE id=?',(utc_now(),int(alert_id)))
