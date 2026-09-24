from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd

from .insight_generator import build_operational_insights
from .utils import (
    PLATFORM_ORDER,
    SENTIMENT_COLORS,
    SENTIMENT_ORDER,
    date_label,
    format_number,
    html_text,
    ordered_values,
    safe_text,
    truncate_text,
)


ANALYSIS_COLUMNS = [
    "sentiment_label",
    "sentiment_score",
    "sentiment_keywords",
    "issue_category",
    "matched_keywords",
    "high_interaction",
    "operation_suggestion",
]


def compute_kpis(data: pd.DataFrame) -> dict[str, float | int]:
    total = len(data)
    counts = data["sentiment_label"].value_counts() if total else pd.Series(dtype=int)
    return {
        "total": total,
        "platforms": int(data["platform"].nunique()) if total else 0,
        "interactions": int(data["interaction_count"].sum()) if total else 0,
        "positive_pct": counts.get("好评", 0) / total * 100 if total else 0,
        "neutral_pct": counts.get("中评", 0) / total * 100 if total else 0,
        "negative_pct": counts.get("差评", 0) / total * 100 if total else 0,
        "negative_count": int(counts.get("差评", 0)),
        "high_interaction_count": int(data["high_interaction"].sum()) if total else 0,
    }


def sentiment_distribution(data: pd.DataFrame) -> pd.DataFrame:
    total = len(data)
    values = data["sentiment_label"].value_counts() if total else pd.Series(dtype=int)
    return pd.DataFrame(
        [
            {
                "sentiment_label": label,
                "count": int(values.get(label, 0)),
                "percentage": values.get(label, 0) / total * 100 if total else 0,
            }
            for label in SENTIMENT_ORDER
        ]
    )


def platform_overview(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    platforms = ordered_values(data["platform"].tolist(), PLATFORM_ORDER)
    rows = []
    for platform in platforms:
        group = data.loc[data["platform"] == platform]
        negative = group.loc[group["sentiment_label"] == "差评"]
        top_problem = (
            negative["issue_category"].value_counts().index[0]
            if not negative.empty
            else "暂无集中差评"
        )
        counts = group["sentiment_label"].value_counts()
        rows.append(
            {
                "平台": platform,
                "内容数": len(group),
                "好评占比": counts.get("好评", 0) / len(group),
                "中评占比": counts.get("中评", 0) / len(group),
                "差评占比": counts.get("差评", 0) / len(group),
                "平均情绪分": round(group["sentiment_score"].mean(), 3),
                "负面 Top 问题": top_problem,
            }
        )
    return pd.DataFrame(rows)


def featured_sets(data: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if data.empty:
        return {name: data.copy() for name in ["高互动好评", "高互动差评", "近期新增负面", "社区热词机会"]}
    community_platforms = ["小红书", "B站", "微博"]
    social_mask = (
        data["platform"].isin(community_platforms)
        & data["issue_category"].eq("社区二创/热梗")
        & data["sentiment_label"].ne("差评")
    )
    return {
        "高互动好评": data.loc[data["sentiment_label"] == "好评"]
        .sort_values(["interaction_count", "date"], ascending=[False, False])
        .head(5),
        "高互动差评": data.loc[data["sentiment_label"] == "差评"]
        .sort_values(["interaction_count", "date"], ascending=[False, False])
        .head(5),
        "近期新增负面": data.loc[data["sentiment_label"] == "差评"]
        .sort_values(["date", "interaction_count"], ascending=[False, False])
        .head(5),
        "社区热词机会": data.loc[social_mask]
        .sort_values(["interaction_count", "date"], ascending=[False, False])
        .head(5),
    }


def generate_operational_summary(
    data: pd.DataFrame,
    game_name: str = "当前游戏",
    extra_stopwords: set[str] | None = None,
) -> str:
    return build_operational_insights(
        data,
        game_name=game_name,
        extra_stopwords=extra_stopwords,
    ).to_text()


def dataframe_to_excel(data: pd.DataFrame, summary: str | None = None, analysis: bool = True) -> bytes:
    export_data = data.copy()
    if not analysis:
        export_data = export_data.drop(columns=ANALYSIS_COLUMNS, errors="ignore")
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export_data.to_excel(writer, index=False, sheet_name="分析结果" if analysis else "清洗数据")
        if analysis:
            platform_overview(data).to_excel(writer, index=False, sheet_name="平台汇总")
            sentiment_distribution(data).to_excel(writer, index=False, sheet_name="情绪分布")
            if summary:
                pd.DataFrame({"运营摘要": summary.splitlines()}).to_excel(
                    writer, index=False, sheet_name="运营摘要"
                )
    return buffer.getvalue()


def _review_card_html(row: pd.Series) -> str:
    label = html_text(row.get("sentiment_label"), "中评")
    card_class = "negative" if label == "差评" else ""
    url = safe_text(row.get("url"))
    link = (
        f'<a class="report-link" href="{html_text(url)}" target="_blank">查看原文</a>'
        if url.startswith("http")
        else ""
    )
    return f"""
    <article class="report-card {card_class}">
      <div class="card-head"><strong>{html_text(row.get("author"), "匿名玩家")}</strong><time>{date_label(row.get("date"))}</time></div>
      <div class="card-platform">{html_text(row.get("platform"), "未知平台")}</div>
      <h4>{html_text(row.get("title"), "玩家反馈")}</h4>
      <p>{html_text(truncate_text(row.get("content"), 180))}</p>
      <div class="card-foot">
        <span class="sentiment {label}">{label}</span>
        <span>{html_text(row.get("issue_category"), "未归类/待复核")}</span>
        <span>赞 {format_number(row.get("likes"))} · 评 {format_number(row.get("comments"))} · 转 {format_number(row.get("shares"))}</span>
        {link}
      </div>
      <div class="suggestion">{html_text(row.get("operation_suggestion"))}</div>
    </article>
    """


def generate_html_report(
    data: pd.DataFrame,
    summary: str | None = None,
    report_title: str = "游戏舆情报告",
    game_name: str = "当前游戏",
) -> str:
    summary = summary or generate_operational_summary(data, game_name=game_name)
    distribution = sentiment_distribution(data)
    date_data = data["date"].dropna()
    date_range = (
        f"{date_data.min():%Y.%m.%d} - {date_data.max():%Y.%m.%d}"
        if not date_data.empty
        else "日期未提供"
    )
    bars = []
    for _, row in distribution.iterrows():
        color = SENTIMENT_COLORS[row["sentiment_label"]]
        bars.append(
            f'<div class="bar-row"><label>{row["sentiment_label"]}</label>'
            f'<div class="track"><div class="fill" style="width:{row["percentage"]:.1f}%;background:{color}"></div></div>'
            f'<b>{row["count"]} / {row["percentage"]:.1f}%</b></div>'
        )
    platform_sections = []
    for platform in ordered_values(data["platform"].tolist(), PLATFORM_ORDER):
        cards = "".join(
            _review_card_html(row)
            for _, row in data.loc[data["platform"] == platform]
            .sort_values(["interaction_count", "date"], ascending=[False, False])
            .head(6)
            .iterrows()
        )
        platform_sections.append(f'<section><h2>{html_text(platform)} 内容</h2><div class="cards">{cards}</div></section>')
    escaped_summary = "<br>".join(html_text(summary).splitlines())
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_text(report_title)} - {date_range}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background:#0d0d14; color:#e8ebf5; font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif; }}
.page {{ max-width:1240px; margin:auto; padding:42px 38px 60px; }}
h1 {{ font-size:32px; margin:0 0 6px; border-left:4px solid #8173ff; padding-left:18px; }}
.subtitle {{ color:#9299ae; margin:0 0 28px 22px; }}
.panel {{ background:#191b25; border:1px solid #292d3b; border-radius:14px; padding:22px; margin-bottom:30px; }}
.panel h3 {{ margin:0 0 18px; font-size:17px; }}
.bar-row {{ display:flex; gap:14px; align-items:center; margin:15px 0; }}
.bar-row label {{ width:48px; color:#aeb4c5; }}
.bar-row b {{ width:110px; color:#9ba3b7; font-size:13px; font-weight:500; }}
.track {{ flex:1; height:10px; border-radius:9px; background:#292d38; overflow:hidden; }}
.fill {{ height:100%; border-radius:9px; }}
h2 {{ font-size:19px; color:#8173ff; margin:30px 0 14px; }}
.cards {{ display:grid; grid-template-columns:repeat(2,minmax(300px,1fr)); gap:16px; }}
.report-card {{ background:#191b25; border:1px solid #2b3040; border-radius:12px; padding:18px; min-height:195px; }}
.report-card.negative {{ border-color:#5b372f; }}
.card-head {{ display:flex; justify-content:space-between; margin-bottom:10px; }}
.card-head time {{ color:#8990a5; font-size:13px; }}
.card-platform {{ display:inline-block; font-size:12px; background:#292546; color:#9c90ff; padding:4px 9px; border-radius:4px; }}
h4 {{ margin:11px 0 8px; font-size:15px; }}
p {{ color:#d0d4df; line-height:1.65; margin:0 0 14px; }}
.card-foot {{ display:flex; gap:9px; align-items:center; flex-wrap:wrap; font-size:12px; color:#9da5b8; }}
.sentiment {{ border-radius:5px; padding:4px 8px; }}
.sentiment.好评 {{ background:#193c35; color:#63d5b2; }}
.sentiment.中评 {{ background:#2c303b; color:#bcc3d2; }}
.sentiment.差评 {{ background:#422923; color:#f28368; }}
.suggestion {{ margin-top:12px; padding:8px 10px; background:#121520; border-radius:6px; color:#b7bfda; font-size:12px; }}
.summary {{ line-height:1.8; color:#d3d7e3; }}
.report-link {{ color:#8c82ff; text-decoration:none; }}
@media (max-width:760px) {{ .cards {{ grid-template-columns:1fr; }} .page {{ padding:24px 14px; }} }}
</style></head>
<body><main class="page">
<h1>{html_text(report_title)} <small>({date_range})</small></h1>
<p class="subtitle">{html_text(game_name)} 舆情监控系统 · 基于多平台玩家评论的情绪分析与运营洞察</p>
<div class="panel"><h3>评论情感分布</h3>{''.join(bars)}<p class="subtitle">基于上传数据汇总判断</p></div>
{''.join(platform_sections)}
<section class="panel summary"><h3>运营日报式摘要</h3>{escaped_summary}</section>
</main></body></html>"""


def save_html_report(data: pd.DataFrame, output_path: Path) -> Path:
    output_path.write_text(generate_html_report(data), encoding="utf-8")
    return output_path
