#!/bin/bash
# 一键检查机器人各环节状态
DOCKER_BIN="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.qqbot_onebot_token}"

echo "===== 容器 ====="
"$DOCKER_BIN" ps --filter name=snowluma --format '{{.Names}}\t{{.Status}}' 2>/dev/null || echo "docker 未运行"

echo "===== QQ 登录 ====="
if [ -f "$TOKEN_FILE" ]; then
  TOKEN=$(cat "$TOKEN_FILE")
  curl -s -m 8 -H "Authorization: Bearer $TOKEN" http://127.0.0.1:3000/get_login_info || echo "OneBot 无响应"
  echo
  curl -s -m 8 -H "Authorization: Bearer $TOKEN" http://127.0.0.1:3000/get_status || true
  echo
else
  echo "未找到 $TOKEN_FILE（写入 OneBot HTTP AccessToken 后可用）"
fi

echo "===== 端口 ====="
for p in 6081 5099 3000 3001 6199 11434; do
  lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1 && echo "  $p  监听中" || echo "  $p  未监听"
done

echo "===== Ollama ====="
curl -s -m 5 http://127.0.0.1:11434/api/version || echo "Ollama 未运行"; echo

echo "===== 最近收发（今天）====="
L=$("$DOCKER_BIN" exec snowluma sh -c "ls -t /app/data/logs/*.log | head -1" 2>/dev/null | tr -d '\r')
[ -n "$L" ] && "$DOCKER_BIN" exec snowluma sh -c "grep -E '\[Event\] (私聊|群)|\| 发送：' $L | tail -5"
