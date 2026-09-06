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


if __name__ == "__main__":
    raise SystemExit(main())
