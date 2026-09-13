from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from unidl.core.drm import CdmError
from unidl.core.exports import Document, Entry, dumps, entry_for, loads
from unidl.core.flow import Back, Emit
from unidl.core.playback import DrmInfo, Playback
from unidl.core.service import Service, ServiceRegistry
from unidl.core.titles import Title, TitleKind
from unidl.downloader import api
from unidl.tui.app import UnidlApp
from unidl.tui.session import DONE, Job, SessionController


def _entry(index: int, *, service: str = "former") -> Entry:
    return Entry(
        save_name=f"episode-{index}",
        title=Title(str(index), TitleKind.EPISODE, f"Episode {index}", service=service),
        manifest_url=f"https://example.invalid/{index}.mpd",
    )


def test_import_delivery_keeps_the_whole_export_batch() -> None:
    class App:
        size = type("Size", (), {"width": 100})()
        palette = None

    class ImportService:
        ID = "probe"
        NAME = "Probe"
        settings = {}
        ctx = type("Context", (), {})()

    document = Document(service="probe", entries=[_entry(index) for index in range(1, 4)])
    controller = SessionController(App(), ImportService(), type("Engine", (), {})())
    controller.post_log = lambda *_args: None
    controller.post_status = lambda *_args: None
    controller._on_ui = lambda *_args: True
    delivered: list[str] = []
    batch_totals: list[int | None] = []

    def deliver(playback, _presenter, _ctx):
        delivered.append(playback.save_name)
        batch_totals.append(controller.batch_total)
        controller.jobs.append(Job(playback.save_name, state=DONE))
        if controller.queue_complete:
            raise Back()

    def drive(flow, _presenter, *, on_emit):
        while True:
            try:
                ask = next(flow)
            except StopIteration:
                return
            if not isinstance(ask, Emit):
                continue
            try:
                on_emit(ask.playback)
            except Back:
                try:
                    flow.throw(Back())
                except (Back, StopIteration):
                    return

    controller.deliver = deliver
    ImportService.ctx.extras = {"initial_import": document}
    with patch("unidl.tui.session.run_flow", side_effect=drive):
        controller._drive()

    assert delivered == ["episode-1", "episode-2", "episode-3"]
    assert batch_totals == [3, 3, 3]


def test_export_round_trip_preserves_non_license_drm_and_manifest_base() -> None:
    title = Title("1", TitleKind.EPISODE, "Episode", service="probe")
    clear = Playback(
        title,
        "Clear.Episode",
        manifest_url="https://example.invalid/clear.mpd",
        drm=DrmInfo(system="widevine", clear=True),
    )
    aes = Playback(
        title,
        "AES.Episode",
        manifest_url="/tmp/authorized-master.m3u8",
        manifest_base_url="https://cdn.example.invalid/master.m3u8",
        drm=DrmInfo(hls_key="00" * 16, hls_iv="11" * 16, hls_method="AES_128"),
    )

    restored = loads(dumps(Document(service="probe", entries=[entry_for(clear), entry_for(aes)]))).playbacks()

    assert restored[0].drm is not None
    assert restored[0].drm.clear
    assert not restored[0].drm.needs_license
    assert restored[1].drm is not None
    assert restored[1].drm.hls_key == "00" * 16
    assert restored[1].drm.hls_iv == "11" * 16
    assert restored[1].drm.hls_method == "AES_128"
    assert not restored[1].drm.needs_license
    assert restored[1].manifest_base_url == "https://cdn.example.invalid/master.m3u8"


def test_import_accepts_direct_and_playlist_aes_entries_without_a_keys_field() -> None:
    document = loads(
        json.dumps(
            {
                "kind": "unidl-export",
                "version": 1,
                "service": "not-installed",
                "titles": [
                    {
                        "save_name": "Direct.Media",
                        "manifest_url": "https://example.invalid/video.mp4",
                    },
                    {
                        "save_name": "Playlist.AES128",
                        "manifest_url": "https://example.invalid/master.m3u8",
                    },
                ],
            }
        )
    )

    assert [entry.keys for entry in document.entries] == [[], []]
    assert all(playback.drm is None for playback in document.playbacks())


def test_registry_resolves_declared_legacy_id_without_alias_matching() -> None:
    class Renamed(Service):
        ID = "current"
        NAME = "Current"
        LEGACY_IDS = ("former",)
        ALIASES = ("display-alias",)

    registry = ServiceRegistry()
    registry.register(Renamed)

    assert registry.for_id("former") is Renamed
    assert registry.for_id("display-alias") is None
    assert registry.get("former") is Renamed


class _Registry:
    def __init__(self, installed=None):
        self.installed = installed
        self.built_class = None
        self.built = None

    def for_id(self, _service_id):
        return self.installed

    def build(self, service_cls, _config, _store, *, globals_scope):
        del globals_scope
        self.built_class = service_cls
        helpers = type("Helpers", (), {"ready": True})()
        context = type("Context", (), {"settings": {}, "extras": {}, "helpers": helpers})()
        self.built = service_cls(context)
        return self.built


class _Host:
    config = object()
    settings_store = object()
    globals = object()
    vault = object()
    vaults = object()

    def __init__(self, installed=None):
        self.registry = _Registry(installed)
        self.controller = None
        self.screen = None

    def register_session(self, controller):
        self.controller = controller

    def push_screen(self, screen):
        self.screen = screen


class _Controller:
    def __init__(self, _app, service, _engine):
        self.service = service

    @staticmethod
    def start_screen():
        return "import-screen"


def test_import_without_installed_service_uses_generic_context() -> None:
    document = Document(service="not-installed", service_name="Not Installed", entries=[_entry(1)])
    host = _Host()

    with patch("unidl.tui.app.Engine", return_value=object()), patch(
        "unidl.tui.session.SessionController", _Controller
    ):
        assert UnidlApp.open_import(host, document)

    assert host.registry.built.ID == "not-installed"
    assert host.registry.built._PORTABLE_IMPORT_FALLBACK
    assert host.registry.built.ctx.extras["initial_import"] is document
    assert host.screen == "import-screen"

    protected = Playback(
        Title("protected", TitleKind.EPISODE, "Protected", service="not-installed"),
        "Protected.WEB-DL",
        manifest_url="https://example.invalid/protected.mpd",
        drm=DrmInfo(system="widevine", pssh="AAAA"),
    )
    protected.drm.context["license_tracks"] = [object()]
    with pytest.raises(CdmError, match="licence-bound DRM"):
        host.registry.built.get_keys(protected)
    protected.drm.context["license_tracks"] = []
    assert host.registry.built.get_keys(protected) == []


def test_import_with_installed_service_keeps_real_service_class() -> None:
    class Installed(Service):
        ID = "installed"
        NAME = "Installed"

        def prepare_download(self, playback, log):
            del playback, log

    document = Document(service="installed", entries=[_entry(1, service="installed")])
    host = _Host(Installed)

    with patch("unidl.tui.app.Engine", return_value=object()), patch(
        "unidl.tui.session.SessionController", _Controller
    ):
        assert UnidlApp.open_import(host, document)

    assert host.registry.built_class is Installed
    assert isinstance(host.controller.service, Installed)


def test_embedding_api_preserves_service_owned_hls_decryptor() -> None:
    decryptor = object()
    with patch("unidl.downloader.api.cli._download", return_value=0) as run:
        assert api.download(
            api.DownloadOptions(
                input="https://example.invalid/manifest.m3u8",
                custom_hls_method="AES_128",
                hls_decryptor=decryptor,
            )
        ) == 0

    assert run.call_args.args[0].hls_decryptor is decryptor
