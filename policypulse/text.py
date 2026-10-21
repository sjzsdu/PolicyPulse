"""HTML -> 正文纯文本。

不依赖站点的正文容器 class，按“保留块级标签换行 + 密度打分”取主区域，
政府站点模板差异大，密度法比硬编码选择器更稳。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from bs4 import BeautifulSoup

BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "td", "table", "section"}
DROP_TAGS = {"script", "style", "noscript", "iframe", "svg", "form"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D102
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):  # noqa: D102
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):  # noqa: D102
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(html: str) -> str:
    """带结构信息的全文文本（用于指纹与句子切分）。"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(DROP_TAGS)):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


_NOISE_RE = re.compile(
    r"(header|nav|footer|breadcrumb|crumb|share|logo|search|siteurl|pagination|"
    r"pageinfo|related|xg-list|mhide|pchide)")
# 政府站点常见的正文容器命名，命中就直接采用，比密度法更稳
_CONTENT_RE = re.compile(
    r"(ucap-content|pages_content|trs_ueditor|trs_editor_view|zoom|xl_content|"
    r"article|content|zcwj|zwnr|detail|text_box)")


def _flatten(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def extract_main_text(html: str) -> str:
    """正文主体：文本密度最高的块级容器；失败时退回全文。

    两步：先排除掉"文本几乎全部由某个子容器贡献"的外壳节点（否则会选到整页
    包裹层或页头），再在剩下的候选里按 (正文长度 - 2×链接文本) 取最大。
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(DROP_TAGS)):
        tag.decompose()
    for tag in soup.find_all(["header", "nav", "footer"]):
        tag.decompose()
    # gov.cn 一类页面会被 lxml 解析出多个 <html>，正文容器可能落在 soup.body 之外，
    # 因此从文档根开始找，并按 class/id 剔除页头页脚等非正文区块。
    noisy = []
    for tag in soup.find_all(True):
        if not (tag.get("class") or tag.get("id")):
            continue
        mark = f"{_flatten(tag.get('class'))} {tag.get('id') or ''}".lower()
        if _NOISE_RE.search(mark):
            noisy.append(tag)
    for tag in noisy:
        tag.decompose()

    # 先认正文容器（政府站点命名有迹可循），再退化到密度法
    hinted = [
        tag for tag in soup.find_all(["div", "td", "article", "section", "span"])
        if _CONTENT_RE.search(f"{_flatten(tag.get('class'))} {tag.get('id') or ''}".lower())
        and len(tag.get_text(" ", strip=True)) >= 150
    ]
    if hinted:
        best_node = max(hinted, key=lambda t: len(t.get_text(" ", strip=True)))
        return _clean(best_node.get_text("\n", strip=True))

    root = soup
    MIN_LEN = 150
    candidates: list[tuple[object, int]] = []
    for node in root.find_all(["div", "td", "article", "section", "span"]):
        text = node.get_text(" ", strip=True)
        if len(text) < MIN_LEN:
            continue
        link_text = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
        candidates.append((node, len(text) - 2 * link_text))

    inner: list[tuple[object, int]] = []
    for node, score in candidates:
        own = len(node.get_text(" ", strip=True))
        max_child = 0
        for child in node.find_all(["div", "td", "article", "section"]):
            max_child = max(max_child, len(child.get_text(" ", strip=True)))
        if max_child >= 0.9 * own:
            continue  # 文本基本来自子容器：这是个外壳
        inner.append((node, score))

    pool = inner or candidates
    raw = pool[0][0].get_text("\n", strip=True) if pool else root.get_text("\n", strip=True)
    return _clean(raw)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


_DATE_PAT = r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})"


def extract_publish_time(html: str, text: str = "") -> str:
    """从页面里找发布时间，找不到返回空串（调用方用 fetched_at 兜底）。

    html / text 两条路径必须返回同一种零填充 `YYYY-MM-DD`：`_in_window` 是裸字符串
    比较，`2026年10月19`、`2026/10/19` 的分隔符码位大于 `-`，不归一会把窗口内稿件
    判成窗口外（误杀还会被计入 out_of_window_docs，与真剔除混在一起）。
    越界日期同样不返回原始串，交给调用方按「无日期」处理。
    """
    for hay in (html[:200000], text or ""):
        m = re.search(_DATE_PAT, hay)
        if not m:
            continue
        y, mo, d = (int(x) for x in m.groups())
        if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def split_sentences(text: str) -> list[str]:
    """按中文句读切句，保留原句（抽取的 quote 必须是原文子串）。"""
    chunks = re.split(r"(?<=[。！？；\n])", text)
    out: list[str] = []
    for c in chunks:
        c = c.strip()
        if len(c) >= 8:
            out.append(c)
    return out
