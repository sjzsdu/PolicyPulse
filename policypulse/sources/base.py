"""信息源适配层：每个源只声明入口与定位规则，由适配器做链接发现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from policypulse.config import SourceSpec


@dataclass
class DiscoveredItem:
    """入口页发现的一条候选（尚未抓正文）。"""

    url: str
    title: str
    published_at: str = ""  # YYYY-MM-DD，可为空


class SourceAdapter(Protocol):
    def discover(self, spec: SourceSpec, fetcher) -> list[DiscoveredItem]:
        ...
