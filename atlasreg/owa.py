"""Outlook Web 接码：用 Playwright 持久化登录态读信（无需 OAuth、能过 MFA）。

为什么需要它：
  - 微软已封 MSA 密码流与 IMAP basic auth
  - MsaArtifacts 令牌绑定未公开 client，换不了 token
  - device code 要求人在 15 分钟内完成授权，容易超时
  → 让用户在一个真实浏览器窗口里登录一次（MFA 也能过），脚本保存 profile，
    之后所有读信都复用同一个会话。

读信方式：拦截 OWA 自家的 `service.svc` 响应（FindItem / GetItem / SyncState），
里面带完整 HTML 正文，直接提 6 位码；不依赖脆弱的 DOM 选择器。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

CODE_RE = re.compile(r"\b(\d{6})\b")

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
]


def chrome_path() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


class Owa:
    def __init__(self, profile_dir: str | Path):
        self.profile = str(profile_dir)
        Path(self.profile).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 内部
    def _launch(self, pw, headless: bool):
        kw: dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled", "--lang=en-US",
                     "--no-first-run", "--no-default-browser-check"],
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        exe = chrome_path()
        if exe:
            kw["executable_path"] = exe
        return pw.chromium.launch_persistent_context(self.profile, **kw)

    @staticmethod
    def logged_in(page) -> bool:
        u = page.url.lower()
        if "outlook.live.com" in u or "outlook.office.com" in u or "outlook.office365.com" in u:
            return "login" not in u and "oauth" not in u
        return False

    # ---------------------------------------------------------------- 登录
    def login(self, timeout: int = 300, headless: bool = False, log=print) -> dict:
        """打开可见窗口，等用户手动登录 outlook，成功后保存 profile。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            ctx = self._launch(pw, headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded", timeout=60000)
            log("浏览器已打开，请在窗口里登录 hotmail（需要验证码/身份验证器都可以过）")
            log("登录成功、能看到收件箱后，脚本会自动继续 ...")
            deadline = time.time() + timeout
            ok = False
            while time.time() < deadline:
                time.sleep(2)
                try:
                    if self.logged_in(page) and page.query_selector('[data-automationid="MessageListView"], div[role="main"]'):
                        time.sleep(2)
                        ok = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not ok:
                ctx.close()
                raise RuntimeError("等待登录超时（没检测到收件箱）")
            # 记录邮箱地址
            email = ""
            try:
                email = page.evaluate("""() => {
                    const m = document.body.innerText.match(/[\\w.+-]+@(hotmail|outlook|live)\\.com/i);
                    return m ? m[0] : '';
                }""")
            except Exception:  # noqa: BLE001
                pass
            log(f"✔ 登录成功 {email or '(地址未知)'}")
            ctx.close()
            return {"ok": True, "email": email, "profile": self.profile}

    # ---------------------------------------------------------------- 自动登录
    def auto_login(self, email: str, password: str, timeout: int = 120, log=print) -> dict:
        """用邮箱+密码自动登录并保存 profile（实测 hotmail 可直接过，无需 MFA）。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            ctx = self._launch(pw, False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://login.live.com/", wait_until="domcontentloaded", timeout=60000)
                time.sleep(2.0)

                if self.logged_in(page):
                    log("profile 已登录，跳过")
                    return {"ok": True, "email": email, "profile": self.profile, "reused": True}

                # 邮箱
                for sel in ('input[type=email]', 'input[name=loginfmt]', '#i0116'):
                    try:
                        page.fill(sel, email, timeout=4000)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                for nm in ("Next", "下一步"):
                    try:
                        page.get_by_role("button", name=nm).first.click(timeout=2500)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                time.sleep(3)

                # 密码
                for sel in ('input[type=password]', 'input[name=passwd]', '#i0118'):
                    try:
                        page.fill(sel, password, timeout=6000)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                for nm in ("Sign in", "登录", "Next"):
                    try:
                        page.get_by_role("button", name=nm).first.click(timeout=2500)
                        break
                    except Exception:  # noqa: BLE001
                        continue
                time.sleep(6)

                # 处理 "A quick note about your Microsoft account" / "Stay signed in?"
                for nm in ("OK", "Yes", "是", "好的"):
                    try:
                        page.get_by_role("button", name=nm).first.click(timeout=2000)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(2)

                if "outlook.live.com" not in page.url:
                    for _ in range(3):
                        try:
                            page.goto("https://outlook.live.com/mail/0/", wait_until="commit", timeout=45000)
                            break
                        except Exception:  # noqa: BLE001
                            time.sleep(2)
                time.sleep(3)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if self.logged_in(page) and page.query_selector('div[role="main"], [data-automationid="MessageListView"]'):
                        time.sleep(2)
                        log(f"✔ 登录成功：{email}")
                        return {"ok": True, "email": email, "profile": self.profile}
                    time.sleep(2)
                raise RuntimeError(f"自动登录未进入收件箱（当前 {page.url}）")
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- 读码
    def wait_code(self, address: str, since_ts: float | None = None,
                  timeout: int = 180, poll: float = 4.0, snapshot: bool = True,
                  latest: bool = False) -> str:
        from playwright.sync_api import sync_playwright

        addr = address.lower()
        base = addr.split("@")[0].split("+")[0]
        deadline = time.time() + timeout
        seen: set[str] = set()

        with sync_playwright() as pw:
            ctx = self._launch(pw, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            payloads: list[dict] = []

            def on_resp(resp):
                try:
                    u = resp.url
                    if "service.svc" not in u:
                        return
                    if not any(k in u for k in ("FindItem", "GetItem", "SyncState", "GetConversationItems")):
                        return
                    if resp.status != 200:
                        return
                    ctype = (resp.headers or {}).get("content-type", "")
                    if "json" not in ctype and "text" not in ctype:
                        return
                    payloads.append({"url": u, "text": resp.text()[:400_000]})
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", on_resp)
            try:
                if "outlook.live.com" not in page.url:
                    for _ in range(3):
                        try:
                            page.goto("https://outlook.live.com/mail/0/", wait_until="commit", timeout=45000)
                            break
                        except Exception:  # noqa: BLE001
                            time.sleep(2)
                time.sleep(3)
                if not self.logged_in(page):
                    raise RuntimeError("profile 未登录，先跑 reg.py ms-link 登录一次")

                # 先把"已有的码"记为已见，只认之后新到的；latest=True 则不过滤
                time.sleep(4)
                if snapshot:
                    for _, old in self._codes_from_dom(page):
                        seen.add(old)
                    if latest:
                        seen.clear()

                while time.time() < deadline:
                    # 1) DOM 列表里的验证码（最快，预览行就带码）
                    for row_text, code in self._codes_from_dom(page):
                        if code in seen:
                            continue
                        if self.CODE_HINT.search(row_text) or True:   # 列表已按时间排序，取最新
                            if since_ts is None or True:
                                seen.add(code)
                                return code
                    # 2) 列表预览没码 → 点开最新一封读正文
                    if time.time() + 6 < deadline:
                        code = self._open_and_read(page, 0)
                        if code and code not in seen:
                            return code
                    # 3) 兜底：从 service.svc 响应里挖（老版本 OWA）
                    for p in payloads:
                        code = self._scan(p["text"], addr, base, seen, since_ts)
                        if code:
                            return code
                    payloads.clear()

                    # 触发同步
                    try:
                        btn = page.query_selector('[data-automationid="RefreshButton"], button[title*="Refresh" i]')
                        if btn:
                            btn.click()
                        else:
                            page.reload()
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(poll)
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        raise RuntimeError(f"OWA 等码超时（{address}）")

    # ---------------------------------------------------------------- DOM 读码
    CODE_HINT = re.compile(r"verif|code|验证码|one[\s-]?time|sign[\s-]?in|登录|world\s*labs|clerk",
                           re.I)

    @classmethod
    def _codes_from_dom(cls, page) -> list[tuple[str, str]]:
        """从收件箱列表行里取 (行文本, 6位码)。"""
        out: list[tuple[str, str]] = []
        sels = ('[role="option"]', '[data-automationid="MessageListView"] [role="listitem"]',
                'div[role="listbox"] div[role="option"]')
        for sel in sels:
            try:
                rows = page.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                rows = []
            for r in rows[:15]:
                try:
                    t = r.inner_text() or ""
                except Exception:  # noqa: BLE001
                    continue
                m = CODE_RE.search(t)
                if m:
                    out.append((t.replace("\n", " | "), m.group(1)))
            if out:
                break
        return out

    @classmethod
    def _open_and_read(cls, page, row_index: int = 0) -> str:
        """点开列表第 N 行，读正文里的验证码。"""
        try:
            rows = page.query_selector_all('[role="option"]')
            if row_index < len(rows):
                rows[row_index].click()
                time.sleep(3)
        except Exception:  # noqa: BLE001
            pass
        for sel in ('[data-automationid="ReadingPane"]', 'div[role="main"]', '[aria-label*="Message body" i]'):
            try:
                el = page.query_selector(sel)
                if not el:
                    continue
                txt = el.inner_text() or ""
                m = CODE_RE.search(txt)
                if m:
                    return m.group(1)
            except Exception:  # noqa: BLE001
                continue
        return ""

    @staticmethod
    def _scan(text: str, addr: str, base: str, seen: set[str], since_ts: float | None) -> str | None:
        """在 OWA 的 JSON 响应里找目标收件人 + 验证码。"""
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = None

        def walk(node, depth=0):
            if depth > 12:
                return None
            if isinstance(node, dict):
                blob = " ".join(str(v) for v in node.values() if isinstance(v, (str, int)))
                low = blob.lower()
                hit_to = addr in low or base in low
                if hit_to:
                    for field in ("Body", "body", "Preview", "preview", "Subject", "subject",
                                  "UniqueBody", "NormalizedBody", "BodyPreview"):
                        val = node.get(field)
                        if isinstance(val, str):
                            m = CODE_RE.search(re.sub(r"<[^>]+>", " ", val))
                            if m:
                                key = node.get("ItemId", {}).get("Id") if isinstance(node.get("ItemId"), dict) else None
                                key = key or m.group(1) + blob[:40]
                                if key in seen:
                                    continue
                                seen.add(key)
                                return m.group(1)
                for v in node.values():
                    r = walk(v, depth + 1)
                    if r:
                        return r
            elif isinstance(node, list):
                for v in node[:40]:
                    r = walk(v, depth + 1)
                    if r:
                        return r
            return None

        return walk(data)
