# ComfyUI-DLSS-NeuralRender-Linux

video2dlssnr v1.4.1 的实验性 Linux/Wine 适配节点，支持 DLSS Super Resolution + Neural Rendering 的图像及视频处理。Windows 仍直接调用上游 EXE。

**状态：RTX 5090 / Linux 610.57.04 已通过 NR feature 18 探测及单图输出；视频在旧编译器的光流阶段失败。新版编译器与文件传输修复仍待实机复测。GitHub CI 验证传输和部署逻辑，不验证 GPU。**

上游：[DaniilSokolyuk/video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr)。这是独立的社区适配项目，不属于 NVIDIA 官方项目。

## 功能和边界

- 保留上游三个节点及原有 ID：`DLSSNR_Image`、`DLSSNR_Video`、`DLSSNR_RuntimeInfo`。
- Linux 的工作程序通过 Wine 启动；ComfyUI、PyTorch 和视频编码仍原生运行。
- 按实际 Wine 盘符转换图像路径；SDR 8-bit 和 HDR 16-bit 支持二进制管道及 Linux 专用原始帧文件传输。当前自动部署使用文件方式隔离 Wine 日志与帧数据，Windows 继续使用管道。
- 无桌面环境可用 Xvfb 包装每次调用，明确筛选 NVIDIA GPU；只有工作子进程使用专用配置。
- 提供进程超时和独立进程组清理，修复上游 Runtime Check 的参数数量错误。
- 不增加视频帧数，不修改或伪造 NVIDIA 驱动版本，不修改上游 EXE/DLL。
- Git 仓库只保存源码。运行库按固定发布资产和 SHA-256 从上游下载。

## 当前自动安装目标

Ubuntu 24.04、Linux x86-64、Docker、RTX 5090、NVIDIA **610.57.04**。容器应已能通过 `vulkaninfo --summary` 识别真实 NVIDIA GPU，并有配套 `libnvidia-ngx.so`。

自动安装脚本暂时只接受此驱动版本与发行版，遇到其他环境会停止；其他版本需按 [Linux 手动配置说明](LINUX.md) 另行验证。无桌面、容器及 HDR 的真实生成效果仍未验证。

## 从 GitHub 安装到当前机器

在**实际运行 ComfyUI 的 Conda/venv 环境**中执行。系统依赖安装需要 root；下面适用于当前 `/root/ComfyUI`。

### 1. 获取源码，处理原版节点冲突

先在平台停止 ComfyUI 的当前生成任务。原版 `video2dlssnr` 与本项目使用相同节点 ID，只保留其中一个可加载目录。

```bash
apt-get update && apt-get install -y --no-install-recommends git
mkdir -p "$HOME/ComfyUI/node_backups"
if [ -d "$HOME/ComfyUI/custom_nodes/video2dlssnr" ]; then
  mv "$HOME/ComfyUI/custom_nodes/video2dlssnr" \
    "$HOME/ComfyUI/node_backups/video2dlssnr-$(date +%Y%m%d-%H%M%S)"
fi
cd "$HOME/ComfyUI/custom_nodes"
git clone https://github.com/guomenghao/ComfyUI-DLSS-NeuralRender-Linux.git
cd ComfyUI-DLSS-NeuralRender-Linux
```

若新目录已经存在，不重复 clone；先检查它的 `git remote -v` 和本地修改。

### 2. 安装运行库、Wine 和本节点配置

```bash
bash setup_ubuntu24.sh install
```

脚本会：

1. 安装 Ubuntu 用户态依赖、Wine、32 位 glibc 加载器（`libc6-i386`）、Xvfb/Xauth、Vulkan/CAB 工具和 FFmpeg。
2. 优先校验当前节点和 `~/ComfyUI/node_backups/video2dlssnr-*/bin/` 中的原版运行库。能凑齐六个匹配的二进制时直接复用，无需下载 ZIP；否则下载固定的 v1.4.1 ComfyUI 发布包，验证整个 ZIP 和六个二进制哈希，仅提取 `bin/`，不覆盖本项目 Python。
3. 下载并校验 GE-Proton11-6 x86_64，使用其配套 Wine、DXVK、VKD3D-Proton、DXVK-NVAPI。
4. 校验 NVIDIA 610.57.04 `.run` 包，仅 `--extract-only` 解压匹配的 `_nvngx.dll`，不运行 `nvidia-installer`，不安装 NVIDIA apt 包或替换宿主机驱动。
5. 从微软 SDK CAB 获取并校验原生 x64 shader compiler **10.0.28000.2705**，建立受本项目标记的专用 prefix。旧版 6.3.9600 编译器不适用于本节点的多分量 UAV 光流着色器。
6. 备份已有本机配置及驱动桥，写入 `linux-runtime.json`，每次 Wine 调用使用 Xvfb，GPU 选择 RTX 5090。

需要下载超过 1 GB 的运行文件及依赖，安装前预留至少 8 GiB 空间。所有配置与下载状态默认保存在：

```text
~/.local/share/video2dlssnr-linux-610.57.04/
```

可设置 `V2D_STATE` 指向空间足够的持久化目录；`V2D_PYTHON` 指定实际 ComfyUI Python。脚本不升级或重装 PyTorch。

安装显示 `INSTALL COMPLETE` 才进入测试；出现 `FAILED` 则先处理对应日志。重跑会复用校验通过的下载及本项目管理的 prefix，不重新初始化已有图形 DLL。

### GE-Proton 下载慢或中断

GE-Proton11-6 压缩包约 509 MiB。安装脚本支持 `.part` 断点续传，连接失败会从最新下载位置重试；不再因一小时总超时丢失进度。GE-Proton、NVIDIA 驱动归档和微软 CAB 均使用此机制，完成后仍验证固定 SHA-256。

正在运行旧脚本时，先按 **Ctrl+C**，保留状态目录中的 `.part` 文件，然后更新源码、改用 SourceForge 镜像续传：

```bash
cd "$HOME/ComfyUI/custom_nodes/ComfyUI-DLSS-NeuralRender-Linux"
git pull --ff-only && \
V2D_PROTON_URL='https://downloads.sourceforge.net/project/proton-ge-custom.mirror/GE-Proton11-6/GE-Proton11-6-x86_64.tar.gz' \
bash setup_ubuntu24.sh install
```

[SourceForge 镜像页面](https://sourceforge.net/projects/proton-ge-custom.mirror/files/GE-Proton11-6/GE-Proton11-6-x86_64.tar.gz/)列出的 SHA-256 与固定上游文件一致；镜像速度取决于服务器网络。不设置 `V2D_PROTON_URL` 时仍从官方 GitHub Release 获取。切换来源不会改变运行库版本或校验值。若来源不支持续传，脚本停止并保留部分文件，不自动从头覆盖。

若在其他机器下载后上传，将完整文件放到 `$V2D_STATE/downloads/GE-Proton11-6-x86_64.tar.gz`（未设置 `V2D_STATE` 时为 `~/.local/share/video2dlssnr-linux-610.57.04/downloads/`）；安装时哈希校验通过会直接复用。

### 上游 ZIP 下载慢或没有进度

新版 `fetch_runtime.py` 使用 curl 显示进度并续传上游 ZIP，自动保留旧版的 `video2dlssnr-comfyui-v1.4.1.part` 进度。旧脚本运行中请先按 Ctrl+C，再 `git pull --ff-only` 后重跑安装。它会先检查原版节点备份；所有必需文件的哈希匹配时跳过 ZIP 下载。已有文件与固定版本不符会报出具体文件名并停止，不覆盖。

原版备份放在其他位置时，`V2D_RUNTIME_DIR` 指定其 **bin 目录**：

```bash
V2D_RUNTIME_DIR='/actual/backup/video2dlssnr/bin' bash setup_ubuntu24.sh install
```

也可以在电脑浏览器从[上游 v1.4.1 Release](https://github.com/DaniilSokolyuk/video2dlssnr/releases/tag/v1.4.1)下载 `video2dlssnr-comfyui.zip`，上传服务器后指定：

```bash
V2D_RUNTIME_ARCHIVE='/root/downloads/video2dlssnr-comfyui.zip' bash setup_ubuntu24.sh install
```

该 ZIP 固定大小为 247413258 字节，SHA-256 为 `4eda7e13d1d6bac229309297e33f51d1210987f86249b75dd03530a86302798b`。`V2D_RUNTIME_URL` 可以指定同一 ZIP 的其他下载地址，仍必须通过相同校验。安装仍需 GE-Proton、NVIDIA 归档和微软 CAB；如需切换 GE-Proton 来源，继续保留前述 `V2D_PROTON_URL`。

### 3. 分层测试实际 NR

```bash
bash setup_ubuntu24.sh test
```

依次运行 EXE 帮助页、上游生产路线 NR feature 18 探测、真实图像及两帧 SDR 视频处理；任一阶段失败即停止。不会启用上游诊断性的 `--probe-core`。

第三阶段使用 1280×720、2 帧、2 FPS 的合成无音轨样本，`scale=1`、`motion_engine=lk`；保存 PNG、MP4、工作日志和 JSON，并检查可解码视频的尺寸、帧数、帧率。它不替代真实视频的音轨、HDR、超分和视觉质量验收。

```bash
tail -n 100 "$HOME/.local/share/video2dlssnr-linux-610.57.04/install.log"
tail -n 100 "$HOME/.local/share/video2dlssnr-linux-610.57.04/test.log"
cat "$HOME/.local/share/video2dlssnr-linux-610.57.04/gpu-test/results.json"
```

使用自定义 `V2D_STATE` 时，相应替换日志目录。

### 视频退出失败但 stderr 日志为空

Wine 崩溃报告可能写入本应传输原始帧的 stdout。新版节点在任务失败时将有限的 stdout 预览附入异常和 `video-worker.log`，文本保留开头/结尾，二进制只显示前 64 字节的十六进制；不会把错误文本当作图片，也不会在成功时记录帧数据。

如果 NR 探测和单图已通过，只重跑短视频即可。新目录保留此前结果：

```bash
V2D_VIDEO_TEST_DIR="${V2D_STATE:-$HOME/.local/share/video2dlssnr-linux-610.57.04}/gpu-test-video-$(date +%Y%m%d-%H%M%S)"
python -u smoke_gpu.py --node "$PWD" --output "$V2D_VIDEO_TEST_DIR" --video-only
cat "$V2D_VIDEO_TEST_DIR/video-worker.log"
```

这是获取失败证据的诊断增强，不代表视频故障已修复。`--video-only` 的报告标记测试范围，不重复单图和 NR 探测。

### 光流编译 `X3676` 与 stdout 日志混入

RTX 5090 / 610.57.04 的实机日志已证明 feature 18 的创建、求值及单图输出成功；短视频随后在 `optflow up` 着色器报 `typed UAV loads are only allowed for single-component 32-bit element types`。安装器原先采用的 Windows 8.1 SDK 编译器为 6.3.9600.16384，现改为固定的微软 SDK **10.0.28000.2705**：CAB 9495635 字节，SHA-256 `d951f1eefc051643ed28a0ba8bee804097bb9d6e7ee309d5c1be04f8272c0486`；x64 DLL SHA-256 `3e56c1a0867aa04cbca948b3664fcac5d51275865cd044a45a3fd6ce636ee914`。旧下载保留在原路径，替换前备份 prefix 中的编译器。

同一日志还证明 Wine/程序诊断进入了 stdout 原始帧通道。新版自动部署在 `linux-runtime.json` 设置 `VIDEO2DLSSNR_VIDEO_TRANSPORT=files`，使用 Wine 内的 cmd 批处理将 stdin、stdout 和 stderr 分别绑定到本次任务的独立文件。整个视频仍由上游 `--nr-video` 一次处理，保留时序处理；不逐帧改用单图，不过滤混合流中的“疑似日志”。完成后检查退出状态与输出字节数，再读取原始帧。Wine 启动器日志单独捕获。

文件方式需要临时磁盘容纳输入和输出 RGBA 帧，结束后自动清理本次临时目录；进度目前在完成时更新。支持普通空格、UTF-8 路径以及参数内的百分号、`&` 和 `!`；Windows 参数不能含双引号或换行，批处理自身所在临时目录还不能含 `%` 或 `!`，必要时设置 `TMPDIR=/tmp`。未配置该选项的 Linux 环境仍默认 `pipe`。新版文件方式及编译器更新通过本地模拟传输验证，**修复后的真实 GPU 视频仍待实机复测**。

更新后运行 `bash setup_ubuntu24.sh install` 获取约 9.1 MiB 的新 CAB、更新专用配置，再按上一节 `--video-only` 重跑。既有 GE-Proton 和 NVIDIA 大文件校验通过后复用。

依据：[Winetricks 编译器更新记录](https://github.com/Winetricks/winetricks/issues/2522)、[微软 typed UAV loads 文档](https://learn.microsoft.com/en-us/windows/win32/direct3d12/typed-unordered-access-view-loads)。本仓库采用官方微软下载地址，并独立核验了 CAB、DLL 哈希与 x64 文件版本。

### 初始化报 `/lib/ld-linux.so.2: could not open`

这是当前直接调用 GE-Proton Wine 的环境缺少 32 位 glibc 加载器，发生在 prefix 初始化，尚未运行 DLSS。Ubuntu 24.04 amd64 可用 `apt-get install -y --no-install-recommends libc6-i386` 补齐；无需为这个加载器开启整个 i386 软件源。新版安装器已包含该依赖并在初始化前检查 `/lib/ld-linux.so.2`。

更新源码后重跑 `bash setup_ubuntu24.sh install`，复用已校验的下载与本项目标记的 prefix，不删除运行库或 prefix。若仍失败，保留新日志；补齐加载器只解决此处缺失，其他 Wine 共享库和 GPU 功能仍需验收。

微软 CAB 若提示 `possible ... extra bytes at end of file`，同时显示完整包 SHA-256 校验通过及 `All done, no errors`，该提示不是此处 Wine 初始化失败的原因。

参考：[Ubuntu libc6-i386](https://packages.ubuntu.com/noble/libc6-i386)、[包内加载器文件](https://packages.ubuntu.com/noble/amd64/libc6-i386/filelist)。当前方案直接调用 GE-Proton 的 Wine；[上游推荐通过 Steam/umu 的配套运行时启动](https://github.com/GloriousEggroll/proton-ge-custom/blob/GE-Proton11-6/README.md)，因此直接调用时需在宿主容器补齐系统共享库，仍属于实验性适配。

### 4. 重启 ComfyUI 并验证工作流

按平台原有方式重启 ComfyUI，搜索 `video2dlssnr` 分类：

```text
Load Image → DLSS Neural Rendering (Image) → Save Image
Load Video → DLSS Neural Rendering (Video) → Save Video
DLSS NR Runtime Check → STRING
```

先验证短 SDR 视频，再测试 `scale=1.5/2`、`auto/nvof` 和 HDR。检查实际分辨率、帧数、帧率、音轨及视觉效果。该节点不执行插帧。

## 更新与回滚

```bash
git status --short
git pull --ff-only
bash setup_ubuntu24.sh install
bash setup_ubuntu24.sh test
```

只有源码更新、配置变更或新失败需要重复测试。更新后按平台方式重启 ComfyUI。

恢复原版时，停止相关任务，将本项目整个目录移到 `custom_nodes` 外，再把 `node_backups` 中原版目录移回。不要在 `custom_nodes` 内给旧版改名作为备份，否则仍可能加载重复节点。

## Windows

使用现有 ComfyUI Python 执行 `python fetch_runtime.py` 下载校验后的上游运行库（需要 PATH 中的 curl），或用 `python fetch_runtime.py --archive C:/downloads/video2dlssnr-comfyui.zip` 安装已经下载的完整包，然后重启 ComfyUI。此时直接执行 EXE；不读取 Linux 配置，不启动 Wine。上游要求 NVIDIA RTX 与 Windows 驱动 616.56+；本项目未进行 Windows GPU 回归测试。

## 本地/CI 测试

在已有 NumPy、PyTorch、Pillow 的环境中执行：

```bash
python -m unittest discover -s . -p 'test_*.py' -v
bash -n setup_ubuntu24.sh
```

测试使用模拟 Wine 工作程序验证实际文件和子进程管道，包括 8-bit/16-bit、Xvfb 包装、错误输出、超时和配置隔离；GPU 冒烟测试单独由 `smoke_gpu.py` 执行。

## 兼容性限制与许可

上游 C++ 用 DXGI UMD 版本检查 Windows 616.56 门槛；Wine 的报告值不一定是真实 Linux 驱动版本。通过版本检查、帮助页或 CI 均不证明 NR 可以执行，需以真实 feature-18 和图像/视频结果为准。

代码按 [MIT](LICENSE) 许可，保留上游作者声明；NVIDIA 和其他运行库按其自身条款使用，不随 Git 仓库提交。

参考：[NVIDIA 解压选项](https://download.nvidia.com/XFree86/Linux-x86_64/610.57.04/README/installdriver.html)、[NGX Wine 组件](https://download.nvidia.com/XFree86/Linux-x86_64/610.57.04/README/installedcomponents.html)、[GE-Proton11-6](https://github.com/GloriousEggroll/proton-ge-custom/releases/tag/GE-Proton11-6)、[环境配置参考](https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation/blob/main/docs/linux.md)。环境参考项目的成功不代表本项目已通过 GPU 验证。
