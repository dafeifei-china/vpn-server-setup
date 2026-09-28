# PPTP 使用指南（Docker 版）

> 部署脚本 `setup-pptp.sh`。基于 Ubuntu 20.04 + `pptpd` 自建镜像，容器名 `pptp-server`，镜像 `pptp-vpn`。
>
> ⚠️ **PPTP 早已不安全**（MS-CHAPv2 可被破解，MPPE 也很弱），仅用于兼容旧客户端/测试。生产请用 `openvpn/`、`Strongswan/` 或 `L2TP_IPSEC/`。

## 连接信息

| 项 | 值 |
|---|---|
| 服务器地址 | `10.20.8.90` |
| 类型 | PPTP |
| 端口 | `1723/tcp` + **GRE（协议 47）** |
| 用户名 / 密码 | `test` / `test` |
| VPN 网段 | `192.168.45.0/24`（服务端 `192.168.45.1`，地址池 `.10-.250`） |
| MPPE | 默认要求（`require-mppe-128`） |

配置/文件：

| 文件 | 作用 |
|---|---|
| `setup-pptp.sh` | 一键部署（生成配置 → 构建镜像 → 启动容器） |
| `Dockerfile` | 镜像定义（Ubuntu 20.04 + pptpd + iptables） |
| `entrypoint.sh` | 容器入口：ip_forward、NAT、MSS、加载 conntrack 模块、启动 pptpd |
| `etc/pptpd.conf` | `localip` / `remoteip` |
| `etc/ppp/pptpd-options` | pppd 参数（认证、MPPE、DNS、MTU） |
| `etc/ppp/chap-secrets` | 账号密码 |

---

## 1. 服务端管理

```bash
sg docker -c "docker ps --filter name=pptp-server"          # 状态
sg docker -c "docker stop pptp-server"                      # 停用
sg docker -c "docker start pptp-server"                     # 启用
sg docker -c "docker logs -f pptp-server"                   # 日志
sg docker -c "docker exec pptp-server ps -ef | grep pptpd"  # 进程
ss -tlnp | grep 1723                                        # 确认监听
```

- 容器策略 `--restart unless-stopped`：手动 `stop` 后重启机器不会自启。
- 重新部署/改配置：编辑脚本顶部变量后重跑 `./setup-pptp.sh`（重建镜像与容器）。

### 改账号
编辑 `etc/ppp/chap-secrets`，格式 `用户名 pptpd 密码 *`，然后重跑脚本或重建容器。

### 改网段
改脚本里的 `VPN_SUBNET/LOCAL_IP/POOL_START/POOL_END` 后重跑。

### MPPE 开关
`MPPE=yes`（默认，`require-mppe-128`）/ `MPPE=no ./setup-pptp.sh`（不要求 MPPE）。容器为 `--privileged` 且挂载 `/lib/modules`，可加载 `ppp_mppe`。

---

## 2. 客户端连接

> PPTP 需要 **TCP 1723 + GRE（协议 47）** 都能到达服务器；很多网络会屏蔽 GRE，这是 PPTP 连不上的最常见原因。

### UOS / Linux（NetworkManager）

```bash
sudo nmcli connection add type vpn con-name pptp vpn-type pptp \
  vpn.data "gateway=10.20.8.90,user=test,password-flags=0,require-mppe=yes" \
  vpn.secrets "password=test"
sudo nmcli connection up pptp
```

图形界面：控制中心 → 网络 → VPN → 添加 → **点对点隧道协议 (PPTP)**：网关 `10.20.8.90`、用户名/密码 `test`/`test`，高级里勾选“使用点对点加密 (MPPE)”。

验证：`ip -4 addr show ppp0`（应拿到 `192.168.45.x`），`ping -c3 192.168.45.1`。

### Windows
设置 → 网络和 Internet → VPN → 添加：
- VPN 提供商：`Windows（内置）`
- VPN 类型：**PPTP**
- 服务器名称或地址：`10.20.8.90`
- 用户名/密码：`test` / `test`

### macOS / iOS
**新版 macOS/iOS 已移除 PPTP**，无法连接；请改用 OpenVPN/strongSwan/L2TP。

### Android
原生 VPN 一般不再支持 PPTP；如需可用第三方客户端，或改用其它协议。

---

## 3. 端口 / 并存

| | PPTP | OpenVPN | L2TP/IPsec | strongSwan | VPNC | L2TP_PLAIN | OpenConnect |
|---|---|---|---|---|---|---|---|
| 端口 | `1723/tcp` + GRE | 1194/udp | 500,4500/udp | 500,4500/udp | 500,4500/udp | 1701/udp | 8443/tcp+udp |
| 网段 | 192.168.45.0/24 | 10.99.99.0/24 | 192.168.42.0/24 | 10.10.10.0/24 | 192.168.47.0/24 | 192.168.44.0/24 | 192.168.46.0/24 |

- PPTP 用 `1723/tcp` + GRE，与其它 VPN **不冲突**，可同时运行。
- 但 **strongSwan / L2TP/IPsec / VPNC 三者都占 500/4500，只能三选一**。

---

## 4. 服务器 IP 变化

PPTP **没有证书**，换 IP 只需：
1. 重跑脚本或改客户端里的服务器地址为新 IP。
2. 防火墙/安全组放行新 IP 的 `1723/tcp` 与 GRE。

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 客户端卡在“正在连接/验证” | GRE(协议 47) 被网络/防火墙拦截；放行 GRE，或换网络 |
| 认证失败 | `chap-secrets` 账号/密码或服务名（`pptpd`）不匹配 |
| 连上但打不开网页 | 检查服务端 NAT/`ip_forward`；确认客户端默认路由走 `ppp0` |
| 容器反复重启 | 多为 `pptpd.conf` 语法问题。**`remoteip` 范围必须用末位八位组写法**（`192.168.45.10-250`），写成 `192.168.45.10-192.168.45.250` 会被当主机名解析而启动失败 |
| 要求 MPPE 但客户端不支持 | 服务端 `MPPE=no` 重跑，或客户端勾选 MPPE |
| 想用 IKEv2/更安全方案 | 改用 `Strongswan/` 或 `L2TP_IPSEC/` |

排障：

```bash
sg docker -c "docker logs --tail 100 pptp-server"
sg docker -c "docker exec pptp-server iptables -t nat -S POSTROUTING | grep 192.168.45"
ss -tlnp | grep 1723
```

---

## 6. 卸载

```bash
sg docker -c "docker rm -f pptp-server"
sg docker -c "docker rmi pptp-vpn"
# 清理 NAT 残留（在宿主机上）
sudo iptables -t nat -D POSTROUTING -s 192.168.45.0/24 ! -d 192.168.45.0/24 -j MASQUERADE
sudo iptables -D FORWARD -s 192.168.45.0/24 -j ACCEPT
sudo iptables -D FORWARD -d 192.168.45.0/24 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -D FORWARD -s 192.168.45.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
```

> 不影响 OpenVPN / strongSwan / L2TP。
