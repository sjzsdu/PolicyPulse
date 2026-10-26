"""路径、默认配置与 sources.yaml 加载."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_FILE = REPO_ROOT / "sources.yaml"
DEFAULT_THEME_ALIAS = REPO_ROOT / "data" / "theme_alias.csv"
# 主题 -> 公司业务关键词：相关性闸门拿它去和 F10 主营/营收构成比对
DEFAULT_THEME_BUSINESS = REPO_ROOT / "data" / "theme_business.csv"

DISCLAIMER = (
    "本报告由程序自动抓取公开会议与政策信息、由模型生成，仅用于个人研究，"
    "不构成任何投资建议；个股仅为政策主题映射结果，不代表公司基本面判断或收益承诺。"
)

# 映射口径标注：概念板块成分由第三方维护，产物里必须显式说明
MAPPING_CAVEAT = (
    "概念板块成分由第三方（通达信）维护，宽口径，可能包含非核心主题个股；"
    "本表为政策主题映射结果，不代表个股基本面判断。"
)

# 当日增量口径下的“空日报”文案：没有新内容是常态，不是故障
EMPTY_DAY_NOTICE = "今日无新增政策 / 事件"

# 公司级相关性闸门口径说明，固定出现在日报概览下方
GATE_CAVEAT = (
    "个股先过「公司级相关性闸门」：用通达信 F10 的主营业务 / 营收构成 / 研究行业"
    "与政策主题的业务关键词求交集，无实质交集的个股相关性被压到最低并不再进入正文；"
    "闸门是过滤器，不是基本面判断。"
)

# 信号级可投资性闸门口径说明
INVESTABLE_CAVEAT = (
    "信号先过「信号级可投资性闸门」：LLM 抽取时对每条信号做受益主体明确性语义判定"
    "（有明确受益主体 / 有产业方向但无具体主体 / 无受益链条），判「无受益链条」的信号"
    "不映射板块、不展开个股；其余信号再过确定性词表校验（泛经济样板表述且不含具名"
    "产业词的同样拦下），被拦信号只在「非投资信号」区留一行审计记录。"
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
    # 日报默认只收当日增量：窗口 = [target-(daily_window_days-1), target]。
    # 历史回补不再走默认路径，必须显式 `policypulse run --since N`。
    daily_window_days: int = 2
    max_docs_per_source: int = 12
    min_confidence: float = 0.45
    min_strength_for_report: int = 3
    request_timeout: float = 25.0
    request_retries: int = 2
    # robots.txt 拿不到时的保守请求间隔（秒）
    robots_unavailable_interval: float = 5.0
    # 跨 run 去重：正文指纹已入库则不再重复抽取
    cross_run_dedupe: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PolicyPulse/0.1 (personal research)"
    )
    # 打分权重（外置可调）：公司级相关性主导，市值流动性只作并列决胜位
    weights: dict = field(
        default_factory=lambda: {
            "strength": 0.25,
            "relevance": 0.50,
            "liquidity": 0.10,
            "breadth": 0.05,
            "novelty": 0.10,
        }
    )
    # 公司级相关性闸门
    relevance_gate: bool = True
    # drop=闸门不通过直接不进正文；demote=保留入库但相关性压到最低（默认，便于审计）
    relevance_gate_mode: str = "demote"
    # 信号级可投资性闸门：泛经济样板表述不得映射板块、不得进受益表。
    # 关闭后所有信号按可投资处理（旧行为），但审计字段仍照常写入
    investable_gate: bool = True
    # 全局相关性截断：低于该值的个股不进日报（取代“每板块 Top-N”）
    min_relevance: float = 0.5
    # 算力上界：单条信号最多展开多少只候选再过闸门（只影响评估量，不参与最终排序）
    max_candidates_per_signal: int = 300
    # 公司画像（F10）缓存 TTL：主营业务/营收构成变化以季度计，7 天足够
    company_profile_ttl_hours: int = 168
    # 深度研读：对全局 Top-K 个股产出结构化小报告（需 LLM，未配置则跳过并写明原因）
    deepdive_top_k: int = 3
    # 过滤
    min_market_cap_yi: float = 30.0
    exclude_bj: bool = True
    # 选股硬上限（WAN-21）：一次选股最多产出几只个股。
    # 口径是「先排序、再截断」——按打分（score 越高 = 相对上涨概率越大）降序排，
    # 取前 max_picks 只；候选不足时按实际数量返回，不补位。信号级排序
    # （scoring.rank）与事件级个股汇总（clustering.cluster_events）共用这一上限。
    max_picks: int = 5
    # tongstock
    tongstock_bin: str = "tongstock"
    tongstock_cache_ttl_hours: int = 24

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # WAN-21 起 `max_tickers_per_signal` 更名为 `max_picks`（选股 5 只硬上限）。
        # 老配置里的旧键按上限语义迁移，且不允许把「先排序再取前 5」的硬约束放大回去。
        if "max_picks" not in raw and "max_tickers_per_signal" in raw:
            raw["max_picks"] = min(int(raw["max_tickers_per_signal"]), cls.max_picks)
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
    # utf-8-sig：theme_alias.csv 带 BOM，不剥掉会把首行注释变成一个假主题
    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#"):
                continue
            theme = row[0].strip()
            aliases = [a.strip() for a in (row[1].split("|") if len(row) > 1 else []) if a.strip()]
            mapping.setdefault(theme, []).extend(aliases)
    for theme, aliases in mapping.items():
        mapping[theme] = sorted(set(aliases + [theme]))
    return mapping


def load_theme_business(path: Path | None = None) -> dict[str, list[str]]:
    """返回 canonical_theme -> [公司业务关键词...]，供相关性闸门比对主营业务/营收构成。

    与 theme_alias 的区别：别名用于「政策主题 ↔ 板块名」，业务关键词用于
    「政策主题 ↔ 公司主营业务」，因此刻意避开「消费」「装备」这类一抓一大把的泛词。
    文件缺失时返回空表（闸门退化为只用别名，不阻塞运行）。
    """
    import csv

    p = path or DEFAULT_THEME_BUSINESS
    mapping: dict[str, list[str]] = {}
    if not p.exists():
        return mapping
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#"):
                continue
            theme = row[0].strip()
            words = [w.strip() for w in (row[1].split("|") if len(row) > 1 else []) if w.strip()]
            mapping.setdefault(theme, []).extend(words)
    return mapping
