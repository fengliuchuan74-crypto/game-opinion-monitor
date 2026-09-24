"""A presentation-friendly dashboard backed by the current review window."""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from agent_ui import report_overview

from modules.dashboard import (SENTIMENT_COLORS, UNCLASSIFIED, categories_for, category_counts, day_reviews,
    category_mask, category_timeline, keyword_counts, negative_rows, ratings, sentiments, timeline, versions, version_label)
from modules.operations import SENTIMENTS, LOCAL_TZ
from modules.issue_classifier import ISSUE_RULES
from modules.review_cards import card_html, featured_reviews
from modules.deliverables import excel_bytes

PURPLE = '#a18aff'
PURPLE_SCALE = ['#565078','#7663b5','#a18aff','#c7b7ff']
KEYWORD_SCALE = ['#514c77','#537f9c','#58bdbd','#79efd0']
CATEGORY_COLORS = dict(zip([*ISSUE_RULES,'正向口碑/泛好评',UNCLASSIFIED],
    ['#70b8ff','#ffb38e','#b69bff','#6ad6ef','#f6d578','#ef84a8','#d0adff','#f5ad64',
     '#64d4ba','#99a7ff','#a7dfa4','#f3a1c2','#8dcde2','#c6d984','#84a4f6','#e7b491',
     '#79d9d6','#c9b9ff','#eea89e','#cf93dc','#91c7f2','#ecd18c','#afd3aa','#79efd0','#929fbd']))
CHART_TITLES = {'sentiment_distribution':'评论情绪构成','rating_distribution':'1—5 星分布',
    'volume_trend':'评论声量与负面变化','sentiment_trend':'情绪构成随时间变化','rating_trend':'低星占比与平均星级',
    'issue_trend':'问题类别随时间变化','version_sentiment':'版本口碑','region_comparison':'同一游戏地区对比',
    'category_comparison':'问题类别分析','keyword_ranking':'关键词频率排行','daily_categories':'当天问题类别'}
PLOT_CONFIG = {'displaylogo':False,'displayModeBar':True,'scrollZoom':False,
    # Keep direct dragging in pan mode.  Zoom is deliberately a toolbar action
    # so a normal drag can never silently turn into a horizontal zoom.
    'modeBarButtonsToRemove':['lasso2d','select2d','autoScale2d'],
    'doubleClick':'reset+autosize', 'responsive':True,
    'toImageButtonOptions':{'format':'png','scale':2}}
LARGE_PLOT_CONFIG = {**PLOT_CONFIG,'locale':'zh-CN','locales':{'zh-CN':{'dictionary':{
    'Zoom':'框选缩放','Pan':'拖动平移','Zoom in':'放大一级','Zoom out':'缩小一级',
    'Autoscale':'适应数据','Reset axes':'恢复坐标','Download plot as a PNG':'下载 PNG 图片'}}}}


def chart(fig, key, height=330, detail=None, allow_zoom=True):
    # A reserved top row keeps every modebar button outside the data. Legends live below the axes.
    has_legend = fig.layout.showlegend is not False
    fig.update_layout(template='plotly_dark', height=height+70, paper_bgcolor='#191c2b',
        plot_bgcolor='#191c2b', font=dict(family='Microsoft YaHei, Arial', color='#c5cde0', size=12),
        margin=dict(l=10,r=20,t=64,b=90 if has_legend else 35),
        legend=dict(orientation='h', y=-.24, yanchor='top', x=0, xanchor='left',traceorder='normal',font_size=11),
        modebar=dict(bgcolor='rgba(0,0,0,0)',color='#aebbd5',activecolor='#c3b3ff'),
        hoverlabel=dict(font_size=13,bgcolor='#232736',font_color='#f2f4ff'),
        colorway=[PURPLE,'#79efd0','#ff968a','#ffd580','#70b8ff'],dragmode='pan')
    fig.update_xaxes(showgrid=False, zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor='rgba(180,190,215,.10)', zeroline=False, automargin=True)
    if key in ['volume_trend','sentiment_trend','rating_trend','issue_trend']:
        periods=list(dict.fromkeys(str(v) for v in fig.data[0].x))
        ticks=periods[::max(1,(len(periods)+5)//6)]
        fig.update_xaxes(type='category',tickmode='array',tickvals=ticks,
            ticktext=[v if len(v)==7 else v[5:] for v in ticks])
    if key=='issue_trend':
        legend_space=max(100,((len(fig.data)+1)//2)*26+40)
        fig.update_layout(height=max(height+70,320+legend_space),
            legend=dict(y=-.22,font_size=11),margin_b=legend_space)
    fig.update_traces(line_width=2.8,marker_size=6,selector=dict(type='scatter'))
    fig.update_traces(marker_line_width=.7,marker_line_color='#191c2b',selector=dict(type='bar'))
    if key in ['sentiment_trend','rating_trend']:
        # Percentages and star ratings retain their meaningful scales while dates move.
        fig.update_yaxes(fixedrange=True)
    st.plotly_chart(fig, width='stretch', key=key, theme=None, config=PLOT_CONFIG)
    if allow_zoom:
        st.button('⤢ 放大图表',key='zoom_'+key,width='stretch',help='打开大图；默认拖动平移，可切换框选缩放、恢复全图或下载',
            on_click=open_dialog,args=({'type':'chart','figure':fig.to_dict(),'key':key,'detail':detail,
                'scope':st.session_state.get('scope','')},))


def gradient_ranking(table, label, value, colors=PURPLE_SCALE, hover=None):
    fig=px.bar(table,x=value,y=label,orientation='h',color=value,color_continuous_scale=colors,
        range_color=(0,max(1,float(table[value].max()))),text=value,
        category_orders={label:table[label].tolist()},hover_data=hover)
    fig.update_layout(coloraxis_showscale=False,showlegend=False)
    fig.update_traces(textposition='outside',cliponaxis=False)
    fig.update_yaxes(title=None)
    fig.update_xaxes(range=[0,max(1,float(table[value].max()))*1.17])
    return fig


def clear_dialog():
    st.session_state.pop('dashboard_dialog',None)


def open_dialog(payload):
    # Streamlit 1.64 callbacks can request a full rerun, skipping the expensive
    # dashboard fragment that owns the clicked button. The main script opens
    # the saved chart first, outside that fragment, before refreshing the page.
    revision=st.session_state.get('dialog_revision',0)+1
    st.session_state.dialog_revision=revision
    st.session_state.pop('collection_dialog',None)
    st.session_state.dashboard_dialog={**payload,'revision':revision}
    st.rerun(scope='app')


def reset_chart(prefix):
    st.session_state[prefix+'_reset']=st.session_state.get(prefix+'_reset',0)+1


@st.dialog('图表放大',width='large',on_dismiss=clear_dialog)
def enlarged_chart(figure,key,detail=None,revision=0):
    prefix=f'large_{key}_{revision}'
    title,close=st.columns([5,1])
    title.markdown('**'+CHART_TITLES[key]+'**')
    if close.button('返回看板',key='close_large_'+key,width='stretch'): clear_dialog(); st.rerun()
    if detail:
        # Hidden lazy-tab widgets are otherwise removed by Streamlit. Retain
        # the reader's date and filters when returning from the enlarged plot.
        daily_prefix=f"zoom_day_{key}_{detail['scope']}"
        for widget_key in [daily_prefix+suffix for suffix in ['_day','_scope','_category','_review']]:
            if widget_key in st.session_state:
                st.session_state[widget_key]=st.session_state[widget_key]
        picture,day_tab=st.tabs(['放大图表','按天细看'],key=prefix+'_view',on_change='rerun')
        if day_tab.open:
            with day_tab: render_daily_detail(detail,'zoom_day_'+key)
        if not picture.open: return
    else:
        picture=st.container()
    with picture:
        render_enlarged_figure(figure,key,prefix)


def render_enlarged_figure(figure,key,prefix):
    fig=go.Figure(figure)
    has_axes=any(trace.type!='pie' for trace in fig.data)
    if has_axes:
        hint,reset=st.columns([5,1])
        hint.caption('默认拖动平移；图内工具栏可切换“框选缩放”。双击或点“恢复全图”还原，点击图例可筛选。')
        reset.button('恢复全图',key=prefix+'_restore',on_click=reset_chart,args=(prefix,),width='stretch')
    else:
        st.caption('悬停查看数量与占比；点击图例可筛选，工具栏可下载图片。')
    reset_count=st.session_state.get(prefix+'_reset',0)
    chart_key=f'{prefix}_{reset_count}'
    # Switch drag modes entirely in Plotly's toolbar. A server-side mode widget
    # remounts the chart and loses the region the user just zoomed into.
    # The source figure is captured before it is rendered in the overview.  Do
    # not carry a previous Plotly viewport or drag mode into this new canvas:
    # otherwise a rerun can reopen on a one-day x range and make bars appear
    # stretched horizontally.  Each open starts in pan mode; users can choose
    # box zoom from the toolbar and use Restore to return to the full range.
    fig.update_layout(width=None,height=None,autosize=True,font_size=13,dragmode='pan',
        legend=dict(font_size=12,y=-.2),margin_l=20,margin_r=35,margin_t=55,
        margin_b=85 if fig.layout.showlegend is not False else 40,uirevision=None)
    if key=='issue_trend':
        # A scrollable legend keeps every category accessible without squeezing the plot.
        fig.update_layout(margin_b=110,legend=dict(y=-.22,maxheight=100))
    long_ranking=key in ['keyword_ranking','category_comparison']
    source_height=int(figure.get('layout',{}).get('height') or 560)
    # Regular charts use an explicit pixel height so Plotly measures against
    # the dialog width rather than a stale stretch-container width.  Long
    # rankings keep their full canvas inside a scrollable reader.
    chart_height=max(520,min(source_height,760)) if not long_ranking else max(560,min(source_height,1200))
    # Match Plotly's own canvas height to the Streamlit slot.  Leaving the
    # source height in place is what made the modal clip or squeeze charts on
    # screens whose width differs from the overview.
    fig.update_layout(height=chart_height)
    if long_ranking:
        st.caption('完整榜单可在窗口内上下滚动查看。')
        panel_height=min(chart_height,680)
        with st.container(key='enlarged_chart_list',height=panel_height,border=False):
            st.plotly_chart(fig,width='stretch',height=chart_height,theme=None,key=chart_key,config=LARGE_PLOT_CONFIG)
    else:
        st.plotly_chart(fig,width='stretch',height=chart_height,theme=None,key=chart_key,config=LARGE_PLOT_CONFIG)


@st.dialog('逐日问题详情',width='large',on_dismiss=clear_dialog)
def daily_dialog(detail):
    if st.button('返回看板',key='close_daily'): clear_dialog(); st.rerun()
    render_daily_detail(detail,'daily')


def move_day(key,days,offset):
    index=days.index(st.session_state[key])
    st.session_state[key]=days[max(0,min(len(days)-1,index+offset))]


def render_daily_detail(detail,prefix):
    data,start,end=detail['data'],detail['start'],detail['end']
    scope=detail['scope']
    prefix=f'{prefix}_{scope}'
    st.caption(f"{detail['name']} · {detail['country'].upper()} · 中国时间 · {start:%Y-%m-%d %H:%M} — {end:%Y-%m-%d %H:%M}")
    daily=timeline(data,start,end)
    days=daily['时间'].tolist()
    counts=daily.set_index('时间')['评论数'].to_dict()
    populated=[day for day in days if counts[day]]
    default=populated[-1] if populated else days[-1]
    day_key=prefix+'_day'
    if st.session_state.get(day_key) not in days: st.session_state[day_key]=default
    previous,selector,next_day=st.columns([1,4,1])
    current=st.session_state[day_key]
    previous.button('← 前一天',key=prefix+'_previous',disabled=current==days[0],on_click=move_day,args=(day_key,days,-1))
    next_day.button('后一天 →',key=prefix+'_next',disabled=current==days[-1],on_click=move_day,args=(day_key,days,1))
    day=selector.selectbox('查看日期',days,key=day_key,format_func=lambda v:f'{v} · {counts[v]} 条评论')
    scoped=day_reviews(data,day,start,end)
    scope_label=st.radio('当天评论范围',['全部评论','负面相关评论'],horizontal=True,key=prefix+'_scope')
    selected=negative_rows(scoped) if scope_label=='负面相关评论' else scoped
    table=category_counts(selected,selected.iloc[:0]).rename(columns={'本期':'评论数','本期占比':'占比'})
    st.markdown(f'**{day} · {len(selected)} 条评论 · {len(table)} 类主题**')
    st.caption('一条评论可能涉及多个主题。这里只展示所选统计区间与当天的交集，未采集到评论不代表真实零评论。')
    if selected.empty:
        st.info('当天在当前范围内没有已采集评论，可切换日期或评论范围。')
        return
    figure=gradient_ranking(table,'类别','评论数',hover={'占比':':.1f'})
    chart(figure,'daily_categories_'+prefix,height=max(360,len(table)*30+90),allow_zoom=False)
    # This is an evidence reader, without a review/approval workflow.
    options=['全部类别']+table['类别'].tolist()
    filter_key=prefix+'_category'
    if st.session_state.get(filter_key) not in options: st.session_state[filter_key]='全部类别'
    category=st.selectbox('查看该类别的评论',options,key=filter_key)
    matched=selected if category=='全部类别' else selected.loc[category_mask(selected,category)]
    shown=matched.sort_values('date',ascending=False).copy()
    shown['日期']=pd.to_datetime(shown['date'],utc=True).dt.tz_convert(LOCAL_TZ).dt.strftime('%Y-%m-%d %H:%M')
    table_display=shown[['日期','rating','sentiment_label','issue_category','title','content']].rename(
        columns={'rating':'星级','sentiment_label':'情绪','issue_category':'主类别','title':'标题','content':'评论原文'})
    st.caption(f'对应 {len(shown)} 条评论，可在表格中浏览，或选择下方条目阅读完整原文。')
    st.dataframe(table_display,hide_index=True,width='stretch',height=260)
    st.download_button('下载当天筛选后的评论 Excel',excel_bytes({'当天评论':table_display,'当天类别':table}),
        f"AppStore_{detail['app_id']}_{detail['country']}_{day}_comments.xlsx",mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        key=prefix+'_download',on_click='ignore')
    review_key=prefix+'_review'
    choices=shown['review_id'].tolist()
    if st.session_state.get(review_key) not in choices: st.session_state[review_key]=choices[0]
    lookup=shown.set_index('review_id')
    review_id=st.selectbox('阅读完整评论',choices,key=review_key,
        format_func=lambda value:f"#{value} · {lookup.loc[value,'rating']} 星 · {str(lookup.loc[value,'content'])[:60]}")
    item=lookup.loc[review_id]
    st.markdown(card_html({**item.to_dict(),'review_id':review_id}),unsafe_allow_html=True)


@st.cache_data(max_entries=24, show_spinner=False)
def keywords(data, name):
    return keyword_counts(data, name,top_n=30)


def pct(value):
    return f'{value:.1f}%' if value is not None and pd.notna(value) else '—'


def core_metrics(view):
    st.subheader('核心指标')
    m = view['metrics']
    distribution = sentiments(view['current']).set_index('情绪')
    metrics = [('评论总数',f"{m['total']:,}",'gray'),
        ('样本平均星级',f"{m['avg_rating']:.2f} / 5" if m['avg_rating'] is not None else '—','gray'),
        ('1—2 星占比',pct(m['low_pct']),'red'),
        ('负面情绪评论',f"{int(distribution.loc['差评','评论数']):,}",'red')]
    for i, (col, (label,value,color)) in enumerate(zip(st.columns(4),metrics)):
        with col, st.container(key=f'kpi_{color}_{i}'):
            st.metric(label,value,border=True)
    for i,(col, label, color) in enumerate(zip(st.columns(4), SENTIMENTS,['green','gray','red','gray'])):
        with col, st.container(key=f'kpi_{color}_{i+4}'):
            st.metric(('待判定情绪' if label=='待复核' else label)+'占比', pct(distribution.loc[label,'占比']), border=True,
                help=f"{int(distribution.loc[label,'评论数'])} 条 / 全部 {m['total']} 条。"+
                    ('尚未分析或语义不明确的评论保留为待判定。' if view.get('analysis_mode')=='Agent分析' else '星级冲突、语言或语义不明确的评论保留为待判定。'))
    st.caption(f"低星 {m['low']} 条 / 有效星级 {m['rated']} 条；情绪占比以全部评论为分母。"
               '样本平均星级与 App Store 展示的累计评分不同。公开评论源不提供可靠互动数。')


def insights(profile, view, plans):
    st.subheader('舆情深度分析与建议')
    st.caption('规则初筛 · 以下主题归纳与处理方向来自规则及固定模板；可在页面上方运行 Agent 获取原文研判。')
    if view['risk'] in ['需要优先核查','持续关注']: st.warning(f"{view['risk']} · {view['reason']}")
    else: st.info(f"{view['risk']} · {view['reason']}")
    data = view['current']
    if data.empty:
        st.write('采集到评论后，这里将按实际主题给出重点问题、变化信号和处理建议。')
        return
    cols = st.columns(3)
    with cols[0], st.container(border=True,key='insight_problem'):
        st.markdown('**主要问题 · 玩家在反馈什么**')
        ranked = sorted(plans, key=lambda p: -p['count'])
        if ranked:
            p = ranked[0]
            st.write(f"最常见负面主题是「{p['category']}」，涉及 {p['count']} 条评论，占本期全部评论 {p['count']/len(data)*100:.1f}%。")
            st.caption(f"上期同主题 {p['previous_count']} 条；主题可能重叠。")
        else:
            st.write('当前未发现可归入具体负面主题的反馈。仍需结合样本量与采集状态判断。')
    with cols[1], st.container(border=True,key='insight_change'):
        st.markdown('**变化信号 · 与上期相比**')
        st.write(f"本期 {len(data)} 条，上期 {len(view['previous'])} 条，均为等长窗口中的已采集评论。")
        if view['delta'] is not None:
            direction = '上升' if view['delta']>0 else '下降' if view['delta']<0 else '持平'
            st.write(f"低星占比{direction}"+(f" {abs(view['delta']):.1f} 个百分点。" if view['delta'] else '。'))
        else:
            st.write('任一期有效星级少于 30 条，暂不下整体升降结论。')
        st.caption('结合发布、活动时间和采集覆盖核实原因。')
    with cols[2], st.container(border=True,key='insight_positive'):
        st.markdown('**正向口碑 · 可以继续验证什么**')
        positive = data.loc[data['sentiment_label'].eq('好评')]
        words = keywords(positive, profile['app_name'])
        st.write(f"识别好评 {len(positive)} 条。"+('常见用词：'+ '、'.join(words['关键词'].head(5))+'。' if len(words) else '当前缺少可归纳的正向文本。'))
        st.caption('用于寻找体验亮点与调研线索；关键词本身不证明满意原因。')
    if plans:
        st.markdown('**优先处理方向**')
        st.dataframe(pd.DataFrame([{'优先级':p['priority'],'问题':p['category'],'本期 / 上期':f"{p['count']} / {p['previous_count']}",
            '建议协作':p['owner'],'核心处理方法':p['action']} for p in plans[:3]]), hide_index=True, width='stretch')
        st.caption('P1 为优先核实，P2 为常规跟进。详细方案、验收方式和代表评论见页面下方。')


def emotion_charts(data):
    st.subheader('情绪分布')
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown('**评论情绪构成**')
        dist = sentiments(data)
        if data.empty: st.info('当前区间暂无评论，暂不绘制情绪比例。')
        else:
            fig = go.Figure(go.Pie(labels=dist['情绪'], values=dist['评论数'], hole=.73, sort=False,
                marker=dict(colors=[SENTIMENT_COLORS[s] for s in SENTIMENTS],line=dict(color='#191c2b',width=1.5)), textinfo='text',
                text=[f'{v:.1f}%' if v>=5 else '' for v in dist['占比']],textposition='inside',
                insidetextorientation='horizontal',textfont=dict(size=12,color='#182032'),
                hovertemplate='%{label}<br>%{value} 条 · %{percent}<extra></extra>'))
            fig.add_annotation(text=f'<b>{len(data):,}</b><br><span style="font-size:13px;color:#a8aec5">条评论</span>',
                x=.5,y=.5,showarrow=False,font=dict(size=26,color='#eee9f6'))
            chart(fig,'sentiment_distribution')
        st.caption('使用页面上方所选分析方式；待判定单独显示，不计入中评。')
    with right, st.container(border=True):
        st.markdown('**1—5 星分布**')
        dist = ratings(data)
        if not dist['评论数'].sum(): st.info('当前区间暂无有效星级。')
        else:
            fig = px.bar(dist,x='星级',y='评论数',text='评论数',color='星级',
                color_discrete_sequence=[SENTIMENT_COLORS['差评'],'#f4a0b5','#e9cb82','#91e0d1',SENTIMENT_COLORS['好评']],
                hover_data={'占比':':.1f'})
            fig.update_layout(showlegend=False,barcornerradius=4)
            fig.update_traces(width=.58,textposition='outside',cliponaxis=False,textfont=dict(color='#dad7e5',size=12))
            chart(fig,'rating_distribution')
        st.caption('星级是用户直接给出的评分；与正文情绪分别展示。')


def trend_charts(profile,view):
    st.subheader('趋势分析')
    grain = st.radio('时间粒度',['按日','按周','按月'],horizontal=True, key='trend_grain')
    data = view['current']
    if data.empty: st.info('当前区间暂无评论；请采集数据或切换历史区间。'); return
    detail={'data':data,'start':view['start'],'end':view['end'],'app_id':profile['app_id'],
        'country':profile['country'],'name':profile['app_name'],
        'scope':f"{profile['app_id']}_{profile['country']}_{view['start']:%Y%m%d}_{view['end']:%Y%m%d}"}
    st.caption('每张图下方可放大查看；逐日详情可以按日期与类别阅读对应评论。')
    st.button('按天查看全部问题与评论',key='daily_detail_open',type='primary',on_click=open_dialog,
        args=({'type':'daily','detail':detail,'scope':st.session_state.get('scope','')},))
    series = timeline(data,view['start'],view['end'],grain)
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown('**评论声量与负面变化**')
        fig = go.Figure()
        for col, name, color in [('评论数','全部评论',PURPLE),('差评','负面情绪',SENTIMENT_COLORS['差评']),('低星数','1—2 星','#ffd580')]:
            fig.add_trace(go.Scatter(x=series['时间'],y=series[col],name=name,mode='lines+markers',line_color=color))
        fig.update_yaxes(title='评论数', rangemode='tozero')
        chart(fig,'volume_trend',detail=detail)
    with right, st.container(border=True):
        st.markdown('**情绪构成随时间变化**')
        fig = go.Figure()
        for sentiment in SENTIMENTS:
            fig.add_trace(go.Bar(x=series['时间'],y=series[sentiment]/series['评论数'].replace(0,float('nan'))*100,
                name=sentiment,marker_color=SENTIMENT_COLORS[sentiment],customdata=series[sentiment],
                hovertemplate='%{x}<br>%{customdata} 条 · %{y:.1f}%<extra>%{fullData.name}</extra>'))
        fig.update_layout(barmode='stack')
        fig.update_yaxes(title='情绪占比 (%)',range=[0,100])
        chart(fig,'sentiment_trend',detail=detail)
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown('**低星占比与平均星级**')
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=series['时间'],y=series['低星占比'],name='1—2 星占比',mode='lines+markers',
            line_color=SENTIMENT_COLORS['差评'],customdata=series['有效星级'],connectgaps=False,
            hovertemplate='%{x}<br>%{y:.1f}% · 有效星级 %{customdata} 条<extra></extra>'))
        fig.add_trace(go.Scatter(x=series['时间'],y=series['平均星级'],name='平均星级',mode='lines+markers',
            line_color=SENTIMENT_COLORS['好评'],yaxis='y2',connectgaps=False,hovertemplate='%{x}<br>%{y:.2f} 星<extra></extra>'))
        fig.update_layout(yaxis2=dict(title='平均星级',overlaying='y',side='right',range=[1,5],dtick=1,showgrid=False))
        fig.update_layout(yaxis=dict(title='低星占比 (%)',range=[0,100]))
        chart(fig,'rating_trend',detail=detail)
    with right, st.container(border=True):
        st.markdown('**问题类别随时间变化**')
        trend_scope=st.selectbox('问题走势范围',['全部评论','负面相关评论'],key='trend_issue_scope')
        trend_data=data if trend_scope=='全部评论' else negative_rows(data)
        all_categories=category_counts(trend_data,trend_data.iloc[:0])['类别'].tolist()
        category_range=st.selectbox('走势类别',['全部类别','前 5 类'],key='trend_category_range')
        shown_categories=all_categories if category_range=='全部类别' else all_categories[:5]
        if not shown_categories: st.info('当前区间未识别问题主题。')
        else:
            trend = category_timeline(trend_data,view['start'],view['end'],shown_categories,grain)
            fig = px.bar(trend,x='时间',y='评论数',color='类别',color_discrete_map=CATEGORY_COLORS,
                category_orders={'类别':shown_categories},hover_data={'类别':True,'评论数':True})
            fig.update_layout(legend_title_text='')
            chart(fig,'issue_trend',max(440,330+len(shown_categories)*12),detail=detail)
            st.caption(f'显示 {len(shown_categories)} / {len(all_categories)} 类。不同颜色对应不同主题；同条评论可能涉及多类。')
    st.caption('按中国时间分桶；周从周一开始。首尾周期可能不完整。空档表示本地未采集到评论，比例和均分留空；不等于真实零评论。')


def platform_table_style(table, dimension):
    """Keep exact values sortable while giving the compact tables a shared visual hierarchy."""
    return (table.style
        .apply(lambda row:['background-color: '+('#1b1e2c' if row.name%2==0 else '#202333')]*len(row),axis=1)
        .set_properties(**{'color':'#d9dce9'})
        .set_properties(subset=[dimension],**{'color':'#c9b8ed'})
        .set_properties(subset=['评论数'],**{'color':'#f0eaf6'})
        .set_properties(subset=['低星占比'],**{'background-color':'#382631','color':'#efb6c6'})
        .set_properties(subset=['平均星级'],**{'background-color':'#29273d','color':'#ccc7e7'})
        .format({key:value for key,value in {'评论数':'{:,.0f}','来源提供':'{:,.0f}',
            '按日期推定':'{:,.0f}','低星占比':'{:.1f}%','平均星级':'{:.2f}'}.items() if key in table},na_rep='—'))


def platform_charts(profile, view, regions):
    st.subheader('平台维度分析')
    st.caption('当前数据源：App Store。版本图使用当前地区；地区对比仅包含同一 App ID、同一时间窗口的已添加地区。')
    data = view['current']
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown('**版本口碑 · 评论最多的 10 个版本**')
        table = versions(data)
        if table.empty: st.info('当前区间暂无版本样本。')
        else:
            melted = table.head(10)[['版本','来源提供','按日期推定',*SENTIMENTS]].melt(
                id_vars=['版本','来源提供','按日期推定'],value_vars=SENTIMENTS,var_name='情绪',value_name='评论数')
            fig = px.bar(melted,x='评论数',y='版本',color='情绪',orientation='h',color_discrete_map=SENTIMENT_COLORS,
                category_orders={'版本':table.head(10)['版本'].tolist(),'情绪':SENTIMENTS},
                hover_data={'来源提供':True,'按日期推定':True},
                labels={'来源提供':'来源提供（该版本）','按日期推定':'按日期推定（该版本）'})
            fig.update_traces(width=.5)
            fig.update_layout(barcornerradius=4,legend_title_text='')
            fig.update_yaxes(type='category')
            chart(fig,'version_sentiment')
            st.dataframe(platform_table_style(table[['版本','评论数','来源提供','按日期推定','低星占比','平均星级']].reset_index(drop=True),'版本'),
                hide_index=True, width='stretch',height=185)
        st.caption('优先采用评论来源提供的版本；缺失时可按该地区版本发布时期推定，并单独计数。按日期推定不代表玩家实际安装版本；缺少可核实发布记录时仍为「版本未知」。')
    with right, st.container(border=True):
        st.markdown('**同一游戏 · 地区低星对比**')
        if regions.empty or not regions['有效星级'].sum(): st.info('已添加地区在当前区间没有有效星级。')
        else:
            table = regions.copy()
            table['标记'] = table['地区'].map(lambda v:'当前地区' if v==profile['country'].upper() else '其他地区')
            fig = px.bar(table,x='地区',y='低星占比',color='标记',text='评论数',
                color_discrete_map={'当前地区':PURPLE,'其他地区':'#70b5fa'},
                hover_data={'评论数':True,'有效星级':True,'平均星级':':.2f','低星占比':':.1f'})
            fig.update_traces(texttemplate='%{text} 条',textposition='outside',cliponaxis=False)
            fig.update_yaxes(range=[0,110],title='低星占比 (%)')
            chart(fig,'region_comparison')
        if len(regions):
            st.dataframe(platform_table_style(regions[['地区','评论数','有效星级','低星占比','平均星级']].reset_index(drop=True),'地区'),
                hide_index=True,width='stretch',height=185)
        st.caption('只有一个地区时如实显示单个地区。少于 30 条有效星级仅作样本展示，不据此排名。')


def issue_keyword_charts(profile, view):
    st.subheader('问题类别与关键词')
    data = view['current']
    version_values=data.get('version_display',data.get('topic',pd.Series('',index=data.index)).map(version_label))
    version_values=version_values.fillna('版本未知').replace('','版本未知')
    version_options=['全部版本']+sorted(version_values.unique().tolist())
    if st.session_state.get('issue_keyword_version') not in version_options:
        st.session_state.issue_keyword_version='全部版本'
    selected_version=st.selectbox('版本筛选',version_options,key='issue_keyword_version',
        help='同时筛选下方的问题类别、关键词及对应评论原文；沿用页面所选统计时间。')
    if selected_version!='全部版本': data=data.loc[version_values.eq(selected_version)]
    st.caption(f'当前统计时间内 · {selected_version} · {len(data):,} 条评论。来源版本与按日期推定的版本共同参与筛选；推定不代表玩家实际安装版本。')
    left, right = st.columns(2)
    with left, st.container(border=True):
        scope = st.selectbox('问题统计范围',['全部评论','负面相关评论'],key='issue_scope')
        current = negative_rows(data) if scope=='负面相关评论' else data
        table = category_counts(current,current.iloc[:0])[['类别','本期','本期占比']].rename(
            columns={'本期':'评论数','本期占比':'占比'})
        category_limit=st.selectbox('排行类别',['全部类别','前 12 类'],key='category_limit')
        st.markdown('**问题类别分析**')
        if table.empty: st.info('当前筛选范围没有可展示的问题主题。')
        else:
            visible=table
            if category_limit=='前 12 类': visible=visible.head(12)
            fig=gradient_ranking(visible,'类别','评论数',hover={'占比':':.1f'})
            chart(fig,'category_comparison',max(390,len(visible)*30+80))
        unclassified = sum(categories_for(row)==[UNCLASSIFIED] for _, row in current.iterrows())
        classified=len(current)-unclassified
        st.caption(f'分类覆盖：{classified} / {len(current)} 条'+(f'（{classified/len(current)*100:.1f}%）' if len(current) else '')+
            f'；未归类 {unclassified} 条。支持 {len(ISSUE_RULES)} 类问题主题及正向口碑标记；每条评论可归入多个主题。')
        with st.expander('查看完整问题统计'):
            st.dataframe(table,hide_index=True,width='stretch')
            st.caption('支持的主题：'+'、'.join(ISSUE_RULES))
    with right, st.container(border=True):
        scope = st.selectbox('关键词范围',['全部评论','负面相关评论','好评评论','按问题类别'],key='keyword_scope')
        selected = data
        if scope=='负面相关评论': selected=negative_rows(data)
        elif scope=='好评评论': selected=data.loc[data['sentiment_label'].eq('好评')]
        elif scope=='按问题类别':
            choices = category_counts(data,data.iloc[:0])['类别'].tolist()
            if choices:
                if st.session_state.get('keyword_category') not in choices:
                    st.session_state.keyword_category=choices[0]
                category = st.selectbox('选择问题类别',choices,key='keyword_category')
                selected=data.loc[category_mask(data,category)]
            else: selected=data.iloc[:0]
        top_n=st.selectbox('关键词数量',[30,20,10],key='keyword_count')
        words = keywords(selected,profile['app_name']).head(top_n)
        st.markdown(f'**关键词 · 前 {top_n} 名**')
        if words.empty: st.info('当前范围没有可展示的关键词。')
        else:
            fig=gradient_ranking(words,'关键词','提及评论数',KEYWORD_SCALE,hover={'占比':':.1f'})
            chart(fig,'keyword_ranking',max(390,len(words)*27+80))
        st.caption(f'选中 {len(selected)} 条评论。同一词在单条评论中重复出现只计一次；已排除当前游戏名称、常用虚词和版本元数据。')
        st.caption('颜色从紫蓝过渡到薄荷青绿，越亮表示提及该词的评论越多。')
    # Trace a chart finding back to the actual review without opening an approval workflow.
    with st.expander('查看关键词对应的评论原文'):
        if len(words):
            if st.session_state.get('keyword_evidence') not in words['关键词'].tolist():
                st.session_state.keyword_evidence=words['关键词'].iloc[0]
            selected_word=st.selectbox('选择关键词',words['关键词'].tolist(),key='keyword_evidence')
            # Match the exact tokenizer output, using the same frequency definition as the bars.
            from modules.keyword_extractor import tokenize, build_dynamic_stopwords
            stops=build_dynamic_stopwords(profile['app_name'])
            matched=selected.loc[selected.apply(lambda r:selected_word in set(tokenize(
                str(r.get('title') or '')+' '+str(r.get('content') or ''),stops)),axis=1)]
            st.caption(f'包含该关键词 {len(matched)} 条，展示最近 30 条。')
            st.dataframe(matched.sort_values('date',ascending=False)[['date','rating','title','content']].head(30),
                hide_index=True,width='stretch')
        else: st.write('暂无关键词原文。')


def typical_reviews(data):
    st.subheader('典型评论卡片')
    st.caption('按主题覆盖与最近时间选择，单组最多 5 条；典型好评排除情绪未确认的高星评论。当前公开源缺少可靠互动数，不按点赞量排行。')
    featured=featured_reviews(data)
    for tab,(label,rows) in zip(st.tabs(list(featured)),featured.items()):
        with tab:
            if rows.empty: st.info('当前区间没有符合条件的评论。'); continue
            for start in range(0,len(rows),2):
                for col,(_,row) in zip(st.columns(2),rows.iloc[start:start+2].iterrows()):
                    with col: st.markdown(card_html(row),unsafe_allow_html=True)


def render_dashboard(profile, view, plans, regions):
    core_metrics(view)
    if view.get('analysis_mode')=='Agent分析': report_overview(view)
    else: insights(profile,view,plans)
    emotion_charts(view['current'])
    trend_charts(profile,view)
    platform_charts(profile,view,regions)
    issue_keyword_charts(profile,view)
    typical_reviews(view['current'])


def render_active_dialog():
    # Keep modal ownership outside the 15-second dashboard fragment. Opening a modal
    # triggers an app rerun; periodic data updates must not recreate its React subtree.
    active=st.session_state.get('dashboard_dialog')
    if active:
        if active['scope']!=st.session_state.get('scope',''): clear_dialog()
        elif active['type']=='daily': daily_dialog(active['detail'])
        else: enlarged_chart(active['figure'],active['key'],active['detail'],active['revision'])
