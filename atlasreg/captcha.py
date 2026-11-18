"""Turnstile token 获取：capsolver / yescaptcha（打码服务）+ 本地浏览器 relay 兜底。

Clerk 的生产实例对脚本化浏览器判定很强（实测 patchright/真 Chrome + CDP 都被拦），
量产唯一稳的路子是打码服务；没有 key 时可用 browser relay（人工点一次）。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config import CaptchaCfg


class CaptchaError(RuntimeError):
    pass


class Solver:
    def __init__(self, cfg: CaptchaCfg):
        self.cfg = cfg

    async def solve(self) -> str:
        p = self.cfg.provider.lower()
        if p == "capsolver":
            return await self._capsolver()
        if p in ("yescaptcha", "yes"):
            return await self._yescaptcha()
        if p in ("2captcha", "twocaptcha"):
            return await self._twocaptcha()
        if p == "manual":
            if not self.cfg.manual_token:
                raise CaptchaError(
                    "provider=manual 但没有 token。用 `--captcha-token <token>` 或 CAPTCHA_TOKEN 传入"
                )
            return self.cfg.manual_token
        raise CaptchaError(f"unknown captcha provider: {self.cfg.provider}")

    # ---------------- capsolver ----------------
    async def _capsolver(self) -> str:
        if not self.cfg.api_key:
            raise CaptchaError("captcha.api_key 为空（CAPTCHA_KEY）")
        url = self.cfg.api_url or "https://api.capsolver.com"
        task = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": self.cfg.page_url,
            "websiteKey": self.cfg.sitekey,
            "metadata": {"action": self.cfg.action},
        }
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(f"{url}/createTask", json={"clientKey": self.cfg.api_key, "task": task})
            d = r.json()
            if d.get("errorId"):
                raise CaptchaError(f"capsolver createTask: {d}")
            tid = d["taskId"]
            deadline = time.time() + self.cfg.timeout
            while time.time() < deadline:
                await asyncio.sleep(2.5)
                r = await c.post(f"{url}/getTaskResult", json={"clientKey": self.cfg.api_key, "taskId": tid})
                d = r.json()
                if d.get("errorId"):
                    raise CaptchaError(f"capsolver getTaskResult: {d}")
                if d.get("status") == "ready":
                    tok = d.get("solution", {}).get("token") or d.get("solution", {}).get("gRecaptchaResponse")
                    if not tok:
                        raise CaptchaError(f"capsolver 无 token: {d}")
                    return tok
        raise CaptchaError("capsolver 超时")

    # ---------------- yescaptcha ----------------
    async def _yescaptcha(self) -> str:
        if not self.cfg.api_key:
            raise CaptchaError("captcha.api_key 为空（CAPTCHA_KEY）")
        url = self.cfg.api_url or "https://api.yescaptcha.com"
        task = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": self.cfg.page_url,
            "websiteKey": self.cfg.sitekey,
            "pageAction": self.cfg.action,
        }
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(f"{url}/createTask", json={"clientKey": self.cfg.api_key, "task": task})
            d = r.json()
            if d.get("errorId"):
                raise CaptchaError(f"yescaptcha createTask: {d}")
            tid = d["taskId"]
            deadline = time.time() + self.cfg.timeout
            while time.time() < deadline:
                await asyncio.sleep(2.5)
                r = await c.post(f"{url}/getTaskResult", json={"clientKey": self.cfg.api_key, "taskId": tid})
                d = r.json()
                if d.get("errorId"):
                    raise CaptchaError(f"yescaptcha getTaskResult: {d}")
                if d.get("status") == "ready":
                    tok = d.get("solution", {}).get("token")
                    if not tok:
                        raise CaptchaError(f"yescaptcha 无 token: {d}")
                    return tok
        raise CaptchaError("yescaptcha 超时")

    # ---------------- 2captcha ----------------
    async def _twocaptcha(self) -> str:
        if not self.cfg.api_key:
            raise CaptchaError("captcha.api_key 为空（CAPTCHA_KEY）")
        url = self.cfg.api_url or "https://2captcha.com"
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(
                f"{url}/in.php",
                data={
                    "key": self.cfg.api_key,
                    "method": "turnstile",
                    "sitekey": self.cfg.sitekey,
                    "pageurl": self.cfg.page_url,
                    "action": self.cfg.action,
                    "json": 1,
                },
            )
            d = r.json()
            if d.get("status") != 1:
                raise CaptchaError(f"2captcha in: {d}")
            tid = d["request"]
            deadline = time.time() + self.cfg.timeout
            while time.time() < deadline:
                await asyncio.sleep(5)
                r = await c.get(
                    f"{url}/res.php",
                    params={"key": self.cfg.api_key, "action": "get", "id": tid, "json": 1},
                )
                d = r.json()
                if d.get("status") == 1:
                    return d["request"]
                if d.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                    raise CaptchaError(f"2captcha res: {d}")
        raise CaptchaError("2captcha 超时")

    @staticmethod
    def token_fields(token: str, style: str = "both") -> dict[str, str]:
        """Clerk FAPI 的 captcha 字段名在版本间有 snake/camel 两种，双写最稳。"""
        if style == "snake":
            return {"captcha_token": token, "captcha_widget_type": "smart"}
        if style == "camel":
            return {"captchaToken": token, "captchaWidgetType": "smart"}
        return {
            "captcha_token": token,
            "captchaToken": token,
            "captcha_widget_type": "smart",
            "captchaWidgetType": "smart",
        }


def parse_manual_token(raw: str) -> str:
    """从浏览器手动复制回来的 JSON 或裸 token 里取 token。"""
    raw = raw.strip()
    if raw.startswith("{"):
        d = json.loads(raw)
        for k in ("token", "turnstile_token", "captcha", "value"):
            if d.get(k):
                return d[k]
    return raw
