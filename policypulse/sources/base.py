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
    # 原文快照的落盘后缀：本地 inbound 源放的是 md/txt，不该硬写成 .html
    snapshot_ext: str = ".html"


class SourceAdapter(Protocol):
    def discover(self, spec: SourceSpec, fetcher) -> list[DiscoveredItem]:
        ...
