# Agent.md — 近期工作与结论汇总

> 本文件汇总最近一轮排查/修复的结论，作为速查。稳定性的仓库级说明见 `AGENTS.md`。

## 环境速记
- 服务端 IP：`10.20.8.90`；客户端：V20 `10.20.9.40`（用户 `xiong`）、V25 `10.20.9.189`（用户 `uos`）
- Docker 命令用 `sg docker -c '...'`（当前 shell 组快照早于加 docker 组，免重登/免 sudo 密码）
- 七套 VPN：`openvpn/`、`L2TP_IPSEC/`、`L2TP_PLAIN/`、`Strongswan/`、`PPTP/`、`OpenConnect/`、`VPNC/`
- 服务端 WiFi 下是 DHCP，**IP 会漂**（实测 `10.20.50.203 → 10.20.51.22 → 10.20.8.90`）；排查前先确认当前 IP

## 1. L2TP/IPsec
- 账号/PSK 在 `L2TP_IPSEC/vpn.env`（现为 `test/test`）；改账号编辑该文件后重跑 `setup-l2tp.sh`。
- **IKE 提议（V20 曾连不上）**：hwdsl2 服务端默认 `ike=` 只到 MODP2048；UOS V20（strongSwan 5.7.2）默认首选 MODP3072 → `NO_PROPOSAL_CHOSEN`（客户端报“VPN认证失败”）。UOS V25（strongSwan 6.0.1）默认首选 MODP2048，本来就能连。
  - `setup-l2tp.sh` 现会在容器启动后给 `/etc/ipsec.conf` 追加 `MODP3072/4096 + dh19/dh20/dh21/dh31` 并重启 ipsec，V20/V25 客户端用默认配置即可。
  - **不要加 `modp1024`**：Libreswan 5.4 会判定整条 `ike=` 非法，`l2tp-psk` 不加载（只剩 `ikev2-cp`）。用 `ipsec status | grep 'Total IPsec connections'` 应为 `loaded 3`。
- **MPPE**：服务端 pppd 有 `noccp`（不协商 CCP，故无 MPPE）。客户端一律**不要勾“使用 MPPE 点到点加密”**。V25 的 NM-l2tp 1.20.20 勾上会去掉 `noccp` 改 `require-mppe` → `pppd: MPPE required...` 断开；V20 的 NM-l2tp 1.2.10 永远写 `noccp`，勾不勾都能连。
- **UDP 1701 冲突**：`L2TP_PLAIN`（宿主机 `xl2tpd`）与 `L2TP_IPSEC` 隧道内 L2TP 都用 1701。客户端首次连接若 IPsec 未就绪，明文 L2TP 可能被宿主机裸 L2TP 抢答，拿到 `192.168.44.x` 而非 `192.168.42.x`；重连即可，彻底避免则停 `xl2tpd`。
- 客户端属性键 V20/V25 都是 `ipsec-ike`/`ipsec-esp`（不是 `phase1-algs`）。

## 2. strongSwan（IKEv2 证书）
图形界面（NetworkManager）能连的**三要素，缺一不可**：
1. 服务端 `rightsendcert=always`（`setup-strongswan.sh` 已内置）。否则服务端不在 IKE_SA_INIT 发 `CERTREQ`，NM 客户端默认 `leftsendcert=ifasked` 就不发证书 → `AUTH_FAILED` / `no trusted RSA public key found for 'CN=client1'`。
2. 服务端证书 CN/SAN = **当前服务器 IP**。否则客户端 `constraint check failed: certificate does not confirm identity '<IP>'`。
3. 客户端图形界面勾选**“请求内网 IP”**（`virtual=yes`）。否则服务端 `expected a virtual IP request, sending FAILED_CP_REQUIRED` + `TS_UNACCEPT`，拿不到 `10.10.10.x`。

- 命令行方式（`Strongswan/客户端连接方法.md` §1）需客户端 `/etc/ipsec.conf` 显式写 `leftsendcert=always`。
- 实测 V20（NM-strongswan 1.4.4）与 V25（1.6.0）都能用图形界面连上，拿到 `10.10.10.x` 并可出网。

## 3. IP 变更与证书
| VPN | 证书是否绑 IP | IP 变了要做什么 |
|---|---|---|
| **strongSwan** | **是**（服务端证书 CN/SAN） | **只重签服务端证书**；CA 与客户端证书不动 |
| **OpenConnect** | **是**（服务端证书 CN/SAN） | 重跑 `SERVER_IP=<新IP> ./setup-openconnect.sh` 重签服务端证书；改客户端网关与 `gwcert` pin |
| OpenVPN | 否 | 只改客户端 `remote` |
| L2TP/IPsec | 否（PSK） | 只改客户端填的服务器地址 |
| PPTP | 否（账号认证） | 只改客户端填的服务器地址 |
| VPNC | 否（PSK+XAUTH） | 只改客户端填的服务器地址 |

- `setup-strongswan.sh` 现会自动检测 `pki/certs/server.crt` 的 IP，不符时只重签服务端证书（保留 CA/客户端证书）。
- 手动重签、连 CA 一起换的完整步骤见 `Strongswan/STRONGSWAN使用指南.md` §3.1。
- 重签后必须 `docker restart strongswan-server` 才生效。
- 用域名签证书（`--san 域名`）则换 IP 只改 DNS。

## 4. PPTP（Docker）
- 目录 `PPTP/`，脚本 `setup-pptp.sh`，容器 `pptp-server`（`--net host --privileged`），镜像 `pptp-vpn`。
- 端口 `1723/tcp` + **GRE（协议 47）**；网段 `192.168.45.0/24`；账号 `test/test`；默认要求 MPPE（`MPPE=no ./setup-pptp.sh` 可关）。
- 与其它 VPN 端口不冲突，可并存（TCP 1723 + GRE 唯一）。
- **坑**：`etc/pptpd.conf` 的 `remoteip` 范围必须用末位八位组写法 `192.168.45.10-250`；写成 `192.168.45.10-192.168.45.250` 会被 pptpd 当主机名做 DNS 解析 → `exit 1` 崩溃重启。
- 实测 V20/V25（network-manager-pptp）连接成功，`ppp0=192.168.45.x`，可出网。
- 详见 `PPTP/PPTP使用指南.md`。

## 5. OpenConnect (ocserv)
- 目录 `OpenConnect/`，脚本 `setup-openconnect.sh`，容器 `openconnect-server`（`--privileged` + 映射 `/dev/net/tun`），镜像 `openconnect-vpn`。
- 端口 `8443/tcp`（TLS）+ `8443/udp`（DTLS）；网段 `192.168.46.0/24`；账号 `test/test`；自签服务端证书（CN/SAN=IP）。
- 客户端自签证书：命令行用 `--servercert pin-sha256:<pin>`（或 `--cafile server.crt`），图形界面首次连接接受/导入证书。
- 与其它 VPN 端口不冲突，可并存；防火墙需放行 TCP+UDP 8443。
- 实测 V20（openconnect 8.02 CLI）：`tun0=192.168.46.x`，DTLS 建立，能出网。
- 详见 `OpenConnect/OpenConnect使用指南.md`。

## 6. VPNC（Cisco 兼容 IPSec）
- 目录 `VPNC/`，脚本 `setup-vpnc.sh`，容器 `vpnc-server`（`--net host --privileged`），镜像 `vpnc-vpn`。
- 端口 `500/4500/udp`；网段 `192.168.47.0/24`（客户端池 `.10-.250`）；组密钥(PSK) `test`，账号 `test/test`。
- 协议：IKEv1 主动模式 + PSK + XAUTH + MODECFG（供 `vpnc` / `network-manager-vpnc` 连接）。
- **关键坑**：strongSwan 5.8+ 默认禁用 Aggressive Mode+PSK（客户端报 `ISAKMP_N_INVALID_EXCHANGE_TYPE`，服务端日志 `Aggressive Mode PSK disabled for security reasons`）；镜像已内置 `i_dont_care_about_security_and_use_aggressive_mode_psk = yes`。
- 客户端 `vpnc` 在 `/usr/sbin`；组密钥/密码只能写配置文件（`/etc/vpnc/vpnc.conf`）。无 ping 网关，验证以“能否出网”为准。
- 实测 10.20.9.40：CLI 与图形界面（连接 `VPN VPNC 1`）均可连，`tun0=192.168.47.x`，可出网。
- 详见 `VPNC/VPNC使用指南.md`。

## 7. 端口/互斥关系
- `Strongswan`、`L2TP_IPSEC`、`VPNC` 都占 `500/4500/udp`，**只能三选一**。
  - 切到 strongSwan：`sg docker -c "docker stop ipsec-vpn-server vpnc-server" && sg docker -c "docker start strongswan-server"`
  - 切到 VPNC：`sg docker -c "docker stop strongswan-server" && sg docker -c "docker start vpnc-server"`
  - 切回 L2TP/IPsec：`sg docker -c "docker stop strongswan-server vpnc-server" && sg docker -c "docker start ipsec-vpn-server"`
- OpenVPN（1194/udp）、L2TP_PLAIN（1701/udp）、PPTP（1723/tcp + GRE）、OpenConnect（8443/tcp+udp）可与上述任一者并存。

## 8. 当前状态（截至本次汇总）
- `vpnc-server` 运行中（占 500/4500），`cisco-vpnc` 已加载，组密钥/账号 `test`/`test`
- `strongswan-server` 已停（500/4500 让给 VPNC）
- `pptp-server` 运行中（`1723/tcp`），账号 `test/test`
- `openconnect-server` 运行中（`8443/tcp+udp`），账号 `test/test`
- `running-vpn-openvpn-tls` 运行中（`1194/udp`）；宿主机 `xl2tpd` active（`1701/udp`）
- `ipsec-vpn-server` 已停
- 10.20.9.40 上已验证：strongSwan GUI、PPTP、OpenConnect CLI、VPNC（CLI + GUI）均可连

## 9. 相关文档
- `AGENTS.md` — 仓库级稳定说明（布局、跨切面坑、环境）
- `VPN连接失败排查方法.md` — 分层排查、常见现象、端口冲突、`sg docker`、§6 IP 变更与证书、**§7 客户端连通性测试方法**
- `L2TP_IPSEC/UOS_V20_V25客户端差异总结.md` — V20/V25 版本、默认 IKE 提议、MPPE/CCP 差异
- `Strongswan/客户端连接方法.md` — 各平台连接方式 + §10 FAQ
- `Strongswan/STRONGSWAN使用指南.md` — §3.1 重签证书三种方式
- `PPTP/PPTP使用指南.md` — PPTP 部署、客户端连接、`remoteip` 坑
- `OpenConnect/OpenConnect使用指南.md` — ocserv 部署、客户端连接、证书信任
- `VPNC/VPNC使用指南.md` — VPNC(Cisco IPSec) 部署、客户端连接、Aggressive Mode+PSK 坑
