from __future__ import annotations

from .base import PlaceholderCollector


class TapTapCollector(PlaceholderCollector):
    platform = "TapTap"


class XHSCollector(PlaceholderCollector):
    platform = "小红书"


class BilibiliCollector(PlaceholderCollector):
    platform = "B站"


class WeiboCollector(PlaceholderCollector):
    platform = "微博"
