"""带限速、重试与 UA 的抓取客户端。

只抓公开页面；单站点串行、请求间隔可配置；不做任何登录/验证码/付费墙绕过。
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PolicyPulse/0.1 (personal research)"
)


class FetchError(RuntimeError):
    pass


@dataclass
class Fetcher:
    timeout: float = 25.0
    retries: int = 2
    user_agent: str = DEFAULT_UA
    min_interval: float = 2.0
    verify_tls: bool = True
    _last_request_at: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"},
            verify=self.verify_tls,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self, url: str) -> None:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at[host] = time.monotonic()

    def get_text(self, url: str) -> str:
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle(url)
            try:
                resp = self._client.get(url)
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                return decode_response(resp)
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
