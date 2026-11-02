#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康检查：服务运行/端口监听/证书到期/证书与当前 IP 是否一致。

供 `vpnctl health|watch` 与网页面板使用。只读，不修改任何服务。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import vpnctl

_RUNNING = ("running", "active")


def _docker_status(target):
    r = vpnctl.docker(["inspect", "--format", "{{.State.Status}}", target])
    return "absent" if r.returncode != 0 else (r.stdout.strip() or "unknown")


def _systemd_status(unit):
    r = vpnctl._run(["systemctl", "is-active", unit])
    return r.stdout.strip() or "unknown"


def service_health(d, live):
    status = _docker_status(d["target"]) if d["kind"] == "docker" \
        else _systemd_status(d["target"])
    running = status in _RUNNING
    open_ports = ["%s/%s" % (pr, p) for pr, p in d["probes"] if (pr, p) in live]
    listening = (len(open_ports) == len(d["probes"])) if d["probes"] else running
    unhealthy = []
    if status == "failed":
        unhealthy.append("服务处于 failed 状态")
    if running and d["probes"] and not listening:
        missing = ["%s/%s" % (pr, p) for pr, p in d["probes"] if (pr, p) not in live]
        unhealthy.append("运行中但端口未监听：" + ",".join(missing))
    return {
        "key": d["key"], "title": d["title"], "kind": d["kind"],
        "target": d["target"], "status": status, "running": running,
        "listening": listening, "open_ports": open_ports, "unhealthy": unhealthy,
    }


def cert_health(key, path, ip):
    info = {"key": key, "path": str(path.relative_to(vpnctl.ROOT)),
            "exists": path.exists(), "cn": None, "san_ips": [],
            "days_left": None, "expires": None, "matches_ip": False}
    if not path.exists():
        return info
    info["cn"] = vpnctl.cert_cn(path)
    info["san_ips"] = vpnctl.cert_san_ips(path)
    info["matches_ip"] = vpnctl.cert_matches_ip(path, ip)
    out = vpnctl._run(["openssl", "x509", "-in", str(path),
                       "-noout", "-enddate"]).stdout.strip()
    if out.startswith("notAfter="):
        raw = out.split("=", 1)[1].strip()
        try:
            dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            info["expires"] = dt.isoformat()
            info["days_left"] = (dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            pass
    return info


def check_all():
    ip = vpnctl.server_ip()
    live = vpnctl.listeners()
    services = [service_health(d, live) for d in vpnctl.VPN_DEFS]
    certs = [cert_health(k, p, ip) for k, p in vpnctl.CERT_CHECKS]
    problems = []
    for s in services:
        problems += ["%s: %s" % (s["title"], u) for u in s["unhealthy"]]
    for c in certs:
        if c["days_left"] is not None and c["days_left"] <= 0:
            problems.append("%s 证书已过期" % c["key"])
        if not c["matches_ip"]:
            name = c["cn"] or (", ".join(c.get("san_ips") or []) or "无 CN/SAN")
            problems.append("%s 证书(CN/SAN=%s) 与当前 IP(%s) 不一致"
                            % (c["key"], name, ip))
    return {"ts": time.time(), "server_ip": ip, "services": services,
            "certs": certs, "problems": problems}
