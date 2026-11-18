"""浏览器内 SDK 模式 —— 实测唯一稳定能过 Turnstile 的路子。

为什么不用纯 HTTP：
  Clerk 服务端在 `POST /v1/client/sign_ups` 上对 captcha 的校验只认
  "页面上下文 + SDK 组装的请求"（实测带真实 token 的纯 HTTP 请求仍返回
  `captcha_missing_token`，见 RECON.md 第 3 节）。所以把浏览器当 transport，
  协议全交给 clerk-js：
    - 页面内渲染 Turnstile（action=signup）拿 token
    - Clerk.client.signUp.create({...})
    - signUp.prepareVerification / attemptVerification（email_code → phone_code）
    - signUp.createdSessionId → session.getToken() → Marble 开户
  Turnstile 在干净住宅 IP + 真实 Chrome 下多数时候自动 invisible 通过；
  需要人点时会同步阻塞等人工点一次（on_need_human 回调）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import random
import time
from pathlib import Path
from typing import Any, Callable

from .atlas import Marble
from .clerk import gen_password
from .config import Config
from .http import Http
from .mail import Mailbox
from .pipeline import FIRST_NAMES, LAST_NAMES
from .sms import Sms
from .store import Store

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# ---------------------------------------------------------------- 页面内脚本
RENDER_TURNSTILE = """
async ({sitekey, action, waitMs}) => {
  window.__TK = null; window.__TE = null;
  if (!window.turnstile) {
    await new Promise((res) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.onload = res; document.head.appendChild(s); setTimeout(res, 8000);
    });
  }
  if (!window.turnstile) return {err: 'api.js not loaded'};
  let el = document.getElementById('__ts_box');
  if (!el) {
    el = document.createElement('div');
    el.id = '__ts_box';
    el.style.cssText = 'position:fixed;bottom:16px;left:16px;z-index:2147483647;background:#fff;padding:10px;border-radius:8px';
    document.body.appendChild(el);
  }
  window.turnstile.render('#__ts_box', {
    sitekey, action,
    callback: (t) => { window.__TK = t; },
    'error-callback': (e) => { window.__TE = String(e); },
    'timeout-callback': () => { window.__TE = 'timeout'; },
  });
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 800));
    if (window.__TK) return {token: window.__TK};
    if (window.__TE) return {err: window.__TE};
  }
  return {err: 'still_challenging'};   // 需要人工点
}
"""

SDK_CREATE = """
async ({email, phone, password, captchaToken}) => {
  window.__R = null;
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  window.Clerk.client.signUp.create({
    emailAddress: email, phoneNumber: phone, password,
    captchaToken, captchaWidgetType: 'smart', timezone: tz, locale: 'en-US',
  }).then((su) => {
    window.__SU = su;
    window.__R = { ok: true, id: su.id, status: su.status,
      unverified: su.unverifiedFields, missing: su.missingFields,
      required: su.requiredFields, verifications: su.verifications,
      email: su.emailAddress, phone: su.phoneNumber };
  }).catch((e) => {
    window.__R = { ok: false, msg: String((e && e.message) || e),
      code: e && e.code, errors: e && e.errors };
  });
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 500));
    if (window.__R) return window.__R;
  }
  return { ok: false, msg: 'create_timeout' };
}
"""

SDK_PREPARE = """
async (strategy) => {
  try { await window.__SU.prepareVerification({ strategy }); return { ok: true }; }
  catch (e) { return { ok: false, msg: String((e && e.message) || e), code: e && e.code, errors: e && e.errors }; }
}
"""

SDK_ATTEMPT = """
async ({code, strategy}) => {
  try {
    const su = await window.__SU.attemptVerification({ code, strategy });
    window.__SU = su;
    return { ok: true, status: su.status, unverified: su.unverifiedFields,
             missing: su.missingFields, sessionId: su.createdSessionId };
  } catch (e) {
    return { ok: false, msg: String((e && e.message) || e), code: e && e.code, errors: e && e.errors };
  }
}
"""

SDK_SET_ACTIVE = """
async () => {
  try {
    const sid = window.__SU.createdSessionId;
    if (!sid) return { ok: false, msg: 'no session id' };
    await window.Clerk.setActive({ session: sid });
    const jwt = await window.Clerk.session.getToken();
    return { ok: true, sessionId: sid, jwt };
  } catch (e) { return { ok: false, msg: String((e && e.message) || e) }; }
}
"""


def _sync(coro):
    """Playwright sync API 占用当前线程事件循环，协程要在独立线程跑。"""
    if not asyncio.iscoroutine(coro) and not isinstance(coro, asyncio.Future):
        return coro
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


def chrome_path() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def run_sdk_signup(
    cfg: Config,
    store: Store,
    proxy: str | None = None,
    headless: bool = False,
    profile_dir: str | None = None,
    on_need_human: Callable[[Any], None] | None = None,
    turnstile_wait: int = 60_000,
    human_wait: int = 180_000,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    email_box = Mailbox(cfg.mail)
    password = cfg.run.password or gen_password()
    first, last = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
    display = f"{first} {last}"

    row_id = store.insert(email=email_box.address, password=password, status="running",
                          stage="init", proxy=proxy)
    exe = chrome_path()
    sms = Sms(cfg.sms)
    rent = None

    with sync_playwright() as pw:
        kw: dict[str, Any] = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled", "--lang=en-US"],
        }
        if exe:
            kw["executable_path"] = exe
        if proxy:
            kw["proxy"] = {"server": proxy}

        if profile_dir:
            ctx = pw.chromium.launch_persistent_context(profile_dir, viewport={"width": 1280, "height": 900},
                                                        locale="en-US", **kw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            browser = None
        else:
            browser = pw.chromium.launch(**kw)
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            page = ctx.new_page()

        try:
            # ---------- 0. 打开站点，Clerk 就绪 ----------
            store.update(row_id, stage="page_load")
            page.goto(f"{cfg.atlas.app_url}/sign-in", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_function("window.Clerk && window.Clerk.loaded", timeout=45000)
            time.sleep(2.5)

            # ---------- 1. Turnstile ----------
            store.update(row_id, stage="turnstile")
            tk = page.evaluate(RENDER_TURNSTILE, {"sitekey": cfg.captcha.sitekey,
                                                  "action": cfg.captcha.action,
                                                  "waitMs": turnstile_wait})
            if not tk.get("token"):
                # 让人点一下
                if on_need_human:
                    on_need_human(page)
                tk = page.evaluate(RENDER_TURNSTILE, {"sitekey": cfg.captcha.sitekey,
                                                      "action": cfg.captcha.action,
                                                      "waitMs": human_wait})
            if not tk.get("token"):
                raise RuntimeError(f"Turnstile 未通过: {tk.get('err')}")
            token = tk["token"]

            # ---------- 2. 手机号 ----------
            store.update(row_id, stage="sms_rent")
            rent = _sync(sms.rent())
            store.update(row_id, phone=rent.phone)

            # ---------- 3. SDK 创建 ----------
            store.update(row_id, stage="clerk_create")
            r = page.evaluate(SDK_CREATE, {"email": email_box.address, "phone": rent.phone,
                                          "password": password, "captchaToken": token})
            if not r.get("ok"):
                raise RuntimeError(f"signUp.create: {r.get('code') or r.get('msg')} {json.dumps(r.get('errors') or [])[:300]}")

            # ---------- 4. email_code ----------
            store.update(row_id, stage="email_code")
            p = page.evaluate(SDK_PREPARE, "email_code")
            if not p.get("ok"):
                raise RuntimeError(f"prepare email_code: {p.get('code') or p.get('msg')}")
            code = _sync(email_box.wait_code())
            r = page.evaluate(SDK_ATTEMPT, {"code": code, "strategy": "email_code"})
            if not r.get("ok"):
                raise RuntimeError(f"email_code verify: {r.get('code') or r.get('msg')}")
            if r.get("status") != "complete" and "phone_number" in (r.get("unverified") or []):
                # ---------- 5. phone_code ----------
                store.update(row_id, stage="phone_code")
                p = page.evaluate(SDK_PREPARE, "phone_code")
                if not p.get("ok"):
                    raise RuntimeError(f"prepare phone_code: {p.get('code') or p.get('msg')}")
                sms_code = _sync(sms.wait_code(rent))
                r = page.evaluate(SDK_ATTEMPT, {"code": sms_code, "strategy": "phone_code"})
                if not r.get("ok"):
                    raise RuntimeError(f"phone_code verify: {r.get('code') or r.get('msg')}")

            if r.get("status") != "complete":
                raise RuntimeError(f"sign_up 未完成: status={r.get('status')} unverified={r.get('unverified')}")

            # ---------- 6. session token ----------
            store.update(row_id, stage="session")
            act = page.evaluate(SDK_SET_ACTIVE)
            if not act.get("ok"):
                raise RuntimeError(f"setActive: {act.get('msg')}")
            jwt = act["jwt"]
            cookies = ctx.cookies()
            store.update(row_id, clerk_sign_up_id=page.evaluate("window.__SU.id"), jwt=jwt,
                         clerk_session_id=act["sessionId"], cookies=cookies, status="clerk_ok")

            # ---------- 7. Marble 开户 ----------
            store.update(row_id, stage="marble_signup")

            async def marble_flow() -> dict[str, Any]:
                async with Http(cfg.run.user_agent, proxy=None) as http:
                    m = Marble(cfg.atlas, http, jwt)
                    su = await m.sign_up_user(email=email_box.address, display_name=display)
                    me = await m.me()
                    acc = await m.first_account()
                    credits = await m.credits_total(acc) if acc else None
                    return {"marble": su, "me": me, "account": acc, "credits": credits}

            out = _sync(marble_flow())
            uid = None
            if isinstance(out.get("me"), dict):
                uid = out["me"].get("id") or (out["me"].get("user") or {}).get("id")
            store.update(row_id, clerk_user_id=str(uid) if uid else None, account_id=out.get("account"),
                         credits=out.get("credits"), status="ok", stage="done",
                         raw=json.dumps(out, ensure_ascii=False))
            _sync(sms.done(rent))
            return {"ok": True, "email": email_box.address, "phone": rent.phone, "password": password,
                    "jwt": jwt, **out}

        except Exception as e:  # noqa: BLE001
            stage = store.conn.execute("SELECT stage FROM accounts WHERE id=?", (row_id,)).fetchone()
            store.update(row_id, status="failed", error=f"{type(e).__name__}: {e}")
            if rent is not None:
                _sync(sms.done(rent))
            raise
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
