#!/usr/bin/env python3
"""Atlas Pool Gateway —— 多账号额度池 + OpenAI 风格 API。

面向生产：
  · 全局连接池 + HTTP/2 + keep-alive，支撑高并发中转
  · 上游重试（429/5xx/网络抖动，指数退避）、并发闸门、单 key 限流
  · 长任务自动轮询（splat / mesh 会先返回 RUNNING）
  · 请求 ID 贯穿日志，调用留痕可审计
  · SQLite WAL + 索引，账号熔断

对外：
  GET  /v1/models
  POST /v1/tasks/{model}?wait=true
  GET  /v1/operations/{id}?wait=true
  GET  /v1/usage
  GET  /v1/health

对内：
  GET  /admin/state   POST /admin/accounts   POST /admin/keys
  POST /admin/sync-credits   POST /admin/test/{model}
  GET  /admin/image?url=      （GCS 直链代理）
  GET  /
"""
from __future__ import annotations

import argparse
import asyncio
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
from fastapi.responses import HTMLResponse, JSONResponse, Response

ROOT = Path(__file__).resolve().parent
DB = ROOT / "pool.db"
ATLAS = "https://atlas.worldlabs.ai/api/v2"

PROXY: str | None = None
UPSTREAM_CONCURRENCY = 64          # 同时打到上游的最大请求数
KEY_RATE_LIMIT = 60                # 单 key 每分钟
LONG_TASK_TIMEOUT = 900            # 长任务轮询上限（秒）

MODELS = [
    # ---- 图像 ----
    {"id": "atlasTextToImage", "category": "image", "object": "model", "owned_by": "worldlabs",
     "description": "文生图：文本 → 世界导向场景图",
     "params": ["prompt", "aspectRatio?", "enhancePrompt?", "numSamples?"], "latency": "30-41s"},
    {"id": "atlasGenerate", "category": "image", "object": "model", "owned_by": "worldlabs",
     "description": "位姿/深度上下文 → 生成新视角",
     "params": ["contextFrames[]", "targetCameras[]", "prompt", "returnDepth?"], "latency": "19s"},
    {"id": "atlasChisel", "category": "image", "object": "model", "owned_by": "worldlabs",
     "description": "深度目标帧 + 提示 → 对应视角",
     "params": ["targetFrames[]", "prompt", "returnDepth?"], "latency": "11s"},
    # ---- 3D ----
    {"id": "images2PosedRGBD", "category": "3d", "object": "model", "owned_by": "worldlabs",
     "description": "图像 → 相机位姿 + 线性深度",
     "params": ["frames[].imageAsset.assetId", "targetResolution?", "canonicalizeToFirstFrame?"],
     "latency": "6-16s"},
    {"id": "posedImages2Splats", "category": "3d", "object": "model", "owned_by": "worldlabs",
     "description": "多视图（≥2）→ 3D Gaussian Splats (.spz)",
     "params": ["posedFrames[] (>=2)"], "latency": "2-3 min"},
    {"id": "splats2Mesh", "category": "3d", "object": "model", "owned_by": "worldlabs",
     "description": "Splats → 三角网格 + 锚点",
     "params": ["splatAsset.assetId"], "latency": "1 min"},
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
  calls INTEGER DEFAULT 0, credits_used REAL DEFAULT 0, rpm INTEGER DEFAULT 60
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sk TEXT, model TEXT, account_id TEXT, status INTEGER, elapsed REAL,
  op_id TEXT, err TEXT, created_at REAL, req_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_time ON calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_sk ON calls(sk);
"""


class Pool:
    def __init__(self, path: Path = DB):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        except Exception:  # noqa: BLE001
            pass
        self.db.commit()
        self._migrate()
        self._rr = 0
        self._lock = asyncio.Lock()
        self._bucket: dict[str, list[float]] = {}

    def _migrate(self) -> None:
        """老库升级：补齐新增列（CREATE TABLE IF NOT EXISTS 不会加列）。"""
        def cols(t: str) -> set[str]:
            return {r[1] for r in self.db.execute(f"PRAGMA table_info({t})")}

        need = {
            "calls": {"req_id": "TEXT"},
            "sk_keys": {"rpm": "INTEGER DEFAULT 60", "credits_used": "REAL DEFAULT 0"},
            "accounts": {"last_error": "TEXT", "last_used": "REAL"},
        }
        for table, columns in need.items():
            have = cols(table)
            for name, decl in columns.items():
                if name not in have:
                    try:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    except Exception:  # noqa: BLE001
                        pass
        self.db.commit()

    # ---------------- 账号
    def accounts(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM accounts ORDER BY id").fetchall()]

    def add_account(self, email: str, wlt_key: str, account_id: str = "",
                    project_id: str = "", credits: float = 0) -> dict:
        self.db.execute(
            "INSERT INTO accounts(email,wlt_key,account_id,project_id,credits,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
            "wlt_key=excluded.wlt_key, account_id=excluded.account_id, "
            "project_id=excluded.project_id, enabled=1",
            (email, wlt_key, account_id, project_id, credits, time.time()),
        )
        self.db.commit()
        return {"ok": True}

    async def pick(self) -> dict | None:
        async with self._lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM accounts WHERE enabled=1 AND wlt_key!=''").fetchall()]
            if not rows:
                return None
            rows.sort(key=lambda a: (a["fail_count"] - a["ok_count"] * 3, -a["credits"]))
            usable = [a for a in rows if a["fail_count"] < 8] or rows
            acc = usable[self._rr % len(usable)]
            self._rr += 1
            return acc

    def mark(self, account_id: str, ok: bool, err: str = "") -> None:
        if ok:
            self.db.execute("UPDATE accounts SET ok_count=ok_count+1,last_used=?,last_error='' "
                            "WHERE account_id=?", (time.time(), account_id))
        else:
            self.db.execute("UPDATE accounts SET fail_count=fail_count+1,last_error=?,last_used=? "
                            "WHERE account_id=?", (err[:300], time.time(), account_id))
        self.db.commit()

    def set_credits(self, email: str, credits: float) -> None:
        self.db.execute("UPDATE accounts SET credits=? WHERE lower(email)=lower(?)", (credits, email))
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

    def rate_ok(self, sk: str, rpm: int = KEY_RATE_LIMIT) -> bool:
        now = time.time()
        bucket = [t for t in self._bucket.get(sk, []) if now - t < 60]
        if len(bucket) >= rpm:
            self._bucket[sk] = bucket
            return False
        bucket.append(now)
        self._bucket[sk] = bucket
        return True

    # ---------------- 日志
    def log_call(self, sk: str, model: str, account_id: str, status: int, elapsed: float,
                 op_id: str = "", err: str = "", req_id: str = "") -> None:
        try:
            self.db.execute(
                "INSERT INTO calls(sk,model,account_id,status,elapsed,op_id,err,created_at,req_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sk, model, account_id, status, elapsed, op_id, err[:300], time.time(), req_id),
            )
            self.db.commit()
        except Exception:  # noqa: BLE001  日志失败绝不影响业务响应
            try:
                self.db.execute(
                    "INSERT INTO calls(sk,model,account_id,status,elapsed,op_id,err,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (sk, model, account_id, status, elapsed, op_id, err[:300], time.time()),
                )
                self.db.commit()
            except Exception:  # noqa: BLE001
                pass

    def calls(self, limit: int = 60) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def stats(self) -> dict:
        a = self.db.execute("SELECT COUNT(*) n,COALESCE(SUM(credits),0) c,"
                            "COALESCE(SUM(ok_count),0) ok,COALESCE(SUM(fail_count),0) fail "
                            "FROM accounts").fetchone()
        k = self.db.execute("SELECT COUNT(*) n,COALESCE(SUM(calls),0) calls FROM sk_keys").fetchone()
        cl = self.db.execute("SELECT COUNT(*) n,COALESCE(SUM(CASE WHEN status<300 AND status>0 THEN 1 ELSE 0 END),0) ok,"
                             "COALESCE(AVG(CASE WHEN elapsed>0 THEN elapsed END),0) avg "
                             "FROM calls").fetchone()
        day = self.db.execute("SELECT COUNT(*) n FROM calls WHERE created_at>?",
                              (time.time() - 86400,)).fetchone()
        return {"accounts": a["n"], "credits": a["c"], "ok": a["ok"], "fail": a["fail"],
                "keys": k["n"], "key_calls": k["calls"], "calls": cl["n"], "calls_ok": cl["ok"],
                "avg_elapsed": round(cl["avg"] or 0, 1), "calls_24h": day["n"]}


POOL = Pool()


# ---------------------------------------------------------------- 上游客户端（全局连接池）
_client: httpx.AsyncClient | None = None
_sem = asyncio.Semaphore(UPSTREAM_CONCURRENCY)


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            proxy=PROXY,
            timeout=httpx.Timeout(300.0, connect=20.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=60,
                                keepalive_expiry=90.0),
            http2=False,          # 经 HTTP 代理时 h2 复用会 RECORD_LAYER_FAILURE
            headers={"accept-encoding": "gzip, deflate"},
        )
    return _client


async def upstream(method: str, path: str, wlt_key: str, *, body: bytes | None = None,
                   idem: str | None = None, retries: int = 3) -> httpx.Response:
    """带并发闸门与退避重试的上游调用。"""
    url = f"{ATLAS}{path}"
    headers = {"WLT-Api-Key": wlt_key}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idem:
        headers["Idempotency-Key"] = idem
    last_exc: Exception | None = None
    async with _sem:
        for attempt in range(retries):
            try:
                r = await client().request(method, url, content=body, headers=headers)
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt < retries - 1:
                        await asyncio.sleep(min(2 ** attempt * 1.5, 8))
                        continue
                return r
            except (httpx.HTTPError, OSError, ConnectionError) as e:
                last_exc = e
                if attempt < retries - 1:
                    await asyncio.sleep(min(2 ** attempt, 6))
                    continue
                raise
    assert last_exc
    raise last_exc


# ---------------------------------------------------------------- 应用
app = FastAPI(title="Atlas Pool Gateway", version="2.0.0")


def _err(status: int, msg: str, code: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": code}}, status_code=status)


def _auth(request: Request) -> tuple[dict, str]:
    raw = request.headers.get("authorization", "")
    sk = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    if not sk:
        sk = request.headers.get("wlt-api-key", "").strip()
    return (POOL.sk_valid(sk) or {}), sk


@app.middleware("http")
async def _reqid(request: Request, call_next):
    request.state.req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    t0 = time.time()
    resp = await call_next(request)
    resp.headers["x-request-id"] = request.state.req_id
    resp.headers["x-atlas-elapsed"] = f"{time.time() - t0:.3f}"
    return resp


@app.get("/v1/health")
async def health():
    async with _sem:
        ok = True
    return {"status": "ok", "accounts": POOL.stats()["accounts"], "credits": POOL.stats()["credits"],
            "concurrency": UPSTREAM_CONCURRENCY, "version": "2.0.0"}


@app.get("/v1/models")
async def models(request: Request):
    key, _ = _auth(request)
    if not key:
        return _err(401, "missing key: Authorization: Bearer sk-pool-…", "authentication_error")
    return {"object": "list",
            "data": [{k: m[k] for k in ("id", "object", "owned_by", "category", "description",
                                        "params", "latency")} for m in MODELS]}


@app.get("/v1/usage")
async def usage(request: Request):
    key, sk = _auth(request)
    if not key:
        return _err(401, "missing key", "authentication_error")
    mine = [c for c in POOL.calls(300) if c["sk"] == sk]
    return {"key": {"name": key["name"], "calls": key["calls"], "enabled": bool(key["enabled"])},
            "recent": mine[:20], "pool": POOL.stats()}


async def _poll_until_done(op_id: str, wlt_key: str, req_id: str) -> tuple[httpx.Response, dict]:
    deadline = time.time() + LONG_TASK_TIMEOUT
    r = await upstream("GET", f"/operations/{op_id}", wlt_key)
    data = r.json() if r.status_code < 300 else {}
    while not data.get("done") and time.time() < deadline:
        await asyncio.sleep(4)
        r = await upstream("GET", f"/operations/{op_id}", wlt_key)
        if r.status_code >= 400:
            break
        data = r.json()
    return r, data


@app.post("/v1/tasks/{model}")
async def submit(model: str, request: Request):
    req_id = getattr(request.state, "req_id", "-")
    key, sk = _auth(request)
    if not key:
        return _err(401, "missing key", "authentication_error")
    if model not in MODEL_IDS:
        return _err(404, f"unknown model '{model}' — see /v1/models", "model_not_found")
    if not POOL.rate_ok(sk, key.get("rpm") or KEY_RATE_LIMIT):
        return _err(429, "rate limit: 60 req/min per key", "rate_limit_exceeded")

    acc = await POOL.pick()
    if not acc:
        return _err(503, "账号池为空：先在 /admin/accounts 添加带 WLT-Api-Key 的账号", "pool_empty")

    body = await request.body()
    try:
        json.loads(body or b"{}")
    except Exception:  # noqa: BLE001
        return _err(400, "body must be JSON")

    q = str(request.url).lower()
    wait = "wait=true" in q or "wait=full" in q
    wait_full = "wait=false" not in q
    t0 = time.time()
    try:
        r = await upstream("POST", f"/tasks:{model}" + ("?wait=true" if wait else ""), acc["wlt_key"],
                           body=body, idem=request.headers.get("idempotency-key") or uuid.uuid4().hex)
        data: Any
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"raw": r.text[:2000]}

        op_id = str(data.get("id") or "") if isinstance(data, dict) else ""
        if (wait_full and r.status_code < 300 and isinstance(data, dict) and op_id
                and not data.get("done")):
            r2, d2 = await _poll_until_done(op_id, acc["wlt_key"], req_id)
            if d2:
                r, data = r2, d2

        elapsed = time.time() - t0
        POOL.bump_key(sk)
        POOL.log_call(sk, model, acc["account_id"], r.status_code, elapsed, op_id,
                      "" if r.status_code < 300 else str(data)[:200], req_id)
        POOL.mark(acc["account_id"], r.status_code < 300, str(data)[:200])

        if r.status_code >= 300:
            return JSONResponse({"error": {"message": "upstream error", "upstream": data,
                                           "request_id": req_id}}, status_code=r.status_code)
        if isinstance(data, dict):
            data["_gateway"] = {"model": model, "account": acc["email"],
                                "elapsed": round(elapsed, 2), "request_id": req_id}
        return JSONResponse(data)

    except Exception as e:  # noqa: BLE001
        elapsed = time.time() - t0
        POOL.bump_key(sk)
        POOL.mark(acc["account_id"], False, f"{type(e).__name__}: {e}")
        POOL.log_call(sk, model, acc["account_id"], 0, elapsed, "", str(e)[:200], req_id)
        return _err(502, f"上游不可达: {type(e).__name__}: {e}", "upstream_error")


@app.get("/v1/operations/{op_id}")
async def operation(op_id: str, request: Request):
    key, sk = _auth(request)
    if not key:
        return _err(401, "missing key", "authentication_error")
    acc = await POOL.pick()
    if not acc:
        return _err(503, "账号池为空", "pool_empty")
    wait = "wait=true" in str(request.url).lower()
    r = await upstream("GET", f"/operations/{op_id}" + ("?wait=true" if wait else ""), acc["wlt_key"])
    try:
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception:  # noqa: BLE001
        return _err(502, r.text[:500], "upstream_error")


@app.get("/admin/image")
async def admin_image(url: str):
    if not url.startswith("https://storage.googleapis.com/"):
        return _err(400, "only storage.googleapis.com allowed")
    r = await client().get(url, timeout=120)
    if r.status_code != 200:
        return _err(r.status_code, "fetch failed")
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"cache-control": "public, max-age=300"})


# ---------------------------------------------------------------- 管理端
@app.get("/admin/state")
async def admin_state():
    return {"pool": POOL.stats(), "accounts": POOL.accounts(),
            "keys": POOL.keys(), "calls": POOL.calls(50), "models": MODELS}


@app.post("/admin/accounts")
async def admin_add_account(request: Request):
    b = await request.json()
    email = (b.get("email") or "").strip()
    wlt = (b.get("wlt_key") or "").strip()
    if not email or not wlt:
        return _err(400, "email 与 wlt_key 必填")
    POOL.add_account(email, wlt, b.get("account_id", ""), b.get("project_id", ""),
                     float(b.get("credits") or 0))
    return {"ok": True, "accounts": POOL.accounts()}


@app.post("/admin/keys")
async def admin_new_key(request: Request):
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        b = {}
    return {"ok": True, "key": POOL.new_sk(b.get("name") or "default")}


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
    adb = ROOT / "accounts.db"
    if not adb.exists():
        return {"ok": False, "msg": "accounts.db 不存在"}
    src = sqlite3.connect(str(adb))
    src.row_factory = sqlite3.Row
    n = 0
    for r in src.execute("SELECT email, credits FROM accounts WHERE status='ok'").fetchall():
        POOL.set_credits(r["email"], r["credits"] or 0)
        n += 1
    return {"ok": True, "synced": n, "pool": POOL.stats()}


@app.post("/admin/test/{model}")
async def admin_test(model: str, request: Request):
    acc = await POOL.pick()
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
    r = await upstream("POST", f"/tasks:{model}?wait=true", acc["wlt_key"], body=body,
                       idem=uuid.uuid4().hex)
    POOL.mark(acc["account_id"], r.status_code < 300, r.text[:200])
    POOL.log_call("admin-test", model, acc["account_id"], r.status_code, time.time() - t0)
    try:
        return {"ok": r.status_code < 300, "status": r.status_code,
                "elapsed": round(time.time() - t0, 1), "response": r.json()}
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": r.status_code, "raw": r.text[:400]}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (ROOT / "web" / "gateway.html").read_text(encoding="utf-8")


def main() -> int:
    global PROXY, UPSTREAM_CONCURRENCY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--proxy", default="http://127.0.0.1:7890")
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--seed-account", default="", help="email::wlt_key::credits")
    a = ap.parse_args()
    PROXY = a.proxy or None
    UPSTREAM_CONCURRENCY = a.concurrency

    if a.seed_account and "::" in a.seed_account:
        parts = a.seed_account.split("::")
        POOL.add_account(parts[0], parts[1],
                         credits=float(parts[2]) if len(parts) > 2 and parts[2] else 0.0)
    if not POOL.keys():
        POOL.new_sk("default")
        print("已生成用户 key:", POOL.keys()[0]["sk"], flush=True)
    print(f"\n  Console  → http://{a.host}:{a.port}/", flush=True)
    print(f"  base_url → http://{a.host}:{a.port}/v1", flush=True)
    print(f"  并发上限 → {UPSTREAM_CONCURRENCY}\n", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
