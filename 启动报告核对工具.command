#!/bin/bash
# 报告核对工具：安装依赖并启动本机工作台（关闭本窗口即停止服务）
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir -p output/logs

echo "== 1/3 安装依赖（uv sync，首次会下载 RapidOCR 等，约 1～3 分钟）=="
if ! command -v uv >/dev/null 2>&1; then
  echo "没有找到 uv，请先安装 uv：https://docs.astral.sh/uv/"
  read -r -p "按回车退出" _; exit 1
fi
uv sync || { echo "uv sync 失败，请把上面的报错发给 Claude"; read -r -p "按回车退出" _; exit 1; }
.venv/bin/python -c "import fitz, rapidocr_onnxruntime; print('依赖就绪：PyMuPDF', fitz.VersionBind, '+ RapidOCR')" || { read -r -p "依赖检查失败，按回车退出" _; exit 1; }

echo "== 2/3 停止旧的工作台进程（只停本工具的 mvp 服务）=="
for port in 8765 8766 8767; do
  for pid in $(lsof -ti tcp:$port 2>/dev/null); do
    if ps -p "$pid" -o command= | grep -q "mvp\."; then kill "$pid" && echo "已停止端口 $port 上的旧进程 $pid"; fi
  done
done
sleep 1

echo "== 3/3 启动服务 =="
.venv/bin/python -m mvp.run_state_server --host 127.0.0.1 --port 8767 --database output/run-state.sqlite3 > output/logs/run-state-server.log 2>&1 &
.venv/bin/python -m mvp.static_server --mode upload --bind 127.0.0.1 --port 8765 > output/logs/static-upload.log 2>&1 &
.venv/bin/python -m mvp.static_server --mode real --bind 127.0.0.1 --port 8766 > output/logs/static-real.log 2>&1 &
# 状态服务首次打开较大的 output/run-state.sqlite3 需要几十秒，最多等 60 秒
for port in 8765 8766 8767; do
  for _ in $(seq 1 60); do lsof -ti tcp:$port >/dev/null 2>&1 && break; sleep 1; done
  if lsof -ti tcp:$port >/dev/null 2>&1; then echo "端口 $port 已启动"; else echo "端口 $port 启动失败，日志见 output/logs/"; fi
done
open "http://127.0.0.1:8765/"
open "http://127.0.0.1:8766/workbench-real.html"
echo
echo "上传核对： http://127.0.0.1:8765/"
echo "已有结果： http://127.0.0.1:8766/workbench-real.html"
echo "保持本窗口打开；关闭窗口或按 Ctrl+C 即停止服务。"
trap 'kill $(jobs -p) 2>/dev/null' EXIT
wait
