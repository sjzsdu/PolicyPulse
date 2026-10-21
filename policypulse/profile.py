"""公司画像：从通达信 F10 取主营业务 / 经营范围 / 营收构成。

这是「公司级相关性闸门」与「个股深度研读」共用的公司数据层。F10 内容来自上市公司
公开披露（年报 / 公告），因此可以作为溯源链上的「公开公司来源」；取不到就明确返回
无画像，绝不拿空串或别的公司的数据顶替。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

OVERVIEW_BLOCK = "公司概况"
ANALYSIS_BLOCK = "经营分析"

# F10 公司概况表格里我们要读的字段；只取这些，避免把「公司简介」长文串进主营业务
TABLE_FIELDS = ("公司名称", "证券简称", "通达信研究行业", "证监会行业", "主营业务", "经营范围")

_SEGMENT_RE = re.compile(r"^(\S+?)\((行业|产品|地区|销售模式)\)")


@dataclass
class CompanyProfile:
    code: str
    name: str = ""
    industry: str = ""          # 通达信研究行业，如「商贸-一般零售」
    csrc_industry: str = ""     # 证监会行业
    main_business: str = ""     # 主营业务（一句话，强证据）
    scope: str = ""             # 经营范围（弱证据：普遍含「技术培训」类样板词）
    segments: list[str] = field(default_factory=list)  # 主营构成的行业/产品名，强证据
    fetched: bool = False       # 是否真的取到了 F10 文本
    source: str = "通达信F10"

    @property
    def has_profile(self) -> bool:
        """有画像 = 至少拿到了一段能说明业务的文本。"""
        return bool(self.fetched and (self.main_business or self.industry or self.segments or self.scope))

    def strong_text(self) -> str:
        """强证据字段：主营 + 营收构成 + 研究行业。"""
        parts = [self.main_business, " ".join(self.segments), self.industry, self.csrc_industry]
        return " | ".join(p for p in parts if p)

    def weak_text(self) -> str:
        """弱证据字段：经营范围，样板词多，只允许长关键词命中。"""
        return self.scope


def parse_overview(text: str) -> dict[str, str]:
    """解析 F10 公司概况的 │标签│值│ 表格。

    行结构约定：首格非空 = 新的一行标签组，首格为空 = 上一格值的续行。
    这样「主营业务」不会被后面的「公司简介」或关联方表里的同名列串掉。
    """
    out: dict[str, str] = {}
    pending: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not (line.startswith("│") and line.endswith("│")):
            continue
        cells = [c.strip() for c in line[1:-1].split("│")]
        if cells and cells[0]:
            pending = None
            i = 0
            while i + 1 < len(cells):
                if cells[i] in TABLE_FIELDS:
                    pending = cells[i]
                    out[pending] = cells[i + 1].strip()
                    i += 2
                else:
                    i += 1
        elif pending:
            out[pending] += "".join(cells[1:])
    return {k: v for k, v in out.items() if v and v != "---"}


def parse_segments(text: str) -> list[str]:
    """经营分析里的主营构成：只取「行业」「产品」两类项目名，去重保序。"""
    seen: list[str] = []
    for line in text.splitlines():
        m = _SEGMENT_RE.match(line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        if m.group(2) in ("行业", "产品") and name and name not in seen:
            seen.append(name)
    return seen


def load_profile(client, code: str) -> CompanyProfile:
    """取一只个股的公司画像；任何一步失败都降级为无画像，不抛异常。"""
    overview = client.f10_block(code, OVERVIEW_BLOCK) if hasattr(client, "f10_block") else ""
    analysis = client.f10_block(code, ANALYSIS_BLOCK) if hasattr(client, "f10_block") else ""
    fields = parse_overview(overview) if overview else {}
    return CompanyProfile(
        code=code,
        name=fields.get("公司名称") or fields.get("证券简称", ""),
        industry=fields.get("通达信研究行业", ""),
        csrc_industry=fields.get("证监会行业", ""),
        main_business=fields.get("主营业务", ""),
        scope=fields.get("经营范围", ""),
        segments=parse_segments(analysis) if analysis else [],
        fetched=bool(overview or analysis),
    )
