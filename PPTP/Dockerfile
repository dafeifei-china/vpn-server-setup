FROM ubuntu:20.04

ENV DEBIAN_FRONTEND noninteractive
RUN sed -i 's|//archive.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g; \
           s|//security.ubuntu.com|//mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y pptpd iptables iproute2 kmod \
 && rm -rf /var/lib/apt/lists/*

COPY etc/pptpd.conf /etc/pptpd.conf
COPY etc/ppp/pptpd-options /etc/ppp/pptpd-options
COPY etc/ppp/chap-secrets /etc/ppp/chap-secrets
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0700 /entrypoint.sh

EXPOSE 1723/tcp
ENTRYPOINT ["/entrypoint.sh"]
