"""Public-data collectors for the multi-game opinion monitor."""

from .app_store import AppStoreCollector
from .base import BaseCollector, CollectionResult, RawReview
from .placeholders import BilibiliCollector, TapTapCollector, WeiboCollector, XHSCollector

__all__ = [
    "AppStoreCollector",
    "BaseCollector",
    "CollectionResult",
    "RawReview",
    "TapTapCollector",
    "XHSCollector",
    "BilibiliCollector",
    "WeiboCollector",
]
