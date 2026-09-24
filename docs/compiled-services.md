# Compiled-only services

UniDL accepts two service package forms:

* **source** (the default): a Python package containing a `Service` subclass;
* **compiled-only**: a package containing `service.toml` and native Python
  extension modules, with the service implementation omitted from the shipped
  files.

The manifest must identify the service and its implementation:

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

At startup the loader checks every compatibility field and verifies that the
entry module is a Python extension (`.so` or `.pyd`). It then registers the
exported `Service` class in the same registry used by source services. It does
not fall back to a missing source file. If a package is for another Python ABI
or platform, the service remains unavailable and readiness reports the reason
under the blue `partial ready`/enhancement category.

Native extensions are not universally portable. Build and ship a separate
artifact for every supported operating-system/architecture and Python ABI,
usually as a platform-specific service bundle alongside UniDL rather than in
the generic `py3-none-any` UniDL wheel. Do not copy tokens, cookies, CDMs, or
other runtime credentials into a compiled package.

Register the package from **Settings → Services → Register a service**, restart
UniDL, and verify it with `unidl services`. A compiled-only package can keep all
normal service settings, login flows, playback and downloader behaviour because
its exported class implements the same `Service` contract.
