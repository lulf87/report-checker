#!/bin/bash
# 报告核对工具：安装依赖并启动本机工作台（关闭本窗口即停止服务）
cd "$(dirname "$0")" || exit 1
PROJECT_DIR="$PWD"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir -p output/logs

APP_PORTS=(8765 8767)

listener_pids() {
  # 服务固定绑定 127.0.0.1；查询这个地址，避免误碰其他项目的 IPv6 通配监听。
  lsof -ti "TCP@127.0.0.1:$1" -sTCP:LISTEN 2>/dev/null || true
}

is_project_process() {
  local pid="$1"
  local command cwd
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$command" == *"$PROJECT_DIR"* ]]; then
    return 0
  fi
  cwd="$(lsof -p "$pid" -a -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
  [[ "$cwd" == "$PROJECT_DIR" || "$cwd" == "$PROJECT_DIR/"* ]]
}

stop_old_services() {
  local port pid command busy

  echo "== 1/3 停止旧的工作台进程（只停本项目占用的 8765/8767）=="
  for port in "${APP_PORTS[@]}"; do
    while read -r pid; do
      [ -z "$pid" ] && continue
      if is_project_process "$pid"; then
        command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
        kill "$pid" 2>/dev/null || true
        echo "已停止端口 $port 上的旧进程 $pid：${command:-未知进程}"
      else
        echo "端口 $port 被其他进程占用，保留 PID $pid；不会误杀其他项目。"
      fi
    done < <(listener_pids "$port")
  done

  # 给旧服务一个正常退出的机会；仍未释放的本项目进程才强制结束。
  for _ in $(seq 1 20); do
    busy=0
    for port in "${APP_PORTS[@]}"; do
      if [ -n "$(listener_pids "$port")" ]; then busy=1; break; fi
    done
    [ "$busy" -eq 0 ] && return 0
    sleep 0.25
  done

  for port in "${APP_PORTS[@]}"; do
    while read -r pid; do
      [ -z "$pid" ] && continue
      if is_project_process "$pid"; then
        kill -KILL "$pid" 2>/dev/null || true
        echo "旧进程 $pid 未及时退出，已强制停止。"
      fi
    done < <(listener_pids "$port")
  done

  for port in "${APP_PORTS[@]}"; do
    if [ -n "$(listener_pids "$port")" ]; then
      echo "端口 $port 仍被占用，未启动新服务。请先关闭占用它的其他程序。"
      return 1
    fi
  done
  return 0
}

stop_old_services || { read -r -p "按回车退出" _; exit 1; }

echo "== 2/3 安装依赖（uv sync，首次会下载 RapidOCR 等，约 1～3 分钟）=="
if ! command -v uv >/dev/null 2>&1; then
  echo "没有找到 uv，请先安装 uv：https://docs.astral.sh/uv/"
  read -r -p "按回车退出" _; exit 1
fi
uv sync || { echo "uv sync 失败，请把上面的报错发给 Claude"; read -r -p "按回车退出" _; exit 1; }
.venv/bin/python -c "import fitz, rapidocr_onnxruntime; print('依赖就绪：PyMuPDF', fitz.VersionBind, '+ RapidOCR')" || { read -r -p "依赖检查失败，按回车退出" _; exit 1; }

echo "== 3/3 启动服务 =="
.venv/bin/python -m mvp.run_state_server --host 127.0.0.1 --port 8767 --database output/run-state.sqlite3 > output/logs/run-state-server.log 2>&1 &
.venv/bin/python -m mvp.static_server --mode upload --bind 127.0.0.1 --port 8765 > output/logs/static-upload.log 2>&1 &
# 状态服务首次打开较大的 output/run-state.sqlite3 需要几十秒，最多等 60 秒
for port in "${APP_PORTS[@]}"; do
  for _ in $(seq 1 60); do [ -n "$(listener_pids "$port")" ] && break; sleep 1; done
  if [ -n "$(listener_pids "$port")" ]; then echo "端口 $port 已启动"; else echo "端口 $port 启动失败，日志见 output/logs/"; fi
done
open "http://127.0.0.1:8765/"
echo
echo "上传核对： http://127.0.0.1:8765/"
echo "以前核对过的结果：在页面里点开“最近核对”即可查看。"
echo "保持本窗口打开；关闭窗口或按 Ctrl+C 即停止服务。"
trap 'kill $(jobs -p) 2>/dev/null' EXIT
wait
