# DLSS 0.2.0 内存与视频交接

VIDEO-only 图自动使用文件输出，连接 IMAGE 后 auto 会重新计算模式（不会复用此前的 IMAGE blocker）。缺少图元信息的 API 调用保留 IMAGE 行为。输出名称、顺序、旧参数、节点 ID 均不变；新增参数位于末尾。

输入是 VideoFromComponents 时只引用已有画面，逐帧量化；输入是文件 VIDEO 时使用官方 save_to 处理裁剪、时间范围和容器，再逐帧解码，不调用 get_components。每条视频只有一个 DLSS worker，保留跨帧运动历史。PIPE 输出缓冲只容纳一帧；Linux files 传输逐帧写原始输入、逐帧读原始输出。files 仍占用完整输入＋输出的临时磁盘，并不是零磁盘交换模式。

临时结果位于 ComfyUI temp/dlss_stream，需接 SaveVideo 永久保存。SDR 使用 RGB 无损 H.264 中间编码，以免 SR→NR 反复有损转换；HDR 保留 PQ/HLG 色彩标记，以 10-bit 4:4:4 H.264 编码（不是 16-bit 无损保证）。音轨保留采样率与声道，按画面时长裁切并归一化起始时间，编码 AAC 192kb/s；AAC 不是位级无损。中间文件可能较大，CPU 编码有时间成本。

旧 ComfyUI 缺少 VIDEO API 时，IMAGE-only 调用保留历史路径。当前文件路径需 PyAV av>=14，以及 libx264/libx264rgb 编码器；ComfyUI 自带的 PyAV 通常具备这些能力。初始化/编码失败不会发布不完整视频。取消、超时、worker 错误、短帧和多余输出均保留错误检查并清理临时结果。

日志：DLSS memory input_before / worker_before / encode_progress（每60帧）/ worker_after / file_video_ready，Linux 附 RSS、匿名页及 cgroup 用量。它们可与 CineStack export_model_release_*、export_frame_handoff_after 对照。普通 IMAGE 图仍需要整片张量；只有文件 VIDEO 链路避免画面内存随视频长度增长。

验证包括真实 CPU 编解码、模拟 DLSS 的真实子进程/管道、Wine 文件传输协议、两级视频交接、音轨和 HDR 标记、失败清理。模拟 worker 只做像素复制/最近邻缩放，不代表 NVIDIA DLSS 质量或 GPU 性能。服务器仍需实跑确认 Wine/驱动及实际内存峰值。
