#!/bin/bash
# 拉起 SnowLuma 协议端容器（QQ + Xvfb + noVNC + OneBot）
set -e
DOCKER_BIN="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"

echo "==> 确保 Docker Desktop 已启动"
open -a Docker 2>/dev/null || true
for i in $(seq 1 60); do "$DOCKER_BIN" info >/dev/null 2>&1 && break; sleep 2; done

if "$DOCKER_BIN" ps -a --format '{{.Names}}' | grep -qx snowluma; then
  echo "==> 容器已存在，直接启动"
  "$DOCKER_BIN" start snowluma
else
  echo "==> 创建并启动容器"
  "$DOCKER_BIN" run -d --name snowluma --restart unless-stopped \
    --shm-size=1g --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
    -p 6081:6081 -p 5099:5099 -p 3000:3000 -p 3001:3001 \
    -v qq-gateway-data:/app/data \
    -v qq-client-config:/app/.config \
    -v qq-client-data:/app/.local/share \
    motricseven7/snowluma:latest
fi

echo "==> 等待日志..."
sleep 20
"$DOCKER_BIN" logs snowluma 2>&1 | grep -E "远程桌面密码|remote desktop password|initial credentials" | tail -4
echo
echo "noVNC : http://localhost:6081   （用手机 QQ 扫码登录）"
echo "WebUI : http://localhost:5099   （账号 admin）"
