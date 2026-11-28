# 环境要求与安装

[English](../requirements.md) | [简体中文](requirements.md)

## 支持的运行环境

UniDL 需要 **Python 3.11 或更高版本**，支持在受维护的 Windows、macOS 与 Linux 版本上运行。建议使用 64 位 Python 构建，因为 CDM 库和各类媒体工具通常仅提供 64 位平台分发。

Python 依赖声明在 `pyproject.toml` 中。仓库同时附带了 `requirements.txt`（便于按最低版本要求快速安装）与 `requirements-dev.txt`（包含测试与代码检查工具链）。`pip` 会自动解析传递依赖。

## 从 PyPI 安装

在虚拟环境中安装已发布的包：

~~~console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install unidl
~~~

使用 `python -m pip` 可确保 `unidl` 命令入口安装到当前激活的 Python 解释器环境中。

## 从源码仓库安装

从本地源码检出执行常规安装：

~~~console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .
~~~

参与开发时，以可编辑模式安装并附带测试和代码检查工具：

~~~console
python -m pip install -e '.[dev]'
~~~

使用依赖清单文件的等价方式如下：

~~~console
python -m pip install -r requirements-dev.txt
~~~

使用 `uv` 时：

~~~console
uv sync --extra dev
~~~

## 外部工具要求

Python 包本身不包含原生媒体二进制文件或私有设备数据。

UniDL 会优先查找操作系统的 `PATH`。如果某个命令未在系统 `PATH` 中安装，则会继续检查项目根目录下的 `binaries/` 目录。这样，便携式本地目录可以把 `ffmpeg`、`ffprobe`、`mkvmerge` 和 `aria2c` 等工具直接放在项目旁，同时不会覆盖系统中版本更新的全局安装。在类 Unix 系统上，放入该目录的文件必须具有可执行权限；在 Windows 上，请保留正常的 `.exe` 或 `.cmd` 扩展名。

- **FFmpeg 与 ffprobe**：强烈建议安装，多条混流、音频转码、字幕转换及媒体探测路径均依赖它们。
- **pymonalisa** 与 **wasmtime**：已随依赖包含，用于 MonaLisa 本地票据 DRM 系统；不过 `.mld` 设备仍需配备其引用的 WebAssembly 模块。
- **mkvmerge**：可选。在用户显式选择或可产出更佳 Matroska 封装结果时使用；在可行场景下 FFmpeg 会作为回退方案。
- **MP4Box**（GPAC）：可选。选择 MP4Box 封装或输出立体 3D 点播内容时使用。
- **dovi_tool**：可选。用于 Dolby Vision + HDR10 混合输出（含自动 L5 黑边裁切与 L6 亮度元数据写入）以及 HDR10+ 转 DV Profile 8.1 生成。
- **hdr10plus_tool**：可选。仅在开启 `generate_dv_from_hdr10p` 时用于提取 HDR10+ 动态元数据并合成为 Dolby Vision Profile 8.1。
- **shaka-packager**：可选。供相关封装/解密流程或声明依赖的服务使用。
- **aria2c**：可选。仅在用户选择该分段下载后端时使用。
- **curl**：可选。供显式声明其为外部工具（Helper）的服务或传输回退路径使用。

可通过操作系统的包管理器安装上述工具，也可以直接在 TUI 的**就绪检查 → 安装工具**（点击顶栏按钮或按 `i`）中查看各平台推荐安装命令并确认安装。验证安装示例：

~~~console
ffmpeg -version
ffprobe -version
~~~

若服务声明的外部工具缺失，TUI 会在开始播放前给出提示。部分服务可能存在额外的提供方专属依赖，具体应查阅对应服务的文档和外部工具声明。

## 运行时数据

UniDL 会在需要时自动创建配置好的状态目录。CDM 设备文件、浏览器 Cookie、账号凭据、访问/刷新令牌、密钥库数据库、外部工具、签名 URL 及导出的命令文件均属于运行时数据，不会随 Python 包分发。请将它们放在 `unidl.yaml` 配置的路径中，并确保私有数据不被纳入版本控制。

安装完成后，运行 `unidl` 即可启动界面；若配置文件不在当前目录，请显式指定 YAML 路径：

~~~console
unidl
unidl --config ./unidl.yaml
~~~

在进入具体服务前，可执行只读环境检查，或刷新已配置的 Git 服务仓库：

~~~console
unidl --config ./unidl.yaml services
unidl --config ./unidl.yaml cdm --check
unidl --config ./unidl.yaml schedule list
unidl --config ./unidl.yaml refresh-services          # 附带 --force 可忽略 TTL 强制刷新
~~~
