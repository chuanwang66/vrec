#!/usr/bin/env bash
# One-command bootstrap + launch. First run sets everything up; later runs are fast.
#   ./start.sh                # cloud engine only
#   ./start.sh --with-local   # also install the offline (local) engine deps
set -euo pipefail
cd "$(dirname "$0")"

echo "==> vrec 准备中…"

# 1) ffmpeg (recording + audio processing)
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "==> 未发现 ffmpeg，正在安装…"
  if command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  else
    echo "!! 需要 ffmpeg。请先安装 Homebrew (https://brew.sh) 后重试，或手动安装 ffmpeg。"
    exit 1
  fi
fi

# 2) Python virtual environment
if [ ! -d .venv ]; then
  echo "==> 创建虚拟环境 .venv …"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 3) Python dependencies
echo "==> 安装依赖…"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if [ "${1:-}" = "--with-local" ]; then
  echo "==> 安装本地离线识别引擎依赖（较大，请耐心等待）…"
  python -m pip install --quiet -r requirements-local.txt
fi

# 4) First-run config from template (key stays empty; set it in the app)
mkdir -p "$HOME/.vrec"
if [ ! -f "$HOME/.vrec/config.json" ]; then
  cp config.example.json "$HOME/.vrec/config.json"
  chmod 600 "$HOME/.vrec/config.json"
  echo "==> 已生成配置 ~/.vrec/config.json（请在菜单栏「设置 → API Key」中填入你的 key）"
fi

# 5) Launch the menu-bar app, detached so you can close this window
echo "==> 启动菜单栏应用（请看屏幕右上角的 🎙 图标）"
nohup python menubar.py >>"$HOME/.vrec/app.log" 2>&1 &
sleep 1
echo "==> 已启动。可关闭此终端窗口。日志：~/.vrec/app.log"
