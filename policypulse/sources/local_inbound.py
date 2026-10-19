"""本地 inbound 源：用户自己下载的原稿 → 走同一条解析/去重/入库链路。

为什么要有这条路（WAN-10）：`pbc.gov.cn` 的 robots.txt 是 `User-agent: * / Disallow: /`，
程序**不能**主动抓取。但用户本人用浏览器打开央行官网、另存页面，是用户自己的浏览
行为；把这类文件放进本地目录再入库，是唯一既能拿到央行**原文**、又不与站点意愿冲突
的获取法。本适配器**不发任何网络请求**。

目录约定（`inbound/<source_id>/`，可用 `inbound_dir` 覆盖）：

    inbound/pbc_inbound/
      2026-10-19-公开市场业务交易公告.html     # 正文（html / htm / md / txt）
      2026-10-19-公开市场业务交易公告.url      # 可选：一行，原文 URL（溯源用）

溯源 URL 的三个来源，按优先级：

1. 同名 `.url` 伴生文件（一行 URL）；
2. 正文前 5 行里的 `原文: URL` / `来源链接: URL` 之类标记（md/txt 也可以用
   HTML 注释 `<!-- source: URL -->`）；
3. HTML 里的 `<link rel="canonical">` 或 `<base href>`。

三者都没有 → **拒绝入库并告警**：没有 URL 就没有 `source_url`，结论无法回溯；
宁可不产结论，也不产出假溯源。

文件处理完**不删除、不移动**：跨 run 去重靠正文指纹，同一批文件重复跑不会重复抽取。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from policypulse.config import REPO_ROOT, SourceSpec
from policypulse.sources.base import DiscoveredItem
from policypulse.text import extract_publish_time, extract_title

SUFFIXES = {".html", ".htm", ".md", ".txt"}
SIDECAR_SUFFIX = ".url"
MARKER_RE = re.compile(
    r"(?:原文|来源链接|来源|URL|source)\s*[:：]\s*<?(https?://[^\s<>\"')]+)>?", re.IGNORECASE)
FILENAME_DATE_RE = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
CANONICAL_RE = re.compile(r"<link[^>]+rel=[\"']?canonical[\"']?[^>]+href=[\"']([^\"']+)[\"']", re.I)
CANONICAL_REV_RE = re.compile(r"<link[^>]+href=[\"']([^\"']+)[\"'][^>]+rel=[\"']?canonical[\"']?", re.I)
BASE_RE = re.compile(r"<base[^>]+href=[\"']([^\"']+)[\"']", re.I)
# 文件名去掉日期前缀后剩下的部分当标题（用户手工下载时通常就是这个）
LEADING_DATE_RE = re.compile(r"^\s*20\d{2}[-_.]?\d{2}[-_.]?\d{2}\s*[-_ ]?\s*")


class LocalInboundAdapter:
    """adapter: local_inbound —— 只读本地文件，零网络请求。"""

    def directory(self, spec: SourceSpec) -> Path:
        home = Path(os.environ.get("POLICYPULSE_HOME") or REPO_ROOT).expanduser()
        raw = spec.extra.get("inbound_dir")
        if raw:
            custom = Path(str(raw)).expanduser()
            return custom if custom.is_absolute() else home / custom
        return home / "inbound" / spec.id

    # ---------- 发现 ----------
    def discover(self, spec: SourceSpec, fetcher) -> list[DiscoveredItem]:
        directory = self.directory(spec)
        files = self._candidate_files(directory)
        items: list[DiscoveredItem] = []
        for path in files:
            url = self._source_url(path)
            if not url:
                continue  # 无溯源 URL：不入库（告警在 run 的 warnings 里）
            items.append(DiscoveredItem(
                url=url,
                title=self._title(path),
                published_at=self._published_at(path),
                snapshot_ext=path.suffix.lower() or ".html",
            ))
        return items

    # ---------- 取正文 ----------
    def load(self, spec: SourceSpec, fetcher, item: DiscoveredItem) -> str:
        for path in self._candidate_files(self.directory(spec)):
            if self._source_url(path) == item.url:
                return path.read_text(encoding="utf-8", errors="replace")
        return ""

    def missing_source_url(self, spec: SourceSpec) -> list[str]:
        """没有溯源 URL 的文件名：run 里要显式告警，不能静默跳过。"""
        return [p.name for p in self._candidate_files(self.directory(spec))
                if not self._source_url(p)]

    # ---------- 内部 ----------
    @staticmethod
    def _candidate_files(directory: Path) -> list[Path]:
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.iterdir()
                      if p.is_file() and p.suffix.lower() in SUFFIXES)

    @staticmethod
    def _source_url(path: Path) -> str:
        sidecar = path.with_suffix(SIDECAR_SUFFIX)
        if sidecar.is_file():
            url = sidecar.read_text(encoding="utf-8", errors="replace").strip().split()[0:1]
            if url:
                return url[0]
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines()[:5]:
            m = MARKER_RE.search(line)
            if m:
                return m.group(1)
        for pattern in (CANONICAL_RE, CANONICAL_REV_RE, BASE_RE):
            m = pattern.search(text[:200000])
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _title(path: Path) -> str:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in (".html", ".htm"):
            title = extract_title(text)
            if title:
                return title
        for line in text.splitlines():
            line = line.strip().lstrip("#").strip()
            if len(line) >= 6:
                return line[:120]
        return LEADING_DATE_RE.sub("", path.stem).strip() or path.stem

    @staticmethod
    def _published_at(path: Path) -> str:
        """文件名日期 → 正文/页面里的日期 → 空（宁缺勿错，不拿 mtime 冒充发布日期）。"""
        m = FILENAME_DATE_RE.search(path.name)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if path.suffix.lower() in (".html", ".htm"):
            text = path.read_text(encoding="utf-8", errors="replace")
            return extract_publish_time(text, text)
        return ""
