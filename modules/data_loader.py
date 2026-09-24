from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from .utils import clean_text


STANDARD_COLUMNS = [
    "platform",
    "date",
    "author",
    "title",
    "content",
    "rating",
    "likes",
    "comments",
    "shares",
    "url",
    "note_type",
    "topic",
]

LINEAGE_COLUMNS = ["data_source", "collected_at"]

FIELD_ALIASES = {
    "platform": ["platform", "平台", "来源平台", "渠道", "source", "channel"],
    "date": ["date", "日期", "发布时间", "发布日", "时间", "created_at", "publish_time"],
    "author": ["author", "作者", "用户名", "用户", "昵称", "user", "nickname"],
    "title": ["title", "标题", "笔记标题", "主题", "subject"],
    "content": ["content", "正文", "内容", "评论", "评论内容", "笔记正文", "文本", "body", "text"],
    "rating": ["rating", "评分", "星级", "score", "stars"],
    "likes": ["likes", "点赞", "点赞量", "赞", "like_count"],
    "comments": ["comments", "评论数", "回复数", "评论量", "comment_count"],
    "shares": ["shares", "分享", "转发", "分享量", "转发量", "share_count"],
    "url": ["url", "链接", "原始链接", "地址", "link"],
    "note_type": ["note_type", "内容类型", "类型", "笔记类型", "content_type"],
    "topic": ["topic", "话题", "标签", "话题标签", "tag", "hashtag"],
}


@dataclass
class DataPackage:
    data: pd.DataFrame
    original_rows: int
    removed_empty_rows: int
    removed_duplicates: int
    column_mapping: dict[str, str]
    source_name: str


def _normalized_column_name(name: object) -> str:
    return str(name).strip().lower().replace(" ", "").replace("_", "")


def _find_source_column(columns: list[str], aliases: list[str]) -> str | None:
    lookup = {_normalized_column_name(column): column for column in columns}
    for alias in aliases:
        matched = lookup.get(_normalized_column_name(alias))
        if matched is not None:
            return matched
    return None


def _read_csv(raw_bytes: bytes) -> pd.DataFrame:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return pd.read_csv(BytesIO(raw_bytes), encoding=encoding, dtype=str)
        except UnicodeDecodeError:
            errors.append(encoding)
    raise ValueError(f"CSV 编码无法识别，已尝试：{', '.join(errors)}。")


def normalize_datetime_column(values: pd.Series) -> pd.Series:
    """Parse uploaded/API dates to timezone-naive UTC datetimes.

    App Store review feeds include offsets such as ``-07:00`` while uploaded
    sheets often contain plain dates. Keeping both shapes in one column causes
    pandas comparison errors in date filters, so the app uses one internal
    representation here.
    """
    parsed = pd.to_datetime(values, errors="coerce", utc=True, format="mixed")
    return parsed.dt.tz_convert(None)


def read_file(file: BinaryIO, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".csv":
            return _read_csv(file.read())
        if suffix in {".xlsx", ".xls"}:
            return pd.read_excel(file, dtype=str)
    except Exception as exc:
        raise ValueError(f"读取文件失败：{exc}") from exc
    raise ValueError("仅支持上传 CSV、XLSX 或 XLS 文件。")


def prepare_dataframe(raw_data: pd.DataFrame, source_name: str = "uploaded") -> DataPackage:
    if raw_data is None or raw_data.empty:
        raise ValueError("文件中没有可分析的数据行。")

    data = raw_data.copy()
    original_rows = len(data)
    columns = [str(column) for column in data.columns]
    data.columns = columns
    mapping: dict[str, str] = {}

    for standard_name in STANDARD_COLUMNS:
        source_column = _find_source_column(columns, FIELD_ALIASES[standard_name])
        if source_column is not None:
            mapping[standard_name] = source_column
            if standard_name not in data.columns:
                data[standard_name] = data[source_column]
        elif standard_name not in data.columns:
            data[standard_name] = pd.NA

    data["title"] = data["title"].map(clean_text)
    data["content"] = data["content"].map(clean_text)
    has_title_only = data["content"].eq("") & data["title"].ne("")
    data.loc[has_title_only, "content"] = data.loc[has_title_only, "title"]
    data["platform"] = data["platform"].map(lambda value: clean_text(value) or "未知平台")
    data["author"] = data["author"].map(lambda value: clean_text(value) or "匿名玩家")
    data["note_type"] = data["note_type"].map(lambda value: clean_text(value) or "评论")
    data["date"] = normalize_datetime_column(data["date"])

    for column in ["rating", "likes", "comments", "shares"]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data[["likes", "comments", "shares"]] = data[["likes", "comments", "shares"]].fillna(0)
    data["interaction_count"] = data[["likes", "comments", "shares"]].sum(axis=1).astype(int)

    empty_mask = data["content"].eq("")
    removed_empty_rows = int(empty_mask.sum())
    data = data.loc[~empty_mask].copy()

    duplicate_columns = ["platform", "date", "author", "content"]
    before_deduplication = len(data)
    # Only stable source IDs establish identity; identical anonymous text can be independent reviews.
    if "external_id" in data:
        identified = data["external_id"].fillna("").astype(str).str.strip().ne("")
        identity = [c for c in ["platform", "app_id", "country", "external_id"] if c in data]
        duplicates = data.loc[identified].duplicated(subset=identity, keep="last")
        data = data.drop(index=duplicates[duplicates].index)
    removed_duplicates = before_deduplication - len(data)

    data["source_name"] = source_name
    if "data_source" not in data.columns:
        data["data_source"] = source_name
    if "collected_at" not in data.columns:
        data["collected_at"] = pd.NA
    data = data.reset_index(drop=True)
    return DataPackage(
        data=data,
        original_rows=original_rows,
        removed_empty_rows=removed_empty_rows,
        removed_duplicates=removed_duplicates,
        column_mapping=mapping,
        source_name=source_name,
    )


def load_sample_data(sample_path: Path) -> DataPackage:
    raw_data = pd.read_csv(sample_path, encoding="utf-8-sig")
    return prepare_dataframe(raw_data, source_name="内置示例数据")


def load_uploaded_data(uploaded_file: BinaryIO) -> DataPackage:
    filename = getattr(uploaded_file, "name", "uploaded.csv")
    if hasattr(uploaded_file, "seek"):
        uploaded_file.seek(0)
    raw_data = read_file(uploaded_file, filename)
    return prepare_dataframe(raw_data, source_name=filename)
