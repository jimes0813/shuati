#!/bin/bash
# 刷题营启动脚本: ./start.sh 后浏览器打开 http://127.0.0.1:8787
cd "$(dirname "$0")"
docker start searxng >/dev/null 2>&1; pgrep -f "server.py 8787" >/dev/null || nohup python3 server.py 8787 >/tmp/shuati_server.log 2>&1 &
sleep 1
xdg-open http://127.0.0.1:8787 2>/dev/null || echo "请打开 http://127.0.0.1:8787"
