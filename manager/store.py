#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vpnctl 持久化层：SQLite 记录「连接事件」与「管理审计」。

纯标准库。数据库默认在 <repo>/state/vpnplat.db，可用环境变量覆盖：
  VPNCTL_STATE_DIR  状态目录（默认 <repo>/state，已 .gitignore）
  VPNCTL_DB         数据库文件（默认 $VPNCTL_STATE_DIR/vpnplat.db）

采用「每次操作短连接」，对单机低并发足够，避免跨线程共享连接的问题。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

MANAGER_DIR = Path(__file__).resolve().parent
ROOT = MANAGER_DIR.parent
STATE_DIR = Path(os.environ.get("VPNCTL_STATE_DIR", str(ROOT / "state")))
DB_PATH = Path(os.environ.get("VPNCTL_DB", str(STATE_DIR / "vpnplat.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    key    TEXT NOT NULL,
    source TEXT NOT NULL,
    kind   TEXT NOT NULL DEFAULT 'event',
    line   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_key_ts ON events(key, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    actor         TEXT,
    source        TEXT,
    action        TEXT NOT NULL,
    key           TEXT,
    ok            INTEGER NOT NULL,
    stopped_peers TEXT,
    duration_ms   INTEGER,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL,
    title   TEXT NOT NULL,
    message TEXT,
    source  TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def record_event(key, line, source="docker", kind="event", ts=None):
    with closing(connect()) as con:
        con.execute("INSERT INTO events(ts,key,source,kind,line) VALUES(?,?,?,?,?)",
                    (float(ts or time.time()), key, source, kind, line))
        con.commit()


def record_audit(action, key=None, ok=True, actor=None, source="cli",
                 stopped_peers=None, duration_ms=None, message=None):
    with closing(connect()) as con:
        con.execute(
            "INSERT INTO audit(ts,actor,source,action,key,ok,stopped_peers,"
            "duration_ms,message) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), actor or "-", source or "cli", action, key,
             1 if ok else 0, ",".join(stopped_peers or []),
             int(duration_ms) if duration_ms is not None else None,
             (message or "")[:2000]))
        con.commit()


def record_alert(level, title, message="", source="watchdog", ts=None):
    with closing(connect()) as con:
        con.execute("INSERT INTO alerts(ts,level,title,message,source) VALUES(?,?,?,?,?)",
                    (float(ts or time.time()), level, title, (message or "")[:4000], source))
        con.commit()


# --------------------------------------------------------------------------- #
# 键值状态（守护进程跨重启记住上次状态）
# --------------------------------------------------------------------------- #
def get_meta(key, default=None):
    with closing(connect()) as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value):
    with closing(connect()) as con:
        con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (key, str(value)))
        con.commit()


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def recent_events(key=None, limit=200):
    limit = max(1, min(int(limit), 5000))
    with closing(connect()) as con:
        if key:
            rows = con.execute(
                "SELECT ts,key,source,kind,line FROM events WHERE key=? "
                "ORDER BY id DESC LIMIT ?", (key, limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT ts,key,source,kind,line FROM events "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def recent_audit(limit=200):
    limit = max(1, min(int(limit), 5000))
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT ts,actor,source,action,key,ok,stopped_peers,duration_ms,message "
            "FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def recent_alerts(limit=200):
    limit = max(1, min(int(limit), 5000))
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT ts,level,title,message,source FROM alerts "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def clear_alerts(keep_titles=None):
    """Delete alerts whose title is not in keep_titles (titles of currently-active
    problems). Returns (cleared, kept). Only resolved/historical alerts are removed."""
    keep = set(keep_titles or [])
    with closing(connect()) as con:
        rows = con.execute("SELECT id, title FROM alerts").fetchall()
        del_ids = [r["id"] for r in rows if r["title"] not in keep]
        if del_ids:
            con.executemany("DELETE FROM alerts WHERE id=?", [(i,) for i in del_ids])
        con.commit()
    return len(del_ids), len(rows) - len(del_ids)


def counts():
    with closing(connect()) as con:
        ev = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        ev_by_key = {r[0]: r[1] for r in con.execute(
            "SELECT key, COUNT(*) FROM events GROUP BY key")}
        au = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        al = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"db": str(DB_PATH), "db_bytes": size,
            "events": ev, "events_by_key": ev_by_key, "audit": au, "alerts": al}


# --------------------------------------------------------------------------- #
# 维护
# --------------------------------------------------------------------------- #
def prune(events_days=30, audit_days=180, alerts_days=90, max_events=200000):
    """删除过期事件/审计/告警，并限制 events 总量，避免无限增长。"""
    now = time.time()
    with closing(connect()) as con:
        deleted = con.execute("DELETE FROM events WHERE ts < ?",
                              (now - events_days * 86400,)).rowcount
        deleted += con.execute("DELETE FROM audit WHERE ts < ?",
                               (now - audit_days * 86400,)).rowcount
        deleted += con.execute("DELETE FROM alerts WHERE ts < ?",
                               (now - alerts_days * 86400,)).rowcount
        deleted += con.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (int(max_events),)).rowcount
        con.commit()
    return deleted
