"""Compact collection summary and a dated, read-only collection journal."""
from __future__ import annotations

import html
from datetime import datetime,timedelta
from math import ceil

import pandas as pd
import streamlit as st

from modules.collection_coverage import run_details,run_request
from modules.monitoring import collection_progress,control_collection,request_collection,target_state
from modules.operations import LOCAL_TZ
from modules.storage import database

STATUS_STYLES={'成功':'success','部分成功':'partial','失败':'failed','采集中':'running',
    '等待续采':'running','等待重试':'partial','已暂停':'paused','排队中':'running',
    '已取消':'interrupted','中断':'interrupted'}
ACTIVE_STATUSES={'采集中','等待续采','等待重试','排队中'}
MODES={'最新评论 / 衔接上次采集':'latest','按固定日期补采':'date_range','历史回溯':'history'}
MODE_LABELS={'latest':'最新增量','date_range':'日期补采','history':'历史回溯'}
SCAN_LIMITS={'500 条':500,'5,000 条':5000,'10,000 条':10000,'50,000 条':50000}
MAX_SCAN_REVIEWS=1_000_000


def local_time(value, seconds=False):
    if not value: return '未记录'
    stamp=pd.to_datetime(value,utc=True,errors='coerce')
    if pd.isna(stamp): return '未记录'
    return stamp.tz_convert(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S' if seconds else '%Y-%m-%d %H:%M')


def collection_busy(state):
    lease=pd.to_datetime(state.get('lease_until'),utc=True,errors='coerce')
    return bool(state.get('requested') or state.get('active_status') in ACTIVE_STATUSES|{'已暂停'}
        or (pd.notna(lease) and lease>pd.Timestamp.now(tz='UTC')))


def scan_pages(count):
    """Round a user-facing scan budget to the source's 50-review batches."""
    if not 50<=int(count)<=MAX_SCAN_REVIEWS:
        raise ValueError('单次扫描上限须为 50—1,000,000 条')
    return ceil(int(count)/50)


def collection_totals(db,app_id,country):
    """Aggregate in SQLite without loading historical review bodies into the UI."""
    with database(db) as connection:
        row=connection.execute('''SELECT COUNT(*) AS total,
            datetime(MIN(julianday(date))) AS oldest_date,
            datetime(MAX(julianday(date))) AS newest_date
            FROM review_records WHERE app_id=? AND country=?''',(str(app_id),country)).fetchone()
    return dict(row)


def progress_card(run,state=None):
    state=state or {}
    queued=bool(state.get('requested') and not state.get('active_run_id'))
    status='排队中' if queued else str(run.get('status') or state.get('active_status') or '等待采集')
    meta=run_details(run) if not queued else {}
    request=run_request(run) if not queued else {}
    scanned=int(meta.get('scanned_count',run.get('fetched',0)) or 0) if not queued else 0
    inserted=int(run.get('inserted') or 0) if not queued else 0
    oldest=meta.get('oldest_date') or run.get('oldest_date') if not queued else None
    newest=meta.get('newest_date') if not queued else None
    mode=MODE_LABELS.get(request.get('mode'),'评论采集')
    control=state.get('active_control') or run.get('control')
    waiting_control=status=='采集中' and control in ('pause','cancel')
    displayed_status=('正在暂停' if control=='pause' else '正在结束') if waiting_control else status
    hints={
        '采集中':'后台逐页采集并保存，关闭此浏览器页面也可继续。',
        '等待续采':'本段已保存，正在排队继续读取下一段。',
        '等待重试':'已保存当前进度，等待网络恢复后自动重试。',
        '已暂停':'进度与已获取评论均已保留，可继续本次采集。',
        '排队中':'已加入后台队列，即将开始采集。',
        '已取消':'本次采集已结束，已保存的评论仍可浏览和分析。',
        '成功':'本次采集已完成，覆盖情况可在采集日志查看。',
        '部分成功':'已保存可获取的评论，未完成原因请查看采集日志。',
        '失败':'本次采集未完成，请查看采集日志中的原因。',
        '中断':'采集曾中断，详情可在采集日志查看。',
    }
    period=(local_time(oldest)+' — '+local_time(newest)) if oldest and newest else ('已读到 '+local_time(oldest) if oldest else '正在等待有效评论')
    hint=('已收到暂停请求，正在等待当前页保存完成。' if control=='pause' else
        '已收到结束请求，正在保存当前页；已获取评论会全部保留。') if waiting_control else hints.get(status,'在采集设置中选择最新增量、日期补采或历史回溯。')
    budget=(f"已完成 {int(run.get('pages') or 0):,} 批 · 最多扫描约 {int(request['pages'])*50:,} 条"
        if request.get('pages') else '')
    escape=lambda value:html.escape(str(value))
    target=''
    if request.get('mode')=='date_range':
        matched=meta.get('matched_count')
        matched_label=f'{int(matched):,} 条' if matched is not None else '尚未记录'
        target=('<div class="collection-log-range">目标日期范围 · '+
            escape(local_time(request.get('start_at'))+' — '+local_time(request.get('end_at')))+
            '（北京时间，结束时间不含）</div>'+
            '<div class="collection-log-range">目标范围内去重评论 · <b>'+escape(matched_label)+
            '</b>；其中已有评论不会重复新增入库。</div>')
    return ('<section class="collection-live '+STATUS_STYLES.get(status,'interrupted')+'">'
        '<div class="collection-live-heading"><strong>'+escape(mode)+'</strong>'
        '<span class="collection-status">'+escape(displayed_status)+'</span></div>'
        '<div class="collection-live-stats"><span>本次已读取 <b>'+f'{scanned:,}'+'</b> 条</span>'
        '<span class="new">新增入库 <b>'+f'{inserted:,}'+'</b> 条</span></div>'
        '<div class="collection-log-range">本轮扫描到的评论时间 · '+escape(period)+'</div>'+
        target+
        '<div class="collection-log-range">'+escape(budget)+'</div>'
        '<p>'+escape(hint)+'</p></section>')


def log_dates(db,app_id,country):
    with database(db) as connection:
        return [row[0] for row in connection.execute('''SELECT DISTINCT date(started_at,'+8 hours') AS day
            FROM collection_runs WHERE app_id=? AND country=? AND day IS NOT NULL ORDER BY day DESC''',
            (str(app_id),country))]


def log_entries(db,app_id,country,day='全部日期',page=1,page_size=10):
    predicate='app_id=? AND country=?'
    parameters=[str(app_id),country]
    if day!='全部日期':
        predicate+=" AND date(started_at,'+8 hours')=?"
        parameters.append(day)
    with database(db) as connection:
        count=connection.execute('SELECT COUNT(*) FROM collection_runs WHERE '+predicate,parameters).fetchone()[0]
        page=max(1,min(int(page),max(1,ceil(count/page_size))))
        rows=connection.execute('SELECT * FROM collection_runs WHERE '+predicate+
            ' ORDER BY id DESC LIMIT ? OFFSET ?',parameters+[page_size,(page-1)*page_size]).fetchall()
    return [dict(row) for row in rows],count,page


def log_card(run):
    escape=lambda value:html.escape(str(value if value is not None else ''))
    status=str(run.get('status') or '未记录')
    style=STATUS_STYLES.get(status,'interrupted')
    meta=run_details(run)
    request=run_request(run)
    stamp=local_time(run.get('started_at'),True)
    clock=stamp.split(' ')[-1]
    earliest=meta.get('oldest_date') or run.get('oldest_date')
    newest=meta.get('newest_date')
    period=(local_time(earliest)+' — '+local_time(newest)) if earliest else '本轮未记录有效评论时间'
    count=int(run.get('fetched') or 0)
    mode=MODE_LABELS.get(request.get('mode'),'最新评论')
    return ('<article class="collection-log '+style+'"><div class="collection-log-top"><strong>'+
        escape(clock)+' <span>· '+mode+'</span></strong><span class="collection-status">'+escape(status)+
        '</span></div><div class="collection-log-count">获取 <b>'+str(count)+'</b> 条评论'+
        '<span>新增 '+str(int(run.get('inserted') or 0))+' · 更新 '+str(int(run.get('updated') or 0))+
        ' · 重复 '+str(int(run.get('duplicates') or 0))+'</span></div>'+
        '<div class="collection-log-range">本轮读到的评论：'+escape(period)+'</div></article>')


def close_collection_log():
    st.session_state.pop('collection_dialog',None)


def open_collection_log(profile):
    st.session_state.pop('dashboard_dialog',None)
    st.session_state.collection_dialog=dict(profile=profile,scope=st.session_state.get('scope',''))


@st.dialog('采集日志',width='large',on_dismiss=close_collection_log)
def collection_log_dialog(db,profile):
    prefix=f"journal_{profile['app_id']}_{profile['country']}"
    title,refresh,close=st.columns([4,1,1])
    title.markdown('**'+profile['app_name']+' · '+profile['country'].upper()+'**')
    refresh.button('刷新日志',key=prefix+'_refresh',width='stretch')
    if close.button('关闭日志',key=prefix+'_close',width='stretch'):
        close_collection_log(); st.rerun()
    st.caption('按采集开始时间分组 · 北京时间。评论时间范围记录本轮实际读到的内容，范围内仍可能存在未获取评论。')
    st.markdown('<div class="collection-legend"><span class="success">● 成功</span>'
        '<span class="partial">● 部分成功</span><span class="failed">● 失败</span>'
        '<span class="running">● 采集中 / 续采</span><span class="paused">● 已暂停</span>'
        '<span class="interrupted">● 中断 / 已取消</span></div>',unsafe_allow_html=True)
    current=target_state(db,profile['app_id'],profile['country'])
    if current.get('requested'): st.info('新采集任务正在排队，开始后会出现在日志中。')
    dates=['全部日期']+log_dates(db,profile['app_id'],profile['country'])
    if st.session_state.get(prefix+'_day') not in dates: st.session_state[prefix+'_day']='全部日期'
    date_col,page_col=st.columns([3,1])
    day=date_col.selectbox('查看采集日期',dates,key=prefix+'_day')
    _,count,_=log_entries(db,profile['app_id'],profile['country'],day)
    pages=list(range(1,max(1,ceil(count/10))+1))
    if st.session_state.get(prefix+'_page') not in pages: st.session_state[prefix+'_page']=1
    page=page_col.selectbox('日志页码',pages,key=prefix+'_page',format_func=lambda value:f'{value} / {len(pages)}')
    rows,_,_=log_entries(db,profile['app_id'],profile['country'],day,page)
    if not rows:
        st.info('还没有采集记录。点击“立即采集最新评论”后，可在这里查看采集情况。')
        return
    last_day=None
    for run in rows:
        group=local_time(run.get('started_at')).split(' ')[0]
        if group!=last_day:
            st.markdown('<div class="collection-log-day">'+html.escape(group)+'</div>',unsafe_allow_html=True)
            last_day=group
        st.markdown(log_card(run),unsafe_allow_html=True)
        with st.expander(f"本轮详情 · #{run['id']}"):
            meta=run_details(run); request=run_request(run)
            st.write(f"读取 {meta.get('scanned_count',run.get('fetched',0))} 条；目标范围内 {run.get('fetched',0)} 条；尝试 {run.get('pages',0)} 页。")
            st.caption('采集时间：'+local_time(run.get('started_at'),True)+' — '+
                (local_time(run.get('finished_at'),True) if run.get('finished_at') else '本次任务尚未结束'))
            if request.get('start_at'):
                st.caption(('目标补采区间：' if request.get('mode')=='date_range' else '连续采集重扫区间：')+
                    local_time(request['start_at'])+' — '+local_time(request.get('end_at')))
            st.write('覆盖情况：'+str(meta.get('coverage_status') or '旧记录未验证连续覆盖'))
            if request.get('pages'):
                st.caption(f"本次最多扫描约 {int(request['pages'])*50:,} 条。包含重复评论与目标日期范围之外的评论；不等于新增数量。")
            source=str(meta.get('source_kind') or meta.get('source') or meta.get('transport') or '')
            if source: st.caption('采集来源：'+{'public_review_list':'App Store 公开评论列表','public_rss':'App Store RSS 兼容列表'}.get(source,source))
            if 'rss' in source.lower() or 'RSS' in str(run.get('detail') or ''):
                st.caption('RSS 为兼容回退来源，可见分页可能少于公开评论列表；回退结果不能证明完整历史已采完。')
            if run.get('stop_reason'): st.write('停止原因：'+str(run['stop_reason']))
            if run.get('detail'): st.text(run['detail'])
    st.caption(f'共 {count} 条记录 · 每页 10 条。采集方式、日期范围与扫描上限可在左侧“采集与设置”调整。')


def render_collection_log(db):
    active=st.session_state.get('collection_dialog')
    if not active: return
    if active['scope']!=st.session_state.get('scope',''):
        close_collection_log(); return
    collection_log_dialog(db,active['profile'])


@st.fragment(run_every='3s')
def collection_live_totals(db,profile):
    """Refresh persisted totals independently of controls and analysis charts."""
    totals=collection_totals(db,profile['app_id'],profile['country'])
    left,span=st.columns([1.25,3])
    left.markdown(f'<div class="collection-summary"><span>累计已采集</span><strong>{totals["total"]:,}<small> 条评论</small></strong></div>',unsafe_allow_html=True)
    period=(local_time(totals['oldest_date'])+' — '+local_time(totals['newest_date'])) if totals['oldest_date'] else '暂无有效评论日期'
    span.markdown('<div class="collection-summary collection-period"><span>评论时间 · 北京时间</span><strong>'+html.escape(period)+'</strong></div>',unsafe_allow_html=True)


def collection_summary(db,profile,raw,state,runs):
    summary,collect,logs=st.columns([4.25,1.4,1])
    with summary:
        collection_live_totals(db,profile)
    busy=collection_busy(state)
    label='已有采集任务' if busy else '立即采集最新评论'
    if collect.button(label,key='collect_latest',type='primary',disabled=busy,width='stretch'):
        request_collection(db,profile['app_id'],profile['country'],pages=100)
        st.rerun()
    logs.button('采集日志',key='collection_log_open',width='stretch',on_click=open_collection_log,args=(profile,))
    collection_live_progress(db,profile)


@st.fragment(run_every='3s')
def collection_live_progress(db,profile):
    """Refresh only this compact progress panel while large jobs write pages."""
    state=target_state(db,profile['app_id'],profile['country'])
    run=collection_progress(db,profile['app_id'],profile['country'])
    if not run and not state.get('requested'): return
    st.markdown(progress_card(run,state),unsafe_allow_html=True)
    status=state.get('active_status') or run.get('status')
    active=status in ACTIVE_STATUSES or bool(state.get('requested'))
    paused=status=='已暂停'
    awaiting_control=status=='采集中' and state.get('active_control') in ('pause','cancel')
    queued_without_run=bool(state.get('requested') and not state.get('active_run_id'))
    prefix=f"collection_live_{profile['app_id']}_{profile['country']}"
    pause,resume,cancel,_=st.columns([1,1,1.5,4])
    action=None
    if pause.button('暂停采集',key=prefix+'_pause',disabled=not active or awaiting_control or queued_without_run,width='stretch'): action='pause'
    if resume.button('继续采集',key=prefix+'_resume',disabled=not paused,width='stretch'): action='resume'
    if cancel.button('结束本次采集',key=prefix+'_cancel',disabled=not(active or paused) or awaiting_control,width='stretch',help='结束本次任务，已采集的评论全部保留。'): action='cancel'
    if action:
        if control_collection(db,profile['app_id'],profile['country'],action=action):
            st.rerun()
        else:
            st.info('任务状态刚刚发生变化，请稍候刷新。')


def collection_request_form(db,profile,state):
    st.subheader('本次采集范围')
    prefix=f"collect_{profile['app_id']}_{profile['country']}"
    mode=st.radio('本轮采集方式',list(MODES),horizontal=True,key=prefix+'_mode')
    explanations={
        'latest':'持续监控新评论，并衔接上一次成功采集的时间。',
        'date_range':'从最新向前翻页，读到目标起始日期后停止，仅保存所选日期范围内的评论。',
        'history':'从最新评论开始持续向前回溯，建立可长期复用的本地历史评论库。',
    }
    st.caption(explanations[MODES[mode]])
    today=datetime.now(LOCAL_TZ).date()
    with st.expander('采集数量上限',expanded=False):
        mode_key=prefix+'_'+MODES[mode]
        budget=st.selectbox('本次最多扫描',list(SCAN_LIMITS)+['自定义数量'],
            index=1 if MODES[mode]=='latest' else 2,key=mode_key+'_budget')
        if budget=='自定义数量':
            count=int(st.number_input('最多扫描评论数',min_value=50,max_value=MAX_SCAN_REVIEWS,
                value=10000,step=50,key=mode_key+'_count'))
        else: count=SCAN_LIMITS[budget]
        pages=scan_pages(count)
        st.caption(f'最多扫描约 {pages*50:,} 条，每批通常约 50 条。这个上限包含重复评论、日期范围外评论，不等于新增条数。')
        st.caption('采集分段保存，可暂停和继续；达到上限、已越过起始日期或源站不再返回数据时结束，实际覆盖记录在日志中。')
    with st.form(prefix+'_request'):
        dates=()
        if MODES[mode]=='date_range':
            dates=st.date_input('目标日期范围（北京时间，包含起止两天）',
                value=(today-timedelta(days=6),today),max_value=today,key=prefix+'_dates')
        st.caption(f"{MODE_LABELS[MODES[mode]]} · 本次最多扫描约 {pages*50:,} 条")
        submit=st.form_submit_button('按以上设置开始采集',disabled=collection_busy(state),type='primary')
    if submit:
        if MODES[mode]=='date_range' and len(dates)!=2:
            st.error('请选择起始日期和结束日期。')
        else:
            kwargs={'mode':MODES[mode],'pages':int(pages)}
            if MODES[mode]=='date_range':
                kwargs.update(mode='date_range',start_at=pd.Timestamp(dates[0],tz=LOCAL_TZ).isoformat(),
                    end_at=min(pd.Timestamp(dates[1]+timedelta(days=1),tz=LOCAL_TZ),pd.Timestamp.now(tz=LOCAL_TZ)).isoformat())
            queued=request_collection(db,profile['app_id'],profile['country'],**kwargs)
            if queued:
                st.toast('已加入采集队列。进度将在上方持续更新。')
                st.rerun()
            else: st.info('已有采集任务，可继续、暂停或结束当前任务后再开始。')
    if collection_busy(state): st.caption('当前已有采集任务，进度与暂停 / 继续入口见上方。')
