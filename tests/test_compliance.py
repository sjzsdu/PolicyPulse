"""合规测试：限速、UA、robots.txt。全部离线，不访问真实站点。"""

from __future__ import annotations

import time

import pytest

from policypulse.config import SourceSpec
from policypulse.http import DEFAULT_UA, Fetcher, RobotsCache, RobotsDisallowed, content_hash


class _FakeFetcher:
    """只替换 HTTP 层：返回冻结的真实页面，限流/robots 逻辑仍是真实代码。"""

    def __init__(self, html_by_url: dict, robots_body: str | None = "User-agent: *\nAllow: /") -> None:
        self.html_by_url = html_by_url
        self.robots_body = robots_body
        self.calls: list[tuple[float, str]] = []
        self.errors: dict[str, Exception] = {}

    def get_text(self, url: str) -> str:
        self.calls.append((time.monotonic(), url))
        if url in self.errors:
            raise self.errors[url]
        if url.endswith("robots.txt"):
            if self.robots_body is None:
                from policypulse.http import FetchError

                raise FetchError("HTTP 403 for robots.txt")
            return self.robots_body
        return self.html_by_url.get(url, "")

    def get_json(self, url: str):
        import json

        return json.loads(self.get_text(url))


def _spec(**extra) -> SourceSpec:
    return SourceSpec(id="s", name="n", org="o", tier="部委", adapter="html_list",
                      url="https://example.gov.cn/list/", extra=extra)


def test_rate_limit_from_sources_yaml_is_respected():
    """E：源的 rate_limit_seconds 真接上，而不是被硬编码的 2.0 覆盖。"""
    f = Fetcher(min_interval=0.0)
    f.configure_rate_limit("https://ndrc.gov.cn/a", 2.0)
    f.configure_rate_limit("https://pbc.gov.cn/a", 7.5)
    assert f.interval_for("https://ndrc.gov.cn/x") == 2.0
    assert f.interval_for("https://pbc.gov.cn/x") == 7.5
    assert f.interval_for("https://unknown.gov.cn/x") == 0.0  # 未配置 → 默认值


def test_rate_limit_actually_sleeps_between_requests():
    from policypulse.http import Fetcher as RealFetcher

    f = RealFetcher(min_interval=0.25, retries=0, check_robots=False)
    try:
        # 只验证节流本身：连一个本地不可达端口，间隔仍应被遵守
        t0 = time.monotonic()
        for _ in range(2):
            try:
                f.get_text("http://127.0.0.1:9/one")
            except Exception:
                pass
        elapsed = time.monotonic() - t0
    finally:
        f.close()
    assert elapsed >= 0.25, "同一 host 连续请求之间没有等待"


def test_user_agent_is_explicit_and_identifies_the_project():
    assert "PolicyPulse" in DEFAULT_UA
    assert "personal research" in DEFAULT_UA


def test_robots_disallow_blocks_the_fetch():
    robots = "User-agent: *\nDisallow: /secret/\nAllow: /open/"
    cache = RobotsCache()
    cache._cache["example.gov.cn"] = cache._parse_or_unavailable(robots, "example.gov.cn")
    assert cache.policy("https://example.gov.cn/open/a.htm").allows("https://example.gov.cn/open/a.htm")
    assert not cache.policy("https://example.gov.cn/secret/a.htm").allows(
        "https://example.gov.cn/secret/a.htm")


def test_robots_unavailable_triggers_conservative_throttle_and_warning():
    """B：拿不到 robots 的源不能当没事，要有保守策略 + 留痕。"""
    f = Fetcher(min_interval=1.0, robots_unavailable_interval=5.0, check_robots=True)
    f.robots = RobotsCache()
    f.robots._cache["example.gov.cn"] = f.robots._parse_or_unavailable(None, "example.gov.cn")
    f._check_robots("https://example.gov.cn/a.htm")
    assert f.interval_for("https://example.gov.cn/a.htm") == 5.0
    assert any("example.gov.cn" in w for w in f.warnings)
    assert "robots.txt" in f.warnings[0]


def test_content_hash_is_stable():
    assert content_hash("甲 乙\n丙") == content_hash("甲乙丙")
