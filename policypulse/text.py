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


# 繁体→简体的定点字形映射：LLM 输出偶发夹繁体字（实测 WAN-15 的深读文本里
# 出现「中東」，是「中—东 / 中东」类表述的繁体滑字），入库/渲染前统一归一。
# 只做字形转换，不做语义改写；未收录的字原样保留。
_HANT_TO_HANS = str.maketrans({
    "東": "东", "國": "国", "貿": "贸", "會": "会", "經": "经",
    "聯": "联", "員": "员", "開": "开", "華": "华", "業": "业",
    "對": "对", "傳": "传", "統": "统", "資": "资", "場": "场",
    "產": "产", "財": "财", "際": "际", "實": "实", "現": "现",
    "發": "发", "擴": "扩", "應": "应", "樣": "样", "標": "标",
    "評": "评", "審": "审", "議": "议", "決": "决", "證": "证",
    "務": "务", "劃": "划", "導": "导", "進": "进", "動": "动",
    "總": "总", "結": "结", "構": "构", "設": "设", "級": "级",
    "權": "权", "勢": "势", "術": "术", "訂": "订", "單": "单",
    "額": "额", "項": "项", "準": "准", "據": "据", "點": "点",
})


def simplify_hant(text: str) -> str:
    """把 LLM 文本里偶发的繁体字归一为简体（如「中東」→「中东」）。"""
    return (text or "").translate(_HANT_TO_HANS)


_DATE_PAT = r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})"

# 稿件自家的发布时间元数据：比在整页里撞第一个日期可靠（页面上先出现的可能是
# 栏目模板里的其它日期）。商务部等 CMS 站点都会写 meta PubDate。
_PUBDATE_META_KEYS = ("pubdate", "publishdate", "publish_date", "articledate",
                      "article:published_time", "og:release_date", "releasetime")


def _normalize_date(pattern: str, hay: str) -> str:
    m = re.search(pattern, hay)
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def _meta_publish_time(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - 解析不了就退回正则路径
        return ""
    for meta in soup.find_all("meta"):
        key = (meta.get("name") or meta.get("property") or "").strip().lower()
        if key and key in _PUBDATE_META_KEYS:
            got = _normalize_date(_DATE_PAT, meta.get("content") or "")
            if got:
                return got
    return ""


def extract_publish_time(html: str, text: str = "") -> str:
    """从页面里找发布时间，找不到返回空串（调用方用 fetched_at 兜底）。

    顺序：meta 发布时间 → 页面 HTML 的第一个日期 → 正文的第一个日期。

    html / text 两条路径必须返回同一种零填充 `YYYY-MM-DD`：`_in_window` 是裸字符串
    比较，`2026年10月19`、`2026/10/19` 的分隔符码位大于 `-`，不归一会把窗口内稿件
    判成窗口外（误杀还会被计入 out_of_window_docs，与真剔除混在一起）。
    越界日期同样不返回原始串，交给调用方按「无日期」处理。
    """
    got = _meta_publish_time(html)
    if got:
        return got
    for hay in (html[:200000], text or ""):
        got = _normalize_date(_DATE_PAT, hay)
        if got:
            return got
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
