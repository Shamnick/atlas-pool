"""账号库（SQLite）：注册结果、会话、积分、失败原因全部落库。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE,
  phone TEXT,
  password TEXT,
  clerk_sign_up_id TEXT,
  clerk_session_id TEXT,
  clerk_user_id TEXT,
  jwt TEXT,
  account_id TEXT,
  credits REAL,
  status TEXT,          -- ok | failed
  stage TEXT,           -- 失败停在哪个阶段
  error TEXT,
  proxy TEXT,
  cookies TEXT,
  raw TEXT,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_status ON accounts(status);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert(self, **kw: Any) -> int:
        kw.setdefault("created_at", time.time())
        for k in ("cookies", "raw"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        cols = ",".join(kw)
        ph = ",".join("?" * len(kw))
        cur = self.conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({ph})", list(kw.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def update(self, row_id: int, **kw: Any) -> None:
        for k in ("cookies", "raw"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        if not kw:
            return
        sets = ",".join(f"{k}=?" for k in kw)
        self.conn.execute(f"UPDATE accounts SET {sets} WHERE id=?", [*kw.values(), row_id])
        self.conn.commit()

    def ok_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM accounts WHERE status='ok'").fetchone()[0])

    def credits_sum(self) -> float:
        v = self.conn.execute("SELECT COALESCE(SUM(credits),0) FROM accounts WHERE status='ok'").fetchone()[0]
        return float(v or 0)

    def export(self, out: str | Path) -> int:
        rows = self.conn.execute(
            "SELECT email,phone,password,clerk_user_id,account_id,credits,jwt,cookies FROM accounts "
            "WHERE status='ok' ORDER BY id"
        ).fetchall()
        p = Path(out)
        if p.suffix.lower() == ".csv":
            import csv

            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["email", "phone", "password", "clerk_user_id", "account_id", "credits", "jwt", "cookies"])
                for r in rows:
                    w.writerow([r[k] for k in r.keys()])
        else:
            p.write_text(
                json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return len(rows)
