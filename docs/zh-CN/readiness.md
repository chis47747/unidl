# 就绪检查（Readiness）级别

[English](../readiness.md) | [简体中文](readiness.md)

状态栏中的就绪检查标签与**就绪检查（Readiness）**界面将依赖划分为三个级别。缺少某项依赖并不等同于安装损坏，颜色本身代表了受影响功能的范围。

| 颜色 | 级别 | 示例 | 影响 |
| --- | --- | --- | --- |
| 红色 | `core` | Textual、PyYAML、Requests、Cryptography、PyCryptodome、可写的 UniDL 路径 | UniDL 正常的 TUI / 下载器运行环境不完整 |
| 黄色 | `global` | Widevine / PlayReady / MonaLisa 相关库，以及本地或远程 CDM 设备 | 受 DRM 保护的服务可能无法使用；未加密播放及不依赖对应 DRM 的服务仍可正常工作 |
| 蓝色 | `enhancement` | 服务声明的外部助手、`dovi_tool`，以及混合输出（Hybrid）所需的 ffmpeg / ffprobe / mkvmerge 工具 | 对应服务或可选功能不可用；UniDL 的其余部分仍可正常使用 |
| 绿色 | — | 以上三个级别的依赖全部就位 | 当前安装已完全就绪 |

服务外部助手的影响范围始终限定在声明它的服务之内。某个服务标记为必需的助手缺失时，会阻止该服务运行，但绝不应导致整个应用亮红灯。同理，CDM 属于全局能力而非启动 UniDL 的先决条件：在用户添加本地或远程设备前，它显示为黄色。

`dovi_tool` 被刻意归为蓝色级别。Dolby Vision + HDR10 混合输出（Hybrid）属于可选的后处理功能，常规播放与下载从不依赖它。其就绪检查条目还会一并检查混合输出流水线所调用的配套工具。
