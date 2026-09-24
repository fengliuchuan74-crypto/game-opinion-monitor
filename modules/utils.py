from __future__ import annotations

import html
import re
from typing import Iterable

import pandas as pd


PLATFORM_ORDER = ["TapTap", "小红书", "App Store", "B站", "微博"]
SENTIMENT_ORDER = ["好评", "中评", "差评"]
SENTIMENT_COLORS = {"好评": "#63d5b2", "中评": "#9da6b5", "差评": "#f28368"}
PLATFORM_COLORS = {
    "TapTap": "#8173ff",
    "小红书": "#e86888",
    "App Store": "#4d9bff",
    "B站": "#40bfe8",
    "微博": "#f2a34f",
}


def safe_text(value: object, default: str = "") -> str:
    """Return a stripped string without pandas null representations."""
    if value is None or pd.isna(value):
        return default
    return str(value).strip() or default


def clean_text(value: object) -> str:
    text = safe_text(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def html_text(value: object, default: str = "") -> str:
    return html.escape(safe_text(value, default))


def truncate_text(value: object, limit: int = 160) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}..."


def format_number(value: object) -> str:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = 0
    if number >= 10000:
        return f"{number / 10000:.1f}w"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)


def ordered_values(values: Iterable[str], preferred: list[str]) -> list[str]:
    present = {safe_text(value) for value in values if safe_text(value)}
    first = [value for value in preferred if value in present]
    return first + sorted(present.difference(first))


def date_label(value: object) -> str:
    if value is None or pd.isna(value):
        return "日期未知"
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def combine_text_fields(row: pd.Series) -> str:
    return clean_text(f"{safe_text(row.get('title'))} {safe_text(row.get('content'))}")
