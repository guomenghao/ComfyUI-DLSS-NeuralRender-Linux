# 影栈（CineStack）DLSS · 运行维护指南

本指南说明节点的运行行为、资源要求与维护检查方法。功能与安装见 [README](README.md)，输出验收见 [功能验收指南](VALIDATION.md)。

## 运行与数据处理

- Linux 工作程序使用专用 Wine 配置，Windows 直接执行上游 EXE。
- 输入在启动前检查尺寸、数据类型、色彩格式、帧率及相关参数。
- Linux 可使用原始帧文件传输，隔离 Wine 日志与图像数据；返回字节数和退出状态必须满足要求。
- HDR 输入需要兼容的 ComfyUI `VIDEO` API；无法正确保留 HDR 元数据时会报错。
- 运行文件通过固定发布资产与 SHA-256 校验获取；更新配置会保留有效的任务超时设置。

## 资源规划

ComfyUI 的 `IMAGE` / `VIDEO` 接口与节点处理仍可能持有完整帧批次。逐帧转换缓冲并不等于端到端流式处理；输入张量、RGB 数组与浮点输出需要 RAM。

文件传输还需要输入与输出 RGBA 原始帧的临时磁盘空间。长视频应先使用代表性短片评估资源，再确定批次大小、临时目录和任务超时。运行结束后检查输出完整性。

## 超时与取消

`linux-runtime.json` 中的 `timeout_seconds` 优先于环境默认值。默认超时为 1800 秒，自动安装首次配置为 600 秒；重新配置会保留已有有效值。

取消或超时会终止本次工作程序所属进程组，并向 ComfyUI 返回相应异常。自行脱离该进程组的后代进程不属于这一清理保证的范围。不要使用影响所有 Python 或共享 Wine 客户端的批量终止命令。

## 手动启停

将 `start.sh` 与 `stop.sh` 一起放到包含 `main.py` 的 ComfyUI 安装目录。Linux 需要可用的 Xvfb / Xauth；启停时使用同一组设置。

```bash
COMFYUI_DIR="/path/to/ComfyUI"
export V2D_COMFY_PYTHON="/path/to/comfyui-python"
cp start.sh stop.sh "$COMFYUI_DIR/"
chmod +x "$COMFYUI_DIR/start.sh" "$COMFYUI_DIR/stop.sh"
"$COMFYUI_DIR/start.sh"
```

停止或重新启动：

```bash
"$COMFYUI_DIR/stop.sh"
"$COMFYUI_DIR/start.sh"
```

| 设置 | 用途 |
| --- | --- |
| `V2D_COMFY_PYTHON` | ComfyUI 使用的 Python 可执行文件绝对路径；通用部署应显式指定 |
| `V2D_COMFY_PORT` | 监听端口，默认 `8188` |
| `V2D_COMFY_STOP_TIMEOUT` | 停止等待秒数，默认 `20` |

脚本以 `--listen 0.0.0.0 --port 8188 --enable-manager` 的默认参数启动，端口可按表中设置调整。日志追加到安装目录的 `comfy.log`；启动信息仅确认进程已创建，服务就绪以日志与实际连接为准。

控制器检查脚本、工作目录、端口和进程启动身份，避免重复启动；停止仅处理匹配的 ComfyUI 实例。`.comfyui-control/` 保存每个端口的状态与锁。该工具依赖 Linux `/proc`。

## 更新与恢复

在节点目录检查本地修改并更新：

```bash
git status --short
git pull --ff-only
```

源码更新通常不需要重新下载运行库。运行库或 Wine 配置变更时，按 [Linux 指南](LINUX.md) 重新配置，再完成相应验收。不要用强制重置覆盖需要保留的本地修改。

回退节点时，先停止相关任务，将不再使用的节点目录移到 `custom_nodes` 之外，再恢复备份。备份目录放在 `custom_nodes` 内仍可能被加载，导致节点 ID 冲突。

## NVENC 与开机入口

需要容器 GPU 编码修复时，先按 [NVENC 指南](NVENC_CONTAINER.md) 通过实际编码测试，再部署持久化配置。开机入口必须由运行平台实际执行；仅创建脚本不代表自动启动已生效。

每次重新启动后，检查服务日志与实际 GPU 编码工作流。更换镜像或替换存储需要重新部署。

## 开发验证

在具有 NumPy、PyTorch、Pillow 的 Python 环境中执行：

```bash
python -m unittest discover -s . -p 'test_*.py' -v
bash -n setup_ubuntu24.sh start.sh stop.sh
```

回归覆盖数据传输、元数据、路径、超时、取消、日志边界、下载校验、安装恢复、进程身份及持久化配置。真实 Wine 协议测试需要 Wine / Xvfb / MinGW，并设置 `V2D_TEST_REAL_WINE=1`。

这些测试不执行 NVIDIA DLSS，也不评价上游模型的视觉质量。上游 EXE / DLL 保留原始二进制；GPU 与制作流程仍按 [验收指南](VALIDATION.md) 检查。
