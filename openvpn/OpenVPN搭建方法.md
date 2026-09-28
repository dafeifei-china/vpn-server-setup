# UOS 从零搭建 OpenVPN 完整方法

> 适用：在 UOS Desktop 20（或同系 Debian 系发行版）上，用 Docker 部署 OpenVPN 服务端（证书 TLS 认证），并生成客户端 `.ovpn`。
> 本文结合一次真实排障整理，已规避证书过期、BF-CBC 不兼容、Docker Hub 不可达、重启不自启等坑。

---

## 0. 架构与关键结论

| 项目 | 说明 |
|---|---|
| 认证方式 | 证书 TLS（`tls-server`），不是账号密码 |
| 服务端 OpenVPN | **必须 ≥ 2.4**（支持 AES-GCM）。2.3.x 默认 `BF-CBC`，现代客户端（2.5/2.6）已移除该算法，会报 `Cipher BF-CBC not supported` |
| 证书 | 用 easy-rsa 生成，**必须显式设置有效期**，否则易过期导致 `certificate has expired` |
| 部署 | Docker + `--net host --privileged` |
| 自启 | 容器加 `--restart unless-stopped`，否则服务器重启后不会拉起 |
| 客户端 | 自包含 `.ovpn`（内嵌 CA/证书/私钥），私钥可加密 |
| 内网网段 | 示例 `10.99.99.0/24`，可按需修改 |

拓扑：

```
[客户端 UOS/Win/Mac] --UDP 1194--> [UOS 服务端: Docker 容器 OpenVPN]
                                         |  tun: 10.99.99.1
                                         |  NAT 转发到服务端所在网络
```

---

## 1. 服务端准备

### 1.1 安装依赖

```bash
sudo apt update
sudo apt install -y docker.io easy-rsa
sudo systemctl enable --now docker
docker version
```

### 1.2（可选）Docker Hub 不可达时导入基础镜像

若 `docker pull ubuntu:20.04` 超时，用 Ubuntu 官方 base 根文件系统导入：

```bash
curl -L -o /tmp/ubuntu-base.tar.gz \
  https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage/ubuntu-base/releases/20.04/release/ubuntu-base-20.04.5-base-amd64.tar.gz
docker import /tmp/ubuntu-base.tar.gz ubuntu:20.04
docker images ubuntu:20.04
```

---

## 2. 生成证书（easy-rsa）

先建构建目录：

```bash
mkdir -p ~/vpn-openvpn-tls/etc/openvpn
cd ~/vpn-openvpn-tls/etc/openvpn
cp -a /usr/share/easy-rsa ./easy-rsa
cd easy-rsa
```

生成 CA、服务端、客户端证书：

```bash
export EASYRSA_BATCH=1
export EASYRSA_ALGO=rsa
export EASYRSA_KEY_SIZE=2048
export EASYRSA_CA_EXPIRE=3650      # CA 10 年
export EASYRSA_CERT_EXPIRE=3650    # 证书 10 年

./easyrsa init-pki
EASYRSA_REQ_CN="my-vpn-ca" ./easyrsa build-ca nopass
EASYRSA_REQ_CN="server"    ./easyrsa build-server-full server nopass
EASYRSA_REQ_CN="client"    ./easyrsa build-client-full client nopass
./easyrsa gen-dh
```

给客户端私钥加口令（示例口令 `test`，导入 .ovpn 时会提示输入）：

```bash
openssl pkcs8 -topk8 -in pki/private/client.key \
  -out pki/private/client.key.enc -passout pass:test -v2 aes-256-cbc
mv -f pki/private/client.key.enc pki/private/client.key
chmod 600 pki/private/client.key
```

校验有效期：

```bash
openssl x509 -in pki/ca.crt -noout -dates
openssl x509 -in pki/issued/server.crt -noout -dates
openssl x509 -in pki/issued/client.crt -noout -dates
```

> 注意：`client.key` 是加密私钥，口令为 `test`。若不需要加密，跳过上面 openssl 步骤即可。

---

## 3. 编写服务端配置

目录结构：

```
vpn-openvpn-tls/
├── Dockerfile
├── entrypoint.sh
└── etc/openvpn/
    ├── openvpn.conf
    └── easy-rsa/           # 第 2 步生成的 pki
```

### 3.1 Dockerfile

```dockerfile
FROM ubuntu:20.04

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y iptables openvpn iproute2 \
 && rm -rf /var/lib/apt/lists/*

COPY ./etc/openvpn/openvpn.conf /etc/openvpn/openvpn.conf
COPY ./etc/openvpn/easy-rsa/ /etc/openvpn/easy-rsa/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE 1194/udp
ENTRYPOINT ["/entrypoint.sh"]
CMD openvpn --config /etc/openvpn/openvpn.conf
```

### 3.2 entrypoint.sh（开启转发 + NAT）

```sh
#!/bin/sh
set -e
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -s 10.99.99.0/24 ! -d 10.99.99.0/24 -j MASQUERADE
iptables -A FORWARD -s 10.99.99.0/24 -p tcp \
  -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356
exec "$@"
```

```bash
chmod +x entrypoint.sh
```

### 3.3 etc/openvpn/openvpn.conf（关键：cipher 配置）

```conf
tls-server
port 1194
proto udp
dev tun
ca   /etc/openvpn/easy-rsa/pki/ca.crt
cert /etc/openvpn/easy-rsa/pki/issued/server.crt
key  /etc/openvpn/easy-rsa/pki/private/server.key
dh   /etc/openvpn/easy-rsa/pki/dh.pem
server 10.99.99.0 255.255.255.0
client-to-client
keepalive 10 120
persist-key
persist-tun
cipher AES-256-CBC
ncp-ciphers AES-256-GCM:AES-128-GCM
verb 3
```

> 若要全流量走 VPN（默认网关模式），再加一行：
> `push "redirect-gateway def1 bypass-dhcp"`
> `push "dhcp-option DNS 8.8.8.8"`

---

## 4. 构建镜像并启动容器

```bash
cd ~/vpn-openvpn-tls
docker build -t vpn-openvpn-tls .

docker run -d --net host --privileged --restart unless-stopped \
  --name running-vpn-openvpn-tls vpn-openvpn-tls
```

验证：

```bash
docker ps
docker logs --tail 30 running-vpn-openvpn-tls
# 出现 "Initialization Sequence Completed" 即成功
docker exec running-vpn-openvpn-tls openvpn --version | head -1
```

### 4.1 放行防火墙

```bash
# ufw
sudo ufw allow 1194/udp
# firewalld
sudo firewall-cmd --add-port=1194/udp --permanent && sudo firewall-cmd --reload
```

### 4.2 云服务器/跨网访问

需在网关/安全组放行 **UDP 1194**，客户端 `remote` 填公网可达地址。

---

## 5. 生成客户端 .ovpn

在 `etc/openvpn/easy-rsa` 目录执行：

```bash
cat > client.ovpn <<'EOF'
client
dev tun
proto udp
remote <服务端IP> 1194
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
auth-nocache
verb 3
EOF
echo "<ca>"   >> client.ovpn; cat pki/ca.crt             >> client.ovpn; echo "</ca>"   >> client.ovpn
echo "<cert>" >> client.ovpn; cat pki/issued/client.crt  >> client.ovpn; echo "</cert>" >> client.ovpn
echo "<key>"  >> client.ovpn; cat pki/private/client.key  >> client.ovpn; echo "</key>"  >> client.ovpn
chmod 600 client.ovpn
```

把 `<服务端IP>` 换成实际地址（如 `10.20.8.198`）。

---

## 6. 客户端使用

### 6.1 命令行（UOS / Linux）

```bash
sudo openvpn --config client.ovpn
# 提示 Enter Private Key Password: 输入 test
# 看到 Initialization Sequence Completed 即连接成功
```

后台运行：加 `--daemon`；停止：`sudo pkill openvpn`。

### 6.2 UOS 图形界面（NetworkManager）

控制中心 → 网络 → VPN → 添加（+）→ 选 OpenVPN → 导入文件 → 选 `client.ovpn` → 私钥密码填 `test` → 保存并打开。

### 6.3 Windows / macOS

拷贝 `client.ovpn`，用 OpenVPN Connect（Win/Mac）或 Tunnelblick（Mac）导入，密码 `test`。

连接后客户端分配 `10.99.99.x`，可访问 `10.99.99.0/24`。默认分流（仅该网段走 VPN）。

---

## 7. 常见问题排查

| 现象 | 根因 | 解决 |
|---|---|---|
| `VERIFY ERROR: certificate has expired` | 证书过期 | easy-rsa 重签，显式设有效期 |
| `Cipher BF-CBC not supported` | 服务端 OpenVPN 2.3.x 默认 BF-CBC，客户端已移除 | 服务端升级到 ≥2.4，配 `ncp-ciphers AES-256-GCM:AES-128-GCM` |
| `docker pull` 超时 | Docker Hub 不可达 | ubuntu-base rootfs `docker import`（见 1.2） |
| 端口通但连不上 | `remote` 地址/端口错，或服务端没监听 | 核对 `remote`、`docker ps`、`ss -ulnp \| grep 1194` |
| 私钥加载失败/一直提示口令 | key 加密且未提供口令 | 导入时输入口令，或重新生成无口令 key |
| `Cannot ioctl TUNSETIFF ... Operation not permitted` | 非 root 运行客户端 | 用 `sudo openvpn` 或 GUI |
| 服务器重启后 VPN 没了 | 容器 restart=no | `docker update --restart=unless-stopped <容器>` |
| `TLS handshake failed` 且服务端日志无记录 | 防火墙/安全组未放行 UDP 1194 | 放行端口 |
| 能连上但访问不了内网 | 未开转发/NAT，或路由未下发 | 检查 `ip_forward`、MASQUERADE、`server`/`push route` |

排障常用命令：

```bash
# 服务端
docker logs --tail 100 running-vpn-openvpn-tls
ss -ulnp | grep 1194
sysctl net.ipv4.ip_forward
iptables -t nat -L POSTROUTING -n -v

# 客户端（加 verb 4 看握手细节）
sudo openvpn --config client.ovpn --verb 4
```

---

## 8. 日常运维

```bash
# 状态 / 日志
docker ps
docker logs --tail 50 running-vpn-openvpn-tls

# 重启
docker restart running-vpn-openvpn-tls

# 查看证书到期时间（提前续签）
cd ~/vpn-openvpn-tls/etc/openvpn/easy-rsa
openssl x509 -in pki/issued/server.crt -noout -enddate
openssl x509 -in pki/ca.crt -noout -enddate

# 新增客户端
EASYRSA_REQ_CN="client2" ./easyrsa build-client-full client2 nopass
# 再按第 5 步生成 client2.ovpn

# 吊销客户端
./easyrsa revoke client2
./easyrsa gen-crl
# openvpn.conf 增加：crl-verify /etc/openvpn/easy-rsa/pki/crl.pem
# 然后重建镜像/重启容器
```

### 证书续签流程（临近到期时）

1. 重新生成 CA + 服务端 + 客户端证书（第 2 步，注意先备份旧 `pki`）。
2. 覆盖构建目录 `etc/openvpn/easy-rsa/pki`。
3. `docker build -t vpn-openvpn-tls .` 重建镜像。
4. `docker rm -f running-vpn-openvpn-tls` 后按第 4 步重新 run（保持 `--net host --privileged --restart unless-stopped`）。
5. 重新分发新的 `client.ovpn` 给所有客户端。

> 注意：CA 无法简单续期，CA 一旦重签，所有客户端都要换新证书。

---

## 9. 本次环境参考值

| 项 | 值 |
|---|---|
| 服务端 IP | 10.20.8.198（主机名 PECI2414） |
| OpenVPN 版本 | 2.4.12（容器） |
| 隧道网段 | 10.99.99.0/24，服务端 10.99.99.1 |
| 端口/协议 | 1194/udp |
| CA CN | deepin-qa-team |
| 客户端密钥口令 | test |
| 构建目录 | `/media/ut002998/_dde_data/04.temp/可用VPN/vpn-openvpn-tls` |
| 容器名 | running-vpn-openvpn-tls（restart=unless-stopped） |

---

## 10. 本地（UOS 25）实操补充

本节记录一次在 UOS Desktop 25 上按本文部署时遇到的差异与解法，构建产物在仓库 `local-server/`。

> 以下流程已固化为 **`setup-openvpn.sh`**（一键搭建：装 Docker → 生成证书 → 构建镜像 → 启动容器 → 生成客户端），日常启停/IP 变更/客户端连接见 **`OpenVPN使用指南.md`**。

### 10.1 Docker 安装（UOS 软件源不可达时）

UOS 自带的 `docker.io` 依赖 CDN `cdn-update-platform-packages.uniontech.com`（解析到内网 `10.20.0.52`），若被现有 VPN 路由（如 `utun7`）劫持会连接超时。改用清华镜像的静态二进制：

```bash
cd /tmp
curl -L -o docker-static.tgz \
  https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/static/stable/x86_64/docker-29.8.1.tgz
tar xzf docker-static.tgz
sudo install -m0755 docker/* /usr/local/bin/
sudo groupadd -r docker 2>/dev/null; sudo usermod -aG docker "$USER"
```

`/etc/docker/daemon.json`（Docker Hub 不可达时加速）：

```json
{ "registry-mirrors": ["https://docker.m.daocloud.io"] }
```

`/etc/systemd/system/docker.service`（静态二进制版；dockerd 会自行拉起 containerd，无需单独 unit）：

```ini
[Unit]
Description=Docker Application Container Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd -H unix:///var/run/docker.sock
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=2
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now docker
sg docker -c 'docker version'   # 加入 docker 组后无需重登录，用 sg 即可免 sudo
```

> dockerd 在 `docker` 组存在之前启动时，`/var/run/docker.sock` 会归属 `root:root`；建组后需 `sudo systemctl restart docker` 修正权限。

### 10.2 easy-rsa（UOS 源里没有）

直接从 GitHub 下载 EasyRSA 3.1.7（兼容 OpenSSL 3.x），解压到构建目录，无需安装：

```bash
curl -L -o /tmp/EasyRSA.tgz \
  https://github.com/OpenVPN/easy-rsa/releases/download/v3.1.7/EasyRSA-3.1.7.tgz
mkdir -p local-server/etc/openvpn
tar xzf /tmp/EasyRSA.tgz -C local-server/etc/openvpn
mv local-server/etc/openvpn/EasyRSA-3.1.7 local-server/etc/openvpn/easy-rsa
```

> 3.1.7 差异：`build-server-full` / `build-client-full` **不接受** `EASYRSA_REQ_CN`（会报 `does not support setting an external commonName`），CN 由文件名决定；只有 `build-ca` 需要 `EASYRSA_REQ_CN`。第 2 步相应改为：
> `./easyrsa build-server-full server nopass`、`./easyrsa build-client-full client nopass`。

### 10.3 本地验证的坑

用桥接容器当客户端测试时，服务端（`--net host`）回包源地址会变成 Docker 网关 `172.17.0.1`，而客户端配置写的是 `remote 192.168.0.106`，会被 `Incoming packet rejected ... expected peer address` 丢弃。测试时把 remote 临时改为 `172.17.0.1` 即可；局域网真机连 `192.168.0.106` 无此问题。

```bash
sg docker -c 'docker run --rm --cap-add NET_ADMIN --device /dev/net/tun \
  -v "$PWD/local-server/clients:/clients:ro" --entrypoint bash vpn-openvpn-tls -c "
    sed \"s|^remote .*|remote 172.17.0.1 1194|\" /clients/client-nopass.ovpn > /tmp/t.ovpn
    openvpn --config /tmp/t.ovpn --verb 3 & sleep 8; ip -4 addr show tun0"'
```

### 10.4 清理（还原系统）

```bash
sg docker -c 'docker rm -f running-vpn-openvpn-tls; docker rmi vpn-openvpn-tls'
sudo systemctl disable --now docker
sudo rm -f /etc/systemd/system/docker.service /etc/docker/daemon.json \
  /usr/local/bin/{dockerd,containerd,containerd-shim-runc-v2,docker-proxy,runc,ctr,docker,docker-init}
sudo rm -rf /var/lib/docker /var/lib/containerd
sudo iptables-legacy -t nat -D POSTROUTING -s 10.99.99.0/24 ! -d 10.99.99.0/24 -j MASQUERADE
sudo iptables-legacy -D FORWARD -s 10.99.99.0/24 -p tcp -m tcp --tcp-flags FIN,SYN,RST,ACK SYN -j TCPMSS --set-mss 1356
sudo groupdel docker
```

### 10.5 本次本地环境参考值

| 项 | 值 |
|---|---|
| 服务端 IP / remote | 192.168.0.106 |
| CA CN | local-vpn-ca |
| 客户端密钥口令 | test |
| 构建目录 | 仓库内 `local-server/` |
| 容器名 | running-vpn-openvpn-tls |

---

## 11. 客户端连接（UOS V25）

### 11.1 需要的文件

从服务端 `local-server/clients/` 拷到客户端（示例放 `~/vpn-certs/`）：

| 文件 | 用途 |
|---|---|
| `ca.crt` | CA 证书 |
| `client.crt` | 用户证书 |
| `client.key` | 私钥（未加密，无需口令） |
| `client.key.enc` | 私钥（加密，口令 `test`） |
| `client.ovpn` / `client-nopass.ovpn` | 自包含配置（导入用，需改名 `.conf`） |

```bash
scp uos@192.168.0.106:".../local-server/clients/"{ca.crt,client.crt,client.key,client.key.enc} ~/vpn-certs/
```

### 11.2 图形界面手动创建

控制中心 → 网络 → VPN → 添加（+）→ OpenVPN：

| 字段 | 值 |
|---|---|
| 网关 | `192.168.0.106` |
| 端口 | `1194` |
| 协议 | `UDP` |
| 设备 | `TUN` |
| 认证类型 | `TLS 证书` |
| CA 证书 | `~/vpn-certs/ca.crt` |
| 用户证书 | `~/vpn-certs/client.crt` |
| 私钥 | `~/vpn-certs/client.key.enc`（或 `client.key`） |
| 私钥密码 | `test`（仅用 `.enc` 时；用 `client.key` 留空） |
| 用户名/密码 | **留空**（服务端用证书认证，无账号密码） |
| 验证服务器证书 | `server`（如有该选项） |

### 11.3 nmcli 手动创建（等价，无头环境推荐）

```bash
sudo nmcli connection add type vpn con-name vpn-106 vpn-type openvpn \
  vpn.data "remote=192.168.0.106:1194,connection-type=tls,\
ca=/home/uos/vpn-certs/ca.crt,cert=/home/uos/vpn-certs/client.crt,\
key=/home/uos/vpn-certs/client.key,remote-cert-tls=server,cipher=AES-256-CBC"
sudo nmcli connection up vpn-106
```

> 在 SSH 等无图形会话里建连/导入必须用 `sudo`，否则报 `Insufficient privileges`（polkit 不放行普通用户）。

### 11.4 导入方式（GUI 只认 `.conf`）

```bash
cp client.ovpn ~/vpn-106.conf
sudo nmcli connection import type openvpn file ~/vpn-106.conf   # 内嵌证书会被自动提取
```

图形界面导入则把 `.ovpn` 改名为 `.conf` 再选文件。

### 11.5 客户端常见坑

- **加密私钥 + 无头环境**：NM 私钥口令的密钥名是 `cert-pass`，且需要 secret agent；SSH/无图形会话会报 `没有有效的密钥` / `No agents were available`。**图形界面会弹框输口令，正常**；纯命令行/无头场景请用未加密的 `client.key`。
- **错连旧配置**：仓库根目录的 `client.conf` 指向 `remote 10.20.8.198`，不是本地服务端。建连前务必 `grep '^remote'` 确认是 `192.168.0.106 1194`。
- **路由被别的 VPN 抢走**：客户端若同时开着 `utun7` 之类 VPN，先 `ip route get 192.168.0.106` 确认走的是 `wlpXs0`。
- 服务端 `verb 3` 日志出现 `VERIFY OK: CN=local-vpn-ca`、`Peer Connection Initiated` 即握手成功；客户端看到 `Initialization Sequence Completed` 即连上。

验证：

```bash
ip -4 addr show tun0        # 应拿到 10.99.99.x
ping -c 3 10.99.99.1
nmcli -f NAME,DEVICE connection show --active | grep -i vpn
```
