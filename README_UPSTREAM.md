# 影栈（CineStack）DLSS · 上游技术参考

影栈 DLSS ComfyUI 节点基于 Daniil Sokolyuk 的 **video2dlssnr** 实现构建。上游工作程序提供 NVIDIA DLSS Super Resolution 与 Neural Rendering 的图像、视频处理能力；本项目提供 ComfyUI 集成、Linux / Wine 适配与相关部署工具。

本仓库固定使用上游 **v1.4.1** 运行资产，并通过 SHA-256 校验获取。上游 EXE 与 DLSS / NR DLL 保留原始二进制。

## 参考入口

- [上游项目与原始说明](https://github.com/DaniilSokolyuk/video2dlssnr/tree/v1.4.1)
- [v1.4.1 发布资产](https://github.com/DaniilSokolyuk/video2dlssnr/releases/tag/v1.4.1)
- [上游 C++ 源码](https://github.com/DaniilSokolyuk/video2dlssnr/tree/v1.4.1/src)

上游的桌面 UI、独立命令行工具、编译方法及其他发行方式以对应版本的官方说明为准。它们与影栈 ComfyUI 节点的安装和验证范围分别适用。

## 影栈文档入口

| 文档 | 内容 |
| --- | --- |
| [产品说明](README.md) | 节点功能、安装入口与使用示例 |
| [Linux 配置指南](LINUX.md) | Wine、图形运行库与节点配置 |
| [容器 GPU 编码指南](NVENC_CONTAINER.md) | 可选 NVENC 修复与持久化启动 |
| [功能验收指南](VALIDATION.md) | 输出检查与制作工作流验收 |
| [运行维护指南](CODE_REVIEW.md) | 资源、启停、更新与开发验证 |

## 署名与许可

上游作者：**Daniil Sokolyuk**。主体代码的上游版权声明保存在 [MIT License](LICENSE) 中。

NVIDIA、DLSS 及其他第三方组件遵循各自许可与商标规则。影栈（CineStack）为本项目的产品品牌；本项目与 NVIDIA 官方无隶属或背书关系。
