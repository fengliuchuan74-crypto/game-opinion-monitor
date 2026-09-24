"""Nonblocking Agent controls and evidence-backed report presentation."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from modules.agent_analysis import (analysis_status, cancel_analysis, configure_analysis,
    latest_report, recent_analysis_runs, request_analysis, start_analysis_worker)
from modules.codex_runner import (available_models, available_reasoning_efforts,
    check_runtime, configured_model, configured_reasoning_effort,
    model_is_locally_configured, MODEL_REASONING_EFFORTS)
from modules.operations import LOCAL_TZ

STATUSES = {'queued':'排队中','running':'分析中','completed':'已完成','failed':'失败','cancelled':'已取消'}
MODEL_OPTIONS = [
    ('沿用本机 Codex 默认模型',''),
    ('gpt-6-astra（复杂分析）','gpt-6-astra'),
    ('gpt-5.6-sol（高质量分析）','gpt-5.6-sol'),
    ('gpt-5.6-terra（平衡）','gpt-5.6-terra'),
    ('gpt-5.6-luna（快速）','gpt-5.6-luna'),
    ('gpt-5.5（通用）','gpt-5.5'),
]
REASONING_OPTIONS = [
    ('跟随模型默认','default'),('低（更快）','low'),('中（平衡）','medium'),
    ('高（更细致）','high'),('极高（最细致）','xhigh'),('最高（平衡模型）','max'),
    ('超高（复杂问题）','ultra'),
]
RULE_FIELDS = ['sentiment_label','sentiment_score','sentiment_keywords','issue_category',
    'issue_categories','issue_keywords','analysis_basis','needs_review']


@st.cache_resource
def agent_worker(path, output_dir):
    return start_analysis_worker(Path(path), Path(output_dir))


def display_time(value):
    if not value: return '—'
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None: stamp = stamp.tz_localize('UTC')
    return stamp.tz_convert(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')


def analysis_stage(run):
    stage = str(run.get('stage') or '')
    if run.get('status')!='running': return STATUSES.get(run.get('status'),'等待分析')
    if '理解评论' in stage or '逐条' in stage: return '正在逐条解读评论'
    if '选择' in stage and '核查' in stage: return '正在选择需要核查的问题'
    if '查询' in stage or '上期' in stage: return '正在核对原文与历史反馈'
    if '归纳' in stage or '报告' in stage: return '正在形成分析与处理建议'
    return '正在分析'


def _model_options(settings):
    """Return desktop picker candidates with an honest verification label.

    Codex CLI has no model-list endpoint. Candidates absent from local config
    are therefore marked ``启动时验证``; the real ``codex exec`` call remains
    the final availability check and returns a safe error if unavailable.
    """
    current = str(configured_model() or '').strip()
    saved = str((settings or {}).get('model') or '').strip()
    values = list(available_models())
    if current and current not in values: values.insert(0,current)
    # An old saved model is selectable only when the current Codex configuration
    # still exposes it. Arbitrary model names can never be entered in the UI.
    values = list(dict.fromkeys(v for v in values if v))
    result = [('沿用本机 Codex 默认模型', '')]
    labels = {value:label for label,value in MODEL_OPTIONS}
    for value in values:
        label = labels.get(value, value)
        if value and not model_is_locally_configured(value):
            label += ' · 启动时验证'
        elif value:
            label += ' · 本机已配置'
        result.append((label, value))
    return result


def _reasoning_label(value):
    return {candidate:label for label,candidate in REASONING_OPTIONS}.get(value or 'default', value or 'default')


def _reasoning_options(model=''):
    # Apply the selected model's ceiling (for example Luna has no Ultra), then
    # intersect with strengths enabled by the local Codex configuration.
    enabled = set(available_reasoning_efforts())
    allowed = MODEL_REASONING_EFFORTS.get(
        str(model or '').strip(),
        tuple(value for _, value in REASONING_OPTIONS if value != 'default'),
    )
    supported = enabled.intersection(allowed)
    return [('default','跟随模型默认')]+[(value,label) for label,value in REASONING_OPTIONS if value in supported]


def _running_eta(run):
    """Estimate remaining wall time from durable run counters."""
    if not run or run.get('status') not in {'queued','running'}:
        return None
    selected = max(0, int(run.get('selected') or 0))
    total = max(0, int(run.get('total') or 0))
    processed = max(0, int(run.get('processed') or 0))
    # processed includes the cache reused at queue time; remove that baseline.
    baseline = int(run.get('initial_processed') or max(0, total-selected))
    done = min(selected, max(0, processed - baseline))
    remaining = max(0, selected-done)
    if not remaining:
        # Investigation/report calls follow classification; avoid a misleading
        # "0 seconds" while the run is still active.
        return 45 if run.get('status') == 'running' else 60
    started = run.get('started_at') or run.get('created_at')
    try:
        stamp = pd.Timestamp(started)
        if stamp.tzinfo is None: stamp = stamp.tz_localize('UTC')
        elapsed = max(0.0, (pd.Timestamp.now(tz='UTC')-stamp.tz_convert('UTC')).total_seconds())
    except (TypeError,ValueError):
        elapsed = 0.0
    per_item = 8.0 if done <= 0 or elapsed < 2 else max(2.0, min(90.0, elapsed/done))
    return int(max(1, round(remaining*per_item + (45 if run.get('status') == 'running' else 0))))


def _eta_text(seconds):
    if seconds is None: return ''
    seconds = max(1, int(seconds))
    if seconds < 60: return f'约 {seconds} 秒'
    minutes, rest = divmod(seconds, 60)
    return f'约 {minutes} 分钟' + (f' {rest} 秒' if rest else '')


@st.fragment(run_every='3s')
def _running_status(db, app_id, country, run_id):
    """Refresh progress independently while the rest of the dashboard stays usable."""
    latest = next((run for run in recent_analysis_runs(db,app_id,country,limit=10)
                   if int(run['id']) == int(run_id)), None)
    if not latest: return
    if latest.get('status') not in {'queued','running'}:
        # Refresh counts, charts and final report once when this run completes.
        st.rerun(scope='app')
    processed, total = int(latest.get('processed',0)), int(latest.get('total',0))
    eta = _eta_text(_running_eta(latest))
    prefix = 'Agent 正在分析中，请稍等' if latest.get('status') == 'running' else 'Agent 任务已排队，请稍等'
    st.info(f'{prefix} · 预计还需 {eta}')
    st.progress(min(1.0,processed/max(1,total)),text=f'已分析 {processed:,} / {total:,} 条 · {analysis_stage(latest)}')
    st.caption('等待时间为动态估计，会随实际批次速度调整；联网、重试和报告生成可能影响时间。状态每 3 秒自动刷新。')


def _unsupported_model(settings):
    saved = str((settings or {}).get('model') or '').strip()
    if not saved or saved == 'codex-default': return ''
    allowed = {value for _,value in _model_options(settings) if value}
    return saved if saved not in allowed else ''


def analysis_data(data, mode):
    """Never silently mix rule sentiment with unanalysed Agent rows."""
    result = data.copy()
    manual = result.get('manual_reviewed', pd.Series(False, index=result.index)).fillna(False).astype(bool)
    if mode == '规则初筛':
        for field in RULE_FIELDS:
            if 'rule_'+field in result:
                result.loc[~manual, field] = result.loc[~manual, 'rule_'+field]
        result.loc[~manual, 'analysis_source'] = '规则初筛'
        for field in ['agent_reason','agent_demand','agent_target','agent_quote','agent_model']:
            if field in result: result[field] = ''
        for field in ['agent_analyzed','agent_uncertain']:
            if field in result: result[field] = False
        if 'agent_status' in result: result['agent_status'] = '不适用：本地规则初筛'
    else:
        analyzed = result.get('agent_analyzed', pd.Series(False,index=result.index)).fillna(False).astype(bool)
        pending = ~(analyzed | manual)
        result.loc[pending, 'sentiment_label'] = '待复核'
        result.loc[pending, 'sentiment_score'] = float('nan')
        result.loc[pending, 'sentiment_keywords'] = ''
        result.loc[pending, 'issue_category'] = '未归类/待复核'
        result.loc[pending, 'issue_categories'] = '未归类/待复核'
        result.loc[pending, 'issue_keywords'] = ''
        result.loc[pending, 'analysis_basis'] = 'Agent 尚未分析；保留原文与星级，暂不判断情绪和问题类别'
        result.loc[pending, 'needs_review'] = True
        result.loc[pending, 'analysis_source'] = '待分析'
    return result


def runtime_controls(prefix):
    if st.button('检查 Agent 连接', key=prefix+'_runtime'):
        with st.spinner('检查本机 Codex 登录状态…'):
            st.session_state.agent_runtime = check_runtime()
    runtime = st.session_state.get('agent_runtime')
    if runtime:
        ok = runtime.get('available') and runtime.get('authenticated')
        (st.success if ok else st.warning)(runtime.get('message') or ('Agent 已连接' if ok else 'Agent 尚未就绪'))
        if runtime.get('model'): st.caption('本机配置模型：'+str(runtime['model']))
        runtime_effort = runtime.get('reasoning_effort') or configured_reasoning_effort()
        if runtime_effort:
            st.caption('本机配置推理强度：'+_reasoning_label(str(runtime_effort)))
    st.caption('沿用本机 Codex 登录与模型配置；分析使用该账号的可用额度。启动分析后，所选评论会提交给模型处理。')


def analysis_panel(db, profile, view):
    app_id, country = profile['app_id'], profile['country']
    prefix = f'agent_{app_id}_{country}'
    mode_key, preference_key = prefix+'_mode', prefix+'_preference'
    if mode_key not in st.session_state:
        has_results = bool(view['current'].get('agent_analyzed',pd.Series(dtype=bool)).fillna(False).any())
        st.session_state[mode_key] = st.session_state.get(preference_key,'Agent分析' if has_results else '规则初筛')
    with st.container(key='agent_control_panel', border=True):
        mode = st.radio('看板分析方式', ['Agent分析','规则初筛'], horizontal=True, key=mode_key)
        st.session_state[preference_key] = mode
        if mode == '规则初筛':
            st.subheader('规则初筛')
            st.write(f"本期 {len(view['current']):,} 条评论 · 已完成本地初筛")
            st.caption('使用本地规则分析已采集评论，无需联网，不调用模型。情绪、问题类别与处理参考均采用规则结果；有效人工复核优先。')
            return mode, None, {'total':len(view['current']), 'analyzed':len(view['current']), 'pending':0}

        status = analysis_status(db, app_id, country, view['start'], view['end'])
        settings, latest = status.get('settings') or {}, status.get('latest_run') or {}
        busy = latest.get('status') in ['queued','running']
        st.subheader('Agent 深度分析')
        st.caption('理解评论原文、核查同类反馈，形成问题判断与处理建议。后台分析期间可继续浏览。')
        columns = st.columns(4)
        for col, label, value in zip(columns, ['本期已采集','已分析','待分析','已分析中待复核'],
                [status.get('total',0),status.get('analyzed',0),status.get('pending',0),status.get('uncertain',0)]):
            col.markdown(f'**{int(value):,} 条**  \n{label}')
        review_count = int(status.get('uncertain', 0) or 0)
        if st.button(f'查看待复核评论 · {review_count}', key=prefix+'_review_uncertain',
                     disabled=review_count == 0,
                     help='打开评论浏览，并只显示当前统计时间范围内需要人工确认的评论。'):
            st.session_state.review_browser_request = {
                'app_id': str(app_id), 'country': str(country), 'status': '待复核',
                'start': view['start'].isoformat(), 'end': view['end'].isoformat(),
            }
            st.rerun(scope='app')
        st.caption('图表仅采用已有的逐条解读及有效人工复核；未分析评论标为待判定，不混入规则判断。')
        manual_count = int(view['current'].get('manual_reviewed',pd.Series(dtype=bool)).fillna(False).sum())
        if manual_count: st.caption(f'本期包含 {manual_count} 条有效人工复核，优先采用人工结果。')
        model_candidates = _model_options(settings)
        saved_model = str(settings.get('model') or '')
        configured = str(configured_model() or '')
        if saved_model in {'codex-default', configured}:
            saved_model = ''
        model_value = st.selectbox('本次使用的模型', [value for _,value in model_candidates],
            index=next((i for i,(_,value) in enumerate(model_candidates) if value==saved_model),0),
            format_func=lambda value: next(label for label,candidate in model_candidates if candidate==value),
            key=prefix+'_model', help='列出 Codex 当前模型选项；未在本机配置中确认的候选会在启动分析时由 Codex 验证。')
        unsupported = _unsupported_model(settings)
        if unsupported:
            st.warning(f'数据库中保存的模型“{unsupported}”当前未出现在 Codex 可用候选中，本次默认使用本机配置；请保存新的候选。')
        effective_model = model_value or configured
        reasoning_options = _reasoning_options(effective_model)
        reasoning_candidates = [value for value,_ in reasoning_options]
        saved_reasoning = str(settings.get('reasoning_effort') or 'default')
        if saved_reasoning not in reasoning_candidates: saved_reasoning = 'default'
        if st.session_state.get(prefix+'_reasoning') not in reasoning_candidates:
            st.session_state[prefix+'_reasoning'] = saved_reasoning
        reasoning = st.selectbox('推理强度', reasoning_candidates,
            index=reasoning_candidates.index(saved_reasoning),
            format_func=lambda value: next(label for candidate,label in reasoning_options if candidate==value), key=prefix+'_reasoning',
            help='只显示所选模型对应的强度；强度越高通常越细致，但耗时和额度消耗可能增加。')
        st.caption('模型候选与 Codex 选择器一致；是否可调用由当前账号与服务在任务开始时验证。建议先用中等强度，复杂争议再提高。')
        st.caption('本次任务会使用上面选择；下方“保存设置”可将它设为当前游戏与地区的默认值。')
        size = st.radio('本次分析数量',['全部待分析评论','自定义数量'],horizontal=True,key=prefix+'_size')
        budget = None
        if size == '自定义数量':
            budget = st.number_input('本轮最多新增分析评论数',min_value=10,max_value=1000,
                value=max(10,min(1000,int(settings.get('max_reviews') or 200))),step=10,key=prefix+'_budget')
        else:
            st.caption(f"本次将分析当前区间全部 {int(status.get('pending',0)):,} 条待分析评论；已有有效结果会复用。")
        st.caption(f"分析范围：{view['start']:%Y-%m-%d %H:%M} 至 {view['end']:%Y-%m-%d %H:%M}（北京时间）")
        controls = st.columns([3,1])
        retry = latest.get('status') in ['failed','cancelled']
        start = controls[0].button('重试本期 Agent 分析' if retry else '开始 Agent 分析',
            type='primary',disabled=busy or not status.get('total'),key=prefix+'_start',width='stretch')
        cancel = controls[1].button('取消分析',disabled=not busy,key=prefix+'_cancel',width='stretch')
        if start:
            try:
                request_analysis(db,app_id,country,start_at=view['start'],end_at=view['end'],
                    max_reviews=None if budget is None else int(budget),model=model_value,
                    reasoning_effort='' if reasoning=='default' else reasoning)
                st.rerun()
            except (ValueError,RuntimeError) as exc: st.error(str(exc))
        if cancel:
            cancel_analysis(db,latest['id'])
            st.rerun()
        if latest:
            label = STATUSES.get(latest.get('status'),latest.get('status','未知'))
            if busy:
                _running_status(db,app_id,country,latest['id'])
            elif latest.get('status') == 'failed':
                st.error('本轮分析未完成：'+str(latest.get('error') or '请检查连接后重试。'))
            elif latest.get('status') == 'cancelled':
                st.info('本轮已取消，已完成的逐条结果仍可查看。')
            else:
                st.caption(f"最近分析：{label} · {display_time(latest.get('finished_at') or latest.get('created_at'))}")
        with st.expander('运行详情与连接设置'):
            if latest:
                st.write(f"任务 #{latest['id']} · {analysis_stage(latest)}")
                st.caption('任务范围：'+display_time(latest.get('start_at'))+' 至 '+display_time(latest.get('end_at')))
                st.caption('本次全部待分析评论' if latest.get('all_reviews') else f"本次选中 {int(latest.get('selected',0))} 条待分析评论")
                st.caption(f"模型：{latest.get('model') or '本机默认'} · 推理强度：{_reasoning_label(latest.get('reasoning_effort','default'))}")
                st.caption('每批任务有独立超时与重试；网络或额度异常时可重试，已完成的逐条结果会保留。')
            runtime_controls(prefix)
            rows = recent_analysis_runs(db,app_id,country,limit=10)
            if rows:
                st.dataframe(pd.DataFrame([{'任务':r['id'],'开始时间':display_time(r.get('created_at')),
                    '状态':STATUSES.get(r.get('status'),r.get('status')),
                    '范围':'全部待分析' if r.get('all_reviews') else f"{r.get('selected',0)} 条",
                    '已分析':r.get('processed',0),'模型':r.get('model') or '本机默认',
                    '推理强度':_reasoning_label(r.get('reasoning_effort','default'))} for r in rows]),
                    hide_index=True,width='stretch')
            else: st.caption('暂无分析任务。')
    return mode,latest_report(db,app_id,country,view['start'],view['end']),status


def analysis_settings(db, profile):
    preference = st.session_state.get(f"agent_{profile['app_id']}_{profile['country']}_preference",'规则初筛')
    if preference == '规则初筛':
        st.subheader('规则分析设置')
        st.caption('已采集评论会自动完成本地初筛，规则分析无需联网，也不使用模型额度。采集数据的联网设置独立配置。')
        return
    now = pd.Timestamp.now(tz=LOCAL_TZ)
    status = analysis_status(db, profile['app_id'], profile['country'], now.normalize()-pd.Timedelta(days=6), now)
    settings = status.get('settings') or {}
    prefix = f"agent_settings_{profile['app_id']}_{profile['country']}"
    st.subheader('Agent 分析设置')
    st.caption('自动分析针对当前游戏和地区；每轮采集完成后检查最近窗口，只分析新增或内容已变化的评论。')
    # Model changes must rerun immediately so the strength choices can follow
    # the selected model; a Streamlit form defers changes until submit.
    with st.container(border=True):
        auto = st.checkbox('采集完成后自动进行 Agent 分析', value=bool(settings.get('auto_enabled',False)))
        days = st.number_input('自动分析最近多少天（北京时间）', min_value=1,max_value=365,
            value=int(settings.get('days',7)),step=1)
        budget = st.number_input('每轮最多新增分析评论数', min_value=10,max_value=1000,
            value=max(10,min(1000,int(settings.get('max_reviews') or 200))),step=10)
        with st.expander('高级模型设置'):
            model_candidates = _model_options(settings)
            saved_model = str(settings.get('model') or '')
            configured = str(configured_model() or '')
            if saved_model in {'codex-default',configured}: saved_model = ''
            model = st.selectbox('默认模型', [value for _,value in model_candidates],
                index=next((i for i,(_,value) in enumerate(model_candidates) if value==saved_model),0),
                format_func=lambda value: next(label for label,candidate in model_candidates if candidate==value),
                key=prefix+'_model', help='与 Codex 选择器一致；未在本机配置确认的候选在启动分析时验证。')
            unsupported = _unsupported_model(settings)
            if unsupported:
                st.warning(f'历史模型“{unsupported}”当前不可用，已不作为可选项；请选择可用候选并保存。')
            reasoning_options = _reasoning_options(model or configured)
            reasoning_values = [value for value,_ in reasoning_options]
            saved_reasoning = str(settings.get('reasoning_effort') or 'default')
            if saved_reasoning not in reasoning_values: saved_reasoning = 'default'
            if st.session_state.get(prefix+'_reasoning') not in reasoning_values:
                st.session_state[prefix+'_reasoning'] = saved_reasoning
            reasoning = st.selectbox('默认推理强度', reasoning_values,
                index=reasoning_values.index(saved_reasoning),
                format_func=lambda value: next(label for candidate,label in reasoning_options if candidate==value), key=prefix+'_reasoning',
                help='高强度可能更慢且消耗更多额度；不确定时使用模型默认。')
            st.caption('模型和推理强度按当前游戏与地区保存；任务开始时仍可在概览中临时选择。')
        save = st.button('保存 Agent 分析设置',type='primary',key=prefix+'_save')
    if save:
        try:
            configure_analysis(db, profile['app_id'],profile['country'],auto_enabled=auto,
                max_reviews=int(budget),days=int(days),model=model,
                reasoning_effort='' if reasoning=='default' else reasoning)
            st.success('Agent 分析设置已保存。')
        except ValueError as exc: st.error(str(exc))
    runtime_controls(prefix)


def render_analysis_reviews(view):
    """Read every in-window review and its own result, without mixing sources."""
    from modules.dashboard import categories_for, category_mask
    mode = view.get('analysis_mode','规则初筛')
    is_agent = mode == 'Agent分析'
    prefix = 'analysis_reviews_'+('agent' if is_agent else 'rule')
    title = 'Agent逐条解读' if is_agent else '规则分类明细'
    panel = st.expander(title+' · 查看评论原文与分析结果',key=prefix+'_open',on_change='rerun')
    if not panel.open: return
    with panel:
        data = analysis_data(view['current'],mode)
        if data.empty:
            st.info('当前区间没有已采集评论。')
            return
        manual = data.get('manual_reviewed',pd.Series(False,index=data.index)).fillna(False).astype(bool)
        uncertain = data['needs_review'].fillna(True).astype(bool)
        data['_display_status'] = '已初筛'
        if is_agent:
            completed = data.get('agent_analyzed',pd.Series(False,index=data.index)).fillna(False).astype(bool)
            data['_display_status'] = '待分析'
            data.loc[completed,'_display_status'] = '已分析'
            data.loc[completed & uncertain,'_display_status'] = '待复核'
        else:
            data.loc[uncertain,'_display_status'] = '待复核'
        data.loc[manual,'_display_status'] = '人工复核'
        filters = st.columns(3)
        statuses = ['全部状态']+[value for value in ['待分析','已分析','已初筛','待复核','人工复核'] if value in set(data['_display_status'])]
        categories = ['全部类别']+sorted({category for _,row in data.iterrows() for category in categories_for(row)})
        for suffix,options in [('_status',statuses),('_category',categories)]:
            if st.session_state.get(prefix+suffix) not in options: st.session_state[prefix+suffix] = options[0]
        selected_status = filters[0].selectbox('分析状态',statuses,key=prefix+'_status')
        selected_sentiment = filters[1].selectbox('评论情绪',['全部情绪','好评','中评','差评','待复核'],key=prefix+'_sentiment')
        selected_category = filters[2].selectbox('问题类别',categories,key=prefix+'_category')
        query = st.text_input('搜索评论原文',placeholder='输入标题或正文中的词语',key=prefix+'_search').strip()
        selected = data
        if selected_status!='全部状态': selected=selected.loc[selected['_display_status'].eq(selected_status)]
        if selected_sentiment!='全部情绪': selected=selected.loc[selected['sentiment_label'].eq(selected_sentiment)]
        if selected_category!='全部类别': selected=selected.loc[category_mask(selected,selected_category)]
        if query:
            original = selected['title'].fillna('').astype(str)+' '+selected['content'].fillna('').astype(str)
            selected = selected.loc[original.str.contains(query,case=False,regex=False)]
        selected = selected.sort_values(['date','review_id'],ascending=[False,False])
        fingerprint = (selected_status,selected_sentiment,selected_category,query,tuple(selected['review_id']))
        if st.session_state.get(prefix+'_filters')!=fingerprint:
            st.session_state[prefix+'_filters']=fingerprint
            st.session_state[prefix+'_page']=1
        if selected.empty:
            st.info('当前筛选没有匹配评论，可调整状态、情绪、类别或搜索词。')
            return
        total_pages = (len(selected)+9)//10
        page = st.number_input('评论页码',min_value=1,max_value=total_pages,value=None,step=1,key=prefix+'_page') or 1
        st.caption(f'筛选结果 {len(selected):,} / 本期 {len(data):,} 条 · 第 {page} / {total_pages} 页 · 每页最多 10 条，按最新评论排序。')
        for _,row in selected.iloc[(page-1)*10:page*10].iterrows():
            review_id = int(row['review_id'])
            with st.container(key=prefix+'_item_'+str(review_id),border=True):
                st.write(str(row.get('title') or '未填写标题'))
                rating = row.get('rating')
                rating_text = f'{float(rating):g} 星' if pd.notna(rating) else '星级未知'
                st.caption(f"#{review_id} · {display_time(row['date'])} · {rating_text} · {row['_display_status']}")
                st.markdown('**评论原文**')
                st.text(str(row.get('content') or ''))
                st.caption('情绪：'+str(row['sentiment_label'])+' · 类别：'+'、'.join(categories_for(row)))
                if is_agent and row['_display_status']=='待分析':
                    st.info('此条尚未分析，暂不判断情绪、问题类别或诉求。')
                elif is_agent and row['_display_status']!='人工复核':
                    for label,field in [('判断理由','agent_reason'),('玩家诉求','agent_demand'),('判断依据 · 原文引用','agent_quote')]:
                        st.markdown('**'+label+'**')
                        value = row.get(field)
                        st.text(str(value) if pd.notna(value) and str(value).strip() else '当前结果未提供此项。')
                else:
                    st.markdown('**'+('人工复核依据' if row['_display_status']=='人工复核' else '规则判断依据')+'**')
                    st.text(str(row.get('analysis_basis') or '按本地情绪与类别规则初筛，待复核项需阅读原文确认。'))


def report_overview(view):
    st.subheader('舆情深度分析与建议')
    report = view.get('agent_report')
    if not report:
        st.info('当前区间尚无完成的 Agent 深度报告。上方启动分析后，这里会展示有原文依据的发现与处理建议。')
        return
    coverage = report.get('coverage') or {}
    st.caption(f"Agent 报告 · 任务 #{report.get('run_id','—')} · 分析截至 {display_time(coverage.get('end_at'))}")
    st.caption(f"报告范围共 {int(coverage.get('total',0)):,} 条；已分析 {int(coverage.get('analyzed',0)):,} 条，待分析 {int(coverage.get('pending',0)):,} 条。报告结论只基于已分析部分。")
    if report.get('stale'):
        st.warning('这份报告尚未覆盖最新数据：'+str(report.get('stale_reason') or '请重新分析以更新结论。'))
    with st.container(key='agent_report_summary',border=True):
        st.write(report.get('summary') or '本轮未形成可确认的总体结论。')
    investigations = report.get('investigations') or []
    if investigations:
        with st.expander(f'Agent 已进一步核查 {len(investigations)} 个问题'):
            for item in investigations:
                query = item.get('query') or {}
                if query.get('question'): st.write('• '+str(query['question']))
    positive = report.get('positive') or []
    if positive:
        with st.container(key='agent_report_positive',border=True):
            st.markdown('**正向反馈与可验证亮点**')
            for item in positive: st.write('• '+str(item))
    limitations = report.get('limitations') or []
    if limitations:
        with st.expander('分析覆盖与待确认事项', expanded=True):
            for item in limitations: st.write('• '+str(item))
    st.caption('以下情绪与类别图使用已保存的逐条分析；深度报告为上方注明截止时间的快照。')


def report_plans(view, evidence_card):
    st.subheader('具体处理方案与代表评论')
    report = view.get('agent_report') or {}
    findings = report.get('findings') or []
    if not findings:
        st.info('当前没有 Agent 已生成的处理方案。完成分析后，根据真实评论展示观察、待验证原因、处理步骤和验收方式。')
        return
    st.caption('Agent 建议关联原文证据；“可能原因”是待验证假设，协作角色是建议分工。')
    st.caption(f"方案来自任务 #{report.get('run_id','—')}，分析截至 {display_time((report.get('coverage') or {}).get('end_at'))}。")
    if report.get('stale'): st.warning('以下方案来自较早的数据快照，请更新 Agent 分析后再用于当前决策。')
    evidence = report.get('evidence') or []
    if isinstance(evidence,dict):
        lookup = {str(k):v for k,v in evidence.items()}
    else:
        lookup = {str(row.get('review_id')):row for row in evidence}
    for index, item in enumerate(findings):
        with st.container(key=f'agent_finding_{index}',border=True):
            st.write(f"{index+1:02d} · {item.get('title') or item.get('category') or '待核实发现'}")
            st.caption('问题类别：'+str(item.get('category') or '未归类/待复核')+' · 建议协作：'+str(item.get('owner') or '项目运营'))
            cols = st.columns(3)
            for col, title, value in zip(cols,['观察到的现象','可能原因 · 待验证','验收与回看'],
                    [item.get('observation'),item.get('hypothesis'),item.get('validation')]):
                with col:
                    st.markdown('**'+title+'**')
                    st.write(value or '暂缺证据，需进一步核实。')
            st.markdown('**建议处理步骤**')
            for number, action in enumerate(item.get('actions') or [],1): st.write(f'{number}. {action}')
            ids = [str(value) for value in item.get('evidence_ids') or []]
            matched = [lookup[value] for value in ids if value in lookup]
            if matched:
                for col,row in zip(st.columns(2),matched[:2]):
                    with col: evidence_card(row)
                if len(matched)>2:
                    with st.expander(f'查看其余 {len(matched)-2} 条原文证据'):
                        for row in matched[2:]: evidence_card(row)
            else:
                st.caption('本条尚无可展示的原文证据，请结合数据范围核实。')
