# VPN 统一管理（vpnctl A+B 方案）

把本目录下 7 套独立 VPN recipe 统一到**一个 CLI 内核 + 一个本地网页面板**。
**不修改任何现有文件**——只读取状态、调用 `docker` / `systemctl` 启停。

- **A 内核**：`vpnctl.py`（纯 Python 标准库，无第三方依赖）
- **B 外壳**：`web.py`（stdlib `http.server`，默认只绑 `127.0.0.1`）

## 目录

```
manager/
├── vpnctl.py               # A：核心库 + CLI（唯一干活的层）
├── vpnctl -> vpnctl.py     # 便捷软链
├── web.py                  # B：网页面板（复用 vpnctl）
├── .token                  # 运行后自动生成的写操作令牌（0600）
├── install-web-service.sh  # 可选：装成 systemd 服务
├── install-sudoers.sh      # 可选：授权面板无密码管理宿主机 xl2tpd
├── systemd/
│   └── vpn-manager-web.service
└── README.md
```

## A. CLI 用法

```bash
cd manager

./vpnctl list                    # 全部状态一览（--json 机器可读）
./vpnctl doctor                  # 自检：docker/端口/互斥冲突/证书 IP
./vpnctl ip                      # 当前服务器 IP（DHCP 会漂，先看这个）

./vpnctl status vpnc             # 单个详情
./vpnctl info   vpnc             # 连接信息（不显示密钥内容）

./vpnctl up      strongswan      # 启动；自动停掉互斥组内其它服务
./vpnctl down    strongswan      # 仅停止
./vpnctl restart openvpn
./vpnctl enable  strongswan      # 启动并恢复自启（docker --restart=unless-stopped / systemctl enable）
./vpnctl disable vpnc            # 停止并禁止自启（docker --restart=no / systemctl disable）
./vpnctl logs    openvpn --tail 40
./vpnctl clients l2tp-plain      # 判断客户端是否连上（服务端证据）
./vpnctl raw     pptp            # 原始状态输出（ps/logs/systemctl status）
```

> `enable` = 启动 + 恢复自启；`disable` = 停止 + 禁止自启；`down` 只停止、不动自启策略。

支持的 key：`openvpn` `l2tp-ipsec` `l2tp-plain` `strongswan` `pptp` `openconnect` `vpnc`

**互斥逻辑**：`strongswan` / `l2tp-ipsec` / `vpnc` 同属 `ipsec500` 组（抢 500,4500/udp）。
`up` 默认先停同组其它服务；想强制共存（一般不这样）加 `--keep-conflicts`。

```bash
./vpnctl up strongswan
# [互斥] 已停止: VPNC (Cisco IPSec)
# [OK] up strongswan
```

## B. 网页面板

```bash
cd manager
python3 web.py                       # http://127.0.0.1:8787
python3 web.py --port 9000           # 换端口
python3 web.py --read-only           # 只读，禁止一切启停
python3 web.py --auth                # 开启令牌校验（默认关闭）
```

- **左侧导航 + 数据看板**：总览统计卡（服务总数 / 运行中 / 已停止 / 端口冲突）、各服务状态条、互斥组冲突提示、全部服务表格（可一键启停/进入管理）。
- **每种 VPN 一个独立页面**（OpenVPN / L2TP-IPsec / L2TP 纯 / strongSwan / PPTP / OpenConnect / VPNC），页面内分 3 个标签：
  - **状态概览**：运行状态、监听端口、隧道网段、网关、说明、校验命令；
  - **配置信息**：服务端配置一览（类型/目标/端口/网段/互斥组/文档/客户端凭据路径/校验命令），以及**本页运行偏好**——「启动/重启时自动停用互斥服务」（映射 `--keep-conflicts`）与「日志默认行数」（存本机浏览器 localStorage，每套 VPN 独立）；
  - **实时日志**：直接抓取服务器日志（Docker 用 `docker logs <容器>`，宿主机服务用 `journalctl -u <unit>`），同时包含**服务自身运行日志**与**客户端连接/断开时服务端产生的事件**；连接事件按各协议关键词高亮，可切换「全部 / 仅连接事件」、行数、暂停，勾选「实时」后每 2 秒自动刷新并跟随最新。
- 页面每 4 秒自动刷新状态；实时日志标签页另按 2 秒刷新。
- **默认无需登录/令牌**：只绑 `127.0.0.1`，打开网页即可启停。需要令牌时用 `--auth`（令牌在启动时打印，也存在 `manager/.token`，此时页面右上角出现输入框）。
- 默认只监听 `127.0.0.1`，**不要**直接暴露到公网。远程访问走 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 uos@10.20.8.90
# 然后本机浏览器打开 http://127.0.0.1:8787
```

### 装成 systemd 服务（可选）

```bash
cd manager
./install-web-service.sh             # sudo 密码：1
sudo journalctl -u vpn-manager-web.service -f
```

## 安全边界

- 只绑 `127.0.0.1`，默认**不需要令牌**即可启停；如需令牌加 `--auth`（可用 `--read-only` 彻底禁用写操作）。
- 面板**不显示任何密钥内容**，只给出凭据文件路径（`L2TP_IPSEC/vpn.env`、`clients/` 等）。
- 不触碰现有 `setup-*.sh` 与生成的配置；容器/服务仍可继续用原脚本管理。

## 已知限制

- 网页 `up/down` 依赖运行面板的账号能访问 Docker（`docker` 组或 root）。`./vpnctl doctor` 会显示当前访问方式。
- `l2tp-plain` 是宿主机 `xl2tpd.service`（SysV 包装），启停/禁用以及读取系统日志都需要 root。若面板以普通用户运行，执行一次
  `sudo ./install-sudoers.sh` 即授予该用户对 `xl2tpd` 的 `systemctl start/stop/restart/enable/disable/is-active/status`
  与 `journalctl -u xl2tpd` 无密码权限（仅这两类，其它命令仍需密码）；也可直接用 root 运行面板。
  否则普通用户读不到系统日志，实时日志会显示 `-- No entries --`。
- SysV 服务不认 `systemctl --now`，因此 `enable` = `enable` + `start`、`disable` = `stop` + `disable`（已由 `vpnctl` 显式处理）。
- 端口占用只做「监听探测」；500/4500 被互斥组共享，未运行的服务不认领端口。
