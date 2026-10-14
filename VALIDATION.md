# Linux/Wine 实机验证记录

日期：2026-10-14。证据来自用户提供的目标机器终端日志与 `smoke_gpu.py` 输出，维护者未远程登录该机器，也未直接查看其生成视频的视觉效果。此记录不包含主机标识或完整私人日志。

## 环境

| 项目 | 已报告配置 |
| --- | --- |
| 系统 | Ubuntu 24.04.1 LTS，x86-64，Docker，无桌面 |
| GPU | NVIDIA GeForce RTX 5090，32607 MiB |
| 实际 Linux 驱动 | 610.57.04 |
| Wine / 图形库 | GE-Proton11-6，DXVK 3.10，VKD3D-Proton 3.1.0 |
| 上游运行库 | video2dlssnr v1.4.1；DLSS/NR DLL 310.8.0.0，原版二进制未修改 |
| Shader compiler | 微软 x64 d3dcompiler_47.dll 10.0.28000.2705 |
| 视频传输 | 独立 raw RGBA 文件，通过 Wine cmd 重定向；日志单独读取 |
| 视频测试源码提交 | `dd7249d95f126492efb9641c8fffeb1581750ebf` |

Wine/DXGI 在 Windows API 中报告的 `999.99 / UMD 35.0.19.9999` 不是本机实际 Linux 驱动版本；上表采用 `nvidia-smi` 的版本。

## 已通过的检查

1. 生产 NR feature 18 探测：route F forwarder，`Init_Ext`、`CreateFeature 18`、`EvaluateFeature 18` 返回成功，worker 正常退出。
2. 单图阶段：CLI 返回可解码的 1280×720 RGB PNG，dtype 与尺寸检查通过。它在此前测试运行中完成；随后修复编译器及视频调用并单独复测视频。
3. 视频阶段：新版编译器与批处理路径修复后，上游 `--nr-video` 返回完整帧数据，保存 PNG 与 MP4；FFprobe 验证分辨率、帧数、帧率。

视频报告中的关键字段：

```json
{
  "status": "PASS",
  "kind": "real target Wine/NVIDIA NR test",
  "scale": 1,
  "motion_engine": "lk",
  "scope": "video-only",
  "audio": "synthetic input has no audio; not an audio preservation test",
  "video": {
    "width": 1280,
    "height": 720,
    "r_frame_rate": "2/1",
    "nb_read_frames": "2"
  }
}
```

测试使用合成画面，两帧之间有横向位移。GPU 日志报告 `optical flow: Lucas-Kanade (GPU compute)`。运行中仍有无显示器 EDID、OpenXR 及 Vulkan fixme 提示，但这次任务最终完成并通过输出结构检查。

此外，[GitHub CI 的真实 Wine 测试](https://github.com/guomenghao/ComfyUI-DLSS-NeuralRender-Linux/actions/runs/38508006233)使用 MinGW 编译的小型 Windows 程序，验证实际 Wine cmd、带空格路径、二进制字节完整性及 stderr 隔离。CI 不调用 NVIDIA GPU 或 DLSS；该结果与目标机器 GPU 测试分别记录。

## 尚未验收

- ComfyUI 内实际用户工作流和真实素材的视觉质量。
- scale=1.5/2 等放大模式、其他分辨率及长视频。
- NVOF/auto 后端、HDR PQ/HLG、音轨保留。
- 其他显卡、驱动、发行版与 Windows GPU 路线。

短样本成功证明这套环境能够完成 NR 图像及基础 SDR 视频处理，不构成对全部参数、时长、质量或其他硬件的保证。该节点保持原有帧数，不执行插帧。
