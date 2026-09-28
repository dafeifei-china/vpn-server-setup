# 裸 L2TP 使用指南（无 IPsec）

> 部署脚本 `setup-l2tp-plain.sh`。使用宿主机 `xl2tpd` + `pppd`，监听 **UDP 1701**。
>
> ⚠️ **裸 L2TP 不加密**，账号密码和流量都是明文，仅限可信网络/测试使用。生产环境请用 `L2TP_IPSEC/` 或 OpenVPN。

## 连接信息

| 项 | 值 |
|---|---|
| 服务器地址 | `192.168.0.106` |
| 类型 | L2TP（**不启用 IPsec**） |
| 端口 | `1701/udp` |
| 用户名 / 密码 | `test` / `test` |
| VPN 网段 | `192.168.44.0/24`（服务端 `192.168.44.1`） |

配置文件：

| 文件 | 作用 |
|---|---|
| `/etc/xl2tpd/xl2tpd.conf` | xl2tpd 主配置（网段、认证方式） |
| `/etc/ppp/options.xl2tpd` | pppd 参数（MSCHAPv2、DNS、MTU） |
| `/etc/ppp/chap-secrets` | 账号密码 |

---

## 1. 服务端管理

```bash
sudo systemctl status  xl2tpd
sudo systemctl restart xl2tpd      # 改配置后重启
sudo systemctl stop    xl2tpd      # 禁用
sudo systemctl start   xl2tpd      # 启用
sudo systemctl disable xl2tpd      # 取消开机自启
sudo journalctl -u xl2tpd -f       # 日志
sudo ss -ulnp | grep 1701          # 确认监听
```

改账号：编辑 `/etc/ppp/chap-secrets`，格式 `用户名 服务名 密码 IP`（服务名固定 `l2tpserver`），然后 `sudo systemctl restart xl2tpd`。

改网段：编辑脚本顶部变量后重跑 `./setup-l2tp-plain.sh`。

---

## 2. 客户端连接

### UOS / Linux（NetworkManager）—— 不要启用 IPsec

```bash
sudo nmcli connection add type vpn con-name l2tp-plain vpn-type l2tp \
  vpn.data "gateway=192.168.0.106,ipsec-enabled=no,user=test,password-flags=0" \
  vpn.secrets "password=test"
sudo nmcli connection up l2tp-plain
```

图形界面：控制中心 → 网络 → VPN → 添加 → **Layer 2 Tunneling Protocol (L2TP)**：网关 `192.168.0.106`、用户名/密码 `test`/`test`，**高级里不要勾"启用 IPsec 隧道"**。

验证：`ip -4 addr show ppp0` 应拿到 `192.168.44.x`，`ping 192.168.44.1` 通。

### Android
设置 → 网络和互联网 → VPN → 添加：类型选 **L2TP**（不是 L2TP/IPSec），服务器 `192.168.0.106`，账号密码 `test`/`test`。

### Windows
Windows 默认强制 L2TP 走 IPsec，需先禁用（管理员执行后重启）：

```
reg add HKLM\SYSTEM\CurrentControlSet\Services\PolicyAgent /v ProhibitIpSec /t REG_DWORD /d 1 /f
```

然后建 VPN，类型仍选 L2TP/IPsec，但**不填预共享密钥**即可走裸 L2TP。

### macOS / iOS
原生只支持 L2TP over IPsec，**无法连接裸 L2TP**。

---

## 3. 与其他 VPN 并存

| | 裸 L2TP | L2TP/IPsec | OpenVPN |
|---|---|---|---|
| 端口 | `1701/udp`（宿主机） | `500/4500/udp`（容器） | `1194/udp`（容器） |
| 网段 | `192.168.44.0/24` | `192.168.42.0/24` | `10.99.99.0/24` |
| 进程 | 宿主机 xl2tpd | 容器内 xl2tpd | 容器内 openvpn |

三者**可同时运行**，端口/网段/进程互相隔离。实测：`1194/500/4500/1701` 同时监听，裸 L2TP 与 L2TP/IPsec 都能连。

注意：
- **同一台客户端**的 NetworkManager l2tp 插件一次只允许一个活动的 L2TP 连接（裸 L2TP 和 L2TP/IPsec 二选一），但不同客户端各连各的没问题。
- 裸 L2TP 需在防火墙/安全组额外放行 `1701/udp`。
- ⚠️ **strongSwan / L2TP/IPsec / VPNC 都占 500/4500，三者只能三选一**（裸 L2TP 与它们不冲突，可共存）。
- ⚠️ **与 L2TP/IPsec 共用 UDP 1701**：L2TP/IPsec 的 IPsec 走 500/4500，但隧道内的 L2TP 仍是 1701。客户端首次连接时若 IPsec 尚未就绪，明文 L2TP 可能被本机裸 L2TP 抢答，表现为客户端拿到 `192.168.44.x`（本网段）而非 `192.168.42.x`。要避免歧义，用 L2TP/IPsec 时先 `sudo systemctl stop xl2tpd`。

---

## 4. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 服务端 pppd `unrecognized option 'lock'/'local'/'proxyarp'` | UOS 的 pppd 2.4.9 构建不支持这些选项，已从 `options.xl2tpd` 移除 |
| 客户端连上又被服务端断开 | 服务端 pppd 启动失败或认证失败，`journalctl -u xl2tpd -n 50` |
| 认证失败 | `chap-secrets` 的账号/密码/服务名（`l2tpserver`）不匹配 |
| 同一客户端两个 L2TP 连不上 | NM l2tp 插件只允许一个活动连接，先停掉另一个 |
| 连上打不开网页 | 检查 `ip_forward`、NAT 规则、DNS |

排障：

```bash
sudo journalctl -u xl2tpd -n 50 --no-pager
sudo iptables -t nat -S POSTROUTING | grep 192.168.44
sudo iptables -S FORWARD | grep 192.168.44
```

---

## 5. 卸载

```bash
sudo systemctl disable --now xl2tpd
# 删除 NAT/转发规则
sudo iptables -t nat -D POSTROUTING -s 192.168.44.0/24 ! -d 192.168.44.0/24 -j MASQUERADE
sudo iptables -D FORWARD -s 192.168.44.0/24 -j ACCEPT
sudo iptables -D FORWARD -d 192.168.44.0/24 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
sudo iptables -D FORWARD -s 192.168.44.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
# 恢复配置（若有备份）
sudo mv /etc/xl2tpd/xl2tpd.conf.bak /etc/xl2tpd/xl2tpd.conf
sudo rm -f /etc/ppp/options.xl2tpd /etc/sysctl.d/99-l2tp-plain.conf
# 删除账号行（手动编辑 /etc/ppp/chap-secrets）
```

> 不影响 OpenVPN 和 L2TP/IPsec。
