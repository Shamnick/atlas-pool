"""Microsoft Graph 接码：一个 hotmail 账号 = 无限收件地址（plus 别名）。

凭据来源（三选一，优先级从高到低）：
  1. refresh_token —— device code 登录一次拿到的长期令牌（推荐，免密码、免 MFA）
  2. password      —— 仅用于 OWA 兜底（微软已禁 basic auth / ROPC，一般不生效）
  3. 手工 cookie   —— 最后手段

拿码路径：Graph `GET /me/messages` → 匹配 toRecipients → 提 6 位数字。
plus 别名（user+atlas1@hotmail.com）天然落到同一收件箱，等于一个号能喂无限次注册。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
# Microsoft Graph Command Line Tools：public client，允许 device code，个人账号可用
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
SCOPE = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read offline_access"

CODE_RE = re.compile(r"\b(\d{6})\b")
CODE_RE_HTML = re.compile(r">\s*(\d{6})\s*<")


class MSError(RuntimeError):
    pass


@dataclass
class MSAccount:
    email: str
    refresh_token: str = ""
    password: str = ""
    uuid: str = ""
    access_token: str = ""
    token_exp: float = 0.0
    alias_prefix: str = ""          # 用于生成 plus 别名
    note: str = ""
    ok_count: int = 0
    fail_count: int = 0

    @property
    def local(self) -> str:
        return self.email.split("@")[0]

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1]

    def alias(self, tag: str) -> str:
        """user@hotmail.com → user+tag@hotmail.com"""
        base = self.local.split("+")[0]
        return f"{base}+{tag}@{self.domain}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email, "refresh_token": self.refresh_token, "password": self.password,
            "uuid": self.uuid, "note": self.note, "ok_count": self.ok_count, "fail_count": self.fail_count,
        }


def parse_line(line: str) -> MSAccount:
    """解析 `email----password----uuid----refresh_token` 这种一行四段的格式。"""
    line = line.strip()
    if not line or line.startswith("#"):
        raise MSError("空行")
    parts = line.split("----")
    if len(parts) < 2:
        raise MSError(f"格式不对（至少 email----password）：{line[:40]}")
    acc = MSAccount(email=parts[0].strip(), password=parts[1].strip())
    if len(parts) >= 3:
        acc.uuid = parts[2].strip()
    if len(parts) >= 4:
        acc.refresh_token = parts[3].strip()
    return acc


class Pool:
    """账号池：文件持久化 + 轮转 + 令牌自动刷新。"""

    def __init__(self, path: str | Path, accounts: list[MSAccount] | None = None):
        self.path = Path(path)
        self.accounts: list[MSAccount] = accounts or []
        self._idx = 0
        self._lock = asyncio.Lock()
        if self.path.exists() and not accounts:
            self.load()

    # ---------------------------------------------------------------- 持久化
    def load(self) -> "Pool":
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.accounts = [MSAccount(**{k: v for k, v in a.items() if k in MSAccount.__annotations__})
                         for a in raw]
        return self

    def save(self) -> None:
        self.path.write_text(
            json.dumps([a.to_dict() for a in self.accounts], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    def add(self, acc: MSAccount) -> None:
        for i, a in enumerate(self.accounts):
            if a.email.lower() == acc.email.lower():
                if acc.refresh_token:
                    self.accounts[i].refresh_token = acc.refresh_token
                if acc.password:
                    self.accounts[i].password = acc.password
                self.save()
                return
        self.accounts.append(acc)
        self.save()

    def pick(self) -> MSAccount:
        if not self.accounts:
            raise MSError("微软账号池为空（先 reg.py ms-login 或导入账号）")
        # 按成功率轮转
        usable = [a for a in self.accounts if a.refresh_token] or self.accounts
        usable.sort(key=lambda a: a.fail_count - a.ok_count * 3)
        acc = usable[self._idx % len(usable)]
        self._idx += 1
        return acc

    # ---------------------------------------------------------------- token
    async def token(self, acc: MSAccount) -> str:
        if acc.access_token and acc.token_exp - 120 > time.time():
            return acc.access_token
        if not acc.refresh_token:
            raise MSError(f"{acc.email} 没有 refresh_token，先跑 reg.py ms-login 授权一次")
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post(f"{AUTHORITY}/token", data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": acc.refresh_token,
                "scope": SCOPE,
            })
            if r.status_code != 200:
                acc.fail_count += 1
                self.save()
                raise MSError(f"{acc.email} 刷新令牌失败: {r.status_code} {r.text[:200]}")
            d = r.json()
        acc.access_token = d["access_token"]
        acc.token_exp = time.time() + int(d.get("expires_in", 3600))
        if d.get("refresh_token"):
            acc.refresh_token = d["refresh_token"]     # MSA 会轮换，必须回写
            self.save()
        return acc.access_token

    # ---------------------------------------------------------------- 取码
    async def wait_code(self, address: str, since_ts: float | None = None,
                        timeout: int = 180, poll: float = 3.0) -> str:
        acc = self.pick()
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            tok = await self.token(acc)
            msgs = await self._messages(acc, tok, top=15)
            for m in msgs:
                mid = m.get("id", "")
                if mid in seen:
                    continue
                seen.add(mid)
                if since_ts:
                    ts = _parse_dt(m.get("receivedDateTime"))
                    if ts and ts < since_ts - 180:
                        continue
                if not _to_matches(m, address):
                    continue
                code = _extract_code(m)
                if code:
                    acc.ok_count += 1
                    return code
            await asyncio.sleep(poll)
        raise MSError(f"Graph 等码超时（{address}, 账号 {acc.email}）")

    async def _messages(self, acc: MSAccount, tok: str, top: int = 15) -> list[dict]:
        url = (f"{GRAPH}/me/messages?$top={top}"
               "&$select=id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,isDraft"
               "&$orderby=receivedDateTime%20desc")
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 401:
                acc.access_token = ""
                tok = await self.token(acc)
                r = await c.get(url, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code != 200:
                raise MSError(f"Graph messages {r.status_code}: {r.text[:200]}")
            return r.json().get("value", [])


def _parse_dt(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:  # noqa: BLE001
        return None


def _to_matches(msg: dict, address: str) -> bool:
    addr = address.lower()
    base = addr.split("@")[0].split("+")[0]
    dom = addr.split("@")[-1]
    for rc in msg.get("toRecipients") or []:
        e = ((rc.get("emailAddress") or {}).get("address") or "").lower()
        if e == addr:
            return True
        # plus 别名 / 大小写 / 同一账号不同别名都算
        if e.split("@")[0].split("+")[0] == base and e.split("@")[-1] == dom:
            return True
    return False


def _extract_code(msg: dict) -> str | None:
    subject = msg.get("subject") or ""
    preview = msg.get("bodyPreview") or ""
    body = ((msg.get("body") or {}).get("content") or "")[:4000]
    for text in (subject, preview, body):
        if not text:
            continue
        m = CODE_RE.search(text.replace("&nbsp;", " "))
        if m:
            return m.group(1)
        m = CODE_RE_HTML.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- device code 登录
async def device_login(account_email: str | None = None, on_code=None, timeout: int = 900) -> dict:
    """device code 授权：把 user_code 给用户，轮询拿 refresh_token。"""
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(f"{AUTHORITY}/devicecode", data={"client_id": CLIENT_ID, "scope": SCOPE})
        if r.status_code != 200:
            raise MSError(f"devicecode {r.status_code}: {r.text[:300]}")
        d = r.json()
    code = d["user_code"]
    url = d.get("verification_uri", "https://microsoft.com/devicelogin")
    if on_code:
        on_code(code, url)
    interval = int(d.get("interval", 5))
    deadline = time.time() + min(timeout, int(d.get("expires_in", 900)))
    async with httpx.AsyncClient(timeout=40) as c:
        while time.time() < deadline:
            await asyncio.sleep(interval)
            r = await c.post(f"{AUTHORITY}/token", data={
                "client_id": CLIENT_ID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": d["device_code"],
            })
            if r.status_code == 200:
                tok = r.json()
                # 取一下邮箱确认是哪个账号
                me = None
                try:
                    g = await c.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
                    if g.status_code == 200:
                        me = g.json()
                except Exception:  # noqa: BLE001
                    pass
                email = (me or {}).get("mail") or (me or {}).get("userPrincipalName") or account_email
                return {"email": email, **tok, "me": me}
            err = r.json().get("error", "")
            if err not in ("authorization_pending", "slow_down"):
                raise MSError(f"device login: {err} {r.text[:200]}")
    raise MSError("device code 登录超时")
