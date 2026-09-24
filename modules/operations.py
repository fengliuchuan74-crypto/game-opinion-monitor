"""App Store evidence, window comparisons and human-owned response workflow."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from .data_loader import prepare_dataframe
from .issue_classifier import ANALYSIS_RULES_VERSION, ISSUE_RULES, analyze_issues
from .sentiment import add_sentiment_analysis
from .storage import audit, database, utc_now

LOCAL_TZ = timezone(timedelta(hours=8))
STATUSES = ['待核实', '处理中', '观察效果', '已解决', '不处理']
SENTIMENTS = ['好评', '中评', '差评', '待复核']
CATEGORIES = list(ISSUE_RULES) + ['正向口碑/泛好评', '未归类/待复核']

# Ownership is a suggested role. Users assign a named owner before execution.
PLAYBOOKS = {
 '服务器/网络问题': ('客户端 / 服务端 / 客服', '核对发生时间、地区、网络环境与版本；比对服务监控和登录失败日志', '复现登录或断线路径，确认影响范围；已证实的服务故障先恢复服务，再发布状态说明', '同地区同版本新反馈是否下降、登录成功率是否恢复；24 小时后复查'),
 'Bug/闪退问题': ('客户端研发 / QA / 客服', '收集版本、机型、系统版本、触发步骤与错误日志；同类重复证据归组', '建立缺陷单并复现；验证临时绕行方法，确认修复版本后再更新 FAQ 和客服口径', 'QA 复现用例通过；发布后 24 / 72 小时同类新增评论与崩溃指标回看'),
 '账号/隐私安全': ('客服 / 安全 / 账号服务', '核实账号访问、封禁或隐私问题的具体类型；通过正式客服渠道收集必要信息', '优先人工核查并建立受限工单；确认范围和事实后确定处置与说明，不凭关键词定性', '工单核验完成、相关功能恢复；跟踪复发与未结个案'),
 '付费/商业化问题': ('商业化策划 / 支付 / 客服', '区分扣款未到账、价格体验和概率质疑；核对订单、区服、活动规则与配置', '到账故障走支付核单；价格或概率争议复核披露和配置；证实差异后确定修复或补偿方案', '到账工单处理结果、配置核验及 72 小时同类反馈趋势'),
 '退款/下架诉求': ('客服 / 运营负责人', '逐条区分退款、退游和停服诉求，关联具体原因与已知问题', '提供官方退款查询路径与客服入口；将可复现问题关联对应缺陷，记录无法确认的个案', '退款咨询积压与关联问题复发；不把口头退游等同实际流失'),
 '活动/奖励问题': ('活动策划 / 客服', '核对活动时间、资格、奖励条件、到账延迟与实际配置', '复测领取流程；补齐有歧义的说明；异常发奖先核对名单，修复或补发须由负责人确认', '活动用例通过、工单闭环、24 / 72 小时相同投诉变化'),
 '本地化/地区差异': ('本地化 / 地区运营 / QA', '定位原文、语言、地区和版本，核对翻译、功能及活动差异', '建立逐条文本或地区配置修订单；由对应语言人员复核，明确预期差异并修正错误', '译文与配置验收、对应地区新增问题变化'),
 '广告/宣传问题': ('投放 / 市场 / 产品', '收集具体广告素材、落地页及玩家看到的玩法差异', '核对素材承诺与实际体验；确认不一致后修订素材或说明，再同步客服', '涉及素材修订完成，后续评论中同类预期落差是否减少'),
 '养成/进度反馈': ('数值 / 系统策划', '按玩家阶段核对关卡、资源获得、任务耗时与进度卡点', '将高频卡点转为可验证的配置假设，结合行为数据评估；小范围验证后调整', '卡点转化、进度分布及相关评论变化；不能单靠评论决定数值'),
 '匹配/排队问题': ('匹配服务 / 玩法策划', '区分排队时长、队伍质量和断线，核对时段、段位与地区', '比对匹配日志和实际等待时长；复测异常场景，明确修复或配置调整', '等待时长与匹配成功率恢复，持续回看同类评论'),
 '外挂/作弊问题': ('反作弊 / 客服', '保存具体行为描述、发生场景与可核验的对局线索', '通过正式举报流程验证，合并重复工单；未经核实不公开认定个体作弊', '举报处理结果、复发样本与反作弊指标'),
 '移动端体验问题': ('客户端 / 性能 QA', '按机型、系统、版本定位发热、耗电、操作或适配问题', '建立设备复现矩阵；验证性能优化和设置说明，按影响面排修复优先级', '相关设备性能用例通过，版本后反馈趋势改善'),
 '政治/历史争议': ('项目负责人 / 公关 / 内容审核', '人工核对原文上下文、被指向的具体素材和传播时间，避免关键词误判', '汇总可核验事实、争议点和待确认项；由项目负责人组织专业复核并统一回应', '事实核验与内容处置完成；24 / 72 小时新增证据与同类反馈回看'),
 '价值观/性别争议': ('内容策划 / 项目负责人 / 公关', '定位具体剧情、角色、文案或活动，不从身份词单独推断争议', '复核具体表达和玩家诉求；明确可调整项与解释依据，回应经负责人确认', '相关内容复核完成、重复诉求与新反馈变化'),
 '厂商信任/经营争议': ('项目负责人 / 运营 / 公关', '列出玩家质疑的承诺、时间线和可验证资料', '区分已证实差异与未确认指控；用明确事实说明已完成与待处理事项', '承诺事项按节点完成，后续质疑是否获得证据回应'),
 '官方回应/公关信任': ('运营负责人 / 客服 / 公关', '汇总未被回答的问题与既有公告，识别矛盾和信息缺口', '建立统一 FAQ 与下一次反馈节点；只使用已核实事实，避免空泛保证', '核心问题答复覆盖、承诺节点完成和重复咨询变化'),
 '评分抗议/集中差评': ('运营分析 / 项目负责人', '比较新增一星数量、总评论量和内容，查找具体触发事件', '优先处理评论所指的实际问题；相同文字只标记待核查，不直接认定刷评', '同类问题修复与新增星级分布变化，保留自然反馈'),
 '社区环境/玩家冲突': ('社区运营 / 客服', '区分游戏内互动与商店评论中的冲突，核实具体场景', '针对可确认违规走举报与治理流程；围绕事实解释规则，避免扩大群体对立', '个案处理结果与相同场景复发'),
 '角色/剧情争议': ('内容策划 / 编剧 / 美术', '按角色、剧情节点和版本整理具体分歧及原文证据', '区分偏好与事实性错误；错误走修订，偏好进入内容评审与调研', '内容修订验收和后续反馈，保留不同偏好样本'),
 '可玩性/内容单薄': ('玩法 / 内容策划', '整理重复环节、内容缺口和玩家阶段，与行为数据核对', '把泛泛抱怨细化为场景及需求；评估版本候选方案并小规模验证', '相应玩法参与、内容消耗与后续反馈'),
 '内容/玩法反馈': ('玩法策划 / 运营', '归纳具体机制、角色或版本诉求，并保留支持与反对证据', '整理为需求候选及验证问题；结合行为数据与成本评审后排期', '方案验证结果、需求处理状态和后续玩家反馈'),
 '强负面口碑/情绪宣泄': ('客服 / 运营分析', '抽样寻找具体原因；无明确事实的内容保留为情绪信号', '邀请玩家补充问题场景；将补充证据转交对应团队，避免猜测原因或承诺补偿', '补充信息比例、可转交问题数与新反馈变化'),
 '未归类/待复核': ('客服 / 运营分析', '逐条阅读原文、确认语言与上下文，补充问题类别', '先人工分类并核对星级冲突；证据不足则记录待补信息，不自动定性', '复核队列清理、分类后可执行事项数量'),
}


def analyze_reviews(raw, db_path=None):
    if raw.empty:
        raw = pd.DataFrame(columns=['review_id','content_hash','date','content','title','rating','author','url','topic'])
        result = analyze_issues(add_sentiment_analysis(raw.assign(interaction_count=0)))
        result['date'] = pd.to_datetime(result['date'], utc=True)
        result['manual_reviewed'] = False
        result['override_stale'] = False
        from .agent_analysis import apply_agent_results
        result = apply_agent_results(result, db_path)
        return result
    result = analyze_issues(add_sentiment_analysis(prepare_dataframe(raw, 'App Store 本地评论库').data))
    result['date'] = pd.to_datetime(result['date'], utc=True, errors='coerce')
    result['rating'] = pd.to_numeric(result['rating'], errors='coerce').where(lambda x: x.between(1,5))
    from .agent_analysis import apply_agent_results
    result = apply_agent_results(result, db_path)
    result['manual_reviewed'], result['override_stale'] = False, False
    if db_path:
        with database(db_path) as connection:
            overrides = {row['review_id']:dict(row) for row in connection.execute('SELECT * FROM review_overrides')}
        for idx, row in result.iterrows():
            override = overrides.get(row.get('review_id'))
            if not override:
                continue
            if override['content_hash'] != row['content_hash']:
                result.loc[idx,'override_stale'] = True
                result.loc[idx,'needs_review'] = True
                continue
            result.loc[idx,'sentiment_label'] = override['sentiment']
            result.loc[idx,'issue_category'] = override['category']
            result.loc[idx,'issue_categories'] = override['category']
            result.loc[idx,'analysis_basis'] = '人工复核：' + override['reviewer']
            result.loc[idx,'analysis_source'] = '人工复核'
            result.loc[idx,'sentiment_score'] = {'好评':1.0,'中评':0.0,'差评':-1.0,'待复核':float('nan')}[override['sentiment']]
            result.loc[idx,'sentiment_keywords'] = ''
            result.loc[idx,'issue_keywords'] = ''
            result.loc[idx,'agent_status'] = '人工复核'
            result.loc[idx,'agent_reason'] = override['note']
            result.loc[idx,'agent_demand'] = ''
            result.loc[idx,'agent_target'] = ''
            result.loc[idx,'agent_quote'] = ''
            result.loc[idx,'manual_reviewed'] = True
            result.loc[idx,'needs_review'] = override['sentiment'] == '待复核' or override['category'] == '未归类/待复核'
            result.loc[idx,'agent_uncertain'] = result.loc[idx,'needs_review']
    if 'issue_categories' not in result:
        result['issue_categories'] = result['issue_category']
    result['needs_review'] = result['needs_review'].astype(bool) | result['issue_category'].eq('未归类/待复核')
    return result


def review_override(db_path, review_id, expected_hash, sentiment, category, reviewer, note):
    if sentiment not in SENTIMENTS or category not in CATEGORIES or not reviewer.strip() or not note.strip():
        raise ValueError('请填写复核人、依据，并选择有效情绪和类别')
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT content_hash FROM review_records WHERE id=?',(int(review_id),)).fetchone()
        if not row or row[0] != expected_hash:
            raise ValueError('原文已更新，请刷新后重新复核')
        payload = dict(sentiment=sentiment,category=category,reviewer=reviewer.strip(),note=note.strip())
        connection.execute('''INSERT INTO review_overrides VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(review_id) DO UPDATE SET content_hash=excluded.content_hash,sentiment=excluded.sentiment,
           category=excluded.category,note=excluded.note,reviewer=excluded.reviewer,updated_at=excluded.updated_at''',
           (int(review_id),expected_hash,sentiment,category,note.strip(),reviewer.strip(),utc_now()))
        audit(connection,'评论复核',review_id,reviewer,payload)


def snapshot(data, days=7, now=None, start=None, end=None):
    now = pd.Timestamp(now or datetime.now(LOCAL_TZ))
    if now.tzinfo is None:
        now = now.tz_localize(LOCAL_TZ)
    now = now.tz_convert(LOCAL_TZ)
    end = pd.Timestamp(end) if end is not None else now
    start = pd.Timestamp(start) if start is not None else now.normalize()-pd.Timedelta(days=days-1)
    if start.tzinfo is None: start = start.tz_localize(LOCAL_TZ)
    if end.tzinfo is None: end = end.tz_localize(LOCAL_TZ)
    if end <= start: raise ValueError('结束时间须晚于开始时间')
    previous_start = start-(end-start)
    dates = pd.to_datetime(data['date'], errors='coerce', utc=True)
    current = data.loc[dates.ge(start)&dates.lt(end)].copy()
    previous = data.loc[dates.ge(previous_start)&dates.lt(start)].copy()
    def stats(rows):
        rated = rows.loc[rows['rating'].between(1,5)]
        low = int(rated['rating'].le(2).sum())
        return dict(total=len(rows),rated=len(rated),low=low,
                    low_pct=round(low/len(rated)*100,1) if len(rated) else None,
                    avg_rating=round(float(rated['rating'].mean()),2) if len(rated) else None,
                    review_count=int(rows['needs_review'].sum()) if len(rows) else 0)
    metrics, prior = stats(current), stats(previous)
    comparable = metrics['rated']>=30 and prior['rated']>=30
    delta = round(metrics['low_pct']-prior['low_pct'],1) if comparable else None
    if not len(current): risk,reason = '暂无当期样本','当前时间范围没有评论，请检查采集状态或查看历史区间。'
    elif metrics['rated'] < 30: risk,reason = '样本不足','有效星级样本少于 30 条，仅跟进具体反馈，不判定整体异常。'
    elif comparable and delta >= 15 and metrics['low'] >= 10: risk,reason = '需要优先核查',f'低星占比较等长上期增加 {delta:.1f} 个百分点，且低星评论不少于 10 条。'
    elif metrics['low_pct'] >= 40 and metrics['low'] >= 10: risk,reason = '持续关注','当前样本低星占比至少 40%，低星评论不少于 10 条；先核实集中问题。'
    else: risk,reason = '常规巡检','本轮未达到设定的大盘提示阈值；仍需处理个别具体问题。'
    return dict(current=current,previous=previous,metrics=metrics,prior=prior,delta=delta,
                risk=risk,reason=reason,start=start,end=end,previous_start=previous_start,
                undated=int(dates.isna().sum()),future=int(dates.gt(now).sum()),rule_version=ANALYSIS_RULES_VERSION)


def issue_plans(view):
    current,previous = view['current'],view['previous']
    def negatives(rows):
        return rows.loc[rows['rating'].le(2)|rows['sentiment_label'].eq('差评')]
    current, previous = negatives(current),negatives(previous)
    plans=[]
    categories = sorted(set(c for value in current.get('issue_categories',[]) for c in str(value).split('；') if c))
    if len(current) and current['issue_category'].eq('未归类/待复核').any(): categories.append('未归类/待复核')
    for category in dict.fromkeys(categories):
        def matching(rows):
            return rows.loc[rows['issue_categories'].fillna('').str.split('；').map(lambda values: category in values) | rows['issue_category'].eq(category)]
        rows, prior = matching(current),matching(previous)
        if rows.empty: continue
        count = len(rows)
        serious = category in {'账号/隐私安全','政治/历史争议','Bug/闪退问题','服务器/网络问题'}
        surge = count>=5 and len(view['current'])>=30 and len(view['previous'])>=30 and count>=max(2*len(prior),len(prior)+5)
        priority = 'P1' if serious or surge else 'P2'
        owner,check,action,validation = PLAYBOOKS.get(category,PLAYBOOKS['未归类/待复核'])
        evidence = []
        for _, row in rows.sort_values('date',ascending=False).head(5).iterrows():
            evidence.append({k: (None if pd.isna(row.get(k)) else str(row.get(k))) for k in
                             ['review_id','date','title','content','rating','url','topic','content_hash','analysis_basis']})
        versions=rows['topic'].fillna('').value_counts().head(3)
        plans.append(dict(category=category,count=count,previous_count=len(prior),priority=priority,
          trigger=('严重个案优先核实；尚未确认事故' if serious else '同类反馈增加，需核实' if surge else '具体问题常规跟进'),
          owner=owner,first_response='当班内核实并指定责任人' if priority=='P1' else '2 个工作日内完成归类与分派',
          verify=check,action=action,validation=validation,evidence=evidence,
          evidence_ids=[int(v) for v in rows['review_id'].tolist()],
          versions={str(k):int(v) for k,v in versions.items() if k},
          review_count=int(rows['needs_review'].sum()),
          response_draft=f'感谢您反馈{category}相关体验。我们希望进一步核实具体情况，请通过游戏内客服补充发生时间、游戏版本和操作步骤。请勿在公开评论中留下账号或订单等个人资料。核实后我们会通过客服渠道跟进。',
          response_draft_en='Thank you for sharing your experience. Please contact in-game support with the time, game version, and steps involved so the team can investigate. Please do not post account or order details publicly.',
          window={'start':view['start'].isoformat(),'end':view['end'].isoformat()},rule_version=view['rule_version']))
    serious_categories={'账号/隐私安全','政治/历史争议','Bug/闪退问题','服务器/网络问题'}
    return sorted(plans,key=lambda plan:(plan['priority'],plan['category'] not in serious_categories,-plan['count']))


def create_action(db_path, app_id, country, plan, actor='本机运营'):
    stamp=utc_now()
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        existing=connection.execute("SELECT id FROM action_items WHERE app_id=? AND country=? AND category=? AND status NOT IN ('已解决','不处理')",(app_id,country,plan['category'])).fetchone()
        if existing: return existing[0],False
        action_id=connection.execute('''INSERT INTO action_items(app_id,country,category,title,priority,plan_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?)''',(app_id,country,plan['category'],plan['category']+'处理',plan['priority'],json.dumps(plan,ensure_ascii=False),stamp,stamp)).lastrowid
        audit(connection,'创建处理事项',action_id,actor,plan)
    return action_id,True


def actions_for(db_path,app_id,country):
    with database(db_path) as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM action_items WHERE app_id=? AND country=? ORDER BY id DESC',(app_id,country))]


def update_action(db_path,action_id,expected_version,status,owner,due_date,notes,actor):
    if status not in STATUSES or not actor.strip(): raise ValueError('请选择状态并填写更新人')
    if status not in ['待核实','不处理'] and not owner.strip(): raise ValueError('请指定实际责任人')
    if status in ['处理中','观察效果'] and not due_date: raise ValueError('请指定处理或回看的截止日期')
    if status in ['已解决','不处理','观察效果'] and not notes.strip(): raise ValueError('请记录处理证据、验证结果或不处理原因')
    if due_date:
        try: datetime.strptime(due_date,'%Y-%m-%d')
        except ValueError: raise ValueError('截止日期格式应为 YYYY-MM-DD')
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        old=connection.execute('SELECT * FROM action_items WHERE id=?',(int(action_id),)).fetchone()
        if not old or old['updated_at']!=expected_version: raise ValueError('事项已被更新，请刷新后再保存')
        payload=dict(status=status,owner=owner.strip(),due_date=due_date,notes=notes.strip())
        connection.execute('UPDATE action_items SET status=?,owner=?,due_date=?,notes=?,updated_at=? WHERE id=?',
                           (status,owner.strip(),due_date,notes.strip(),utc_now(),int(action_id)))
        audit(connection,'更新处理事项',action_id,actor,{'before':dict(old),'after':payload})


def dataset_hash(data):
    records=data.sort_values('review_id').astype(str).to_dict('records') if len(data) else []
    return hashlib.sha256(json.dumps(records,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
