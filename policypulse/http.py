"""带限速、UA 与 robots.txt 约束的抓取客户端。

只抓公开页面；单站点串行；不做任何登录/验证码/付费墙绕过。

robots.txt 处理（需求 §1.2），判定分四档，绝不含糊：

- `allowed`    —— 取到规则，按 Allow/Disallow 判定，被禁路径直接拒绝；
- `disallowed` —— 规则明确禁止该路径；
- `unavailable`—— **拿到了 HTTP 响应**但无可用规则（403/404/302 到 HTML/内容不是
                  robots 格式）：走保守策略，仍抓公开页面，但把该 host 间隔抬到
                  `robots_unavailable_interval`，原因写进 warnings 与日报告警区；
- `error`      —— **根本没拿到响应**（TLS 校验失败 / 连接失败 / 超时）：无法判定，
                  **fail-closed，不抓**，并显式报错进告警区。这一档绝不能退化成
                  "unavailable 照抓"，否则遇到真 `Disallow: /` 的站会静默违规。

robots 请求复用 Fetcher 的同一个 `httpx.Client`：两套 TLS 栈（urllib vs httpx）
的 CA / 代理配置不同，同一台机器上可能一个通一个不通，会让上面四档静默翻转。
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PolicyPulse/0.1 (personal research)"
)

log = logging.getLogger("policypulse.http")


class FetchError(RuntimeError):
    pass


class RobotsDisallowed(FetchError):
    """robots.txt 明确禁止，不是抓取失败。"""


class RobotsUnverifiable(FetchError):
    """robots.txt 取不到响应，无法判定：按不抓取处理，不静默放行。"""


@dataclass
class RobotsPolicy:
    """单个 host 的 robots 判定结果。"""

    host: str
    status: str  # allowed / unavailable / error
    parser: RobotFileParser | None = None
    reason: str = ""
    # 站点自己声明的抓取间隔（Crawl-delay）；我们按它来，不讨价还价
    crawl_delay: float = 0.0

    def allows(self, url: str) -> bool:
        if self.status == "allowed":
            return bool(self.parser and self.parser.can_fetch(DEFAULT_UA, url))
        if self.status == "unavailable":
            return True  # 无规则可依，走保守限速抓取
        return False  # error：无法判定，不抓


class RobotsCache:
    """每个 host 只取一次 robots.txt，结果、状态与失败原因都缓存下来。

    `client` 必须是 Fetcher 那个 `httpx.Client`——不另起 urllib，避免两套 TLS 栈
    给出不同结论。
    """

    def __init__(self, client: httpx.Client | None = None, timeout: float = 10.0,
                 user_agent: str = DEFAULT_UA) -> None:
        self.client = client
        self.timeout = timeout
        self.user_agent = user_agent
        self._cache: dict[str, RobotsPolicy] = {}

    def _http(self) -> httpx.Client:
        if self.client is None:  # 独立使用时的兜底，不该在正常链路发生
            self.client = httpx.Client(timeout=self.timeout, follow_redirects=True,
                                       headers={"User-Agent": self.user_agent})
        return self.client

    def observed(self) -> dict[str, RobotsPolicy]:
        """本轮**已经发生过**的判定快照。只为出合规台账用，不触发新请求。"""
        return dict(self._cache)

    def policy_for_host(self, host: str) -> RobotsPolicy | None:
        """已判定过的 host 才返回；没判定过返回 None（不代为发起请求）。"""
        return self._cache.get(host)

    def policy(self, url: str) -> RobotsPolicy:
        host = urlparse(url).netloc
        if host in self._cache:
            return self._cache[host]
        policy = self._fetch(url, host)
        self._cache[host] = policy
        return policy

    @staticmethod
    def _crawl_delay_of(body: str, user_agent: str) -> float:
        """取适用于本 UA 的 Crawl-delay。没有声明就是 0（由调用方决定兜底间隔）。"""
        delay = 0.0
        applies = False
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.lower().startswith("user-agent:"):
                applies = line.split(":", 1)[1].strip() in ("*", user_agent)
                continue
            if applies and line.lower().startswith("crawl-delay:"):
                try:
                    delay = max(delay, float(line.split(":", 1)[1].strip()))
                except ValueError:
                    pass
        return delay

    def _parse_or_unavailable(self, body: str | None, host: str, reason: str = "") -> RobotsPolicy:
        """把 robots 文本解析成策略；拿不到就落成 unavailable 并记下原因。"""
        if not body:
            return RobotsPolicy(host, "unavailable", None, reason or "robots.txt 不可获取")
        parser = RobotFileParser()
        parser.set_url(f"https://{host}/robots.txt")
        parser.parse(body.splitlines())
        return RobotsPolicy(host, "allowed", parser, "robots.txt 已获取",
                            self._crawl_delay_of(body, self.user_agent))

    def _fetch(self, url: str, host: str) -> RobotsPolicy:
        parts = urlparse(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = self._http().get(robots_url, timeout=self.timeout)
        except httpx.HTTPError as exc:
            # 传输层失败 ≠ 站点没有 robots：这一档必须 fail-closed
            return RobotsPolicy(host, "error", None,
                                f"robots.txt 传输失败（{type(exc).__name__}）: {exc}")
        if resp.status_code >= 400:
            return self._parse_or_unavailable(None, host, f"robots.txt HTTP {resp.status_code}")
        # 302 到 HTML 错误页 / 空文件都算"没有规则"：当成规则解析会得出错误结论
        final_url = str(resp.url)
        if not final_url.rstrip("/").endswith("robots.txt"):
            return self._parse_or_unavailable(None, host, f"robots.txt 被重定向到 {final_url}")
        body = resp.text
        if "user-agent" not in body.lower():
            return self._parse_or_unavailable(None, host, "robots.txt 内容不是 robots 格式")
        return self._parse_or_unavailable(body, host)


@dataclass
class Fetcher:
    timeout: float = 25.0
    retries: int = 2
    user_agent: str = DEFAULT_UA
    min_interval: float = 2.0
    verify_tls: bool = True
    robots_unavailable_interval: float = 5.0
    check_robots: bool = True
    _last_request_at: dict[str, float] = field(default_factory=dict)
    _host_interval: dict[str, float] = field(default_factory=dict)
    _warned_hosts: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"},
            verify=self.verify_tls,
        )
        self.robots = RobotsCache(client=self._client, timeout=self.timeout,
                                  user_agent=self.user_agent)
        self.warnings: list[str] = []

    def close(self) -> None:
        self._client.close()

    @property
    def client(self) -> httpx.Client:
        """对外暴露同一个 httpx.Client，便于断言 robots 与正文共用一套传输栈。"""
        return self._client

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 限速 ----------
    def configure_rate_limit(self, url: str, seconds: float) -> None:
        """按 host 设置请求间隔；未配置的 host 用 min_interval。"""
        self._host_interval[urlparse(url).netloc] = float(seconds)

    def interval_for(self, url: str) -> float:
        host = urlparse(url).netloc
        base = self._host_interval.get(host, self.min_interval)
        # 站点声明的 Crawl-delay 优先于我们自己设的更小间隔
        policy = self.robots.policy_for_host(host)
        if policy and policy.crawl_delay:
            base = max(base, policy.crawl_delay)
        return base

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        interval = self._host_interval.get(host, self.min_interval)
        policy = self.robots.policy_for_host(host)
        if policy and policy.crawl_delay:
            interval = max(interval, policy.crawl_delay)
        last = self._last_request_at.get(host)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # ---------- robots ----------
    def _check_robots(self, url: str) -> None:
        if not self.check_robots:
            return
        policy = self.robots.policy(url)
        if policy.status == "allowed" and not policy.parser.can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"robots.txt 禁止抓取 {url}")
        if policy.status == "error":
            # 拿不到响应就无法判定，fail-closed：不抓，并留痕
            raise RobotsUnverifiable(f"{policy.host}: {policy.reason}，无法判定，按不抓取处理")
        if policy.status == "unavailable":
            host = policy.host
            conservative = max(self.robots_unavailable_interval, self.min_interval)
            current = self._host_interval.get(host)
            # 保守间隔优先于源自己声明的较小间隔，并只告警一次
            if current is None or current < conservative:
                self._host_interval[host] = conservative
            if host not in self._warned_hosts:
                self._warned_hosts.add(host)
                msg = (f"{host}: {policy.reason}，无 robots 判定依据，"
                       f"按保守间隔 {conservative}s 抓取")
                self.warnings.append(msg)
                log.warning(msg)

    # ---------- 抓取 ----------
    def get_text(self, url: str) -> str:
        self._check_robots(url)
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            try:
                resp = self._client.get(url)
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                return decode_response(resp)
            except RobotsDisallowed:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络错误统一收敛
                last_err = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1) + random.random())
        raise FetchError(f"failed to fetch {url}: {last_err}")

    def get_json(self, url: str):
        import json

        return json.loads(self.get_text(url))


def decode_response(resp: httpx.Response) -> str:
    """按 Content-Type / meta charset 推断编码，gov.cn 与部委站点混用 utf-8 与 gb2312。"""
    ctype = resp.headers.get("content-type", "")
    enc = None
    for part in ctype.split(";"):
        part = part.strip().lower()
        if part.startswith("charset="):
            enc = part.split("=", 1)[1].strip('"')
    if not enc:
        head = resp.content[:2048].decode("ascii", "ignore").lower()
        for marker in ("charset=gb2312", "charset=gbk", "charset=utf-8"):
            if marker in head:
                enc = marker.split("=", 1)[1]
                break
    if not enc:
        enc = "utf-8"
    if enc in ("gb2312", "gbk"):
        enc = "gb18030"
    return resp.content.decode(enc, errors="replace")


def absolutize(url: str, base: str) -> str:
    return urljoin(base, url)


def content_hash(text: str) -> str:
    normalized = "".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
