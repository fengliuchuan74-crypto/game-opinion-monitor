"""Version-period controls; reading charts never triggers a network request."""
from pathlib import Path

import pandas as pd
import streamlit as st

from modules.version_history import get_version_history


def version_history_controls(profile, cache_dir):
    with st.expander('版本归属与发布时间'):
        st.caption('优先使用评论自带版本。没有版本时，根据当前地区的商店版本发布时间归类，标记为“按日期推定”；玩家可能仍在使用旧版本。')
        history=get_version_history(profile['app_id'],profile['country'],Path(cache_dir))
        if st.button('同步商店版本记录',key=f"versions_sync_{profile['app_id']}_{profile['country']}"):
            with st.spinner('读取 App Store 版本更新记录…'):
                history=get_version_history(profile['app_id'],profile['country'],Path(cache_dir),allow_fetch=True,force=True)
            if history.get('error'):
                st.warning('同步未完整成功：'+str(history['error']))
            else:
                st.success('版本记录已同步，评论归属会按新记录重新计算。')
        records=history.get('releases') or []
        if not records:
            st.info('尚无可用版本时间线，点击上方按钮同步。已有的评论原始版本仍可显示。')
            return
        checked=pd.to_datetime(history.get('checked_at'),utc=True,errors='coerce')
        checked_label=checked.tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M') if pd.notna(checked) else '未记录'
        st.markdown(f"**商店当前版本：{history.get('current_version') or '未确认'}** · 已读取 {len(records)} 条版本记录")
        st.caption(f'上次成功读取：{checked_label}（北京时间）。日期推定仅覆盖已读取时间线；更新日时间不明确、日期缺失或超出范围时保留未知。')
        table=pd.DataFrame([{'商店版本':row['version'],
            '发布时间（北京时间）':pd.Timestamp(row['released_at']).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S')
                if row.get('precision')!='day' else str(row['released_at'])[:10]+'（仅日期）',
            '时间精度':'秒' if row.get('precision')=='timestamp' else '日期'} for row in records])
        st.dataframe(table,hide_index=True,width='stretch',height=280)
        st.caption('这里使用 App Store 安装包版本。游戏内热更新、活动版本可能有独立时间线，不会自动混入。浏览和规则初筛使用本地记录，无需联网。')
