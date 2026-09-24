from __future__ import annotations

import pandas as pd


def apply_filters(
    data: pd.DataFrame,
    selected_platforms: list[str],
    selected_sentiments: list[str],
    selected_issues: list[str],
    selected_range: tuple[object, object] | None,
) -> pd.DataFrame:
    filtered = data.loc[
        data["platform"].isin(selected_platforms)
        & data["sentiment_label"].isin(selected_sentiments)
        & data["issue_category"].isin(selected_issues)
    ].copy()
    if selected_range is not None:
        start_date = pd.Timestamp(selected_range[0]).date()
        end_date = pd.Timestamp(selected_range[1]).date()
        date_values = pd.to_datetime(
            filtered["date"], errors="coerce", utc=True
        ).dt.tz_convert(None).dt.date
        in_range = (date_values >= start_date) & (date_values <= end_date)
        filtered = filtered.loc[in_range]
    return filtered
