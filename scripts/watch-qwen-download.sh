#!/bin/bash
MODEL_DIR="/Users/sxliuyu/.cache/huggingface/hub/models--mlx-community--Qwen3.5-9B-MLX-4bit"
PID=60226
TURBOQUANT_REPO="https://github.com/rachittshah/mlx-turboquant"

check_and_install() {
    current=$(du -sm "$MODEL_DIR" 2>/dev/null | cut -f1)

    if ! ps -p $PID > /dev/null 2>&1; then
        echo "[$(date)] 进程已结束，当前大小: ${current}MB"
        sleep 3

        safetensors=$(ls "$MODEL_DIR"/*.safetensors 2>/dev/null | wc -l)
        echo "[$(date)] 发现 $safetensors 个 safetensors 文件"

        if [ "$safetensors" -ge 2 ]; then
            echo "[$(date)] 模型下载完成，开始安装 mlx-turboquant..."

            cd /Users/sxliuyu
            if [ ! -d "mlx-turboquant" ]; then
                git clone $TURBOQUANT_REPO
            fi
            cd mlx-turboquant && pip install -e .

            echo "[$(date)] 安装完成！" | tee /tmp/qwen-download-complete.log
            touch /tmp/qwen-download-complete
            exit 0
        fi
    else
        echo "[$(date '+%H:%M:%S')] 下载进行中... ${current}MB"
    fi
}

while true; do
    check_and_install
    [ -f /tmp/qwen-download-complete ] && break
    sleep 60
done