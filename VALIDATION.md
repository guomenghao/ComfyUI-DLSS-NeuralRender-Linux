# 影栈（CineStack）DLSS · 功能验收指南

本指南用于检查节点安装、GPU 调用与实际输出。建议在首次部署、运行库变更或迁移硬件后执行；完整工作流应另用实际制作素材验收。

## 验收层级

| 层级 | 检查内容 | 可以确认的范围 |
| --- | --- | --- |
| 配置检查 | 路径、Wine prefix、运行库文件 | 文件与配置可用 |
| 启动检查 | 工作程序帮助页 | Wine 能启动 Windows 程序 |
| NR 探测 | 创建与求值 NGX feature 18 | 基础 NR 调用可执行 |
| 图像 / 视频测试 | 实际处理并检查生成文件 | 所测尺寸与参数下的输出完整性 |
| 制作工作流 | 真实素材、保存节点、编码及回放 | 实际交付流程的可用性 |

## 自动安装配置验收

在节点目录及 ComfyUI Python 环境中执行：

```bash
bash setup_ubuntu24.sh test
```

该入口依次检查帮助页、生产 NR 路线和实际图像 / 视频输出；任一阶段失败即停止。其适用范围与自动安装配置一致，见 [Linux 指南](LINUX.md)。

## 手动配置验收

```bash
python check_runtime.py
python check_runtime.py --help-worker
python check_runtime.py --probe-nr --adapter 0
```

前三项通过后，在节点目录运行输出测试；选择空的验收目录：

```bash
V2D_ACCEPTANCE_DIR="/path/to/acceptance-output"
python -u smoke_gpu.py --node "$PWD" --output "$V2D_ACCEPTANCE_DIR"
cat "$V2D_ACCEPTANCE_DIR/results.json"
```

仅复测视频时可追加 `--video-only`。不要将单阶段测试结果视为完整验收。

## 合成样本测试范围

`smoke_gpu.py` 默认使用原生分辨率、1280×720 的图像与两帧 SDR 视频样本，视频为 2 FPS，`scale=1`、`motion_engine=lk`。样本不含音轨。

通过标准包括工作程序正常退出、返回帧数据完整、输出可解码，以及视频尺寸、帧数和帧率与预期一致。检查 JSON 中的 `status` 与测试范围，并查看生成图像与视频。

合成短样本不覆盖超分、音轨保留、HDR、长视频性能或视觉质量。可选 NVENC 工具的编码测试也不替代完整 ComfyUI 工作流验收。

## ComfyUI 工作流验收

1. 使用 `Load Image → DLSS Neural Rendering (Image) → Save Image` 检查图像处理，从 `scale=1` 开始。
2. 使用 `Load Video → DLSS Neural Rendering (Video) → Save Video` 检查短 SDR 视频，从 `motion_engine=lk` 开始。
3. 对制作中需要的缩放、光流引擎、音频和 HDR 参数逐项测试，避免一次变更多个设置。
4. 检查保存文件的分辨率、帧数、帧率、色彩标记、音轨、完整解码和实际回放效果。
5. 使用代表性时长的素材评估 RAM、显存、临时磁盘占用与处理时间。

使用 `IMAGE` 帧批次时需明确 `images_fps`，该输入自身不带音轨。Neural Rendering 节点保持帧数；插帧需使用独立 Frame Generation 节点。

## 兼容性状态

| 功能 | 验证范围 |
| --- | --- |
| Linux NR 单图 | 已有原生分辨率输出验证；部署后按需复测 |
| Linux SDR 视频 / LK | 已有短样本验证；不代表任意时长与尺寸 |
| 可选 H.264 NVENC 工具 | 已有短编码与解码验证；需在部署环境复测 |
| 超分、HDR、NVOF / auto、音轨与长视频 | 需独立验收 |
| Windows、其他硬件 / 驱动组合 | 需独立验收 |
| 完整 ComfyUI DLSS + GPU 编码链路 | 按实际工作流验收 |

CI 检查传输协议、文件、进程与配置行为，不调用 NVIDIA DLSS。测试通过与画面审美评价应分别记录。

## 验收资料

保留所用源码版本、参数、输入素材、输出文件及测试报告，便于复现。对外提交问题时只提供必要信息；不要公开账号、凭据、主机标识或包含私人路径的完整环境与日志。
