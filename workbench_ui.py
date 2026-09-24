from __future__ import annotations

import html
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from dashboard_ui import clear_dialog, render_dashboard, render_active_dialog
from agent_ui import agent_worker, analysis_data, analysis_panel, analysis_settings, report_plans, render_analysis_reviews
from collection_ui import collection_summary, collection_request_form, render_collection_log, close_collection_log
from modules.agent_analysis import analysis_revision, latest_report
from modules.dashboard import region_comparison, categories_for, category_mask
from modules.app_icons import game_hero_html, get_app_icon
from modules.collection_coverage import coverage_calendar, run_details, run_request
from modules.bundled_snapshot import ensure_bundled_snapshot, snapshot_info
from collectors.app_store import AppStoreCollector
from modules.alerts import publish_alerts
from modules.deliverables import build_report, workbook
from modules.imports import parse_import
from modules.issue_classifier import ANALYSIS_RULES_VERSION
from modules.monitoring import configure_target, recent_runs, request_collection, start_worker, target_state
from modules.operations import LOCAL_TZ, analyze_reviews, dataset_hash, issue_plans, snapshot
from modules.review_exports import export_reviews_by_game
from modules.review_store import init_db, list_game_profiles, read_reviews, save_reviews, upsert_game_profile
from modules.storage import backup_database, database
from modules.version_history import get_version_history
from modules.version_attribution import attribute_versions, review_version_text
from version_ui import version_history_controls

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('APPSTORE_DATA_DIR',str(ROOT/'data')))
DB=DATA/'reviews.sqlite3'
OUTPUT=DATA.parent/'outputs'
MARKETS={'中国大陆':'cn','美国':'us','中国香港':'hk','中国台湾':'tw','日本':'jp','韩国':'kr',
         '英国':'gb','德国':'de','法国':'fr','加拿大':'ca','澳大利亚':'au','新加坡':'sg','其他地区代码':'other'}
BUNDLED_REPORT_WINDOW='已完成 Agent 报告时段'


def local_time(value):
    if not value or pd.isna(value): return '暂无记录'
    return pd.Timestamp(value).tz_convert(LOCAL_TZ).strftime('%Y-%m-%d %H:%M') if pd.Timestamp(value).tzinfo else str(value)


def apply_snapshot_defaults(info,labels):
    """Choose an immediately readable snapshot once, without replacing user choices."""
    if not info or st.session_state.get('bundled_snapshot_defaults_applied'):
        return
    preferred=next((label for label,row in labels.items()
        if str(row.get('app_id'))==str(info.get('preferred_app_id'))
        and str(row.get('country')).lower()==str(info.get('preferred_country')).lower()),None)
    if preferred and 'current_game' not in st.session_state:
        st.session_state.current_game=preferred
    if 'statistics_window' not in st.session_state:
        st.session_state.statistics_window='全部已采集历史'
    for row in labels.values():
        key=f"agent_{row['app_id']}_{row['country']}_preference"
        if key not in st.session_state:
            st.session_state[key]='规则初筛'
    st.session_state.bundled_snapshot_defaults_applied=True


def _bundled_report(info,profile):
    """Show only the fixed, checksum-verified report for this game and market."""
    report=info.get('report_summary')
    if (not isinstance(report,dict) or report.get('filename')!='agent-report.md'
            or str(report.get('app_id'))!=str(profile.get('app_id'))
            or str(report.get('country')).lower()!=str(profile.get('country')).lower()):
        return None
    try:
        total,analyzed=int(report['total']),int(report['analyzed'])
        start,end=pd.Timestamp(report['start_at']),pd.Timestamp(report['end_at'])
        if total<=0 or analyzed!=total or start.tzinfo is None or end.tzinfo is None or not start<end:
            return None
        body=(ROOT/'bundled_data'/'agent-report.md').read_bytes()
        if hashlib.sha256(body).hexdigest()!=report.get('sha256'):
            return None
        return report,body.decode('utf-8')
    except (OSError,UnicodeError,ValueError,TypeError,KeyError):
        return None


def open_bundled_report(app_id,country):
    """Select the saved report without scheduling analysis or changing model settings."""
    prefix=f'agent_{app_id}_{country}'
    st.session_state[prefix+'_mode']='Agent分析'
    st.session_state[prefix+'_preference']='Agent分析'
    st.session_state.statistics_window=BUNDLED_REPORT_WINDOW
    st.session_state.workspace_page='舆情概览'


def snapshot_caption(info,profile):
    if not info: return
    count=int(info.get('review_count') or 0)
    stamp=local_time(info.get('created_at'))
    st.caption(f'随附真实 App Store 评论快照 · 初始全库 {count:,} 条 · 生成于 {stamp}（北京时间）。'
        '快照不代表实时评论；新增数据以实际采集记录为准。')
    saved=_bundled_report(info,profile)
    if saved:
        report,body=saved
        st.button(f"查看完整 Agent 分析 · {int(report['analyzed']):,} 条",type='primary',
            key='bundled_agent_report_open',on_click=open_bundled_report,
            args=(profile['app_id'],profile['country']),
            help='打开已完成报告对应的统计时段，在同一看板查看深度分析、图表及处理方案，不启动新分析。')
        st.caption('已完成历史报告 · '+local_time(report['start_at'])+' — '+local_time(report['end_at'])+
            f"（北京时间）· 模型 {report.get('model') or '报告内注明'}。点击后显示该时段的完整分析看板。")
        with st.expander(f"查看随附 Agent 历史报告 · {int(report['analyzed']):,} 条",expanded=False):
            st.caption('独立历史报告 · '+local_time(report['start_at'])+' — '+local_time(report['end_at'])+
                f"（北京时间，结束时间不含）· 已分析 {int(report['analyzed']):,}/{int(report['total']):,} 条。"
                '此固定区间不随当前看板筛选变化，阅读不会启动新的 Agent 分析。')
            st.markdown(body)
            st.download_button('下载随附 Agent 历史报告',body,file_name='agent-report.md',
                mime='text/markdown',key='bundled_agent_report_download',on_click='ignore')


@st.cache_resource
def worker(path):
    return start_worker(Path(path),OUTPUT/'collector_logs')


@st.cache_data(max_entries=12,show_spinner=False)
def analyzed(raw, revision, path):
    return analyze_reviews(raw,Path(path))


def dashboard_update_signature(db, app_id, country):
    """Refresh analysis after collection settles; page writes use their own fragment."""
    with database(db) as connection:
        latest=connection.execute('SELECT id,status,finished_at,fetched,inserted,updated FROM collection_runs WHERE app_id=? AND country=? ORDER BY id DESC LIMIT 1',
            (str(app_id),country)).fetchone()
        target=connection.execute('SELECT requested,last_status,last_success,enabled,next_due FROM monitor_targets WHERE app_id=? AND country=?',
            (str(app_id),country)).fetchone()
        collecting=bool((target and target['requested']) or (latest and latest['status'] in ('采集中','等待续采','等待重试','排队中')))
        queries = [
            ("SELECT value FROM agent_meta WHERE key='revision'",()),
            ('SELECT COALESCE(MAX(id),0) FROM audit_log',()),
            ('SELECT id,status,stage,processed,total,error FROM agent_runs WHERE app_id=? AND country=? ORDER BY id DESC LIMIT 1',(str(app_id),country)),
        ]
        signature=[]
        for sql,parameters in queries:
            row=connection.execute(sql,parameters).fetchone()
            signature.append(tuple(row) if row else None)
        if collecting:
            # Large collections save each page. Avoid rebuilding every chart while
            # those counters advance; live collection progress refreshes separately.
            signature.append(('collection_active',str(app_id),country,bool(target and target['enabled'])))
        else:
            reviews=connection.execute('SELECT COUNT(*),MAX(collected_at) FROM review_records WHERE app_id=?',(str(app_id),)).fetchone()
            signature.extend((tuple(reviews),tuple(target) if target else None,tuple(latest) if latest else None))
    try:
        signature.append((Path(db).parent/'app_versions'/f'{app_id}_{country}.json').stat().st_mtime_ns)
    except OSError:
        signature.append(None)
    return tuple(signature)


@st.fragment(run_every='15s')
def poll_dashboard_updates(db, app_id, country, scope):
    # This fragment owns no interactive UI or variable-length chart/report tree.
    # Streamlit 1.64 can replay obsolete delta paths when a timed fragment's
    # dynamic body is interrupted by successive radio/number-input changes.
    # User controls therefore belong to the normal app run; only this small
    # observer runs on a timer and requests a full rerun when data changes.
    if st.session_state.get('scope')!=scope or st.session_state.get('dashboard_dialog') or st.session_state.get('collection_dialog'):
        return
    signature=dashboard_update_signature(db,app_id,country)
    key='dashboard_update_signature'
    if signature!=st.session_state.get(key):
        st.session_state[key]=signature
        st.rerun(scope='app')


def add_game():
    st.subheader('添加任何 App Store 游戏')
    market=st.selectbox('商店地区',list(MARKETS))
    country=MARKETS[market]
    if country=='other': country=st.text_input('两位商店地区代码',placeholder='例如 th、br、it').strip().lower()
    term=st.text_input('游戏名称',placeholder='输入游戏名称，搜索对应地区的商店')
    if st.button('搜索 App Store'):
        if len(country)!=2 or not country.isalpha(): st.error('请输入两位地区代码')
        else:
            client=AppStoreCollector(OUTPUT/'collector_logs',timeout=8)
            try:
                with st.spinner('搜索游戏…'): results,errors=client.search_apps(term,country)
            finally: client.session.close()
            st.session_state.search_results=results
            st.session_state.search_country=country
            for error in errors: st.error(error)
    results=st.session_state.get('search_results',[]) if st.session_state.get('search_country')==country else []
    if results:
        selected=st.selectbox('确认游戏与发行商',range(len(results)),format_func=lambda i:f"{results[i]['name']} · {results[i]['seller']} · {results[i]['app_id']}")
        result=results[selected]
        if result['url'].startswith('https://apps.apple.com/'): st.link_button('核对商店页面',result['url'])
        if st.button('添加所选游戏',type='primary'):
            upsert_game_profile(DB,app_id=result['app_id'],country=country,app_name=result['name'],seller=result['seller'],bundle_id=result['bundle_id'],track_url=result['url'])
            get_app_icon(result['app_id'],country,DATA/'app_icons',result.get('artwork_url',''),
                allow_fetch=os.environ.get('APPSTORE_DISABLE_ICON_FETCH')!='1')
            configure_target(DB,result['app_id'],country)
            get_version_history(result['app_id'],country,DATA/'app_versions',allow_fetch=True)
            st.session_state.next_game=f"{result['name']} · {country} ({result['app_id']})"
            st.success('已添加。选择该游戏后可立即采集，或在巡检设置中启用自动监控。')
            st.rerun()
    with st.expander('已知 App ID，直接添加'):
        with st.form('manual_game'):
            app_id=st.text_input('数字 App ID',placeholder='商店网址 id 后的数字')
            name=st.text_input('游戏名称（用于识别）')
            submitted=st.form_submit_button('保存游戏')
        if submitted:
            if not app_id.isdigit() or len(country)!=2 or not country.isalpha() or not name.strip():
                st.error('请填写名称、数字 App ID 和两位地区代码')
            else:
                upsert_game_profile(DB,app_id=app_id,country=country,app_name=name.strip())
                configure_target(DB,app_id,country)
                get_version_history(app_id,country,DATA/'app_versions',allow_fetch=True)
                st.session_state.next_game=f"{name.strip()} · {country} ({app_id})"
                st.rerun()


def source_banner(profile,raw,state,runs):
    collection_summary(DB,profile,raw,state,runs)


def with_version_attribution(data,profile):
    history=get_version_history(profile['app_id'],profile['country'],DATA/'app_versions')
    return attribute_versions(data,history)


def evidence_card(item,profile=None):
    if profile is not None:
        item=dict(item)
        item.setdefault('app_id',profile['app_id'])
        item.setdefault('country',profile['country'])
        item=with_version_attribution(pd.DataFrame([item]),profile).iloc[0].to_dict()
    escape=lambda value:html.escape(str(value or ''))
    content=str(item.get('content') or '')
    body=escape(content[:240])
    if len(content)>240:
        body+='…<details><summary>展开完整原文</summary><div class="evidence-full">'+escape(content)+'</div></details>'
    url=str(item.get('url') or '')
    parsed=urlparse(url)
    source=''
    if parsed.scheme=='https' and (parsed.hostname or '') in ['apps.apple.com','itunes.apple.com']:
        source=f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">查看商店来源 ↗</a>'
    st.markdown('<article class="plan-evidence"><div class="evidence-top">'
        '<span class="evidence-label">代表评论 · 原文</span><span class="evidence-rating">'
        +escape(item.get('rating'))+' 星</span></div><div class="evidence-title">'
        +escape(item.get('title') or '未填写标题')+'</div><div class="evidence-body">'+body
        +'</div><div class="evidence-meta">#'+escape(item.get('review_id'))+' · '
        +escape(local_time(item.get('date')))+'<br>'+escape(review_version_text(item))
        +'</div>'+source+'</article>',unsafe_allow_html=True)


def _browser_status(data, mode):
    """Attach the same review status labels used by the analysis reader.

    The monitoring page is intentionally a direct evidence browser.  Keeping
    the status calculation here in one small helper prevents the page from
    silently showing rule labels for reviews that are still waiting on Agent.
    """
    result = data.copy()
    manual = result.get('manual_reviewed', pd.Series(False, index=result.index)).fillna(False).astype(bool)
    uncertain = result.get('needs_review', pd.Series(True, index=result.index)).fillna(True).astype(bool)
    result['_browser_status'] = '已初筛'
    if mode == 'Agent分析':
        completed = result.get('agent_analyzed', pd.Series(False, index=result.index)).fillna(False).astype(bool)
        result['_browser_status'] = '待分析'
        result.loc[completed, '_browser_status'] = '已分析'
        result.loc[completed & uncertain, '_browser_status'] = '待复核'
    else:
        result.loc[uncertain, '_browser_status'] = '待复核'
    result.loc[manual, '_browser_status'] = '人工复核'
    return result


def _browser_card(row):
    """Render one readable, colour-coded review card for the overview browser."""
    esc = lambda value: html.escape(str(value or ''))
    status = str(row.get('_browser_status') or '待复核')
    sentiment = str(row.get('sentiment_label') or '待复核')
    rating = row.get('rating')
    rating_text = f'{float(rating):g} 星' if pd.notna(rating) else '星级未知'
    categories = [str(item) for item in categories_for(row) if str(item).strip()]
    tags = ''.join(f'<span class="comment-browser-tag category">{esc(item)}</span>' for item in categories[:4])
    if not tags:
        tags = '<span class="comment-browser-tag muted">未归类</span>'
    sentiment_class = {'好评':'good','差评':'bad','中评':'neutral'}.get(sentiment,'unknown')
    content = str(row.get('content') or '')
    body = esc(content)
    if len(content) > 500:
        body = esc(content[:500]) + '…<details><summary>展开完整评论</summary><div>' + esc(content) + '</div></details>'
    title = esc(row.get('title') or '未填写标题')
    date = esc(local_time(row.get('date')))
    version_text=review_version_text(row)
    version_kind='inferred' if row.get('version_basis')=='date_inferred' else ('source' if '来源提供' in version_text else 'unknown')
    status_class = {'已分析':'done','待分析':'pending','待复核':'review','人工复核':'manual'}.get(status,'screened')
    extra = ''
    if bool(row.get('agent_analyzed', False)) and status != '人工复核':
        reason = esc(row.get('agent_reason') or '已完成逐条 Agent 判断')
        quote = esc(row.get('agent_quote') or '')
        demand = esc(row.get('agent_demand') or '当前结果未提供明确诉求。')
        extra = f'<div class="comment-browser-detail"><b>判断理由</b> {reason}<br><b>玩家诉求</b> {demand}' + (f'<br><b>原文依据</b> “{quote}”' if quote else '') + '</div>'
    elif status == '待分析':
        extra = '<div class="comment-browser-detail pending-text">Agent 尚未分析，先保留原文与星级。</div>'
    else:
        extra = f'<div class="comment-browser-detail"><b>分析依据</b> {esc(row.get("analysis_basis") or "阅读原文后确认")}</div>'
    return ('<article class="comment-browser-card">'
            f'<div class="comment-browser-head"><div><span class="comment-browser-status {status_class}">{esc(status)}</span>'
            f'<span class="comment-browser-sentiment {sentiment_class}">{esc(sentiment)}</span>'
            f'<span class="comment-browser-rating">{esc(rating_text)}</span></div><time>{date}</time></div>'
            f'<h4>{title}</h4><div class="comment-browser-body">{body}</div>'
            f'<div class="comment-browser-tags">{tags}</div>{extra}'
            f'<div class="comment-browser-meta">#{esc(row.get("review_id"))} · {esc(row.get("author") or "匿名玩家")} · App Store · <span class="version-badge {version_kind}">{esc(version_text)}</span></div>'
            '</article>')


def render_review_browser(profile, request=None):
    """A compact, filterable evidence browser for the monitoring overview."""
    with database(DB) as connection:
        total = connection.execute('SELECT COUNT(*) FROM review_records WHERE app_id=? AND country=?',
                                   (str(profile['app_id']), str(profile['country']).lower())).fetchone()[0]
        revision = connection.execute('SELECT COALESCE(MAX(id),0) FROM audit_log').fetchone()[0]
    if not total:
        st.info('当前游戏还没有已采集评论。先在“采集与设置”中完成一次采集。')
        return
    mode_key = f"agent_{profile['app_id']}_{profile['country']}_preference"
    prefix = f"browser_{profile['app_id']}_{profile['country']}"
    mode = 'Agent分析' if st.session_state.get(mode_key) == 'Agent分析' else '规则初筛'
    if request and request.get('mode') in ('Agent分析','规则初筛'):
        mode = request['mode']
        st.session_state[mode_key] = mode
        st.session_state[prefix+'_mode'] = mode
    if st.session_state.get(prefix+'_mode') not in ('Agent分析','规则初筛'):
        st.session_state[prefix+'_mode'] = mode
    mode = st.selectbox('评论分析方式', ['Agent分析','规则初筛'], key=prefix+'_mode',
                        help='只切换评论浏览使用的结果来源，不会启动新的分析任务。')
    st.session_state[mode_key] = mode
    st.caption(f"当前浏览：{profile['app_name']} · {profile['country']} · {mode}。在状态筛选中选择“待复核”，查看需要人工确认的评论。")
    # A request from the dashboard is applied once, before widgets are built.
    requested_status = request.get('status') if request else None
    requested_start = request.get('start') if request else None
    requested_end = request.get('end') if request else None
    if requested_start and requested_end:
        st.session_state[prefix+'_range'] = '看板统计范围'
        st.session_state[prefix+'_requested_dates'] = (str(requested_start), str(requested_end))
    if requested_status in {'待复核','待分析','已分析','人工复核','已初筛'}:
        st.session_state[prefix+'_status'] = requested_status
    controls = st.columns([1.05, 1.05, 1.15, 1.15])
    range_options = ['近 7 天','近 30 天','全部已采集','看板统计范围','自定义日期','最新 N 条']
    if st.session_state.get(prefix+'_range') not in range_options:
        st.session_state[prefix+'_range'] = '近 7 天'
    range_mode = controls[0].selectbox('浏览范围', range_options, key=prefix+'_range')
    today = datetime.now(LOCAL_TZ).date()
    read_options = {'app_id':profile['app_id'], 'country':profile['country']}
    start = end = None
    if range_mode in ('近 7 天','近 30 天'):
        days = 7 if range_mode == '近 7 天' else 30
        start = pd.Timestamp(today - timedelta(days=days-1), tz=LOCAL_TZ)
        end = pd.Timestamp(today + timedelta(days=1), tz=LOCAL_TZ)
    elif range_mode == '看板统计范围':
        requested = st.session_state.get(prefix+'_requested_dates')
        if requested:
            try:
                start, end = pd.Timestamp(requested[0]), pd.Timestamp(requested[1])
                if start.tzinfo is None: start = start.tz_localize(LOCAL_TZ)
                else: start = start.tz_convert(LOCAL_TZ)
                if end.tzinfo is None: end = end.tz_localize(LOCAL_TZ)
                else: end = end.tz_convert(LOCAL_TZ)
                if pd.isna(start) or pd.isna(end) or start >= end: raise ValueError('无效日期范围')
            except (TypeError, ValueError):
                st.info('看板统计范围无效，请重新从看板打开，或改选其他浏览范围。')
                return
        else:
            st.info('当前没有可复用的看板统计范围，请改选其他浏览范围。')
            return
    elif range_mode == '自定义日期':
        date_value = controls[1].date_input('日期范围', value=(today-timedelta(days=6), today), max_value=today, key=prefix+'_dates')
        if not isinstance(date_value, (tuple, list)) or len(date_value) != 2:
            st.info('请选择完整的开始日期和结束日期。')
            return
        start = pd.Timestamp(date_value[0], tz=LOCAL_TZ)
        end = pd.Timestamp(date_value[1] + timedelta(days=1), tz=LOCAL_TZ)
    elif range_mode == '最新 N 条':
        max_count = max(1, total)
        default_count = min(50, max_count)
        if prefix+'_count' in st.session_state:
            st.session_state[prefix+'_count'] = min(max_count, max(1, int(st.session_state[prefix+'_count'])))
            count_options = {}
        else:
            count_options = {'value':default_count}
        count = controls[1].number_input('条数', min_value=1, max_value=max_count, step=1, key=prefix+'_count', **count_options)
        read_options['limit'] = int(count)
    elif total > 10000:
        st.caption(f'将读取全部 {total:,} 条已采集评论，首次筛选可能较慢；可选择日期范围或最新 N 条加快浏览。')
    if start is not None:
        read_options.update(start_at=start.isoformat(), end_at=end.isoformat())
    # Restrict rows in SQLite before loading text or computing local analysis.
    # The explicit all-collected option still includes every stored review.
    raw = read_reviews(DB, **read_options)
    selected = _browser_status(analysis_data(analyzed(raw, (revision, analysis_revision(DB)), str(DB)), mode), mode)
    selected = with_version_attribution(selected,profile)
    status_options = ['全部状态'] + [value for value in ['待复核','待分析','已分析','人工复核','已初筛'] if value in set(selected['_browser_status'])]
    categories = ['全部类别'] + sorted({category for _, row in selected.iterrows() for category in categories_for(row)})
    for suffix, choices in [('_status',status_options),('_category',categories)]:
        if st.session_state.get(prefix+suffix) not in choices: st.session_state[prefix+suffix] = choices[0]
    filter_cols = st.columns(3)
    status = filter_cols[0].selectbox('快速筛选状态', status_options, key=prefix+'_status',
        help='“待复核”表示已有分析结果但仍需人工确认；“待分析”表示尚无有效 Agent 分析结果。')
    if status == '待复核':
        filter_cols[0].caption('已分析中待复核：逐条判断完成，但需要人工确认。')
    category = filter_cols[1].selectbox('问题类别', categories, key=prefix+'_category')
    sentiment_options = ['全部情绪'] + [value for value in ['差评','中评','好评','待复核'] if value in set(selected['sentiment_label'])]
    if st.session_state.get(prefix+'_sentiment') not in sentiment_options:
        st.session_state[prefix+'_sentiment'] = sentiment_options[0]
    sentiment = filter_cols[2].selectbox('情绪筛选', sentiment_options, key=prefix+'_sentiment')
    version_cols=st.columns(2)
    version_options=['全部版本']+sorted(set(selected['version_display']))
    if st.session_state.get(prefix+'_version') not in version_options:
        st.session_state[prefix+'_version']='全部版本'
    selected_version=version_cols[0].selectbox('版本归属',version_options,key=prefix+'_version')
    basis_labels={'全部依据':None,'来源提供':'source','按日期推定':'date_inferred','版本未能确定':'unknown'}
    selected_basis=version_cols[1].selectbox('版本依据',list(basis_labels),key=prefix+'_version_basis')
    query = st.text_input('搜索评论', placeholder='输入标题、正文或关键词', key=prefix+'_query').strip()
    if status != '全部状态': selected = selected.loc[selected['_browser_status'].eq(status)]
    if category != '全部类别': selected = selected.loc[category_mask(selected, category)]
    if sentiment != '全部情绪': selected = selected.loc[selected['sentiment_label'].eq(sentiment)]
    if selected_version!='全部版本': selected=selected.loc[selected['version_display'].eq(selected_version)]
    if basis_labels[selected_basis]: selected=selected.loc[selected['version_basis'].eq(basis_labels[selected_basis])]
    if query:
        haystack = selected['title'].fillna('').astype(str) + ' ' + selected['content'].fillna('').astype(str)
        selected = selected.loc[haystack.str.contains(query, case=False, regex=False)]
    selected = selected.sort_values(['date','review_id'], ascending=[False,False])
    st.markdown(f'<div class="comment-browser-summary"><strong>{len(selected):,}</strong> 条匹配评论 <span>当前库共 {total:,} 条 · 可按时间、数量、状态和类别筛选</span></div>', unsafe_allow_html=True)
    version_counts=selected['version_basis'].value_counts()
    st.caption(f"版本依据：来源提供 {version_counts.get('source',0):,} 条 · 按日期推定 {version_counts.get('date_inferred',0):,} 条 · 未能确定 {version_counts.get('unknown',0):,} 条")
    if selected.empty:
        st.info('当前筛选没有匹配评论。可以切换日期范围，或点击“全部状态”。')
        return
    fingerprint = (range_mode,status,category,sentiment,selected_version,selected_basis,query,tuple(selected['review_id']))
    if st.session_state.get(prefix+'_filters') != fingerprint:
        st.session_state[prefix+'_filters'] = fingerprint
        st.session_state[prefix+'_page'] = 1
    pages = max(1, (len(selected)+7)//8)
    current_page = min(max(1, int(st.session_state.get(prefix+'_page', 1))), pages)
    st.session_state[prefix+'_page'] = current_page
    if prefix+'_page' in st.session_state:
        page = st.number_input('评论页码', min_value=1, max_value=pages, step=1, key=prefix+'_page')
    else:
        page = st.number_input('评论页码', min_value=1, max_value=pages, value=1, step=1, key=prefix+'_page')
    st.caption(f'第 {int(page)} / {pages} 页 · 每页最多 8 条 · 按最新评论排序')
    st.caption('优先显示来源提供的版本；缺失时按同地区商店发布时间推定，并明确标注。推定代表评论所属发布时期，不代表玩家实际安装版本。可在“采集与设置 → 版本归属与发布时间”同步记录。')
    visible = selected.iloc[(int(page)-1)*8:int(page)*8]
    display = st.radio('评论展示', ['评论卡片','彩色表格'],horizontal=True,key=prefix+'_display')
    if display == '彩色表格':
        table = pd.DataFrame({'状态':visible['_browser_status'],'评论时间':visible['date'].map(local_time),
            '情绪':visible['sentiment_label'],'星级':visible['rating'], '标题':visible['title'],
            '评论原文':visible['content'],'问题类别':visible.apply(lambda row:'、'.join(categories_for(row)),axis=1),
            '评论版本':visible.apply(review_version_text,axis=1)})
        colors = {'好评':('#213b3b','#91e5ce'),'差评':('#422b3e','#ffafc4'),'中评':('#243750','#a2d0ff'),'待复核':('#393349','#d3bffc')}
        def row_style(row):
            bg,fg = colors.get(row['情绪'],('#272d40','#d7dff0'))
            return [f'background-color:{bg};color:{fg};']*len(row)
        styled = table.style.apply(row_style,axis=1).format({'星级':'{:.0f}'},na_rep='—')
        st.dataframe(styled,hide_index=True,width='stretch',row_height=70,
                     column_config={'评论原文':st.column_config.TextColumn(width='large'), '标题':st.column_config.TextColumn(width='medium')})
        choices = visible['review_id'].tolist()
        if st.session_state.get(prefix+'_read') not in choices: st.session_state[prefix+'_read'] = choices[0]
        review_id = st.selectbox('展开原文与分析依据',choices,key=prefix+'_read',
            format_func=lambda value:f"#{value} · {visible.loc[visible['review_id'].eq(value),'title'].iloc[0] or '未填写标题'}")
        visible = visible.loc[visible['review_id'].eq(review_id)]
    for _, row in visible.iterrows():
        st.markdown(_browser_card(row), unsafe_allow_html=True)


def overview(profile,view,plans,regions):
    render_dashboard(profile,view,plans,regions)
    render_analysis_reviews(view)
    if view.get('analysis_mode')=='Agent分析':
        report_plans(view,lambda item:evidence_card(item,profile))
        with st.expander('统计口径与数据质量'):
            st.write(f"无日期：{view['undated']} 条；未来日期：{view['future']} 条；不计入当前窗口。")
            st.write('情绪与问题类别采用 Agent 分析及有效人工复核；未分析评论保留为待判定与未归类，不借用规则结果。原始星级与关键词统计覆盖本期所有评论。')
            st.write('Agent 结论是基于已采集评论的研判，不能代替故障日志或实际用户行为数据。报告中的原因假设需要核实。')
        return
    st.subheader('具体处理方案与代表评论')
    st.caption('规则处理参考：每个主题提供固定的核实步骤、处理方法与效果验收，结合原文确认后使用。')
    if not plans: st.info('当前区间没有可生成处理方案的负面主题。')
    for i,plan in enumerate(plans):
        priority='p1' if plan['priority']=='P1' else 'p2'
        badge=':orange-background[P1 · 优先核实]' if priority=='p1' else ':violet-background[P2 · 常规跟进]'
        label=f"{badge}　**{plan['category']}**　· 本期 {plan['count']} 条 / 上期 {plan['previous_count']} 条"
        with st.container(key=f'plan_{priority}_{i}'), st.expander(label,expanded=i==0):
            st.markdown('<div class="plan-trigger"><span>关注信号</span>'
                +html.escape(plan['trigger'])+'</div><div class="plan-meta">'
                '<div><span>建议协作</span><strong>'+html.escape(plan['owner'])+'</strong></div>'
                '<div><span>首次处理</span><strong>'+html.escape(plan['first_response'])+'</strong></div></div>',unsafe_allow_html=True)
            steps=''
            for number,label,field in [('01','核实事实','verify'),('02','核心处理方法','action'),('03','验收与回看','validation')]:
                steps+=f'<section class="plan-step {field}"><div class="step-heading"><span>{number}</span><strong>{label}</strong></div><p>{html.escape(plan[field])}</p></section>'
            st.markdown('<div class="plan-steps">'+steps+'</div>',unsafe_allow_html=True)
            st.caption(f"来源记录的版本分布：{plan['versions'] or '缺少原始版本信息，可查看下方评论的日期推定'}")
            if plan['evidence']:
                for col,item in zip(st.columns(2),plan['evidence'][:2]):
                    with col: evidence_card(item,profile)
                st.caption('保留本地评论原文；商店链接不一定能定位到单条评论。')
            with st.container(key=f'plan_response_{i}',border=True):
                st.markdown('**商店回复参考 · 使用前确认事实**')
                st.text(plan['response_draft'])
                st.text(plan['response_draft_en'])
    with st.expander('统计口径与数据质量'):
        st.write(f"无日期：{view['undated']} 条；未来日期：{view['future']} 条；不计入当前窗口。")
        st.write('正文采用可解释的中英文规则，常见繁体表达做归一化；语言、语义或星级冲突保留为待判定。图中待复核表示情绪尚不能可靠判定。')
        st.write('有效样本至少 30 条、低星至少 10 条；较等长上期增加至少 15 个百分点提示优先核查；低星占比至少 40% 提示持续关注。阈值需结合项目实际验证。')


def collection_settings(profile,state,runs):
    collection_request_form(DB,profile,state)
    version_history_controls(profile,DATA/'app_versions')
    st.divider()
    analysis_settings(DB,profile)
    st.divider()
    st.subheader('自动巡检设置')
    st.write('每次从最新评论向前读取，重扫到上次已接收的最新评论前 24 小时，按评论 ID 去重、保留更新。无法在页数上限内衔接时提示缺口，并保留原有采集水位。')
    st.caption('电脑开机且工具运行时巡检；关闭浏览器不影响采集。休眠或退出期间不采集，重开后补跑到期任务；若缺口早于公开源可见范围，无法仅靠公开源补全。')
    with st.form('monitor_settings'):
        enabled=st.checkbox('启用当前游戏、当前地区自动巡检',value=bool(state.get('enabled')))
        interval=st.selectbox('巡检间隔（分钟）',[15,30,60,120,360,720,1440],index=[15,30,60,120,360,720,1440].index(state.get('interval_minutes',30)))
        count=st.number_input('每轮最多扫描评论数',min_value=50,max_value=100000,
            value=max(50,min(100000,int(state.get('pages',100))*50)),step=50)
        pages=(int(count)+49)//50
        save=st.form_submit_button('保存巡检设置',type='primary')
    if save:
        configure_target(DB,profile['app_id'],profile['country'],enabled,interval,pages)
        st.rerun()
    st.caption('高声量游戏建议每 15—30 分钟巡检，先设每轮最多扫描 5,000 条，再按日志中的衔接情况调整。上限包含重复评论；公开源延迟、限流或历史不可见仍可能造成缺口。')
    if state.get('last_review_at'): st.caption('已成功接收的最新评论水位：'+local_time(state['last_review_at']))
    st.subheader('逐日采集覆盖')
    today=datetime.now(LOCAL_TZ).date()
    selected=st.date_input('查看覆盖日期',value=(today-timedelta(days=6),today),max_value=today,
        key=f"coverage_{profile['app_id']}_{profile['country']}")
    if len(selected)==2:
        st.dataframe(coverage_calendar(DB,profile['app_id'],profile['country'],None,*selected),hide_index=True,width='stretch')
    st.caption('“已遍历”指该时段公开源可见分页已读取，不代表 Apple 已发布该时段的全部评论。零条与无覆盖记录分别展示；导入的历史评论数量也不等于连续采集证明。')
    st.subheader('采集记录')
    if runs:
        records=[]
        for run in runs:
            meta=run_details(run); request=run_request(run)
            records.append({'开始（北京时间）':local_time(run['started_at']),'状态':run['status'],
                '方式':{'date_range':'日期补采','history':'历史回溯'}.get(request.get('mode'),'最新增量'),
                '读取':meta.get('scanned_count',run['fetched']),'区间内':run['fetched'],'新增':run['inserted'],
                '更新':run['updated'],'重复':run['duplicates'],'页数':run['pages'],
                '最早评论':local_time(meta.get('oldest_date') or run.get('oldest_date')),
                '最新评论':local_time(meta.get('newest_date')),'覆盖判断':meta.get('coverage_status','旧记录未验证'),
                '停止原因':run.get('stop_reason'),'详情':run['detail']})
        st.dataframe(pd.DataFrame(records),hide_index=True,width='stretch')
    else: st.info('还没有采集记录。点击上方立即采集开始。')
    with database(DB) as connection:
        row=connection.execute("SELECT value FROM runtime_state WHERE key='heartbeat'").fetchone()
    st.caption('巡检进程最近活动：'+local_time(row[0] if row else None))
    with st.expander('运行诊断详情'):
        st.caption('分析规则：'+ANALYSIS_RULES_VERSION)
        if runs and runs[0].get('detail'): st.text(runs[0]['detail'])
        else: st.caption('最近一轮暂无异常记录。')


def data_tools(profile,view,plans,runs):
    st.subheader('导出本轮分析与处理建议')
    st.caption('当前分析方式：'+view.get('analysis_mode','规则初筛')+'。与舆情概览的选择一致，导出保留分析来源、统计范围、评论原文、采集状态和处理建议。')
    signature=hashlib.sha256((str(profile)+str(view['start'])+str(view['end'])+dataset_hash(view['current'])+str(runs)
        +str(view.get('analysis_mode'))+str(view.get('agent_report'))).encode()).hexdigest()
    if st.button('生成本轮交付包',type='primary'):
        with st.spinner('整理图表数据、评论和分析报告…'):
            md,html,meta=build_report(profile,view,plans,[],runs[0] if runs else None,include_workflow=False)
            st.session_state.delivery=dict(signature=signature,md=md,html=html,xlsx=workbook(profile,view,plans,[],runs,include_workflow=False),meta=meta)
    delivery=st.session_state.get('delivery')
    if delivery and delivery['signature']==signature:
        stem=f"AppStore_{profile['app_id']}_{profile['country']}_{view['end']:%Y%m%d}"
        st.download_button('下载 Excel 分析数据与建议',delivery['xlsx'],stem+'.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',on_click='ignore')
        st.download_button('下载 HTML 处理简报',delivery['html'],stem+'.html',mime='text/html',on_click='ignore')
        st.download_button('下载 Markdown 简报',delivery['md'],stem+'.md',mime='text/markdown',on_click='ignore')
    st.divider()
    st.subheader('导入 App Store 评论')
    st.write(f"目标：{profile['app_name']} · {profile['country']} · {profile['app_id']}。导入文件不会自动启用巡检。")
    template='app_id,country,external_id,date,rating,title,content,author,version\n'
    st.download_button('下载空白 CSV 模板',template.encode('utf-8-sig'),'appstore_import_template.csv',mime='text/csv')
    st.caption('必填 date、rating、content；日期无时区按中国时间。建议保留外部评论 ID，否则只对相同文件去重。模板不包含示例评论，避免混入正式数据。')
    upload=st.file_uploader('上传 CSV / Excel',type=['csv','xlsx','xls'])
    if upload:
        try:
            reviews,errors,mapping,file_hash=parse_import(upload.getvalue(),upload.name,profile['app_id'],profile['country'])
            st.write(f'有效 {len(reviews)} 行；错误 {len(errors)} 行。')
            if errors: st.dataframe(pd.DataFrame(errors),hide_index=True)
            if reviews: st.dataframe(pd.DataFrame([{'日期':r.date,'星级':r.rating,'正文':r.content} for r in reviews[:10]]),hide_index=True)
            confirmed=st.checkbox('我确认这些数据来自所选游戏和地区的 App Store，不包含示例数据',key='confirm_'+file_hash)
            if st.button('导入到当前评论库',disabled=bool(errors) or not reviews or not confirmed):
                result=save_reviews(DB,reviews)
                publish_alerts(DB,profile['app_id'],profile['country'])
                st.success(f'导入完成：新增 {result.inserted}，更新 {result.updated}，重复 {result.duplicates}。')
                st.cache_data.clear()
        except ValueError as exc: st.error(str(exc))
    st.divider()
    st.subheader('备份与版本化导出')
    if st.button('备份完整数据库'):
        target=backup_database(DB)
        st.success('备份完整性检查通过：'+str(target))
    if st.button('导出所有游戏评论到新文件夹'):
        try:
            with st.spinner('生成新版本；已有文件会保留…'): files=export_reviews_by_game(DB,OUTPUT/'review_excels')
            st.success(f"完成 {len(files)} 个表格，位置：{files[0].parent if files else OUTPUT/'review_excels'}")
        except Exception as exc: st.error('本次导出失败，已有文件保留：'+str(exc))
    st.caption('备份目录：'+str(DATA/'backups')+'。恢复方法见项目 README；恢复前需关闭工具。')


def main():
    st.set_page_config(page_title='App Store 舆情工作台',page_icon='📋',layout='wide')
    st.markdown('<style>'+(ROOT/'assets/dashboard.css').read_text(encoding='utf-8')+'</style>',unsafe_allow_html=True)
    init_db(DB)
    ensure_bundled_snapshot(DB,ROOT/'bundled_data')
    bundled=snapshot_info(DB)
    if os.environ.get('APPSTORE_DISABLE_WORKER')!='1':
        worker(str(DB))
        agent_worker(str(DB),str(OUTPUT/'agent_analysis'))
    profiles=list_game_profiles(DB)
    with st.sidebar:
        st.title('App Store\n舆情工作台')
        st.caption('实时监控 → 深度分析 → 处理建议')
        labels={f"{row['app_name']} · {row['country']} ({row['app_id']})":row.to_dict() for _,row in profiles.iterrows()}
        apply_snapshot_defaults(bundled,labels)
        pending=st.session_state.pop('next_game',None)
        if pending in labels: st.session_state.current_game=pending
        review_request=st.session_state.pop('review_browser_request',None)
        if isinstance(review_request,dict):
            requested_label=next((label for label,row in labels.items()
                                  if str(row.get('app_id'))==str(review_request.get('app_id'))
                                  and str(row.get('country'))==str(review_request.get('country'))),None)
            if requested_label: st.session_state.current_game=requested_label
            st.session_state.workspace_page='评论浏览'
            st.session_state.browser_request=review_request
        if st.session_state.get('workspace_page')=='监控总览 / 评论浏览':
            st.session_state.workspace_page='评论浏览'
        choice=st.selectbox('当前游戏与地区',list(labels)+['＋ 添加游戏'],key='current_game')
        page=st.radio('工作区',['舆情概览','监控总览','评论浏览','采集与设置','数据与导出'],key='workspace_page')
        saved_report=_bundled_report(bundled,labels[choice]) if choice in labels else None
        windows=['今天','近 7 天','近 30 天','全部已采集历史','自定义历史区间']
        if saved_report:
            windows.append(BUNDLED_REPORT_WINDOW)
        elif st.session_state.get('statistics_window')==BUNDLED_REPORT_WINDOW:
            st.session_state.statistics_window='全部已采集历史'
        window=st.selectbox('统计时间',windows,
            key='statistics_window',**({'index':1} if 'statistics_window' not in st.session_state else {}))
        historical=None
        if window=='自定义历史区间':
            selected=st.date_input('选择起止日期',value=(datetime.now(LOCAL_TZ).date()-timedelta(days=7),datetime.now(LOCAL_TZ).date()),max_value=datetime.now(LOCAL_TZ).date())
            if len(selected)==2: historical=selected
        if st.button('刷新数据与游戏列表'): st.rerun()
        st.caption('App Store 舆情分析 · 2.3\n真实采集 · Agent 深度分析 · 多游戏')
    if choice=='＋ 添加游戏': clear_dialog(); close_collection_log(); add_game(); return
    profile=labels[choice]
    snapshot_caption(bundled,profile)
    if page=='监控总览':
        clear_dialog()
        close_collection_log()
        st.title('所有游戏 · 监控总览')
        st.caption('各游戏、各地区分别展示评论数量、自动巡检与最近采集状态。')
        rows=[]
        for _,p in list_game_profiles(DB).iterrows():
            state=target_state(DB,p['app_id'],p['country'])
            rows.append({'游戏':p['app_name'],'地区':p['country'],'评论数':p['total_reviews'],
                '自动巡检':'启用' if state.get('enabled') else '未启用','最近状态':state.get('last_status','未采集'),
                '上次成功':local_time(state.get('last_success')),'下次巡检':local_time(state.get('next_due')) if state.get('enabled') else '—'})
        st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
        return
    if page=='评论浏览':
        clear_dialog()
        close_collection_log()
        st.title(f'评论浏览 · {profile["app_name"]} · {profile["country"].upper()}')
        st.caption('这里是面向人工核查的评论入口。已分析中待复核的评论会保留原文、判断依据和主题标签，支持按时间或数量范围快速浏览。')
        browser_request=st.session_state.pop('browser_request',None)
        render_review_browser(profile,browser_request)
        return
    # Freeze the report cutoff until the next refresh, so generated downloads stay consistent.
    fixed_report=saved_report[0] if saved_report and window==BUNDLED_REPORT_WINDOW else None
    scope=f"{profile['app_id']}:{profile['country']}:{window}:{historical}:{page}"
    if st.session_state.get('scope')!=scope:
        st.session_state.scope=scope
        st.session_state.cutoff=fixed_report['end_at'] if fixed_report else datetime.now(LOCAL_TZ).isoformat()
    if st.sidebar.button('更新统计至当前时间',disabled=bool(fixed_report),
            help='历史报告使用其已完成的固定时段；切换其他统计时间后可以更新。' if fixed_report else None):
        st.session_state.cutoff=datetime.now(LOCAL_TZ).isoformat()
        st.rerun()
    def render():
        state=target_state(DB,profile['app_id'],profile['country'])
        runs=recent_runs(DB,profile['app_id'],profile['country'])
        mode_prefix=f"agent_{profile['app_id']}_{profile['country']}"
        chosen_mode=st.session_state.get(mode_prefix+'_mode',
            st.session_state.get(mode_prefix+'_preference','规则初筛'))
        icon=get_app_icon(profile['app_id'],profile['country'],DATA/'app_icons',
            allow_fetch=not fixed_report and chosen_mode=='Agent分析' and os.environ.get('APPSTORE_DISABLE_ICON_FETCH')!='1')
        st.markdown(game_hero_html(profile,icon),unsafe_allow_html=True)
        source_banner(profile,None,state,runs)
        if page=='采集与设置':
            collection_settings(profile,state,runs)
            return
        cutoff=pd.Timestamp(fixed_report['end_at'] if fixed_report else
            st.session_state.cutoff if page=='数据与导出' else datetime.now(LOCAL_TZ)).tz_convert(LOCAL_TZ)
        kwargs={}
        if fixed_report:
            kwargs={'start':pd.Timestamp(fixed_report['start_at']).tz_convert(LOCAL_TZ),
                'end':pd.Timestamp(fixed_report['end_at']).tz_convert(LOCAL_TZ)}
        elif historical:
            kwargs={'start':pd.Timestamp(historical[0],tz=LOCAL_TZ),'end':min(pd.Timestamp(historical[1]+timedelta(days=1),tz=LOCAL_TZ),cutoff)}
        elif window=='全部已采集历史':
            with database(DB) as connection:
                earliest=connection.execute('SELECT MIN(julianday(date)) FROM review_records WHERE app_id=? AND country=? AND julianday(date)<=julianday(?)',
                    (profile['app_id'],profile['country'],cutoff.isoformat())).fetchone()[0]
            kwargs={'start':pd.to_datetime(earliest,unit='D',origin='julian',utc=True).tz_convert(LOCAL_TZ).normalize() if earliest else cutoff.normalize()}
        window_start=kwargs.get('start',cutoff.normalize()-pd.Timedelta(days={'今天':1,'近 7 天':7,'近 30 天':30}.get(window,7)-1))
        window_end=kwargs.get('end',cutoff)
        previous_start=window_start-(window_end-window_start)
        raw=read_reviews(DB,app_id=profile['app_id'],country=profile['country'],
            start_at=previous_start.isoformat(),end_at=window_end.isoformat())
        with database(DB) as connection:
            revision=connection.execute('SELECT COALESCE(MAX(id),0) FROM audit_log').fetchone()[0]
        revision=(revision,analysis_revision(DB))
        data=analyzed(raw,revision,str(DB))
        view=snapshot(data,days={'今天':1,'近 7 天':7,'近 30 天':30}.get(window,7),now=cutoff,**kwargs)
        st.caption(f"当前区间 {view['start']:%Y-%m-%d %H:%M} — {view['end']:%Y-%m-%d %H:%M}；上期 {view['previous_start']:%Y-%m-%d %H:%M} — {view['start']:%Y-%m-%d %H:%M}。")
        if page=='舆情概览':
            analysis_mode,report,agent_status=analysis_panel(DB,profile,view)
        else:
            analysis_mode=st.session_state.get(f"agent_{profile['app_id']}_{profile['country']}_preference",'规则初筛')
            report=latest_report(DB,profile['app_id'],profile['country'],view['start'],view['end']) if analysis_mode=='Agent分析' else None
        data=analysis_data(data,analysis_mode)
        data=with_version_attribution(data,profile)
        view=snapshot(data,start=view['start'],end=view['end'],now=cutoff)
        with database(DB) as connection:
            quality=connection.execute('''SELECT SUM(julianday(date) IS NULL) AS undated,
              SUM(julianday(date)>julianday(?)) AS future FROM review_records WHERE app_id=? AND country=?''',
              (cutoff.isoformat(),profile['app_id'],profile['country'])).fetchone()
        view.update(undated=int(quality['undated'] or 0),future=int(quality['future'] or 0))
        view['analysis_mode']=analysis_mode
        view['agent_report']=report
        plans=issue_plans(view) if analysis_mode=='规则初筛' else []
        if page=='舆情概览':
            countries=profiles.loc[profiles['app_id'].astype(str).eq(str(profile['app_id'])),'country'].tolist()
            all_regions=analysis_data(analyzed(read_reviews(DB,app_id=profile['app_id'],
                start_at=previous_start.isoformat(),end_at=window_end.isoformat()),revision,str(DB)),analysis_mode) if len(countries)>1 else data
            regions=region_comparison(all_regions,profile['app_id'],countries,view['start'],view['end'])
            overview(profile,view,plans,regions)
        elif page=='采集与设置': collection_settings(profile,state,runs)
        else: data_tools(profile,view,plans,runs)
    # Interactive controls and charts belong to the normal app render, avoiding
    # interrupted fragment deltas when switching analysis mode and editing the
    # budget quickly. The timer only observes persisted progress and never
    # interrupts an open chart dialog.
    if page=='舆情概览':
        st.session_state.dashboard_update_signature=dashboard_update_signature(DB,profile['app_id'],profile['country'])
        poll_dashboard_updates(DB,profile['app_id'],profile['country'],scope)
    render_collection_log(DB)
    render_active_dialog()
    render()
