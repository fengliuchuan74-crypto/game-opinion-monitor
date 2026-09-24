from __future__ import annotations

import re
from collections import Counter

import pandas as pd

try:
    import jieba
except ImportError:  # pragma: no cover - fallback keeps uploads usable before install
    jieba = None


STOPWORDS = {
    "没有", "自己", "什么", "现在", "这么", "那么", "不会", "不能", "不是", "这种",
    "怎么", "很多", "一直", "不要", "还有", "而且", "知道", "觉得", "这些", "我们", "你们", "他们", "她们",
    "的",
    "了",
    "是",
    "我",
    "你",
    "他",
    "她",
    "它",
    "这个",
    "那个",
    "真的",
    "感觉",
    "一个",
    "还是",
    "但是",
    "不过",
    "因为",
    "所以",
    "游戏",
    "手游",
    "game",
    "games",
    "app",
    "apps",
    "play",
    "playing",
    "played",
    "player",
    "players",
    "the",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "from",
    "at",
    "by",
    "as",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "you",
    "your",
    "my",
    "me",
    "we",
    "our",
    "they",
    "their",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "not",
    "no",
    "yes",
    "but",
    "if",
    "so",
    "because",
    "just",
    "very",
    "really",
    "still",
    "also",
    "can",
    "cant",
    "cannot",
    "will",
    "would",
    "should",
    "could",
    "get",
    "got",
    "like",
    "one",
    "all",
    "more",
    "most",
    "much",
    "many",
    "一下",
    "比较",
    "非常",
    "有点",
    "就是",
    "已经",
    "可以",
    "希望",
    "之后",
    "目前",
    "这次",
    "版本",
    "version",
    "versions",
    "ver",
    "玩家",
    "玩",
    "挺",
    "太",
}

CUSTOM_TERMS = [
    "呆呆鸟",
    "甄嬛传",
    "节目效果",
    "新手引导",
    "匹配机制",
    "服务器",
    "活动奖励",
    "登录失败",
    "App Store",
]

if jieba is not None:
    for term in CUSTOM_TERMS:
        jieba.add_word(term)


def _normalize_content(text: object, game_aliases: set[str] | None = None) -> str:
    normalized = str(text).lower()
    for alias in sorted(game_aliases or set(), key=len, reverse=True):
        if alias:
            normalized = normalized.replace(alias.lower(), " ")
    normalized = re.sub(r"https?://\S+", " ", normalized)
    return normalized


def _keyword_topic_text(topic: object) -> str:
    """Keep user-provided tags, but remove collector metadata such as version:1.0."""

    text = str(topic or "")
    if not text:
        return ""
    parts = re.split(r"[,，;；\s]+", text)
    cleaned_parts = []
    for part in parts:
        value = part.strip()
        if not value:
            continue
        if re.match(r"^(version|ver|版本)\s*[:：]", value, flags=re.IGNORECASE):
            continue
        cleaned_parts.append(value)
    return " ".join(cleaned_parts)


def tokenize(text: object, extra_stopwords: set[str] | None = None) -> list[str]:
    dynamic_stopwords = {word.lower() for word in (extra_stopwords or set()) if word}
    normalized = _normalize_content(text, dynamic_stopwords)
    if jieba is not None:
        raw_tokens = jieba.lcut(normalized)
    else:
        raw_tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}", normalized)

    tokens: list[str] = []
    for token in raw_tokens:
        cleaned = token.strip().lower()
        cleaned = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]+", "", cleaned)
        if not cleaned or cleaned in STOPWORDS or cleaned in dynamic_stopwords or cleaned.isdigit():
            continue
        if len(cleaned) == 1:
            continue
        tokens.append(cleaned)
    return tokens


def build_dynamic_stopwords(*names: object) -> set[str]:
    words: set[str] = set()
    for name in names:
        text = str(name or "").strip().lower()
        if not text:
            continue
        words.add(text)
        words.update(re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}", text))
        compact = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "", text)
        if compact:
            words.add(compact)
    return words


def extract_keywords(
    data: pd.DataFrame, top_n: int = 30, extra_stopwords: set[str] | None = None
) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame(columns=["keyword", "count"])
    counter: Counter[str] = Counter()
    for _, row in data.iterrows():
        content = f"{row.get('title', '')} {row.get('content', '')} {_keyword_topic_text(row.get('topic', ''))}"
        counter.update(tokenize(content, extra_stopwords=extra_stopwords))
    return pd.DataFrame(counter.most_common(top_n), columns=["keyword", "count"])


def keyword_string(
    data: pd.DataFrame, top_n: int = 3, extra_stopwords: set[str] | None = None
) -> str:
    keywords = extract_keywords(data, top_n=top_n, extra_stopwords=extra_stopwords)
    return "、".join(keywords["keyword"].tolist()) if not keywords.empty else "暂无明显关键词"
