"""Clerk 会话：用浏览器导出的 cookie 纯 HTTP 刷新 JWT（60s 一续），无需常开浏览器。

用法：
    cs = ClerkSession("clerk_session.json")
    cs.ensure()                    # 从 profile 导出 cookie（首次）
    jwt = cs.jwt()                 # 自动刷新，缓存到过期前
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

FAPI = "https://clerk.worldlabs.ai"
ORIGIN = "https://atlas.worldlabs.ai"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class ClerkSession:
    def __init__(self, path: str | Path = "clerk_session.json"):
        self.path = Path(path)
        self.cookies: dict[str, str] = {}
        self.sid: str = ""
        self._jwt = ""
        self._exp = 0.0
        if self.path.exists():
            self._load()

    # ---------------------------------------------------------------- 存取
    def _load(self) -> None:
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.cookies = d.get("cookies", {})
        self.sid = d.get("sid", "")

    def save(self) -> None:
        self.path.write_text(json.dumps(
            {"cookies": self.cookies, "sid": self.sid, "savedAt": time.time()},
            ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------------- 从浏览器导出
    def ensure(self, profile: str | None = None, force: bool = False) -> bool:
        if self.cookies and self.sid and not force:
            return True
        profile = profile or str(Path("ms_profile").absolute())
        from playwright.sync_api import sync_playwright
        engine = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        exe = next((p for p in engine if Path(p).exists()), None)
        with sync_playwright() as pw:
            kw = {"headless": False, "viewport": {"width": 1000, "height": 700}}
            if exe:
                kw["executablePath"] = exe
            try:
                ctx = pw.chromium.launch_persistent_context(profile, **kw)
            except Exception:  # noqa: BLE001
                return False
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(f"{ORIGIN}/", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_function("window.Clerk && window.Clerk.loaded", timeout=45000)
                time.sleep(2)
                sid = page.evaluate("()=>window.Clerk.session && window.Clerk.session.id")
                if not sid:
                    return False
                self.sid = sid
                want = ("__client", "__client_uat", "__session",
                        "__client_uat_X5Nb4K6s", "__session_X5Nb4K6s")
                self.cookies = {c["name"]: c["value"] for c in ctx.cookies()
                                if c["name"].startswith("__")}
                self.save()
                return True
            except Exception:  # noqa: BLE001
                return False
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- 刷新
    def jwt(self) -> str:
        if self._jwt and time.time() < self._exp - 5:
            return self._jwt
        if not self.sid:
            raise RuntimeError("没有 Clerk 会话，先 ensure()")
        r = httpx.post(
            f"{FAPI}/v1/client/sessions/{self.sid}/tokens?_clerk_js_version=6.43.0",
            cookies=self.cookies,
            headers={"Origin": ORIGIN, "Referer": f"{ORIGIN}/", "User-Agent": UA},
            timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"刷新 JWT 失败 {r.status_code}: {r.text[:200]}")
        d = r.json()
        self._jwt = d["jwt"]
        self._exp = time.time() + 50      # Clerk token 60s，留 10s 余量
        # 服务端可能轮换 cookie
        for c in r.cookies.jar:
            self.cookies[c.name] = c.value
        self.save()
        return self._jwt

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt()}",
                "accept": "application/json", "content-type": "application/json"}
