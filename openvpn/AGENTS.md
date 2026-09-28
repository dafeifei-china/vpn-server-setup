# AGENTS.md

## What this repo is
Not a code project. It is an OpenVPN deployment runbook plus ready-to-use client profiles. There is no build, test, lint, or package manifest — do not invent commands.

- `OpenVPN搭建方法.md` — authoritative, Chinese, end-to-end runbook (server via Docker + easy-rsa, client generation, troubleshooting, renewal). Read it before changing anything.
- `client.conf` / `client-nopass.conf` — self-contained OpenVPN client profiles (embedded CA/cert/key). They are identical except for the private key.
- `setup-openvpn.sh` — one-shot installer (Docker → certs → image → container → clients). Re-runnable: reuses an existing `local-server/etc/openvpn/easy-rsa/pki` and only regenerates clients. `SERVER_IP`/`VPN_SUBNET`/`FULL_TUNNEL` are env overrides; see header.
- `OpenVPN使用指南.md` — daily ops (start/stop, IP change, client connect, cert maintenance, uninstall).
- `local-server/` — self-contained build tree for the working local deployment (Dockerfile, entrypoint, openvpn.conf, easy-rsa pki, generated `clients/*.ovpn`). Unrelated to the top-level profiles above.

## Local deployment (`local-server/`)
Image `vpn-openvpn-tls`, container `running-vpn-openvpn-tls` (`--net host --privileged`). Server workflow/gotchas/teardown are in §10, client-side connection (GUI + `nmcli`) in §11 of the runbook.

- Client side (UOS/NetworkManager): an encrypted private key needs a secret agent, so activating over SSH/headless fails with `没有有效的密钥` (`No agents were available`); use the plaintext `client.key` or connect from the GUI. The key-passphrase secret is named `cert-pass`, not `key-passwd`.

- Docker was installed from Tsinghua **static binaries** into `/usr/local/bin` because the UOS apt CDN (`cdn-update-platform-packages.uniontech.com` → `10.20.0.52`) is hijacked by the existing `utun7` VPN and times out.
- Docker access: `uos` is in the `docker` group, so new shells just run `docker`. A long-lived shell opened before the group add needs `sg docker -c '...'` (no re-login needed).
- Registry mirror `https://docker.m.daocloud.io` is set in `/etc/docker/daemon.json`; Docker Hub itself is unreachable.
- EasyRSA 3.1.7 quirk: `build-server-full` / `build-client-full` **reject** `EASYRSA_REQ_CN` (CN comes from the filename); only `build-ca` accepts it.
- Bridge-container test gotcha: the `--net host` server replies from `172.17.0.1`, so a client configured with `remote 192.168.0.106` drops the packets. Override `remote` to `172.17.0.1` for the test only.

## Profile facts
- `client.conf`: private key is encrypted; passphrase is `test`.
- `client-nopass.conf`: unencrypted private key.
- Both hardcode `remote 10.20.8.198 1194` (UDP). Any other network requires editing this line.
- CA CN is `deepin-qa-team`; certs expire 2036-09-15.
- These files embed live private keys. Treat as secrets; never paste contents into external services or logs.

## Editing gotchas
- The `<cert>` block in `client.conf` includes an `openssl x509 -text` dump before the `-----BEGIN CERTIFICATE-----` PEM; `client-nopass.conf` has only the PEM. Match the existing format when regenerating.
- Two unrelated cert sets exist: `local-server/` (CA `local-vpn-ca`, `remote 192.168.0.106`) and the top-level `client*.conf` (CA `deepin-qa-team`, `remote 10.20.8.198`). The latter came from an external build tree (path in §9) that is not on this machine; do not mix certs or keys between them.
- Keep server OpenVPN ≥ 2.4; 2.3.x defaults to BF-CBC, which modern clients reject (`Cipher BF-CBC not supported`).
