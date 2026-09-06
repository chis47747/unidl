from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_clean_config_is_portable_and_uses_only_project_state(tmp_path, monkeypatch):
    import unidl
    from unidl.core.config import Config

    assert unidl.__version__ == "2.0.1"
    monkeypatch.chdir(tmp_path)
    config = Config.load(ROOT / "unidl.yaml")
    assert config.paths.home == ROOT
    assert config.paths.keys_db == ROOT / "db" / "keys.db"
    assert all(str(path).startswith(str(ROOT)) for path in config.devices.values())


def test_retired_home_config_is_rejected(tmp_path, monkeypatch):
    import unidl.core.config as config_module

    legacy = tmp_path / "legacy" / "unidl.yaml"
    legacy.parent.mkdir()
    legacy.write_text("services: {}\n", encoding="utf-8")
    link = tmp_path / "link.yaml"
    link.symlink_to(legacy)
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", legacy)

    with pytest.raises(ValueError, match="retired config location"):
        config_module.Config.load(legacy)
    with pytest.raises(ValueError, match="retired config location"):
        config_module.Config.load(link)


def test_registers_expected_service_and_drm_systems():
    from unidl import services
    from unidl.core import drm

    assert services.load_all() == 1
    assert [service.ID for service in services.registry.all()] == ["bbc"]
    assert drm.ids() == ["widevine", "playready", "monalisa"]


def test_cli_services_smoke():
    result = subprocess.run(
        [sys.executable, "-m", "unidl", "--config", str(ROOT / "unidl.yaml"), "services"],
        cwd=ROOT,
        env={"PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("bbc")


def test_detached_log_follow_callback_is_safe_after_screen_pop():
    from unidl.tui.logpane import SelectableLog

    log = SelectableLog()
    assert log.has_selection is False
    log._follow_tail()


def test_example_service_scaffold_is_import_safe_and_unregistered():
    from unidl.services import registry
    from unidl.services.example import Example

    assert Example.ID == "example"
    assert Example.SUPPORTS_SEARCH is True
    assert (SRC / "unidl" / "services" / "example" / "__init__.py").is_file()
    assert (SRC / "unidl" / "services" / "example" / "api.py").is_file()
    assert registry.get("example") is None
