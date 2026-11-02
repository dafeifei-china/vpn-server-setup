#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康守护：周期检查 → 状态变化/证书/告警，并可选对 failed 服务自动重启。

- 状态迁移（启动/停止）与健康异常 → 告警（同类去重）
- 服务器 IP 变化、证书到期、证书与 IP 不一致 → 告警
- 上次状态/IP 持久化在 SQLite `meta`，跨进程重启仍能识别变化
- 独立运行用 state/watchdog.lock 加锁，避免与面板实例重复告警

可作为后台线程（web.py 内）或 `vpnctl watch` 前台运行。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path

import health
import notify
import settings
import store
import vpnctl


def active_alert_titles(data=None, cfg=None):
    """Titles of alerts that reflect currently-active problems (must NOT be cleared).

    Mirrors the exact title strings emitted in Watchdog._check_services/_check_certs.
    Resolved problems (e.g. a cert that now matches the IP) drop out, so their
    historical alerts become clearable.
    """
    data = data if data is not None else health.check_all()
    cfg = cfg or settings.load()
    warn = int(cfg["certs"].get("warn_days", 30))
    active = set()
    for c in data.get("certs", []):
        if c.get("matches_ip") is False:
            active.add("%s 证书与当前 IP 不一致" % c["key"])
        if c.get("days_left") is not None and c["days_left"] <= warn:
            active.add("%s 证书即将到期" % c["key"])
    for s in data.get("services", []):
        if s.get("unhealthy"):
            active.add("%s 健康异常" % s["title"])
    return active


class Watchdog(threading.Thread):
    def __init__(self, cfg=None, keys=None):
        super().__init__(name="vpn-watchdog", daemon=True)
        self.cfg = cfg or settings.load()
        self.keys = set(keys) if keys else None
        self._stop = threading.Event()
        self._dedup = {}
        self.last = None

    def _once(self, tag, seconds):
        now = time.time()
        if now - self._dedup.get(tag, 0) < seconds:
            return False
        self._dedup[tag] = now
        return True

    def _sel(self, seq):
        return [x for x in seq if not self.keys or x["key"] in self.keys]

    def tick(self):
        data = health.check_all()
        self.last = data
        self._check_ip(data)
        self._check_services(data)
        self._check_certs(data)
        return data

    def _check_ip(self, data):
        ip = data["server_ip"]
        prev = store.get_meta("watch.ip")
        if prev and prev != ip:
            notify.notify("warning", "服务器 IP 已变化",
                          "%s → %s；strongSwan/OpenConnect 服务器证书绑定 IP，"
                          "客户端配置也需同步更新。" % (prev, ip),
                          source="watchdog", cfg=self.cfg)
        store.set_meta("watch.ip", ip)

    def _check_services(self, data):
        h = self.cfg["health"]
        dedup = int(h.get("dedup_seconds", 600))
        auto = bool(h.get("auto_restart", False))
        cooldown = int(h.get("restart_cooldown", 300))
        for s in self._sel(data["services"]):
            prev = store.get_meta("watch.status." + s["key"])
            cur = s["status"]
            if prev is not None and prev != cur:
                if s["running"]:
                    notify.notify("info", "%s 已启动" % s["title"],
                                  "状态 %s → %s" % (prev, cur), cfg=self.cfg)
                else:
                    notify.notify("warning", "%s 已停止" % s["title"],
                                  "状态 %s → %s" % (prev, cur), cfg=self.cfg)
            store.set_meta("watch.status." + s["key"], cur)
            if s["unhealthy"]:
                if self._once("unhealthy." + s["key"], dedup):
                    notify.notify("warning", "%s 健康异常" % s["title"],
                                  "；".join(s["unhealthy"]), cfg=self.cfg)
                if auto and cur == "failed":
                    last = float(store.get_meta("watch.restart." + s["key"], 0) or 0)
                    if time.time() - last >= cooldown:
                        store.set_meta("watch.restart." + s["key"], time.time())
                        res = vpnctl.restart(s["key"], stop_peers=False,
                                             actor="watchdog", source="watchdog")
                        notify.notify("info" if res.get("ok") else "error",
                                      "自动重启 %s" % s["title"],
                                      "成功" if res.get("ok") else res.get("message", "失败"),
                                      cfg=self.cfg)

    def _check_certs(self, data):
        warn = int(self.cfg["certs"].get("warn_days", 30))
        today = time.strftime("%Y-%m-%d")
        for c in self._sel(data["certs"]):
            if not c.get("exists"):
                continue
            dl = c.get("days_left")
            if dl is not None and dl <= warn:
                if store.get_meta("watch.certalert." + c["key"]) != today:
                    level = "error" if dl <= 7 else "warning"
                    notify.notify(level, "%s 证书即将到期" % c["key"],
                                  "%s 剩余 %d 天（CN=%s）" % (c["path"], int(dl), c["cn"]),
                                  cfg=self.cfg)
                    store.set_meta("watch.certalert." + c["key"], today)
            if not c.get("matches_ip") and self._once("certip." + c["key"], 3600):
                notify.notify("warning", "%s 证书与当前 IP 不一致" % c["key"],
                              "CN=%s 当前 IP=%s；需重签服务器证书（见 vpnctl doctor）"
                              % (c["cn"], data["server_ip"]), cfg=self.cfg)

    def run(self):
        interval = max(5, int(self.cfg["health"].get("interval", 30)))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._stop.wait(interval)

    def shutdown(self):
        self._stop.set()


def acquire_lock(path=None):
    p = Path(path) if path else (store.STATE_DIR / "watchdog.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run_once(cfg=None, keys=None):
    return Watchdog(cfg or settings.load(), keys=keys).tick()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpnctl watch", description="健康守护（Ctrl-C 停止）")
    ap.add_argument("--once", action="store_true", help="只检查一次并打印 JSON 后退出")
    ap.add_argument("--interval", type=int, help="覆盖检查间隔（秒）")
    ap.add_argument("keys", nargs="*", help="只守护指定协议 key（默认全部）")
    args = ap.parse_args(argv)
    cfg = settings.load()
    if args.interval:
        cfg["health"]["interval"] = args.interval
    if args.once:
        print(json.dumps(run_once(cfg, keys=args.keys or None),
                         ensure_ascii=False, indent=2))
        return 0
    lock = acquire_lock()
    if lock is None:
        print("已有守护进程在运行（state/watchdog.lock）", file=sys.stderr)
        return 1
    wd = Watchdog(cfg, keys=args.keys or None)
    print("健康守护启动：每 %ss 检查一次。Ctrl-C 停止。" % cfg["health"]["interval"])
    wd.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        wd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
