"""中国政府网 JSON 列表源。

gov.cn 的栏目页是 JS 渲染，但渲染所用的数据是一个同目录静态 JSON
（如 /zhengce/zuixin/ZUIXINZHENGCE.json、/xinwen/yaowen/TONGYONGGAILAN.json），
直接取这个 JSON 比解析渲染后的 DOM 稳定得多，字段也自带发布时间。
"""

from __future__ import annotations

from policypulse.config import SourceSpec
from policypulse.sources.base import DiscoveredItem

DATE_KEYS = ("DOCRELPUBTIME", "PUBTIME", "pubtime", "TIME")


class GovJsonFeedAdapter:
    """adapter: gov_json_feed"""

    def discover(self, spec: SourceSpec, fetcher) -> list[DiscoveredItem]:
        url = spec.extra.get("feed_url") or spec.url
        payload = fetcher.get_json(url)
        if isinstance(payload, dict):
            for key in ("data", "list", "results", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            return []

        items: list[DiscoveredItem] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            link = row.get("URL") or row.get("url") or ""
            title = (row.get("TITLE") or row.get("title") or "").strip()
            if not link or not title:
                continue
            published = ""
            for key in DATE_KEYS:
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    published = value.strip()[:10].replace("/", "-")
                    break
            items.append(DiscoveredItem(url=link, title=title, published_at=published))
        return items
