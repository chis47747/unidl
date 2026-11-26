# 仅编译服务

[English](../compiled-services.md) | [简体中文](compiled-services.md)

UniDL 支持两种形式的服务包：

* **源码包**（`source`，默认）：包含 `Service` 子类的常规 Python 包；
* **仅编译包**（`compiled-only`）：包含 `service.toml` 清单与原生 Python 扩展模块，分发文件中不包含源码形式的服务实现。

清单文件必须明确声明服务标识及其实现方式：

```toml
id = "amc"
name = "AMC+"
implementation = "compiled-only"
module = "_service_native"
entry_classes = ["Amc"]
python_abis = ["cpython-313"]
platforms = ["macos-arm64"]
min_unidl = "2.1.5"
```

启动时，加载器会检查每一项兼容性字段，并验证入口模块确为 Python 原生扩展（`.so` 或 `.pyd`），随后将导出的 `Service` 类注册到与源码服务共用的注册表中。加载器不会回退去查找缺失的源码文件。如果服务包面向的是其他 Python ABI 或平台，该服务将保持不可用，就绪检查会在蓝色的 `partial ready`（部分就绪）/ 可选增强分类中说明具体原因。

原生扩展不具备跨平台通用性。你需要为每一个受支持的操作系统/架构及 Python ABI 分别构建并分发产物——通常作为独立于通用 `py3-none-any` UniDL wheel 之外的平台专属服务包提供。切勿将令牌、Cookie、CDM 或其他运行时凭据打入编译后的服务包。

如果需要在同一个服务包目录中汇总多个平台/ABI 构建，可以在根目录清单中声明 `variants_dir`。其下每个子目录各自包含一套 `service.toml` 与原生扩展模块。UniDL 会依据当前运行的 Python ABI 和平台精确匹配唯一子目录，并将其以规范的服务包名加载，从而保证相对导入维持标准的服务契约。如果无任何变体匹配，或同时命中多个变体，该服务会被隔离，并由就绪检查报告原因。

根目录清单还支持可选布尔字段 `custom_license = true`，用于显式声明该服务具备自定义许可证能力。设置后，服务的**许可证操作**（License action）选项中会出现**自定义许可证**（Custom license），无需依据扩展模块名或方法覆写进行猜测。该声明同样适用于选中的 ABI/平台变体。仅使用标准 Before/After 模式的服务省略此字段即可。

安装完成后，在 **设置 → 服务 → 注册服务** 中注册该包并重启 UniDL，再运行 `unidl services` 核验加载状态。由于导出的类实现了同一套 `Service` 契约，仅编译服务包可以完整保留常规的服务设置、登录流程、播放与下载行为。
