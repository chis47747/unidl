"""Entry point.

unidl is primarily a TUI: pointing and clicking is the service interface and
keyboard shortcuts are secondary.  The native media engine is also available
for exported/resolved manifests; it does not implement a second service login
or title-resolution interface.

What remains is the minimum needed to launch and to diagnose an install:

    unidl                 launch the interface
    unidl --help
    unidl --version
    unidl --config PATH   use a specific unidl.yaml
    unidl list INPUT      inspect a resolved manifest
    unidl download INPUT  run the native media engine
    unidl services        list registered services (read only)
    unidl cdm [--check]   list configured local CDM devices (read only)
    unidl keys ...        inspect selected local key vaults (read only)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unidl",
        description="Interactive downloader for streaming services. Run without arguments to start.",
        epilog="Quality, codec and track selection happen in the interface, not on the command line.",
    )
    parser.add_argument("--version", action="version", version=f"unidl {__version__}")
    parser.add_argument("--config", type=Path, metavar="PATH", help="path to unidl.yaml")

    sub = parser.add_subparsers(dest="command", metavar="[diagnostics]")

    sub.add_parser("services", help="list registered services and exit")

    cdm = sub.add_parser("cdm", help="list configured local CDM devices and exit")
    cdm.add_argument("--check", action="store_true", help="validate each local device and report what can be read")

    keys = sub.add_parser("keys", help="inspect selected local key vaults and exit")
    keys.add_argument("kid", nargs="?", help="look up a single KID")
    keys.add_argument("--service", help="limit to one service")

    sched = sub.add_parser("schedule", help="manage and run scheduled downloads")
    sched_sub = sched.add_subparsers(dest="schedule_action", metavar="{list,run,daemon,host}")
    sched_sub.add_parser("list", help="list scheduled jobs and exit")
    sched_run = sched_sub.add_parser("run", help="run due scheduled jobs (or one --id) and exit")
    sched_run.add_argument("--id", dest="job_id", help="run one specific job ID regardless of time")
    sched_run.add_argument("--dry-run", action="store_true", help="verify login, title match and episodes without downloading")
    sched_daemon = sched_sub.add_parser("daemon", help="keep checking and running scheduled jobs in the background")
    sched_daemon.add_argument("--interval", type=int, default=60, help="polling interval in seconds (default: 60)")
    sched_host = sched_sub.add_parser("host", help="install or inspect OS background scheduling (launchd / systemd / Task Scheduler)")
    sched_host.add_argument("host_action", choices=("status", "install", "uninstall"))
    sched_host.add_argument("--interval", type=int, default=5, help="check interval in minutes (default: 5)")

    refresh = sub.add_parser("refresh-services", help="clone or update configured Git service repositories")
    refresh.add_argument("--force", action="store_true", help="bypass TTL cache and check remote immediately")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"download", "list"}:
        from .downloader.cli import main as downloader_main

        return downloader_main(argv)

    parser = _build_parser()
    args = parser.parse_args(argv)

    from .core.config import Config

    config = Config.load(args.config)

    if args.command == "services":
        from . import services
        from .core.service import registry

        services.load_all(config)
        return _list_services(registry)
    if args.command == "cdm":
        return _list_cdm(config, check=args.check)
    if args.command == "keys":
        return _keys(config, args)
    if args.command == "schedule":
        return _schedule(config, args)
    if args.command == "refresh-services":
        return _refresh_services(config, args)

    from .tui.app import run

    run(config=config)
    return 0


def _list_services(registry) -> int:
    for service in registry.all():
        features = ",".join(
            name
            for name, on in (
                ("vod", service.SUPPORTS_URL),
                ("live", service.SUPPORTS_LIVE),
                ("search", service.SUPPORTS_SEARCH),
                ("library", service.SUPPORTS_LIBRARY),
            )
            if on
        )
        print(f"{service.ID:<14} {service.NAME:<30} {features:<24} {service.USES.summary()}")
    return 0


def _list_cdm(config, *, check: bool) -> int:
    devices = config.devices
    if not devices:
        print("No cdm.devices configured in unidl.yaml", file=sys.stderr)
        return 1
    for name, path in devices.items():
        state = "ok" if path.is_file() else "missing"
        if check and path.is_file():
            state = _device_summary(path)
        print(f"{name:<24} {state:<30} {path}")
    return 0


def _device_summary(path: Path) -> str:
    """Validate a configured device with the implementation for its suffix."""
    from .core import drm

    system = drm.system_of(path)
    if system == "widevine":
        from .core.cdm import device_summary

        return device_summary(path)
    if system == "playready":
        try:
            from pyplayready.device import Device

            device = Device.load(str(path))
        except Exception as exc:
            return f"unusable ({exc})"
        level = getattr(device, "security_level", None)
        return f"PlayReady SL{level}" if level else "PlayReady device loads"
    if system == "monalisa":
        from .core.monalisa import device_summary

        return device_summary(path)
    return f"unusable (unknown device extension {path.suffix or '-'})"


def _keys(config, args) -> int:
    from .core import vaults
    from .core.settings import SettingsStore, global_settings
    from .core.vault import KeyVault

    primary = KeyVault(config.paths.keys_db)
    collection = vaults.build(config, local=primary)
    settings = global_settings(
        SettingsStore(config.paths.home / "settings.json"),
        config=config,
    )
    selected = vaults.parse_targets(settings.get("vault_read_targets", ""))
    names = [backend.name for backend in collection.enabled(
        use_local=True,
        use_remote=False,
        local_names=selected,
    )]
    try:
        if args.kid:
            rows = collection.find_local(
                args.kid,
                service=args.service,
                local_names=selected,
            )
            if not rows:
                print("no match")
                return 1
            for row in rows:
                print(f"{row.kid}:{row.key}  {row.service:<14} {row.title or '-'}  {row.created_at}")
            return 0

        stats = collection.stats_local(local_names=selected)
        where = ", ".join(names) or "no selected local vault"
        print(f"{stats['keys']} keys across {stats['services']} services in {where}")
        for service, count in stats["by_service"]:
            print(f"  {service:<20} {count}")
        return 0
    finally:
        collection.close()


def _schedule(config, args) -> int:
    import signal
    import time

    from .core import schedule_host, scheduler

    action = getattr(args, "schedule_action", None) or "list"
    if action == "host":
        host_action = getattr(args, "host_action", "status")
        interval = max(1, int(getattr(args, "interval", 5) or 5))
        if host_action == "install":
            status = schedule_host.install_host_scheduler(config, interval_minutes=interval)
        elif host_action == "uninstall":
            status = schedule_host.uninstall_host_scheduler(config)
        else:
            status = schedule_host.get_host_status(config)
        state = "active" if status.active else ("installed" if status.installed else "off")
        print(f"backend={status.backend}  state={state}  interval={status.interval_minutes}m")
        if status.target_path:
            print(f"target: {status.target_path}")
        if status.detail:
            print(f"detail: {status.detail}")
        return 0 if status.supported else 1

    store = scheduler.ScheduleStore(scheduler.schedule_store_path(config.paths.home))
    if action == "list":
        jobs = store.list_jobs()
        if not jobs:
            print("No scheduled jobs.")
            return 0
        for job in jobs:
            due = scheduler.format_local_time(job.next_attempt_at or job.run_at)
            state = job.status if job.enabled else "paused"
            extra = f" [{job.episode_expr}]" if job.episode_expr else ""
            print(f"{job.id:<10} {state:<16} {due:<18} {job.service_id:<14} {job.name}{extra}")
        return 0

    from . import services
    from .core.service import registry

    config.paths.ensure()
    services.load_all(config)

    if action == "run":
        results = scheduler.run_due_jobs(
            config=config,
            job_id=getattr(args, "job_id", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            service_registry=registry,
            on_log=lambda line: print(line),
        )
        if not results:
            print("No due scheduled jobs.")
            return 0
        failed = 0
        for item in results:
            print(f"{item.job.id}  {item.record.status:<16} {item.record.summary}")
            if not item.ok:
                failed += 1
        return 1 if failed else 0

    if action == "daemon":
        interval = max(5, int(getattr(args, "interval", 60) or 60))
        stopping = False

        def _stop(_sig, _frame) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGINT, _stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _stop)
        print(f"UniDL scheduler daemon running (poll every {interval}s)...")
        while not stopping:
            results = scheduler.run_due_jobs(
                config=config,
                service_registry=registry,
                on_log=lambda line: print(line),
            )
            for item in results:
                print(f"{item.job.id}  {item.record.status:<16} {item.record.summary}")
            for _ in range(interval):
                if stopping:
                    break
                time.sleep(1)
        print("UniDL scheduler daemon stopped.")
        return 0

    return 1


def _refresh_services(config, args) -> int:
    from .core.service_repo import refresh_service_repos

    results = refresh_service_repos(config, force=bool(getattr(args, "force", False)))
    if not results:
        print("No service_repos configured in unidl.yaml.")
        return 0
    failed = 0
    for res in results:
        print(f"{res.spec.name:<22} {res.action:<10} {res.detail}")
        if not res.ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
