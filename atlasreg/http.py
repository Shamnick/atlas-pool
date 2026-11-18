"""HTTP 客户端：cookie 会话 + 代理 + 指纹一致的默认头。"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx


class Http:
    """一个实例 = 一个账号的独立上下文（cookie 隔离）。"""

    def __init__(self, ua: str, proxy: str | None = None, origin: str = "https://atlas.worldlabs.ai"):
        self.ua = ua
        self.proxy = proxy
        self.origin = origin
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Http":
        transport_kwargs: dict[str, Any] = {}
        if self.proxy:
            # socks5:// 或 http:// 都支持（httpx-socks）
            transport_kwargs["proxy"] = self.proxy
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(30.0, connect=20.0),
            follow_redirects=True,
            headers={
                "User-Agent": self.ua,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "application/json, text/plain, */*",
            },
            **transport_kwargs,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def cookies(self) -> dict[str, str]:
        assert self._client is not None
        return {c.name: c.value for c in self._client.cookies.jar}

    def headers(self, referer: str | None = None) -> dict[str, str]:
        ref = referer or f"{self.origin}/sign-in"
        return {
            "Origin": self.origin,
            "Referer": ref,
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

    async def get(self, url: str, **kw) -> httpx.Response:
        assert self._client
        return await self._client.get(url, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        assert self._client
        return await self._client.post(url, **kw)

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        assert self._client
        return await self._client.request(method, url, **kw)


async def jitter(base: float, spread: float = 0.6) -> None:
    await asyncio.sleep(base + random.uniform(0, spread))
