"""单账号端到端编排：登录邮箱 → 注册 → 验证 → 签发 WLT-Api-Key → 入池。

供 webapp 的批量队列调用。每一步都落状态回调，面板能看到实时阶段。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from . import config as cfgmod
from .captcha import Solver
from .clerk import gen_password
from .owa import Owa
from .sms import Sms

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Library/Caches/ms-playwright/chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    "/usr/bin/google-chrome",
]
ATLAS = "https://atlas.worldlabs.ai"

RENDER_TS = """
async ({sitekey, action, waitMs}) => {
  window.__TK=null; window.__TE=null;
  if(!window.turnstile){ await new Promise(r=>{const s=document.createElement('script');
    s.src='https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    s.onload=r; document.head.appendChild(s); setTimeout(r,8000);}); }
  document.querySelectorAll('#__ts').forEach(e=>e.remove());
  const el=document.createElement('div'); el.id='__ts';
  el.style.cssText='position:fixed;bottom:20px;left:20px;z-index:2147483647;background:#fff;padding:10px;border-radius:8px';
  document.body.appendChild(el);
  window.turnstile.render('#__ts',{sitekey,action,callback:t=>{window.__TK=t;},'error-callback':e=>{window.__TE=String(e);}});
  const dl=Date.now()+waitMs;
  while(Date.now()<dl){ await new Promise(r=>setTimeout(r,600)); if(window.__TK) return {token:window.__TK}; if(window.__TE) return {err:window.__TE}; }
  return {err:'challenging'};
}
"""


def chrome_path() -> str | None:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _sync(coro):
    if not asyncio.iscoroutine(coro) and not isinstance(coro, asyncio.Future):
        return coro
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Stage:
    LOGIN = "login"
    CAPTCHA = "captcha"
    CREATE = "create"
    EMAIL = "email_code"
    PHONE = "phone_code"
    SESSION = "session"
    APIKEY = "apikey"
    POOL = "pool"


def onboard_one(
    email: str,
    password: str,
    cfg: cfgmod.Config,
    *,
    pool_url: str = "http://127.0.0.1:8790",
    profile_dir: str | None = None,
    on_stage: Callable[[str], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    timeout_email: int = 240,
    timeout_sms: int = 300,
) -> dict[str, Any]:
    """跑完一个账号。返回 {ok, email, wlt_key, account_id, project_id, credits, jwt}。"""
    from playwright.sync_api import sync_playwright

    say = on_log or (lambda m: None)
    stage = (lambda s: on_stage and on_stage(s))

    profile = profile_dir or f"ms_profile_{email.split('@')[0]}"
    proxy = cfg.run.proxies[0] if cfg.run.proxies else None
    exe = chrome_path()

    # ---------- 1. 邮箱登录态 ----------
    stage(Stage.LOGIN)
    owa = Owa(profile)
    try:
        r = owa.auto_login(email, password, timeout=90, log=say)
        say(f"邮箱登录 ok: {r.get('email') or email}")
    except Exception as e:  # noqa: BLE001
        say(f"邮箱登录失败（沿用已有 profile）: {str(e)[:120]}")

    jwt = None
    try:
        with sync_playwright() as pw:
            kw: dict[str, Any] = {
                "headless": False,
                "args": ["--disable-blink-features=AutomationControlled", "--lang=en-US"],
            }
            if exe:
                kw["executable_path"] = exe
            if proxy:
                kw["proxy"] = {"server": proxy}
            ctx = pw.chromium.launch_persistent_context(
                str(Path(profile).absolute()), viewport={"width": 1200, "height": 860},
                locale="en-US", **kw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def go(path: str = "/sign-in"):
                for _ in range(3):
                    try:
                        page.goto(f"{ATLAS}{path}", wait_until="domcontentloaded", timeout=60000)
                        return True
                    except Exception:  # noqa: BLE001
                        time.sleep(3)
                return False

            go()
            page.wait_for_function("window.Clerk && window.Clerk.loaded", timeout=45000)
            time.sleep(2)

            # ---------- 2. Turnstile ----------
            stage(Stage.CAPTCHA)
            token = None
            for rnd in range(4):
                t = page.evaluate(RENDER_TS, {"sitekey": cfg.captcha.sitekey,
                                              "action": cfg.captcha.action, "waitMs": 12000})
                if t.get("token"):
                    token = t["token"]
                    break
                cf = next((f for f in page.frames if "challenges.cloudflare" in f.url), None)
                if cf:
                    for _ in range(3):
                        try:
                            cf.locator("body").click(position={"x": 30, "y": 32}, timeout=4000)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            page.mouse.click(50, 845)
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(6)
                        tk = page.evaluate("()=>window.__TK")
                        if tk:
                            token = tk
                            break
                    if token:
                        break
                say(f"Turnstile 第 {rnd + 1} 轮未过: {t.get('err')}")
            if not token:
                raise RuntimeError("Turnstile 未通过（换住宅 IP 或配置打码 key）")
            say(f"Turnstile ok ({len(token)} chars)")

            # ---------- 3. 手机号 ----------
            sms = Sms(cfg.sms)
            rent = _sync(sms.rent())
            say(f"接码号 {rent.phone}")

            # ---------- 4. 创建 ----------
            stage(Stage.CREATE)
            res = page.evaluate("""async ({email,phone,pw,tok})=>{
              try{
                const su=await window.Clerk.client.signUp.create({emailAddress:email,phoneNumber:phone,
                  password:pw,captchaToken:tok,captchaWidgetType:'smart',timezone:'America/New_York'});
                window.__SU=su;
                return {ok:true,id:su.id,status:su.status,unverified:su.unverifiedFields,missing:su.missingFields};
              }catch(e){return {ok:false,code:e&&e.code,msg:String(e&&(e.message||e)),errors:e&&e.errors};}
            }""", {"email": email, "phone": rent.phone, "pw": password, "tok": token})
            if not res.get("ok"):
                raise RuntimeError(f"signUp.create: {res.get('code') or res.get('msg')}")
            say(f"sign_up {res.get('id')} → {res.get('status')}")

            # ---------- 5. 邮箱码 ----------
            stage(Stage.EMAIL)
            page.evaluate("""async()=>{ try{ await window.__SU.prepareVerification({strategy:'email_code'}); }catch(e){} }""")
            code = owa.wait_code(email, timeout=timeout_email, snapshot=False, latest=True)
            say(f"邮箱码 {code}")
            v = page.evaluate("""async (c)=>{ try{ const s=await window.__SU.attemptVerification({strategy:'email_code',code:c});
                window.__SU=s; return {ok:true,status:s.status,unverified:s.unverifiedFields};}
                catch(e){return {ok:false,code:e&&e.code,msg:String(e&&e.message||e)};} }""", code)
            if not v.get("ok"):
                raise RuntimeError(f"email_code: {v.get('code') or v.get('msg')}")

            # ---------- 6. 短信码 ----------
            if (v.get("unverified") or []) and "phone_number" in v["unverified"]:
                stage(Stage.PHONE)
                page.evaluate("""async()=>{ try{ await window.__SU.prepareVerification({strategy:'phone_code'}); }catch(e){} }""")
                scode = _sync(sms.wait_code(rent, timeout_sms))
                say(f"短信码 {scode}")
                v2 = page.evaluate("""async (c)=>{ try{ const s=await window.__SU.attemptVerification({strategy:'phone_code',code:c});
                    window.__SU=s; return {ok:true,status:s.status,sessionId:s.createdSessionId};}
                    catch(e){return {ok:false,code:e&&e.code,msg:String(e&&e.message||e)};} }""", scode)
                if not v2.get("ok"):
                    raise RuntimeError(f"phone_code: {v2.get('code') or v2.get('msg')}")

            # ---------- 7. session ----------
            stage(Stage.SESSION)
            act = page.evaluate("""async ()=>{ try{ const s=window.__SU;
                if(s.createdSessionId) await window.Clerk.setActive({session:s.createdSessionId});
                const t=await window.Clerk.session.getToken();
                return {ok:true,jwt:t,sid:window.Clerk.session.id};}
                catch(e){return {ok:false,msg:String(e&&e.message||e)};} }""")
            if not act.get("ok"):
                raise RuntimeError(f"session: {act.get('msg')}")
            jwt = act["jwt"]
            say("session token ok")
            ctx.close()
    finally:
        try:
            _sync(sms.done(rent))  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            pass

    # ---------- 8. 开户 + 签 API Key + 入池 ----------
    stage(Stage.APIKEY)
    out = _sync(_provision(cfg, jwt, email, pool_url, say))
    return {"ok": True, "email": email, **out}


async def _provision(cfg: cfgmod.Config, jwt: str, email: str, pool_url: str,
                     say: Callable[[str], None]) -> dict[str, Any]:
    proxy = cfg.run.proxies[0] if cfg.run.proxies else None
    h = {"Authorization": f"Bearer {jwt}", "accept": "application/json",
         "content-type": "application/json"}
    async with httpx.AsyncClient(proxy=proxy, timeout=60) as c:
        r = await c.get(f"{ATLAS}/api/v2/accounts", headers=h)
        r.raise_for_status()
        acc = r.json()["accounts"][0]
        acct = acc["name"].split("/")[-1]
        proj = acc["defaultProject"].split("/")[-1]
        say(f"account {acct[:16]}…")

        # 开户（幂等；409 视为已存在）
        try:
            await c.post(f"{ATLAS}/api/v2/users:signUp", headers=h, json={
                "displayName": "Atlas User",
                "accountDisplayName": f"Atlas {email.split('@')[0][:12]}",
                "defaultProjectDisplayName": "My First World",
                "email": email,
                "residencyRegion": cfg.atlas.residency_region or "US_CANADA",
            })
        except Exception:  # noqa: BLE001
            pass

        # 签 API Key
        r = await c.post(f"{ATLAS}/api/v2/accounts/{acct}/projects/{proj}/apiKeys", headers=h, json={
            "apiKey": {"displayName": "pool-gateway",
                       "scopes": ["tasks.create", "operations.read", "operations.cancel",
                                  "assets.read", "assets.create"]}})
        if r.status_code >= 300:
            raise RuntimeError(f"apiKeys: {r.status_code} {r.text[:200]}")
        wlt = r.json()["secret"]
        say(f"WLT key {wlt[:14]}…")

        # 读余额
        credits = None
        try:
            rr = await c.post(
                f"{ATLAS}/api/v2/accounts/{acct}/billing/developerApi:getPrepaidBalance",
                headers=h, json={})
            if rr.status_code < 300:
                credits = rr.json().get("totalCredits")
        except Exception:  # noqa: BLE001
            pass

    # 入池
    stage_ok = True
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            rr = await c.post(f"{pool_url.rstrip('/')}/admin/accounts",
                              json={"email": email, "wlt_key": wlt, "account_id": acct,
                                    "project_id": proj, "credits": credits or 0})
        stage_ok = rr.status_code < 300
        say(f"已入池 {pool_url} ({rr.status_code})" if stage_ok else f"入池失败 {rr.status_code}")
    except Exception as e:  # noqa: BLE001
        stage_ok = False
        say(f"入池异常: {str(e)[:100]}  —— key 已拿到，可手工添加")

    return {"wlt_key": wlt, "account_id": acct, "project_id": proj,
            "credits": credits, "jwt": jwt, "pooled": stage_ok}
