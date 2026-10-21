"""tongstock CLI 封装：A 股基础信息、行业/概念板块与成分股。

所有数据来自本机 `tongstock`（底层通达信 TDX 数据），结果落本地缓存，
TTL 内不重复打 TDX。个股名称以 `codes list` 为准（`block show` 的名称列不可信）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from policypulse.config import Settings

CONCEPT_FILE = "block_gn.dat"
INDUSTRY_FILE = "block_fg.dat"

_ROW_RE = re.compile(r"^(\d+)\s+(\S+)\s+(\d+)\s+(.*)$")
_CODE_ROW_RE = re.compile(r"^(\d{6})\s+(.*)$")


class TongstockError(RuntimeError):
    pass


def resolve_bin(name: str) -> str:
    """定位 tongstock 可执行文件。

    cron 的 PATH 里通常没有 `~/.local/bin`，只靠 `shutil.which` 会在定时任务里
    静默变成「找不到 tongstock」；which 找不到时再兜底一次用户本地安装路径。
    """
    found = shutil.which(name)
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / name
    if candidate.exists():
        return str(candidate)
    return name


@dataclass
class Ticker:
    code: str
    name: str
    exchange: str
    board: str = ""
    market_cap_yi: float = 0.0

    @property
    def is_st(self) -> bool:
        return "ST" in self.name.upper() or "退" in self.name


class TongstockClient:
    def __init__(self, settings: Settings | None = None, cache_dir: Path | None = None,
                 refresh: bool = False) -> None:
        self.settings = settings or Settings()
        self.refresh = refresh
        self.cache_dir = Path(cache_dir) if cache_dir else (Path(__file__).resolve().parents[1] / "data" / "cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        # 延迟到真正执行时再解析，且带 ~/.local/bin 兜底（cron PATH 坑）
        self._bin_override = self.settings.tongstock_bin
        self._resolved_bin: str | None = None
        self._memory: dict[str, object] = {}

    # ---------- 命令执行 ----------
    @property
    def bin(self) -> str:
        if self._resolved_bin is None:
            self._resolved_bin = resolve_bin(self._bin_override)
        return self._resolved_bin

    def run(self, args: list[str], timeout: float = 180.0) -> str:
        self.calls += 1
        try:
            proc = subprocess.run(
                [self.bin, *args],
                capture_output=True, text=True, timeout=timeout,
                env=None,
            )
        except FileNotFoundError as exc:
            raise TongstockError(f"{self.bin} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise TongstockError(f"{' '.join(args)} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            raise TongstockError(f"{' '.join(args)} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def cached(self, key: str, producer, ttl_hours: int | None = None):
        """带磁盘缓存的惰性数据源；同一次 run 内同 key 只算一次。"""
        if not self.refresh and key in self._memory:
            return self._memory[key]
        data = self._cached(key, producer, ttl_hours)
        self._memory[key] = data
        return data

    def _cached(self, key: str, producer, ttl_hours: int | None = None):
        ttl = ttl_hours if ttl_hours is not None else self.settings.tongstock_cache_ttl_hours
        path = self._cache_path(key)
        if not self.refresh and path.exists():
            age_h = (time.time() - path.stat().st_mtime) / 3600
            if age_h <= ttl:
                return json.loads(path.read_text(encoding="utf-8"))
        data = producer()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    # ---------- F10 公司画像 ----------
    def f10_block(self, code: str, block: str) -> str:
        """个股 F10 某个信息块的原始文本（公司概况 / 经营分析 …）。

        这是公司主营业务与营收构成的唯一取数口，缓存 TTL 单独配置（默认 7 天）。
        取不到时返回空串——调用方按「无画像」处理，不能拿空串当「无交集」。
        """
        key = f"f10_{code}_{re.sub(r'[^0-9A-Za-z一-龥]', '_', block)}"
        ttl = self.settings.company_profile_ttl_hours

        def _load():
            try:
                return self.run(["company-content", code, "--block", block], timeout=60.0)
            except TongstockError:
                return ""

        return self.cached(key, _load, ttl_hours=ttl) or ""

    # ---------- 基础数据 ----------
    def tickers(self) -> dict[str, Ticker]:
        """代码 -> Ticker。市值来自 stockinfo，名称以 stockinfo 为准。"""
        raw = self._cached("tickers", self._load_tickers)
        return {code: Ticker(**item) for code, item in raw.items()}

    def _load_tickers(self) -> dict:
        out: dict[str, dict] = {}
        for exchange in ("sh", "sz"):
            payload = json.loads(self.run(["stockinfo", "--exchange", exchange, "--json"]))
            for item in payload.get("infos", []):
                code = str(item.get("code", "")).zfill(6)
                if not code or not re.fullmatch(r"\d{6}", code):
                    continue
                if not self._is_a_share(code, exchange):
                    continue
                out[code] = {
                    "code": code,
                    "name": str(item.get("name", "")).strip(),
                    "exchange": exchange,
                    "board": board_of(code, exchange),
                    "market_cap_yi": round(float(item.get("marketCap") or 0.0), 4),
                }
        names = self.code_names()
        for code, tk in out.items():
            if not tk["name"] and code in names:
                tk["name"] = names[code]
        return out

    @staticmethod
    def _is_a_share(code: str, exchange: str) -> bool:
        if exchange == "sh":
            return code.startswith(("60", "68"))
        return code.startswith(("00", "30"))

    def code_names(self) -> dict[str, str]:
        """`codes list` 的名称表，用于订正 stockinfo 缺失/错误的名称。"""
        return self._cached("code_names", self._load_code_names)

    def _load_code_names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for exchange in ("sh", "sz"):
            text = self.run(["codes", "list", "-c", "stock", "-e", exchange])
            for line in text.splitlines():
                m = _CODE_ROW_RE.match(line.strip())
                if not m:
                    continue
                code, rest = m.group(1), m.group(2)
                name = rest.split("[")[0].strip()
                name = re.sub(r"\s*(上交所|深交所|北交所)$", "", name).strip()
                if code.startswith(("60", "68", "00", "30")) and name:
                    names.setdefault(code, name)
        return names

    # ---------- 板块 ----------
    def sectors(self, block_file: str = CONCEPT_FILE) -> dict[str, int]:
        """板块名 -> 成分股数量。"""
        return self._cached(f"sectors_{block_file}",
                            lambda: self._load_sectors(block_file))

    def _load_sectors(self, block_file: str) -> dict[str, int]:
        text = self.run(["block", "list", "-f", block_file], timeout=240.0)
        out: dict[str, int] = {}
        for line in text.splitlines():
            m = _ROW_RE.match(line.strip())
            if m:
                out[m.group(2)] = int(m.group(3))
        return out

    def members(self, sector: str, block_file: str = CONCEPT_FILE) -> list[str]:
        """板块成分股代码列表（名称列不可信，只取代码）。"""
        key = f"members_{block_file}_{re.sub(r'[^0-9A-Za-z一-龥]', '_', sector)}"
        return self._cached(key, lambda: self._load_members(sector, block_file))

    def _load_members(self, sector: str, block_file: str) -> list[str]:
        text = self.run(["block", "show", sector, "-f", block_file], timeout=240.0)
        codes: list[str] = []
        for line in text.splitlines():
            m = _CODE_ROW_RE.match(line.strip())
            if m:
                codes.append(m.group(1))
        return codes


def board_of(code: str, exchange: str) -> str:
    if exchange == "sh":
        if code.startswith("688"):
            return "科创板"
        return "沪市主板"
    if code.startswith("300") or code.startswith("301"):
        return "创业板"
    return "深市主板"
