"""邮箱池与验证码收取：自建 catch-all（IMAP / HTTP worker）或 mail.tm。"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import random
import re
import string
import time
from email.header import decode_header

import httpx

from .config import MailCfg
from .mail_ms import Pool as MSPool, MSAccount, parse_line

CODE_RE = re.compile(r"\b(\d{6})\b")
_MS_POOLS: dict[str, MSPool] = {}


def ms_pool(cfg: MailCfg) -> MSPool:
    """按文件路径缓存的微软账号池单例。"""
    path = cfg.ms_accounts_file or "ms_accounts.json"
    if path not in _MS_POOLS:
        _MS_POOLS[path] = MSPool(path)
    return _MS_POOLS[path]
CLERK_SENDERS = ("clerk", "worldlabs", "notifications")


class MailError(RuntimeError):
    pass


def _mailtm_domain() -> str:
    import urllib.request

    try:
        with urllib.request.urlopen("https://api.mail.tm/domains?page=1", timeout=20) as r:
            d = json.load(r)
        return d["hydra:member"][0]["domain"]
    except Exception as e:  # noqa: BLE001
        raise MailError(f"取 mail.tm 域名失败: {e}") from e


def random_local(n: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=n))


class Mailbox:
    def __init__(self, cfg: MailCfg, address: str | None = None):
        self.cfg = cfg
        if address:
            self.address = address
            return
        if cfg.provider.lower() == "msowa":
            base = cfg.ms_email
            if not base:
                raise MailError("mail.ms_email 未配置（msowa 模式要知道用哪个 hotmail 收信）")
            local, dom = base.split("@")[0].split("+")[0], base.split("@")[-1]
            self.address = f"{local}+{random_local(7)}@{dom}"
            return
        if cfg.provider.lower() == "ms":
            pool = ms_pool(cfg)
            acc = pool.pick()
            tag = random_local(8)
            self.address = acc.alias(tag)      # user+tag@hotmail.com，邮件落到同一收件箱
            self.ms_account = acc
            return
        domain = cfg.domain
        if not domain:
            if cfg.provider.lower() == "mailtm":
                domain = _mailtm_domain()
            else:
                raise MailError(
                    "mail.domain 未配置（MAIL_DOMAIN）。自建 catch-all 域名或改用 provider: mailtm 测试"
                )
        self.address = f"{random_local(cfg.local_len)}@{domain}"

    # ------------------------------------------------------------------ 取码
    async def wait_code(self, since_ts: float | None = None, timeout: int | None = None) -> str:
        p = self.cfg.provider.lower()
        deadline = time.time() + (timeout or self.cfg.wait)
        while time.time() < deadline:
            if p == "imap":
                code = await asyncio.to_thread(self._imap_code, since_ts)
            elif p == "http":
                code = await self._http_code()
            elif p == "msowa":
                from .owa import Owa

                code = await asyncio.to_thread(
                    Owa(self.cfg.owa_profile).wait_code,
                    self.address, since_ts, timeout or self.cfg.wait,
                )
            elif p == "ms":
                code = await ms_pool(self.cfg).wait_code(self.address, since_ts, timeout=timeout or self.cfg.wait)
            else:
                code = await self._mailtm_code()
            if code:
                return code
            await asyncio.sleep(3)
        raise MailError(f"等待验证码超时: {self.address}")

    # ------------------------------------------------------------------ IMAP
    def _imap_code(self, since_ts: float | None) -> str | None:
        c = self.cfg
        if not c.imap_host:
            raise MailError("imap_host 未配置")
        try:
            M = imaplib.IMAP4_SSL(c.imap_host, c.imap_port)
            M.login(c.imap_user, c.imap_pass)
            M.select(c.imap_folder)
            # 只取最近 20 封，按 To 过滤
            typ, data = M.search(None, "ALL")
            ids = data[0].split()[-20:]
            for i in reversed(ids):
                typ, msg_data = M.fetch(i, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                to = " ".join(
                    str(v) for k, v in msg.items() if k.lower() in ("to", "delivered-to", "x-original-to")
                ).lower()
                if self.address.lower() not in to:
                    continue
                if since_ts:
                    try:
                        ts = email.utils.mktime_tz(email.utils.parsedate_tz(msg.get("Date")))
                        if ts < since_ts - 120:
                            continue
                    except Exception:
                        pass
                body = self._body(msg)
                m = CODE_RE.search(body)
                if m:
                    M.logout()
                    return m.group(1)
            M.logout()
        except Exception as e:  # noqa: BLE001
            raise MailError(f"IMAP 错误: {e}") from e
        return None

    @staticmethod
    def _body(msg) -> str:
        chunks: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True) or b""
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        else:
            payload = msg.get_payload(decode=True) or b""
            chunks.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
        return "\n".join(chunks)

    # ------------------------------------------------------------------ HTTP worker
    async def _http_code(self) -> str | None:
        c = self.cfg
        if not c.http_url:
            raise MailError("mail.http_url 未配置")
        url = c.http_url.replace("{addr}", self.address).replace("{address}", self.address)
        headers = {"Authorization": f"Bearer {c.http_token}"} if c.http_token else {}
        async with httpx.AsyncClient(timeout=20) as cli:
            r = await cli.get(url, headers=headers)
            if r.status_code != 200:
                return None
            m = CODE_RE.search(r.text)
            return m.group(1) if m else None

    # ------------------------------------------------------------------ mail.tm
    async def _mailtm_code(self) -> str | None:
        c = self.cfg
        async with httpx.AsyncClient(timeout=25) as cli:
            # 首次调用时创建账号（address 已在构造时定）
            pw = "Wl!" + random_local(12)
            await cli.post("https://api.mail.tm/accounts", json={"address": self.address, "password": pw})
            r = await cli.post("https://api.mail.tm/token", json={"address": self.address, "password": pw})
            if r.status_code != 200:
                return None
            tok = r.json()["token"]
            r = await cli.get(
                "https://api.mail.tm/messages",
                headers={"Authorization": f"Bearer {tok}"},
            )
            if r.status_code != 200:
                return None
            for msg in r.json().get("hydra:member", []):
                rid = msg["id"]
                d = await cli.get(f"https://api.mail.tm/messages/{rid}", headers={"Authorization": f"Bearer {tok}"})
                if d.status_code != 200:
                    continue
                text = d.text
                m = CODE_RE.search(text)
                if m:
                    return m.group(1)
        return None


def decode_header_value(v: str) -> str:
    out = []
    for part, enc in decode_header(v):
        out.append(part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
    return "".join(out)
