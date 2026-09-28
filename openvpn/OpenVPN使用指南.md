# OpenVPN 使用指南（Docker 版）

> 服务端一键搭建见 `setup-openvpn.sh`；详细原理与排障见 `OpenVPN搭建方法.md`（§10 本地实操、§11 客户端连接）。
> 本文档讲**日常使用**：服务端启停、IP 变化处理、客户端连接、证书维护、卸载。

约定：

| 项 | 值 |
|---|---|
| 服务端容器 | `running-vpn-openvpn-tls` |
| 镜像 | `vpn-openvpn-tls` |
| 端口 / 协议 | `1194/udp` |
| 隧道网段 | `10.99.99.0/24`（服务端 `10.99.99.1`） |
| 客户端文件 | `local-server/clients/` |
| 客户端私钥口令 | `test`（`client.ovpn`）/ 空（`client-nopass.ovpn`） |

> 所有 `docker` 命令若提示权限不足，用 `sudo docker`；本机已把用户加入 `docker` 组，也可用 `sg docker -c 'docker ...'`。

---

## 1. 服务端：启用 / 禁用 / 查看

### 1.1 只停 / 启 VPN 服务（保留 Docker，推荐）

```bash
sudo docker stop  running-vpn-openvpn-tls     # 禁用
sudo docker start running-vpn-openvpn-tls     # 启用
sudo docker restart running-vpn-openvpn-tls   # 重启
sudo docker ps -a --filter name=running-vpn-openvpn-tls   # 看状态
sudo docker logs --tail 50 running-vpn-openvpn-tls        # 看日志
```

容器策略是 `unless-stopped`：**手动 `stop` 之后，即使机器重启它也不会自己起来**，直到你再 `start` 一次。

### 1.2 取消 / 恢复开机自启

```bash
sudo docker update --restart=no             running-vpn-openvpn-tls   # 取消自启
sudo docker update --restart=unless-stopped running-vpn-openvpn-tls   # 恢复自启
```

### 1.3 连 Docker 一起停 / 启（影响所有容器）

```bash
sudo systemctl stop docker          # 停守护进程 + 所有容器
sudo systemctl disable docker       # 取消 Docker 开机自启
sudo systemctl enable --now docker  # 恢复
sudo docker start running-vpn-openvpn-tls
```

### 1.4 停用后的网络残留与清理

停容器会移除 `tun0`、结束 OpenVPN，但宿主机的这些设置**会残留**：

- `net.ipv4.ip_forward = 1`
- legacy iptables 的 MASQUERADE / TCPMSS 规则

要让机器完全恢复原状：

```bash
sudo iptables-legacy -t nat -D POSTROUTING -s 10.99.99.0/24 ! -d 10.99.99.0/24 -j MASQUERADE
sudo iptables-legacy -D FORWARD -s 10.99.99.0/24 -p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356
sudo sysctl -w net.ipv4.ip_forward=0
```

再次 `docker start` 时 entrypoint 会自动把规则加回来（已做幂等判断）。

### 1.5 健康检查

```bash
sudo docker logs --tail 20 running-vpn-openvpn-tls | grep "Initialization Sequence Completed"
ss -ulnp | grep 1194
ip -4 addr show tun0
```

---

## 2. 服务端 IP 变了怎么办

**结论：证书不用重签**（证书和 IP 无关），只需更新客户端配置里的 `remote`，并保证新 IP 对客户端可达。

### 2.1 查新 IP

```bash
ip -4 addr show | grep 'inet '
# 或：ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}'
```

### 2.2 重新生成客户端配置（推荐）

脚本会**复用已有证书**，只更新客户端的 `remote`，并重建容器：

```bash
cd /path/to/openvpn
SERVER_IP=<新IP> ./setup-openvpn.sh
```

### 2.3 或者只手工改 remote（不重跑脚本）

```bash
cd local-server/clients
sed -i 's/^remote .*/remote <新IP> 1194/' client.ovpn client.conf client-nopass.ovpn client-nopass.conf
```

> 若新 IP 不在同一局域网，还要在路由器/云安全组放行 `1194/udp`；用域名的话只改 DNS 记录即可（推荐用 DDNS，免得每次改配置）。

### 2.4 客户端侧：禁用旧连接、启用新连接

在**客户端**机器上：

```bash
# 1) 断开并删除旧连接
nmcli connection down <旧连接名>
sudo nmcli connection delete <旧连接名>

# 2) 导入新配置（把新 client.conf 拷过来）
sudo nmcli connection import type openvpn file ~/client.conf
nmcli connection up client

# 3) 验证
ip -4 addr show tun0
ping -c 3 10.99.99.1
```

图形界面：先把旧 VPN 删掉，再导入新的 `client.conf`。

### 2.5 换 IP 期间的"禁用/启用"做法

- 想先停服务再改：`sudo docker stop running-vpn-openvpn-tls` → 改配置/网络 → `sudo docker start running-vpn-openvpn-tls`。
- 也可以不停服务，直接让客户端指向新 IP。

### 2.6 隧道网段也变了

改脚本变量重跑（会自动同步 `openvpn.conf` 和 `entrypoint.sh`）：

```bash
SERVER_IP=<新IP> VPN_SUBNET=10.88.0.0 ./setup-openvpn.sh
```

然后客户端重新导入新配置。

---

## 3. 客户端连接（UOS V25）

### 3.1 图形界面导入（最简单）

控制中心 → 网络 → VPN → 添加（+）→ OpenVPN → **导入文件** → 选 `local-server/clients/client.conf` → 私钥密码 `test` → 保存并打开。

> 图形界面只认 `.conf`，脚本已同时生成 `.ovpn` 和 `.conf`。

### 3.2 nmcli 导入

```bash
sudo nmcli connection import type openvpn file ~/client.conf
nmcli connection up client
```

### 3.3 命令行

```bash
sudo openvpn --config client.ovpn        # 提示口令输 test
# 后台：加 --daemon；停止：sudo pkill openvpn
```

### 3.4 手动创建（不想导入文件时）

网关 `192.168.0.106`、端口 `1194`、协议 `UDP`、认证类型 `TLS 证书`、CA=`ca.crt`、用户证书=`client.crt`、私钥=`client.key`（密码留空）或 `client.key.enc`（密码 `test`）。用户名/密码**留空**。

### 3.5 验证

```bash
ip -4 addr show tun0        # 应拿到 10.99.99.x
ping -c 3 10.99.99.1
nmcli -f NAME,DEVICE connection show --active | grep -i vpn
```

---

## 4. 新增 / 吊销客户端

### 4.1 新增

```bash
cd local-server/etc/openvpn/easy-rsa
./easyrsa build-client-full client2 nopass        # 3.1.7 不能带 EASYRSA_REQ_CN
# 再按脚本方式生成 client2.ovpn（把 client 换成 client2）
```

### 4.2 吊销

```bash
cd local-server/etc/openvpn/easy-rsa
./easyrsa revoke client2
./easyrsa gen-crl
# 在 openvpn.conf 增加： crl-verify /etc/openvpn/easy-rsa/pki/crl.pem
# 然后重建镜像并重启容器
```

---

## 5. 证书续期

1. 备份旧 `local-server/etc/openvpn/easy-rsa/pki`。
2. 删除 `pki` 后重跑 `./setup-openvpn.sh`（会自动重签并重新生成客户端配置）。
3. 重新分发新客户端配置。

> CA 一旦重签，**所有客户端都要换新证书**。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 客户端连不上，服务端日志无该客户端记录 | `remote` 写错（常见是错用了旧配置 `10.20.8.198`） | `grep '^remote' client.conf` 确认是当前服务端 IP |
| 客户端报 `没有有效的密钥` | 加密私钥 + 无图形会话缺 secret agent | 用 `client-nopass` 或图形界面连接 |
| `Cipher BF-CBC not supported` | 服务端 OpenVPN < 2.4 | 用本脚本镜像（2.4.12） |
| 能连上但访问不了内网 | 未开转发/NAT 或未下发路由 | 检查 `ip_forward`、MASQUERADE；需要时用 `FULL_TUNNEL=yes` |
| 路由被别的 VPN 抢走 | 客户端同时开着 `utun7` 等 | `ip route get <服务端IP>` 确认走 `wlpXs0` |
| 服务端重启后 VPN 没了 | 容器未设自启 | `sudo docker update --restart=unless-stopped running-vpn-openvpn-tls` |

排障命令：

```bash
sudo docker logs --tail 100 running-vpn-openvpn-tls
ss -ulnp | grep 1194
sudo iptables-legacy -t nat -L POSTROUTING -n -v
journalctl -u NetworkManager -n 50 --no-pager    # 客户端侧
```

---

## 7. 卸载 / 清理

```bash
# 1) 停并删除容器、镜像
sudo docker rm -f running-vpn-openvpn-tls
sudo docker rmi vpn-openvpn-tls

# 2) 停用 Docker（如不再需要）
sudo systemctl disable --now docker

# 3) 删除网络残留
sudo iptables-legacy -t nat -D POSTROUTING -s 10.99.99.0/24 ! -d 10.99.99.0/24 -j MASQUERADE
sudo iptables-legacy -D FORWARD -s 10.99.99.0/24 -p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356
sudo sysctl -w net.ipv4.ip_forward=0

# 4) 若是静态二进制安装的 Docker，再删这些（apt 装的不要删）
sudo rm -f /etc/systemd/system/docker.service /etc/docker/daemon.json \
  /usr/local/bin/{dockerd,containerd,containerd-shim-runc-v2,docker-proxy,runc,ctr,docker,docker-init}
sudo rm -rf /var/lib/docker /var/lib/containerd
sudo groupdel docker

# 5) 删除构建产物（含私钥，谨慎）
rm -rf local-server
```

> `local-server/` 内含 CA 私钥与所有客户端私钥，删除前请确认已备份或不再需要。
