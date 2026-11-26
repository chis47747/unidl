# 播放会话生命周期

[English](../playback-lifecycle.md) | [简体中文](playback-lifecycle.md)

提供方侧的播放会话或并发会话由服务代码全权负责管理。原生下载器和共享 DRM 层不得自行创建、刷新或关闭提供方会话。

当服务在发出 `Playback` 之前打开了会话时，应将整个流程包裹在同一个 `try/finally` 语句块中：

```python
opened = client.open_playback_session(source)
try:
    yield ctx.emit(playback)
finally:
    if opened:
        client.close_playback_session(source)
```

该 `finally` 块必须完整覆盖 manifest 解析、密钥获取、命令导出、下载、取消、返回/退出（Back/Quit）以及正常结束等全部路径。如果服务源码本身并不存在提供方会话或心跳机制，切勿仅仅因为使用了 DRM 而凭空添加。本地 CDM 会话与提供方会话彼此独立，并始终由 DRM 实现层自行关闭。

本文档是面向所有需要开启提供方播放或并发会话的服务的框架契约。不涉及此类生命周期的服务直接发出 `Playback` 即可，无需额外引入会话逻辑。
