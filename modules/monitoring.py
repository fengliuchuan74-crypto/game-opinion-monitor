"""Durable collection slices: page writes and cursors commit under the same lease."""
from __future__ import annotations
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collectors.app_store import AppStoreCollector
from collectors.app_store_bulk import collect_batch
from collectors.app_store_window import timestamp
from .review_store import init_db, save_reviews
from .storage import database, utc_now
from .version_history import get_version_history

MAX_COLLECTION_PAGES = 20000
DEFAULT_COLLECTION_PAGES = 100
BATCH_PAGES = 20
ACTIVE_STATUSES = ('采集中','等待续采','等待重试','已暂停','排队中')

class LeaseLost(RuntimeError):
    pass

def _json(value):
    try:
        result=json.loads(value or '{}')
        return result if isinstance(result,dict) else {}
    except (TypeError,ValueError): return {}

def _validate_target(app_id,country):
    if (not str(app_id).isascii() or not str(app_id).isdigit() or not str(country).isascii()
            or not str(country).isalpha() or len(country)!=2):
        raise ValueError('请提供数字 App ID 和两位地区代码')
    return str(app_id),country.lower()

def configure_target(db_path,app_id,country,enabled=False,interval_minutes=30,pages=100):
    app_id,country=_validate_target(app_id,country)
    if not 15<=int(interval_minutes)<=1440 or not 1<=int(pages)<=2000:
        raise ValueError('巡检间隔须为 15—1440 分钟，每轮 1—2000 页')
    init_db(db_path)
    with database(db_path) as connection:
        connection.execute('''INSERT INTO monitor_targets(app_id,country,enabled,interval_minutes,pages,next_due)
          VALUES(?,?,?,?,?,?) ON CONFLICT(app_id,country) DO UPDATE SET enabled=excluded.enabled,
          interval_minutes=excluded.interval_minutes,pages=excluded.pages,
          next_due=CASE WHEN excluded.enabled=1 AND monitor_targets.enabled=0 THEN excluded.next_due ELSE monitor_targets.next_due END''',
          (app_id,country,int(enabled),int(interval_minutes),int(pages),utc_now()))

def request_collection(db_path,app_id,country,*,mode='latest',pages=None,start_at=None,end_at=None):
    app_id,country=_validate_target(app_id,country)
    if mode not in ('latest','date_range','history') or (pages is not None and not 1<=int(pages)<=MAX_COLLECTION_PAGES):
        raise ValueError('采集方式无效或页数超出 1—20000 页')
    if mode=='date_range' and (not timestamp(start_at) or not timestamp(end_at)
            or timestamp(start_at)>=timestamp(end_at) or timestamp(start_at)>=datetime.now(timezone.utc)):
        raise ValueError('请选择有效的起止日期')
    init_db(db_path)
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT OR IGNORE INTO monitor_targets(app_id,country,pages) VALUES(?,?,?)',(app_id,country,DEFAULT_COLLECTION_PAGES))
        state=connection.execute('SELECT * FROM monitor_targets WHERE app_id=? AND country=?',(app_id,country)).fetchone()
        active=connection.execute('SELECT status FROM collection_runs WHERE id=?',(state['active_run_id'],)).fetchone()
        if state['requested'] or (state['lease_until'] and state['lease_until']>utc_now()) or (active and active['status'] in ACTIVE_STATUSES): return False
        request={'mode':mode,'pages':int(pages if pages is not None else max(DEFAULT_COLLECTION_PAGES,state['pages'])),
                 'start_at':start_at if mode=='date_range' else None,'end_at':end_at if mode=='date_range' else None}
        connection.execute("UPDATE monitor_targets SET requested=1,request_json=?,active_run_id=NULL,last_status='排队中' WHERE app_id=? AND country=?",
                           (json.dumps(request,ensure_ascii=False),app_id,country))
    return True

def target_state(db_path,app_id,country):
    with database(db_path) as connection:
        row=connection.execute('''SELECT t.*,r.status AS active_status,r.control AS active_control FROM monitor_targets t
          LEFT JOIN collection_runs r ON r.id=t.active_run_id WHERE t.app_id=? AND t.country=?''',(str(app_id),country.lower())).fetchone()
    return dict(row) if row else {}

def recent_runs(db_path,app_id,country,limit=20):
    with database(db_path) as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM collection_runs WHERE app_id=? AND country=? ORDER BY id DESC LIMIT ?',
                                                       (str(app_id),country.lower(),limit))]

def collection_progress(db_path,app_id,country):
    rows=recent_runs(db_path,app_id,country,1)
    return rows[0] if rows else {}

def control_collection(db_path,app_id,country,action):
    if action not in ('pause','resume','cancel'): raise ValueError('未知的采集操作')
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        state=connection.execute('SELECT * FROM monitor_targets WHERE app_id=? AND country=?',(str(app_id),country.lower())).fetchone()
        if not state: return False
        run=connection.execute('SELECT * FROM collection_runs WHERE id=?',(state['active_run_id'],)).fetchone()
        if not run:
            if state['requested'] and action=='cancel':
                connection.execute("UPDATE monitor_targets SET requested=0,request_json='{}',last_status='已取消' WHERE app_id=? AND country=?",(str(app_id),country.lower()))
                return True
            return False
        running=bool(state['lease_until'] and state['lease_until']>utc_now())
        if action=='resume':
            if running or run['status'] not in ('已暂停','等待重试','中断'): return False
            connection.execute("UPDATE collection_runs SET status='等待续采',control='',retry_count=0,available_at=NULL,finished_at=NULL WHERE id=?",(run['id'],))
            connection.execute("UPDATE monitor_targets SET requested=1,lease_until=NULL,lease_token=NULL,last_status='等待续采' WHERE app_id=? AND country=?",(str(app_id),country.lower()))
        elif run['status'] in ACTIVE_STATUSES:
            status='已暂停' if action=='pause' else '已取消'
            connection.execute('UPDATE collection_runs SET control=? WHERE id=?',(action,run['id']))
            if not running:
                connection.execute('UPDATE collection_runs SET status=?,finished_at=? WHERE id=?',(status,utc_now() if action=='cancel' else None,run['id']))
                connection.execute('''UPDATE monitor_targets SET requested=0,lease_until=NULL,lease_token=NULL,last_status=?,next_due=?
                  WHERE app_id=? AND country=?''',(status,(datetime.now(timezone.utc)+timedelta(minutes=state['interval_minutes'])).isoformat(),str(app_id),country.lower()))
        else: return False
    return True

def claim_job(db_path,app_id,country,now=None):
    now=now or datetime.now(timezone.utc); stamp=now.isoformat()
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        cooldown=connection.execute("SELECT value FROM runtime_state WHERE key='collection_not_before'").fetchone()
        if cooldown and cooldown[0]>stamp: return None
        row=connection.execute('SELECT * FROM monitor_targets WHERE app_id=? AND country=?',(app_id,country)).fetchone()
        if not row or (row['lease_until'] and row['lease_until']>stamp): return None
        active=connection.execute('SELECT * FROM collection_runs WHERE id=?',(row['active_run_id'],)).fetchone()
        if active and active['status'] in ACTIVE_STATUSES:
            if active['control'] in ('pause','cancel'):
                status='已暂停' if active['control']=='pause' else '已取消'
                connection.execute('UPDATE collection_runs SET status=?,finished_at=? WHERE id=?',(status,stamp if status=='已取消' else None,active['id']))
                connection.execute('UPDATE monitor_targets SET requested=0,lease_until=NULL,lease_token=NULL,last_status=? WHERE app_id=? AND country=?',(status,app_id,country))
                return None
            if active['status']=='已暂停' or (active['available_at'] and active['available_at']>stamp): return None
            request=_json(active['request_json']); checkpoint=_json(active['checkpoint_json']); run_id=active['id']
            connection.execute("UPDATE collection_runs SET status='采集中',available_at=NULL WHERE id=?",(run_id,))
        else:
            if not row['requested'] and not (row['enabled'] and (not row['next_due'] or row['next_due']<=stamp)): return None
            connection.execute("UPDATE collection_runs SET status='中断',finished_at=?,detail=detail || '\n旧版任务没有断点，新任务重新采集' WHERE app_id=? AND country=? AND status='采集中'",(stamp,app_id,country))
            request={'mode':'latest','pages':row['pages'],**(_json(row['request_json']) if row['requested'] else {})}
            request['end_at']=request.get('end_at') or stamp
            previous=timestamp(row['last_review_at'])
            if request['mode']=='latest': request['start_at']=(previous-timedelta(hours=24)).isoformat() if previous else None
            checkpoint={}
            run_id=connection.execute("INSERT INTO collection_runs(app_id,country,started_at,status,request_json) VALUES(?,?,?,'采集中',?)",
                (app_id,country,stamp,json.dumps(request,ensure_ascii=False))).lastrowid
        token=uuid.uuid4().hex
        connection.execute("""UPDATE monitor_targets SET requested=0,request_json='{}',active_run_id=?,lease_token=?,lease_until=?,
          last_attempt=?,last_status='采集中' WHERE app_id=? AND country=?""",
          (run_id,token,(now+timedelta(minutes=5)).isoformat(),stamp,app_id,country))
    return {**dict(row),**request,'run_id':run_id,'lease_token':token,'checkpoint':checkpoint}

def _owned(connection,job):
    state=connection.execute('SELECT * FROM monitor_targets WHERE app_id=? AND country=?',(job['app_id'],job['country'])).fetchone()
    if (not state or state['active_run_id']!=job['run_id'] or state['lease_token']!=job['lease_token']
            or not state['lease_until'] or state['lease_until']<=utc_now()): raise LeaseLost('采集任务已由新的工作进程接管')
    return state

def perform_job(db_path,job,log_dir,collector=None,stop_event=None):
    collector=collector or AppStoreCollector(log_dir=Path(log_dir),timeout=8)
    app_id,country=job['app_id'],job['country']
    metadata,detail,stop_reason={},[],''
    legacy=not isinstance(collector,AppStoreCollector)
    transport_failed=False
    def should_stop():
        if stop_event is not None and stop_event.is_set(): return True
        with database(db_path) as connection:
            _owned(connection,job)
            row=connection.execute('SELECT control FROM collection_runs WHERE id=?',(job['run_id'],)).fetchone()
            return bool(row['control'])
    def persist_page(rows,meta):
        if any(str(r.app_id)!=app_id or r.country!=country or r.platform!='App Store' for r in rows):
            raise ValueError('采集评论的游戏、地区或平台与当前任务不一致')
        with database(db_path) as connection:
            connection.execute('BEGIN IMMEDIATE'); _owned(connection,job)
            saved=save_reviews(db_path,rows,connection=connection)
            if saved.failed: raise RuntimeError('评论保存未完成：'+'；'.join(saved.errors or []))
            checkpoint=meta.get('checkpoint',{})
            old_checkpoint=_json(connection.execute('SELECT checkpoint_json FROM collection_runs WHERE id=?',(job['run_id'],)).fetchone()[0])
            advanced=int(checkpoint.get('next_page',1))>int(old_checkpoint.get('next_page',1))
            connection.execute('''UPDATE collection_runs SET fetched=?,inserted=inserted+?,updated=updated+?,duplicates=duplicates+?,
              pages=?,oldest_date=?,result_json=?,checkpoint_json=?,retry_count=CASE WHEN ? THEN 0 ELSE retry_count END WHERE id=?''',
              (meta.get('matched_count',len(rows)),saved.inserted,saved.updated,saved.duplicates,
               meta.get('pages_completed',max(0,int(checkpoint.get('next_page',1))-1)),meta.get('oldest_date'),
               json.dumps(meta,ensure_ascii=False),json.dumps(checkpoint,ensure_ascii=False),int(advanced),job['run_id']))
            connection.execute('UPDATE monitor_targets SET lease_until=? WHERE app_id=? AND country=?',
                ((datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),app_id,country))
            connection.execute("INSERT OR REPLACE INTO runtime_state(key,value) VALUES('heartbeat',?)",(utc_now(),))
    try:
        if legacy:
            result=collector.collect(app_id=app_id,country=country,max_pages=job['pages'],delay_seconds=1,
                consecutive_empty_limit=1,detect_repeated_pages=True,follow_feed_links=True,
                start_at=job.get('start_at'),end_at=job.get('end_at'),prefer_public_reviews=True)
            metadata=dict(result.metadata)
            dates=[timestamp(r.date) for r in result.reviews if timestamp(r.date)]
            metadata.update(done=True,matched_count=len(result.reviews),pages_completed=result.requested,
                oldest_date=min(dates).isoformat() if dates else None,newest_date=max(dates).isoformat() if dates else None)
            persist_page(result.reviews,metadata)
        else:
            result=collect_batch(collector,app_id,country,start_at=job.get('start_at'),end_at=job.get('end_at'),
                max_pages=job['pages'],page_budget=BATCH_PAGES,checkpoint=job.get('checkpoint'),
                on_page=persist_page,should_stop=should_stop,delay_seconds=1)
            metadata=dict(result.metadata)
        detail=result.errors+result.warnings; stop_reason=result.stop_reason; transport_failed=bool(result.errors)
    except LeaseLost: return '已由其他进程接管'
    except Exception as exc:
        logging.exception('Collection slice failed')
        detail=[f'采集未完成：{type(exc).__name__}: {exc}']
        with database(db_path) as connection:
            row=connection.execute('SELECT result_json,checkpoint_json FROM collection_runs WHERE id=?',(job['run_id'],)).fetchone()
            metadata=_json(row['result_json'])
            metadata.update(checkpoint=_json(row['checkpoint_json']),done=False,retryable=True)
        transport_failed=True; stop_reason='保存或请求失败，保留断点'
    finally:
        if hasattr(collector,'session') and hasattr(collector.session,'close'): collector.session.close()
    now=datetime.now(timezone.utc)
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try: state=_owned(connection,job)
        except LeaseLost: return '已由其他进程接管'
        run=dict(connection.execute('SELECT * FROM collection_runs WHERE id=?',(job['run_id'],)).fetchone())
        retry_count=run['retry_count']; available=None
        if run['control']=='cancel': status='已取消'
        elif run['control']=='pause': status='已暂停'
        elif metadata.get('retryable'):
            retry_count+=1
            wait=max(30*2**min(retry_count-1,6),float(metadata.get('retry_after_seconds') or 0))
            available=(now+timedelta(seconds=wait)).isoformat()
            status='等待重试' if retry_count<6 else '已暂停'
            if status=='已暂停': detail.append('连续重试仍未恢复，任务已暂停；数据与断点保留，可点击继续采集。')
            if '限流' in stop_reason or any('限流' in item for item in detail):
                connection.execute("INSERT OR REPLACE INTO runtime_state(key,value) VALUES('collection_not_before',?)",(available,))
        elif not metadata.get('done',True): status='等待续采'
        elif transport_failed: status='部分成功' if run['fetched'] else '失败'
        elif (not metadata.get('sequence_verified',True) or (job.get('start_at') and not metadata.get('boundary_reached'))): status='部分成功'
        else: status='成功'
        terminal=status in ('成功','部分成功','失败','已取消')
        if terminal and job.get('mode','latest')=='latest' and job.get('start_at') and not metadata.get('boundary_reached'):
            detail.append('连续性缺口：尚未衔接到上一轮的重扫起点；提高采集上限后补采。原有连续采集水位保留。')
        detail=list(dict.fromkeys(run['detail'].splitlines()+detail))[-30:]
        connection.execute('''UPDATE collection_runs SET finished_at=?,status=?,detail=?,stop_reason=?,result_json=?,
          checkpoint_json=?,retry_count=?,available_at=? WHERE id=?''',
          (now.isoformat() if terminal else None,status,'\n'.join(detail),stop_reason,json.dumps(metadata,ensure_ascii=False),
           json.dumps(metadata.get('checkpoint',job.get('checkpoint',{})),ensure_ascii=False),retry_count,available,job['run_id']))
        failures=int(state['failures'])+1 if transport_failed else 0
        minutes=max(state['interval_minutes'],min(240,15*2**min(failures,4))) if failures else state['interval_minutes']
        connection.execute('''UPDATE monitor_targets SET lease_until=NULL,lease_token=NULL,requested=?,next_due=?,last_status=?,
          failures=?,last_success=CASE WHEN ?='成功' THEN ? ELSE last_success END WHERE app_id=? AND country=?''',
          (int(status in ('等待续采','等待重试')),available or (now+timedelta(minutes=minutes)).isoformat(),status,failures,status,now.isoformat(),app_id,country))
        newest=timestamp(metadata.get('newest_matched_date',metadata.get('newest_date')))
        if job.get('mode','latest')=='latest' and status=='成功' and newest and (not job.get('start_at') or metadata.get('boundary_reached')):
            previous=timestamp(state['last_review_at'])
            connection.execute('UPDATE monitor_targets SET last_review_at=? WHERE app_id=? AND country=?',(max(previous,newest).isoformat() if previous else newest.isoformat(),app_id,country))
    if terminal and run['fetched'] and status in ('成功','部分成功'):
        try:
            from .alerts import publish_alerts
            publish_alerts(db_path,app_id,country)
        except Exception: logging.exception('Comments saved; alerts could not be updated')
        try:
            from .agent_analysis import maybe_queue_analysis
            maybe_queue_analysis(db_path,app_id,country)
        except Exception: logging.exception('Comments saved; optional Agent queue could not be updated')
    return status

def run_due(db_path,log_dir,factory=None,stop_event=None):
    init_db(db_path)
    with database(db_path) as connection:
        connection.execute("INSERT OR REPLACE INTO runtime_state(key,value) VALUES('heartbeat',?)",(utc_now(),))
        targets=connection.execute('''SELECT app_id,country FROM monitor_targets WHERE enabled=1 OR requested=1
          OR (active_run_id IS NOT NULL AND lease_until IS NOT NULL) ORDER BY COALESCE(last_attempt,'')''').fetchall()
    for target in targets:
        if stop_event is not None and stop_event.is_set(): break
        job=claim_job(db_path,target['app_id'],target['country'])
        if not job: continue
        status=perform_job(db_path,job,log_dir,collector=factory() if factory else None,stop_event=stop_event)
        if (factory is None and status in ('成功','部分成功')
                and not (stop_event is not None and stop_event.is_set())):
            try:
                # Finish and close collection transactions before optional I/O.
                with database(db_path) as connection:
                    cooldown=connection.execute("SELECT value FROM runtime_state WHERE key='collection_not_before'").fetchone()
                not_before=timestamp(cooldown['value']) if cooldown else None
                if not_before is not None and not_before>datetime.now(timezone.utc):
                    continue
                get_version_history(job['app_id'],job['country'],Path(db_path).parent/'app_versions',allow_fetch=True)
            except Exception:
                logging.exception('Comments saved; optional version history could not be refreshed')

def start_worker(db_path,log_dir):
    stop=threading.Event()
    def loop():
        while not stop.is_set():
            try: run_due(db_path,log_dir,stop_event=stop)
            except Exception: logging.exception('Monitoring loop error; retrying')
            stop.wait(2)
    thread=threading.Thread(target=loop,name='appstore-monitor',daemon=True)
    thread.start()
    return stop
