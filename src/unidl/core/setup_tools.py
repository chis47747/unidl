"""External tool dependency inspection and explicit setup wizard."""

from __future__ import annotations

import platform
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .binaries import BinarySpec, probe_binary_version, registered_binaries, resolve_binary


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Catalog entry for an external binary UniDL uses."""

    name: str
    label: str
    purpose: str
    candidates: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    brew: str = ""
    apt: str = ""
    winget: str = ""
    choco: str = ""
    url: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True)
class ToolStatus:
    """Inspection result for one external tool."""

    spec: ToolSpec
    installed: bool
    path: str = ""
    version: str = ""
    install_command: str = ""


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Result of executing one explicit tool installation."""

    tool: str
    ok: bool
    method: str
    detail: str = ""
    path: str = ""


TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="ffmpeg",
        label="FFmpeg",
        purpose="Remuxing, audio conversion, VUI normalization and L5 crop detection",
        candidates=("ffmpeg", "ffmpeg.exe"),
        version_args=("-version",),
        brew="brew install ffmpeg",
        apt="sudo apt-get install -y ffmpeg",
        winget="winget install Gyan.FFmpeg",
        choco="choco install ffmpeg -y",
        url="https://ffmpeg.org/download.html",
        required=True,
    ),
    ToolSpec(
        name="ffprobe",
        label="FFprobe",
        purpose="Stream probing, duration and HDR luminance inspection",
        candidates=("ffprobe", "ffprobe.exe"),
        version_args=("-version",),
        brew="brew install ffmpeg",
        apt="sudo apt-get install -y ffmpeg",
        winget="winget install Gyan.FFmpeg",
        choco="choco install ffmpeg -y",
        url="https://ffmpeg.org/download.html",
        required=True,
    ),
    ToolSpec(
        name="mkvmerge",
        label="MKVToolNix (mkvmerge)",
        purpose="Matroska muxing, default track flags and Dolby Vision hybrid remux",
        candidates=("mkvmerge", "mkvmerge.exe"),
        version_args=("--version",),
        brew="brew install mkvtoolnix",
        apt="sudo apt-get install -y mkvtoolnix",
        winget="winget install MoritzBunkus.MKVToolNix",
        choco="choco install mkvtoolnix -y",
        url="https://mkvtoolnix.download/",
        required=False,
    ),
    ToolSpec(
        name="MP4Box",
        label="GPAC MP4Box",
        purpose="MP4Box muxing and lossless MV-HEVC 3D packaging",
        candidates=("MP4Box", "mp4box", "MP4Box.exe"),
        version_args=("-version",),
        brew="brew install gpac",
        apt="sudo apt-get install -y gpac",
        winget="winget install GPAC.GPAC",
        choco="choco install gpac -y",
        url="https://gpac.io/downloads/",
        required=False,
    ),
    ToolSpec(
        name="dovi_tool",
        label="dovi_tool",
        purpose="Dolby Vision RPU extraction, L5/L6 editing, injection and P8.1 generation",
        candidates=("dovi_tool", "dovi_tool.exe"),
        version_args=("--version",),
        brew="brew install dovi_tool",
        apt="",
        winget="",
        choco="",
        url="https://github.com/quietvoid/dovi_tool/releases",
        required=False,
    ),
    ToolSpec(
        name="hdr10plus_tool",
        label="hdr10plus_tool",
        purpose="HDR10+ ST 2094-40 metadata extraction for Dolby Vision P8.1 generation",
        candidates=("hdr10plus_tool", "hdr10plus_tool.exe"),
        version_args=("--version",),
        brew="brew install hdr10plus_tool",
        apt="",
        winget="",
        choco="",
        url="https://github.com/quietvoid/hdr10plus_tool/releases",
        required=False,
    ),
    ToolSpec(
        name="shaka-packager",
        label="Shaka Packager",
        purpose="Optional external packager/decrypter companion",
        candidates=("shaka-packager", "packager", "shaka-packager.exe"),
        version_args=("--version",),
        brew="brew install shaka-packager",
        apt="",
        winget="",
        choco="",
        url="https://github.com/shaka-project/shaka-packager/releases",
        required=False,
    ),
    ToolSpec(
        name="aria2c",
        label="aria2c",
        purpose="Optional external segment downloader engine",
        candidates=("aria2c", "aria2c.exe"),
        version_args=("--version",),
        brew="brew install aria2",
        apt="sudo apt-get install -y aria2",
        winget="winget install aria2.aria2",
        choco="choco install aria2 -y",
        url="https://aria2.github.io/",
        required=False,
    ),
)


def detect_package_manager() -> str:
    """Detect the preferred OS package manager on the current machine."""
    system = platform.system().lower()
    if system == "darwin":
        if shutil.which("brew"):
            return "brew"
        return "manual"
    if system == "windows":
        if shutil.which("winget"):
            return "winget"
        if shutil.which("choco"):
            return "choco"
        return "manual"
    if shutil.which("apt-get"):
        return "apt"
    if shutil.which("brew"):
        return "brew"
    return "manual"


def recommended_install_command(spec: ToolSpec, manager: str | None = None) -> str:
    """Return the recommended shell install command (or release URL) for ``spec``."""
    mgr = manager or detect_package_manager()
    mapping = {
        "brew": spec.brew,
        "apt": spec.apt,
        "winget": spec.winget,
        "choco": spec.choco,
    }
    cmd = mapping.get(mgr, "")
    if cmd:
        return cmd
    if spec.url:
        return f"Download from {spec.url}"
    return f"Install {spec.name} and add it to PATH or <home>/binaries/"


def _all_tool_specs(*, include_registered: bool = True) -> list[ToolSpec]:
    specs = list(TOOL_CATALOG)
    if include_registered:
        seen = {spec.name.casefold() for spec in specs}
        for reg in registered_binaries():
            if reg.name.casefold() in seen:
                continue
            specs.append(
                ToolSpec(
                    name=reg.name,
                    label=f"{reg.name} ({reg.service_id})" if reg.service_id else reg.name,
                    purpose=f"Custom binary registered by {reg.service_id}" if reg.service_id else "Custom registered binary",
                    candidates=reg.candidates,
                    version_args=reg.version_args,
                    url=reg.install_hint,
                    required=False,
                )
            )
            seen.add(reg.name.casefold())
    return specs


def find_tool_spec(name: str) -> ToolSpec | None:
    wanted = str(name or "").strip().casefold()
    for spec in _all_tool_specs(include_registered=True):
        if spec.name.casefold() == wanted or wanted in {c.casefold() for c in spec.candidates}:
            return spec
    return None


def inspect_tools(
    *,
    include_registered: bool = True,
    use_cache: bool = False,
    manager: str | None = None,
) -> list[ToolStatus]:
    """Inspect all catalogued (and optionally service-registered) external tools."""
    mgr = manager or detect_package_manager()
    statuses: list[ToolStatus] = []
    for spec in _all_tool_specs(include_registered=include_registered):
        found = resolve_binary(BinarySpec(spec.name, spec.candidates, spec.version_args))
        version = (
            probe_binary_version(found, spec.version_args, use_cache=use_cache)
            if found
            else ""
        )
        statuses.append(
            ToolStatus(
                spec=spec,
                installed=bool(found),
                path=found or "",
                version=version,
                install_command=recommended_install_command(spec, mgr),
            )
        )
    return statuses


def install_tool(
    tool_name: str,
    *,
    manager: str | None = None,
    dry_run: bool = False,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> InstallResult:
    """Explicitly install one catalogued tool using the detected OS package manager."""
    spec = find_tool_spec(tool_name)
    if spec is None:
        return InstallResult(tool=tool_name, ok=False, method="unknown", detail=f"Unknown tool: {tool_name}")
    mgr = manager or detect_package_manager()
    cmd_text = {
        "brew": spec.brew,
        "apt": spec.apt,
        "winget": spec.winget,
        "choco": spec.choco,
    }.get(mgr, "")
    if not cmd_text:
        return InstallResult(
            tool=spec.name,
            ok=False,
            method="manual",
            detail=recommended_install_command(spec, mgr),
        )
    argv = shlex.split(cmd_text, posix=(platform.system().lower() != "windows"))
    if dry_run:
        return InstallResult(tool=spec.name, ok=True, method=f"dry-run:{mgr}", detail=cmd_text)

    run = runner or (
        lambda args: subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=600,
            check=False,
        )
    )
    try:
        proc = run(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return InstallResult(tool=spec.name, ok=False, method=mgr, detail=str(exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-400:] or f"exit {proc.returncode}"
        return InstallResult(tool=spec.name, ok=False, method=mgr, detail=detail)
    found = resolve_binary(BinarySpec(spec.name, spec.candidates, spec.version_args)) or ""
    return InstallResult(
        tool=spec.name,
        ok=True,
        method=mgr,
        detail=f"Installed via {mgr}" + (f" ({found})" if found else ""),
        path=found,
    )


def format_setup_report(statuses: Sequence[ToolStatus]) -> str:
    """Render a concise plain-text dependency setup table."""
    lines: list[str] = []
    for st in statuses:
        state = "OK     " if st.installed else ("MISSING" if st.spec.required else "OPTIONAL")
        detail = st.version or st.path if st.installed else st.install_command
        lines.append(f"{st.spec.name:<16} {state:<8} {detail}")
    return "\n".join(lines)


__all__ = [
    "TOOL_CATALOG",
    "InstallResult",
    "ToolSpec",
    "ToolStatus",
    "detect_package_manager",
    "find_tool_spec",
    "format_setup_report",
    "inspect_tools",
    "install_tool",
    "recommended_install_command",
]
