# 发布 UniDL

[English](../publishing.md) | [简体中文](publishing.md)

GitHub 与 PyPI 是两个相互独立的服务。GitHub 登录仅控制代码仓库操作，并不决定包在 PyPI 上归属哪个账号。UniDL 的发布通过 PyPI API 令牌或 GitHub Actions 受信任发布（trusted publisher）完成。

## 应用内更新检查使用的发布元数据

主界面将 PyPI 视为可安装版本的权威来源，并查询 `https://pypi.org/pypi/unidl/json` 获取最新发布的版本号。同时，它还会查询 `chis47747/unidl` 的公开 GitHub Releases API；GitHub 仅用于展示面向用户的更新说明和浏览器跳转链接，不作为安装权威源。发布时请先将发行包上传到 PyPI，再发布对应的 GitHub Release，并使用一致的 `vX.Y.Z` 标签，确保两侧描述的是同一版本。仅向仓库推送提交并不构成发布，也不会向更新检查器提供发布说明。更新检查遵循尽力而为原则，会在当前进程内缓存结果，离线时亦可安全运行，绝不会自动下载或执行更新。

## 使用 PyPI 令牌本地发布

1. 登录目标账号的 <https://pypi.org/>，在 **Account settings → API tokens** 中创建 API 令牌。如果包尚未创建过，首次上传需要账号级（account-scoped）令牌；首次发布完成后，请轮换该令牌，并为后续发布改用项目级（project-scoped）令牌。
2. 在目标提交上打好版本标签，然后基于该标签构建并校验发行包。构建助手只会将该标签下的公开输入导出到干净目录中，并在复制到输出目录前校验 wheel 与源码包：

   ~~~console
   python tools/build_public_release.py vX.Y.Z --out-dir dist/vX.Y.Z
   uvx --from twine twine check dist/vX.Y.Z/*
   ~~~

3. 仅通过当前 Shell 环境变量传入令牌，切勿将其写入已提交的文件、YAML、命令导出文件或聊天记录：

   ~~~console
   read -r -s UV_PUBLISH_TOKEN
   export UV_PUBLISH_TOKEN
   uv publish dist/vX.Y.Z/*
   unset UV_PUBLISH_TOKEN
   ~~~

   `uv publish` 会将 wheel 与源码归档包一并上传至 `https://upload.pypi.org/legacy/`。由于 PyPI 拒绝重复文件名，每次发布新版本都必须先递增 `pyproject.toml` 中的版本号。上传完成后，请在 <https://pypi.org/project/unidl/> 查看结果，并在干净的虚拟环境中通过 `python -m pip install unidl` 验证安装。

等价的 Twine 上传命令如下：

~~~console
python -m pip install twine
python -m twine upload dist/*
~~~

Twine 读取 `TWINE_USERNAME=__token__` 与 `TWINE_PASSWORD=pypi-...`；请沿用上文同样的“不落盘”凭据管理方式，不要将 PyPI 令牌写入 `.pypirc` 或代码仓库。

## 切换 GitHub 账号

GitHub CLI 支持同时保存多个 `github.com` 账号。可在不删除现有凭据的情况下登录新账号，再将其切换为当前仓库操作使用的活跃账号：

~~~console
gh auth login --hostname github.com --git-protocol https --web
gh auth status --hostname github.com
gh auth switch --hostname github.com --user NEW_GITHUB_USERNAME
~~~

在创建私有仓库或执行推送前，请先核对当前活跃身份：

~~~console
gh auth status --active --hostname github.com
gh repo view --json nameWithOwner,visibility
~~~

`gh auth switch` 仅切换活跃的 GitHub 账号，不会改变 PyPI 账号或 `uv publish` 所用的令牌。在完成仓库内容与运行时数据排除规则的全面审查前，请保持项目仓库为私有状态。

## GitHub Actions 受信任发布

如需无人值守自动发布，请在 PyPI 账号设置中为目标 PyPI 项目、代码仓库和发布工作流配置**受信任发布（trusted publisher）**。配置后，工作流会申请临时 OIDC 令牌完成发布，无需在仓库中保存长期有效的 PyPI 密钥。对应的作业需要 `id-token: write` 权限，并应构建相同的 wheel 与 sdist、运行 `twine check`，且仅在推送版本标签时触发发布。

这一路径最终使用的仍是配置了受信任发布的那个 PyPI 账号；拥有仓库的 GitHub 账号属于独立身份。可以先在本地使用账号级令牌完成首次发布，随后启用受信任发布并撤销该账号级令牌。
