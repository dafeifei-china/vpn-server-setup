#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志采集器：跟随每套 VPN 的日志，解析连接事件并持久化。

- Docker 容器：`docker logs -f --tail 0 <target>`
- 宿主机单元：`journalctl -f -u <unit> -n 0`（必要时 `sudo -n`）
- 命中 `vpnctl.LOG_EVENTS[key]` 的行 → 作为「连接事件」写入 SQLite（可检索）
- 全部行 → 追加到 state/logs/<key>.jsonl（按大小轮转，做原始留档）
- 周期性调用 store.prune() 控制增长

纯标准库。可作为后台线程（web.py 内）或独立命令 `vpnctl collect` 运行；
独立运行用 state/collector.lock 加锁，避免与面板实例重复采集。
"""
from __future__ import annotations

import argparse
import atexit
import fcntl
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import store
import vpnctl

LOG_DIR = store.STATE_DIR / "logs"
LOCK_PATH = store.STATE_DIR / "collector.lock"

MAX_BYTES = 5 * 1024 * 1024   # 单个 jsonl 轮转阈值
BACKUPS = 3                   # 保留的轮转份数
IDLE_BACKOFF = 5              # 进程退出后的重试起始间隔（秒）


class _Rotating:
    """按大小轮转的 JSONL 写入器（每行一个 {"ts","line"}）。"""

    def __init__(self, path, max_bytes=MAX_BYTES, backups=BACKUPS):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self._fh = None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, line):
        if self._fh is None:
            self._open()
        self._fh.write(json.dumps({"ts": time.time(), "line": line},
                                  ensure_ascii=False) + "\n")
        self._fh.flush()
        if self._fh.tell() >= self.max_bytes:
            self._rotate()

    def _rotate(self):
        self._fh.close()
        self._fh = None
        for i in range(self.backups - 1, 0, -1):
            old = Path("%s.%d" % (self.path, i))
            if old.exists():
                old.replace(Path("%s.%d" % (self.path, i + 1)))
        if self.path.exists():
            self.path.replace(Path("%s.1" % self.path))

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


def _argv_for(d):
    if d["kind"] == "docker":
        return vpnctl.docker_argv(["logs", "--tail", "0", "-f", d["target"]])
    return vpnctl.journal_argv(d["target"], 0, follow=True)


def _handle(key, regex, rot, line, source):
    try:
        rot.write(line)
    except Exception:
        pass
    if regex and regex.search(line):
        try:
            store.record_event(key, line, source=source, kind="event")
        except Exception:
            pass


def _follow(key, regex, rot, stop, backoff=IDLE_BACKOFF, track=None):
    """跟随单个协议日志，进程退出后按指数退避重连。"""
    d = vpnctl.VPN_BY_KEY[key]
    source = "docker" if d["kind"] == "docker" else "journal"
    delay = backoff
    while not stop.is_set():
        try:
            started = time.time()
            proc = subprocess.Popen(_argv_for(d), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
        except Exception:
            stop.wait(delay)
            delay = min(delay * 2, 60)
            continue
        if track:
            track.add(proc)
        fd = proc.stdout.fileno()
        buf = b""
        try:
            while not stop.is_set():
                ready, _, _ = select.select([fd], [], [], 5)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    _handle(key, regex, rot,
                            raw.decode("utf-8", "replace").rstrip("\r"), source)
        except OSError:
            pass
        finally:
            if buf:
                _handle(key, regex, rot,
                        buf.decode("utf-8", "replace").rstrip("\r"), source)
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if track:
                track.discard(proc)
        # 立即退出（如容器已停止）时退避增长，避免每 5 秒反复拉起进程
        delay = min(delay * 2, 60) if time.time() - started < 3 else backoff
        stop.wait(delay)


class LogCollector:
    def __init__(self, keys=None):
        self.keys = list(keys) if keys else list(vpnctl.VPN_BY_KEY)
        self._stop = threading.Event()
        self._threads = []
        self._procs = set()
        self._procs_lock = threading.Lock()

    def add(self, proc):
        with self._procs_lock:
            self._procs.add(proc)

    def discard(self, proc):
        with self._procs_lock:
            self._procs.discard(proc)

    def start(self):
        store.STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            store.prune()
        except Exception:
            pass
        for key in self.keys:
            d = vpnctl.VPN_BY_KEY.get(key)
            if not d:
                continue
            pattern = vpnctl.LOG_EVENTS.get(key, "")
            regex = re.compile(pattern, re.I) if pattern else None
            rot = _Rotating(LOG_DIR / (key + ".jsonl"))
            th = threading.Thread(target=_follow,
                                  args=(key, regex, rot, self._stop),
                                  kwargs={"track": self},
                                  name="vpnlog-" + key, daemon=True)
            th.start()
            self._threads.append(th)
        threading.Thread(target=self._prune_loop, name="vpnlog-prune",
                         daemon=True).start()
        atexit.register(self.shutdown)

    def _prune_loop(self):
        while not self._stop.wait(6 * 3600):
            try:
                store.prune()
            except Exception:
                pass

    def shutdown(self):
        self._stop.set()
        with self._procs_lock:
            procs = list(self._procs)
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        for th in self._threads:
            th.join(timeout=7)

    def run_forever(self):
        self.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


def acquire_lock():
    """独占采集器锁；返回打开的句柄（需保持存活），已被占用则返回 None。"""
    store.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vpnctl collect",
                                 description="日志采集器（前台运行，Ctrl-C 停止）")
    ap.add_argument("keys", nargs="*", help="只采集指定协议 key（默认全部）")
    args = ap.parse_args(argv)
    lock = acquire_lock()
    if lock is None:
        print("已有采集器在运行（state/collector.lock）", file=sys.stderr)
        return 1
    print("日志采集器启动：连接事件落库，全部日志写入 state/logs/*.jsonl。Ctrl-C 停止。")
    LogCollector(args.keys or None).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
