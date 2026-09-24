from __future__ import annotations

import re
from collections import OrderedDict

import pandas as pd

from .utils import combine_text_fields


ANALYSIS_RULES_VERSION = "2026.09.22-appstore-v4"


ISSUE_RULES = OrderedDict(
    [
        (
            "服务器/网络问题",
            [
                "服务器",
                "卡顿",
                "延迟",
                "掉线",
                "连接",
                "登录",
                "进不去",
                "网络",
                "太卡",
                "server",
                "lag",
                "latency",
                "disconnect",
                "connection",
                "login",
                "network",
                "loading",
                "stuck",
            ],
        ),
        (
            "Bug/闪退问题",
            [
                "bug",
                "闪退",
                "崩溃",
                "黑屏",
                "报错",
                "异常",
                "crash",
                "crashing",
                "freeze",
                "frozen",
                "black screen",
                "error",
                "glitch",
                "穿模",
                "卡死",
                "闪屏",
            ],
        ),
        ("广告/宣传问题", ["false advertising", "misleading", "advertising", "advertised", "ads", "nothing like"]),
        (
            "付费/商业化问题",
            [
                "付费",
                "氪金",
                "逼氪",
                "骗氪",
                "圈钱",
                "卡池",
                "抽卡",
                "月卡",
                "周卡",
                "充值",
                "价格",
                "太贵",
                "流水",
                "paywall",
                "pay wall",
                "p2w",
                "pay to win",
                "cash grab",
                "spend money",
                "purchase",
                "monetization",
                "gacha",
                "gotcha",
                "pulls",
            ],
        ),
        ("养成/进度反馈", ["time restrictions", "progression", "progress", "grind", "upgrade", "materials", "resources", "afk", "level", "gated", "stamina"]),
        (
            "评分抗议/集中差评",
            [
                "每日一星",
                "每天一星",
                "打一星",
                "一星送上",
                "一星记录",
                "一颗星",
                "星星礼包",
                "分期给星",
                "分期付款",
                "分五天",
                "分30天",
                "分三十天",
                "30颗星",
                "三十颗星",
                "掉分",
                "日活",
                "打卡",
                "刷差评",
                "评分抗议",
                "review bomb",
                "one star every day",
            ],
        ),
        (
            "价值观/性别争议",
            [
                "女性",
                "女玩家",
                "女主",
                "辱女",
                "厌女",
                "尊重女性",
                "女性消费者",
                "性别",
                "媚男",
                "性向",
                "全性向",
                "柜子",
                "gender",
                "female",
                "women",
                "woman",
                "girl",
                "sexism",
                "sexist",
                "misogyny",
                "misogynistic",
            ],
        ),
        (
            "政治/历史争议",
            [
                "辱华",
                "ru 华",
                "rh",
                "rn",
                "政治立场",
                "立场问题",
                "国家底线",
                "民族立场",
                "历史问题",
                "731",
                "611",
                "1113",
                "日本",
                "小日本",
                "鬼子",
                "中华人民共和国",
                "文化出口",
                "汉字",
                "传统节日",
                "刑事案件",
                "忘本",
                "辱国",
                "卖国",
                "汉奸",
                "倭寇",
                "走狗",
                "日资企业",
                "🇯🇵",
                "political",
                "politics",
                "history",
                "culture",
                "national",
            ],
        ),
        (
            "本地化/地区差异",
            [
                "国服",
                "外服",
                "国内服",
                "国际服",
                "海外服",
                "国内玩家",
                "海外玩家",
                "环大陆",
                "区别对待",
                "地区差异",
                "中文文案",
                "翻译",
                "本地化",
                "简体中文",
                "繁体中文",
                "global server",
                "global version",
                "localization",
                "localisation",
                "translation",
                "region locked",
            ],
        ),
        (
            "退款/下架诉求",
            [
                "退款",
                "退钱",
                "退游",
                "卸载",
                "下架",
                "停服",
                "关服",
                "倒闭",
                "别玩",
                "抵制",
                "refund",
                "refunded",
                "uninstall",
                "delete this",
                "quit",
                "boycott",
                "shut down",
                "remove",
            ],
        ),
        (
            "厂商信任/经营争议",
            [
                "诈骗",
                "骗子",
                "虚假承诺",
                "双标",
                "背刺",
                "无良厂商",
                "无良公司",
                "不尊重消费者",
                "不尊重玩家",
                "没有初心",
                "忘记初心",
                "杀猪盘",
                "割韭菜",
                "产能不足",
                "信用",
                "欺骗玩家",
                "scam",
                "fraud",
                "betray",
                "betrayed",
                "broken promise",
                "developer greed",
            ],
        ),
        (
            "官方回应/公关信任",
            [
                "回应",
                "解释",
                "道歉",
                "公告",
                "声明",
                "认错",
                "客服",
                "不听玩家",
                "玩家意见",
                "信用危机",
                "逃避",
                "解决",
                "处理",
                "态度",
                "补偿",
                "不公平对待",
                "发声",
                "官方",
                "策划",
                "运营",
                "拉流水",
                "买水军",
                "水军",
                "listen",
                "listening",
                "response",
                "respond",
                "communication",
                "communicate",
                "apology",
                "support",
                "customer service",
                "compensation",
            ],
        ),
        (
            "角色/剧情争议",
            [
                "剧情",
                "主线",
                "男主",
                "女主",
                "主控",
                "人设",
                "ooc",
                "建模",
                "站桩",
                "专属元素",
                "秦彻",
                "黎深",
                "祁煜",
                "夏以昼",
                "小野人",
                "胡萝卜",
                "香菜",
                "围裙",
                "story",
                "plot",
                "character",
                "characters",
                "route",
                "romance",
                "model",
                "animation",
                "voice acting",
            ],
        ),
        (
            "社区环境/玩家冲突",
            [
                "骂人",
                "吵架",
                "分化社区",
                "社区",
                "舅舅党",
                "带节奏",
                "节奏",
                "素质",
                "恶意",
                "举报",
                "攻击",
                "拉踩",
                "community",
                "toxic",
                "toxicity",
                "harassment",
                "bully",
                "bullying",
                "drama",
                "review bomb",
            ],
        ),
        (
            "账号/隐私安全",
            [
                "账号",
                "私自登陆",
                "隐私",
                "安全",
                "政策",
                "个人信息",
                "实名",
                "密码",
                "盗号",
                "封号",
                "account",
                "privacy",
                "policy",
                "personal information",
                "data leak",
                "user data",
                "security",
                "ban",
                "banned",
            ],
        ),
        (
            "强负面口碑/情绪宣泄",
            [
                "垃圾",
                "狗屎",
                "屎",
                "恶心",
                "烂",
                "辣鸡",
                "lj游戏",
                "拉完",
                "太烂",
                "难玩",
                "没意思",
                "很无聊",
                "无聊",
                "不好",
                "不推荐",
                "避雷",
                "差评",
                "差劲",
                "很差",
                "差差差",
                "极差",
                "超级差",
                "无比差",
                "很失望",
                "差的没边",
                "完蛋",
                "最恨",
                "破游戏",
                "一坨",
                "勾石",
                "依托构思",
                "真不要脸",
                "失望透顶",
                "失望至极",
                "寒心",
                "无语",
                "污染我的眼睛",
                "绝不姑息",
                "活该",
                "白吃",
                "bad",
                "trash",
                "garbage",
                "awful",
                "terrible",
                "horrible",
                "boring",
                "disgusting",
                "worst",
                "hate",
            ],
        ),
        ("外挂/作弊问题", ["外挂", "作弊", "脚本", "开挂", "cheat", "cheating", "hack", "hacker", "bot", "bots"]),
        ("匹配/排队问题", ["匹配", "排队", "等太久", "没人", "队友", "挂机", "matchmaking", "queue", "teammate"]),
        ("移动端体验问题", ["手机", "移动端", "适配", "发热", "耗电", "操作", "phone", "mobile", "device", "battery", "overheat", "controls", "iphone", "ipad"]),
        ("活动/奖励问题", ["活动", "奖励", "抽奖", "积分", "不到账", "规则", "不清楚", "event", "reward", "rewards", "lottery", "points", "not received", "rules", "unclear"]),
        (
            "可玩性/内容单薄",
            [
                "可玩性低",
                "可玩性极低",
                "内容少",
                "没内容",
                "没有内容",
                "内容单薄",
                "内容重复",
                "重复度高",
                "玩法单一",
                "没东西玩",
                "能玩的东西",
                "很无聊",
                "太无聊",
                "无聊的游戏",
                "lack of content",
                "not enough content",
                "repetitive",
                "nothing to do",
                "boring gameplay",
            ],
        ),
        (
            "内容/玩法反馈",
            [
                "角色",
                "地图",
                "模式",
                "玩法",
                "可玩性",
                "内容少",
                "没内容",
                "内容单薄",
                "重复度",
                "更新",
                "赛季",
                "character",
                "characters",
                "customization",
                "race choice",
                "class",
                "map",
                "mode",
                "gameplay",
                "story",
                "rpg",
                "quest",
                "dungeon",
                "combat",
                "gear",
                "skill",
                "skills",
                "pets",
                "relics",
                "graphics",
                "voice acting",
                "collaboration",
            ],
        ),
        ("社区二创/热梗", ["整活", "梗", "表情包", "甄嬛传", "呆呆鸟", "穿搭", "二创", "名场面", "meme", "fan art", "cosplay"]),
    ]
)


# When one review mentions several topics, operational risk determines the
# primary category. The full set of hits is still retained in matched_keywords.
ISSUE_PRIORITY = [
    "政治/历史争议",
    "账号/隐私安全",
    "价值观/性别争议",
    "服务器/网络问题",
    "Bug/闪退问题",
    "外挂/作弊问题",
    "广告/宣传问题",
    "厂商信任/经营争议",
    "退款/下架诉求",
    "官方回应/公关信任",
    "付费/商业化问题",
    "活动/奖励问题",
    "评分抗议/集中差评",
    "本地化/地区差异",
    "社区环境/玩家冲突",
    "匹配/排队问题",
    "移动端体验问题",
    "角色/剧情争议",
    "养成/进度反馈",
    "可玩性/内容单薄",
    "内容/玩法反馈",
    "社区二创/热梗",
    "强负面口碑/情绪宣泄",
]
ISSUE_PRIORITY_RANK = {category: rank for rank, category in enumerate(ISSUE_PRIORITY)}


def _keyword_matches(text: str, keyword: str) -> bool:
    normalized_keyword = keyword.lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9\s/\-]*", normalized_keyword):
        pattern = r"(?<![a-z0-9])" + re.escape(normalized_keyword) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return normalized_keyword in text


# Remove broad subjects that should not independently accuse an incident.
from .language_rules import active_hits, normalize

_BROAD = {'更新','服务器','连接','登录','网络','server','connection','login','network',
          '日本','汉字','传统节日','history','culture','national','rh','rn','🇯🇵',
          '女性','女玩家','女主','女','gender','female','women','woman','girl',
          '账号','安全','政策','实名','account','policy','security',
          '手机','移动端','操作','phone','mobile','device','iphone','ipad',
          '日活','打卡','活动','奖励','规则','event','reward','rewards','rules'}
for _category, _terms in ISSUE_RULES.items():
    ISSUE_RULES[_category] = [term for term in _terms if term not in _BROAD]
ISSUE_RULES['服务器/网络问题'] += ['登录失败','无法登录','不能登录','连不上','登入失败','伺服器异常','server down','cannot log in','unable to login']
ISSUE_RULES['活动/奖励问题'] += ['奖励没收到','奖励没有到账','活动规则不清楚','奖励消失','missing reward']


def classify_issue(text: object) -> dict[str, str]:
    text = normalize(text)
    matches = {category: active_hits(text, terms) for category, terms in ISSUE_RULES.items()}
    matches = {category: hits for category, hits in matches.items() if hits}
    ordered = sorted(matches, key=lambda category: ISSUE_PRIORITY_RANK.get(category, 999))
    return {'issue_category': ordered[0] if ordered else '未归类/待复核',
            'issue_categories': '；'.join(ordered),
            'matched_keywords': '、'.join(dict.fromkeys(term for c in ordered for term in matches[c]))}


def add_issue_categories(data: pd.DataFrame) -> pd.DataFrame:
    analyzed = data.copy()
    if analyzed.empty:
        for column in ['issue_category', 'issue_categories', 'matched_keywords']:
            analyzed[column] = pd.Series(dtype=str)
        return analyzed
    def classify_row(row: pd.Series) -> dict[str, str]:
        result = classify_issue(combine_text_fields(row))
        if result["issue_category"] == "未归类/待复核" and row.get("sentiment_label") == "好评":
            result["issue_category"] = "正向口碑/泛好评"
        return result

    classified = analyzed.apply(classify_row, axis=1, result_type="expand")
    return pd.concat([analyzed, classified], axis=1)


def _operation_suggestion(row: pd.Series) -> str:
    label = row.get("sentiment_label", "中评")
    issue = row.get("issue_category", "未归类/待复核")
    high_interaction = bool(row.get("high_interaction", False))
    if label == "差评" and issue in {"服务器/网络问题", "Bug/闪退问题"}:
        return "【产品/技术问题】需要同步研发或客服，核查版本与服务状态。"
    if issue == "广告/宣传问题":
        return "【投放素材核查】建议核对广告素材与实际玩法一致性，关注误导宣传风险。"
    if issue == "付费/商业化问题":
        return "【商业化体验优化】建议关注付费门槛、抽卡与 P2W 相关负面反馈。"
    if issue == "养成/进度反馈":
        return "【玩法节奏优化】建议关注进度卡点、养成资源与每日体验时长。"
    if issue == "评分抗议/集中差评":
        return "【集中差评预警】建议识别差评行动的起因与扩散节奏，并单独跟踪评分和新增评论变化。"
    if issue == "价值观/性别争议":
        return "【价值观舆情核查】建议优先复核争议内容、话术与社区反馈，必要时准备公开回应口径。"
    if issue == "政治/历史争议":
        return "【高优先级合规舆情】建议立即复核相关素材、文本和活动配置，并同步法务/公关/发行团队。"
    if issue == "本地化/地区差异":
        return "【地区版本体验核查】建议对比国服、外服及本地化内容差异，统一版本说明和玩家沟通口径。"
    if issue == "退款/下架诉求":
        return "【流失与退款风险】建议关注差评扩散、退款诉求和下架舆情，整理高频证据给运营负责人。"
    if issue == "厂商信任/经营争议":
        return "【厂商信任修复】建议梳理玩家对承诺、商业决策和消费者态度的质疑，准备可验证的回应信息。"
    if issue == "官方回应/公关信任":
        return "【公关回应优化】建议汇总玩家核心质疑，评估是否需要公告、FAQ、客服话术或补偿说明。"
    if issue == "角色/剧情争议":
        return "【内容体验复盘】建议整理角色、人设、剧情与美术表现相关反馈，反馈给内容策划与制作团队。"
    if issue == "社区环境/玩家冲突":
        return "【社区秩序治理】建议关注骂战、带节奏和玩家对立，必要时加强评论区引导与 moderation。"
    if issue == "账号/隐私安全":
        return "【账号与隐私风险】建议同步客服与安全团队核查账号、隐私、封禁或政策相关反馈。"
    if issue == "强负面口碑/情绪宣泄":
        return "【负面口碑观察】建议聚合原文样本，判断是否由具体问题引发，持续跟踪扩散趋势。"
    if issue == "可玩性/内容单薄":
        return "【内容供给优化】建议归纳玩家认为单调、重复或内容不足的环节，反馈给玩法与内容团队。"
    if issue == "活动/奖励问题":
        return "【活动规则优化】建议补充 FAQ，并及时在评论区答疑。"
    if issue == "社区二创/热梗":
        return "【可二创扩散】适合进入热词库或发起官方互动。"
    if label == "好评" and high_interaction:
        return "【可放大传播】适合互动、二次转发或收录为用户反馈素材。"
    if label == "中评":
        return "【持续观察】关注后续同类反馈是否集中出现。"
    if label == "差评":
        return "【舆情跟进】建议汇总问题证据并观察负面扩散趋势。"
    return "【正向反馈沉淀】可作为玩法认可点纳入日常素材库。"


def add_operation_suggestions(data: pd.DataFrame) -> pd.DataFrame:
    analyzed = data.copy()
    if analyzed.empty:
        analyzed["high_interaction"] = pd.Series(dtype=bool)
        analyzed["operation_suggestion"] = pd.Series(dtype=str)
        return analyzed
    positive_interactions = analyzed.loc[analyzed["interaction_count"] > 0, "interaction_count"]
    threshold = max(5, int(positive_interactions.quantile(0.75))) if not positive_interactions.empty else 5
    analyzed["high_interaction"] = analyzed["interaction_count"].ge(threshold)
    analyzed["operation_suggestion"] = analyzed.apply(_operation_suggestion, axis=1)
    return analyzed


def analyze_issues(data: pd.DataFrame) -> pd.DataFrame:
    return add_operation_suggestions(add_issue_categories(data))
