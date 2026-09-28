#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警分发：始终落库；再按配置发桌面通知 / webhook。任何通道失败都不抛出。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request

import settings
import store

_LEVEL = {"info": 10, "warning": 20, "error": 30}


def _desktop(title, message, level):
    if not shutil.which("notify-send"):
        return
    env = dict(os.environ)
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        bus = "/run/user/%d/bus" % os.getuid()
        if os.path.exists(bus):
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus
    urgency = {"info": "normal", "warning": "normal", "error": "critical"}.get(level, "normal")
    try:
        subprocess.run(["notify-send", "-u", urgency, "-a", "VPN管控平台", title, message],
                       env=env, timeout=5, capture_output=True)
    except Exception:
        pass


def _webhook(url, level, title, message):
    data = json.dumps({"level": level, "title": title, "message": message},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def notify(level, title, message="", source="watchdog", cfg=None):
    try:
        store.record_alert(level, title, message, source)
    except Exception:
        pass
    c = cfg or settings.load()
    alerts = c.get("alerts", {})
    if _LEVEL.get(level, 20) < _LEVEL.get(alerts.get("min_level", "warning"), 20):
        return
    if alerts.get("desktop"):
        _desktop(title, message, level)
    url = (alerts.get("webhook") or "").strip()
    if url:
        _webhook(url, level, title, message)
