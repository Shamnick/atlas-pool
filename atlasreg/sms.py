"""手机号接码：sms-activate / 5sim / 人工。"""
from __future__ import annotations

import asyncio
import time

import httpx

from .config import SmsCfg


class SmsError(RuntimeError):
    pass


class Rent:
    """一次租号会话。"""

    def __init__(self, provider: str, phone: str, ref: str, extra: dict | None = None):
        self.provider = provider
        self.phone = phone          # E.164，如 +12025550143
        self.ref = ref              # 各平台自己的 id
        self.extra = extra or {}


class Sms:
    def __init__(self, cfg: SmsCfg):
        self.cfg = cfg

    async def rent(self) -> Rent:
        p = self.cfg.provider.lower()
        if p == "sms-activate":
            return await self._sa_rent()
        if p == "5sim":
            return await self._5sim_rent()
        raise SmsError(f"unknown sms provider: {self.cfg.provider}")

    async def wait_code(self, rent: Rent, timeout: int | None = None) -> str:
        p = rent.provider.lower()
        if p == "sms-activate":
            return await self._sa_code(rent, timeout)
        if p == "5sim":
            return await self._5sim_code(rent, timeout)
        raise SmsError(f"unknown sms provider: {rent.provider}")

    async def done(self, rent: Rent) -> None:
        try:
            if rent.provider == "sms-activate":
                async with httpx.AsyncClient(timeout=20) as c:
                    await c.get(
                        "https://api.sms-activate.org/stubs/handler_api.php",
                        params={"api_key": self.cfg.api_key, "action": "setStatus",
                                "status": 6, "id": rent.ref},
                    )
            elif rent.provider == "5sim":
                async with httpx.AsyncClient(timeout=20) as c:
                    await c.get(
                        f"https://5sim.net/v1/user/finish/{rent.ref}",
                        headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                    )
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- sms-activate
    async def _sa_rent(self) -> Rent:
        if not self.cfg.api_key:
            raise SmsError("sms.api_key 为空（SMS_KEY）")
        params = {
            "api_key": self.cfg.api_key,
            "action": "getNumber",
            "service": self.cfg.service or "ot",
            "country": self.cfg.country,
        }
        if self.cfg.operator and self.cfg.operator != "any":
            params["operator"] = self.cfg.operator
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get("https://api.sms-activate.org/stubs/handler_api.php", params=params)
            txt = r.text.strip()
            if txt.startswith("ACCESS_NUMBER"):
                _, ref, phone = txt.split(":")
                return Rent("sms-activate", "+" + phone.lstrip("+"), ref)
            raise SmsError(f"sms-activate getNumber: {txt}")

    async def _sa_code(self, rent: Rent, timeout: int | None) -> str:
        deadline = time.time() + (timeout or self.cfg.wait)
        async with httpx.AsyncClient(timeout=30) as c:
            while time.time() < deadline:
                await asyncio.sleep(5)
                r = await c.get(
                    "https://api.sms-activate.org/stubs/handler_api.php",
                    params={"api_key": self.cfg.api_key, "action": "getStatus", "id": rent.ref},
                )
                txt = r.text.strip()
                if txt.startswith("STATUS_OK"):
                    return txt.split(":")[1]
                if txt in ("STATUS_CANCEL", "NO_ACTIVATION"):
                    raise SmsError(f"sms-activate 状态异常: {txt}")
        raise SmsError("sms-activate 等码超时")

    # ---------------------------------------------------------------- 5sim
    async def _5sim_rent(self) -> Rent:
        if not self.cfg.api_key:
            raise SmsError("sms.api_key 为空（SMS_KEY）")
        url = f"https://5sim.net/v1/user/buy/activation/{self.cfg.fivesim_country}/any/{self.cfg.fivesim_product}"
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {self.cfg.api_key}"})
            if r.status_code != 200:
                raise SmsError(f"5sim buy: {r.status_code} {r.text[:200]}")
            d = r.json()
            return Rent("5sim", "+" + str(d["phone"]), str(d["id"]))

    async def _5sim_code(self, rent: Rent, timeout: int | None) -> str:
        deadline = time.time() + (timeout or self.cfg.wait)
        async with httpx.AsyncClient(timeout=30) as c:
            while time.time() < deadline:
                await asyncio.sleep(5)
                r = await c.get(
                    f"https://5sim.net/v1/user/check/{rent.ref}",
                    headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                )
                if r.status_code != 200:
                    continue
                d = r.json()
                for sms in d.get("sms", []):
                    code = str(sms.get("code", "")).strip()
                    if code:
                        return code
        raise SmsError("5sim 等码超时")
