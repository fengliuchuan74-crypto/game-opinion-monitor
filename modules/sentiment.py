from __future__ import annotations

import math
import re

import pandas as pd

from .utils import combine_text_fields, clean_text


POSITIVE_TERMS = {
    "好玩": 1.4,
    "有趣": 1.2,
    "有意思": 1.3,
    "上头": 1.3,
    "可爱": 1.0,
    "朋友": 0.7,
    "开黑": 1.0,
    "欢乐": 1.2,
    "整活": 1.0,
    "喜欢": 1.2,
    "推荐": 1.2,
    "笑死": 1.2,
    "节目效果": 1.4,
    "神作": 1.6,
    "不错": 0.9,
    "爱了": 1.3,
    "流畅": 1.0,
    "惊喜": 1.1,
    "great": 1.2,
    "good": 0.9,
    "fun": 1.1,
    "amazing": 1.3,
    "love": 1.2,
    "enjoy": 1.0,
    "entertaining": 1.0,
    "addictive": 1.0,
    "beautiful": 1.0,
    "relaxing": 0.9,
    "best": 1.1,
    "hooked": 1.0,
    "recommend": 1.0,
    "excellent": 1.2,
    "awesome": 1.2,
}

NEGATIVE_TERMS = {
    "卡顿": 1.5,
    "太卡": 1.5,
    "服务器": 1.1,
    "闪退": 1.7,
    "进不去": 1.7,
    "外挂": 1.7,
    "作弊": 1.7,
    "bug": 1.3,
    "匹配慢": 1.3,
    "登录失败": 1.7,
    "掉线": 1.5,
    "优化差": 1.5,
    "体验差": 1.4,
    "退款": 1.4,
    "无语": 1.1,
    "恶心": 1.5,
    "失望": 1.3,
    "差劲": 1.5,
    "崩溃": 1.6,
    "黑屏": 1.5,
    "发热": 1.1,
    "耗电": 1.1,
    "挂机": 1.0,
    "不清楚": 0.8,
    "卡": 0.45,
    "bad": 1.0,
    "awful": 1.5,
    "boring": 1.2,
    "false advertising": 1.6,
    "misleading": 1.4,
    "paywall": 1.1,
    "sucks": 1.2,
    "pathetic": 1.4,
    "trash": 1.5,
    "scam": 1.6,
    "disappointed": 1.2,
    "grind": 0.8,
    "p2w": 1.2,
    "crash": 1.4,
    "lag": 1.1,
    "lifeless": 1.1,
    "pointless": 1.2,
    "annoying": 1.0,
    "fake": 1.0,
}


from .language_rules import normalize, occurrences, negated

# Neutral subjects are not sentiment. The score expresses a rule signal, not probability.
for term in ('服务器', '卡', 'grind'):
    NEGATIVE_TERMS.pop(term, None)
for term in ('朋友', '开黑', '整活'):
    POSITIVE_TERMS.pop(term, None)
POSITIVE_TERMS.update({'稳定': 1.2, '满意': 1.2, 'stable': 1.2, 'smooth': 1.2})
NEGATIVE_TERMS.update({'垃圾': 1.5, '太贵': 1.3, '骗钱': 1.5, '无法登录': 1.5,
                      '不推荐': 1.5, '差评': 1.2, 'terrible': 1.5, 'broken': 1.3,
                      'crashing': 1.4, 'laggy': 1.2, 'disconnect': 1.2,
                      '不能登录': 1.5, '无法登陆': 1.5, '登入失败': 1.5})


def analyze_sentiment(text: object, rating: object = None) -> dict[str, object]:
    normalized = normalize(clean_text(text))
    unsupported = bool(re.search('[\u3040-\u30ff\uac00-\ud7af]', normalized))
    positive, negative, hits = 0.0, 0.0, []
    occupied = set()
    lexicon = [(term, weight, sign) for terms, sign in [(POSITIVE_TERMS, 1), (NEGATIVE_TERMS, -1)]
               for term, weight in terms.items()]
    for term, weight, sign in sorted(lexicon, key=lambda item: -len(item[0])):
        for match in occurrences(normalized, term):
            positions = set(range(match.start(), match.end()))
            if occupied.intersection(positions):
                continue
            occupied.update(positions)
            is_negated = negated(normalized, match.start())
            direction = -sign if is_negated else sign
            hits.append(('否定:' if is_negated else '') + term)
            if direction > 0:
                positive += weight
            else:
                negative += weight
    value = pd.to_numeric(pd.Series([rating]), errors='coerce').iloc[0]
    valid_rating = pd.notna(value) and 1 <= value <= 5
    star_label = ('差评' if value <= 2 else '好评' if value >= 4 else '中评') if valid_rating else None
    signal_label = '中评' if positive and negative else '好评' if positive else '差评' if negative else None
    conflict = bool(star_label and signal_label and star_label != signal_label)
    uncertain = unsupported or signal_label is None or conflict or bool(positive and negative)
    if unsupported:
        label, basis = star_label or '待复核', '仅按星级；该语言正文需人工复核'
    elif signal_label is None:
        label, basis = star_label or '待复核', '仅按星级；未识别明确情绪' if star_label else '未识别明确情绪'
    elif conflict:
        label, basis = '待复核', '星级与正文信号不一致'
    else:
        label, basis = signal_label, '中英文词语与否定规则；混合情绪需复核' if positive and negative else '中英文词语与否定规则'
    return {'sentiment_label': label, 'sentiment_score': round(math.tanh((positive-negative)/2.8), 3),
            'sentiment_keywords': '、'.join(dict.fromkeys(hits)), 'analysis_basis': basis,
            'needs_review': uncertain, 'rating_label': star_label or '无有效星级'}


def add_sentiment_analysis(data: pd.DataFrame) -> pd.DataFrame:
    analyzed = data.copy()
    columns = list(analyze_sentiment('').keys())
    if analyzed.empty:
        for col in columns:
            analyzed[col] = pd.Series(dtype=object)
        return analyzed
    results = pd.DataFrame([analyze_sentiment(combine_text_fields(row), row.get('rating'))
                            for _, row in analyzed.iterrows()], index=analyzed.index)
    return pd.concat([analyzed.drop(columns=columns, errors='ignore'), results], axis=1)
