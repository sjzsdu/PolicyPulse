"""源注册与分发。"""

from __future__ import annotations

from policypulse.config import SourceSpec
from policypulse.sources.base import SourceAdapter
from policypulse.sources.gov_json import GovJsonFeedAdapter
from policypulse.sources.html_list import HtmlListAdapter
from policypulse.sources.local_inbound import LocalInboundAdapter

_ADAPTERS: dict[str, SourceAdapter] = {
    "gov_json_feed": GovJsonFeedAdapter(),
    "html_list": HtmlListAdapter(),
    # 不发网络请求：读本地 inbound 目录
    "local_inbound": LocalInboundAdapter(),
}


def get_adapter(spec: SourceSpec) -> SourceAdapter:
    if spec.adapter not in _ADAPTERS:
        raise KeyError(f"unknown adapter {spec.adapter!r} for source {spec.id}")
    return _ADAPTERS[spec.adapter]
