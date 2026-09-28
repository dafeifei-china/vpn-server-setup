#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vpnctl — 统一管理本机 7 套 VPN 服务端。

只读状态 + 生命周期（启停/切换/日志/自检）。不修改任何现有 recipe 文件，
仅调用 docker / systemctl / ss / openssl 读取与操作。可作为库被 web.py 复用。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

MANAGER_DIR = Path(__file__).resolve().parent
ROOT = MANAGER_DIR.parent

VPN_DEFS = [
    dict(
        key="openvpn", title="OpenVPN (TLS)", kind="docker",
        target="running-vpn-openvpn-tls", ports=["1194/udp"],
        probes=[("udp", 1194)], group=None,
        subnet="10.99.99.0/24", gateway="10.99.99.1",
        client="openvpn/local-server/clients/", docs="openvpn/OpenVPN使用指南.md",
        verify='docker logs --tail 20 running-vpn-openvpn-tls | grep "Initialization Sequence Completed"',
        note="证书认证；1194/udp 独享，可与其它任意 VPN 共存。",
        client_hint="客户端连上后服务端日志出现 `Peer Connection Initiated` 与 `MULTI: primary virtual IP for <client>`；客户端 `ip addr show tun0` 拿到 10.99.99.x。",
        clients_cmds=[
            ("最近连接日志", "docker",
             "logs --tail 400 running-vpn-openvpn-tls 2>&1 | grep -E 'Peer Connection Initiated|MULTI: primary virtual IP' | tail -20 || echo '（近期无连接记录）'"),
        ],
    ),
    dict(
        key="l2tp-ipsec", title="L2TP/IPsec", kind="docker",
        target="ipsec-vpn-server", ports=["500/udp", "4500/udp"],
        probes=[("udp", 500), ("udp", 4500)], group="ipsec500",
        subnet="192.168.42.0/24", gateway="192.168.42.1",
        client="L2TP_IPSEC/vpn.env", docs="L2TP_IPSEC/L2TP使用指南.md",
        verify="docker exec ipsec-vpn-server ipsec status",
        note="与 strongSwan / VPNC 抢 500,4500/udp，三选一；客户端勿勾选 MPPE。",
        client_hint="`ipsec status` 出现 ESTABLISHED、`ipsec trafficstatus` 有 `l2tp-psk` 会话；容器内出现 pppX 且地址 192.168.42.x。客户端拿到 192.168.42.x 即连到 L2TP/IPsec（若拿到 192.168.44.x 则是被裸 L2TP 抢答）。",
        clients_cmds=[
            ("IPsec 状态", "docker", "exec ipsec-vpn-server ipsec status 2>&1"),
            ("隧道流量", "docker", "exec ipsec-vpn-server ipsec trafficstatus 2>&1"),
            ("容器内 ppp 接口", "docker",
             "exec ipsec-vpn-server ip -4 addr show 2>&1 | grep -A3 'ppp' || echo '（无 ppp 接口，暂无客户端）'"),
        ],
    ),
    dict(
        key="l2tp-plain", title="L2TP (纯, 无 IPsec)", kind="systemd",
        target="xl2tpd.service", ports=["1701/udp"],
        probes=[("udp", 1701)], group=None,
        subnet="192.168.44.0/24", gateway="192.168.44.1",
        client="L2TP_PLAIN/", docs="L2TP_PLAIN/L2TP_PLAIN使用指南.md",
        verify="systemctl status xl2tpd; ss -ulnp | grep 1701",
        note="宿主机 xl2tpd+pppd（非容器）；1701/udp 与 L2TP/IPsec 内层冲突。",
        client_hint="宿主机出现 pppX 接口且地址 192.168.44.x（`ip -4 addr show | grep ppp`），`ps -ef | grep pppd` 有进程，`journalctl -u xl2tpd` 有 Call established。客户端拿到 192.168.44.x 即连到裸 L2TP（无 IPsec）。",
        clients_cmds=[
            ("xl2tpd 服务", "host", "systemctl status xl2tpd --no-pager 2>&1 | head -12"),
            ("宿主机 ppp 接口", "host",
             "ip -4 addr show 2>&1 | grep -A3 'ppp' || echo '（无 ppp 接口，暂无客户端）'"),
            ("pppd 进程", "host", "ps -ef 2>&1 | grep -E '[p]ppd' || echo '（无客户端进程）'"),
            ("最近日志", "host", "journalctl -q -u xl2tpd -n 40 --no-pager 2>&1 | tail -25"),
        ],
    ),
    dict(
        key="strongswan", title="strongSwan (IKEv2)", kind="docker",
        target="strongswan-server", ports=["500/udp", "4500/udp"],
        probes=[("udp", 500), ("udp", 4500)], group="ipsec500",
        subnet="10.10.10.0/24", gateway="10.10.10.1",
        client="Strongswan/clients/", docs="Strongswan/STRONGSWAN使用指南.md",
        verify="docker exec strongswan-server ipsec statusall",
        note="服务器证书绑定 IP；与 L2TP/IPsec / VPNC 三选一。",
        client_hint="`ipsec statusall` 出现 `ikev2-cert[...]: ESTABLISHED` 并分配 10.10.10.x。strongSwan 是路由型隧道，没有 ppp/tun 接口。",
        clients_cmds=[
            ("IKEv2 状态", "docker", "exec strongswan-server ipsec statusall 2>&1"),
        ],
    ),
    dict(
        key="pptp", title="PPTP", kind="docker",
        target="pptp-server", ports=["1723/tcp", "GRE/47"],
        probes=[("tcp", 1723)], group=None,
        subnet="192.168.45.0/24", gateway="192.168.45.1",
        client="PPTP/", docs="PPTP/PPTP使用指南.md",
        verify="ss -tlnp | grep 1723",
        note="需 --net host --privileged（GRE 协议 47）；可与任意 VPN 共存。",
        client_hint="容器内出现 pppX 接口与 pppd 进程；客户端 `ppp0=192.168.45.x`。服务端 `docker exec pptp-server ip -4 addr` 可见对端地址。",
        clients_cmds=[
            ("pppd 进程", "docker",
             "exec pptp-server ps -ef 2>&1 | grep -E '[p]ppd|[p]ptpd' || echo '（无客户端进程）'"),
            ("容器内 ppp 接口", "docker",
             "exec pptp-server ip -4 addr show 2>&1 | grep -A3 'ppp' || echo '（无 ppp 接口，暂无客户端）'"),
            ("最近日志", "docker",
             "logs --tail 80 pptp-server 2>&1 | grep -iE 'pppd|pptpd|connect|peer' | tail -20 || echo '（无相关日志）'"),
        ],
    ),
    dict(
        key="openconnect", title="OpenConnect (ocserv)", kind="docker",
        target="openconnect-server", ports=["8443/tcp", "8443/udp"],
        probes=[("tcp", 8443)], group=None,
        subnet="192.168.46.0/24", gateway="192.168.46.1",
        client="OpenConnect/etc/ocserv/", docs="OpenConnect/OpenConnect使用指南.md",
        verify="docker exec openconnect-server occtl show status",
        note="证书认证；服务器证书绑定 IP；8443 独享，可共存。",
        client_hint="`occtl show users` 列出已登录用户（取自客户端证书 CN，默认 test）；客户端 `vpn0/tun0=192.168.46.x`。",
        clients_cmds=[
            ("在线用户 (occtl show users)", "docker",
             "exec openconnect-server occtl show users 2>&1 || echo '（无在线用户）'"),
            ("服务状态 (occtl show status)", "docker",
             "exec openconnect-server occtl show status 2>&1"),
        ],
    ),
    dict(
        key="vpnc", title="VPNC (Cisco IPSec)", kind="docker",
        target="vpnc-server", ports=["500/udp", "4500/udp"],
        probes=[("udp", 500), ("udp", 4500)], group="ipsec500",
        subnet="192.168.47.0/24", gateway="192.168.47.1",
        client="VPNC/clients/vpnc.conf", docs="VPNC/VPNC使用指南.md",
        verify="docker exec vpnc-server ipsec statusall | grep cisco-vpnc",
        note="IKEv1 Aggressive+PSK+XAUTH；与 strongSwan / L2TP/IPsec 三选一。",
        client_hint="`ipsec statusall` 出现 `cisco-vpnc` 且 Security Associations 不为 0；无 ping 网关，以客户端能否出网为准。客户端 `tun0=192.168.47.x`。",
        clients_cmds=[
            ("IPsec 状态 (cisco-vpnc)", "docker",
             "exec vpnc-server ipsec statusall 2>&1 | grep -A8 cisco-vpnc || echo '（未加载 cisco-vpnc 连接）'"),
            ("charon 日志", "docker",
             "exec vpnc-server sh -c 'tail -n 30 /var/log/charon.log 2>/dev/null' || echo '（无 charon.log）'"),
        ],
    ),
]

VPN_BY_KEY = {d["key"]: d for d in VPN_DEFS}

# 连接事件关键词：用于在实时日志里高亮「客户端连接/断开」等服务端事件。
# 注意：不要用进程名（xl2tpd/pppd/ocserv…），它们出现在该单元的每一行，会匹配全部日志。
LOG_EVENTS = {
    "openvpn": r"Peer Connection Initiated|MULTI: primary virtual IP|Initialization Sequence Completed|AUTH_FAILED|TLS: Initial packet|SIGUSR1|client-instance|client/",
    "l2tp-ipsec": r"ESTABLISHED|CHILD_SA|installed|Call established|IP address|Connect: ppp|Connection terminated|Terminating pppd|LCP terminated|CHAP|PAP|ppp0|AUTH|IKE_SA|deleting",
    "l2tp-plain": r"Call established|IP address|Connect: ppp|Connection terminated|Terminating pppd|LCP terminated|CHAP|PAP|ppp0|peer |AUTH",
    "strongswan": r"ESTABLISHED|CHILD_SA|IKE_SA|installed|deleting|AUTH",
    "pptp": r"Connect:|IP address|Connection terminated|Terminating pppd|LCP terminated|CHAP|PAP|ppp0|GRE|MPPE|peer ",
    "openconnect": r"login|connect|user|DTLS|session|authenticated|accepted|disconnect",
    "vpnc": r"CHILD_SA|IKE_SA|XAUTH|established|MODE_CFG|MODE_CONFIG|peer",
}

CERT_CHECKS = [
    ("strongswan", ROOT / "Strongswan/pki/certs/server.crt"),
    ("openconnect", ROOT / "OpenConnect/etc/ocserv/server.crt"),
]

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


def _result(key, action, r, stopped=None):
    res = {"key": key, "action": action, "stopped_peers": stopped or [],
           "ok": r.returncode == 0, "message": (r.stderr or r.stdout).strip()}
    if not res["ok"] and VPN_BY_KEY[key]["kind"] == "systemd" and os.geteuid() != 0:
        res["hint"] = ("宿主机 systemd 服务需要 root：请执行一次 "
                       "`sudo ./install-sudoers.sh` 授权，或以 root 运行面板。")
    return res


def up(key, stop_peers=True):
    d = VPN_BY_KEY[key]
    stopped = _stop_conflicts(d, stop_peers)
    return _result(key, "up", start_vpn(d), stopped)


def down(key):
    d = VPN_BY_KEY[key]
    return _result(key, "down", stop_vpn(d))


def restart(key, stop_peers=True):
    d = VPN_BY_KEY[key]
    stopped = _stop_conflicts(d, stop_peers)
    return _result(key, "restart", restart_vpn(d), stopped)


def enable(key, stop_peers=True):
    """Start and restore autostart (docker --restart=unless-stopped / systemctl enable)."""
    d = VPN_BY_KEY[key]
    stopped = _stop_conflicts(d, stop_peers)
    if d["kind"] == "docker":
        docker(["update", "--restart=unless-stopped", d["target"]])
        r = start_vpn(d)
    else:
        # SysV-backed units ignore `--now`, so enable and start explicitly.
        re = systemctl(["enable", d["target"]])
        rs = systemctl(["start", d["target"]])
        r = re if re.returncode != 0 else rs
    return _result(key, "enable", r, stopped)


def disable(key):
    """Stop and turn off autostart (docker --restart=no / systemctl disable)."""
    d = VPN_BY_KEY[key]
    if d["kind"] == "docker":
        r = stop_vpn(d)
        docker(["update", "--restart=no", d["target"]])
    else:
        # SysV-backed units ignore `--now`, so stop and disable explicitly.
        rs = systemctl(["stop", d["target"]])
        rd = systemctl(["disable", d["target"]])
        r = rs if rs.returncode != 0 else rd
    return _result(key, "disable", r)


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
                      "cn": cn, "matches_ip": cn == ip})
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
        print(f"  {c['key']:<12} CN={c['cn']}  [{flag}]  {c['path']}")
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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
