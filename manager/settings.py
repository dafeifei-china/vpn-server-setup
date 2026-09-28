#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台配置加载：读取 config/platform.toml，缺失/出错时回退内置默认。

可用环境变量 VPNCTL_CONFIG 指定其它配置文件路径。
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

MANAGER_DIR = Path(__file__).resolve().parent
ROOT = MANAGER_DIR.parent
CONFIG_PATH = Path(os.environ.get("VPNCTL_CONFIG", str(ROOT / "config" / "platform.toml")))

DEFAULTS = {
    "health": {"interval": 30, "auto_restart": False,
               "restart_cooldown": 300, "dedup_seconds": 600},
    "certs": {"warn_days": 30},
    "alerts": {"desktop": True, "min_level": "warning", "webhook": ""},
}


def _merge(base, over):
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _merge(base[key], val)
        else:
            base[key] = val


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else CONFIG_PATH
    if p.exists():
        try:
            with open(p, "rb") as fh:
                _merge(cfg, tomllib.load(fh))
        except Exception:
            pass
    return cfg
