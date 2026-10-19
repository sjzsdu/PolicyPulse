"""合规测试：限速、UA、robots.txt。全部离线，不访问真实站点。"""

from __future__ import annotations

import time

import pytest

from policypulse.config import SourceSpec
from policypulse.http import (
    DEFAULT_UA,
    Fetcher,
    RobotsCache,
    RobotsDisallowed,
    RobotsUnverifiable,
    content_hash,
)


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


# ---------- 央行（pbc.gov.cn）的合规换取法 ----------

def test_crawl_delay_declared_by_the_site_is_honored():
    """站点自己声明 Crawl-delay 就得照办（人民网是 120s，故默认不启用）。"""
    cache = RobotsCache()
    cache._cache["people.com.cn"] = cache._parse_or_unavailable(
        "User-agent: *\nDisallow: \nCrawl-delay: 120", "people.com.cn")
    f = Fetcher(min_interval=1.0, check_robots=True)
    f.robots = cache
    try:
        assert f.interval_for("http://people.com.cn/a.htm") == 120.0
    finally:
        f.close()


class _ObservedOnly:
    """只暴露已发生过的 robots 判定：台账不得为了填表去补一次请求。"""

    def __init__(self, observed: dict) -> None:
        self.robots = type("R", (), {"observed": lambda self: observed})()


def test_source_compliance_ledger_marks_disabled_and_local_sources():
    from policypulse.pipeline import _source_compliance

    specs = [
        SourceSpec(id="pbc_xw", name="新闻", org="中国人民银行", tier="部委",
                   adapter="html_list", url="http://www.pbc.gov.cn/xw/", enabled=False),
        SourceSpec(id="pbc_inbound", name="人工入库", org="中国人民银行", tier="部委",
                   adapter="local_inbound", url="http://www.pbc.gov.cn/xw/", enabled=True),
        SourceSpec(id="xinhua_pbc", name="财经·央行", org="中国人民银行（新华网转载）",
                   tier="转载", adapter="html_list", url="http://www.news.cn/fortune/index.htm"),
    ]
    rows = _source_compliance(specs, [], _ObservedOnly({}))
    by_id = {r["id"]: r for r in rows}

    assert by_id["pbc_xw"]["robots"] == "未启用"
    assert "不发起任何请求" in by_id["pbc_xw"]["robots_reason"]
    assert by_id["pbc_inbound"]["robots"] == "本地入库"
    assert "不发起网络请求" in by_id["pbc_inbound"]["robots_reason"]
    # 没有判定过的 host 只能写「未判定」，绝不能为了出这张表去抓一次 robots
    assert by_id["xinhua_pbc"]["robots"] == "未判定"


def test_pbc_reprint_source_is_allowed_by_its_own_robots():
    """新华社 news.cn 的 robots 是放行：转载这条路是实测可用的，不是猜的。"""
    cache = RobotsCache()
    cache._cache["www.news.cn"] = cache._parse_or_unavailable(
        "# robots.txt for http://www.xinhuanet.com/\n\nUser-Agent: *\nAllow: /\n", "www.news.cn")
    policy = cache.policy("http://www.news.cn/fortune/index.htm")
    assert policy.status == "allowed"
    assert policy.allows("http://www.news.cn/fortune/20261019/abc/c.html")


# ---------- robots.txt：共用一套传输栈，且失败原因必须分开 ----------

class _Resp:
    def __init__(self, status_code: int, text: str = "", url: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.url = url


class _StubClient:
    """替代 httpx.Client：能返回响应，也能抛传输层异常。"""

    def __init__(self, resp=None, exc=None) -> None:
        self.resp = resp
        self.exc = exc
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None):
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.resp


def test_robots_uses_the_same_http_client_as_the_fetcher():
    """robots 不能另起 urllib：两套 TLS 栈的 CA/代理配置不同，结论会翻转。"""
    f = Fetcher(min_interval=0.0)
    try:
        assert f.robots.client is f.client, "robots 必须复用 Fetcher 的 httpx.Client"
    finally:
        f.close()


def test_transport_failure_is_never_silently_treated_as_unavailable():
    """钉住这条：传输层取不到 robots 时，不得退化成「不可获取」照抓。"""
    import httpx

    client = _StubClient(exc=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"))
    cache = RobotsCache(client=client)
    policy = cache.policy("https://example.gov.cn/a.htm")
    assert policy.status == "error", "传输失败必须单独成档"
    assert "CERTIFICATE_VERIFY_FAILED" in policy.reason
    assert policy.allows("https://example.gov.cn/a.htm") is False, "error 档必须 fail-closed"

    f = Fetcher(min_interval=0.0, check_robots=True)
    f.robots = cache
    try:
        with pytest.raises(RobotsUnverifiable):
            f.get_text("https://example.gov.cn/a.htm")
    finally:
        f.close()


def test_robots_failure_reasons_are_distinguished():
    url = "https://example.gov.cn/a.htm"
    cases = [
        (_Resp(403, "", "https://example.gov.cn/robots.txt"), "unavailable", "HTTP 403"),
        (_Resp(404, "", "https://example.gov.cn/robots.txt"), "unavailable", "HTTP 404"),
        (_Resp(200, "", "https://example.gov.cn/404.htm"), "unavailable", "被重定向到"),
        (_Resp(200, "<html>oops</html>", "https://example.gov.cn/robots.txt"),
         "unavailable", "内容不是 robots 格式"),
    ]
    for resp, status, needle in cases:
        policy = RobotsCache(client=_StubClient(resp)).policy(url)
        assert policy.status == status, needle
        assert needle in policy.reason, policy.reason
        assert policy.allows(url) is True, "unavailable 走保守抓取，但要有告警"

    good = RobotsCache(client=_StubClient(_Resp(
        200, "User-agent: *\nDisallow: /secret/", "https://example.gov.cn/robots.txt"))).policy(url)
    assert good.status == "allowed"
    assert good.allows("https://example.gov.cn/ok")
    assert not good.allows("https://example.gov.cn/secret/x")


def test_disallowed_path_raises_before_any_request():
    cache = RobotsCache(client=_StubClient(_Resp(
        200, "User-agent: *\nDisallow: /", "https://example.gov.cn/robots.txt")))
    f = Fetcher(min_interval=0.0, check_robots=True)
    f.robots = cache
    try:
        with pytest.raises(RobotsDisallowed):
            f.get_text("https://example.gov.cn/anything")
    finally:
        f.close()
