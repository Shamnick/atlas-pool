"""Marble API（atlas.worldlabs.ai/api/v2）：开户 + 积分校验。

入口全部来自前端 bundle 反编译（RECON.md 第 4 节）：
  POST /api/v2/users:signUp            ← 开户，后端在此发 10000 credits
  GET  /api/v2/users/me
  GET  /api/v2/accounts
  POST /api/v2/accounts/{account}/billing/developerApi:getPrepaidBalance
  POST /api/v2/accounts/{account}/billing/developerApi:listBillingTransactions
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import AtlasCfg
from .http import Http


class MarbleError(RuntimeError):
    def __init__(self, status: int, payload: Any = None):
        super().__init__(f"marble http {status}: {str(payload)[:300]}")
        self.status = status
        self.payload = payload


class Marble:
    def __init__(self, cfg: AtlasCfg, http: Http, jwt: str):
        self.cfg = cfg
        self.http = http
        self.jwt = jwt

    def _h(self) -> dict[str, str]:
        h = self.http.headers(referer=f"{self.cfg.app_url}/")
        h["Authorization"] = f"Bearer {self.jwt}"
        h["Accept"] = "application/json"
        # 这一路是自家 API，跨域头不需要，去掉 Origin 防后端 CORS 校验误判
        h.pop("Origin", None)
        return h

    async def _post(self, path: str, body: dict | None = None) -> Any:
        r = await self.http.post(f"{self.cfg.app_url}{path}", json=body or {}, headers=self._h())
        if r.status_code >= 400:
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = r.text[:300]
            raise MarbleError(r.status_code, payload)
        if not r.content:
            return None
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return r.text

    async def _get(self, path: str) -> Any:
        r = await self.http.get(f"{self.cfg.app_url}{path}", headers=self._h())
        if r.status_code >= 400:
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = r.text[:300]
            raise MarbleError(r.status_code, payload)
        return r.json()

    # ------------------------------------------------------------------ 账户生命周期
    async def me(self) -> Any:
        return await self._get("/api/v2/users/me")

    async def accounts(self) -> Any:
        return await self._get("/api/v2/accounts")

    async def sign_up_user(
        self,
        email: str | None = None,
        display_name: str = "Atlas User",
        account_display_name: str | None = None,
        project_name: str | None = None,
        residency_region: str | None = None,
    ) -> Any:
        """开户：后端在此给新用户发 10000 credits。409 = 已建过。"""
        body: dict[str, Any] = {
            "displayName": display_name,
            "accountDisplayName": account_display_name or f"{display_name}'s Organization",
            "defaultProjectDisplayName": project_name or self.cfg.default_project_name,
        }
        if email:
            body["email"] = email
        region = residency_region or self.cfg.residency_region
        if region:
            body["residencyRegion"] = region
        try:
            return await self._post("/api/v2/users:signUp", body)
        except MarbleError as e:
            if e.status == 409:
                return {"already_exists": True, "detail": e.payload}
            raise

    # ------------------------------------------------------------------ 积分
    async def balance(self, account: str) -> Any:
        return await self._post(
            f"/api/v2/accounts/{account}/billing/developerApi:getPrepaidBalance", {}
        )

    async def transactions(self, account: str) -> Any:
        return await self._post(
            f"/api/v2/accounts/{account}/billing/developerApi:listBillingTransactions", {}
        )

    async def first_account(self) -> str | None:
        """返回裸 account id（acct_xxx）。API 里的 name 形如 accounts/acct_xxx，
        拼进 URL 会变成 /accounts/accounts/acct_xxx → 404，必须去前缀。"""
        d = await self.accounts()
        items = d if isinstance(d, list) else (d or {}).get("accounts") or (d or {}).get("items") or []
        if isinstance(items, list) and items:
            it = items[0]
            name = it.get("name") or it.get("id") or it.get("account") or ""
            return str(name).split("/")[-1] or None
        return None

    async def credits_summary(self, account: str) -> dict:
        """余额 + 明细（含"Developer Signup Credits"这类入账记录）。"""
        bal = await self.balance(account)
        try:
            tx = await self.transactions(account)
        except MarbleError:
            tx = None
        return {"balance": bal, "transactions": tx}

    async def credits_total(self, account: str) -> float | None:
        """把 getPrepaidBalance 各种返回形态归一成数字。"""
        d = await self.balance(account)
        if not isinstance(d, dict):
            return None
        for k in ("totalCredits", "balance", "credits", "prepaidBalance", "prepaid_balance",
                  "amount", "creditCount"):
            v = d.get(k)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
                return float(v["value"])
        # 有些实现返回 {balance:{credits:10000}}
        for v in d.values():
            if isinstance(v, dict):
                for k2 in ("credits", "balance", "value"):
                    if isinstance(v.get(k2), (int, float)):
                        return float(v[k2])
        return None
