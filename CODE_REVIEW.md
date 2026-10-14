# 代码复查记录 — 2026-10-14

复查基线：公开仓库 `a1f33c34feb8a7763f5661f8305a1d727e8c977b`。范围为所有本项目 Python、安装脚本、运行库下载/部署、测试及 CI；对照上游 v1.4.1 C++ 的像素协议与 HDR 参数，以及当前 ComfyUI VIDEO 和任务中断接口。此报告记录确认的问题及对应修复，不代表整个节点已无缺陷。

## 确认并修复的问题

| 优先级 | 触发条件与影响 | 修复和证据 |
| --- | --- | --- |
| P1 | 内存 VIDEO 标记为 HDR PQ/HLG，但 `auto` 只读取串流；可能误判 SDR，或为探测而编码整个输入 | 优先读取 `get_color_space()`；旧 API 才读取 PyAV；无法探测时报错并提示显式选择。测试确认内存 HDR 不调用串流生成，旧流的游标恢复到原位置 |
| P1 | 旧 ComfyUI 的 VIDEO 构造器不支持 HDR 参数，原代码吞掉 TypeError 后生成 SDR 标签 | 不再静默降级；GPU 处理前检查输出 API 支持。测试拒绝旧构造器，验证 PQ/HLG、10-bit、音频对象和分数帧率的传递 |
| P2 | 文件传输仍先建立整段 alpha/RGBA 副本，输出 RGB 又持有整段 RGBA 数据 | 输入逐帧使用 RGBA 缓冲，输出逐帧读入独立 RGB 数组；管道输入也改为逐帧转换。测试禁止整段 concatenate，检查每次只读取一帧及输出数组拥有独立内存 |
| P2 | `communicate()` 先缓存全部日志，之后截断；单行 stderr 也可无限长 | 图像、文件视频、探测同时排空 stdout/stderr，只保留有限尾部；管道 stderr 限制每次读取长度。真实子进程测试分别输出 5 MB 日志，确认仍正常完成且只返回限定尾部 |
| P2 | 点击取消后，阻塞在工作程序读写，通常只能等待超时；父进程已退出时清理不终止仍存活的同组子进程 | 工作程序监视 ComfyUI 中断状态，终止所拥有的进程组，并在执行线程抛出 ComfyUI 中断异常；关闭时也清理已退出父进程的同组后代。测试覆盖图像、管道、文件三条路径及延迟子进程 |
| P2 | float32、非 RGB、空尺寸、非法传递函数/尺寸仍可能进入原始像素协议，导致字节数错误或难懂的底层失败 | 启动前验证形状、dtype、SDR/HDR 配对、尺寸、光流引擎、白点和帧率；测试确认非法输入不启动工作程序 |
| P2 | 更新安装把手工设置的长视频超时重置为 600 秒；即使保存配置有有效超时，非法 shell 默认值也先触发错误 | 重新配置前验证并保留现有超时；先选用最终配置值再验证。首次安装仍为 600 秒；环境字段按部署方案生成并备份旧配置 |
| P2 | NVIDIA 归档解压中断后留有目标目录，归档的下一次解压可能拒绝已存在的目标 | 改在独立临时目录提取并检查 x64 NGX 桥，成功才移到最终位置；保留原残留目录为备份。测试确认失败不替换旧目录，重试成功且保留旧文件 |
| P2 | 安装器只检查驱动，随后却固定按 RTX 5090 名称过滤 GPU；其他型号可能完成安装却选不到显卡 | 在安装系统包前检查实际 GPU/驱动组合，明确限制自动部署为 RTX 5090 / 610.57.04；其他环境继续使用手动配置入口 |

P1 表示可能影响输出内容或色彩正确性；P2 表示任务运行、资源占用或部署可靠性问题。

## 验证方式与限制

- 本地 unittest 包括真实 Python 子进程和临时文件、回环 HTTP 下载/续传、元数据模拟接口。真实 Wine 测试在本地 macOS 跳过，由 Linux CI 单独执行。
- `test_real_wine.py` 编译并执行真实 Windows 小程序，检查 Wine cmd 文件重定向、带空格路径、二进制完整性、日志隔离、日志上限，以及取消后 Windows 工作程序不能继续创建延迟标记。它不调用 DLSS 或 NVIDIA GPU。
- Python 编译检查和 `bash -n setup_ubuntu24.sh` 检查语法。提取恢复测试使用小型模拟归档；不会运行 NVIDIA 安装器。
- 历史目标机器的单图与两帧 SDR / LK GPU PASS 对应旧实现提交 `dd7249d...`，记录于 [VALIDATION.md](VALIDATION.md)。此次修订需要在目标机器重新测试；不能把 CI 成功视为新版 GPU 实测。

## 尚未完成的实机验收

1. 当前完整 ComfyUI 用户工作流、HDR 保存/色彩和音轨编码；测试已验证 Python 元数据传递，未验证最终 HDR/音频文件。
2. 超分、NVOF/auto、长视频、不同显卡/驱动，以及 Windows GPU 回归。
3. 真实 GE-Proton / NGX 任务在取消或超时后的清理；CI 覆盖普通 Wine Windows 程序。自行脱离所属 Unix 进程组的子进程不能仅凭 `killpg` 保证清理。运行时不会调用影响共享客户端的 `wineserver -k`。
4. ComfyUI IMAGE/VIDEO API 本身要求整个帧批次在内存中；改为逐帧 RGBA 缓冲仍非端到端流式视频，输入张量、RGB 数组、浮点输出仍可能占用大量 RAM。文件传输还需要整段输入/输出的临时磁盘空间，日志文件也占用磁盘。
5. 上游 EXE/DLL 保持固定哈希且未修改；其模型、着色器、视觉质量和闭源 NVIDIA 运行库不在本次 Python 代码修复的验证范围内。

当前运行库获取有固定发布资产、ZIP 与文件 SHA-256 校验，路径与归档遍历拒绝、部署文件备份及原子替换测试。本次未发现需要替换当前 Linux 驱动的代码证据。

## API 对照来源

- [ComfyUI VIDEO 实现](https://github.com/comfyanonymous/ComfyUI/blob/master/comfy_api/latest/_input_impl/video_types.py)：`get_color_space()`、`VideoFromComponents` 的 HDR 参数。
- [ComfyUI 任务中断接口](https://github.com/comfyanonymous/ComfyUI/blob/master/comfy/model_management.py)：`processing_interrupted()`、`throw_exception_if_processing_interrupted()`、`InterruptProcessingException`。
- [video2dlssnr v1.4.1 源码](https://github.com/DaniilSokolyuk/video2dlssnr/tree/v1.4.1/src)：`--nr-video` 的 RGBA 8/16-bit 协议及 HDR 选择。HDR 路线由上游自动选择 16-bit，因此未将未显式传递 `--nr-pipe-bits` 误报为缺陷。

## 更新和重新验收

暂停 ComfyUI 当前生成任务，执行：

```bash
cd "$HOME/ComfyUI/custom_nodes/ComfyUI-DLSS-NeuralRender-Linux"
git status --short
git pull --ff-only
V2D_REVIEW_TEST_DIR="${V2D_STATE:-$HOME/.local/share/video2dlssnr-linux-610.57.04}/gpu-test-review-$(date +%Y%m%d-%H%M%S)"
python -u smoke_gpu.py --node "$PWD" --output "$V2D_REVIEW_TEST_DIR"
cat "$V2D_REVIEW_TEST_DIR/results.json"
```

本次 Python 更新不需要重下运行库或更换驱动。已配置机器可直接运行上述测试并重启 ComfyUI；只有需要重新部署配置时才运行 `bash setup_ubuntu24.sh install`。有本地修改导致 pull 停止时，先处理修改，不使用强制 reset。
