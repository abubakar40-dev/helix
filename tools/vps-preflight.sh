#!/usr/bin/env bash
# VPS preflight for HELIX CALL — read-only, prints a report, changes nothing.
# Run this ON the VPS (Oracle Cloud Shell "Connect via SSH" terminal, or your own
# ssh session). Then paste the whole output back to me.
#
# Why: Phase A step 1 requires confirming RAM/resource headroom BEFORE installing
# anything, the same discipline used for the Ollama install.

echo "===== 1. IDENTITY / IMAGE ====="
echo "hostname : $(hostname)"
echo "user     : $(whoami)"
echo "os       : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "kernel   : $(uname -r)"
echo "arch     : $(uname -m)"

echo
echo "===== 2. SHAPE / CPU ====="
echo "cpus     : $(nproc)"
lscpu 2>/dev/null | grep -E 'Model name|Architecture' || true

echo
echo "===== 3. MEMORY — is there room for coturn + a signaling server? ====="
free -h
echo
echo "-- swap --"
swapon --show 2>/dev/null || echo "  (no swap)"

echo
echo "===== 4. DISK ====="
df -h / | tail -1

echo
echo "===== 5. LOAD — how busy is it already? ====="
uptime

echo
echo "===== 6. FIREWALL (host-level) ====="
echo "-- ufw --"
(command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null) || echo "  (ufw absent, or needs sudo password)"
echo "-- firewalld --"
(command -v firewall-cmd >/dev/null && sudo -n firewall-cmd --list-all 2>/dev/null) || echo "  (firewalld absent, or needs sudo password)"
echo "-- iptables filter rules (unprivileged view) --"
sudo -n iptables -S 2>/dev/null | head -20 || echo "  (needs sudo password, skipped)"

echo
echo "===== 7. LISTENING PORTS — what is already here? ====="
ss -tulpn 2>/dev/null | head -25

echo
echo "===== 8. COTURN ALREADY PRESENT? ====="
(command -v turnserver >/dev/null && echo "  turnserver FOUND: $(command -v turnserver)") \
  || echo "  turnserver: not installed (expected)"

echo
echo "===== 9. NODE / PYTHON — needed for the signaling server ====="
echo "node   : $(command -v node >/dev/null && node --version || echo 'not installed')"
echo "python3: $(python3 --version 2>/dev/null || echo 'not installed')"

echo
echo "===== 10. HTTPS — Web Push REQUIRES a valid TLS cert on the PWA origin ====="
echo "certbot: $(command -v certbot >/dev/null && echo installed || echo 'not installed')"
ls -la /etc/letsencrypt/live/ 2>/dev/null || echo "  (no letsencrypt certs yet)"

echo
echo "===== 11. PUBLIC EGRESS (can it reach the internet?) ====="
curl -s -o /dev/null -w "  openrouter: %{http_code}\n" --max-time 10 https://openrouter.ai/api/v1/models 2>/dev/null || echo "  openrouter: unreachable"
curl -s -o /dev/null -w "  google:     %{http_code}\n" --max-time 10 https://generativelanguage.googleapis.com 2>/dev/null || echo "  google: unreachable"

echo
echo "===== 12. CONTAINER RUNTIMES (only informational) ====="
(command -v docker >/dev/null && docker --version) || echo "  docker: not installed"

echo
echo "===== PREFLIGHT COMPLETE ====="
echo "Paste ALL of the above output back to Helix."