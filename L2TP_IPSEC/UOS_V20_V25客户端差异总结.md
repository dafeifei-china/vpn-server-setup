# UOS V20 / V25 客户端连接 L2TP/IPsec 差异总结

同一套服务端（`L2TP_IPSEC/`，容器 `ipsec-vpn-server`，10.20.8.90），UOS V20 与 V25 客户端行为不同。本文记录实测差异，便于排障和兼容。

实测环境：

| 项 | V20 | V25 |
|---|---|---|
| 客户端 IP | `10.20.9.40` | `10.20.9.189` |
| 系统 | UOS Desktop 20 Professional | UOS Desktop 25 Professional |
| 架构 / 内核 | arm64 / `5.10.97-arm64-desktop` | amd64 / `6.6.0-amd64-desktop` |
| strongSwan | `5.7.2.6-deepin1` | `6.0.1-6+deb13u6` |
| network-manager-l2tp | `1.2.10.2-1+dde+sign` | `1.20.20-1` |
| `nm-l2tp-service` 路径 | `/usr/lib/NetworkManager/nm-l2tp-service` | `/usr/libexec/nm-l2tp-service` |
| xl2tpd | `1.3.12-1.1+sign` | `1.3.18-1` |
| pppd | `2.4.7-2+4.1+deb10u1` | `2.4.9-1+1deepin3` |
| pppd 插件目录 | `/usr/lib/pppd/2.4.7/` | `/usr/lib/pppd/2.4.9/` |
| 登录账号（实测） | `xiong` | `uos` |

---

## 1. 最关键的差异：默认 IKE 提议

L2TP/IPsec 用 **IKEv1**。客户端默认的 IKE 提议不同，直接决定服务端 `ike=` 要配哪些算法。

### V20（strongSwan 5.7.2）默认提议
```
IKE:AES_CBC_128/AES_CBC_192/AES_CBC_256
   /HMAC_SHA2_256_128/HMAC_SHA2_384_192/HMAC_SHA2_512_256/AES_XCBC_96/HMAC_SHA1_96
   /PRF_...(AES128_XCBC/SHA2_256/384/512/SHA1)
   /MODP_3072/MODP_4096/MODP_6144/MODP_8192/MODP_2048
```
- **首个变换 = `AES_CBC_128 + HMAC_SHA2_256 + MODP3072`**
- 只提供 MODP 组，**不含 ECP**
- 服务端若只配到 MODP2048 → `NO_PROPOSAL_CHOSEN`（Libreswan 只尝试客户端第一个组合）

### V25（strongSwan 6.0.1）默认提议
```
ike=aes256-sha2_256-modp2048, aes256-sha2_256-modp1536, aes256-sha2_256-modp1024,
    aes256-sha1-modp2048, aes256-sha1-modp1536, aes256-sha1-modp1024,
    aes256-sha1-ecp384, aes128-sha1-modp1024, aes128-sha1-ecp256,
    3des-sha1-modp2048, 3des-sha1-modp1024!
```
- **首个变换 = `AES_CBC_256 + HMAC_SHA2_256 + MODP2048`**（实测协商结果即此）
- 含 ECP384 / ECP256，且末尾 `!` 表示只用这份列表
- **MODP2048 服务端原本就支持，所以 V25 不补算法也能连**

### 结论
- **V20 需要服务端支持 MODP3072**；V25 不需要。
- `L2TP_IPSEC/setup-l2tp.sh` 已给服务端 `ike=` 追加 `MODP3072/4096 + dh19/dh20/dh21/dh31`，**同时覆盖 V20 与 V25**，客户端无需改动。
- 不要加 `modp1024`：Libreswan 5.4 会判定整条 `ike=` 非法，导致 `l2tp-psk` 不加载（只剩 `ikev2-cp`）。

---

## 2. network-manager-l2tp 属性键（V20 与 V25 相同）

两者都用 **`ipsec-ike` / `ipsec-esp`**（不是 `phase1-algs`/`phase2-algs`），只是 V25 多出一些键：

| 属性键 | V20 | V25 |
|---|---|---|
| `ipsec-enabled` / `ipsec-psk` / `ipsec-ike` / `ipsec-esp` | ✓ | ✓ |
| `ipsec-forceencaps` / `ipsec-gateway-id` / `ipsec-group-name` | ✓ | |
| `ipsec-ikelifetime` / `ipsec-ikev2` | | ✓ |

客户端侧强制算法（不改服务端时）两者写法一致：
```bash
sudo nmcli connection modify "<连接名>" vpn.data "gateway=<IP>,ipsec-enabled=yes,ipsec-psk=<PSK>,password-flags=0,user=test,ipsec-ike=aes128-sha1-modp2048,ipsec-esp=aes128-sha1"
```
> `vpn.data` 是整体替换，原有键必须一起写。

---

## 3. 其它差异

| 项 | V20 | V25 |
|---|---|---|
| `require-mppe`（GUI 里的“使用 MPPE 点到点加密”） | 勾/不勾都能连（1.2.10 永远写 `noccp`） | 必须**不勾**（勾了会失败），详见下节 |
| 连接持久性 | 实测 `vpn.persistent=否`（易失，NM 重启/重登后可能消失） | 已保存到 `/etc/NetworkManager/system-connections/` |
| ppp0 MTU | —（未稳定取到） | 实测 `1280`（pppd 日志显示先覆盖为 1400） |
| 首次连接落裸 L2TP | 未观察到 | 观察到一次（拿到 `192.168.44.x`，见下） |
| 快速 down/up | — | 易失败，需间隔几秒 |

### 首次连接可能落到裸 L2TP（与版本无关，是端口冲突）
本机同时跑着 L2TP_PLAIN（宿主机 `xl2tpd`，也监听 UDP 1701）。L2TP/IPsec 的 IPsec 走 500/4500，但隧道内 L2TP 仍是 1701。客户端**首次**连接时若 IPsec 还没就绪，明文 L2TP 可能被宿主机 `xl2tpd` 抢答：

- 现象：客户端拿到 `192.168.44.x`（裸 L2TP 网段）而不是 `192.168.42.x`
- 处理：慢速重连一次即可；要彻底避免，用 L2TP/IPsec 时 `sudo systemctl stop xl2tpd`
- 排查：服务端 `docker exec ipsec-vpn-server ipsec trafficstatus` 若对应客户端 SA 为 0 字节，说明 L2TP 没走 IPsec

### MPPE / CCP 差异（实测）

服务端 pppd 配置里有 **`noccp`**，即服务端不协商 CCP，而 MPPE 是 CCP 里的选项，所以服务端根本用不上 MPPE。客户端是否勾选“使用 MPPE 点到点加密”，效果完全取决于 `network-manager-l2tp` 版本怎么把它翻译成 pppd 选项：

- **V25（NM-l2tp 1.20.20）**：不勾 → 写 `noccp`；勾上 → **不写 `noccp`**，改写 `require-mppe`。
  - 实测勾上后 pppd 报 `MPPE required, but MS-CHAP[v2] nor EAP-TLS auth are performed.` 并断开。
  - → **V25 必须不勾 MPPE**。
- **V20（NM-l2tp 1.2.10）**：**无条件写 `noccp`**，勾选只是额外再加一行 `require-mppe`。
  - 实测勾/不勾都能连（都拿到 `192.168.42.x`），pppd 日志无 MPPE 行，说明 `noccp` 生效、MPPE 实际没协商。
  - → **V20 勾不勾都能连**；“V20 必须勾上”不是协议要求（可能是配置档/GUI 默认造成的印象）。

实测 ppp-options 关键行对比：

| 场景 | ppp-options 关键行 | 结果 |
|---|---|---|
| V20 不勾 | `noccp` | 成功 |
| V20 勾上 | `noccp` + `require-mppe` | 成功（`noccp` 生效，无 MPPE） |
| V25 不勾 | `noccp` | 成功 |
| V25 勾上 | `require-mppe`（无 `noccp`） | **失败** |

补充：两端的 `ppp_mppe` 内核模块都未加载（V25 有 `ppp_mppe.ko`，V20 在 `/lib/modules/<内核>` 下未见）。真要启用 MPPE，服务端也得去掉 `noccp`，否则两边谈不起来。

**结论：客户端一律不要勾“使用 MPPE 点到点加密”。**

---

## 4. 复现 / 排查命令

客户端（V20 用 `sg`/sudo，V25 用 sudo）：
```bash
# 连接
sudo nmcli con up "<连接名>"
ip -4 addr show ppp0            # 期望 192.168.42.x，网关 192.168.42.1
ping -c3 192.168.42.1
# 抓默认 IKE 提议：把 charon 日志级别调到 2（临时）
# /etc/strongswan.d/charon-logging.conf:
#   charon { syslog { daemon { default = 2 } } }
sudo journalctl --since "1 min ago" | grep -E "configured proposals|selected proposal|ike="
# 用完记得恢复该文件默认（全注释）
```

服务端：
```bash
sg docker -c "docker exec ipsec-vpn-server ipsec status" | grep '"l2tp-psk":   IKE algorithms'
sg docker -c "docker exec ipsec-vpn-server ipsec status" | grep "Total IPsec connections"   # 期望 loaded 3
sg docker -c "docker exec ipsec-vpn-server ipsec trafficstatus"                              # 看 SA 字节数
```

---

## 5. 一句话结论
- 服务端补 `MODP3072/4096 + dh19..dh31` 后，**V20 和 V25 都能用默认配置连 L2TP/IPsec**，分别拿到 `192.168.42.x`。
- 差异根源是 strongSwan 5.7.2（V20）默认首选 `MODP3072`，而 strongSwan 6.0.1（V25）默认首选 `MODP2048`。
- 唯一要小心的坑是宿主机裸 L2TP 与 L2TP/IPsec 共用 1701 导致的首次抢答。
