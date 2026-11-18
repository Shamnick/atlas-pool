#!/usr/bin/env python3
"""Atlas 账号池网关 —— 用一个 base_url + key 调 World Labs 的模型能力。

对外（用户侧）：
    base_url = http://127.0.0.1:8790/v1
    key      = sk-pool-xxxxxxxx      （在管理面板签发 / 或 --key 直接指定）

    GET  /v1/models                     列出可用模型（atlasTextToImage 等）
    POST /v1/tasks/{model}?wait=true    提交任务（body 原样透传给上游）
    GET  /v1/operations/{id}?wait=true  轮询操作
    GET  /v1/usage                      本 key 的调用统计

对内（管理侧）：
    GET  /admin/state        池 / key / 调用日志
    POST /admin/accounts     加账号（邮箱 + WLT-Api-Key + account/project）
    POST /admin/keys         签发用户 key
    GET  /                   管理面板

池化策略：按「可用 + 剩余积分」轮转；某账号报错自动降权，连续失败熔断。
上游真实接口（逆向所得）：
    POST {ATLAS}/api/v2/tasks:{task}?wait=true   Header: WLT-Api-Key
    GET  {ATLAS}/api/v2/operations/{id}?wait=true
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import string
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
DB = ROOT / "pool.db"

ATLAS = "https://atlas.worldlabs.ai/api/v2"

MODELS = [
    {
        "id": "atlasTextToImage", "object": "model", "owned_by": "worldlabs", "category": "image",
        "description": "文生图：从文本提示生成世界导向的场景图",
        "params": ["prompt", "aspectRatio?", "enhancePrompt?", "numSamples?"],
    },
    {
        "id": "images2PosedRGBD", "object": "model", "owned_by": "worldlabs", "category": "3d",
        "description": "图像 → 相机位姿 + 线性深度（3D 重建第一步）",
        "params": ["frames[].imageAsset.assetId", "targetResolution?", "canonicalizeToFirstFrame?"],
    },
    {
        "id": "atlasGenerate", "object": "model", "owned_by": "worldlabs", "category": "image",
        "description": "已知位姿/深度上下文 → 生成新视角画面",
        "params": ["contextFrames[]", "targetCameras[]", "prompt", "returnDepth?"],
    },
    {
        "id": "atlasChisel", "object": "model", "owned_by": "worldlabs", "category": "image",
        "description": "深度目标帧 + 提示 → 生成对应视角",
        "params": ["targetFrames[]", "prompt", "returnDepth?"],
    },
    {
        "id": "posedImages2Splats", "object": "model", "owned_by": "worldlabs", "category": "3d",
        "description": "带位姿的多视图 → 3D Gaussian Splats（至少 2 帧，长任务）",
        "params": ["posedFrames[] (>=2)"],
    },
    {
        "id": "splats2Mesh", "object": "model", "owned_by": "worldlabs", "category": "3d",
        "description": "Splats → 三角网格（mesh）",
        "params": ["splatAsset.assetId"],
    },
]
MODEL_IDS = {m["id"] for m in MODELS}


# ---------------------------------------------------------------- 存储
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE, wlt_key TEXT, account_id TEXT, project_id TEXT,
  credits REAL DEFAULT 0, enabled INTEGER DEFAULT 1,
  ok_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
  last_error TEXT, last_used REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS sk_keys (
  sk TEXT PRIMARY KEY, name TEXT, enabled INTEGER DEFAULT 1, created_at REAL,
  calls INTEGER DEFAULT 0, credits_used REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sk TEXT, model TEXT, account_id TEXT, status INTEGER, elapsed REAL,
  op_id TEXT, err TEXT, created_at REAL
);
"""


class Pool:
    def __init__(self, path: Path = DB):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._rr = 0

    # ---------------- 账号
    def add_account(self, email: str, wlt_key: str, account_id: str = "",
                    project_id: str = "", credits: float = 0) -> dict:
        cur = self.db.execute(
            "INSERT INTO accounts(email,wlt_key,account_id,project_id,credits,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
            "wlt_key=excluded.wlt_key, account_id=excluded.account_id, "
            "project_id=excluded.project_id, enabled=1",
            (email, wlt_key, account_id, project_id, credits, time.time()),
        )
        self.db.commit()
        return {"ok": True, "id": cur.lastrowid or "updated"}

    def accounts(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def pick(self) -> dict | None:
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM accounts WHERE enabled=1 AND wlt_key != '' ORDER BY id").fetchall()]
        if not rows:
            return None
        # 成功率越高、失败越少、积分越多越优先
        rows.sort(key=lambda a: (a["fail_count"] - a["ok_count"] * 3, -a["credits"]))
        usable = [a for a in rows if a["fail_count"] < 5] or rows
        acc = usable[self._rr % len(usable)]
        self._rr += 1
        return acc

    def mark(self, account_id: str, ok: bool, err: str = "") -> None:
        if ok:
            self.db.execute("UPDATE accounts SET ok_count=ok_count+1, last_used=?, last_error='' "
                            "WHERE account_id=?", (time.time(), account_id))
        else:
            self.db.execute("UPDATE accounts SET fail_count=fail_count+1, last_error=?, last_used=? "
                            "WHERE account_id=?", (err[:300], time.time(), account_id))
        self.db.commit()

    def set_credits(self, account_id: str, credits: float) -> None:
        self.db.execute("UPDATE accounts SET credits=? WHERE account_id=?", (credits, account_id))
        self.db.commit()

    # ---------------- 用户 key
    def new_sk(self, name: str = "default") -> str:
        sk = "sk-pool-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=32))
        self.db.execute("INSERT INTO sk_keys(sk,name,created_at) VALUES(?,?,?)", (sk, name, time.time()))
        self.db.commit()
        return sk

    def sk_valid(self, sk: str) -> dict | None:
        row = self.db.execute("SELECT * FROM sk_keys WHERE sk=? AND enabled=1", (sk,)).fetchone()
        return dict(row) if row else None

    def keys(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM sk_keys ORDER BY created_at DESC")]

    def bump_key(self, sk: str) -> None:
        self.db.execute("UPDATE sk_keys SET calls=calls+1 WHERE sk=?", (sk,))
        self.db.commit()

    # ---------------- 调用日志
    def log_call(self, sk: str, model: str, account_id: str, status: int,
                 elapsed: float, op_id: str = "", err: str = "") -> None:
        self.db.execute(
            "INSERT INTO calls(sk,model,account_id,status,elapsed,op_id,err,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sk, model, account_id, status, elapsed, op_id, err[:300], time.time()),
        )
        self.db.commit()

    def calls(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def stats(self) -> dict:
        a = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(credits),0) c, COALESCE(SUM(ok_count),0) ok, "
            "COALESCE(SUM(fail_count),0) fail FROM accounts").fetchone()
        k = self.db.execute("SELECT COUNT(*) n, COALESCE(SUM(calls),0) calls FROM sk_keys").fetchone()
        cl = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN status<300 THEN 1 ELSE 0 END),0) ok "
            "FROM calls").fetchone()
        return {
            "accounts": a["n"], "credits": a["c"], "ok": a["ok"], "fail": a["fail"],
            "keys": k["n"], "key_calls": k["calls"],
            "calls": cl["n"], "calls_ok": cl["ok"],
        }


POOL = Pool()


# ---------------------------------------------------------------- 应用
app = FastAPI(title="Atlas Pool Gateway")
PROXY: str | None = None


def _proxy() -> str | None:
    return PROXY


def _auth(request: Request) -> tuple[dict, str]:
    """返回 (key 记录, 原始凭证)。接受 Bearer 或 WLT-Api-Key 两种写法。"""
    raw = request.headers.get("authorization", "")
    sk = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    if not sk:
        sk = request.headers.get("wlt-api-key", "").strip()
    return POOL.sk_valid(sk) or {}, sk


def _err(status: int, msg: str, code: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": code}}, status_code=status)


@app.get("/v1/models")
async def models(request: Request):
    key, sk = _auth(request)
    if not key:
        return _err(401, "no valid key (Authorization: Bearer sk-pool-… )", "authentication_error")
    return {"object": "list", "data": MODELS}


@app.get("/v1/usage")
async def usage(request: Request):
    key, sk = _auth(request)
    if not key:
        return _err(401, "no valid key", "authentication_error")
    mine = [c for c in POOL.calls(200) if c["sk"] == sk]
    return {
        "key": {"name": key["name"], "calls": key["calls"], "enabled": bool(key["enabled"])},
        "recent": mine[:20],
        "pool": POOL.stats(),
    }


@app.post("/v1/tasks/{model}")
async def submit(model: str, request: Request):
    key, sk = _auth(request)
    if not key:
        return _err(401, "no valid key", "authentication_error")
    if model not in MODEL_IDS:
        return _err(404, f"unknown model '{model}', see /v1/models", "model_not_found")

    acc = POOL.pick()
    if not acc:
        return _err(503, "账号池为空：先在 /admin/accounts 加一个带 WLT-Api-Key 的账号", "pool_empty")

    body = await request.body()
    try:
        json.loads(body or b"{}")
    except Exception:  # noqa: BLE001
        return _err(400, "body 必须是 JSON")

    q = str(request.url).lower()
    wait = "wait=true" in q or "wait=full" in q
    wait_full = "wait=false" not in q            # 默认等到真正 done（长任务自动轮询）
    url = f"{ATLAS}/tasks:{model}" + ("?wait=true" if wait else "")
    headers = {
        "WLT-Api-Key": acc["wlt_key"],
        "Content-Type": "application/json",
        "Idempotency-Key": request.headers.get("idempotency-key") or uuid.uuid4().hex,
    }
    t0 = time.time()
    try:
        async with httpx.AsyncClient(proxy=_proxy(), timeout=600) as c:
            r = await c.post(url, content=body, headers=headers)
            try:
                submitted = r.json()
            except Exception:  # noqa: BLE001
                submitted = {}
            # 长任务（splat / mesh）wait=true 只返回 RUNNING，需要轮询到 done
            if (wait_full and r.status_code < 300 and isinstance(submitted, dict)
                    and submitted.get("id") and not submitted.get("done")):
                op_id = submitted["id"]
                deadline = time.time() + 900
                while time.time() < deadline:
                    await asyncio.sleep(4)
                    rr = await c.get(f"{ATLAS}/operations/{op_id}?wait=true",
                                     headers={"WLT-Api-Key": acc["wlt_key"]})
                    if rr.status_code >= 400:
                        break
                    cur = rr.json()
                    if cur.get("done"):
                        r = rr
                        break
        elapsed = time.time() - t0
        POOL.bump_key(sk)
        data: Any
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"raw": r.text[:2000]}
        op_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        POOL.log_call(sk, model, acc["account_id"], r.status_code, elapsed, op_id,
                      "" if r.status_code < 300 else str(data)[:200])
        POOL.mark(acc["account_id"], r.status_code < 300, str(data)[:200])
        if r.status_code >= 300:
            return JSONResponse({"error": {"message": "upstream error", "upstream": data}},
                                status_code=r.status_code)
        # 给用户补一点元信息，方便按 OpenAI 习惯用
        if isinstance(data, dict) and wait:
            resp = data.get("response")
            data["_gateway"] = {"model": model, "account": acc["email"], "elapsed": round(elapsed, 2)}
            if isinstance(resp, dict):
                data["_gateway"]["output"] = resp
        return JSONResponse(data)
    except Exception as e:  # noqa: BLE001
        elapsed = time.time() - t0
        POOL.bump_key(sk)
        POOL.mark(acc["account_id"], False, f"{type(e).__name__}: {e}")
        POOL.log_call(sk, model, acc["account_id"], 0, elapsed, "", str(e)[:200])
        return _err(502, f"上游不可达: {type(e).__name__}: {e}", "upstream_error")


@app.get("/v1/operations/{op_id}")
async def operation(op_id: str, request: Request):
    key, sk = _auth(request)
    if not key:
        return _err(401, "no valid key", "authentication_error")
    acc = POOL.pick()
    if not acc:
        return _err(503, "账号池为空", "pool_empty")
    wait = "wait=true" in str(request.url).lower()
    url = f"{ATLAS}/operations/{op_id}" + ("?wait=true" if wait else "")
    async with httpx.AsyncClient(proxy=_proxy(), timeout=600) as c:
        r = await c.get(url, headers={"WLT-Api-Key": acc["wlt_key"]})
    try:
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception:  # noqa: BLE001
        return _err(502, r.text[:500], "upstream_error")


# ---------------------------------------------------------------- 管理端
@app.get("/admin/state")
async def admin_state():
    return {"pool": POOL.stats(), "accounts": POOL.accounts(),
            "keys": POOL.keys(), "calls": POOL.calls(40), "models": MODELS}


@app.post("/admin/accounts")
async def admin_add_account(request: Request):
    b = await request.json()
    email = (b.get("email") or "").strip()
    wlt = (b.get("wlt_key") or "").strip()
    if not email or not wlt:
        return _err(400, "需要 email 和 wlt_key")
    POOL.add_account(email, wlt, b.get("account_id", ""), b.get("project_id", ""),
                     float(b.get("credits") or 0))
    return {"ok": True, "accounts": POOL.accounts()}


@app.post("/admin/keys")
async def admin_new_key(request: Request):
    b = await request.json() if await request.body() else {}
    sk = POOL.new_sk(b.get("name") or "default")
    return {"ok": True, "key": sk}


@app.post("/admin/keys/{sk}/toggle")
async def admin_toggle_key(sk: str):
    row = POOL.db.execute("SELECT enabled FROM sk_keys WHERE sk=?", (sk,)).fetchone()
    if not row:
        return _err(404, "no such key")
    POOL.db.execute("UPDATE sk_keys SET enabled=? WHERE sk=?", (0 if row["enabled"] else 1, sk))
    POOL.db.commit()
    return {"ok": True}


@app.post("/admin/sync-credits")
async def admin_sync_credits():
    """从注册机账号库（accounts.db）同步各账号积分到池里。"""
    adb = ROOT / "accounts.db"
    if not adb.exists():
        return {"ok": False, "msg": "accounts.db 不存在"}
    src = sqlite3.connect(str(adb))
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT email, credits FROM accounts WHERE status='ok'").fetchall()
    n = 0
    for r in rows:
        POOL.db.execute("UPDATE accounts SET credits=? WHERE lower(email)=lower(?)",
                        (r["credits"] or 0, r["email"]))
        n += 1
    POOL.db.commit()
    return {"ok": True, "synced": n, "pool": POOL.stats()}


@app.post("/admin/test/{model}")
async def admin_test(model: str, request: Request):
    """面板上的「试跑一次」：用池里任意账号发一个最小任务。"""
    acc = POOL.pick()
    if not acc:
        return _err(503, "池为空")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    prompt = (payload or {}).get("prompt") or "a red apple on a wooden table, studio light"
    body = json.dumps({"prompt": prompt, "aspectRatio": "16:9", "enhancePrompt": True,
                       "numSamples": 1}).encode()
    t0 = time.time()
    async with httpx.AsyncClient(proxy=_proxy(), timeout=600) as c:
        r = await c.post(f"{ATLAS}/tasks:{model}?wait=true", content=body,
                         headers={"WLT-Api-Key": acc["wlt_key"], "Content-Type": "application/json",
                                  "Idempotency-Key": uuid.uuid4().hex})
    POOL.mark(acc["account_id"], r.status_code < 300, r.text[:200])
    POOL.log_call("admin-test", model, acc["account_id"], r.status_code, time.time() - t0)
    try:
        return {"ok": r.status_code < 300, "status": r.status_code, "elapsed": round(time.time() - t0, 1),
                "response": r.json()}
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": r.status_code, "raw": r.text[:400]}


@app.get("/admin/image")
async def admin_image(url: str):
    """图片代理：GCS 签名直链在浏览器里常被拦，这里由服务端取回转给前端。"""
    from fastapi.responses import Response
    if not url.startswith("https://storage.googleapis.com/"):
        return _err(400, "仅允许 storage.googleapis.com")
    async with httpx.AsyncClient(proxy=_proxy(), timeout=120) as c:
        r = await c.get(url)
    if r.status_code != 200:
        return _err(r.status_code, "拉取图片失败")
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"cache-control": "public, max-age=300"})


@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "web" / "gateway.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 面板


def main() -> int:
    global PROXY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--proxy", default="http://127.0.0.1:7890")
    ap.add_argument("--key", default="", help="预置一个用户 key（不填自动生成）")
    ap.add_argument("--seed-account", default="", help="email::wlt_key 预置账号")
    a = ap.parse_args()
    PROXY = a.proxy or None

    if a.seed_account and "::" in a.seed_account:
        parts = a.seed_account.split("::")
        email, wlt = parts[0], parts[1]
        credits = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        POOL.add_account(email, wlt, credits=credits)
    if not POOL.keys():
        POOL.new_sk("default")
        print("已生成用户 key:", POOL.keys()[0]["sk"], flush=True)
    print(f"\n  管理面板 → http://{a.host}:{a.port}/", flush=True)
    print(f"  base_url → http://{a.host}:{a.port}/v1\n", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
