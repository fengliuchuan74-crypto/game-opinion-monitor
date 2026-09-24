from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class RawReview:
    platform: str
    external_id: str | None
    date: str | None
    author: str
    title: str
    content: str
    rating: float | None = None
    likes: int = 0
    comments: int = 0
    shares: int = 0
    url: str | None = None
    note_type: str = "评论"
    topic: str = ""
    data_source: str = "public_feed"
    collected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    app_id: str | None = None
    country: str | None = None
    raw_json: dict[str, Any] | None = None


@dataclass(slots=True)
class CollectionResult:
    platform: str
    stop_reason: str = ""
    requested: int = 0
    reviews: list[RawReview] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def fetched_count(self) -> int:
        return len(self.reviews)

    @property
    def failed_count(self) -> int:
        return len(self.errors)


class BaseCollector(ABC):
    platform: str

    @abstractmethod
    def collect(self, **kwargs: Any) -> CollectionResult:
        """Collect public reviews and return normalized raw review records."""


class PlaceholderCollector(BaseCollector):
    platform = "未知平台"

    def collect(self, **kwargs: Any) -> CollectionResult:
        return CollectionResult(
            platform=self.platform,
            errors=[
                f"{self.platform} 暂未接入官方/授权采集接口。当前版本只保留扩展位。"
            ],
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
