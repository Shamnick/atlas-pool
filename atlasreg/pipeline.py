"""单个账号的完整流水线：邮箱 → 手机 → Turnstile → Clerk → Marble 开户 → 校验 10000。"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from .atlas import Marble, MarbleError
from .captcha import CaptchaError, Solver
from .clerk import Clerk, ClerkError, gen_password
from .config import Config
from .http import Http
from .mail import MailError, Mailbox
from .sms import Sms, SmsError
from .store import Store

FIRST_NAMES = ["Alex", "Jordan", "Casey", "Riley", "Morgan", "Taylor", "Jamie", "Avery", "Quinn", "Rowan"]
LAST_NAMES = ["Lee", "Miller", "Chen", "Davis", "Garcia", "Kim", "Patel", "Novak", "Silva", "Brown"]


class Stage:
    CAPTCHA = "captcha"
    CLERK = "clerk"
    EMAIL = "email_code"
    PHONE = "phone_code"
    MARBLE = "marble_signup"
    VERIFY = "verify_credits"


class Worker:
    def __init__(self, cfg: Config, store: Store, proxy: str | None = None, verbose: bool = True,
                 on_stage=None, on_log=None):
        self.cfg = cfg
        self.store = store
        self.proxy = proxy
        self.verbose = verbose
        self.on_stage = on_stage
        self.on_log = on_log

    def log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)
        if self.verbose:
            print(msg, flush=True)

    def _stage(self, stage: str) -> None:
        if self.on_stage:
            self.on_stage(stage)

    async def run_one(self) -> dict[str, Any]:
        cfg = self.cfg
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        display = f"{first} {last}"
        password = cfg.run.password or gen_password()
        mailbox = Mailbox(cfg.mail)
        row_id = self.store.insert(
            email=mailbox.address, password=password, status="running", stage="init", proxy=self.proxy
        )
        state: dict[str, Any] = {"row_id": row_id, "email": mailbox.address, "proxy": self.proxy}
        stage = "init"
        try:
            async with Http(cfg.run.user_agent, proxy=self.proxy, origin=cfg.atlas.app_url) as http:
                # ---------------- 1. Turnstile ----------------
                stage = Stage.CAPTCHA
                self._stage('captcha')
                solver = Solver(cfg.captcha)
                self.log(f"[1/6] {mailbox.address} 求 Turnstile token ...")
                token = await solver.solve()
                fields = Solver.token_fields(token, cfg.captcha.field_style)
                self.log(f"      token ok ({len(token)} chars)")

                # ---------------- 2. 手机号 ----------------
                stage = "sms_rent"
                self._stage("sms_rent")
                sms = Sms(cfg.sms)
                self.log("[2/6] 租手机号 ...")
                rent = await sms.rent()
                state["phone"] = rent.phone
                self.store.update(row_id, phone=rent.phone)
                self.log(f"      {rent.phone}")

                # ---------------- 3. Clerk 注册 ----------------
                stage = Stage.CLERK
                self._stage('clerk')
                clerk = Clerk(cfg.atlas, http)
                sent_at = time.time()

                async def get_email_code() -> str:
                    return await mailbox.wait_code(since_ts=sent_at)

                async def get_sms_code() -> str:
                    return await sms.wait_code(rent)

                self.log("[3/6] Clerk sign_up ...")
                res = await clerk.full_sign_up(
                    email=mailbox.address,
                    phone=rent.phone,
                    password=password,
                    captcha_fields=fields,
                    get_email_code=get_email_code,
                    get_sms_code=get_sms_code,
                    log=self.log,
                )
                state["clerk_sign_up_id"] = clerk.sign_up_id
                state["clerk_session_id"] = res["session_id"]
                self.store.update(
                    row_id,
                    clerk_sign_up_id=clerk.sign_up_id,
                    clerk_session_id=res["session_id"],
                    cookies=http.cookies,
                    raw={"sign_up": res["sign_up"]},
                )

                # ---------------- 4. 会话 token ----------------
                stage = "session_token"
                self._stage("session_token")
                self.log("[4/6] 取 session token ...")
                jwt = await clerk.session_token(res["session_id"])
                state["jwt"] = jwt
                self.store.update(row_id, jwt=jwt)
                self.log(f"      jwt {jwt[:24]}...")

                # ---------------- 5. Marble 开户（发 10000 积分）----------------
                stage = Stage.MARBLE
                self._stage('marble')
                marble = Marble(cfg.atlas, http, jwt)
                self.log("[5/6] /api/v2/users:signUp 开户 ...")
                su = await marble.sign_up_user(email=mailbox.address, display_name=display)
                me = await marble.me()
                account = await marble.first_account()
                uid = None
                if isinstance(me, dict):
                    uid = me.get("id") or me.get("userId") or (me.get("user") or {}).get("id")
                self.log(f"      account={account} user={uid}")

                # ---------------- 6. 校验积分 ----------------
                stage = Stage.VERIFY
                self._stage('verify')
                credits = None
                if account:
                    credits = await marble.credits_total(account)
                state.update(
                    clerk_user_id=str(uid) if uid else None,
                    account_id=account,
                    credits=credits,
                    marble=su,
                )
                self.log(f"[6/6] 积分 = {credits}")

                self.store.update(
                    row_id,
                    clerk_user_id=str(uid) if uid else None,
                    account_id=account,
                    credits=credits,
                    status="ok",
                    stage="done",
                    cookies=http.cookies,
                    raw={"sign_up": res["sign_up"], "marble": su, "me": me},
                )
                await sms.done(rent)
                return {"ok": True, **state}

        except (CaptchaError, MailError, SmsError, ClerkError, MarbleError) as e:
            self.store.update(row_id, status="failed", stage=stage, error=f"{type(e).__name__}: {e}")
            self.log(f"  !! 失败于 {stage}: {type(e).__name__}: {e}")
            return {"ok": False, "stage": stage, "error": str(e), **state}
        except Exception as e:  # noqa: BLE001
            self.store.update(row_id, status="failed", stage=stage, error=f"{type(e).__name__}: {e}")
            self.log(f"  !! 异常于 {stage}: {type(e).__name__}: {e}")
            return {"ok": False, "stage": stage, "error": str(e), **state}


async def run_batch(cfg: Config, n: int, store: Store, verbose: bool = True) -> list[dict]:
    proxies = list(cfg.run.proxies)
    sem = asyncio.Semaphore(max(1, cfg.run.concurrency))
    results: list[dict] = []

    async def one(i: int) -> None:
        proxy = proxies[i % len(proxies)] if proxies else None
        async with sem:
            w = Worker(cfg, store, proxy=proxy, verbose=verbose)
            r = await w.run_one()
            r["index"] = i
            results.append(r)
            await asyncio.sleep(cfg.run.per_ip_delay)

    await asyncio.gather(*(one(i) for i in range(n)))
    return results
