# ComfyUI-DLSS-NeuralRender-Linux

video2dlssnr v1.4.1 的实验性 Linux/Wine 适配节点，支持 DLSS Super Resolution + Neural Rendering 的图像及视频处理。Windows 仍直接调用上游 EXE。

**状态：本地模拟传输与部署检查通过，真实 Linux/Wine/NVIDIA NR 尚待目标机器验收。GitHub CI 只验证 Python、文件和管道逻辑，不验证 GPU。**

上游：[DaniilSokolyuk/video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr)。这是独立的社区适配项目，不属于 NVIDIA 官方项目。

## 功能和边界

- 保留上游三个节点及原有 ID：`DLSSNR_Image`、`DLSSNR_Video`、`DLSSNR_RuntimeInfo`。
- Linux 的工作程序通过 Wine 启动；ComfyUI、PyTorch 和视频编码仍原生运行。
- 按实际 Wine 盘符转换图像路径；SDR 8-bit 和 HDR 16-bit 帧通过独立二进制管道传输，诊断日志走 stderr。
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

1. 安装 Ubuntu 用户态依赖、Wine、Xvfb/Xauth、Vulkan/CAB 工具和 FFmpeg。
2. 下载固定的 v1.4.1 ComfyUI 发布包，验证整个 ZIP 和六个二进制哈希，仅提取 `bin/`，不覆盖本项目 Python。
3. 下载并校验 GE-Proton11-6 x86_64，使用其配套 Wine、DXVK、VKD3D-Proton、DXVK-NVAPI。
4. 校验 NVIDIA 610.57.04 `.run` 包，仅 `--extract-only` 解压匹配的 `_nvngx.dll`，不运行 `nvidia-installer`，不安装 NVIDIA apt 包或替换宿主机驱动。
5. 从微软 SDK CAB 获取并校验原生 x64 shader compiler，建立受本项目标记的专用 prefix。
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

使用现有 ComfyUI Python 执行 `python fetch_runtime.py` 下载校验后的上游运行库，然后重启 ComfyUI。此时直接执行 EXE；不读取 Linux 配置，不启动 Wine。上游要求 NVIDIA RTX 与 Windows 驱动 616.56+；本项目未进行 Windows GPU 回归测试。

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
