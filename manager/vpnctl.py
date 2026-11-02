#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vpnctl — 统一管理本机 7 套 VPN 服务端。

只读状态 + 生命周期（启停/切换/日志/自检）。不修改任何现有 recipe 文件，
仅调用 docker / systemctl / ss / openssl 读取与操作。可作为库被 web.py 复用。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import store

MANAGER_DIR = Path(__file__).resolve().parent
ROOT = MANAGER_DIR.parent

# --------------------------------------------------------------------------- #
# 协议清单：唯一事实源在 config/protocols/*.toml。
# 启动时加载并构建与旧版一致的内存结构（VPN_DEFS/VPN_BY_KEY/LOG_EVENTS/
# CERT_CHECKS），其余代码与 web.py 无需感知清单来源。
# --------------------------------------------------------------------------- #
CONFIG_DIR = ROOT / "config" / "protocols"

# key -> 连接事件正则：实时日志里高亮「客户端连接/断开」等服务端事件。
# 注意：不要用进程名（xl2tpd/pppd/ocserv…），它们出现在该单元的每一行，会匹配全部日志。
LOG_EVENTS = {}
# [(key, Path)]：绑定 IP、需校验 CN 的服务器证书。
CERT_CHECKS = []


def _load_protocols():
    if not CONFIG_DIR.is_dir():
        sys.exit(f"缺少协议清单目录：{CONFIG_DIR}")
    defs = []
    _certs = {}
    for path in sorted(CONFIG_DIR.glob("*.toml")):
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            sys.exit(f"协议清单解析失败 {path}: {exc}")
        key = raw.get("key") or path.stem
        defs.append({
            "key": key,
            "title": raw.get("title", key),
            "kind": raw.get("kind", "docker"),
            "target": raw["target"],
            "ports": list(raw.get("ports", [])),
            "probes": [(p["proto"], int(p["port"])) for p in raw.get("probes", [])],
            "group": raw.get("group") or None,
            "subnet": raw.get("subnet", ""),
            "gateway": raw.get("gateway", ""),
            "client": raw.get("client", ""),
            "docs": raw.get("docs", ""),
            "verify": raw.get("verify", ""),
            "note": raw.get("note", ""),
            "client_hint": raw.get("client_hint", ""),
            "clients_cmds": [(c["label"], c["kind"], c["cmd"])
                             for c in raw.get("clients_cmds", [])],
            "order": int(raw.get("order", 0)),
        })
        if raw.get("log_events"):
            LOG_EVENTS[key] = raw["log_events"]
        if raw.get("cert_check"):
            _certs[key] = ROOT / raw["cert_check"]
    defs.sort(key=lambda d: (d["order"], d["key"]))
    for d in defs:
        if d["key"] in _certs:
            CERT_CHECKS.append((d["key"], _certs[d["key"]]))
    return defs


VPN_DEFS = _load_protocols()
VPN_BY_KEY = {d["key"]: d for d in VPN_DEFS}

DOCKER_MODE = None


def _run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _shell(cmd_str, timeout=60):
    return subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=timeout)


def docker_mode():
    global DOCKER_MODE
    if DOCKER_MODE is not None:
        return DOCKER_MODE
    if shutil.which("docker") and _shell("docker info >/dev/null 2>&1").returncode == 0:
        DOCKER_MODE = "plain"
    elif shutil.which("sudo") and _shell("sudo -n docker info >/dev/null 2>&1").returncode == 0:
        DOCKER_MODE = "sudo"
    elif shutil.which("sg") and _shell("sg docker -c 'docker info' >/dev/null 2>&1").returncode == 0:
        DOCKER_MODE = "sg"
    else:
        DOCKER_MODE = "none"
    return DOCKER_MODE


def docker(args, timeout=60):
    s = " ".join(shlex.quote(a) for a in args)
    mode = docker_mode()
    if mode == "plain":
        return _shell("docker " + s, timeout)
    if mode == "sudo":
        return _shell("sudo -n docker " + s, timeout)
    if mode == "sg":
        return _shell("sg docker -c " + shlex.quote("docker " + s), timeout)
    return subprocess.CompletedProcess(args, 127, "", "docker 不可用（需 docker 组或 root）")


def docker_shell(pipeline, timeout=60):
    """Run a shell pipeline as if it were prefixed with the docker command.

    Unlike docker(), this accepts `|`/`&&` etc. (used for client/status probes).
    """
    mode = docker_mode()
    if mode == "none":
        return subprocess.CompletedProcess([], 127, "", "docker 不可用（需 docker 组或 root）")
    if mode == "sg":
        return _shell("sg docker -c " + shlex.quote("docker " + pipeline), timeout)
    prefix = "docker " if mode == "plain" else "sudo -n docker "
    return _shell(prefix + pipeline, timeout)


def systemctl(args, timeout=120):
    """Run a (mutating) systemctl command, escalating to sudo when not root.

    Host units like xl2tpd are SysV-backed: enable/disable write /etc/init.d
    symlinks and need root. Try passwordless sudo first (see install-sudoers.sh),
    then fall back to a direct call (works for polkit-allowed start/stop).
    """
    cmd = ["systemctl"] + args
    if os.geteuid() != 0 and shutil.which("sudo"):
        r = _run(["sudo", "-n"] + cmd, timeout=timeout)
        if r.returncode == 0:
            return r
    return _run(cmd, timeout=timeout)


def docker_argv(args):
    """argv to run docker with the detected access mode (for Popen / streaming)."""
    mode = docker_mode()
    if mode == "sudo":
        return ["sudo", "-n", "docker"] + args
    if mode == "sg":
        return ["sg", "docker", "-c", " ".join(shlex.quote(a) for a in (["docker"] + args))]
    return ["docker"] + args


_JOURNAL_SUDO = None


def journal_sudo_ok(unit):
    """Whether we can read the system journal via passwordless sudo (see install-sudoers.sh)."""
    global _JOURNAL_SUDO
    if _JOURNAL_SUDO is None:
        if os.geteuid() == 0:
            _JOURNAL_SUDO = False
        else:
            r = _run(["sudo", "-n", "journalctl", "-u", unit, "-n", "1", "--no-pager"], timeout=15)
            _JOURNAL_SUDO = r.returncode == 0
    return _JOURNAL_SUDO


def journal_argv(unit, tail, follow=False):
    """argv to read a host unit's journal, escalating to sudo when the user can't read it."""
    cmd = ["journalctl", "-u", unit, "-n", str(tail), "--no-pager"]
    if follow:
        cmd.append("-f")
    if journal_sudo_ok(unit):
        return ["sudo", "-n"] + cmd
    return cmd


def run_probe(spec, timeout=30):
    """Run a (label, kind, cmd) probe where kind is 'docker' or 'host'."""
    label, kind, cmd = spec
    try:
        if kind == "docker":
            r = docker_shell(cmd, timeout=timeout)
        else:
            r = _shell(cmd, timeout=timeout)
    except Exception as e:
        return label, "（执行失败：%s）" % e
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return label, out or "（无输出）"


def server_ip():
    try:
        out = _run(["ip", "route", "get", "1.1.1.1"]).stdout
        m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        out = _run(["hostname", "-I"]).stdout.split()
        return out[0] if out else "unknown"
    except Exception:
        return "unknown"


def listeners():
    result = set()
    for proto, flag in (("udp", "-u"), ("tcp", "-t")):
        try:
            out = _run(["ss", "-H", "-l", "-n", flag]).stdout
        except Exception:
            continue
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[-2]
            try:
                port = int(local.rsplit(":", 1)[1])
            except (ValueError, IndexError):
                continue
            result.add((proto, port))
    return result


def cert_cn(path):
    if not path.exists():
        return None
    try:
        out = _run(["openssl", "x509", "-in", str(path), "-noout", "-subject"]).stdout
    except Exception:
        return None
    m = re.search(r"CN\s*=\s*([^\s,/]+)", out)
    return m.group(1) if m else None


def cert_san_ips(path):
    """IP entries in a certificate's subjectAltName, e.g. ['10.20.51.22']."""
    if not path.exists():
        return []
    try:
        out = _run(["openssl", "x509", "-in", str(path), "-noout", "-ext", "subjectAltName"]).stdout
    except Exception:
        return []
    return re.findall(r"IP Address:([0-9A-Fa-f:.]+)", out)


def cert_matches_ip(path, ip):
    """True if the cert is valid for `ip` via subject CN or a SAN IP entry.

    Modern certs (e.g. OpenConnect's) may carry the IP only in SAN, not CN.
    """
    if cert_cn(path) == ip:
        return True
    return ip in cert_san_ips(path)


def vpn_state(d):
    st = {
        "key": d["key"], "title": d["title"], "kind": d["kind"],
        "target": d["target"], "ports": d["ports"], "subnet": d["subnet"],
        "gateway": d["gateway"], "group": d["group"], "docs": d["docs"],
        "client": d["client"], "note": d["note"], "verify": d["verify"],
        "status": "unknown", "running": False, "listening": False, "probes_open": [],
    }
    if d["kind"] == "docker":
        r = docker(["inspect", "--format", "{{.State.Status}}", d["target"]])
        if r.returncode != 0:
            st["status"] = "absent"
        else:
            st["status"] = (r.stdout.strip() or "unknown")
    else:
        r = _run(["systemctl", "is-active", d["target"]])
        st["status"] = (r.stdout.strip() or "unknown")
    st["running"] = st["status"] in ("running", "active")
    live = listeners()
    st["probes_open"] = [f"{p}/{pr}" for pr, p in d["probes"] if (pr, p) in live]
    st["listening"] = len(st["probes_open"]) == len(d["probes"])
    if not st["running"]:
        st["probes_open"] = []
        st["listening"] = False
    return st


def all_states():
    states = [vpn_state(d) for d in VPN_DEFS]
    for d, s in zip(VPN_DEFS, states):
        s["conflict"] = conflict_info(d)
    return states


def group_members(group):
    return [d for d in VPN_DEFS if d["group"] == group]


def conflict_info(d):
    """Which same-group (500/4500) peers are running, i.e. real port conflicts."""
    if not d["group"]:
        return {"group": None, "peers": [], "running": []}
    peers = [p for p in VPN_DEFS if p["group"] == d["group"] and p["key"] != d["key"]]
    running = [p["title"] for p in peers if vpn_state(p)["running"]]
    return {"group": d["group"], "peers": [p["title"] for p in peers], "running": running}


def detail(key):
    d = VPN_BY_KEY[key]
    st = vpn_state(d)
    st["conflict"] = conflict_info(d)
    st["client_hint"] = d.get("client_hint", "")
    st["log_events"] = LOG_EVENTS.get(key, "")
    return st


def raw_status(key):
    """Human-readable status dump for the panel's '查看状态' button."""
    d = VPN_BY_KEY[key]
    out = []
    if d["kind"] == "docker":
        r = docker(["ps", "-a", "--filter", "name=%s" % d["target"],
                    "--format", "{{.Status}}"])
        out.append("$ docker ps -a --filter name=%s\n%s"
                   % (d["target"], r.stdout.strip() or "（未找到容器）"))
        r = docker(["logs", "--tail", "15", d["target"]])
        out.append("$ docker logs --tail 15 %s\n%s"
                   % (d["target"], ((r.stdout or "") + (r.stderr or "")).strip() or "（无日志）"))
    else:
        r = _run(["systemctl", "status", d["target"], "--no-pager"])
        out.append("$ systemctl status %s\n%s"
                   % (d["target"], ((r.stdout or "") + (r.stderr or "")).strip() or "（无输出）"))
    return "\n\n".join(out)


def clients(key):
    """Per-VPN probe output: how to tell a client is connected to this VPN."""
    d = VPN_BY_KEY[key]
    chunks = []
    for spec in d.get("clients_cmds", []):
        label, out = run_probe(spec)
        chunks.append("$ %s\n%s" % (label, out))
    return "\n\n".join(chunks)


def start_vpn(d):
    if d["kind"] == "docker":
        return docker(["start", d["target"]], timeout=120)
    return systemctl(["start", d["target"]])


def stop_vpn(d):
    if d["kind"] == "docker":
        return docker(["stop", d["target"]], timeout=120)
    return systemctl(["stop", d["target"]])


def restart_vpn(d):
    if d["kind"] == "docker":
        return docker(["restart", d["target"]], timeout=120)
    return systemctl(["restart", d["target"]])


def _stop_conflicts(d, stop_peers=True):
    stopped = []
    if d["group"] and stop_peers:
        for p in group_members(d["group"]):
            if p["key"] == d["key"]:
                continue
            if vpn_state(p)["running"]:
                stop_vpn(p)
                stopped.append(p["title"])
    return stopped


def _default_actor():
    try:
        return os.environ.get("SUDO_USER") or getpass.getuser()
    except Exception:
        return "unknown"


def _result(key, action, r, stopped=None, duration_ms=None, actor=None, source="cli"):
    res = {"key": key, "action": action, "stopped_peers": stopped or [],
           "ok": r.returncode == 0, "message": (r.stderr or r.stdout).strip()}
    if not res["ok"] and VPN_BY_KEY[key]["kind"] == "systemd" and os.geteuid() != 0:
        res["hint"] = ("宿主机 systemd 服务需要 root：请执行一次 "
                       "`sudo ./install-sudoers.sh` 授权，或以 root 运行面板。")
    try:
        store.record_audit(action=action, key=key, ok=res["ok"],
                           actor=actor or _default_actor(), source=source,
                           stopped_peers=stopped, duration_ms=duration_ms,
                           message=res.get("message"))
    except Exception:
        pass
    return res


def up(key, stop_peers=True, actor=None, source="cli"):
    d = VPN_BY_KEY[key]
    t0 = time.time()
    stopped = _stop_conflicts(d, stop_peers)
    return _result(key, "up", start_vpn(d), stopped,
                   int((time.time() - t0) * 1000), actor, source)


def down(key, actor=None, source="cli"):
    d = VPN_BY_KEY[key]
    t0 = time.time()
    return _result(key, "down", stop_vpn(d),
                   duration_ms=int((time.time() - t0) * 1000), actor=actor, source=source)


def down_all(actor=None, source="cli"):
    """Stop every currently-running VPN; return the per-service results (audited individually)."""
    return [down(d["key"], actor=actor, source=source)
            for d in VPN_DEFS if vpn_state(d)["running"]]


def restart(key, stop_peers=True, actor=None, source="cli"):
    d = VPN_BY_KEY[key]
    t0 = time.time()
    stopped = _stop_conflicts(d, stop_peers)
    return _result(key, "restart", restart_vpn(d), stopped,
                   int((time.time() - t0) * 1000), actor, source)


def enable(key, stop_peers=True, actor=None, source="cli"):
    """Start and restore autostart (docker --restart=unless-stopped / systemctl enable)."""
    d = VPN_BY_KEY[key]
    t0 = time.time()
    stopped = _stop_conflicts(d, stop_peers)
    if d["kind"] == "docker":
        docker(["update", "--restart=unless-stopped", d["target"]])
        r = start_vpn(d)
    else:
        # SysV-backed units ignore `--now`, so enable and start explicitly.
        re = systemctl(["enable", d["target"]])
        rs = systemctl(["start", d["target"]])
        r = re if re.returncode != 0 else rs
    return _result(key, "enable", r, stopped,
                   int((time.time() - t0) * 1000), actor, source)


def disable(key, actor=None, source="cli"):
    """Stop and turn off autostart (docker --restart=no / systemctl disable)."""
    d = VPN_BY_KEY[key]
    t0 = time.time()
    if d["kind"] == "docker":
        r = stop_vpn(d)
        docker(["update", "--restart=no", d["target"]])
    else:
        # SysV-backed units ignore `--now`, so stop and disable explicitly.
        rs = systemctl(["stop", d["target"]])
        rd = systemctl(["disable", d["target"]])
        r = rs if rs.returncode != 0 else rd
    return _result(key, "disable", r,
                   duration_ms=int((time.time() - t0) * 1000), actor=actor, source=source)


def _journalctl(unit, tail, timeout=60):
    """Read a host unit's journal, escalating to sudo when the user can't read it."""
    cmd = ["journalctl", "-u", unit, "-n", str(tail), "--no-pager"]
    if os.geteuid() != 0 and shutil.which("sudo"):
        r = _run(["sudo", "-n"] + cmd, timeout=timeout)
        if r.returncode == 0:
            return r
    return _run(cmd, timeout=timeout)


def logs(key, tail=40):
    d = VPN_BY_KEY[key]
    if d["kind"] == "docker":
        r = docker(["logs", "--tail", str(tail), d["target"]], timeout=60)
    else:
        r = _journalctl(d["target"], tail)
    return (r.stdout or "") + (r.stderr or "")


def info(key):
    d = VPN_BY_KEY[key]
    st = vpn_state(d)
    return {
        "key": key, "title": d["title"], "kind": d["kind"], "status": st["status"],
        "server_ip": server_ip(), "ports": d["ports"], "subnet": d["subnet"],
        "gateway": d["gateway"], "client_hint": d["client"],
        "docs": d["docs"], "verify": d["verify"], "note": d["note"],
        "client_hint": d.get("client_hint", ""), "conflict": conflict_info(d),
        "credentials": "默认 test/test；其它凭据见文档与客户端配置目录（不在此显示）。",
    }


def doctor():
    ip = server_ip()
    mode = docker_mode()
    conflicts = []
    for group in {d["group"] for d in VPN_DEFS if d["group"]}:
        running = [p["title"] for p in group_members(group) if vpn_state(p)["running"]]
        if len(running) > 1:
            conflicts.append({"group": group, "running": running})
    certs = []
    for key, path in CERT_CHECKS:
        cn = cert_cn(path)
        certs.append({"key": key, "path": str(path.relative_to(ROOT)),
                      "cn": cn, "sans": cert_san_ips(path),
                      "matches_ip": cert_matches_ip(path, ip)})
    return {"docker_mode": mode, "server_ip": ip, "conflicts": conflicts,
            "certs": certs, "states": all_states()}


def _fmt_table(rows, headers):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(out)


STATUS_ICON = {"running": "UP", "active": "UP", "exited": "DOWN",
               "created": "DOWN", "inactive": "DOWN", "absent": "N/A",
               "failed": "ERR", "unknown": "??"}


def cmd_list(args):
    states = all_states()
    if args.json:
        print(json.dumps({"server_ip": server_ip(), "vpns": states},
                         ensure_ascii=False, indent=2))
        return 0
    rows = []
    for s in states:
        rows.append([s["key"], STATUS_ICON.get(s["status"], s["status"]),
                     s["status"], ",".join(s["ports"]), s["subnet"],
                     s["group"] or "-"])
    print(f"服务器 IP: {server_ip()}    docker: {docker_mode()}\n")
    print(_fmt_table(rows, ["KEY", "状态", "原始", "端口", "网段", "互斥组"]))
    return 0


def cmd_status(args):
    s = detail(args.name)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    c = s["conflict"]
    print(f"{s['title']}  [{s['key']}]")
    print(f"  类型/目标 : {s['kind']} / {s['target']}")
    print(f"  状态      : {s['status']}  (端口监听: {'是' if s['listening'] else '否'} {','.join(s['probes_open']) or '-'})")
    print(f"  端口/网段 : {','.join(s['ports'])}   {s['subnet']}  网关 {s['gateway']}")
    if c["group"]:
        print(f"  互斥组    : {c['group']}（同组: {', '.join(c['peers'])}）")
        if c["running"]:
            print(f"  冲突警告  : 以下同组服务正在运行，会抢占 500/4500: {', '.join(c['running'])}")
    else:
        print(f"  互斥组    : 无（可与任意 VPN 共存）")
    print(f"  文档      : {s['docs']}")
    print(f"  说明      : {s['note']}")
    print(f"  客户端判断: {s.get('client_hint') or '-'}")
    return 0


def cmd_up(args):
    res = up(args.name, stop_peers=not args.keep_conflicts)
    _print_action(res)
    return 0 if res["ok"] else 1


def cmd_down(args):
    res = down(args.name)
    _print_action(res)
    return 0 if res["ok"] else 1


def cmd_restart(args):
    res = restart(args.name, stop_peers=not args.keep_conflicts)
    _print_action(res)
    return 0 if res["ok"] else 1


def cmd_enable(args):
    res = enable(args.name, stop_peers=not args.keep_conflicts)
    _print_action(res)
    return 0 if res["ok"] else 1


def cmd_disable(args):
    res = disable(args.name)
    _print_action(res)
    return 0 if res["ok"] else 1


def cmd_clients(args):
    print(clients(args.name))
    return 0


def cmd_raw(args):
    print(raw_status(args.name))
    return 0


def _print_action(res):
    for t in res.get("stopped_peers", []):
        print(f"[互斥] 已停止: {t}")
    tag = "OK" if res["ok"] else "FAIL"
    print(f"[{tag}] {res['action']} {res['key']}")
    if res.get("message"):
        print(res["message"])


def cmd_logs(args):
    print(logs(args.name, args.tail))
    return 0


def cmd_info(args):
    print(json.dumps(info(args.name), ensure_ascii=False, indent=2))
    return 0


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def cmd_events(args):
    rows = store.recent_events(args.name, args.tail)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（无连接事件记录）")
        return 0
    for r in rows:
        print(f"{_fmt_ts(r['ts'])}  {r['key']:<12} [{r['source']:<7}] {r['line']}")
    return 0


def cmd_audit(args):
    rows = store.recent_audit(args.tail)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（无审计记录）")
        return 0
    for r in rows:
        tag = "OK" if r["ok"] else "FAIL"
        extra = ""
        if r["stopped_peers"]:
            extra += " stopped=" + r["stopped_peers"]
        if r["duration_ms"] is not None:
            extra += f" {r['duration_ms']}ms"
        print(f"{_fmt_ts(r['ts'])}  {tag:<4} {r['action']:<8} "
              f"{(r['key'] or '-'):<12} {(r['actor'] or '-'):<16} "
              f"({r['source']}){extra}")
        if not r["ok"] and r["message"]:
            print("        " + r["message"].splitlines()[0])
    return 0


def cmd_collect(args):
    import collector
    return collector.main(args.keys)


def cmd_prune(args):
    n = store.prune(events_days=args.events_days, audit_days=args.audit_days)
    print(f"已清理 {n} 条过期记录")
    return 0


def cmd_health(args):
    import health
    data = health.check_all()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"服务器 IP : {data['server_ip']}\n")
    rows = [[s["key"], s["title"], s["status"],
             "UP" if s["running"] else "DOWN",
             "是" if s["listening"] else "否",
             "；".join(s["unhealthy"]) or "-"] for s in data["services"]]
    print(_fmt_table(rows, ["KEY", "名称", "原始", "运行", "端口", "异常"]))
    if data["certs"]:
        print("\n绑定 IP 的服务器证书:")
        for c in data["certs"]:
            dl = "?" if c["days_left"] is None else f"{c['days_left']:.0f} 天"
            flag = "IP 匹配" if c["matches_ip"] else "IP 不一致(需重签)"
            name = c["cn"] or (",".join(c.get("san_ips") or []) or "-")
            print(f"  {c['key']:<12} CN/SAN={name}  剩余 {dl}  [{flag}]  {c['path']}")
    print("\n问题:")
    if not data["problems"]:
        print("  无")
    for p in data["problems"]:
        print(f"  - {p}")
    return 0


def cmd_alerts(args):
    rows = store.recent_alerts(args.tail)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("（无告警记录）")
        return 0
    for r in rows:
        print(f"{_fmt_ts(r['ts'])}  {r['level'].upper():<7} {r['title']}  {r['message']}")
    return 0


def cmd_watch(args):
    import watchdog
    argv = []
    if args.once:
        argv.append("--once")
    if args.interval:
        argv += ["--interval", str(args.interval)]
    argv += list(args.keys or [])
    return watchdog.main(argv)


def cmd_ip(args):
    print(server_ip())
    return 0


def cmd_doctor(args):
    doc = doctor()
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    print(f"docker 访问 : {doc['docker_mode']}")
    print(f"服务器 IP   : {doc['server_ip']}")
    print("\n互斥组冲突检查:")
    if not doc["conflicts"]:
        print("  无冲突")
    for c in doc["conflicts"]:
        print(f"  [冲突] {c['group']}: {', '.join(c['running'])} 同时运行（应只留一个）")
    print("\n绑定 IP 的服务器证书:")
    for c in doc["certs"]:
        flag = "OK" if c["matches_ip"] else "不一致(需重签)"
        sans = ",".join(c.get("sans") or []) or "-"
        print(f"  {c['key']:<12} CN={c['cn'] or '-'}  SAN={sans}  [{flag}]  {c['path']}")
    print("\n各服务状态:")
    rows = [[s["key"], STATUS_ICON.get(s["status"], s["status"]), s["status"],
             ",".join(s["probes_open"]) or "-"] for s in doc["states"]]
    print(_fmt_table(rows, ["KEY", "状态", "原始", "已监听端口"]))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="vpnctl", description="统一管理本机 7 套 VPN 服务端")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="列出所有 VPN 及状态")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_list)

    for name, fn, extra in (
        ("status", cmd_status, None),
        ("up", cmd_up, "keep_conflicts"),
        ("down", cmd_down, None),
        ("restart", cmd_restart, "keep_conflicts"),
        ("enable", cmd_enable, "keep_conflicts"),
        ("disable", cmd_disable, None),
        ("clients", cmd_clients, None),
        ("raw", cmd_raw, None),
        ("logs", cmd_logs, "tail"),
        ("info", cmd_info, None),
    ):
        sp = sub.add_parser(name, help=f"{name} <key>")
        sp.add_argument("name", choices=list(VPN_BY_KEY))
        if name == "status":
            sp.add_argument("--json", action="store_true")
        if extra == "keep_conflicts":
            sp.add_argument("--keep-conflicts", action="store_true",
                            help="不自动停止互斥组内其它服务")
        if extra == "tail":
            sp.add_argument("--tail", type=int, default=40)
        sp.set_defaults(func=fn)

    sp = sub.add_parser("ip", help="显示当前服务器 IP")
    sp.set_defaults(func=cmd_ip)

    sp = sub.add_parser("doctor", help="自检：docker/冲突/证书/端口")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("events", help="查看持久化的连接事件（默认全部协议）")
    sp.add_argument("name", nargs="?", choices=list(VPN_BY_KEY), help="只看某个协议")
    sp.add_argument("--tail", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("audit", help="查看管理操作审计（谁在何时做了什么）")
    sp.add_argument("--tail", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("collect", help="前台运行日志采集器（落库 + JSONL 留档）")
    sp.add_argument("keys", nargs="*", help="只采集指定协议 key（默认全部）")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("prune", help="清理过期的连接事件/审计记录")
    sp.add_argument("--events-days", type=int, default=30)
    sp.add_argument("--audit-days", type=int, default=180)
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("health", help="健康检查：服务/端口/证书到期/IP 绑定")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("alerts", help="查看守护进程产生的告警")
    sp.add_argument("--tail", type=int, default=50)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_alerts)

    sp = sub.add_parser("watch", help="前台运行健康守护（周期检查 + 告警）")
    sp.add_argument("--once", action="store_true", help="只检查一次并打印 JSON")
    sp.add_argument("--interval", type=int, help="覆盖检查间隔（秒）")
    sp.add_argument("keys", nargs="*", help="只守护指定协议 key（默认全部）")
    sp.set_defaults(func=cmd_watch)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
