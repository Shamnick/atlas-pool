"""Clerk FAPI 客户端：sign_up → email_code → phone_code → session token。

协议细节见 RECON.md 第 2 节，全部来自实测。
"""
from __future__ import annotations

import asyncio
import random
import string
from typing import Any

import httpx

from .config import AtlasCfg
from .http import Http


class ClerkError(RuntimeError):
    def __init__(self, code: str, payload: Any = None):
        super().__init__(f"{code}: {payload}")
        self.code = code
        self.payload = payload


def _err(resp: httpx.Response) -> ClerkError:
    try:
        d = resp.json()
    except Exception:  # noqa: BLE001
        return ClerkError(f"http_{resp.status_code}", resp.text[:300])
    errs = d.get("errors") or []
    code = errs[0].get("code") if errs else f"http_{resp.status_code}"
    return ClerkError(code or f"http_{resp.status_code}", d)


def gen_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    core = "".join(random.choices(alphabet, k=n))
    return f"{core[:6]}!{core[6:12]}#{core[12:]}"[:20]


class Clerk:
    """一个实例对应一个 client（cookie 独立），一次注册一个 sign_up。"""

    def __init__(self, cfg: AtlasCfg, http: Http):
        self.cfg = cfg
        self.http = http
        self.sign_up_id: str | None = None
        self.session_id: str | None = None

    # ---------------------------------------------------------------- 基础
    def _url(self, path: str) -> str:
        return (
            f"{self.cfg.clerk_fapi}{path}"
            f"?__clerk_api_version={self.cfg.clerk_api_version}"
            f"&_clerk_js_version={self.cfg.clerk_js_version}"
        )

    async def bootstrap(self) -> dict:
        """GET /v1/client —— 必须拿到 __client cookie，否则后续 401。"""
        r = await self.http.get(self._url("/v1/client"), headers=self.http.headers())
        if r.status_code != 200:
            raise _err(r)
        return r.json().get("response", {})

    async def environment(self) -> dict:
        r = await self.http.get(self._url("/v1/environment"), headers=self.http.headers())
        if r.status_code != 200:
            raise _err(r)
        d = r.json()
        # environment 是顶层对象（无 response 包装），client 才有
        return d.get("response") or d

    async def _post(self, path: str, body: dict | None = None, referer: str | None = None) -> dict:
        r = await self.http.post(self._url(path), json=body or {}, headers=self.http.headers(referer))
        if r.status_code >= 400:
            raise _err(r)
        return r.json().get("response", r.json())

    # ---------------------------------------------------------------- 注册
    async def create_sign_up(
        self,
        email: str,
        phone: str,
        password: str,
        captcha_fields: dict[str, str],
        timezone: str = "America/New_York",
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "email_address": email,
            "phone_number": phone,
            "password": password,
            "timezone": timezone,
            "locale": "en-US",
            **captcha_fields,
        }
        if first_name:
            body["first_name"] = first_name
        if last_name:
            body["last_name"] = last_name
        d = await self._post("/v1/client/sign_ups", body)
        self.sign_up_id = d.get("id")
        return d

    async def prepare_verification(self, strategy: str = "email_code") -> dict:
        assert self.sign_up_id
        return await self._post(
            f"/v1/client/sign_ups/{self.sign_up_id}/prepare_verification",
            {"strategy": strategy},
        )

    async def attempt_verification(self, code: str, strategy: str = "email_code") -> dict:
        assert self.sign_up_id
        return await self._post(
            f"/v1/client/sign_ups/{self.sign_up_id}/attempt_verification",
            {"strategy": strategy, "code": code},
        )

    async def update_sign_up(self, **fields) -> dict:
        """PATCH /v1/client/sign_ups/{id} —— 补 phone / 补 password 用。"""
        assert self.sign_up_id
        r = await self.http.request(
            "PATCH",
            self._url(f"/v1/client/sign_ups/{self.sign_up_id}"),
            json=fields,
            headers=self.http.headers(),
        )
        if r.status_code >= 400:
            raise _err(r)
        return r.json().get("response", r.json())

    async def fetch_sign_up(self) -> dict:
        assert self.sign_up_id
        r = await self.http.get(
            self._url(f"/v1/client/sign_ups/{self.sign_up_id}"), headers=self.http.headers()
        )
        if r.status_code >= 400:
            raise _err(r)
        return r.json().get("response", r.json())

    # ---------------------------------------------------------------- 会话
    async def touch_session(self, session_id: str | None = None) -> dict:
        sid = session_id or self.session_id
        assert sid
        r = await self.http.post(
            self._url(f"/v1/client/sessions/{sid}/touch"), json={}, headers=self.http.headers()
        )
        if r.status_code >= 400:
            raise _err(r)
        return r.json().get("response", r.json())

    async def session_token(self, session_id: str | None = None) -> str:
        sid = session_id or self.session_id
        assert sid
        r = await self.http.post(
            self._url(f"/v1/client/sessions/{sid}/tokens"), json={}, headers=self.http.headers()
        )
        if r.status_code >= 400:
            raise _err(r)
        d = r.json().get("response", r.json())
        jwt = d.get("jwt")
        if not jwt:
            raise ClerkError("no_jwt", d)
        return jwt

    # ---------------------------------------------------------------- 高阶：完整注册
    async def full_sign_up(
        self,
        email: str,
        phone: str,
        password: str,
        captcha_fields: dict[str, str],
        get_email_code,
        get_sms_code,
        log=print,
    ) -> dict:
        """跑完 Clerk 侧全流程，返回 {sign_up, session_id}。"""
        await self.bootstrap()
        log("  [clerk] client ok")
        su = await self.create_sign_up(email, phone, password, captcha_fields)
        log(f"  [clerk] sign_up={su.get('id')} status={su.get('status')} "
            f"unverified={su.get('unverified_fields')} missing={su.get('missing_fields')}")

        # email_code
        for attempt in range(3):
            unverified = su.get("unverified_fields") or []
            if "email_address" in unverified or not unverified:
                try:
                    su = await self.prepare_verification("email_code")
                    log("  [clerk] email_code 已发送")
                except ClerkError as e:
                    log(f"  [clerk] prepare email_code: {e.code}")
                code = await get_email_code()
                log(f"  [clerk] 收到邮箱验证码 {code}")
                su = await self.attempt_verification(code, "email_code")
                break
            break
        log(f"  [clerk] after email: status={su.get('status')} unverified={su.get('unverified_fields')}")

        # phone_code（Clerk 的 phone 是 required，一般必过）
        if "phone_number" in (su.get("unverified_fields") or []) or su.get("status") != "complete":
            try:
                su = await self.prepare_verification("phone_code")
                log("  [clerk] phone_code 已发送")
            except ClerkError as e:
                log(f"  [clerk] prepare phone_code: {e.code}")
            code = await get_sms_code()
            log(f"  [clerk] 收到短信验证码 {code}")
            su = await self.attempt_verification(code, "phone_code")
            log(f"  [clerk] after phone: status={su.get('status')}")

        if su.get("status") != "complete":
            raise ClerkError("sign_up_incomplete", su)

        sid = su.get("created_session_id")
        if not sid:
            raise ClerkError("no_session", su)
        self.session_id = sid
        await self.touch_session(sid)
        log(f"  [clerk] session={sid} 完成")
        return {"sign_up": su, "session_id": sid}
