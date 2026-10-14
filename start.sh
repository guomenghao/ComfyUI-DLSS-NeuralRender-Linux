#!/usr/bin/env bash
# Install this file and stop.sh together in the ComfyUI root directory.
set -Eeuo pipefail
comfy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
comfy_python="${V2D_COMFY_PYTHON:-/root/miniconda3/bin/python}"
comfy_mode="${1:-start}"
[[ "$comfy_mode" == start || "$comfy_mode" == --stop ]] || {
    printf 'Usage: ./start.sh or ./stop.sh\n' >&2; exit 2;
}
[[ -f "$comfy_python" && -x "$comfy_python" ]] || {
    printf 'Python is unavailable: %s\n' "$comfy_python" >&2; exit 1;
}
if [[ "$comfy_python" == /root/miniconda3/bin/python && -f /root/miniconda3/bin/activate ]]; then
    set +u
    source /root/miniconda3/bin/activate base
    set -u
fi
exec "$comfy_python" - "$comfy_mode" "$comfy_dir" "$comfy_python" <<'PY'
# PYTHON CONTROL BEGIN
import fcntl
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PROC = Path("/proc")


def start_environment(directory):
    """Load persistent FFmpeg selection; LD_PRELOAD remains inside its wrapper."""
    env = os.environ.copy()
    config = directory / "comfyui-runtime.json"
    if not config.exists() and not config.is_symlink():
        return env
    if config.is_symlink() or not config.is_file():
        raise RuntimeError("comfyui-runtime.json 必须是普通文件")
    saved = json.loads(config.read_text())
    allowed = {"DLSS_FFMPEG_PATH", "DLSS_FFPROBE_PATH"}
    if (not isinstance(saved, dict) or set(saved) != {"version", "environment", "nvenc"}
            or saved["version"] != 1 or not isinstance(saved["environment"], dict)
            or set(saved["environment"]) != allowed):
        raise RuntimeError("comfyui-runtime.json 配置格式无效")
    for key, value in saved["environment"].items():
        if not isinstance(value, str):
            raise RuntimeError("FFmpeg 配置路径必须是字符串")
        env.setdefault(key, value)  # explicit per-launch overrides support rollback
        path = Path(env[key])
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"{key} 不可执行或缺失：{path}")
    if env["DLSS_FFMPEG_PATH"] == saved["environment"]["DLSS_FFMPEG_PATH"]:
        record = saved["nvenc"]
        if (not isinstance(record, dict) or set(record) != {
                "source_sha256", "library", "library_sha256", "wrapper_sha256"}):
            raise RuntimeError("NVENC 运行库记录无效")
        for path, expected in ((Path(record["library"]), record["library_sha256"]),
                               (Path(env["DLSS_FFMPEG_PATH"]), record["wrapper_sha256"])):
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                raise RuntimeError(f"NVENC 运行库缺失或已改变：{path}；请检查持久化磁盘")
    print("FFmpeg：" + env["DLSS_FFMPEG_PATH"], flush=True)
    return env


def process_info(pid):
    try:
        root = PROC / str(pid)
        args = [a.decode(errors="replace") for a in (root / "cmdline").read_bytes().split(b"\0") if a]
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if not args or fields[0] == "Z":
            return None
        return {"pid": pid, "args": args, "cwd": str(Path(os.readlink(root / "cwd")).resolve()),
                "start": fields[19], "group": int(fields[2])}
    except (OSError, ValueError, IndexError):
        return None


def worker_matches(info, directory, port):
    if not info or info["cwd"] != str(directory) or not Path(info["args"][0]).name.startswith("python"):
        return False
    args = info["args"]
    index = next((i for i, a in enumerate(args[1:], 1) if Path(a).name == "main.py"), None)
    if index is None or any(a not in {"-u", "-s", "-S", "-E", "-I", "-B", "-O", "-OO", "-q"}
                            for a in args[1:index]):
        return False
    if (directory / args[index]).resolve() != directory / "main.py":
        return False
    selected_port = 8188
    try:
        for i, a in enumerate(args[index + 1:], index + 1):
            if a == "--port":
                selected_port = int(args[i + 1])
            elif a.startswith("--port="):
                selected_port = int(a.split("=", 1)[1])
    except (ValueError, IndexError):
        return False
    return selected_port == port


def workers(directory, port):
    return [info for entry in PROC.iterdir() if entry.name.isdigit()
            for info in [process_info(int(entry.name))] if worker_matches(info, directory, port)]


def same_process(record):
    now = process_info(record["pid"])
    return now is not None and all(now[k] == record[k] for k in ("start", "cwd", "args", "group"))


def same_worker(record, directory, port):
    now = process_info(record["pid"])
    return (now is not None and now["start"] == record["start"]
            and worker_matches(now, directory, port))


def send_signal(record, sig, validator=None):
    # A pidfd binds the signal to the inspected process, even if its PID is reused.
    fd = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                fd = os.pidfd_open(record["pid"])
            except OSError as exc:
                if exc.errno not in (errno.ENOSYS, errno.EINVAL, errno.EPERM):
                    raise
        if not (validator() if validator is not None else same_process(record)):
            return
        if fd is not None:
            signal.pidfd_send_signal(fd, sig)
        else:
            os.kill(record["pid"], sig)
    except ProcessLookupError:
        pass
    finally:
        if fd is not None:
            os.close(fd)


def write_state(path, record):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(record, stream)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def launcher_record(state):
    try:
        record = json.loads(state.read_text())
        if isinstance(record, dict) and set(record) == {"pid", "args", "cwd", "start", "group"}:
            return record
    except (OSError, ValueError):
        pass
    return None


def launcher_matches(record, directory, port):
    if (not record or record["cwd"] != str(directory) or record["group"] != record["pid"]
            or not same_process(record)):
        return False
    args = record["args"]
    index = next((i for i, a in enumerate(args) if Path(a).name == "xvfb-run"), None)
    if index not in (0, 1) or (index == 1 and Path(args[0]).name not in {"sh", "bash", "dash"}):
        return False
    if len(args) <= index + 2 or args[index + 1] != "-a":
        return False
    return worker_matches({**record, "args": args[index + 2:]}, directory, port)


def owns_listener(record, port):
    """Do not send an interrupt to another service while ComfyUI is starting."""
    root = PROC / str(record["pid"])
    try:
        sockets = set()
        for fd in (root / "fd").iterdir():
            try:
                target = os.readlink(fd)
                if target.startswith("socket:["):
                    sockets.add(target[8:-1])
            except OSError:
                pass
        for table in ("tcp", "tcp6"):
            for line in (root / "net" / table).read_text().splitlines()[1:]:
                fields = line.split()
                if (len(fields) >= 10 and fields[3] == "0A" and fields[9] in sockets
                        and int(fields[1].rsplit(":", 1)[1], 16) == port):
                    return True
    except (OSError, ValueError):
        pass
    return False


def stop(directory, port, state, timeout):
    found = workers(directory, port)
    if len(found) > 1:
        raise RuntimeError("发现多个同目录、同端口 ComfyUI，请先检查：" + str([i["pid"] for i in found]))
    if found:
        record = found[0]
        print(f"停止 ComfyUI：PID={record['pid']}，端口={port}", flush=True)
        # Ignore shell proxy variables: the interrupt request is local only.
        request = urllib.request.Request(f"http://127.0.0.1:{port}/interrupt",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        if same_worker(record, directory, port) and owns_listener(record, port):
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=2) as response:
                    response.read(1024)
                time.sleep(0.5)  # allow worker cancellation monitors to clean up
            except OSError:
                pass
        alive = lambda: same_worker(record, directory, port)
        send_signal(record, signal.SIGTERM, alive)
        deadline = time.monotonic() + timeout
        while alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive():
            print("正常退出超时，终止已核对的 ComfyUI 进程。", flush=True)
            send_signal(record, signal.SIGKILL, alive)
            deadline = time.monotonic() + 5
            while alive() and time.monotonic() < deadline:
                time.sleep(0.1)
        if alive() or workers(directory, port):
            raise RuntimeError("进程仍未退出，请检查系统/GPU 状态")
    else:
        print("当前没有该目录、该端口的 ComfyUI 进程。", flush=True)
    # Only our saved, identity-checked session can be cleaned up as a group.
    # An externally started instance is never signalled as a whole group.
    launcher = launcher_record(state)
    if launcher_matches(launcher, directory, port):
        deadline = time.monotonic() + 3
        while launcher_matches(launcher, directory, port) and time.monotonic() < deadline:
            time.sleep(0.1)
        if launcher_matches(launcher, directory, port):
            try:
                os.killpg(launcher["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    state.unlink(missing_ok=True)
    print("ComfyUI 已停止。", flush=True)


def start(directory, python, port, state):
    found = workers(directory, port)
    if found:
        print("ComfyUI 已运行，不重复启动。PID：" + str([i["pid"] for i in found]))
        print("如需重新启动：./stop.sh && ./start.sh")
        return
    old = launcher_record(state)
    if launcher_matches(old, directory, port):
        raise RuntimeError("上次启动程序仍存活；先运行 ./stop.sh，再检查 comfy.log")
    if not (directory / "main.py").is_file():
        raise RuntimeError("请将 start.sh 和 stop.sh 放在包含 main.py 的 ComfyUI 根目录")
    env = start_environment(directory)
    xvfb = shutil.which("xvfb-run")
    if not xvfb or not shutil.which("xauth"):
        raise RuntimeError("缺少 xvfb-run 或 xauth；安装 xvfb 和 xauth 后重试")
    # Binding tests both IPv4 and IPv6 availability without reserving the port.
    # Refuse other services, including listeners not reachable on loopback.
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                probe.bind((address, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (97, 99):
                continue
            raise RuntimeError(f"端口 {port} 不可用，停止启动：{exc}") from exc
    command = [xvfb, "-a", python, "-u", "main.py", "--listen", "0.0.0.0",
               "--port", str(port), "--enable-manager"]
    with (directory / "comfy.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    launcher = process_info(process.pid)
    if launcher is not None:
        write_state(state, launcher)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            state.unlink(missing_ok=True)
            raise RuntimeError(f"启动程序退出，状态 {process.returncode}；查看 {directory / 'comfy.log'}")
        found = workers(directory, port)
        if found:
            # Save the launcher's stable argv after any interpreter/shebang exec.
            launcher = process_info(process.pid)
            if launcher is not None:
                write_state(state, launcher)
            print(f"ComfyUI 进程已启动：PID={found[0]['pid']}，端口={port}")
            print(f"日志：{directory / 'comfy.log'}；等待日志显示服务就绪后访问网页。")
            return
        time.sleep(0.1)
    raise RuntimeError("尚未发现 ComfyUI 进程；启动程序已保留，查看 comfy.log 或运行 ./stop.sh")


def main():
    if sys.platform != "linux":
        raise RuntimeError("这些控制脚本用于当前 Linux 服务器")
    mode, directory, python = sys.argv[1:]
    directory = Path(directory).resolve()
    port = int(os.environ.get("V2D_COMFY_PORT", "8188"))
    timeout = float(os.environ.get("V2D_COMFY_STOP_TIMEOUT", "20"))
    if not 1 <= port <= 65535 or not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("端口或停止超时无效")
    runtime = directory / ".comfyui-control"
    if runtime.is_symlink():
        raise RuntimeError("控制状态目录不能是符号链接")
    runtime.mkdir(mode=0o700, exist_ok=True)
    state = runtime / f"{port}.json"
    with (runtime / f"{port}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个启动/停止操作正在运行，请稍后重试") from exc
        if mode == "--stop":
            stop(directory, port, state, timeout)
        else:
            start(directory, python, port, state)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        print("失败：" + str(exc), file=sys.stderr)
        raise SystemExit(1)
# PYTHON CONTROL END
PY
