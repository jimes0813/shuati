#!/bin/bash
# 启动本地判卷模型(llama.cpp, openclaw同款8080端口约定)
# Qwen3.6-35B-A3B MoE Q4: GB10上推理快, 占内存约22G
MODEL="$HOME/Qwen/Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf"
pgrep -f "llama-server.*8080" >/dev/null && { echo "已在运行: http://127.0.0.1:8080"; exit 0; }
nohup llama-server -m "$MODEL" --host 127.0.0.1 --port 8080 -ngl 99 -c 8192 \
  > /tmp/llama_judge.log 2>&1 &
echo "启动中(加载约1分钟), 日志: /tmp/llama_judge.log"
