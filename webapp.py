#!/usr/bin/env python3
"""atlas.worldlabs.ai 可视化注册机 —— 本地 Web 控制台。

启动：
    ./.venv/bin/python webapp.py            # 默认 http://127.0.0.1:8765
    ./.venv/bin/python webapp.py --port 9000

两种模式：
    Demo  —— 不花一分钱，模拟 6 个注册阶段，用来验证面板/流程（不写账号库）
    Live  —— 真跑：Clerk 注册 → 邮箱码 → 短信码 → 开户 → 10000 积分
             可选「浏览器 SDK 模式」（Turnstile 能自动过，无需打码 key）
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import random
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
import uvicorn
import yaml

from atlasreg import config as cfgmod
from atlasreg.pipeline import Worker
from atlasreg.store import Store

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.yaml"

# ---------------------------------------------------------------- 阶段定义
STAGES = [
    ("captcha", "Turnstile 求解", 8),
    ("sms_rent", "租用手机号", 14),
    ("clerk", "Clerk 注册", 26),
    ("email_code", "邮箱验证码", 20),
    ("phone_code", "短信验证码", 18),
    ("marble", "开户发积分", 8),
    ("verify", "校验积分", 6),
]
STAGE_ORDER = [s[0] for s in STAGES]
STAGE_LABEL = {s[0]: s[1] for s in STAGES}
STAGE_LABEL.update({
    "done": "完成", "login": "登录邮箱", "apikey": "签发 API Key", "pool": "入池",
    "queued": "排队",
})


class Task:
    def __init__(self, idx: int, mode: str):
        self.id = uuid.uuid4().hex[:8]
        self.idx = idx
        self.mode = mode
        self.email = ""
        self.pw = ""
        self.phone = ""
        self.stage = "queued"
        self.progress = 0.0
        self.status = "queued"     # queued | running | ok | failed | stopped
        self.credits: float | None = None
        self.error = ""
        self.started = time.time()
        self.ended: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "idx": self.idx,
            "mode": self.mode,
            "email": self.email,
            "phone": self.phone,
            "stage": self.stage,
            "stage_label": STAGE_LABEL.get(self.stage, "排队中" if self.stage == "queued" else self.stage),
            "progress": round(self.progress, 1),
            "status": self.status,
            "credits": self.credits,
            "error": self.error,
            "elapsed": 0 if self.status == "queued" else round((self.ended or time.time()) - self.started, 1),
        }


class App:
    def __init__(self) -> None:
        self.tasks: list[Task] = []
        self.logs: list[dict[str, Any]] = []
        self.running = False
        self.mode = "demo"
        self.batch: list[asyncio.Task] = []
        self.store: Store | None = None
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.live_workers = 0

    # ------------------------------------------------------------ 日志
    def log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        if len(self.logs) > 1200:
            del self.logs[:400]

    # ------------------------------------------------------------ 状态
    def stats(self) -> dict[str, Any]:
        ok = [t for t in self.tasks if t.status == "ok"]
        failed = [t for t in self.tasks if t.status == "failed"]
        running = [t for t in self.tasks if t.status == "running"]
        credits = sum(t.credits or 0 for t in ok)
        return {
            "total": len(self.tasks),
            "ok": len(ok),
            "failed": len(failed),
            "running": len(running),
            "queued": len([t for t in self.tasks if t.status == "queued"]),
            "credits": credits,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "running_batch": self.running,
            "mode": self.mode,
        }

    def state(self) -> dict[str, Any]:
        return {"stats": self.stats(), "tasks": [t.to_dict() for t in self.tasks][-200:],
                "logs": self.logs[-300:]}


APP = App()


# ---------------------------------------------------------------- Demo 任务
async def run_demo_task(t: Task, concurrency: int) -> None:
    t.status = "running"
    t.email = f"atlas{random.randint(10000, 99999)}@demo.local"
    t.phone = "+1 (2%02d) %03d-%04d" % (random.randint(10, 99), random.randint(200, 999), random.randint(1000, 9999))
    APP.log(f"[demo] {t.email} 开始注册（并发 {concurrency}）")
    base = 0.0
    for stage, label, weight in STAGES:
        if not APP.running:
            t.status = "stopped"
            return
        t.stage = stage
        for i in range(10):
            await asyncio.sleep(random.uniform(0.05, 0.22))
            t.progress = base + weight * (i + 1) / 10
            if not APP.running:
                t.status = "stopped"
                return
        base += weight
        # 真实感：小概率卡在某个阶段
        fail_rate = {"captcha": 0.03, "sms_rent": 0.05, "clerk": 0.06,
                     "email_code": 0.02, "phone_code": 0.06, "marble": 0.02}.get(stage, 0.0)
        if random.random() < fail_rate:
            t.status = "failed"
            t.stage = stage
            t.error = {
                "captcha": "Turnstile 600010",
                "sms_rent": "接码池无号",
                "clerk": "form_email_address_blocked",
                "email_code": "验证码超时",
                "phone_code": "phone_number_verification_failed",
                "marble": "marble http 500",
            }.get(stage, "unknown")
            t.ended = time.time()
            APP.log(f"[demo] {t.email} 失败 @ {label}: {t.error}", "error")
            return
    t.credits = 10000
    t.progress = 100
    t.status = "ok"
    t.stage = "done"
    t.ended = time.time()
    APP.log(f"[demo] {t.email} 完成，+10000 积分（累计 {APP.stats()['credits']:.0f}）", "ok")


# ---------------------------------------------------------------- Live 任务
def _load_cfg():
    return cfgmod.load(CFG_PATH)


async def run_live_task(t: Task, cfg) -> None:
    t.status = "running"
    loop = asyncio.get_running_loop()

    def on_stage(stage: str) -> None:
        t.stage = stage
        try:
            i = STAGE_ORDER.index(stage)
            t.progress = sum(s[2] for s in STAGES[:i])
        except ValueError:
            pass

    def on_log(msg: str) -> None:
        APP.log(f"[{t.id}] {msg}")
        m = re.search(r"([\w.+-]+@[\w.-]+)", msg)
        if m and not t.email:
            t.email = m.group(1)
        m = re.search(r"\+?\d{8,15}", msg)
        if m and not t.phone:
            t.phone = m.group(0)

    worker = Worker(cfg, APP.store, verbose=False, on_stage=on_stage, on_log=on_log)
    try:
        # run_one 是 async，直接 await
        res = await worker.run_one()
        t.email = res.get("email") or t.email
        t.phone = res.get("phone") or t.phone
        if res.get("ok"):
            t.credits = float(res.get("credits") or 0)
            t.status = "ok"
            t.stage = "done"
            t.progress = 100
            APP.log(f"[{t.id}] 成功 {t.email} 积分 {t.credits:.0f}", "ok")
        else:
            t.status = "failed"
            t.stage = res.get("stage") or t.stage
            t.error = res.get("error") or "unknown"
            APP.log(f"[{t.id}] 失败 {t.email}: {t.error}", "error")
    except Exception as e:  # noqa: BLE001
        t.status = "failed"
        t.error = f"{type(e).__name__}: {e}"
        APP.log(f"[{t.id}] 异常: {t.error}", "error")
    finally:
        t.ended = time.time()


async def run_onboard_task(t: Task, cfg, email: str, password: str, pool_url: str) -> None:
    """真跑：登录邮箱 → 注册 → 验证 → 签 Key → 入池。"""
    from atlasreg.onboard import onboard_one

    t.status = "running"
    t.email = email
    loop = asyncio.get_running_loop()

    def on_stage(st: str) -> None:
        t.stage = st
        try:
            i = STAGE_ORDER.index(st)
            t.progress = sum(x[2] for x in STAGES[:i])
        except ValueError:
            t.progress = max(t.progress, 10)

    def on_log(msg: str) -> None:
        APP.log(f"[{t.id}] {msg}")

    try:
        res = await loop.run_in_executor(
            None, lambda: onboard_one(email, password, cfg, pool_url=pool_url,
                                      on_stage=on_stage, on_log=on_log))
        t.credits = float(res.get("credits") or 0)
        t.status = "ok"
        t.stage = "done"
        t.progress = 100
        APP.log(f"[{t.id}] ✔ {email} 完成，积分 {t.credits:.0f}，已入池", "ok")
    except Exception as e:  # noqa: BLE001
        t.status = "failed"
        t.error = f"{type(e).__name__}: {str(e)[:160]}"
        APP.log(f"[{t.id}] ✗ {email} 失败: {t.error}", "error")
    finally:
        t.ended = time.time()


async def run_browser_task(t: Task, cfg) -> None:
    """浏览器 SDK 模式：起子进程跑 reg.py browser，流式解析日志。"""
    t.status = "running"
    t.email = "(浏览器模式)"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "reg.py"), "browser", "--n", "1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(ROOT),
    )
    stage_map = [
        ("turnstile", "captcha"), ("Turnstile", "captcha"), ("租手机号", "sms_rent"),
        ("signUp.create", "clerk"), ("email_code", "email_code"), ("phone_code", "phone_code"),
        ("Marble", "marble"), ("积分", "verify"),
    ]
    assert proc.stdout
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        txt = line.decode("utf-8", "replace").rstrip()
        if not txt:
            continue
        APP.log(f"[{t.id}] {txt}")
        for kw, st in stage_map:
            if kw in txt:
                t.stage = st
                try:
                    i = STAGE_ORDER.index(st)
                    t.progress = sum(s[2] for s in STAGES[:i])
                except ValueError:
                    pass
                break
        m = re.search(r"积分 = ([\d.]+)", txt)
        if m:
            t.credits = float(m.group(1))
    code = await proc.wait()
    t.ended = time.time()
    if code == 0 and t.credits:
        t.status, t.stage, t.progress = "ok", "done", 100
        APP.log(f"[{t.id}] 完成，积分 {t.credits:.0f}", "ok")
    else:
        t.status = "failed"
        t.error = "浏览器模式未拿到积分（看日志）"
        APP.log(f"[{t.id}] 浏览器模式失败，exit={code}", "error")


def parse_pool(text: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "----" in line:
            e, p = line.split("----", 1)
            if e.strip() and p.strip():
                out.append((e.strip(), p.strip()))
    return out


async def runner(mode: str, count: int, concurrency: int, use_browser: bool,
                 pool: str = "", pool_url: str = "http://127.0.0.1:8790") -> None:
    APP.running = True
    APP.mode = mode
    APP.started_at = time.time()
    cfg = _load_cfg()
    if mode == "live":
        APP.store = Store(cfg.run.db_path)
        APP.log(f"== 启动 Live 模式：{count} 个账号，并发 {concurrency}，"
                f"captcha={cfg.captcha.provider} mail={cfg.mail.provider} sms={cfg.sms.provider} ==")
    else:
        APP.log(f"== 启动 Demo 模式：{count} 个模拟账号，并发 {concurrency} ==")

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(t: Task) -> None:
        async with sem:
            if not APP.running:
                t.status = "stopped"
                return
            if mode == "demo":
                await run_demo_task(t, concurrency)
            elif accounts and t.email:
                await run_onboard_task(t, cfg, t.email, t.pw, pool_url)
            elif use_browser:
                await run_browser_task(t, cfg)
            else:
                await run_live_task(t, cfg)

    accounts = parse_pool(pool)
    if mode == "live" and accounts:
        # 批量开号：每个任务绑定一个 hotmail
        n = min(count, len(accounts))
        APP.tasks = APP.tasks[:n]
        for i, t in enumerate(APP.tasks):
            t.email, t.pw = accounts[i] if i < len(accounts) else ("", "")
        APP.log(f"== 批量开号：{n} 个账号，并发 {concurrency} ==")

    APP.batch = [asyncio.create_task(one(t)) for t in APP.tasks if t.status == "queued"]
    await asyncio.gather(*APP.batch, return_exceptions=True)
    APP.running = False
    st = APP.stats()
    APP.log(f"== 结束：成功 {st['ok']} / 失败 {st['failed']} / 总积分 {st['credits']:.0f}，"
            f"耗时 {st['elapsed']:.0f}s ==", "ok" if st["ok"] else "error")


# ---------------------------------------------------------------- FastAPI
app = FastAPI(title="Atlas 注册机")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (ROOT / "web" / "panel.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(APP.state())


@app.get("/api/config")
async def api_config() -> JSONResponse:
    cfg = _load_cfg()
    return JSONResponse({
        "captcha": {"provider": cfg.captcha.provider, "api_key": cfg.captcha.api_key,
                    "sitekey": cfg.captcha.sitekey, "action": cfg.captcha.action},
        "mail": {"provider": cfg.mail.provider, "domain": cfg.mail.domain,
                 "imap_host": cfg.mail.imap_host, "imap_user": cfg.mail.imap_user,
                 "imap_pass": cfg.mail.imap_pass, "http_url": cfg.mail.http_url,
                 "http_token": cfg.mail.http_token,
                 "ms_email": cfg.mail.ms_email, "owa_profile": cfg.mail.owa_profile,
                 "ms_accounts_file": cfg.mail.ms_accounts_file},
        "sms": {"provider": cfg.sms.provider, "api_key": cfg.sms.api_key,
                "country": cfg.sms.country, "service": cfg.sms.service},
        "run": {"concurrency": cfg.run.concurrency, "per_ip_delay": cfg.run.per_ip_delay,
                "proxies": cfg.run.proxies, "db_path": cfg.run.db_path},
    })


@app.post("/api/config")
async def api_config_save(req: Request) -> JSONResponse:
    body = await req.json()
    raw: dict[str, Any] = {}
    if CFG_PATH.exists():
        raw = yaml.safe_load(CFG_PATH.read_text(encoding="utf-8")) or {}
    for section in ("captcha", "mail", "sms", "run"):
        if section in body and isinstance(body[section], dict):
            raw.setdefault(section, {}).update(body[section])
    if "proxies" in raw.get("run", {}) and isinstance(raw["run"]["proxies"], str):
        raw["run"]["proxies"] = [x.strip() for x in raw["run"]["proxies"].split(",") if x.strip()]
    CFG_PATH.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cfg = _load_cfg()
    APP.log(f"配置已保存（captcha={cfg.captcha.provider} mail={cfg.mail.provider} sms={cfg.sms.provider}）")
    return JSONResponse({"ok": True})


@app.post("/api/start")
async def api_start(req: Request) -> JSONResponse:
    body = await req.json()
    if APP.running:
        return JSONResponse({"ok": False, "error": "已在运行中，先停止"}, status_code=400)
    mode = body.get("mode", "demo")
    count = int(body.get("count", 5))
    concurrency = int(body.get("concurrency", 2))
    use_browser = bool(body.get("browser", False))
    pool = body.get("pool") or ""
    pool_url = body.get("pool_url") or (cfgmod.load(CFG_PATH).atlas.pool_url if hasattr(cfgmod.load(CFG_PATH).atlas, "pool_url") else "http://127.0.0.1:8790")
    if mode == "live":
        cfg = _load_cfg()
        miss: list[str] = []
        if not use_browser and cfg.captcha.provider != "none" and not cfg.captcha.api_key:
            miss.append("CAPTCHA_KEY（或勾选浏览器 SDK 模式免打码）")
        if cfg.mail.provider == "imap" and not (cfg.mail.domain and cfg.mail.imap_host):
            miss.append("MAIL_DOMAIN + IMAP_*")
        if cfg.mail.provider == "http" and not (cfg.mail.domain and cfg.mail.http_url):
            miss.append("MAIL_DOMAIN + MAIL_HTTP_URL")
        if cfg.mail.provider == "msowa" and not cfg.mail.ms_email:
            miss.append("hotmail 地址（邮箱来源选 msowa 时必填）")
        if cfg.sms.provider != "manual" and not cfg.sms.api_key:
            miss.append("SMS_KEY（接码平台）")
        if miss:
            return JSONResponse({"ok": False, "error": "缺配置：" + "，".join(miss)}, status_code=400)
    APP.tasks = [Task(i + 1, mode) for i in range(count)]
    APP.logs.clear()
    asyncio.create_task(runner(mode, count, concurrency, use_browser, pool, pool_url))
    return JSONResponse({"ok": True, "count": count})


@app.post("/api/stop")
async def api_stop() -> JSONResponse:
    APP.running = False
    for t in APP.tasks:
        if t.status == "queued":
            t.status = "stopped"
    APP.log("收到停止指令", "error")
    return JSONResponse({"ok": True})


@app.post("/api/clear")
async def api_clear() -> JSONResponse:
    if APP.running:
        return JSONResponse({"ok": False, "error": "运行中不能清空"}, status_code=400)
    APP.tasks = []
    APP.logs.clear()
    return JSONResponse({"ok": True})


def _ms_file():
    cfg = _load_cfg()
    return Path(cfg.mail.ms_accounts_file or "ms_accounts.json")


@app.get("/api/ms")
async def api_ms() -> JSONResponse:
    from atlasreg.mail_ms import Pool

    f = _ms_file()
    pool = Pool(f)
    return JSONResponse({
        "file": str(f),
        "accounts": [
            {"email": a.email, "has_token": bool(a.refresh_token), "has_password": bool(a.password),
             "ok_count": a.ok_count, "fail_count": a.fail_count, "note": a.note,
             "alias_example": a.alias("atlas01") if a.email else ""}
            for a in pool.accounts
        ],
    })


@app.post("/api/ms/import")
async def api_ms_import(req: Request) -> JSONResponse:
    from atlasreg.mail_ms import Pool, parse_line

    body = await req.json()
    lines = body.get("lines") or []
    if isinstance(lines, str):
        lines = lines.splitlines()
    pool = Pool(_ms_file())
    ok, bad = 0, []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            pool.add(parse_line(line))
            ok += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"{line[:28]}… → {e}")
    APP.log(f"微软账号池导入 {ok} 个（共 {len(pool.accounts)} 个）", "ok" if ok else "error")
    return JSONResponse({"ok": True, "imported": ok, "failed": bad})


@app.get("/api/export")
async def api_export() -> PlainTextResponse:
    cfg = _load_cfg()
    rows = []
    if Path(cfg.run.db_path).exists():
        store = Store(cfg.run.db_path)
        cur = store.conn.execute(
            "SELECT email,phone,password,clerk_user_id,account_id,credits,jwt,status FROM accounts "
            "WHERE status='ok' ORDER BY id"
        )
        rows = [list(r) for r in cur.fetchall()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "phone", "password", "clerk_user_id", "account_id", "credits", "jwt", "status"])
    w.writerows(rows)
    return PlainTextResponse(buf.getvalue(), headers={
        "Content-Disposition": 'attachment; filename="atlas_accounts.csv"',
        "Content-Type": "text/csv; charset=utf-8",
    })


@app.get("/api/stream")
async def api_stream() -> StreamingResponse:
    """SSE：状态有变化就推，前端不用轮询。"""
    async def gen():
        last = ""
        while True:
            payload = json.dumps(APP.state(), ensure_ascii=False)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- 前端


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"\n  Atlas 注册机控制台 → http://{a.host}:{a.port}\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
