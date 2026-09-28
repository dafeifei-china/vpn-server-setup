# L2TP/IPsec 使用指南（Docker 版）

> 部署脚本 `setup-l2tp.sh`，凭据 `vpn.env`。基于 `hwdsl2/ipsec-vpn-server` 镜像（Libreswan + xl2tpd）。
> 本机同时运行着 OpenVPN（`running-vpn-openvpn-tls`），两者**互不影响**（见文末）。

## 连接信息

| 项 | 值 |
|---|---|
| 服务器地址 | `192.168.0.106` |
| 类型 | L2TP/IPsec（预共享密钥） |
| IPsec PSK | `bAjddRGqidan24XN1XCRVmUf` |
| 用户名 | `test` |
| 密码 | `test` |
| VPN 网段 | `192.168.42.0/24` |
| 端口 | `500/udp`、`4500/udp` |

> 以上值也可随时查看：`sudo docker logs ipsec-vpn-server | grep -A6 "Connect to your new VPN"`。
> 修改凭据：编辑 `vpn.env` 后重新运行 `./setup-l2tp.sh`（会重建容器）。

---

## 1. 服务端管理

```bash
sudo docker stop  ipsec-vpn-server     # 禁用
sudo docker start ipsec-vpn-server     # 启用
sudo docker restart ipsec-vpn-server   # 重启
sudo docker logs -f ipsec-vpn-server   # 日志
sudo docker ps --filter name=ipsec-vpn-server          # 状态
sudo docker exec -it ipsec-vpn-server ipsec status     # IPsec 状态
sudo docker exec -it ipsec-vpn-server ipsec trafficstatus   # 当前连接
```

开机自启：容器用 `--restart=unless-stopped`，手动 `stop` 后重启机器不会自启。

更新镜像：

```bash
sudo docker pull docker.1panel.live/hwdsl2/ipsec-vpn-server
sudo docker tag  docker.1panel.live/hwdsl2/ipsec-vpn-server hwdsl2/ipsec-vpn-server
./setup-l2tp.sh
```

---

## 2. 客户端连接

### Windows
设置 → 网络和 Internet → VPN → 添加 VPN：
- VPN 提供商：Windows（内置）
- 名称：任意
- 服务器名称或地址：`192.168.0.106`
- VPN 类型：**L2TP/IPsec（使用预共享密钥）**
- 预共享密钥：`bAjddRGqidan24XN1XCRVmUf`
- 用户名/密码：`test` / `test`

> **客户端或服务器在 NAT 后面时**（家庭路由器常见），需一次性注册表修改，否则报错 809：
> `reg add HKLM\SYSTEM\CurrentControlSet\Services\PolicyAgent /v AssumeUDPEncapsulationContextOnSendRule /t REG_DWORD /d 2 /f`，然后重启。

### macOS / iOS
设置 → 通用 → VPN 与设备管理 → VPN → 添加配置：
- 类型：**L2TP over IPSec**
- 服务器：`192.168.0.106`
- 账户：`test`
- 用户鉴定：密码 → `test`
- 共享密钥：`bAjddRGqidan24XN1XCRVmUf`

### Android
设置 → 网络和互联网 → VPN → 添加 VPN：
- 类型：**L2TP/IPSec PSK**
- 服务器地址：`192.168.0.106`
- IPSec 预共享密钥：`bAjddRGqidan24XN1XCRVmUf`
- 用户名/密码：`test` / `test`

### UOS / Linux（NetworkManager）

需要 L2TP 插件：

```bash
sudo apt install -y network-manager-l2tp network-manager-l2tp-gnome
```

图形界面：控制中心 → 网络 → VPN → 添加 → 选 **Layer 2 Tunneling Protocol (L2TP)**：
- 网关：`192.168.0.106`
- 用户名/密码：`test` / `test`
- 高级 / IPsec：勾选"启用 IPsec 隧道"，预共享密钥填 `bAjddRGqidan24XN1XCRVmUf`，阶段1/阶段2 保持默认。

命令行：

```bash
sudo nmcli connection add type vpn con-name l2tp-106 vpn-type l2tp \
  vpn.data "gateway=192.168.0.106,ipsec-enabled=yes,ipsec-psk=bAjddRGqidan24XN1XCRVmUf,user=test" \
  vpn.secrets "password=test"
sudo nmcli connection up l2tp-106
```

验证：`ip -4 addr show ppp0`（拿到 `192.168.42.x`），`ping -c 3 192.168.42.1`。

---

## 3. 备用方式：IKEv2（更省事，推荐）

镜像已顺带启用 IKEv2，无需 PSK/账号密码。客户端配置已生成在容器 `/etc/ipsec.d/`：

```bash
# 拷出配置
sudo docker cp ipsec-vpn-server:/etc/ipsec.d/vpnclient.p12       ./
sudo docker cp ipsec-vpn-server:/etc/ipsec.d/vpnclient.mobileconfig ./
sudo docker cp ipsec-vpn-server:/etc/ipsec.d/vpnclient.sswan     ./
```

- `vpnclient.p12`：Windows / Linux（strongSwan 客户端）导入
- `vpnclient.mobileconfig`：iOS / macOS 导入
- `vpnclient.sswan`：Android strongSwan 客户端导入

默认导入无需密码。新增客户端：

```bash
sudo docker exec -it ipsec-vpn-server ikev2.sh --addclient <名字>
sudo docker exec -it ipsec-vpn-server ikev2.sh --exportclient <名字>
```

> 重要：同一 NAT 后有多台设备时，L2TP/IPsec 只能连一台，请改用 **IKEv2 或 IPsec/XAuth**。

---

## 4. 服务器 IP 变化

脚本每次运行都会把 `vpn.env` 里的 `VPN_PUBLIC_IP` **同步为当前（或传入）的 IP**，所以 IP 变了直接重跑即可：

```bash
./setup-l2tp.sh                      # 自动探测新 IP 并同步
# 或显式指定（NAT 后公网 IP 与本地 IP 不同时用这个）
SERVER_IP=<新IP> ./setup-l2tp.sh
```

1. 重跑 `./setup-l2tp.sh`（会自动更新 `VPN_PUBLIC_IP` 并重建容器）。
2. 客户端把服务器地址改成新 IP（PSK/账号/证书都不变）。
3. 防火墙/安全组放行 `500/udp`、`4500/udp`。

> 常用域名（DDNS）时把客户端地址写成域名，IP 变了只改 DNS。
> 若你的公网 IP 与本地探测到的 IP 不同（如在 NAT 后），请用 `SERVER_IP=<公网IP>` 显式指定，避免被自动探测覆盖。

---

## 5. 与 OpenVPN 的隔离说明

- L2TP 容器用**桥接网络 + 端口映射**（`-p 500/4500`），iptables 在容器自己的网络命名空间里，**不修改宿主机规则**。
- 与 OpenVPN（`1194`）不冲突；但 L2TP/IPsec 与 **strongSwan、VPNC 都占 `500/4500`，三者只能三选一**（`setup-strongswan.sh`/`setup-vpnc.sh` 会自动停 `ipsec-vpn-server`）。
- 网段不冲突：L2TP `192.168.42.0/24` vs OpenVPN `10.99.99.0/24`。
- 检查两套服务都在：`sudo docker ps`；OpenVPN 的 NAT 规则：`sudo iptables-legacy -t nat -S POSTROUTING | grep 10.99.99`。

---

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| Windows 报 809 / 789 | NAT 后需做上面的注册表修改；或防火墙未放行 500/4500 |
| 连接卡在"正在验证" | PSK/账号密码错误；查看 `sudo docker logs ipsec-vpn-server` |
| UOS V20/V25 报"VPN认证失败" | 客户端与服务端 IKE 提议不匹配（服务端 `NO_PROPOSAL_CHOSEN`）。`setup-l2tp.sh` 已给服务端补 MODP3072/4096 等，客户端用默认配置即可；详见 `VPN连接失败排查方法.md` §2.1 |
| 连上但打不开网页 | DNS/MTU 问题；换 `VPN_DNS_SRV1`，或看 hwdsl2 文档的 Android/Linux MTU 修复 |
| 同一网络第二台设备连不上 | L2TP/IPsec 限制，改用 IKEv2/XAuth |
| 客户端能连但访问不了内网 | 服务端默认只做 VPN 出网；访问宿主局域网需额外路由/NAT |

排障：

```bash
sudo docker logs --tail 100 ipsec-vpn-server
sudo docker exec -it ipsec-vpn-server ipsec status
sudo docker exec -it ipsec-vpn-server ipsec trafficstatus
```

---

## 7. 卸载

```bash
sudo docker rm -f ipsec-vpn-server
sudo docker rmi hwdsl2/ipsec-vpn-server docker.1panel.live/hwdsl2/ipsec-vpn-server
sudo docker volume rm ikev2-vpn-data
```

> 删除 `vpn.env` 会丢失凭据；删除卷会丢失 IKEv2 证书。OpenVPN 不受影响。
