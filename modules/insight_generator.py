from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from .keyword_extractor import keyword_string
from .utils import PLATFORM_ORDER, ordered_values, truncate_text


UNCLASSIFIED_CATEGORIES = {"其他", "未归类/待复核"}
SOCIAL_PLATFORMS = {"小红书", "B站", "微博"}
HIGH_RISK_CATEGORIES = {"政治/历史争议", "账号/隐私安全", "服务器/网络问题", "Bug/闪退问题"}
MEDIUM_RISK_CATEGORIES = {
    "价值观/性别争议",
    "退款/下架诉求",
    "厂商信任/经营争议",
    "官方回应/公关信任",
    "外挂/作弊问题",
}


@dataclass(slots=True)
class OperationalInsights:
    overview: str
    positive_feedback: str
    negative_analysis: str
    community_opportunity: str
    actions: list[str]
    risk_level: str
    risk_reason: str
    classified_count: int
    unclassified_count: int
    classification_rate: float
    negative_count: int
    negative_pct: float
    top_issues: list[tuple[str, int]]

    def to_text(self) -> str:
        action_text = "\n".join(
            f"{index}. {action}" for index, action in enumerate(self.actions, start=1)
        )
        return "\n\n".join(
            [
                f"【整体舆情概况】\n{self.overview}",
                f"【玩家主要正向反馈】\n{self.positive_feedback}",
                f"【主要负面问题】\n{self.negative_analysis}",
                f"【社区运营机会】\n{self.community_opportunity}",
                f"【建议动作】\n{action_text}",
            ]
        )


def _clean_suggestion(value: object) -> str:
    text = str(value or "").strip()
    return re.sub(r"^【[^】]+】", "", text).strip()


def _fallback_action(category: str) -> str:
    actions = {
        "政治/历史争议": "立即复核相关素材、文案和版本配置，统一法务、公关与客服回应口径。",
        "账号/隐私安全": "同步安全与客服团队核查账号、隐私、封禁和数据安全反馈。",
        "服务器/网络问题": "按版本、机型和地区汇总日志，推动研发核查服务状态并准备客服说明。",
        "Bug/闪退问题": "汇总复现路径、版本和设备信息，建立缺陷优先级并同步修复进度。",
        "价值观/性别争议": "复核争议内容和玩家核心诉求，准备透明且可验证的对外说明。",
        "退款/下架诉求": "抽样核对退款与退游原因，评估用户流失规模并制定挽回方案。",
        "厂商信任/经营争议": "梳理玩家对承诺和商业决策的质疑，用可验证事实修复信任。",
        "官方回应/公关信任": "整理高频质疑，补充公告、FAQ、客服话术及后续反馈节点。",
        "付费/商业化问题": "复盘付费门槛、卡池和价格反馈，评估商业化体验调整空间。",
        "评分抗议/集中差评": "监测新增一星评论与评分变化，识别集中差评起因和扩散节点。",
        "角色/剧情争议": "按角色、剧情节点和版本归纳反馈，提交内容策划复盘。",
        "社区环境/玩家冲突": "识别争议话题与对立群体，加强评论区引导和社区秩序治理。",
        "可玩性/内容单薄": "归纳重复、单调和内容不足的环节，反馈玩法与内容团队。",
        "内容/玩法反馈": "聚合具体玩法诉求并按频次排序，形成版本优化候选清单。",
        "广告/宣传问题": "核对投放素材与实际玩法，修正可能造成预期落差的宣传表达。",
        "活动/奖励问题": "补充活动规则、奖励到账说明和 FAQ，并在评论区主动答疑。",
        "本地化/地区差异": "对比不同地区版本和本地化内容，统一版本说明与沟通口径。",
        "强负面口碑/情绪宣泄": "对强负面评论进行二次抽样，追溯情绪背后的具体产品问题。",
    }
    return actions.get(category, "抽样复核该类评论，明确责任团队、处理时限和后续监测指标。")


def _priority_for(category: str, ratio: float) -> str:
    if category in HIGH_RISK_CATEGORIES and ratio >= 0.03:
        return "P1"
    if category in HIGH_RISK_CATEGORIES or category in MEDIUM_RISK_CATEGORIES or ratio >= 0.15:
        return "P1"
    return "P2"


def _category_action(
    negative: pd.DataFrame,
    category: str,
    count: int,
    negative_total: int,
) -> str:
    ratio = count / negative_total if negative_total else 0
    category_rows = negative.loc[negative["issue_category"].eq(category)]
    suggestion = ""
    if "operation_suggestion" in category_rows.columns:
        suggestions = category_rows["operation_suggestion"].dropna().map(_clean_suggestion)
        suggestions = suggestions.loc[suggestions.ne("")]
        if not suggestions.empty:
            suggestion = suggestions.value_counts().index[0]
    suggestion = suggestion or _fallback_action(category)
    priority = _priority_for(category, ratio)
    return f"【{priority}｜{category}】命中 {count} 条，占负面评论 {ratio:.1%}。{suggestion}"


def build_operational_insights(
    data: pd.DataFrame,
    game_name: str = "当前游戏",
    extra_stopwords: set[str] | None = None,
) -> OperationalInsights:
    if data.empty:
        return OperationalInsights(
            overview="当前筛选条件下没有可分析内容。",
            positive_feedback="暂无正向反馈样本。",
            negative_analysis="暂无负面反馈样本。",
            community_opportunity="当前数据不足，无法判断社区运营机会。",
            actions=["补充有效评论数据后重新分析。"],
            risk_level="暂无数据",
            risk_reason="缺少可分析样本",
            classified_count=0,
            unclassified_count=0,
            classification_rate=0.0,
            negative_count=0,
            negative_pct=0.0,
            top_issues=[],
        )

    total = len(data)
    platforms = ordered_values(data["platform"].astype(str).tolist(), PLATFORM_ORDER)
    sentiments = data["sentiment_label"].value_counts()
    dominant = sentiments.index[0]
    positive = data.loc[data["sentiment_label"].eq("好评")]
    negative = data.loc[data["sentiment_label"].eq("差评")]
    negative_count = len(negative)
    negative_pct = negative_count / total * 100
    unclassified_mask = data["issue_category"].isin(UNCLASSIFIED_CATEGORIES)
    unclassified_count = int(unclassified_mask.sum())
    classified_count = total - unclassified_count
    classification_rate = classified_count / total * 100

    overview = (
        f"本次监测到 {game_name} 相关内容 {total} 条，覆盖 {'、'.join(platforms)} 等 {len(platforms)} 个平台。"
        f"整体情绪以{dominant}为主，好评占比 {sentiments.get('好评', 0) / total:.1%}，"
        f"差评占比 {negative_count / total:.1%}；问题分类覆盖率为 {classification_rate:.1f}%。"
    )

    positive_terms = keyword_string(positive, 4, extra_stopwords=extra_stopwords)
    if positive.empty:
        positive_feedback = "当前筛选范围暂无明确好评样本，不建议强行提炼正向卖点。"
    else:
        ranked_positive = positive.sort_values("interaction_count", ascending=False)
        typical_positive = truncate_text(ranked_positive.iloc[0]["content"], 56)
        positive_feedback = f"正向反馈高频表达为 {positive_terms}。代表性原文：“{typical_positive}”。"

    specific_negative = negative.loc[~negative["issue_category"].isin(UNCLASSIFIED_CATEGORIES)]
    issue_counts = specific_negative["issue_category"].value_counts()
    top_issues = [(str(category), int(count)) for category, count in issue_counts.head(5).items()]
    if top_issues:
        issue_details = "、".join(
            f"{category} {count} 条（占负面 {count / max(negative_count, 1):.1%}）"
            for category, count in top_issues
        )
        top_issue = top_issues[0][0]
        top_platforms = specific_negative.loc[
            specific_negative["issue_category"].eq(top_issue), "platform"
        ].value_counts()
        top_platform = str(top_platforms.index[0]) if not top_platforms.empty else "当前平台"
        negative_analysis = (
            f"主要明确问题为：{issue_details}。其中 {top_issue} 在 {top_platform} 最集中。"
            f"另有 {int(negative['issue_category'].isin(UNCLASSIFIED_CATEGORIES).sum())} 条负面评论待人工复核，"
            "不参与主要问题排行。"
        )
    else:
        negative_analysis = (
            f"当前有 {negative_count} 条差评，但尚未识别出稳定的具体问题主题，建议先抽样复核原文。"
        )

    available_social_platforms = sorted(set(platforms).intersection(SOCIAL_PLATFORMS))
    social_candidates = data.loc[
        data["platform"].isin(SOCIAL_PLATFORMS)
        & data["issue_category"].eq("社区二创/热梗")
        & data["sentiment_label"].ne("差评")
    ]
    if social_candidates.empty and not available_social_platforms:
        community_opportunity = (
            "当前筛选数据未覆盖小红书、B站或微博，App Store 评论不足以判断二创传播机会。"
            "建议补充社媒内容或平台导出数据后再评估热梗、UGC 和官方互动方向。"
        )
    elif social_candidates.empty:
        community_opportunity = (
            f"已覆盖 {'、'.join(available_social_platforms)}，但暂未检出明确的整活、二创、梗图或表情包候选。"
            "本期不建议强行包装热词，可继续观察高互动非差评内容。"
        )
    else:
        social_terms = keyword_string(social_candidates, 5, extra_stopwords=extra_stopwords)
        community_opportunity = (
            f"识别到 {len(social_candidates)} 条社区扩散候选，高频主题为 {social_terms}。"
            "建议优先复核高互动原文，再用于热词包装、内容征集或官方互动。"
        )

    actions = [
        _category_action(negative, category, count, negative_count)
        for category, count in top_issues[:4]
    ]
    if unclassified_count / total >= 0.1:
        actions.append(
            f"【P2｜人工复核】仍有 {unclassified_count} 条内容未归入具体问题，占总样本 {unclassified_count / total:.1%}。"
            "建议抽样补充新词和表达变体，待复核内容不作为正式舆情结论。"
        )
    if not available_social_platforms:
        actions.append(
            "【P2｜数据补充】当前缺少小红书、B站和微博样本；补充社媒数据后，再判断二创和社区扩散机会。"
        )
    elif not social_candidates.empty:
        actions.append(
            f"【P2｜社区放大】复核 {len(social_candidates)} 条二创候选，挑选高互动内容进入热词库和官方互动排期。"
        )
    if not actions:
        actions.append("【P2｜持续监测】当前未发现集中风险，继续按周追踪情绪、问题占比和高互动内容。")
    actions = actions[:6]

    high_risk_count = int(specific_negative["issue_category"].isin(HIGH_RISK_CATEGORIES).sum())
    if total < 30:
        risk_level = "样本不足"
        risk_reason = "少于 30 条样本，仅跟进具体反馈，不判定大盘风险"
    elif negative_pct >= 60 or high_risk_count / total >= 0.05:
        risk_level = "高风险"
        risk_reason = f"差评占比 {negative_pct:.1f}%，高风险问题 {high_risk_count} 条"
    elif negative_pct >= 30 or high_risk_count > 0:
        risk_level = "中风险"
        risk_reason = f"差评占比 {negative_pct:.1f}%，需跟进高频问题"
    else:
        risk_level = "低风险"
        risk_reason = f"差评占比 {negative_pct:.1f}%，暂未形成集中高风险"

    return OperationalInsights(
        overview=overview,
        positive_feedback=positive_feedback,
        negative_analysis=negative_analysis,
        community_opportunity=community_opportunity,
        actions=actions,
        risk_level=risk_level,
        risk_reason=risk_reason,
        classified_count=classified_count,
        unclassified_count=unclassified_count,
        classification_rate=classification_rate,
        negative_count=negative_count,
        negative_pct=negative_pct,
        top_issues=top_issues,
    )
