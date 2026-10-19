"""通用 HTML 列表页源：入口页 + 链接发现 + 正文页正则过滤。

不硬编码深链：只声明入口 URL、链接必须匹配的正则、可选的排除正则，
栏目改版时改 sources.yaml 即可。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from policypulse.config import SourceSpec
from policypulse.http import absolutize
from policypulse.sources.base import DiscoveredItem


class HtmlListAdapter:
    """adapter: html_list"""

    def discover(self, spec: SourceSpec, fetcher) -> list[DiscoveredItem]:
        html = fetcher.get_text(spec.url)
        soup = BeautifulSoup(html, "lxml")

        link_pat = re.compile(spec.extra["link_pattern"])
        reject_pat = re.compile(spec.extra["reject_pattern"]) if spec.extra.get("reject_pattern") else None
        # 转载源用：只有标题命中的才收（例如只收央行相关通稿）
        title_pat = re.compile(spec.extra["title_pattern"]) if spec.extra.get("title_pattern") else None
        min_len = int(spec.extra.get("min_title_length", 8))
        max_items = int(spec.extra.get("max_items", 40))

        items: list[DiscoveredItem] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            title = anchor.get_text(" ", strip=True)
            if len(title) < min_len:
                continue
            href = anchor["href"].strip()
            if not link_pat.search(href):
                continue
            if reject_pat and reject_pat.search(href):
                continue
            if title_pat and not title_pat.search(title):
                continue
            url = absolutize(href, spec.url)
            if url in seen:
                continue
            seen.add(url)
            items.append(DiscoveredItem(url=url, title=title, published_at=self._date_from_href(href)))
            if len(items) >= max_items:
                break
        return items

    @staticmethod
    def _date_from_href(href: str) -> str:
        m = re.search(r"/(20\d{2})(\d{2})/t(20\d{2})(\d{2})(\d{2})_", href)
        if m:
            return f"{m.group(3)}-{m.group(4)}-{m.group(5)}"
        m = re.search(r"/(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", href)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        # 只有年份不算发布日期，留给正文页解析
        return ""
