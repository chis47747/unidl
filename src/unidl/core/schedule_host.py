"""OS-native background scheduler hosting (macOS launchd, Linux systemd, Windows Task Scheduler).

Installs a lightweight periodic trigger that invokes ``unidl schedule run`` so
scheduled downloads still execute even when the TUI is closed. Cross-process
locking in :mod:`unidl.core.scheduler` guarantees the TUI and OS host never
double-run the same job.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .secureio import atomic_write_bytes, atomic_write_text

LAUNCHD_LABEL = "com.unidl.scheduler"
SYSTEMD_SERVICE_NAME = "unidl-scheduler.service"
SYSTEMD_TIMER_NAME = "unidl-scheduler.timer"
WINDOWS_TASK_NAME = "UniDLScheduler"
DEFAULT_HOST_INTERVAL_MINUTES = 5

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HostScheduleStatus:
    platform: str          # "darwin" | "linux" | "win32" | other
    backend: str           # "launchd" | "systemd" | "schtasks" | "unsupported"
    supported: bool
    installed: bool
    active: bool
    interval_minutes: int
    target_path: str = ""
    command: str = ""
    detail: str = ""


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        args,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def build_schedule_run_argv(
    config: Config,
    *,
    python_executable: str | None = None,
) -> list[str]:
    exe = str(python_executable or sys.executable)
    argv = [exe, "-m", "unidl"]
    if config.source is not None:
        argv.extend(["--config", str(config.source)])
    argv.extend(["schedule", "run"])
    return argv


def _launchd_plist_path(home_dir: Path | None = None) -> Path:
    base = Path(home_dir) if home_dir is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_dir(home_dir: Path | None = None) -> Path:
    base = Path(home_dir) if home_dir is not None else Path.home()
    return base / ".config" / "systemd" / "user"


def build_launchd_plist(
    config: Config,
    *,
    interval_minutes: int = DEFAULT_HOST_INTERVAL_MINUTES,
    python_executable: str | None = None,
) -> dict[str, Any]:
    minutes = max(1, min(1440, int(interval_minutes)))
    argv = build_schedule_run_argv(config, python_executable=python_executable)
    env: dict[str, str] = {}
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    env["UNIDL_HOME"] = str(config.paths.home)
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "StartInterval": minutes * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(config.paths.logs / "scheduler-launchd.log"),
        "StandardErrorPath": str(config.paths.logs / "scheduler-launchd.err.log"),
        "EnvironmentVariables": env,
    }


def build_systemd_units(
    config: Config,
    *,
    interval_minutes: int = DEFAULT_HOST_INTERVAL_MINUTES,
    python_executable: str | None = None,
) -> tuple[str, str]:
    minutes = max(1, min(1440, int(interval_minutes)))
    argv = build_schedule_run_argv(config, python_executable=python_executable)
    exec_start = " ".join(shlex.quote(part) for part in argv)
    service = (
        "[Unit]\n"
        "Description=UniDL Scheduled Downloads Runner\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=UNIDL_HOME={shlex.quote(str(config.paths.home))}\n"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run UniDL Scheduled Downloads Periodically\n\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        f"OnUnitActiveSec={minutes}min\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def get_host_status(
    config: Config,
    *,
    platform_name: str | None = None,
    home_dir: Path | None = None,
    runner: CommandRunner = _default_runner,
    probe_runtime: bool = True,
) -> HostScheduleStatus:
    plat = (platform_name or sys.platform).lower()
    argv = build_schedule_run_argv(config)
    cmd_str = " ".join(shlex.quote(part) for part in argv)

    if plat == "darwin":
        plist_path = _launchd_plist_path(home_dir)
        installed = plist_path.is_file()
        interval = DEFAULT_HOST_INTERVAL_MINUTES
        if installed:
            try:
                loaded = plistlib.loads(plist_path.read_bytes())
                raw_sec = int(loaded.get("StartInterval", interval * 60))
                interval = max(1, raw_sec // 60)
            except Exception:
                pass
        active = False
        detail = ""
        if installed and probe_runtime:
            try:
                proc = runner(["launchctl", "list", LAUNCHD_LABEL])
                active = proc.returncode == 0
                detail = "loaded in launchd" if active else "plist present (not loaded)"
            except (OSError, subprocess.SubprocessError) as exc:
                detail = str(exc)
        elif installed:
            active = True
            detail = "plist installed"
        else:
            detail = "not installed"
        return HostScheduleStatus(
            platform="darwin",
            backend="launchd",
            supported=True,
            installed=installed,
            active=active,
            interval_minutes=interval,
            target_path=str(plist_path),
            command=cmd_str,
            detail=detail,
        )

    if plat.startswith("linux"):
        unit_dir = _systemd_dir(home_dir)
        timer_path = unit_dir / SYSTEMD_TIMER_NAME
        installed = timer_path.is_file()
        interval = DEFAULT_HOST_INTERVAL_MINUTES
        if installed:
            try:
                for line in timer_path.read_text("utf-8").splitlines():
                    if line.startswith("OnUnitActiveSec="):
                        digits = "".join(ch for ch in line.split("=", 1)[1] if ch.isdigit())
                        if digits:
                            interval = max(1, int(digits))
            except OSError:
                pass
        active = False
        detail = "not installed"
        if installed and probe_runtime:
            try:
                proc = runner(["systemctl", "--user", "is-active", SYSTEMD_TIMER_NAME])
                active = proc.returncode == 0
                detail = (proc.stdout or proc.stderr or "").strip() or ("active" if active else "inactive")
            except (OSError, subprocess.SubprocessError) as exc:
                detail = str(exc)
        elif installed:
            active = True
            detail = "systemd user timer installed"
        return HostScheduleStatus(
            platform="linux",
            backend="systemd",
            supported=True,
            installed=installed,
            active=active,
            interval_minutes=interval,
            target_path=str(timer_path),
            command=cmd_str,
            detail=detail,
        )

    if plat == "win32":
        installed = False
        detail = "not installed"
        if probe_runtime:
            try:
                proc = runner(["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME])
                installed = proc.returncode == 0
                detail = "registered in Task Scheduler" if installed else "not installed"
            except (OSError, subprocess.SubprocessError) as exc:
                detail = str(exc)
        return HostScheduleStatus(
            platform="win32",
            backend="schtasks",
            supported=True,
            installed=installed,
            active=installed,
            interval_minutes=DEFAULT_HOST_INTERVAL_MINUTES,
            target_path=WINDOWS_TASK_NAME,
            command=cmd_str,
            detail=detail,
        )

    return HostScheduleStatus(
        platform=plat,
        backend="unsupported",
        supported=False,
        installed=False,
        active=False,
        interval_minutes=DEFAULT_HOST_INTERVAL_MINUTES,
        command=cmd_str,
        detail=f"Automatic host setup is not supported on {plat}.",
    )


def install_host_scheduler(
    config: Config,
    *,
    interval_minutes: int = DEFAULT_HOST_INTERVAL_MINUTES,
    platform_name: str | None = None,
    home_dir: Path | None = None,
    runner: CommandRunner = _default_runner,
    register_runtime: bool = True,
) -> HostScheduleStatus:
    plat = (platform_name or sys.platform).lower()
    minutes = max(1, min(1440, int(interval_minutes)))
    try:
        config.paths.logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return HostScheduleStatus(
            platform=plat,
            backend="launchd" if plat == "darwin" else ("systemd" if plat.startswith("linux") else ("schtasks" if plat == "win32" else "unsupported")),
            supported=plat == "darwin" or plat.startswith("linux") or plat == "win32",
            installed=False,
            active=False,
            interval_minutes=minutes,
            detail=str(exc),
        )

    if plat == "darwin":
        plist_path = _launchd_plist_path(home_dir)
        installed = False
        active = False
        detail = "installed"
        try:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = plistlib.dumps(build_launchd_plist(config, interval_minutes=minutes))
            atomic_write_bytes(plist_path, payload, mode=0o644)
            installed = True
            active = True
            if register_runtime:
                uid = os.getuid() if hasattr(os, "getuid") else 0
                domain = f"gui/{uid}"
                runner(["launchctl", "bootout", domain, str(plist_path)])
                proc = runner(["launchctl", "bootstrap", domain, str(plist_path)])
                if proc.returncode != 0:
                    legacy = runner(["launchctl", "load", "-w", str(plist_path)])
                    if legacy.returncode != 0:
                        active = False
                        detail = (proc.stderr or legacy.stderr or "launchctl failed").strip()
                    else:
                        detail = "loaded via launchctl"
                else:
                    detail = f"loaded in launchd (every {minutes}m)"
        except (OSError, subprocess.SubprocessError) as exc:
            active = False
            detail = str(exc)
        return HostScheduleStatus(
            platform="darwin",
            backend="launchd",
            supported=True,
            installed=installed,
            active=active,
            interval_minutes=minutes,
            target_path=str(plist_path),
            command=" ".join(shlex.quote(p) for p in build_schedule_run_argv(config)),
            detail=detail,
        )

    if plat.startswith("linux"):
        unit_dir = _systemd_dir(home_dir)
        service_path = unit_dir / SYSTEMD_SERVICE_NAME
        timer_path = unit_dir / SYSTEMD_TIMER_NAME
        installed = False
        active = False
        detail = "installed"
        try:
            unit_dir.mkdir(parents=True, exist_ok=True)
            service_text, timer_text = build_systemd_units(config, interval_minutes=minutes)
            atomic_write_text(service_path, service_text, mode=0o644)
            atomic_write_text(timer_path, timer_text, mode=0o644)
            installed = True
            active = True
            if register_runtime:
                runner(["systemctl", "--user", "daemon-reload"])
                proc = runner(["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER_NAME])
                if proc.returncode != 0:
                    active = False
                    detail = (proc.stderr or proc.stdout or "systemctl failed").strip()
                else:
                    detail = f"systemd user timer enabled (every {minutes}m)"
        except (OSError, subprocess.SubprocessError) as exc:
            active = False
            detail = str(exc)
        return HostScheduleStatus(
            platform="linux",
            backend="systemd",
            supported=True,
            installed=installed,
            active=active,
            interval_minutes=minutes,
            target_path=str(timer_path),
            command=" ".join(shlex.quote(p) for p in build_schedule_run_argv(config)),
            detail=detail,
        )

    if plat == "win32":
        argv = build_schedule_run_argv(config)
        tr_cmd = subprocess.list2cmdline(argv)
        active = True
        detail = "installed"
        if register_runtime:
            try:
                proc = runner(
                    [
                        "schtasks",
                        "/Create",
                        "/F",
                        "/SC",
                        "MINUTE",
                        "/MO",
                        str(minutes),
                        "/TN",
                        WINDOWS_TASK_NAME,
                        "/TR",
                        tr_cmd,
                    ]
                )
                if proc.returncode != 0:
                    active = False
                    detail = (proc.stderr or proc.stdout or "schtasks failed").strip()
                else:
                    detail = f"Task Scheduler enabled (every {minutes}m)"
            except (OSError, subprocess.SubprocessError) as exc:
                active = False
                detail = str(exc)
        return HostScheduleStatus(
            platform="win32",
            backend="schtasks",
            supported=True,
            installed=active,
            active=active,
            interval_minutes=minutes,
            target_path=WINDOWS_TASK_NAME,
            command=tr_cmd,
            detail=detail,
        )

    return get_host_status(
        config,
        platform_name=plat,
        home_dir=home_dir,
        runner=runner,
        probe_runtime=False,
    )


def uninstall_host_scheduler(
    config: Config,
    *,
    platform_name: str | None = None,
    home_dir: Path | None = None,
    runner: CommandRunner = _default_runner,
    unregister_runtime: bool = True,
) -> HostScheduleStatus:
    plat = (platform_name or sys.platform).lower()

    if plat == "darwin":
        plist_path = _launchd_plist_path(home_dir)
        detail = "uninstalled"
        try:
            if unregister_runtime and plist_path.is_file():
                uid = os.getuid() if hasattr(os, "getuid") else 0
                proc = runner(["launchctl", "bootout", f"gui/{uid}", str(plist_path)])
                if proc.returncode != 0:
                    runner(["launchctl", "unload", str(plist_path)])
            plist_path.unlink(missing_ok=True)
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
        return HostScheduleStatus(
            platform="darwin",
            backend="launchd",
            supported=True,
            installed=plist_path.is_file(),
            active=False,
            interval_minutes=DEFAULT_HOST_INTERVAL_MINUTES,
            target_path=str(plist_path),
            detail=detail,
        )

    if plat.startswith("linux"):
        unit_dir = _systemd_dir(home_dir)
        timer_path = unit_dir / SYSTEMD_TIMER_NAME
        service_path = unit_dir / SYSTEMD_SERVICE_NAME
        detail = "uninstalled"
        try:
            if unregister_runtime:
                runner(["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_NAME])
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)
        try:
            timer_path.unlink(missing_ok=True)
            service_path.unlink(missing_ok=True)
        except OSError as exc:
            detail = str(exc)
        try:
            if unregister_runtime:
                runner(["systemctl", "--user", "daemon-reload"])
        except (OSError, subprocess.SubprocessError) as exc:
            if detail == "uninstalled":
                detail = str(exc)
        return HostScheduleStatus(
            platform="linux",
            backend="systemd",
            supported=True,
            installed=timer_path.is_file(),
            active=False,
            interval_minutes=DEFAULT_HOST_INTERVAL_MINUTES,
            target_path=str(timer_path),
            detail=detail,
        )

    if plat == "win32":
        detail = "uninstalled"
        if unregister_runtime:
            try:
                runner(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK_NAME])
            except (OSError, subprocess.SubprocessError) as exc:
                detail = str(exc)
        return HostScheduleStatus(
            platform="win32",
            backend="schtasks",
            supported=True,
            installed=False,
            active=False,
            interval_minutes=DEFAULT_HOST_INTERVAL_MINUTES,
            target_path=WINDOWS_TASK_NAME,
            detail=detail,
        )

    return get_host_status(
        config,
        platform_name=plat,
        home_dir=home_dir,
        runner=runner,
        probe_runtime=False,
    )


__all__ = [
    "DEFAULT_HOST_INTERVAL_MINUTES",
    "LAUNCHD_LABEL",
    "SYSTEMD_SERVICE_NAME",
    "SYSTEMD_TIMER_NAME",
    "WINDOWS_TASK_NAME",
    "HostScheduleStatus",
    "build_launchd_plist",
    "build_schedule_run_argv",
    "build_systemd_units",
    "get_host_status",
    "install_host_scheduler",
    "uninstall_host_scheduler",
]
