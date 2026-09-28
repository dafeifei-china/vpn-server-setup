# VPN 连接失败排查方法

适用于本工作区七套 VPN（OpenVPN / L2TP-IPSEC / L2TP_PLAIN / Strongswan / PPTP / OpenConnect / VPNC）。
思路：**从下往上逐层排除**，先定位断在哪一层，再看那一层的日志。不要一上来就重装。
客户端连通性实战测试方法见 §7。

## 0. 分层排查（先定位层次）

| 层 | 要确认的事 | 检查命令 | 通过标准 |
|---|---|---|---|
| 1 网络可达 | 客户端能否到服务端 IP | 客户端 `ping <服务器IP>`；`ip route get <服务器IP>` | 通；路由走物理网卡（不是 `utun7` 等别的 VPN） |
| 2 服务在跑 | 容器/进程是否存活 | `sg docker -c "docker ps -a"`；`systemctl status xl2tpd` | 目标服务是 `Up` / `active` |
| 3 端口在听 | 对应 UDP 端口有监听 | `ss -ulnp \| grep -E ':(1194\|500\|4500\|1701)\b'` | 能看到对应端口，且占用者正确 |
| 4 认证 | 账号/PSK/证书是否正确 | 见下方各服务 | 服务端日志不再报认证失败 |
| 5 隧道建立 | 客户端拿到隧道 IP | 客户端 `ip -4 addr show tun0` / `ppp0` | 有对应网段地址 |
| 6 能出网 | 转发/NAT/DNS/MTU | 服务端 `sysctl net.ipv4.ip_forward`；NAT 规则；客户端 `ping <网关.1>` | 能 ping 通网关并解析域名 |

## 1. 各服务定位命令

### OpenVPN（容器 `running-vpn-openvpn-tls`，1194/udp，10.99.99.0/24）
```bash
sg docker -c "docker ps -a --filter name=running-vpn-openvpn-tls"
sg docker -c "docker logs --tail 50 running-vpn-openvpn-tls"
sg docker -c "docker logs --tail 20 running-vpn-openvpn-tls" | grep "Initialization Sequence Completed"
ss -ulnp | grep ':1194\b'
ip -4 addr show tun0
```
客户端 `remote` 要指向当前服务端 IP：`grep '^remote' openvpn/local-server/clients/client.conf`。

### L2TP/IPsec（容器 `ipsec-vpn-server`，500/4500/udp，192.168.42.0/24）
```bash
sg docker -c "docker ps -a --filter name=ipsec-vpn-server"
sg docker -c "docker logs --tail 100 ipsec-vpn-server"
sg docker -c "docker exec ipsec-vpn-server ipsec status"
sg docker -c "docker exec ipsec-vpn-server ipsec trafficstatus"
```
账号/PSK 在 `L2TP_IPSEC/vpn.env`；改完要重跑 `./setup-l2tp.sh` 重建容器。
> 起不来最常见原因：500/4500 被 `strongswan-server` 占用（见第 3 节）。

### L2TP_PLAIN（宿主机 `xl2tpd`+`pppd`，1701/udp，192.168.44.0/24）
```bash
systemctl status xl2tpd
sudo journalctl -u xl2tpd -n 50 --no-pager
ss -ulnp | grep ':1701\b'
```
账号在 `/etc/ppp/chap-secrets`（服务名固定 `l2tpserver`），改完 `sudo systemctl restart xl2tpd`。

### Strongswan（容器 `strongswan-server`，500/4500/udp，10.10.10.0/24）
```bash
sg docker -c "docker ps -a --filter name=strongswan-server"
sg docker -c "docker logs --tail 50 strongswan-server"
sg docker -c "docker exec strongswan-server ipsec statusall"
```
看 `ikev2-cert` 连接是否加载、SA 是否 `ESTABLISHED`。证书 CN/SAN 绑定了服务器 IP，换 IP 后需重签（`setup-strongswan.sh` 会自动只重签服务端证书）。

### PPTP（容器 `pptp-server`，1723/tcp + GRE(协议 47)，192.168.45.0/24）
```bash
sg docker -c "docker ps -a --filter name=pptp-server"
sg docker -c "docker logs --tail 50 pptp-server"
sg docker -c "docker exec pptp-server ps -ef | grep pptpd"
ss -tlnp | grep ':1723\b'
sg docker -c "docker exec pptp-server last -a | head"      # 登录记录
```
账号 `test/test`（`PPTP/etc/ppp/chap-secrets`）；服务端 `require-mppe-128`，客户端要勾 MPPE；必须放行 **TCP 1723 + GRE(协议 47)**，GRE 被拦则 1723 能连、隧道起不来。

### OpenConnect（容器 `openconnect-server`，8443/tcp+udp，192.168.46.0/24）
```bash
sg docker -c "docker ps -a --filter name=openconnect-server"
sg docker -c "docker logs --tail 50 openconnect-server"
sg docker -c "docker exec openconnect-server occtl show users"
ss -tlnp | grep ':8443\b'
```
证书认证：CA=`etc/ocserv/ca.crt`、用户证书=`client.crt`、私钥=`client.key`（登录名=客户端证书 CN）；客户端网关**必须带端口** `<IP>:8443`。

### VPNC（容器 `vpnc-server`，500/4500/udp，192.168.47.0/24）
```bash
sg docker -c "docker ps -a --filter name=vpnc-server"
sg docker -c "docker logs --tail 50 vpnc-server"
sg docker -c "docker exec vpnc-server ipsec statusall"        # 看 cisco-vpnc
sg docker -c "docker exec vpnc-server cat /var/log/charon.log"
ss -ulnp | grep -E ':(500|4500)\b'
```
Cisco 兼容（给 vpnc / network-manager-vpnc 连）：IKEv1 主动模式 + PSK(组密钥 `test`) + XAUTH(`test`/`test`)。与 strongSwan / L2TP-IPSEC 抢 500/4500，`setup-vpnc.sh` 会自动停它们。

## 2. 常见现象 → 原因

| 现象 | 原因 / 处理 |
|---|---|
| 客户端完全连不上，服务端日志无记录 | 层1/2/3 问题：IP 写错、服务没起、端口没听、防火墙未放行 |
| 服务端 `docker logs` 报 `bind ... address already in use` | 端口冲突，见第 3 节 |
| Windows L2TP 报 809/789 | NAT 后需改注册表 `AssumeUDPEncapsulationContextOnSendRule=2`；或防火墙未放行 500/4500 |
| L2TP 卡在"正在验证" | 账号/密码/PSK 错；看 `docker logs ipsec-vpn-server` |
| L2TP 第一阶段就被拒（客户端日志 `NO_PROPOSAL_CHOSEN`） | IKE 提议不匹配，见 2.1 |
| 客户端勾了“使用 MPPE 点到点加密”后连不上（V25 报 `MPPE required...`） | 服务端 pppd 有 `noccp`，不要勾 MPPE；详见 `L2TP_IPSEC/UOS_V20_V25客户端差异总结.md` |
| OpenVPN 报 `没有有效的密钥` | 加密私钥 + 无图形会话缺 secret agent；用 `client-nopass` 或图形界面 |
| OpenVPN 报 `Cipher BF-CBC not supported` | 服务端 OpenVPN < 2.4 |
| strongSwan `AUTH_FAILED`（IKE_AUTH 请求无 `CERT`） | 客户端没发证书：命令行需客户端 `leftsendcert=always`；图形界面需服务端 `rightsendcert=always` |
| strongSwan IKE_SA 建立但无 `10.10.10.x`（`FAILED_CP_REQUIRED`/`TS_UNACCEPT`） | 图形界面未勾“请求内网 IP”（`virtual=yes`） |
| 连上但打不开网页 | 转发/NAT/DNS/MTU；查 `ip_forward`、MASQUERADE、DNS |
| 客户端路由被别的 VPN 抢走 | `ip route get <服务器IP>` 确认走物理网卡（如 `wlp2s0`），不是 `utun7` |
| 重启机器后 VPN 没了 | 容器是 `unless-stopped`，手动 stop 过就不会自启，需 `docker start` |
| 客户端能连别的 VPN，但某套连不上 | 该 VPN 的端口/协议被中间防火墙拦（如 PPTP 的 GRE 47），或客户端填的服务器 IP 过期；抓包见 §7 |
| PPTP 连不上、服务端抓不到 GRE | 中间防火墙拦了 GRE(协议 47)；或客户端地址填错。抓包方法见 §7.4 |
| OpenConnect 连不上 / 证书不匹配 | 服务端 IP 变了：重签服务端证书并改客户端网关与 `gwcert` pin，见 §6、§7.5 |
| 客户端 SSH 下 `nmcli connection up` 报 `没有有效的密钥` | 桌面密钥代理不可达（`vpn.secrets.cookie`）；改用图形界面连接，GUI 也报错就注销重登一次 |
| VPNC 报 `ISAKMP_N_INVALID_EXCHANGE_TYPE` | strongSwan 5.8+ 默认禁用 Aggressive Mode+PSK；镜像已内置 `i_dont_care_about_security_and_use_aggressive_mode_psk = yes`，见 `VPNC/VPNC使用指南.md` |
| VPNC 连不上、`ipsec statusall` 无 `cisco-vpnc` | 500/4500 被 strongSwan/L2TP-IPSEC 占用（互斥）；`setup-vpnc.sh` 会自动停它们 |

### 2.1 实战：UOS V20/V25 客户端 IKE 第一阶段失败（NO_PROPOSAL_CHOSEN）

**现象**：客户端能 ping 通服务端 IP，但拨号报 `VPN认证失败，请检查配置后重试`；`journalctl` 里 charon 报 `received NO_PROPOSAL_CHOSEN`，服务端 `ipsec trafficstatus` 为空（IKE 都没建起来）。这不是账号密码问题。

**原因**：UOS 自带 strongSwan 的**默认 IKE 提议**第一个变换是 `AES_CBC_128 + HMAC_SHA2_256 + MODP3072`，而 hwdsl2 服务端默认只配到 `MODP2048`；Libreswan 只尝试客户端第一个组合，于是返回 `NO_PROPOSAL_CHOSEN`。

**处理（服务端改一次，`setup-l2tp.sh` 已内置）**：给服务端 `ike=` 追加 MODP3072/4096 以及 `dh19/dh20/dh21/dh31`。改完客户端**无需任何改动**，V20/V25 默认配置都能连。

排查/确认命令：
```bash
# 服务端实际加载的 IKE 算法（应能看到 MODP3072/4096、DH19…）
sg docker -c "docker exec ipsec-vpn-server ipsec status" | grep '"l2tp-psk":   IKE algorithms'
# 连接数应为 3（ikev2-cp / l2tp-psk / xauth-psk）
sg docker -c "docker exec ipsec-vpn-server ipsec status" | grep "Total IPsec connections"
```

> 若连接数变成 1、`l2tp-psk` 不见了，多半是 `ike=` 里写了 Libreswan 不认的算法（如 `modp1024`），导致整条 `ike=` 非法。去掉即可。
> 需要看服务端拒绝细节时，临时开 pluto 调试：
> `sg docker -c "docker exec ipsec-vpn-server sed -i '/^config setup/a\  plutodebug=all\n  logfile=/var/log/pluto.log' /etc/ipsec.conf && docker exec ipsec-vpn-server ipsec restart"`，然后看 `/var/log/pluto.log` 里的 `Oakley Transform [...] refused`。

**客户端侧备用改法**（不改服务端时，V20/V25 属性键相同，都是 `ipsec-ike`/`ipsec-esp`）：
```bash
sudo nmcli connection modify "<连接名>" vpn.data "gateway=<IP>,ipsec-enabled=yes,ipsec-psk=<PSK>,password-flags=0,user=test,ipsec-ike=aes128-sha1-modp2048,ipsec-esp=aes128-sha1"
```
> `nmcli ... vpn.data` 是**整体替换**，务必把 gateway/ipsec-enabled/ipsec-psk/user 等原有键一起写上。

**已实测（服务端补 MODP3072/4096 后，客户端默认配置即可）**：
- V20：strongSwan 5.7.2 / network-manager-l2tp 1.2.10，`10.20.9.40` → 拿到 `192.168.42.10`
- V25：strongSwan 6.0.1 / network-manager-l2tp 1.20.20，`10.20.9.189` → 拿到 `192.168.42.11`

> ⚠️ 本机同时跑着 L2TP_PLAIN（宿主机 `xl2tpd`，也监听 UDP 1701）。客户端**首次**连接时若 IPsec 还没就绪，明文 L2TP 可能被宿主机裸 L2TP 抢答，表现为拿到 `192.168.44.x`（裸 L2TP 网段）而不是 `192.168.42.x`。慢速重连一次即可；要彻底避免，用 L2TP/IPsec 时先停掉 L2TP_PLAIN（`sudo systemctl stop xl2tpd`）。另外快速 `down`/`up` 可能因旧会话未清理而失败，重连间隔留几秒。

> V20/V25 的完整版本、默认 IKE 提议、属性键等差异见 `L2TP_IPSEC/UOS_V20_V25客户端差异总结.md`。

## 3. 端口冲突速查

| 服务 | 端口 | 隧道网段 | 占用方式 |
|---|---|---|---|
| OpenVPN | 1194/udp | 10.99.99.0/24 | 容器 `--net host` |
| L2TP/IPsec | 500,4500/udp | 192.168.42.0/24 | 容器端口映射 |
| L2TP_PLAIN | 1701/udp | 192.168.44.0/24 | 宿主机 `xl2tpd` |
| Strongswan | 500,4500/udp | 10.10.10.0/24 | 容器 `--net host` |
| PPTP | 1723/tcp + GRE(47) | 192.168.45.0/24 | 容器 `--net host` |
| OpenConnect | 8443/tcp+udp | 192.168.46.0/24 | 容器端口映射 |
| VPNC | 500,4500/udp | 192.168.47.0/24 | 容器 `--net host` |

**Strongswan、L2TP/IPsec、VPNC 都用 500/4500，三选一。**

判断端口被谁占用：
```bash
ss -ulnp | grep -E ':(500|4500)\b'      # 有输出=被占；无输出=空闲
sudo lsof -iUDP:500 -iUDP:4500          # 看具体进程
```
切换：
```bash
# 让 strongSwan 上
sg docker -c "docker stop ipsec-vpn-server"
sg docker -c "docker start strongswan-server"
# 让 L2TP/IPsec 上
sg docker -c "docker stop strongswan-server"
sg docker -c "docker start ipsec-vpn-server"
```

## 4. `sg docker -c` 是什么、为什么这么用

- `sg`（switch group，来自 shadow-utils）用于**以另一个用户组身份**运行一条命令：
  ```
  sg <组名> -c '<命令>'
  ```
  这里 `sg docker -c "docker ps"` 就是"以 `docker` 组成员身份执行 `docker ps`"。

- 为什么需要：`docker` 守护进程的 socket `/var/run/docker.sock` 属 `docker` 组。本机用户虽然已被加入 `docker` 组，但**当前 shell 的用户组快照是在加组之前生成的**，所以直接敲 `docker` 会报权限不足（`permission denied ... docker.sock`）。`sg` 会临时切换到该组，**不用重新登录/重开终端**即可生效。

- 等价替代：
  - `newgrp docker`：进入一个以 docker 为主组的新 shell（之后可直接用 `docker`）。
  - `sudo docker ...`：用 root 执行，但本机 `sudo` 需要密码，非交互场景无法输入。
  - 重新登录一次，组权限永久生效。

- 本工作区因此统一用 `sg docker -c '...'`（免密码、免重登）。能交互输密码时，`sudo docker ...` 完全等价。

> 判断当前 shell 是否已有 docker 组：`id | grep docker` 或 `groups | grep docker`。若已生效，可直接 `docker ...`。

## 5. 恢复/禁用速查

```bash
# 停用某服务
sg docker -c "docker stop strongswan-server"
sudo systemctl disable --now xl2tpd        # 宿主机服务，必须 disable 才不会开机自启

# 启用
sg docker -c "docker start strongswan-server"
sudo systemctl enable --now xl2tpd

# 彻底删除容器（含失败的 Created 容器）
sg docker -c "docker rm -f ipsec-vpn-server"
```

## 6. IP 变更与证书（各 VPN 差异）

换服务器 IP 后，是否需要重签证书取决于 VPN 类型——**strongSwan 和 OpenConnect 的服务端证书都绑定了 IP，且都只需重签服务端证书**。

| VPN | 证书是否绑 IP | IP 变了要做什么 |
|---|---|---|
| **strongSwan（IKEv2 证书）** | **是**（服务端证书 CN/SAN 写入 IP） | **重签服务端证书**；CA 与客户端证书**不用重生成** |
| **OpenConnect（ocserv 证书认证）** | **是**（服务端证书 CN/SAN 写入 IP） | 重跑 `SERVER_IP=<新IP> ./setup-openconnect.sh` 重签服务端证书；改客户端网关与 `gwcert` pin |
| OpenVPN | 否（证书与 IP 无关） | 只改客户端配置里的 `remote` 地址 |
| L2TP/IPsec | 否（PSK 认证，无证书） | 只改客户端填的服务器地址 |
| PPTP | 否（账号认证，无证书） | 只改客户端填的服务器地址 |
| VPNC | 否（PSK+XAUTH，无证书） | 只改客户端填的服务器地址 |

- strongSwan：绑定 IP 的是**服务端证书的 CN/SAN**，换 IP 必须让服务端证书反映新 IP；但**不需要**换 CA，也**不需要**换客户端证书。`setup-strongswan.sh` 现在会自动检测并只重签服务端证书。详见 `Strongswan/STRONGSWAN使用指南.md` §3。
- 例外：若服务端证书里写的是**域名**（`--san example.com`），换 IP 只改 DNS 即可，连服务端证书都不用重签（指南推荐常用域名）。
- 现象：strongSwan 客户端日志报 `constraint check failed: certificate does not confirm identity '<新IP>'` 就是这个原因。

## 7. 客户端连通性测试方法（实战）

**测试拓扑（本工作区实测）**
- 服务端：本机，当前 `10.20.8.90`（WiFi `wlp2s0`，MAC `74:12:b3:95:cd:cb`）。⚠️ WiFi 下是 **DHCP，IP 会漂**（已实测 `10.20.50.203 → 10.20.51.22 → 10.20.8.90`）。
- 客户端：`10.20.9.40`（UOS），与服务端同属 `10.20.8.0/23`。
- SSH 进客户端（系统账号，非 VPN 账号）：`sshpass -p 'Linhui@1993' ssh xiong@10.20.9.40`。

### 7.1 先确认服务端当前 IP（IP 会变，最易踩坑）
```bash
ip -4 addr show | grep inet              # 本机当前 IP
ip link show wlp2s0 | grep ether         # WiFi MAC
```
不确定客户端连的 IP 是不是本机：在客户端看 `ip neigh show <IP>` 的 MAC，与上面 MAC 一致即同一台。
> 客户端配置里填的是旧 IP 时，包根本到不了本机，服务端抓包会**一个包都没有**。

### 7.2 服务端侧：服务在跑 / 端口在听
```bash
sg docker -c "docker ps"
ss -tlnp | grep -E ':1723|:8443'
ss -ulnp | grep -E ':1194|:500 |:4500|:1701|:8443'
openssl x509 -in OpenConnect/etc/ocserv/server.crt -noout -subject -ext subjectAltName
openssl x509 -in Strongswan/pki/certs/server.crt -noout -subject -ext subjectAltName
```

### 7.3 客户端侧：可达性（关键一步）
```bash
ip -4 addr show | grep inet
ip route get <服务器IP>          # 确认走物理网卡，不是 utun7 等别的 VPN
nc -vz <服务器IP> <端口>          # TCP 端口：PPTP 1723 / OpenConnect 8443
ping -c2 <服务器IP>              # 仅参考：ICMP 常被防火墙拦
```
> **ping 不通 ≠ 连不上**：VPN 只依赖对应 TCP/UDP 端口，ICMP 被拦不影响 VPN；只有 `nc` 端口不通才是真断。

### 7.4 PPTP 专项（TCP 1723 + GRE 47）
客户端点连接的同时，服务端抓包：
```bash
sudo tcpdump -ni <物理网卡> -tttt 'tcp port 1723 or proto gre'
```
- 有 1723 握手 + 双向 `GREv1 ... compressed PPP data` = 正常；
- 只有 1723、没有 GRE = 中间防火墙拦了 GRE(协议 47)，PPTP 必失败；
- 什么都没有 = 客户端地址填错 / 到不了本机（先查 §7.1）。
客户端验证隧道：`ip -4 addr show ppp0`（应为 `192.168.45.x`）、`ping 192.168.45.1`、`ping 8.8.8.8`。
服务端登录记录：`sg docker -c "docker exec pptp-server last -a | head"`。
客户端必须勾 **“使用点到点加密(MPPE)”**（服务端 `require-mppe-128`）。

### 7.5 OpenConnect 专项（TCP/UDP 8443）
客户端网关**必须带端口**：`<服务器IP>:8443`；CA=`ca.crt`、用户证书=`client.crt`、私钥=`client.key`、密码留空。
服务端 IP 变了：重签服务端证书（`SERVER_IP=<新IP> ./setup-openconnect.sh`）并更新客户端网关与 `gwcert` pin（pin = 服务端证书 SPKI 的 sha256）：
```bash
openssl x509 -in OpenConnect/etc/ocserv/server.crt -noout -pubkey \
  | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | openssl base64
```
headless（SSH）下 `nmcli connection up` 会报 `没有有效的密钥`（`vpn.secrets.cookie`）——那是桌面密钥代理不可达，**改用图形界面连接**；GUI 也报错就注销重登一次。

### 7.6 客户端连接管理（nmcli）
```bash
nmcli -t -f NAME,TYPE connection show | grep -i vpn
sudo nmcli -s connection show "<连接名>" | grep -iE 'vpn.data|vpn.secrets'
# vpn.data 是整体替换，改网关时务必带上原有其它键（user/cacert/usercert/userkey 等）
sudo nmcli connection modify "<连接名>" vpn.data "gateway=<IP>,<其它原有键>"
```
连接文件在 `/etc/NetworkManager/system-connections/<连接名>.nmconnection`，直接改后 `sudo nmcli connection reload`。

### 7.7 实战案例：PPTP 客户端连不上
现象：客户端能连 OpenConnect，但 PPTP 连不上；服务端 `tcpdump 'tcp port 1723 or proto gre'` 抓到 **0 包**。
定位：客户端 `ip neigh show 10.20.8.90` 的 MAC 与服务端 WiFi MAC 相同 → 服务器当前 IP 是 `10.20.8.90`；而客户端 PPTP 里 `gateway` 还是旧地址 `10.20.50.203` → 包到不了。
处理：客户端改 `gateway=10.20.8.90`（`require-mppe=yes` 不变），拨号成功；服务端抓到双向 GRE，客户端 `ppp0=192.168.45.x`、可出网。
教训：**服务器 WiFi 下 IP 会漂**，排查前先用 §7.1 确认当前 IP；根治要给 WiFi MAC 做 DHCP 静态绑定或改用有线。

### 7.8 VPNC 专项（Cisco IPSec，UDP 500/4500）
客户端 `vpnc` 在 `/usr/sbin`（非登录 shell PATH 可能没有）。组密钥/密码只能写配置文件：
```bash
sudo tee /etc/vpnc/vpnc.conf >/dev/null <<'EOF'
IPSec gateway <服务器IP>
IPSec ID test
IPSec secret test
Xauth username test
Xauth password test
IKE Authmode psk
EOF
sudo vpnc /etc/vpnc/vpnc.conf      # 断开：sudo vpnc-disconnect
```
- 无 ping 网关（mode-config 不装隧道 IP），验证 `ip -4 addr show tun0`（`192.168.47.x`）+ `ping 8.8.8.8`。
- UDP 500 不能 `nc` 测；服务端抓包 `sudo tcpdump -ni <物理网卡> 'udp port 500'`，应能看到 `isakmp: phase 1 I agg`。
- 报 `ISAKMP_N_INVALID_EXCHANGE_TYPE` → 服务端 `Aggressive Mode PSK disabled for security reasons`，见 §2 与 `VPNC/VPNC使用指南.md`。
