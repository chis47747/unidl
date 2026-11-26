# 测试 UniDL

[English](../testing.md) | [简体中文](testing.md)

UniDL 采用“快速离线检查 + 定向在线验证”的测试策略：先用离线测试验证契约与确定性行为，再针对提供方接口结构、账号、DRM 与 manifest 进行定向在线检查。请将在线测试夹具和账号凭据保存在代码仓库之外。

## 快速检查

在项目根目录运行以下命令：

~~~console
python -m pytest -q
python -m ruff check src tests
python -m compileall -q src
python -m unidl --config ./unidl.yaml services
python -m unidl --config ./unidl.yaml cdm --check
~~~

前三条命令可直接用于持续集成（CI）。后两条 CLI 命令用于检查服务注册与本地设备配置，不会发起任何下载任务。

## 服务测试

单个服务的测试套件应覆盖以下内容：

1. 模块导入与注册表校验；
2. 每一个已声明的 URL、搜索、直播、片库与登录入口点；
3. Flow 交互应答、分页、多选批处理以及返回（Back）导航；
4. 凭据、Cookie、令牌、刷新与退出登录行为；
5. manifest/档位设置与输出轨道设置的独立性；
6. 每个已声明 DRM 系统的服务本地许可证传输；
7. 多 PSSH/KID 场景以及适用时的直播密钥轮换；
8. 章节、音频元数据、外部工具（Helper）与播放会话清理路径；
9. 至少一个已授权标题完整走到类型化的 `Playback`。

请使用经过 URL 和密钥脱敏的录制响应作为测试夹具。单元测试应使用确定性桩对象（fake）替换真实网络客户端。无头 Flow 测试可借助 `AutoPresenter` 运行：

~~~python
from unidl.core.flow import AutoPresenter, FlowContext, run_flow

ctx = FlowContext(settings=service.settings)
emitted = run_flow(
    service.open_url(ctx, "https://example.invalid/title"),
    AutoPresenter({"Season": 1}),
)
assert emitted
~~~

## 下载器测试

原生交付层应结合本地 manifest 与本地回环 HTTP 测试夹具进行验证。按需覆盖 DASH、HLS、ISM、JSON 与直链媒体解析，并核查：

- 最终传给交付层的轨道与所选轨道完全一致；
- KID/密钥清单包含全部所需密钥；
- 重试与取消操作能够及时中止；
- 直播录制时长为 0 表示不限时；
- 输出文件命名正确反映所选视频/音频编码与动态范围标签；
- 字幕、章节、封面图与 ID3 元数据在混流后完整保留；
- 可选元数据请求失败不会阻断下载流程。

切勿在测试夹具中使用真实的签名 manifest 或内容密钥。

## 直播检查

直播录制测试应使用带滚动媒体序列（moving media sequence）的本地播放列表，或确定性的模拟刷新循环。请确认：选轨发生在录制开始之前；DVR/回看选项按 Back 能退回上一个问题；Stop/Back/Esc 能可靠停止任务；KID 轮换时仅触发该服务已验证的初始化数据路径。

## 打包检查

发布前构建完整的发行产物：

~~~console
python tools/build_public_release.py vX.Y.Z --out-dir dist/vX.Y.Z
~~~

逐项检查生成的 wheel 和源码归档包，确保未误打入凭据、设备文件、令牌、Cookie、日志、本地绝对路径或生成的缓存文件。在各个受支持的操作系统/CPU 目标平台上，分别验证可编辑安装和干净虚拟环境安装。

## 报告测试失败

提交失败报告时，请提供 UniDL 版本号、操作系统、服务 ID、所选档位、manifest 类型、DRM 系统，以及能复现问题的最小脱敏日志片段。严禁附带用户名、密码、刷新令牌、Cookie、`KID:key` 密钥对、许可证响应体、签名 URL 或私有 CDM 材料。
