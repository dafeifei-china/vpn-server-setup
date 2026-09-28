FROM ubuntu:20.04

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y \
      strongswan strongswan-starter libcharon-extra-plugins iptables iproute2 kmod \
 && rm -rf /var/lib/apt/lists/*

COPY etc/ipsec.conf /etc/ipsec.conf
COPY etc/ipsec.secrets /etc/ipsec.secrets
COPY etc/strongswan-vpnc.conf /etc/strongswan.d/vpnc.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE 500/udp 4500/udp
ENTRYPOINT ["/entrypoint.sh"]
