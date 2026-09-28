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
