FROM ubuntu:20.04

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y ocserv iptables iproute2 kmod \
 && rm -rf /var/lib/apt/lists/*

COPY etc/ocserv/ /etc/ocserv/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE 8443/tcp 8443/udp
ENTRYPOINT ["/entrypoint.sh"]
