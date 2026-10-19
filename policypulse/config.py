"""路径、默认配置与 sources.yaml 加载."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_FILE = REPO_ROOT / "sources.yaml"
DEFAULT_THEME_ALIAS = REPO_ROOT / "data" / "theme_alias.csv"

DISCLAIMER = (
    "本报告由程序自动抓取公开会议与政策信息、由模型生成，仅用于个人研究，"
    "不构成任何投资建议；个股仅为政策主题映射结果，不代表公司基本面判断或收益承诺。"
)


@dataclass
class Paths:
    """运行期所有可写路径。可用环境变量 POLICYPULSE_HOME 整体迁移。"""

    home: Path

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def db(self) -> Path:
        return self.data / "policypulse.db"

    @property
    def snapshots(self) -> Path:
        return self.data / "snapshots"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def reports(self) -> Path:
        return self.home / "reports"

    def ensure(self) -> "Paths":
        for p in (self.data, self.snapshots, self.cache, self.reports):
            p.mkdir(parents=True, exist_ok=True)
        return self


def default_paths() -> Paths:
    home = os.environ.get("POLICYPULSE_HOME")
    return Paths(home=Path(home).expanduser() if home else REPO_ROOT).ensure()


@dataclass
class SourceSpec:
    id: str
    name: str
    org: str
    tier: str  # 中央 / 部委 / 省级 / 转载
    adapter: str
    url: str
    column: str = ""
    meeting_type_hint: str = ""
    rate_limit_seconds: float = 2.0
    enabled: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class Settings:
    # 默认看 14 天：部委栏目推送节奏慢于日更，窗口太窄会经常空转
    lookback_days: int = 14
    max_docs_per_source: int = 12
    min_confidence: float = 0.45
    min_strength_for_report: int = 3
    request_timeout: float = 25.0
    request_retries: int = 2
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PolicyPulse/0.1 (personal research)"
    )
    # 打分权重（外置可调）
    weights: dict = field(
        default_factory=lambda: {
            "strength": 0.35,
            "relevance": 0.25,
            "liquidity": 0.20,
            "breadth": 0.10,
            "novelty": 0.10,
        }
    )
    # 过滤
    min_market_cap_yi: float = 30.0
    exclude_bj: bool = True
    top_n_per_sector: int = 5
    max_tickers_per_signal: int = 10
    # tongstock
    tongstock_bin: str = "tongstock"
    tongstock_cache_ttl_hours: int = 24

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


def load_settings(path: Path | None = None) -> Settings:
    p = path or (REPO_ROOT / "policypulse.yaml")
    if p.exists():
        return Settings.from_file(p)
    return Settings()


VALID_TIERS = {"中央", "部委", "省级", "转载"}


def load_sources(path: Path = DEFAULT_SOURCES_FILE) -> list[SourceSpec]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[SourceSpec] = []
    for item in raw.get("sources", []):
        spec = SourceSpec(
            id=item["id"],
            name=item["name"],
            org=item["org"],
            tier=item["tier"],
            adapter=item["adapter"],
            url=item["url"],
            column=item.get("column", ""),
            meeting_type_hint=item.get("meeting_type_hint", ""),
            rate_limit_seconds=float(item.get("rate_limit_seconds", 2.0)),
            enabled=bool(item.get("enabled", True)),
            extra={k: v for k, v in item.items()
                   if k not in SourceSpec.__dataclass_fields__},
        )
        if spec.tier not in VALID_TIERS:
            raise ValueError(f"source {spec.id}: unknown tier {spec.tier!r}")
        out.append(spec)
    return out


def load_theme_alias(path: Path = DEFAULT_THEME_ALIAS) -> dict[str, list[str]]:
    """返回 canonical_theme -> [别名...]。CSV 列: theme,aliases(以 | 分隔)。"""
    import csv

    mapping: dict[str, list[str]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#"):
                continue
            theme = row[0].strip()
            aliases = [a.strip() for a in (row[1].split("|") if len(row) > 1 else []) if a.strip()]
            mapping.setdefault(theme, []).extend(aliases)
    for theme, aliases in mapping.items():
        mapping[theme] = sorted(set(aliases + [theme]))
    return mapping
