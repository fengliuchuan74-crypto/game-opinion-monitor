from __future__ import annotations
import html
import json
from io import BytesIO
import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from .operations import dataset_hash
from .dashboard import chart_tables


def excel_bytes(sheets):
    buffer=BytesIO()
    with pd.ExcelWriter(buffer,engine='openpyxl') as writer:
        for name,data in sheets.items():
            export=data.copy()
            for column in export:
                export[column]=export[column].map(lambda v: v.isoformat() if hasattr(v,'isoformat') else
                    ILLEGAL_CHARACTERS_RE.sub('',v) if isinstance(v,str) else v)
            export.to_excel(writer,index=False,sheet_name=name[:31])
            worksheet=writer.sheets[name[:31]]
            for row in worksheet:
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
            worksheet.freeze_panes='A2'
            worksheet.auto_filter.ref=worksheet.dimensions
    return buffer.getvalue()


def metadata(profile,view,run=None):
    report=view.get('agent_report') or {}
    return dict(app_id=profile['app_id'],country=profile['country'],app_name=profile['app_name'],
        platform='App Store',start=view['start'].isoformat(),end=view['end'].isoformat(),
        previous_start=view['previous_start'].isoformat(),timezone='UTC+08:00',
        rules=view['rule_version'],dataset_sha256=dataset_hash(view['current']),last_run=run or {},
        analysis_mode=view.get('analysis_mode','规则初筛'),
        agent={key:report.get(key) for key in ['run_id','model','prompt_version','coverage','stale','stale_reason']}
              if view.get('analysis_mode')=='Agent分析' else {},
        source='Apple 公开评论列表 / RSS / 经确认的 App Store 导入；不保证完整历史',
        version_attribution='来源提供版本优先；缺失时可按该地区版本发布时期推定。按日期推定不代表玩家实际安装版本；无可核实发布记录时保留未知。',
        denominator='当前区间内有效星级评论；1—2星为低星',
        thresholds='样本30；低星至少10；较等长上期增加15个百分点；无可比上期时低星40%提示关注')


def build_report(profile,view,plans,actions,run=None,include_workflow=True):
    meta=metadata(profile,view,run)
    m=view['metrics']
    pct=f"{m['low_pct']}%" if m['low_pct'] is not None else '无有效星级'
    lines=[f"# {profile['app_name']} · {profile['country']} · App Store 舆情处理简报",
      f"统计区间：{view['start'].isoformat()} 至 {view['end'].isoformat()}（右端不含）",
      f"结论：{view['risk']}。{view['reason']}",
      f"评论 {m['total']} 条；有效星级 {m['rated']} 条；低星 {m['low']} 条；低星占比 {pct}；待复核 {m['review_count']} 条。",
      f"较等长上期变化：{str(view['delta'])+' 个百分点' if view['delta'] is not None else '样本不足，不作变化判断'}。",
      f"库中无日期 {view['undated']} 条、未来日期 {view['future']} 条；均不计入当前指标。",
      f"最新采集状态：{run.get('status','未采集') if run else '未采集'}；{run.get('detail','') if run else ''}",
      '公开评论样本不代表全体玩家；同一评论可涉及多个主题。低星风险提示使用固定规则阈值。',
      '分析口径：'+view.get('analysis_mode','规则初筛')+'；建议及原因假设需要业务核实。']
    if view.get('analysis_mode')=='Agent分析':
        plans=[]
        lines.extend(_agent_report_lines(view.get('agent_report')))
    elif not plans: lines.append('本区间没有可生成处理建议的负面主题；继续常规巡检并检查待复核内容。')
    for plan in plans:
        lines += [f"## {plan['priority']} · {plan['category']} · {plan['count']} 条（上期 {plan['previous_count']} 条）",
            f"触发依据：{plan['trigger']}；其中 {plan['review_count']} 条仍需复核。",
            f"建议协作：{plan['owner']}；首次处理：{plan['first_response']}。",
            f"先核实：{plan['verify']}",f"处理方法：{plan['action']}",f"验收与回看：{plan['validation']}",
            f"中文回复草稿（待审核）：{plan['response_draft']}",f"英文回复草稿（待审核）：{plan['response_draft_en']}"]
        for item in plan['evidence']:
            lines.append(f"证据 #{item['review_id']} | {item['date']} | {item['rating']} 星 | {item['topic']}\n{item['title']}\n{item['content']}")
    if include_workflow:
        lines.append('## 处理台账')
        if not actions: lines.append('尚未创建处理事项。')
        for action in actions:
            lines.append(f"#{action['id']} {action['category']}：{action['status']}；责任人：{action['owner'] or '待分派'}；截止：{action['due_date'] or '待确定'}；记录：{action['notes'] or '暂无'}")
    lines+=['## 数据与规则依据',json.dumps(meta,ensure_ascii=False,indent=2,default=str)]
    markdown='\n\n'.join(lines)
    def paragraph(line):
        tag='h1' if line.startswith('# ') else 'h2' if line.startswith('## ') else 'p'
        value=line.lstrip('# ') if tag!='p' else line
        return '<'+tag+'>'+html.escape(value).replace('\n','<br>')+'</'+tag+'>'
    content='\n'.join(paragraph(line) for line in lines)
    charts=_report_charts(profile,view)
    report='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>App Store 舆情处理简报</title><style>body{font:15px/1.8 system-ui;max-width:1100px;margin:36px auto;padding:20px;color:#e7eafa;background:#0c0d14}p,article{background:#171a24;padding:14px 18px;border:1px solid #303246;border-radius:12px;overflow-wrap:anywhere}h1{color:#b6aaff}h2{border-left:3px solid #8273ff;padding-left:12px}.charts{display:grid;grid-template-columns:1fr 1fr;gap:14px}.bar{height:9px;background:#a99aff;border-radius:6px;margin:4px 0 13px}.muted{color:#a2abc1}@media(max-width:700px){.charts{grid-template-columns:1fr}}@media print{body{background:white;color:#182438;margin:0}p,article{background:white;break-inside:avoid}}</style><body>'+charts+content+'</body></html>'
    return markdown,report,meta


def _agent_report_lines(report):
    if not report:
        return ['## Agent 深度分析','当前区间尚无完成的 Agent 深度报告；待分析评论不会被计作中性评价。']
    coverage=report.get('coverage') or {}
    lines=['## Agent 深度分析',f"任务 #{report.get('run_id','—')} · 模型 {report.get('model','—')} · 分析截至 {coverage.get('end_at','—')}",
           f"范围共 {coverage.get('total',0)} 条；已分析 {coverage.get('analyzed',0)} 条；待分析 {coverage.get('pending',0)} 条。结论仅依据已分析部分。"]
    if report.get('stale'):
        lines.append('历史报告，尚未覆盖当前数据：'+str(report.get('stale_reason') or '请重新分析更新。'))
    lines.append(str(report.get('summary') or '尚无可确认结论。'))
    evidence=report.get('evidence') or []
    lookup={str(r.get('review_id')):r for r in evidence} if isinstance(evidence,list) else evidence
    for item in report.get('findings') or []:
        lines += ['## '+str(item.get('title') or item.get('category')),
                  '问题类别：'+str(item.get('category'))+'；建议协作：'+str(item.get('owner')),
                  '观察：'+str(item.get('observation')),'可能原因（待验证）：'+str(item.get('hypothesis')),
                  '处理步骤：\n'+'\n'.join(f'{i}. {action}' for i,action in enumerate(item.get('actions') or [],1)),
                  '验收与回看：'+str(item.get('validation'))]
        for review_id in item.get('evidence_ids') or []:
            row=lookup.get(str(review_id))
            if row:
                lines.append(f"证据 #{review_id} | {row.get('date','')} | {row.get('rating','')} 星\n{row.get('title','')}\n{row.get('content','')}")
    if report.get('positive'): lines += ['## 正向反馈']+list(report['positive'])
    if report.get('limitations'): lines += ['## 覆盖与待确认事项']+list(report['limitations'])
    return lines


def _report_charts(profile,view):
    """Self-contained, escaped visual summaries; no external scripts or network."""
    tables=chart_tables(profile,view)
    articles=[]
    for title,label,value,color in [('情绪分布','情绪','评论数','#a99aff'),('星级分布','星级','评论数','#f6c86d'),
        ('负面问题类别','类别','本期','#f28383'),('关键词','关键词','提及评论数','#63d5b2')]:
        table=tables[title].head(12)
        maximum=max(1,float(table[value].max())) if len(table) else 1
        rows=''.join('<div>'+html.escape(str(row[label]))+' · '+str(int(row[value]))+' 条</div>'+
            f'<div class="bar" style="background:{color};width:{float(row[value])/maximum*100:.2f}%"></div>' for _,row in table.iterrows())
        articles.append('<article><h2>'+title+'</h2>'+(rows or '当前区间暂无数据')+'</article>')
    return '<h1>'+html.escape(profile['app_name'])+' · 舆情分析看板</h1><p class="muted">'+html.escape(
        f"App Store · {profile['country'].upper()} · {view['start']:%Y-%m-%d %H:%M} — {view['end']:%Y-%m-%d %H:%M} · 中国时间。关键词按提及评论数统计，问题主题可重叠。")+'</p><div class="charts">'+''.join(articles)+'</div>'


def workbook(profile,view,plans,actions,runs,include_workflow=True):
    meta=metadata(profile,view,runs[0] if runs else None)
    report=view.get('agent_report') or {}
    if view.get('analysis_mode')=='Agent分析': plans=report.get('findings') or []
    plan_rows=[{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in plan.items()} for plan in plans]
    sheets={**chart_tables(profile,view),'评论证据':view['current'],'上期对照':view['previous'],'处理建议':pd.DataFrame(plan_rows)}
    if view.get('analysis_mode')=='Agent分析':
        sheets['Agent深度报告']=pd.DataFrame({'内容':_agent_report_lines(report)})
        sheets['Agent统计依据']=pd.DataFrame([{'项目':k,'值':json.dumps(v,ensure_ascii=False,default=str)} for k,v in (report.get('stats') or {}).items()])
    if include_workflow:
        sheets.update({'处理台账':pd.DataFrame([{k:v for k,v in a.items() if k!='plan_json'} for a in actions]),
            '事项证据快照':pd.DataFrame([{'事项ID':a['id'],'创建时证据与草稿':a['plan_json']} for a in actions])})
    sheets.update({'采集记录':pd.DataFrame(runs),'统计口径':pd.DataFrame([{'项目':k,'值':str(v)} for k,v in meta.items()])})
    return excel_bytes(sheets)
