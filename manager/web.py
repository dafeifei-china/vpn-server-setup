#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vpnctl 网页面板（B 层）— 仅调用 vpnctl.py，不修改任何 recipe 文件。

默认绑定 127.0.0.1:8787，打开网页即可使用（默认无需登录/令牌）。
左侧导航：数据看板 + 每套 VPN 独立页面（状态 / 配置 / 实时日志）。
写操作（启停/重启/启用禁用）默认放行；如需令牌用 --auth 打开，--read-only 彻底只读。
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import vpnctl
import health

TOKEN_FILE = vpnctl.MANAGER_DIR / ".token"
WEB_SERVICE = "vpn-manager-web.service"
READ_ONLY = False
REQUIRE_TOKEN = False  # 默认本机开放（只绑 127.0.0.1）；如需令牌用 --auth 打开

_HEALTH_CACHE = {"ts": 0.0, "data": None}


def health_cached(ttl=5):
    now = time.time()
    if _HEALTH_CACHE["data"] is None or now - _HEALTH_CACHE["ts"] >= ttl:
        _HEALTH_CACHE["data"] = health.check_all()
        _HEALTH_CACHE["ts"] = now
    return _HEALTH_CACHE["data"]


def load_token():
    env = os.environ.get("VPNCTL_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(t)
    os.chmod(TOKEN_FILE, 0o600)
    return t


TOKEN = None


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VPN 管控平台</title>
<style>
  :root {
    --sb-bg:#1b2a41; --sb-bg2:#142033; --sb-fg:#c7d3e3; --sb-dim:#7d8ca3;
    --sb-hover:#24395a; --sb-active:#2f7ed8;
    --bg:#eef2f7; --card:#ffffff; --line:#e5eaf1; --fg:#1f2d3d; --mut:#7a8699;
    --acc:#2f7ed8; --up:#1fae63; --down:#9aa5b5; --err:#e5484d; --warn:#d99a2b;
    --shadow:0 1px 2px rgba(16,24,40,.05),0 2px 8px rgba(16,24,40,.05);
    --radius:10px;
  }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:system-ui,-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif;
         font-size:14px; -webkit-font-smoothing:antialiased; }
  .app { display:flex; min-height:100vh; }

  /* ---------- sidebar ---------- */
  .sidebar { width:236px; flex:none; background:linear-gradient(180deg,var(--sb-bg),var(--sb-bg2));
             color:var(--sb-fg); display:flex; flex-direction:column;
             position:sticky; top:0; height:100vh; }
  .brand { display:flex; gap:11px; align-items:center; padding:16px 16px;
           border-bottom:1px solid rgba(255,255,255,.07); }
  .brand .logo { width:38px; height:38px; border-radius:10px; flex:none;
                 background:linear-gradient(135deg,#2f7ed8,#7b5cff); color:#fff;
                 display:flex; align-items:center; justify-content:center;
                 font-weight:700; font-size:13px; letter-spacing:.5px;
                 box-shadow:0 4px 12px rgba(47,126,216,.4); }
  .brand b { display:block; font-size:15px; font-weight:600; color:#fff; }
  .brand span { display:block; font-size:11px; color:var(--sb-dim); margin-top:1px; }
  .menu { flex:1; overflow-y:auto; padding:10px 9px; }
  .menu-title { display:flex; align-items:center; justify-content:space-between;
                color:var(--sb-dim); font-size:11px; letter-spacing:.08em;
                padding:12px 12px 6px; user-select:none; }
  .menu-title .chev { transition:transform .18s; font-size:10px; }
  .menu-group.collapsed .chev { transform:rotate(-90deg); }
  .menu-group.collapsed .menu-item { display:none; }
  .menu-item { display:flex; align-items:center; gap:10px; width:100%; margin:2px 0;
               padding:9px 12px; border:0; background:transparent; color:var(--sb-fg);
               border-radius:8px; cursor:pointer; font-size:13.5px; text-align:left;
               font-family:inherit; }
  .menu-item:hover { background:var(--sb-hover); }
  .menu-item.active { background:var(--sb-active); color:#fff;
                      box-shadow:0 3px 10px rgba(47,126,216,.35); }
  .menu-item svg { width:17px; height:17px; flex:none; opacity:.9; }
  .menu-item .label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .menu-item .dot { margin-left:auto; width:8px; height:8px; border-radius:50%;
                    background:var(--down); flex:none; }
  .side-foot { padding:12px 16px; font-size:11px; color:var(--sb-dim);
               border-top:1px solid rgba(255,255,255,.07); }

  /* ---------- main ---------- */
  .main { flex:1; min-width:0; display:flex; flex-direction:column; }
  .topbar { height:58px; flex:none; background:#fff; border-bottom:1px solid var(--line);
            display:flex; align-items:center; gap:10px; padding:0 20px;
            position:sticky; top:0; z-index:10; }
  .topbar .crumb { font-size:15px; font-weight:600; }
  .topbar .crumb small { color:var(--mut); font-weight:400; margin-left:8px; font-size:12px; }
  .spacer { flex:1; }
  .chip { background:#f4f6fa; border:1px solid var(--line); border-radius:7px;
          padding:4px 10px; color:var(--mut); font-size:12px; white-space:nowrap; }
  .chip b { color:var(--fg); font-weight:600; }
  .chip.ro { color:var(--err); border-color:#f2c9c9; background:#fdf1f1; }
  .sw { display:flex; align-items:center; gap:6px; color:var(--mut); font-size:12.5px;
        user-select:none; cursor:pointer; }
  .content { padding:20px 22px 60px; width:100%; max-width:1320px; }

  /* ---------- buttons ---------- */
  .btn { border:1px solid var(--line); background:#fff; color:var(--fg);
         border-radius:7px; padding:6px 13px; cursor:pointer; font-size:13px;
         font-family:inherit; display:inline-flex; align-items:center; gap:6px; }
  .btn:hover { border-color:var(--acc); color:var(--acc); }
  .btn.primary { background:var(--acc); border-color:var(--acc); color:#fff; }
  .btn.primary:hover { background:#2569b8; color:#fff; }
  .btn.danger { border-color:#f0c4c4; color:var(--err); }
  .btn.danger:hover { background:#fef1f1; border-color:var(--err); }
  .btn:disabled { opacity:.42; cursor:not-allowed; }
  .btn.sm { padding:4px 10px; font-size:12px; }

  /* ---------- cards ---------- */
  .grid { display:grid; gap:14px; }
  .cards4 { grid-template-columns:repeat(4,1fr); }
  .cards3 { grid-template-columns:repeat(3,1fr); }
  .two { grid-template-columns:1.15fr .85fr; }
  @media (max-width:1000px) { .cards4,.cards3,.two { grid-template-columns:1fr 1fr; } }
  @media (max-width:680px)  { .cards4,.cards3,.two { grid-template-columns:1fr; } }
  .card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
          box-shadow:var(--shadow); padding:16px; }
  .card h3 { margin:0 0 12px; font-size:14px; font-weight:600; }
  .stat { display:flex; align-items:center; gap:13px; }
  .stat .ico { width:44px; height:44px; border-radius:11px; flex:none;
               display:flex; align-items:center; justify-content:center; }
  .stat .ico svg { width:22px; height:22px; }
  .stat .k { color:var(--mut); font-size:12.5px; }
  .stat .v { font-size:26px; font-weight:700; line-height:1.15; margin-top:2px; }
  .i-blue { background:#e8f1fc; color:#2f7ed8; }
  .i-green{ background:#e7f8ee; color:#1fae63; }
  .i-gray { background:#eef1f5; color:#7a8699; }
  .i-red  { background:#fdecec; color:#e5484d; }
  .i-amber{ background:#fdf4e3; color:#d99a2b; }

  /* ---------- bars ---------- */
  .barrow { display:grid; grid-template-columns:170px 1fr 78px; align-items:center;
            gap:12px; padding:8px 0; border-bottom:1px solid var(--line); }
  .barrow:last-child { border-bottom:0; }
  .barrow .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:13px; }
  .barrow .name .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
                       margin-right:8px; vertical-align:middle; background:var(--down); }
  .bar { height:9px; border-radius:6px; background:#eef1f6; overflow:hidden; }
  .bar > i { display:block; height:100%; border-radius:6px; background:var(--down);
             transition:width .3s; }
  .bar > i.up { background:var(--up); }
  .bar > i.err { background:var(--err); }
  .barrow .st { text-align:right; font-size:12px; color:var(--mut); }
  .stack { display:flex; height:12px; border-radius:6px; overflow:hidden; background:#eef1f6; }
  .stack > i { display:block; height:100%; }
  .legend { display:flex; gap:16px; margin-top:12px; flex-wrap:wrap; font-size:12.5px; color:var(--mut); }
  .legend span i { display:inline-block; width:9px; height:9px; border-radius:3px; margin-right:6px; }

  /* ---------- table ---------- */
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:10px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:500; font-size:12px; white-space:nowrap; }
  tr:last-child td { border-bottom:0; }
  td .dot { display:inline-block; width:8px; height:8px; border-radius:50%;
            background:var(--down); margin-right:7px; vertical-align:middle; }
  .mono { font-family:ui-monospace,Consolas,monospace; font-size:12px; color:var(--mut); }

  /* ---------- vpn page ---------- */
  .vhead { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:4px; }
  .vhead .vicon { width:40px; height:40px; border-radius:11px; flex:none; background:#e8f1fc;
                  color:var(--acc); display:flex; align-items:center; justify-content:center; }
  .vhead .vicon svg { width:21px; height:21px; }
  .vhead h2 { margin:0; font-size:20px; font-weight:650; }
  .vhead .vkey { font-family:ui-monospace,Consolas,monospace; font-size:12px; color:var(--mut); }
  .vsub { color:var(--mut); font-size:12.5px; margin:0 0 14px 52px; }
  .pill { font-size:12px; padding:3px 11px; border-radius:999px; border:1px solid var(--line);
          background:#f4f6fa; color:var(--mut); display:inline-flex; align-items:center; gap:6px; }
  .pill .dot { width:7px; height:7px; border-radius:50%; background:currentColor; }
  .pill.up { color:var(--up); background:#e9f9f0; border-color:#bce8cf; }
  .pill.err { color:var(--err); background:#fdeeee; border-color:#f2c6c6; }
  .actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  .banner { border-radius:9px; padding:10px 14px; font-size:12.5px; line-height:1.65;
            border:1px solid var(--line); margin-bottom:16px; }
  .banner.ok { background:#effaf3; border-color:#bce8cf; color:#177a48; }
  .banner.warn { background:#fff8e8; border-color:#f0d79c; color:#94651a; }
  .banner.err { background:#fef1f1; border-color:#f2c6c6; color:#b02a2a; }
  .tabs { display:flex; gap:2px; border-bottom:1px solid var(--line); margin-bottom:16px; }
  .tab { border:0; background:transparent; padding:9px 15px; cursor:pointer;
         color:var(--mut); font-size:13.5px; font-family:inherit;
         border-bottom:2px solid transparent; margin-bottom:-1px; }
  .tab:hover { color:var(--fg); }
  .tab.active { color:var(--acc); border-bottom-color:var(--acc); font-weight:600; }
  .kv { width:100%; }
  .kv td { padding:9px 10px; }
  .kv td:first-child { color:var(--mut); width:150px; white-space:nowrap; }
  .kv tr:first-child td { padding-top:0; }
  .note { color:var(--mut); font-size:12.5px; line-height:1.7; }
  .pre { background:#0f172a; color:#d6e2f0; border-radius:9px; padding:12px 14px;
         font-family:ui-monospace,Consolas,monospace; font-size:12px; line-height:1.55;
         white-space:pre-wrap; word-break:break-all; max-height:480px; overflow:auto; margin:0; }
  .pre mark { background:transparent; color:#7ee787; padding:0; }
  .logbar { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-bottom:10px; }
  .logbar label { color:var(--mut); font-size:12.5px; display:flex; align-items:center; gap:6px; }
  select { background:#fff; border:1px solid var(--line); color:var(--fg);
           border-radius:7px; padding:5px 8px; font-size:12.5px; font-family:inherit; }
  .muted { color:var(--mut); font-size:12px; }
  .alertrow { display:flex; gap:8px; align-items:baseline; padding:5px 0;
              border-bottom:1px dashed var(--line); font-size:12.5px; }
  .alertrow:last-child { border-bottom:0; }
  .alertrow .at { color:var(--mut); font-size:11.5px; white-space:nowrap; }
  .alertrow .lvl { text-transform:uppercase; font-size:10.5px; border-radius:4px;
                   padding:1px 6px; background:#eef1f5; color:#5b6878; flex:none; }
  .alertrow .lvl.warning { background:#fdf4e3; color:#b07d16; }
  .alertrow .lvl.error { background:#fdecec; color:#c53030; }
  .alertrow .lvl.info { background:#e8f1fc; color:#2f7ed8; }
  .alertrow .who { color:var(--mut); margin-left:auto; white-space:nowrap; }
  .pref { display:flex; align-items:center; gap:9px; padding:9px 0; border-bottom:1px solid var(--line);
          font-size:13px; }
  .pref:last-child { border-bottom:0; }
  .pref .desc { color:var(--mut); font-size:12px; }
  .switch { position:relative; width:38px; height:21px; flex:none; }
  .switch input { opacity:0; width:0; height:0; }
  .switch .sl { position:absolute; inset:0; background:#cfd8e3; border-radius:999px;
                transition:.2s; cursor:pointer; }
  .switch .sl:before { content:""; position:absolute; width:15px; height:15px; left:3px; top:3px;
                       background:#fff; border-radius:50%; transition:.2s; }
  .switch input:checked + .sl { background:var(--acc); }
  .switch input:checked + .sl:before { transform:translateX(17px); }

  #toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%) translateY(10px);
           background:#1f2d3d; color:#fff; padding:10px 18px; border-radius:9px;
           box-shadow:0 8px 24px rgba(0,0,0,.25); opacity:0; transition:.25s;
           pointer-events:none; z-index:60; font-size:13px; max-width:80vw; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  .empty { color:var(--mut); text-align:center; padding:30px 0; }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="logo">VPN</div>
      <div><b>VPN 管控平台</b><span>服务器统一管理</span></div>
    </div>
    <nav class="menu" id="menu"></nav>
    <div class="side-foot">免登录 · 仅本机 127.0.0.1</div>
  </aside>

  <div class="main">
    <header class="topbar">
      <div class="crumb" id="crumb">数据看板</div>
      <div class="spacer"></div>
      <span class="chip">服务器 <b id="sip">-</b></span>
      <span class="chip">Docker <b id="dmode">-</b></span>
      <span class="chip ro" id="rochip" style="display:none">只读模式</span>
      <input id="token" type="password" placeholder="操作令牌" style="display:none;background:#f4f6fa;border:1px solid var(--line);border-radius:7px;padding:5px 9px;font-size:12px;width:150px">
      <label class="sw"><input type="checkbox" id="auto" checked style="width:auto"> 自动刷新</label>
      <button class="btn" id="refresh">刷新</button>
    </header>
    <section class="content" id="content"><div class="empty">加载中…</div></section>
  </div>
</div>

<div id="toast"></div>
<script>
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));

const ICON = (p) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
const ICONS = {
  dashboard: ICON('<rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/>'),
  openvpn:   ICON('<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 2.6 3.8 5.7 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.7-3.8-9S9.5 5.6 12 3z"/>'),
  'l2tp-ipsec': ICON('<rect x="4" y="10" width="16" height="10" rx="2.2"/><path d="M8 10V7.5a4 4 0 0 1 8 0V10"/>'),
  'l2tp-plain': ICON('<path d="M9.5 14.5 14.5 9.5"/><path d="M8 18H6a4 4 0 0 1 0-8h2"/><path d="M16 6h2a4 4 0 0 1 0 8h-2"/>'),
  strongswan: ICON('<path d="M12 3 5 6v5c0 4.6 3 8 7 10 4-2 7-5.4 7-10V6l-7-3z"/><path d="m9 12 2 2 4-4"/>'),
  pptp:      ICON('<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8z"/>'),
  openconnect: ICON('<path d="M7 18h10a4 4 0 0 0 .4-8A5.5 5.5 0 0 0 6.6 9.3 4.2 4.2 0 0 0 7 18z"/>'),
  vpnc:      ICON('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
  default:   ICON('<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>'),
};
const icon = (k) => ICONS[k] || ICONS.default;

const STATUS_TEXT = { running:'运行中', active:'运行中', exited:'已停止', created:'已创建',
  inactive:'未运行', absent:'未部署', failed:'异常', unknown:'未知' };
const stateCls = (st) => (st === 'running' || st === 'active') ? 'up'
                       : (st === 'failed' ? 'err' : 'down');
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtTs = (ts) => { try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return ''; } };

let LIST = null, VPNS = [], ACTIVE = 'overview', TAB = 'overview', DETAIL = null;
let autoTimer = null, logES = null, logBuf = [], logRenderTimer = null, logPause = false, menuCollapsed = false;
let OV_SIG = null, OV_EXTRA = { alerts: '', audit: '' }, MENU_SIG = null, CRUMB_SIG = null;

function ovSig() {
  return (LIST ? LIST.server_ip : '') + '|' + VPNS.map(v =>
    v.key + ':' + v.status + ':' + (v.running ? 1 : 0) + ':' + ((v.probes_open || []).join(','))).join(';');
}

function prefs(key) { try { return JSON.parse(localStorage.getItem('vpnctl.prefs.' + key)) || {}; } catch(e) { return {}; } }
function setPref(key, k, v) { const p = prefs(key); p[k] = v; localStorage.setItem('vpnctl.prefs.' + key, JSON.stringify(p)); }
function tail(key) { return prefs(key).tail || 80; }
function autoStop(key) { return prefs(key).autoStop !== false; }

function toast(msg, ms) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms || 2600);
}

/* ---------------- sidebar ---------------- */
function renderSidebar() {
  const sig = ACTIVE + '|' + menuCollapsed + '|' +
    VPNS.map(v => v.key + ':' + v.status).join(',');
  if (sig === MENU_SIG && $('#menu').children.length) return;
  MENU_SIG = sig;
  const items = VPNS.map(v =>
    '<button class="menu-item ' + (ACTIVE === v.key ? 'active' : '') + '" data-view="' + v.key + '" title="' + esc(v.title) + '">'
    + icon(v.key) + '<span class="label">' + esc(v.title) + '</span>'
    + '<span class="dot ' + stateCls(v.status) + '"></span></button>').join('');
  $('#menu').innerHTML =
    '<button class="menu-item ' + (ACTIVE === 'overview' ? 'active' : '') + '" data-view="overview">'
    + ICONS.dashboard + '<span class="label">数据看板</span></button>'
    + '<div class="menu-group ' + (menuCollapsed ? 'collapsed' : '') + '" id="mg">'
    +   '<div class="menu-title" id="mgt">VPN 服务（' + VPNS.length + '）<span class="chev">▼</span></div>'
    +   items
    + '</div>';
  $$('#menu .menu-item').forEach(b => b.onclick = () => go(b.dataset.view));
  const mgt = $('#mgt');
  if (mgt) mgt.onclick = () => { menuCollapsed = !menuCollapsed; $('#mg').classList.toggle('collapsed', menuCollapsed); };
}

function setCrumb() {
  const html = (ACTIVE === 'overview')
    ? '数据看板<small>全部 VPN 服务总览</small>'
    : (() => { const v = VPNS.find(x => x.key === ACTIVE);
        return esc(v ? v.title : ACTIVE) + '<small>' + (v ? esc(v.kind + ' · ' + v.target) : '') + '</small>'; })();
  if (html === CRUMB_SIG) return;
  CRUMB_SIG = html;
  $('#crumb').innerHTML = html;
}

/* ---------------- router ---------------- */
function go(view) {
  ACTIVE = view; TAB = 'overview'; DETAIL = null;
  stopStream();
  renderSidebar(); setCrumb();
  if (view === 'overview') renderOverview();
  else loadDetail(view, true);
}

/* ---------------- data ---------------- */
async function fetchList() {
  const r = await fetch('/api/list');
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const d = await r.json();
  LIST = d; VPNS = d.vpns || [];
  $('#sip').textContent = d.server_ip; $('#dmode').textContent = d.docker_mode;
  $('#rochip').style.display = d.read_only ? '' : 'none';
  const ti = $('#token'); if (ti) ti.style.display = d.auth ? '' : 'none';
  renderSidebar(); setCrumb();
  if (ACTIVE === 'overview') {
    const sig = ovSig();
    if (sig !== OV_SIG) { OV_SIG = sig; renderOverview(); }
    else loadOverviewExtras();   // 数据未变：只就地刷新告警/操作，不重建页面
  }
}

async function loadDetail(key, full) {
  try {
    const r = await fetch('/api/detail?name=' + encodeURIComponent(key));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    if (ACTIVE !== key) return;
    DETAIL = d;
    if (full) renderVpnPage(); else patchVpnStatus();
  } catch (e) { toast('加载详情失败: ' + e.message); }
}

/* ---------------- overview ---------------- */
function renderOverview() {
  if (ACTIVE !== 'overview') return;
  const total = VPNS.length;
  const up = VPNS.filter(v => v.running).length;
  const down = total - up;
  const conflicts = VPNS.filter(v => v.running && v.conflict && v.conflict.group && v.conflict.running.length);
  const pct = total ? Math.round(up / total * 100) : 0;

  const stat = (k, v, cls, ic) =>
    '<div class="card stat"><div class="ico i-' + cls + '">' + ICONS[ic] + '</div>'
    + '<div><div class="k">' + k + '</div><div class="v">' + v + '</div></div></div>';

  const rows = VPNS.map(v => {
    const st = STATUS_TEXT[v.status] || v.status;
    const listen = v.probes_open && v.probes_open.length ? v.probes_open.join(', ') : '—';
    const grp = v.group ? v.group : '—';
    return '<tr>'
      + '<td><span class="dot ' + stateCls(v.status) + '"></span><b>' + esc(v.title) + '</b></td>'
      + '<td class="mono">' + esc(v.kind) + '</td>'
      + '<td>' + esc(st) + '</td>'
      + '<td class="mono">' + esc(listen) + '</td>'
      + '<td class="mono">' + esc(v.subnet) + '</td>'
      + '<td class="mono">' + esc(grp) + '</td>'
      + '<td style="text-align:right;white-space:nowrap">'
      +   '<button class="btn sm primary" data-manage="' + v.key + '">管理</button> '
      +   (v.running ? '<button class="btn sm danger" data-act="down" data-key="' + v.key + '">停止</button>'
      :             '<button class="btn sm" data-act="up" data-key="' + v.key + '">启动</button>')
      + '</td></tr>';
  }).join('');

  const bars = VPNS.map(v => {
    const cls = stateCls(v.status);
    const width = v.running ? (v.listening ? 100 : 72) : (v.status === 'absent' ? 4 : 14);
    const st = STATUS_TEXT[v.status] || v.status;
    return '<div class="barrow" data-manage="' + v.key + '" style="cursor:pointer">'
      + '<div class="name"><span class="dot ' + cls + '"></span>' + esc(v.title) + '</div>'
      + '<div class="bar"><i class="' + (cls === 'up' ? 'up' : cls === 'err' ? 'err' : '') + '" style="width:' + width + '%"></i></div>'
      + '<div class="st">' + esc(st) + '</div></div>';
  }).join('');

  const group = (VPNS.find(v => v.conflict && v.conflict.group) || {}).conflict || null;
  let groupHTML = '<div class="note">没有互斥组。</div>';
  if (group) {
    const members = [group.group + ' 组（同占 500/4500/udp）',
      '同组：' + group.peers.join('、')];
    groupHTML = '<div class="note" style="line-height:2">' + members.map(esc).join('<br>') + '</div>';
    if (conflicts.length) groupHTML += '<div class="banner err" style="margin:12px 0 0">冲突：'
      + esc(conflicts.map(v => v.title).join('、')) + ' 同时在运行，请只保留一个。</div>';
    else groupHTML += '<div class="banner ok" style="margin:12px 0 0">当前无端口冲突。</div>';
  }

  $('#content').innerHTML =
    '<div class="grid cards4" style="margin-bottom:14px">'
    + stat('VPN 服务总数', total, 'blue', 'default')
    + stat('运行中', up, 'green', 'strongswan')
    + stat('已停止', down, 'gray', 'default')
    + stat('端口冲突', conflicts.length, conflicts.length ? 'red' : 'amber', 'pptp')
    + '</div>'
    + '<div class="grid two" style="margin-bottom:14px">'
    + '<div class="card"><h3>各服务状态</h3>' + bars + '</div>'
    + '<div class="card"><h3>运行状态分布</h3>'
    +   '<div class="stack"><i class="up" style="width:' + pct + '%;background:var(--up)"></i>'
    +   '<i style="width:' + (100 - pct) + '%;background:#d7dee8"></i></div>'
    +   '<div class="legend"><span><i style="background:var(--up)"></i>运行中 ' + up + '</span>'
    +   '<span><i style="background:#d7dee8"></i>已停止 ' + down + '</span></div>'
    +   '<h3 style="margin-top:20px">互斥组</h3>' + groupHTML
    + '</div>'
    + '</div>'
    + '<div class="grid two" style="margin-bottom:14px">'
    + '<div class="card"><div style="display:flex;align-items:center;justify-content:space-between">'
    +   '<h3 style="margin:0">最近告警</h3>'
    +   '<button class="btn sm" id="btn-clear-alerts" title="只清除已解决的告警；当前仍存在的问题会保留">清空已解决</button></div>'
    +   '<div id="ov-alerts" class="note">' + (OV_EXTRA.alerts || '加载中…') + '</div></div>'
    + '<div class="card"><h3>最近操作</h3><div id="ov-audit" class="note">' + (OV_EXTRA.audit || '加载中…') + '</div></div>'
    + '</div>'
    + '<div class="card" style="padding:0;overflow:hidden">'
    + '<div style="padding:14px 16px;display:flex;align-items:center;justify-content:space-between">'
    +   '<h3 style="margin:0">全部服务</h3>'
    +   '<button class="btn sm danger" id="btn-down-all"' + (up ? '' : ' disabled') + '>全部停止</button>'
    + '</div>'
    + '<table><thead><tr><th>服务</th><th>类型</th><th>状态</th><th>监听端口</th>'
    + '<th>隧道网段</th><th>互斥组</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>'
    + '</div>';

  $$('#content [data-manage]').forEach(el => el.onclick = () => go(el.dataset.manage));
  $$('#content [data-act]').forEach(b => b.onclick = (e) => { e.stopPropagation(); act(b.dataset.key, b.dataset.act); });
  const da = $('#btn-down-all'); if (da) da.onclick = downAll;
  const ca = $('#btn-clear-alerts'); if (ca) ca.onclick = clearAlerts;
  OV_SIG = ovSig();
  loadOverviewExtras();
}

// 只就地更新告警/操作卡片：内容未变不碰 DOM，避免自动刷新闪动
async function loadOverviewExtras() {
  if (ACTIVE !== 'overview') return;
  try {
    const [alerts, h] = await Promise.all([
      fetch('/api/alerts?tail=8').then(r => r.json()),
      fetch('/api/health').then(r => r.json()),
    ]);
    let html = '';
    if (h.problems && h.problems.length)
      html += '<div class="banner err" style="margin:0 0 10px">健康异常：' + esc(h.problems.join('；')) + '</div>';
    html += alerts.length
      ? alerts.map(a => '<div class="alertrow"><span class="lvl ' + esc(a.level) + '">' + esc(a.level)
          + '</span><span class="at">' + esc(fmtTs(a.ts)) + '</span><span>' + esc(a.title) + '</span></div>').join('')
      : '<span class="muted">暂无告警</span>';
    if (html !== OV_EXTRA.alerts) {
      OV_EXTRA.alerts = html;
      const box = $('#ov-alerts');
      if (box && ACTIVE === 'overview') box.innerHTML = html;
    }
  } catch (e) { /* 保留上次内容 */ }
  try {
    const audit = await fetch('/api/audit?tail=8').then(r => r.json());
    const html = audit.length
      ? audit.map(a => '<div class="alertrow"><span class="lvl ' + (a.ok ? 'info' : 'error') + '">'
          + (a.ok ? 'OK' : 'FAIL') + '</span><span class="at">' + esc(fmtTs(a.ts)) + '</span><span>'
          + esc(a.action + ' ' + (a.key || '')) + '</span><span class="who">' + esc(a.actor || '') + '</span></div>').join('')
      : '<span class="muted">暂无操作记录</span>';
    if (html !== OV_EXTRA.audit) {
      OV_EXTRA.audit = html;
      const box = $('#ov-audit');
      if (box && ACTIVE === 'overview') box.innerHTML = html;
    }
  } catch (e) { /* 保留上次内容 */ }
}

/* ---------------- vpn page ---------------- */
function renderVpnPage() {
  const v = DETAIL; if (!v) return;
  const st = STATUS_TEXT[v.status] || v.status;
  $('#content').innerHTML =
    '<div class="vhead"><span class="vicon">' + icon(v.key) + '</span>'
    + '<h2>' + esc(v.title) + '</h2>'
    + '<span class="pill ' + stateCls(v.status) + '" id="vpill"><span class="dot"></span>' + esc(st) + '</span>'
    + '<span class="vkey">' + esc(v.key) + '</span></div>'
    + '<div class="vsub">' + esc(v.kind) + ' · ' + esc(v.target) + ' · 端口 ' + esc(v.ports.join(', ')) + '</div>'
    + '<div class="actions">'
    +   '<button class="btn primary" data-act="enable">启用（含自启）</button>'
    +   '<button class="btn" data-act="up">仅启动</button>'
    +   '<button class="btn danger" data-act="down">停止</button>'
    +   '<button class="btn" data-act="restart">重启</button>'
    +   '<button class="btn" data-act="disable">禁用（停自启）</button>'
    + '</div>'
    + '<div id="vbanner"></div>'
    + '<div class="tabs">'
    +   tabBtn('overview', '状态概览') + tabBtn('config', '配置信息')
    +   tabBtn('logs', '实时日志') + tabBtn('events', '历史事件')
    + '</div><div id="vtabbody"></div>';
  $$('#content [data-act]').forEach(b => b.onclick = () => act(v.key, b.dataset.act));
  $$('#content .tab').forEach(b => b.onclick = () => { TAB = b.dataset.tab; renderVpnBody(); });
  patchVpnStatus();
  renderVpnBody();
}

function tabBtn(id, label) {
  return '<button class="tab ' + (TAB === id ? 'active' : '') + '" data-tab="' + id + '">' + label + '</button>';
}

function renderVpnBody() {
  const v = DETAIL; if (!v) return;
  stopStream();
  $$('#content .tab').forEach(b => b.classList.toggle('active', b.dataset.tab === TAB));
  const body = $('#vtabbody');
  if (TAB === 'overview') {
    const listen = v.probes_open && v.probes_open.length ? v.probes_open.join(', ') : '无（未监听）';
    body.innerHTML =
      '<div class="grid cards4">'
      + card('运行状态', STATUS_TEXT[v.status] || v.status)
      + card('监听端口', listen)
      + card('隧道网段', v.subnet)
      + card('网关', v.gateway)
      + '</div>'
      + '<div class="card" style="margin-top:14px"><h3>说明</h3>'
      + '<div class="note">' + esc(v.note || '（无）') + '</div>'
      + '<div class="note" style="margin-top:10px">校验命令：<span class="mono">' + esc(v.verify || '-') + '</span></div>'
      + '</div>';
  } else if (TAB === 'config') {
    const a = autoStop(v.key);
    const conf = v.conflict || {};
    body.innerHTML =
      '<div class="card" style="padding:16px;margin-bottom:14px">'
      + '<h3>服务端配置</h3><table class="kv"><tbody>'
      + row('管理类型', v.kind) + row('服务目标', v.target)
      + row('监听端口', v.ports.join(', ')) + row('隧道网段', v.subnet)
      + row('网关', v.gateway) + row('互斥组', conf.group || '无（可与任意 VPN 共存）')
      + row('文档', v.docs) + row('客户端 / 凭据', v.client)
      + row('校验命令', v.verify)
      + '</tbody></table>'
      + '<div class="note" style="margin-top:12px">服务端参数（IP / 网段 / 端口）由对应 recipe 的 '
      + '<span class="mono">setup-*.sh</span> 与使用指南管理，本平台不修改这些文件。</div>'
      + '</div>'
      + '<div class="card" style="padding:16px"><h3>本页运行偏好（仅本机浏览器保存）</h3>'
      + '<div class="pref"><label class="switch"><input type="checkbox" id="pref-auto" ' + (a ? 'checked' : '') + '>'
      +   '<span class="sl"></span></label>'
      +   '<div><div>启动 / 重启时自动停用互斥服务</div>'
      +   '<div class="desc">关闭后强制与同组 VPN 共存（可能抢占 500/4500 而失败）。</div></div></div>'
      + '<div class="pref"><div style="width:38px"></div>'
      +   '<div style="display:flex;align-items:center;gap:10px">日志默认行数'
      +   '<select id="pref-tail">' + [40,80,200,500].map(n => '<option' + (n === tail(v.key) ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></div></div>'
      + '</div>';
    $('#pref-auto').onchange = e => { setPref(v.key, 'autoStop', e.target.checked); toast('已保存：自动停用互斥 = ' + (e.target.checked ? '开' : '关')); };
    $('#pref-tail').onchange = e => setPref(v.key, 'tail', parseInt(e.target.value, 10));
  } else if (TAB === 'events') {
    body.innerHTML =
      '<div class="card">'
      + '<div class="logbar"><h3 style="margin:0">历史连接事件</h3>'
      + '<span class="muted">来自后台采集器落库（state/vpnplat.db），客户端连接/断开时记录</span>'
      + '<span class="spacer"></span>'
      + '<label>行数 <select id="hist-tail">' + [40,80,200,500].map(n => '<option' + (n === tail(v.key) ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></label>'
      + '<button class="btn sm" id="hist-refresh">刷新</button></div>'
      + '<pre class="pre" id="hist-out">加载中…</pre></div>';
    $('#hist-tail').onchange = e => { setPref(v.key, 'tail', parseInt(e.target.value, 10)); loadHistoryEvents(v.key); };
    $('#hist-refresh').onclick = () => loadHistoryEvents(v.key);
    loadHistoryEvents(v.key);
  } else {
    const p = prefs(v.key);
    body.innerHTML =
      '<div class="card">'
      + '<div class="logbar"><h3 style="margin:0">实时日志</h3>'
      + '<label>范围 <select id="log-scope">'
      +   '<option value="all"' + ((p.scope || 'all') === 'all' ? ' selected' : '') + '>全部（服务运行 + 连接）</option>'
      +   '<option value="events"' + (p.scope === 'events' ? ' selected' : '') + '>仅连接事件</option>'
      + '</select></label>'
      + '<label>行数 <select id="log-tail">' + [40,80,200,500].map(n => '<option' + (n === tail(v.key) ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></label>'
      + '<label><input type="checkbox" id="log-live" ' + (p.live !== false ? 'checked' : '') + ' style="width:auto"> 实时</label>'
      + '<label><input type="checkbox" id="log-pause" ' + (logPause ? 'checked' : '') + ' style="width:auto"> 暂停</label>'
      + '<button class="btn sm" id="btn-log">重连</button>'
      + '<span class="spacer"></span><span class="muted" id="log-time"></span></div>'
      + '<div class="muted" style="margin-bottom:8px">来源：' + esc(logSource(v)) + '；连接事件高亮。「实时」为服务器日志流（SSE），新行即时到达。</div>'
      + '<pre class="pre" id="log-out">连接日志流…</pre></div>';
    $('#log-scope').onchange = e => { setPref(v.key, 'scope', e.target.value); scheduleRender(true); };
    $('#log-tail').onchange = e => { setPref(v.key, 'tail', parseInt(e.target.value, 10)); restartLog(); };
    $('#log-live').onchange = e => { setPref(v.key, 'live', e.target.checked); if (e.target.checked) startStream(); else { stopStream(); loadOnce(); } };
    $('#log-pause').onchange = e => { logPause = e.target.checked; if (!logPause) scheduleRender(true); };
    $('#btn-log').onclick = () => restartLog();
    logBuf = [];
    if (p.live !== false) startStream(); else loadOnce();
  }
}

function card(k, val) {
  return '<div class="card"><div class="k" style="color:var(--mut);font-size:12.5px">' + esc(k)
    + '</div><div style="font-size:15px;font-weight:600;margin-top:6px;word-break:break-all">' + esc(val) + '</div></div>';
}
function row(k, val) {
  return '<tr><td>' + esc(k) + '</td><td>' + (val ? esc(val) : '—') + '</td></tr>';
}

function patchVpnStatus() {
  const v = DETAIL; if (!v || ACTIVE === 'overview') return;
  const pill = $('#vpill');
  if (pill) { pill.className = 'pill ' + stateCls(v.status); pill.innerHTML = '<span class="dot"></span>' + esc(STATUS_TEXT[v.status] || v.status); }
  const b = $('#vbanner');
  if (b) {
    const c = v.conflict || { group:null, peers:[], running:[] };
    if (!c.group) { b.className = 'banner ok'; b.textContent = '互斥组：无 —— 可与任意其它 VPN 同时运行。'; }
    else if (c.running && c.running.length) {
      b.className = 'banner ' + (v.running ? 'err' : 'warn');
      b.innerHTML = '互斥组 <b>' + esc(c.group) + '</b>（同占 500/4500/udp）：同组 <b>' + esc(c.peers.join('、'))
        + '</b> 正在运行：<b>' + esc(c.running.join('、')) + '</b>。'
        + (v.running ? ' 冲突！请只保留一个。' : (autoStop(v.key) ? ' 启动本服务会先自动停用它们。' : ' 已关闭自动停用，可能启动失败。'));
    } else { b.className = 'banner ok'; b.textContent = '互斥组 ' + c.group + '（同占 500/4500/udp）当前无冲突。'; }
  }
  const up = v.running;
  setDis('[data-act="up"]', up); setDis('[data-act="enable"]', up);
  setDis('[data-act="down"]', !up); setDis('[data-act="restart"]', !up);
}
function setDis(sel, dis) { const b = $(sel); if (b) b.disabled = dis; }

/* ---------------- actions ---------------- */
async function act(key, action) {
  const labels = { up:'启动', enable:'启用', down:'停止', restart:'重启', disable:'禁用' };
  try {
    const r = await fetch('/api/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-VPNCTL-Token': token() },
      body: JSON.stringify({ name: key, action, keep_conflicts: !autoStop(key) })
    });
    const d = await r.json();
    if (!r.ok || !d.ok) toast('失败：' + (d.error || d.message || ('HTTP ' + r.status)) + (d.hint ? ' ｜ ' + d.hint : ''), 7000);
    else {
      let m = (labels[action] || action) + ' ' + key + ' 成功';
      if (d.stopped_peers && d.stopped_peers.length) m += '（已停用互斥：' + d.stopped_peers.join('、') + '）';
      toast(m);
    }
    await fetchList();
    if (ACTIVE !== 'overview') await loadDetail(ACTIVE, false);
  } catch (e) { toast('请求失败：' + e.message); }
}

/* ---------------- 一键停止全部 ---------------- */
async function downAll() {
  const running = VPNS.filter(v => v.running).length;
  if (!running) { toast('当前没有运行中的 VPN 服务'); return; }
  if (!confirm('确定停止全部正在运行的 VPN 服务（共 ' + running + ' 个）吗？')) return;
  const btn = $('#btn-down-all');
  if (btn) { btn.disabled = true; btn.textContent = '停止中…'; }
  try {
    const r = await fetch('/api/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-VPNCTL-Token': token() },
      body: JSON.stringify({ action: 'down-all' })
    });
    const d = await r.json();
    if (!r.ok || !d.ok)
      toast('部分失败：' + (((d.failed || []).join('、')) || d.error || ('HTTP ' + r.status)), 7000);
    else
      toast('已停止全部 VPN' + ((d.stopped && d.stopped.length) ? '：' + d.stopped.join('、') : '（无运行中的服务）'));
  } catch (e) { toast('请求失败：' + e.message); }
  OV_SIG = null;                 // 强制重建总览，刷新状态与按钮
  await fetchList();
  if (ACTIVE !== 'overview') await loadDetail(ACTIVE, false);
}

/* ---------------- 清空已解决的告警 ---------------- */
async function clearAlerts() {
  const btn = $('#btn-clear-alerts'); if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-VPNCTL-Token': token() },
      body: JSON.stringify({ action: 'clear-alerts' })
    });
    const d = await r.json();
    if (!r.ok || !d.ok) toast('清空失败：' + (d.error || ('HTTP ' + r.status)));
    else if (d.kept) toast('已清空 ' + d.cleared + ' 条；保留 ' + d.kept + ' 条未解决的告警');
    else toast('已清空 ' + d.cleared + ' 条历史告警');
    OV_EXTRA.alerts = '';
    await loadOverviewExtras();
  } catch (e) { toast('请求失败：' + e.message); }
  finally { const b = $('#btn-clear-alerts'); if (b) b.disabled = false; }
}

/* ---------------- 实时日志（SSE 流） ---------------- */
function logSource(v) {
  return v.kind === 'docker' ? 'docker logs -f ' + v.target : 'journalctl -f -u ' + v.target;
}
function isStick(out) { return out.scrollHeight - out.scrollTop - out.clientHeight < 30; }
function renderLog(txt, scope, pattern) {
  let re = null;
  if (pattern) { try { re = new RegExp(pattern, 'i'); } catch (e) { re = null; } }
  const raw = (txt || '').replace(/\s+$/, '');
  const lines = raw ? raw.split('\n') : [];
  const out = []; let matched = 0;
  for (const ln of lines) {
    const ev = re ? re.test(ln) : false;
    if (ev) matched++;
    if (scope === 'events' && !ev) continue;
    out.push(ev ? '<mark>' + esc(ln) + '</mark>' : esc(ln));
  }
  const html = out.length ? out.join('\n')
    : '<span class="muted">' + (scope === 'events' ? '（近期无连接事件）' : '（无日志）') + '</span>';
  return { html: html, matched: matched, total: lines.length };
}
function scheduleRender(force) {
  if (logRenderTimer) { clearTimeout(logRenderTimer); logRenderTimer = null; }
  if (force) { renderLogBuffer(); return; }
  logRenderTimer = setTimeout(() => { logRenderTimer = null; renderLogBuffer(); }, 250);
}
function renderLogBuffer() {
  const out = $('#log-out'); if (!out) return;
  const scopeEl = $('#log-scope');
  const scope = scopeEl ? scopeEl.value : 'all';
  const stick = isStick(out);
  const res = renderLog(logBuf.join('\n'), scope, DETAIL && DETAIL.log_events);
  out.innerHTML = res.html;
  if (stick) out.scrollTop = out.scrollHeight;
  const t = $('#log-time');
  if (t) t.textContent = '更新于 ' + new Date().toLocaleTimeString() + ' · ' + logBuf.length + ' 行'
    + (res.matched ? '，' + res.matched + ' 条连接事件' : '') + (scope === 'events' ? ' · 仅连接事件' : '');
}
function stopStream() {
  if (logES) { logES.close(); logES = null; }
  if (logRenderTimer) { clearTimeout(logRenderTimer); logRenderTimer = null; }
}
function startStream() {
  stopStream();
  const key = ACTIVE; if (!key || ACTIVE === 'overview') return;
  const out = $('#log-out'); if (out) out.textContent = '连接日志流…';
  const es = new EventSource('/api/stream?name=' + encodeURIComponent(key) + '&tail=' + tail(key));
  logES = es;
  es.onmessage = ev => {
    let line; try { line = JSON.parse(ev.data); } catch (e) { line = ev.data; }
    logBuf.push(line);
    if (logBuf.length > 2000) logBuf = logBuf.slice(-2000);
    if (!logPause) scheduleRender(false);
  };
  es.onerror = () => { /* EventSource 自动重连 */ };
}
function restartLog() {
  logBuf = [];
  const out = $('#log-out'); if (out) out.textContent = '连接日志流…';
  if ($('#log-live') && $('#log-live').checked) startStream(); else loadOnce();
}
async function loadOnce() {
  const key = ACTIVE; if (!key || ACTIVE === 'overview') return;
  const out = $('#log-out'); if (out) out.textContent = '加载中…';
  try {
    const r = await fetch('/api/logs?name=' + encodeURIComponent(key) + '&tail=' + tail(key));
    const txt = await r.text();
    logBuf = txt.replace(/\s+$/, '').split('\n').filter(l => l.length);
    renderLogBuffer();
  } catch (e) { if (out) out.textContent = '加载失败：' + e.message; }
}

/* ---------------- 历史连接事件（持久化） ---------------- */
async function loadHistoryEvents(key) {
  const out = $('#hist-out'); if (!out) return;
  out.textContent = '加载中…';
  try {
    const rows = await fetch('/api/events?name=' + encodeURIComponent(key) + '&tail=' + tail(key)).then(r => r.json());
    if (ACTIVE !== key || !$('#hist-out')) return;
    if (!rows.length) {
      out.innerHTML = '<span class="muted">（暂无持久化连接事件；后台采集器会在客户端连接/断开时记录）</span>';
      return;
    }
    out.innerHTML = rows.map(r => '<mark>' + esc(fmtTs(r.ts) + '  [' + r.source + '] ' + r.line) + '</mark>').join('\n');
  } catch (e) { if (out) out.textContent = '加载失败：' + e.message; }
}

/* ---------------- token (仅 --auth 时显示) ---------------- */
function token() { return localStorage.getItem('vpnctl_token') || ''; }
const _ti = $('#token');
if (_ti) { _ti.value = token(); _ti.addEventListener('input', e => localStorage.setItem('vpnctl_token', e.target.value.trim())); }

/* ---------------- loop ---------------- */
function setupTimer() {
  if (autoTimer) clearInterval(autoTimer);
  if ($('#auto').checked) autoTimer = setInterval(tick, 4000);
}
async function tick() {
  try { await fetchList(); } catch (e) { /* ignore */ }
  if (ACTIVE !== 'overview') await loadDetail(ACTIVE, false);
}
$('#auto').addEventListener('change', setupTimer);
$('#refresh').addEventListener('click', () => { fetchList().catch(() => {}); if (ACTIVE !== 'overview') loadDetail(ACTIVE, false); if (TAB === 'logs') restartLog(); if (TAB === 'events' && ACTIVE !== 'overview') loadHistoryEvents(ACTIVE); });

async function boot() {
  try { await fetchList(); } catch (e) { $('#content').innerHTML = '<div class="empty">无法连接后端：' + esc(e.message) + '</div>'; return; }
  go('overview');   // 打开即进入数据看板
  setupTimer();
}
boot();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vpnctl-web"

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- Server-Sent Events (chunked) ----
    def _sse_write(self, payload):
        data = payload.encode("utf-8")
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream_logs(self, name, tail):
        d = vpnctl.VPN_BY_KEY[name]
        if d["kind"] == "docker":
            argv = vpnctl.docker_argv(["logs", "--tail", str(tail), "-f", d["target"]])
        else:
            argv = vpnctl.journal_argv(d["target"], tail, follow=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception as e:
            self._sse_write("data: " + json.dumps("启动日志进程失败: %s" % e) + "\n\n")
            self._sse_write("0\r\n\r\n")
            return
        fd = proc.stdout.fileno()
        buf = b""
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 15)
                if not r:
                    self._sse_write(": keepalive\n\n")  # heartbeat + disconnect detection
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._sse_write("data: " + json.dumps(
                        line.decode("utf-8", "replace").rstrip("\r"), ensure_ascii=False) + "\n\n")
            if buf:
                self._sse_write("data: " + json.dumps(
                    buf.decode("utf-8", "replace"), ensure_ascii=False) + "\n\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                self._sse_write("0\r\n\r\n")
            except Exception:
                pass
            self.close_connection = True

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _authed(self):
        if not REQUIRE_TOKEN:
            return True
        supplied = self.headers.get("X-VPNCTL-Token", "")
        if not supplied:
            q = parse_qs(urlparse(self.path).query)
            supplied = (q.get("token") or [""])[0]
        return secrets.compare_digest(supplied, TOKEN)

    def _name(self, query):
        q = parse_qs(query)
        name = (q.get("name") or [""])[0]
        return name if name in vpnctl.VPN_BY_KEY else None

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/list":
            return self._json(200, {"server_ip": vpnctl.server_ip(),
                                    "docker_mode": vpnctl.docker_mode(),
                                    "read_only": READ_ONLY,
                                    "auth": REQUIRE_TOKEN,
                                    "vpns": vpnctl.all_states()})
        if u.path == "/api/detail":
            name = self._name(u.query)
            if not name:
                return self._json(400, {"error": "unknown vpn"})
            return self._json(200, vpnctl.detail(name))
        if u.path == "/api/raw":
            name = self._name(u.query)
            if not name:
                return self._json(400, {"error": "unknown vpn"})
            return self._send(200, vpnctl.raw_status(name), "text/plain; charset=utf-8")
        if u.path == "/api/clients":
            name = self._name(u.query)
            if not name:
                return self._json(400, {"error": "unknown vpn"})
            return self._send(200, vpnctl.clients(name), "text/plain; charset=utf-8")
        if u.path == "/api/logs":
            name = self._name(u.query)
            if not name:
                return self._json(400, {"error": "unknown vpn"})
            q = parse_qs(u.query)
            tail = int((q.get("tail") or ["60"])[0])
            return self._send(200, vpnctl.logs(name, tail), "text/plain; charset=utf-8")
        if u.path == "/api/stream":
            name = self._name(u.query)
            if not name:
                return self._json(400, {"error": "unknown vpn"})
            q = parse_qs(u.query)
            tail = int((q.get("tail") or ["80"])[0])
            return self._stream_logs(name, tail)
        if u.path == "/api/events":
            q = parse_qs(u.query)
            name = (q.get("name") or [""])[0] or None
            if name and name not in vpnctl.VPN_BY_KEY:
                return self._json(400, {"error": "unknown vpn"})
            tail = int((q.get("tail") or ["50"])[0])
            return self._json(200, vpnctl.store.recent_events(name, tail))
        if u.path == "/api/audit":
            q = parse_qs(u.query)
            tail = int((q.get("tail") or ["50"])[0])
            return self._json(200, vpnctl.store.recent_audit(tail))
        if u.path == "/api/health":
            return self._json(200, health_cached())
        if u.path == "/api/alerts":
            q = parse_qs(u.query)
            tail = int((q.get("tail") or ["50"])[0])
            return self._json(200, vpnctl.store.recent_alerts(tail))
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/action":
            return self._json(404, {"error": "not found"})
        if READ_ONLY:
            return self._json(403, {"error": "面板处于只读模式"})
        if not self._authed():
            return self._json(401, {"error": "令牌无效或缺失"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._json(400, {"error": "bad json: " + str(e)})
        name = body.get("name")
        action = body.get("action")
        keep = bool(body.get("keep_conflicts"))
        if action == "down-all":
            actor = self.client_address[0]
            try:
                results = vpnctl.down_all(actor=actor, source="web")
            except Exception as e:
                return self._json(500, {"error": str(e)})
            stopped = [r["key"] for r in results if r.get("ok")]
            failed = [r["key"] for r in results if not r.get("ok")]
            return self._json(200 if not failed else 500,
                              {"ok": not failed, "action": "down-all",
                               "stopped": stopped, "failed": failed, "results": results})
        if action == "clear-alerts":
            try:
                import watchdog
                keep = watchdog.active_alert_titles()
                cleared, kept = vpnctl.store.clear_alerts(keep)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            return self._json(200, {"ok": True, "action": "clear-alerts",
                                    "cleared": cleared, "kept": kept})
        if name not in vpnctl.VPN_BY_KEY:
            return self._json(400, {"error": "unknown vpn: " + str(name)})
        if action not in ("up", "down", "restart", "enable", "disable"):
            return self._json(400, {"error": "unknown action: " + str(action)})
        try:
            actor = self.client_address[0]
            if action in ("up", "restart", "enable"):
                res = getattr(vpnctl, action)(name, stop_peers=not keep,
                                               actor=actor, source="web")
            else:
                res = getattr(vpnctl, action)(name, actor=actor, source="web")
        except Exception as e:
            return self._json(500, {"error": str(e)})
        return self._json(200 if res["ok"] else 500, res)


def _systemctl_state():
    try:
        active = subprocess.run(["systemctl", "is-active", WEB_SERVICE],
                                capture_output=True, text=True, timeout=2).stdout.strip()
        enabled = subprocess.run(["systemctl", "is-enabled", WEB_SERVICE],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        return active, enabled
    except Exception:
        return "", ""


def _print_bind_help(exc, host, port):
    print(f"[x] 无法监听 {host}:{port}：{exc.strerror or exc}", file=sys.stderr)
    if exc.errno == errno.EADDRINUSE:
        active, enabled = _systemctl_state()
        if active in ("active", "activating", "reloading"):
            print("    面板已经由系统服务在后台启动，不需要再执行 python3 web.py。", file=sys.stderr)
            print(f"    直接用浏览器打开：http://{host}:{port}/", file=sys.stderr)
            print("    查看运行状态：systemctl status vpn-manager-web", file=sys.stderr)
            print("    如需手动启动，先停止服务：sudo systemctl stop vpn-manager-web", file=sys.stderr)
            return
        print("    端口已被占用，通常说明面板已经在运行。可以这样处理：", file=sys.stderr)
        print(f"      1) 直接打开：http://{host}:{port}/", file=sys.stderr)
        print(f"      2) 查看占用进程：ss -ltnp 'sport = :{port}'", file=sys.stderr)
        print("      3) 若由系统服务托管：sudo systemctl stop vpn-manager-web", file=sys.stderr)
        print(f"      4) 或换个端口启动：python3 web.py --port {port + 1}", file=sys.stderr)
        if enabled == "enabled":
            print("    提示：系统服务已设为开机自启，可用 sudo systemctl restart vpn-manager-web 重新拉起。", file=sys.stderr)
    elif exc.errno == errno.EACCES:
        print(f"    端口 {port} 需要特权（<1024）。改用高位端口，例如：", file=sys.stderr)
        print("      python3 web.py --port 8787", file=sys.stderr)
    elif exc.errno == errno.EADDRNOTAVAIL:
        print(f"    地址 {host} 不是本机地址。改用可用的监听地址，例如：", file=sys.stderr)
        print("      python3 web.py --host 127.0.0.1", file=sys.stderr)
    else:
        print("    请检查监听地址/端口，或先用 ss -ltnp 查看占用情况。", file=sys.stderr)


def _open_browser(host, port):
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/"
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        print(f"  已在浏览器打开: {url}")
    except Exception:
        pass


def main():
    global READ_ONLY, REQUIRE_TOKEN, TOKEN
    p = argparse.ArgumentParser(description="vpnctl 网页面板")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--read-only", action="store_true", help="禁止一切写操作")
    p.add_argument("--auth", action="store_true", help="要求操作令牌（默认不要求，打开即可用）")
    p.add_argument("--no-auth", action="store_true", help="兼容旧参数，默认即无需令牌")
    p.add_argument("--token", help="指定操作令牌（配合 --auth 使用）")
    p.add_argument("--no-collect", action="store_true",
                   help="不启动后台日志采集器（默认启动：连接事件落库）")
    p.add_argument("--no-watch", action="store_true",
                   help="不启动后台健康守护（默认启动：状态/证书/IP 告警）")
    p.add_argument("--no-open", action="store_true",
                   help="启动后不自动打开浏览器（默认在本机图形环境下自动打开）")
    args = p.parse_args()
    READ_ONLY = args.read_only
    REQUIRE_TOKEN = args.auth and not args.no_auth
    TOKEN = (args.token or load_token()) if REQUIRE_TOKEN else ""

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        _print_bind_help(e, args.host, args.port)
        return 2

    collector_status = "未启动"
    if not args.no_collect:
        try:
            import collector
            _lock = collector.acquire_lock()
            if _lock is None:
                collector_status = "已有采集器在运行，跳过"
            else:
                collector.LogCollector().start()
                collector_status = "已启动（连接事件落库 state/vpnplat.db）"
        except Exception as e:  # 采集器失败不应阻断面板
            collector_status = f"启动失败：{e}"

    watchdog_status = "未启动"
    if not args.no_watch:
        try:
            import watchdog
            _wlock = watchdog.acquire_lock()
            if _wlock is None:
                watchdog_status = "已有守护进程在运行，跳过"
            else:
                _wd = watchdog.Watchdog()
                _wd.start()
                watchdog_status = "已启动（状态/证书/IP 告警）"
        except Exception as e:
            watchdog_status = f"启动失败：{e}"

    print("=" * 56)
    print(f"  VPN 管控平台  http://{args.host}:{args.port}/")
    print(f"  日志采集器: {collector_status}")
    print(f"  健康守护: {watchdog_status}")
    print(f"  只读模式: {'是' if READ_ONLY else '否'}   令牌校验: {'开' if REQUIRE_TOKEN else '关（打开即可用）'}")
    if REQUIRE_TOKEN:
        print(f"  操作令牌: {TOKEN}")
        print(f"  （已保存到 {TOKEN_FILE}，在页面右上角填入后即可启停）")
    print("  远程访问建议: ssh -L %d:127.0.0.1:%d <user>@<server>" % (args.port, args.port))
    print("=" * 56)
    if not args.no_open:
        _open_browser(args.host, args.port)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
