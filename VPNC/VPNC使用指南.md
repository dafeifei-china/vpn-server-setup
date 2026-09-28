# VPNC (Cisco 兼容 IPSec) 使用指南（Docker 版）

> 部署脚本 `setup-vpnc.sh`。基于 Ubuntu 20.04 + strongSwan 自建镜像，容器名 `vpnc-server`，镜像 `vpnc-vpn`。
> 协议兼容 **Cisco IPSec VPN 客户端（vpnc / network-manager-vpnc）**：
> **IKEv1 主动模式(Aggressive) + PSK(组密钥) + XAUTH(用户名/密码) + MODECFG(下发 IP/DNS)**。

## 连接信息

| 项 | 值 |
|---|---|
| 服务器地址 | `10.20.8.90` |
| 端口 | `500/udp`（IKE）+ `4500/udp`（NAT-T） |
| 认证方式 | **PSK(组密钥) + XAUTH(用户名/密码)**，无证书 |
| 组密钥(PSK) | `test` |
| 用户名 / 密码 | `test` / `test` |
| VPN 网段 | `192.168.47.0/24`（客户端地址池 `192.168.47.10-192.168.47.250`） |
| 客户端设备名 | `tun0`（vpnc 默认） |
| 容器 / 镜像 | `vpnc-server` / `vpnc-vpn` |

配置/文件：

| 文件 | 作用 |
|---|---|
| `setup-vpnc.sh` | 一键部署（生成配置 → 构建镜像 → 启动容器；启动前自动停 `strongswan-server`/`ipsec-vpn-server`） |
| `Dockerfile` | 镜像定义（Ubuntu 20.04 + strongswan + libcharon-extra-plugins + iptables） |
| `entrypoint.sh` | 容器入口：ip_forward、NAT、MSS、启动 strongSwan |
| `etc/ipsec.conf` | 连接定义 `conn cisco-vpnc`（`aggressive=yes`、`authby=xauthpsk`、`xauth=server`） |
| `etc/ipsec.secrets` | `: PSK "<组密钥>"` 与 `<用户名> : XAUTH "<密码>"` |
| `etc/strongswan-vpnc.conf` | 关键：打开 strongSwan 5.8+ 默认禁用的 **Aggressive Mode + PSK** |
| `clients/vpnc.conf` | 可直接分发的 vpnc 客户端配置 |

> ⚠️ **500/4500 与 strongSwan / L2TP-IPSEC 冲突，只能三选一**。`setup-vpnc.sh` 会自动停 `strongswan-server` 和 `ipsec-vpn-server`。

---

## 1. 服务端管理

```bash
sg docker -c "docker ps --filter name=vpnc-server"
sg docker -c "docker stop vpnc-server"
sg docker -c "docker start vpnc-server"
sg docker -c "docker logs -f vpnc-server"
sg docker -c "docker exec -it vpnc-server ipsec statusall"      # 看 cisco-vpnc / SA 是否 ESTABLISHED
sg docker -c "docker exec -it vpnc-server cat /var/log/charon.log"   # 详细 IKE 日志
ss -ulnp | grep -E ':(500|4500)\b'
```

- 容器策略 `--restart unless-stopped`：手动 `stop` 后重启机器不会自启。
- 容器用 `--net host --privileged`（IPsec 常规做法），并挂 `/lib/modules:ro`。

### 改账号 / 组密钥 / 网段
改脚本顶部 `USER_NAME` / `USER_PASS` / `VPN_PSK` / `VPN_SUBNET` / `VPN_POOL` 后重跑。
> 注意：**Aggressive Mode 的 PSK 出于安全考虑，客户端填的“组名(ID)”服务端不校验**（`right=%any` + `: PSK` 通配）。

### 切回 strongSwan / L2TP-IPSEC
```bash
sg docker -c "docker stop vpnc-server"
sg docker -c "docker start strongswan-server"     # 或 ipsec-vpn-server
```

---

## 2. 客户端连接

> 一句话：**服务器 = `10.20.8.90`；组密钥 `test`，用户名/密码 `test`**。没有证书。

### 0. 确认客户端装了 vpnc
```bash
dpkg -l | grep -E '^ii\s+vpnc|network-manager-vpnc'
which vpnc || ls /usr/sbin/vpnc        # UOS 在 /usr/sbin/vpnc（非登录 shell 的 PATH 可能没有）
```

### 1. Linux 命令行（vpnc，最快验证）
`vpnc` 的组密钥/密码**只能写在配置文件里**，命令行参数只支持 gateway/id/username：
```bash
sudo tee /etc/vpnc/vpnc.conf >/dev/null <<'EOF'
IPSec gateway 10.20.8.90
IPSec ID test
IPSec secret test
Xauth username test
Xauth password test
IKE Authmode psk
EOF
sudo chmod 600 /etc/vpnc/vpnc.conf
sudo vpnc /etc/vpnc/vpnc.conf
```
- 成功：`ip -4 addr show tun0` 有 `192.168.47.x`；`ping -c3 8.8.8.8` 通。
- 断开：`sudo vpnc-disconnect`。
- 无 ping 网关的说法：IPSec mode-config 不给服务端分配隧道 IP，**验证以“能否出网”为准**（ping 8.8.8.8）。

### 2. UOS / Linux 图形界面（NetworkManager）
需已装 `network-manager-vpnc`。**控制中心 → 网络 → VPN → 添加 → Cisco Compatible VPN (vpnc)**，按下表填：

| 界面字段 | 填什么 |
|---|---|
| 网关 | `10.20.8.90` |
| 组名 (Group name) | `test` |
| 组密钥 (Group secret) | `test` |
| 用户名 | `test` |
| 密码 | `test` |
| 域 (Domain) | 留空 |

- 保存后连接；连上后 `ip addr show tun0` 应为 `192.168.47.x`。
- 无头 `nmcli` 激活可能因缺桌面密钥代理失败（`没有有效的密钥`）——用图形界面，或直接用 `vpnc` CLI。

### 3. Windows
用 **The GreenBow**、**Shrew Soft VPN** 或 Cisco VPN Client（老系统），类型选 IPSec/PSK+XAUTH，填组密钥 `test`、用户名/密码 `test`。

### 4. 直接分发配置
`clients/vpnc.conf` 即客户端配置，拷到客户端 `/etc/vpnc/vpnc.conf` 后 `sudo vpnc /etc/vpnc/vpnc.conf`。

---

## 3. 端口 / 并存

| | VPNC | OpenConnect | OpenVPN | L2TP/IPsec | strongSwan | L2TP_PLAIN | PPTP |
|---|---|---|---|---|---|---|---|
| 端口 | 500,4500/udp | 8443/tcp+udp | 1194/udp | 500,4500/udp | 500,4500/udp | 1701/udp | 1723/tcp+GRE |
| 网段 | 192.168.47.0/24 | 192.168.46.0/24 | 10.99.99.0/24 | 192.168.42.0/24 | 10.10.10.0/24 | 192.168.44.0/24 | 192.168.45.0/24 |

- VPNC 与 **strongSwan / L2TP-IPSEC 互斥**（都用 500/4500）。
- 与 OpenVPN / L2TP_PLAIN / PPTP / OpenConnect **可并存**。
- 防火墙/安全组需放行 **UDP 500 与 4500**（NAT 后靠 4500 NAT-T）。

---

## 4. 服务器 IP 变化

VPNC **无证书、不绑 IP**。换 IP 后：**只改客户端填的服务器地址**即可（`IPSec gateway <新IP>`）。
> 若在 NAT 后，服务端脚本用 `SERVER_IP=<公网IP> ./setup-vpnc.sh`；客户端填公网 IP。

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 客户端报 `ISAKMP_N_INVALID_EXCHANGE_TYPE`，服务端日志 `Aggressive Mode PSK disabled for security reasons` | **strongSwan 5.8+ 默认禁用主动模式+PSK**。脚本已内置 `/etc/strongswan.d/vpnc.conf` 里的 `i_dont_care_about_security_and_use_aggressive_mode_psk = yes`；手改配置时务必加上 |
| 客户端报 `NO_PROPOSAL_CHOSEN` | 客户端/服务端 IKE 提议不匹配；服务端 `ike=` 已含 aes/3des + sha1/md5 + modp1024，覆盖 vpnc 默认 |
| 连不上、服务端 `charon.log` 无记录 | 500/4500 没放行，或客户端地址填错；先 `nc -vz <IP> 500`（UDP 不适用 nc，看服务端抓包 `tcpdump -ni <网卡> 'udp port 500'`） |
| `ipsec statusall` 没有 `cisco-vpnc` | 容器没起来或 500/4500 被占（strongSwan/L2TP-IPSEC）；`docker logs vpnc-server` |
| 连上但打不开网页 | 查服务端 `ip_forward`、NAT（`iptables -t nat -S \| grep 192.168.47`）、客户端默认路由是否走 `tun0` |
| 图形界面报 `没有有效的密钥` | 无头/密钥代理不可达；用 `vpnc` CLI，或注销重登后再试 |
| `vpnc` 命令找不到 | 它在 `/usr/sbin/vpnc`，用绝对路径或 `sudo vpnc` |
| 想换成证书认证 | 当前是 PSK+XAUTH（最兼容 vpnc）；如需证书，改 `authby` 并生成证书，复杂且部分客户端不支持 |

排障：
```bash
sg docker -c "docker logs --tail 100 vpnc-server"
sg docker -c "docker exec -it vpnc-server ipsec statusall"
sg docker -c "docker exec -it vpnc-server cat /var/log/charon.log"
sg docker -c "docker exec vpnc-server iptables -t nat -S POSTROUTING | grep 192.168.47"
```

---

## 6. 卸载

```bash
sg docker -c "docker rm -f vpnc-server"
sg docker -c "docker rmi vpnc-vpn"
# 清理 NAT 残留（宿主机上，若容器内规则未随容器消失）
sudo iptables -t nat -D POSTROUTING -s 192.168.47.0/24 ! -d 192.168.47.0/24 -j MASQUERADE
```

> 不影响其它 VPN。卸载后如需 strongSwan，`sg docker -c "docker start strongswan-server"`。
