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

# 列表项里的发布日期：`[2026-10-23]` / `2026年10月23日` / `2026/10/23` 都归一成 YYYY-MM-DD。
# 商务部一类的 href 形如 `/art/2026/art_<uuid>.html`，只有年份没有月日，日期只在
# 列表项文本里——不从列表页取日期，候选就只能按页面顺序截断，最新的产业稿件反而被截掉。
_ITEM_DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
# 单项容器：日期必须落在「这一条」里，别把相邻条目的日期借过来
_ITEM_CONTAINERS = ("li", "tr", "dd", "dt", "p", "h3", "h4", "span")
# 容器内优先当作「日期节点」的内联标签（与 _ITEM_CONTAINERS 重叠的也算）
_ITEM_DATE_TAGS = ("span", "em", "i", "time", "font", "b")
# 容器文本上限：超过这个长度说明容器裹了一整段列表，不是单条，宁可判成无日期
_ITEM_TEXT_MAX = 300


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
            items.append(DiscoveredItem(url=url, title=title,
                                        published_at=self._date_of(anchor, href)))
            if len(items) >= max_items:
                break
        return items

    @classmethod
    def _date_of(cls, anchor, href: str) -> str:
        """发布日期：href 优先（可精确到日），列表项文本兜底。"""
        return cls._date_from_href(href) or cls._date_from_list_item(anchor)

    @classmethod
    def _date_from_list_item(cls, anchor) -> str:
        """从「这一条」列表项里取日期（`<a>…</a><span>[2026-10-23]</span>`）。

        只在最近一个单项容器内找，且容器文本有长度上限：裹了整段列表的外壳节点
        不当作单条，避免把相邻条目的日期借过来。容器内优先看专门的日期节点
        （span/em/i/time），标题里的「2026年1-9月」一类月份区间不算发布日期。
        不命中就返回空串，交给详情页解析。
        """
        node = anchor
        for _ in range(3):
            node = node.parent
            if node is None or not getattr(node, "name", None):
                return ""
            if node.name not in _ITEM_CONTAINERS:
                continue
            text = node.get_text(" ", strip=True)
            if len(text) > _ITEM_TEXT_MAX:
                return ""  # 不是单条容器
            for tag in node.find_all(_ITEM_DATE_TAGS):
                got = cls._first_date(tag.get_text(" ", strip=True))
                if got:
                    return got
            got = cls._first_date(text)
            if got:
                return got
        return ""

    @classmethod
    def _first_date(cls, text: str) -> str:
        """文本里第一个「真·发布日期」。

        `2026年1-9月` 这类月份区间会被日期正则误命中（`1-9` → 01-09），
        所以紧跟「月」的匹配一律跳过——真正的发布日期后面是「日」或分隔符。
        """
        for m in _ITEM_DATE_RE.finditer(text):
            tail = text[m.end():m.end() + 1]
            if tail == "月":
                continue
            got = cls._normalize(m)
            if got:
                return got
        return ""

    @staticmethod
    def _normalize(match) -> str:
        if not match:
            return ""
        y, mo, d = (int(x) for x in match.groups())
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return ""

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
