# OpenConnect (ocserv) 使用指南（Docker 版）

> 部署脚本 `setup-openconnect.sh`。基于 Ubuntu 20.04 + `ocserv` 自建镜像，容器名 `openconnect-server`，镜像 `openconnect-vpn`。
> 协议兼容 **Cisco AnyConnect / OpenConnect**，走 TLS(TCP) + DTLS(UDP)。

## 连接信息

| 项 | 值 |
|---|---|
| 服务器地址 | `10.20.8.90` |
| 端口 | `8443/tcp`（TLS）+ `8443/udp`（DTLS） |
| 认证方式 | **证书认证**（`auth = "certificate"`）；登录名取自客户端证书 CN |
| 登录名 | `test`（= 客户端证书 `client.crt` 的 CN） |
| VPN 网段 | `192.168.46.0/24` |
| 服务端设备名 | `vpns0`（客户端侧一般是 `tun0`/`vpn0`） |
| CA 证书 | `etc/ocserv/ca.crt`（客户端“CA 证书”填它） |
| 服务端证书 | `etc/ocserv/server.crt`（CA 签发，CN/SAN = 服务器 IP） |
| 客户端证书 / 私钥 | `etc/ocserv/client.crt` / `client.key`（客户端“用户证书/私钥”填它们） |
| 客户端 p12 | `etc/ocserv/client.p12`（Windows/macOS 导入用） |
| 服务端证书 pin-sha256 | `FU29I4gi0J9HKRCVk7hUR1PhI19Ls6lyBNLlcXdND2g=`（重签服务端证书后会变） |

配置/文件：

| 文件 | 作用 |
|---|---|
| `setup-openconnect.sh` | 一键部署（生成配置 + CA/服务端/客户端证书 → 构建镜像 → 启动容器） |
| `Dockerfile` | 镜像定义（Ubuntu 20.04 + ocserv + iptables） |
| `entrypoint.sh` | 容器入口：ip_forward、NAT、MSS、启动 ocserv |
| `etc/ocserv/ocserv.conf` | ocserv 主配置（`auth = "certificate"`、`ca-cert = ca.crt`、`cert-user-oid = 2.5.4.3`） |
| `etc/ocserv/ca.crt` / `ca.key` | 自建 CA（首次生成，之后复用） |
| `etc/ocserv/server.crt` / `server.key` | 服务端证书（CA 签发，CN/SAN = 服务器 IP） |
| `etc/ocserv/client.crt` / `client.key` / `client.p12` | 客户端证书/私钥/p12（CN = 登录名） |
| `clients/OpenConnect.nmconnection` | 可直接分发的 NetworkManager 连接文件 |

---

## 1. 服务端管理

```bash
sg docker -c "docker ps --filter name=openconnect-server"
sg docker -c "docker stop openconnect-server"
sg docker -c "docker start openconnect-server"
sg docker -c "docker logs -f openconnect-server"
sg docker -c "docker exec -it openconnect-server occtl show users"    # 在线用户
ss -tlnp | grep 8443
```

- 容器策略 `--restart unless-stopped`：手动 `stop` 后重启机器不会自启。
- 需要 `/dev/net/tun`（`sudo modprobe tun`），容器用 `--privileged` + 映射 `/dev/net/tun`。

### 改登录名 / 客户端证书
登录名 = 客户端证书的 CN（服务端 `cert-user-oid = 2.5.4.3`）。改法：编辑脚本 `USER_NAME`，删掉
`etc/ocserv/client.crt client.key client.p12` 后重跑（会重新签发客户端证书）；CA 与服务端证书不动。

### 改端口 / 网段
改脚本顶部 `PORT` / `VPN_SUBNET` 后重跑。

---

## 2. 客户端连接

> 一句话说明：**服务器 = `10.20.8.90:8443`；认证靠三个文件** `ca.crt`、`client.crt`、`client.key`
> （或打包好的 `client.p12`）。**没有密码**，登录名就是 `client.crt` 的 CN（默认 `test`）。

### 0. 先把证书文件拷到客户端
把服务器上这几个文件拷到客户端一个目录，例如 `~/Desktop/OpenConnect-cert/`：
`ca.crt`、`client.crt`、`client.key`（Windows/macOS 用 `client.p12`）。
> 服务器路径：`/home/uos/Desktop/VPN服务端搭建/OpenConnect/etc/ocserv/`

### 1. Linux 命令行（最快验证）
```bash
sudo openconnect \
  --certificate=$HOME/Desktop/OpenConnect-cert/client.crt \
  --sslkey=$HOME/Desktop/OpenConnect-cert/client.key \
  --cafile=$HOME/Desktop/OpenConnect-cert/ca.crt \
  10.20.8.90:8443
# 连上后不要关这个终端；断开按 Ctrl+C
```
- 成功标志：出现 `Connected as 192.168.46.x`、`Established DTLS connection`。
- 验证：另开终端 `ip -4 addr show vpn0` 有 `192.168.46.x`；`ping -c3 8.8.8.8` 通。
- 不想用 CA 文件也可固定服务端指纹：`--servercert pin-sha256:FU29I4gi0J9HKRCVk7hUR1PhI19Ls6lyBNLlcXdND2g=`。

### 2. UOS / Linux 图形界面（NetworkManager）
需已装 `network-manager-openconnect`。**控制中心 → 网络 → VPN → 添加 → OpenConnect (Cisco AnyConnect)**，按下表填：

| 界面字段 | 填什么 |
|---|---|
| 网关 | `10.20.8.90:8443`（**必须带端口**） |
| 用户名 | `test`（可留空） |
| CA 证书 | `~/Desktop/OpenConnect-cert/ca.crt` |
| **用户证书** | `~/Desktop/OpenConnect-cert/client.crt` |
| **私钥** | `~/Desktop/OpenConnect-cert/client.key` |
| 密码 / 私钥密码 | 留空（证书认证不需要） |

- 保存后直接点连接；连上后 `ip addr show vpn0` 应为 `192.168.46.x`。
- “用户证书/私钥”**就是填证书文件**，不是填用户名。

### 3. Windows
装 **Cisco AnyConnect** 或 **OpenConnect-GUI**，服务器填 `10.20.8.90:8443`，导入 `client.p12`
（`client.p12` 已含客户端证书 + 私钥 + CA，导入后无需再选文件）。

### 4. macOS / iOS / Android
- macOS：**OpenConnect-GUI** 或 Cisco AnyConnect，导入 `client.p12`。
- iOS / Android：**Cisco Secure Client** / OpenConnect App，导入 `client.p12`。

> 服务端证书由自建 CA 签发，客户端信任 `ca.crt` 即可（或命令行用 `--servercert pin-sha256:...`）。

### 2.5 分发 / 导入客户端配置

> ⚠️ **控制中心的“导入 VPN”按钮只支持 OpenVPN 的 `*.conf`**（内部走 `nmcli ... type openvpn`）。**OpenConnect 没有 GUI 导入入口**，也没有通用配置文件格式。所以 OpenConnect 只能：keyfile 落盘，或在图形界面里手动填（见 §2）。

`clients/OpenConnect.nmconnection` 是一份可直接分发的 NetworkManager 连接文件（网关 `10.20.8.90:8443`、CA/用户证书/私钥指向 `~/Desktop/OpenConnect-cert/`、登录名 `test`）。

**方式 A：keyfile 落盘（推荐，本机 V20 已验证）**
```bash
sudo cp clients/OpenConnect.nmconnection /etc/NetworkManager/system-connections/
sudo chmod 600 /etc/NetworkManager/system-connections/OpenConnect.nmconnection
sudo nmcli connection reload
# 之后在控制中心 → 网络 → VPN 里就能看到并连接 "OpenConnect"
```
> 换服务器 IP/端口改 `gateway=` 与证书路径；证书认证下 `[vpn-secrets]` 的 `cookie/gateway/gwcert` 由密钥代理在首次连接后自动写入，不用手填。

**方式 B：图形界面手动创建**：按 §2 表格填（网关带端口、CA=`ca.crt`、用户证书=`client.crt`、私钥=`client.key`）。
> ⚠️ V20 的 deepin `network-manager-openconnect 1.2.4.2` 的 `nmcli connection import` 有 bug，始终报 `ipv4.method: 缺少属性`；请用方式 A 或手动填。

> **需要的文件**：`ca.crt`、`client.crt`、`client.key`（或 `client.p12`）+ 连接配置 `clients/OpenConnect.nmconnection`


---

## 3. 端口 / 并存

| | OpenConnect | OpenVPN | L2TP/IPsec | strongSwan | VPNC | L2TP_PLAIN | PPTP |
|---|---|---|---|---|---|---|---|
| 端口 | 8443/tcp+udp | 1194/udp | 500,4500/udp | 500,4500/udp | 500,4500/udp | 1701/udp | 1723/tcp+GRE |
| 网段 | 192.168.46.0/24 | 10.99.99.0/24 | 192.168.42.0/24 | 10.10.10.0/24 | 192.168.47.0/24 | 192.168.44.0/24 | 192.168.45.0/24 |

- OpenConnect 用 8443，与其它 VPN **不冲突**，可同时运行。
- 注意：**strongSwan / L2TP/IPsec / VPNC 三者都占 500/4500，只能三选一**（与 OpenConnect 无关）。
- 防火墙/安全组需放行 **TCP 与 UDP 的 8443**（DTLS 用 UDP）。

---

## 4. 服务器 IP 变化

服务端证书 CN/SAN 绑定了 IP（CA 不绑）。换 IP 后：
1. 重跑脚本（`SERVER_IP=<新IP> ./setup-openconnect.sh`）：会用现有 CA 重签**服务端证书**，CA 与客户端证书不变。
2. 客户端把网关改成新 IP；用 pin 校验的要更新 pin（pin 随服务端证书变）。

> 只有服务端证书绑定 IP；CA 与客户端证书（CN=登录名）不绑 IP，无需更换。

---

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 客户端报证书不受信 | 服务端证书由自建 CA 签发；用 `--cafile ca.crt` / `--servercert pin-sha256:...`，或图形界面导入 `ca.crt` |
| 图形界面连不上（连接超时/拒绝） | **网关必须带端口**：`10.20.8.90:8443`。只填 IP 会走默认 443（服务端没监听 443） |
| 图形界面“用户证书/私钥”必填 | 证书认证下就是要填：用户证书=`client.crt`、私钥=`client.key`（**不是**用户名） |
| 图形界面报“没有有效的密钥”，日志有 `agent failed secrets request ...: failed to createPendingKey` | V20 的 `dde-daemon` VPN 密钥代理卡死（密钥环本身正常，WiFi 密码一直可用）。**注销并重新登录**一次即恢复；仍不行就用命令行 |
| 认证失败 / 登录名不对 | 客户端证书 CN 即登录名；确认服务端 `cert-user-oid = 2.5.4.3`、且用的是本机签发的 `client.crt` |
| 无头 `nmcli` 激活报“没有有效的密钥”/“Not authorized to control networking” | 无图形会话缺 secret agent + polkit 限制；用**图形界面**连接，或直接用 `openconnect` CLI |
| 连上但打不开网页 | 检查服务端 NAT/`ip_forward`；确认客户端默认路由走 `vpn0`/`tun0` |
| 只有 TCP 没有 DTLS | UDP 8443 未放行；DTLS 失败会自动回退到 TLS，速度略低 |
| 容器起不来 | 缺 `/dev/net/tun`（`sudo modprobe tun`）或 8443 被占 |
| 想用更通用的方案 | OpenConnect 兼容性好，但需客户端支持 AnyConnect/OpenConnect |

排障：
```bash
sg docker -c "docker logs --tail 100 openconnect-server"
sg docker -c "docker exec -it openconnect-server occtl show status"
sg docker -c "docker exec openconnect-server iptables -t nat -S POSTROUTING | grep 192.168.46"
```

---

## 6. 卸载

```bash
sg docker -c "docker rm -f openconnect-server"
sg docker -c "docker rmi openconnect-vpn"
# 清理 NAT 残留（宿主机上，若容器内规则未随容器消失）
sudo iptables -t nat -D POSTROUTING -s 192.168.46.0/24 ! -d 192.168.46.0/24 -j MASQUERADE
```

> 不影响其它 VPN。
