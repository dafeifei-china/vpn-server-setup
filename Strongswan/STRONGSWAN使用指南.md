# strongSwan IKEv2 使用指南（Docker 版）

> 部署脚本 `setup-strongswan.sh`。基于 `hwdsl2` 无关的自建镜像（Ubuntu 20.04 + strongSwan 5.8.2），**IKEv2 + 证书认证**。
> 目录结构：`Dockerfile`、`entrypoint.sh`、`etc/ipsec.conf`、`etc/ipsec.secrets`、`pki/`、`clients/`。

## 连接信息

| 项 | 值 |
|---|---|
| 服务器地址 | `192.168.0.106` |
| 协议 | IKEv2 / IPsec |
| 端口 | `500/udp`、`4500/udp` |
| 认证 | 证书（客户端证书由本机 CA 签发） |
| VPN 网段 | `10.10.10.0/24` |
| 客户端文件 | `clients/client1.p12`、`clients/ca.crt`、`clients/strongswan-client.conf` |

> ⚠️ **与 L2TP/IPsec、VPNC 冲突**：三者都要用 `500/4500`，**同一时间只能运行一个**。`setup-strongswan.sh` 启用前会停 `ipsec-vpn-server`；切到 VPNC 时先 `docker stop strongswan-server` 再 `SERVER_IP=<IP> ./setup-vpnc.sh`（或 `docker start vpnc-server`）；切回 L2TP/IPsec 则 `docker start ipsec-vpn-server`。

---

## 1. 服务端管理

```bash
sudo docker stop  strongswan-server
sudo docker start strongswan-server
sudo docker restart strongswan-server
sudo docker logs -f strongswan-server
sudo docker exec -it strongswan-server ipsec statusall
sudo docker exec -it strongswan-server ipsec status
```

重新部署（改 IP、重签证书等）：

```bash
./setup-strongswan.sh
```

---

## 2. 客户端连接

### 2.1 Windows

1. 双击 `client1.p12` 导入（无密码），存入"个人"；把 `ca.crt` 导入"受信任的根证书颁发机构"。
2. 设置 → 网络和 Internet → VPN → 添加：提供商 `Windows（内置）`，VPN 类型 **IKEv2**，服务器 `192.168.0.106`，身份验证"证书"，选择 `client1`。

### 2.2 macOS / iOS

把 `client1.p12` 和 `ca.crt` 通过"描述文件"或 AirDrop 安装到设备并信任，然后：
设置 → 通用 → VPN → 添加：类型 **IKEv2**，服务器 `192.168.0.106`，身份验证选证书 `client1`。

### 2.3 Android

安装 **strongSwan VPN Client**，导入 `client1.p12`（无密码），新建 IKEv2 配置，服务器 `192.168.0.106`。

### 2.4 Linux（strongSwan 命令行，已实测）

把客户端证书放进 `/etc/ipsec.d`：

```bash
sudo cp ca.crt /etc/ipsec.d/cacerts/
sudo cp client1.crt /etc/ipsec.d/certs/
sudo cp client1.key /etc/ipsec.d/private/          # 由服务端 pki/private/client1.key 提供
```

`/etc/ipsec.conf`：

```ini
config setup
    uniqueids=no

conn ikev2-client
    keyexchange=ikev2
    right=192.168.0.106
    rightid=192.168.0.106
    rightsubnet=0.0.0.0/0
    left=%defaultroute
    leftcert=client1.crt
    leftid=@client1
    leftsendcert=always
    leftsourceip=%config
    auto=add
```

`/etc/ipsec.secrets`：

```
: RSA client1.key
```

连接：

```bash
sudo ipsec restart
sudo ipsec up ikev2-client
ip -4 addr show | grep 10.10.10     # 拿到 10.10.10.x
```

### 2.5 Linux（NetworkManager 图形界面）

需 `network-manager-strongswan`。控制中心 → 网络 → VPN → 添加 → **IPsec/IKEv2 (strongswan)**：网关填**服务器当前 IP**，认证方式选证书，导入 `client1.p12` 与 `ca.crt`，并**勾选“请求内网 IP”**（对应 `virtual=yes`，否则拿不到 `10.10.10.x`）。

> 图形界面能连的前提：服务端 `rightsendcert=always`（`setup-strongswan.sh` 已内置）且服务端证书 CN/SAN = 当前服务器 IP。详见 `客户端连接方法.md` 的 FAQ。

---

## 3. 服务器 IP 变化

1. 重跑脚本（自动探测或显式指定）：`SERVER_IP=<新IP> ./setup-strongswan.sh`。
   > 服务端证书 CN/SAN 绑定了 IP，换 IP 后**必须重签**。脚本现在会**自动检测** `pki/certs/server.crt` 是否匹配新 IP，不匹配时**只重签服务端证书**（保留 CA 与客户端证书），无需手动删除 `pki/`。
   > 只有确实想连 CA 一起换（所有客户端都要换证书）时，才 `rm -rf pki clients` 再重跑。
2. 客户端把服务器地址改成新 IP（CA/客户端证书不变，无需重新分发）。
3. 防火墙/安全组放行 `500/udp`、`4500/udp`。

> 若常用域名，建议改脚本用域名签证书（把 `--san` 和 `leftid` 换成域名），这样换 IP 只需改 DNS。

### 3.1 重签证书的三种方式

**方式 A：重跑脚本（推荐，自动判断）**
```bash
cd "/home/uos/Desktop/VPN服务端搭建/Strongswan"
SERVER_IP=<新IP> ./setup-strongswan.sh
```
脚本自动检测 `pki/certs/server.crt` 的 IP，不符时只重签服务端证书；CA、客户端证书保持不变。需要 sudo 密码。

**方式 B：手动只重签服务端证书（不换 CA，客户端证书不动）**

用现有 CA 与 server 私钥重签 `server.crt`（适用于没跑脚本、或只想单独操作）：
```bash
cd "/home/uos/Desktop/VPN服务端搭建/Strongswan"
NEWIP=<新IP>

# 1) 用现有 CA 重签服务端证书（CN/SAN = 新 IP）
sg docker -c "docker run --rm --entrypoint /bin/sh -v \"\$PWD/pki:/etc/ipsec.d\" strongswan-vpn -c \"set -e; cd /etc/ipsec.d; \
  pki --pub --in private/server.key --outform pem > /tmp/server.pub; \
  pki --issue --lifetime 3650 --cacert cacerts/ca.crt --cakey private/ca.key \
    --dn 'C=CN, O=StrongSwan VPN, CN=$NEWIP' --san $NEWIP \
    --flag serverAuth --flag ikeIntermediate \
    --in /tmp/server.pub --outform pem > certs/server.crt; echo ISSUED\""

# 2) 修正文件属主（容器以 root 写）
sg docker -c "docker run --rm --entrypoint /bin/sh -v \"\$PWD/pki:/etc/ipsec.d\" strongswan-vpn -c 'chown 1000:0 /etc/ipsec.d/certs/server.crt'"

# 3) 让服务端 leftid 也等于新 IP
sed -i "s/^    leftid=.*/    leftid=$NEWIP/" etc/ipsec.conf

# 4) 重启容器加载新证书
sg docker -c "docker restart strongswan-server"
```
验证：
```bash
openssl x509 -in pki/certs/server.crt -noout -subject -ext subjectAltName   # 应为新 IP
sg docker -c "docker exec strongswan-server ipsec statusall" | grep -E 'local:|cert:'
```
客户端：**不用换证书**，只把连接里的服务器地址改成新 IP 即可。

**方式 C：连 CA 一起换（所有客户端都要换证书）**
```bash
cd "/home/uos/Desktop/VPN服务端搭建/Strongswan"
rm -rf pki clients
SERVER_IP=<新IP> ./setup-strongswan.sh
```
之后把 `clients/` 下的 `ca.crt` / `client1.crt` / `client1.key` / `client1.p12` 重新分发给所有客户端。

> 注意：重签证书后必须 `docker restart strongswan-server`（或容器内 `ipsec restart`）才会生效；只重签文件不重启，charon 仍用旧证书。

---

## 4. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 客户端 `AUTH_FAILED`（IKE_AUTH 请求里没有 `CERT`） | 客户端没发证书：图形界面需服务端 `rightsendcert=always`；命令行需客户端 `leftsendcert=always` |
| IKE_SA 已建立但拿不到 `10.10.10.x`（`FAILED_CP_REQUIRED`/`TS_UNACCEPT`） | 客户端图形界面未勾“请求内网 IP”（`virtual=yes`） |
| 连不上 500/4500 | 与 L2TP/IPsec、VPNC 冲突（都占 500/4500），确认 `ipsec-vpn-server` 与 `vpnc-server` 已停 |
| 连上后上不了网 | 检查服务端 NAT（`iptables-legacy -t nat -S POSTROUTING \| grep 10.10.10`）、`ip_forward`；DNS 生效需几秒 |
| 换 IP 后连不上（`certificate does not confirm identity`） | 证书 CN/SAN 与服务器 IP 绑定，重跑脚本会自动重签服务端证书 |
| 想同时用 L2TP/IPsec 或 VPNC | 不可行，都占 500/4500，三选一 |

排障：

```bash
sudo docker exec -it strongswan-server ipsec statusall
sudo docker logs --tail 50 strongswan-server
sudo iptables-legacy -t nat -S POSTROUTING | grep 10.10.10
```

---

## 5. 卸载

```bash
sudo docker rm -f strongswan-server
sudo docker rmi strongswan-vpn
# 若不再需要，删除证书与客户端文件
rm -rf pki clients
```

> 卸载不影响 OpenVPN；如需恢复 L2TP/IPsec：`sudo docker start ipsec-vpn-server`。
