from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from unidl.core.drm import CdmError
from unidl.core.exports import ExportError, loads
from unidl.core.playback import DrmInfo
from unidl.core.titles import TitleKind
from unidl.tui.app import UnidlApp, _portable_import_service_class


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unshackle_document(*, service: str = "amazon", tracks: dict | None = None) -> str:
    tracks = tracks or {
        "video-1": {
            "type": "Video",
            "id": "video-1",
            "url": "https://media.example/video.m3u8",
            "descriptor": "HLS",
            "codec": "AVC",
            "width": 1920,
            "height": 1080,
            "language": "en",
            "keys": {"{00112233-4455-6677-8899-aabbccddeeff}": "AA" * 16},
            "drm": [{"system": "Widevine", "pssh_b64": _b64(b"pssh")}],
        },
        "audio-1": {
            "type": "Audio",
            "id": "audio-1",
            "url": "https://media.example/audio.m3u8",
            "descriptor": "HLS",
            "codec": "AAC",
            "language": "en",
        },
        "subtitle-1": {
            "type": "Subtitle",
            "id": "subtitle-1",
            "url": "https://media.example/en.vtt",
            "descriptor": "URL",
            "codec": "WebVTT",
            "language": "en",
        },
    }
    return json.dumps(
        {
            "version": 2,
            "service": service,
            "region": "US",
            "titles": {
                "episode-1": {
                    "meta": {
                        "type": "episode",
                        "id": "episode-1",
                        "series_title": "Example Show",
                        "name": "Pilot",
                        "season": 1,
                        "number": 2,
                        "year": 2026,
                        "language": "en",
                    },
                    "manifest_type": "HLS",
                    "tracks": tracks,
                    "chapters": [
                        {"timestamp": "00:00:01.250", "name": "Intro"},
                        {"timestamp": "00:00:00", "name": ""},
                    ],
                }
            },
        }
    )


def _mediaexport_document(*, titles: list[dict] | None = None) -> str:
    return json.dumps({
        "kind": "mediaexport",
        "version": 1,
        "generator": {"app": "test"},
        "service": {"tag": "example", "name": "Example"},
        "titles": titles or [{
            "id": "movie-1",
            "kind": "movie",
            "title": "Example Movie",
            "manifests": [{
                "url": "https://cdn.example/manifest.mpd?profile=main",
                "type": "dash",
                "role": "primary",
                "headers": {"User-Agent": "test", "Cookie": "must-drop", "Authorization": "must-drop"},
            }, {
                "url": "https://cdn.example/manifest.mpd?profile=hevc",
                "type": "dash",
                "role": "extra",
            }],
            "drm": [{"system": "widevine", "pssh": "cHNzaA=="}],
            "keys": {"00112233445566778899aabbccddeeff": "aa" * 16},
            "chapters": [{"start_ms": 0, "title": "Opening"}],
            "tracks": [{"type": "video", "codec": "hevc", "selected": True}],
        }],
    })


def test_mediaexport_v1_is_generic_only_and_keeps_settled_fields() -> None:
    document = loads(_mediaexport_document())
    assert not document.is_native
    assert document.source_format == "mediaexport"
    entry = document.entries[0]
    assert entry.manifest_url.endswith("profile=main")
    assert entry.alternate_manifest_urls == ("https://cdn.example/manifest.mpd?profile=hevc",)
    assert entry.merge_manifests is True
    assert entry.headers == {"User-Agent": "test"}
    assert entry.keys == ["00112233445566778899aabbccddeeff:" + "aa" * 16]
    assert entry.chapters[0].start_ms == 0
    assert entry.playback().drm.system == "widevine"


def test_mediaexport_direct_url_title_uses_generic_json_source() -> None:
    document = loads(_mediaexport_document(titles=[{
        "id": "song-1", "kind": "song", "title": "Song",
        "tracks": [{"id": "audio-1", "type": "audio", "codec": "aac", "language": "en",
                    "url": "https://cdn.example/song.m4a"}],
    }]))
    entry = document.entries[0]
    assert entry.manifest_url == ""
    assert entry.json_manifest is not None
    assert entry.json_manifest["audio_tracks"][0]["url"].endswith("song.m4a")


def test_mediaexport_side_loaded_subtitle_merges_with_primary_manifest() -> None:
    document = loads(_mediaexport_document(titles=[{
        "id": "movie-1", "kind": "movie", "title": "Movie",
        "manifests": [{"url": "https://cdn.example/main.mpd", "type": "dash"}],
        "tracks": [{"id": "sub-en", "type": "subtitle", "codec": "vtt", "language": "en",
                     "url": "https://cdn.example/en.vtt"}],
    }]))
    entry = document.entries[0]
    assert entry.manifest_url == ""
    assert entry.alternate_manifest_urls == ("https://cdn.example/main.mpd",)
    assert entry.json_manifest["subtitle_tracks"][0]["url"].endswith("en.vtt")


def test_mediaexport_rejects_conflicting_keys() -> None:
    with pytest.raises(ExportError, match="conflicting keys"):
        loads(_mediaexport_document(titles=[{
            "id": "1", "kind": "movie", "title": "Bad",
            "manifests": [{"url": "https://cdn.example/a.mpd"}],
            "keys": {"00112233445566778899aabbccddeeff": "aa" * 16,
                     "{00112233-4455-6677-8899-aabbccddeeff}": "bb" * 16},
        }]))


def test_mediaexport_rejects_unsupported_future_version() -> None:
    raw = json.loads(_mediaexport_document())
    raw["version"] = 2
    with pytest.raises(ExportError, match="newer"):
        loads(json.dumps(raw))


def test_mediaexport_refuses_unknown_critical_extension() -> None:
    raw = json.loads(_mediaexport_document())
    raw["titles"][0]["crit"] = ["x-frozen-segments"]
    raw["titles"][0]["x-frozen-segments"] = {"segments": []}
    with pytest.raises(ExportError, match="unsupported field"):
        loads(json.dumps(raw))


def test_mediaexport_rejects_duplicate_title_ids() -> None:
    raw = json.loads(_mediaexport_document())
    duplicate = dict(raw["titles"][0])
    raw["titles"].append(duplicate)
    with pytest.raises(ExportError, match="duplicate title id"):
        loads(json.dumps(raw))


def test_unshackle_v2_is_converted_to_a_generic_playback() -> None:
    document = loads(_unshackle_document())

    assert not document.is_native
    assert document.source_format == "unshackle-v2"
    entry = document.entries[0]
    assert entry.title is not None
    assert entry.title.kind is TitleKind.EPISODE
    assert entry.title.name == "Example Show"
    assert entry.title.episode_name == "Pilot"
    assert entry.title.season == 1
    assert entry.title.episode == 2
    assert entry.keys == ["00112233445566778899aabbccddeeff:" + "aa" * 16]
    assert [chapter.start_ms for chapter in entry.chapters] == [1250]
    assert entry.json_manifest is not None
    assert entry.json_manifest["_unidl_lazy_hls"] is True
    assert entry.json_manifest["video_tracks"][0]["manifest_url"].endswith("video.m3u8")
    assert entry.json_manifest["video_tracks"][0]["key_id"] == "00112233445566778899aabbccddeeff"
    assert entry.json_manifest["subtitle_tracks"][0]["url"].endswith("en.vtt")
    assert "source region US" in entry.note


def test_v2_track_with_multiple_keys_retains_all_kids_for_inventory() -> None:
    tracks = {
        "video-1": {
            "type": "Video",
            "url": "https://media.example/video.mp4",
            "descriptor": "URL",
            "keys": {
                "00112233445566778899aabbccddeeff": "aa" * 16,
                "ffeeddccbbaa99887766554433221100": "bb" * 16,
            },
        }
    }
    entry = loads(_unshackle_document(tracks=tracks)).entries[0]
    assert entry.json_manifest is not None
    track = entry.json_manifest["video_tracks"][0]
    assert track["key_id"] == "00112233445566778899aabbccddeeff"
    assert track["key_ids"] == [
        "00112233445566778899aabbccddeeff",
        "ffeeddccbbaa99887766554433221100",
    ]
    from unidl.downloader.parsers.json_manifest import parse_json

    parsed = parse_json("third-party.json", json.dumps(entry.json_manifest))
    assert parsed[0].extra["key_ids"] == track["key_ids"]


def test_json_manifest_expands_hls_subtitles_and_keeps_wrapper_metadata() -> None:
    from unidl.downloader.loader import Resource
    from unidl.downloader.parsers.json_manifest import parse_json

    subtitle_manifest = "https://media.example/subtitles/en.m3u8"
    playlist = "\n".join(
        (
            "#EXTM3U",
            "#EXT-X-TARGETDURATION:4",
            "#EXTINF:4.0,",
            "segment-1.vtt",
            "#EXT-X-ENDLIST",
        )
    )
    document = {
        "title": "Portable subtitles",
        "subtitle_tracks": [
            {
                "id": "subtitle-en-sdh",
                "manifest_url": subtitle_manifest,
                "language": "en",
                "name": "English SDH",
                "codec": "WebVTT",
                "forced": True,
                "sdh": True,
            }
        ],
    }
    resource = Resource(
        source=subtitle_manifest,
        final_url=subtitle_manifest,
        text=playlist,
    )

    with patch("unidl.downloader.loader.load_text", return_value=resource):
        streams = parse_json("third-party.json", json.dumps(document))

    assert len(streams) == 1
    subtitle = streams[0]
    assert subtitle.media_type == "subtitle"
    assert subtitle.language == "en"
    assert subtitle.name == "English SDH"
    assert subtitle.codecs == "WebVTT"
    assert subtitle.role == "Forced SDH"
    assert subtitle.segments[0].url.endswith("segment-1.vtt")


def test_json_manifest_nested_hls_keeps_exported_key_inventory() -> None:
    from unidl.downloader.loader import Resource
    from unidl.downloader.parsers.json_manifest import parse_json

    video_manifest = "https://media.example/video/main.m3u8"
    playlist = "\n".join(
        (
            "#EXTM3U",
            "#EXT-X-TARGETDURATION:4",
            "#EXTINF:4.0,",
            "segment-1.m4s",
            "#EXT-X-ENDLIST",
        )
    )
    key_ids = [
        "00112233445566778899aabbccddeeff",
        "ffeeddccbbaa99887766554433221100",
    ]
    document = {
        "title": "Portable video",
        "video_tracks": [
            {
                "id": "video-main",
                "manifest_url": video_manifest,
                "key_ids": key_ids,
            }
        ],
    }
    resource = Resource(
        source=video_manifest,
        final_url=video_manifest,
        text=playlist,
    )

    with patch("unidl.downloader.loader.load_text", return_value=resource):
        streams = parse_json("third-party.json", json.dumps(document))

    assert len(streams) == 1
    assert streams[0].media_type == "video"
    assert streams[0].extra["key_ids"] == key_ids
    assert streams[0].extra["key_id"] == key_ids[0]


def test_third_party_hls_manifest_is_lazy_until_track_download() -> None:
    from unidl.downloader.parsers.json_manifest import parse_json

    document = {
        "title": "Lazy export",
        "_unidl_lazy_hls": True,
        "video_tracks": [
            {
                "id": "video-1080",
                "manifest_url": "https://media.example/video.m3u8",
                "codec": "HEVC",
                "width": 1920,
                "height": 1080,
            }
        ],
        "subtitle_tracks": [
            {
                "id": "subtitle-en",
                "manifest_url": "https://media.example/subs/en.m3u8",
                "language": "en",
                "codec": "WebVTT",
            }
        ],
    }
    with patch("unidl.downloader.loader.load_text") as load:
        streams = parse_json("third-party.json", json.dumps(document))
    load.assert_not_called()
    assert [stream.media_type for stream in streams] == ["video", "subtitle"]
    assert all(stream.manifest_type == "hls" for stream in streams)
    assert all(stream.segments == [] for stream in streams)


def test_ec3_joc_export_uses_ddp_atmos_release_audio_name() -> None:
    from unidl.core import naming
    from unidl.downloader.parsers.json_manifest import parse_json

    document = {
        "title": "Mayday",
        "_unidl_lazy_hls": True,
        "audio_tracks": [
            {
                "id": "audio-en",
                "manifest_url": "https://media.example/audio.m3u8",
                "codec": "EC3",
                "channels": "5.1",
                "joc": 16,
            }
        ],
    }
    streams = parse_json("third-party.json", json.dumps(document))
    assert naming.audio_of(streams) == "DDP5.1.Atmos"


def test_playready_and_clearkey_names_are_recognised() -> None:
    tracks = {
        "v": {
            "type": "Video",
            "url": "https://media.example/v.mp4",
            "descriptor": "URL",
            "drm": [{"system": "PlayReady", "pssh_b64": _b64(b"pro")}],
            "keys": {"00112233445566778899aabbccddeeff": "bb" * 16},
        },
        "a": {
            "type": "Audio",
            "url": "https://media.example/a.mp4",
            "descriptor": "URL",
            "drm": [{"system": "ClearKeyCENC"}],
        },
    }
    entry = loads(_unshackle_document(service="vudu", tracks=tracks)).entries[0]
    playback = entry.playback()
    assert playback.drm is not None
    assert playback.drm.system == "playready"
    assert playback.keys == ["00112233445566778899aabbccddeeff:" + "bb" * 16]

    clear_key = loads(
        _unshackle_document(
            tracks={
                "v": {
                    "type": "Video",
                    "url": "https://media.example/v.mp4",
                    "descriptor": "URL",
                    "drm": [{"system": "ClearKeyCENC"}],
                    "keys": {"00112233445566778899aabbccddeeff": "cc" * 16},
                }
            }
        )
    ).entries[0].playback()
    assert clear_key.drm is not None
    assert clear_key.drm.system == "clearkeycenc"


def test_conflicting_duplicate_kids_fail_closed() -> None:
    tracks = {
        "v1": {
            "type": "Video",
            "url": "https://media.example/1.mp4",
            "keys": {"00112233445566778899aabbccddeeff": "aa" * 16},
        },
        "v2": {
            "type": "Video",
            "url": "https://media.example/2.mp4",
            "keys": {"00112233445566778899aabbccddeeff": "bb" * 16},
        },
    }
    with pytest.raises(ExportError, match="conflicting keys"):
        loads(_unshackle_document(tracks=tracks))


def test_dash_export_keeps_additional_authorized_manifests() -> None:
    tracks = {
        "avc": {
            "type": "Video",
            "url": "https://media.example/avc.mpd",
            "descriptor": "DASH",
            "codec": "AVC",
        },
        "hevc": {
            "type": "Video",
            "url": "https://media.example/hevc.mpd",
            "descriptor": "DASH",
            "codec": "HEVC",
        },
    }
    raw = json.loads(_unshackle_document(tracks=tracks))
    raw["titles"]["episode-1"]["manifest_url"] = "https://media.example/avc.mpd"
    raw["titles"]["episode-1"]["manifest_type"] = "DASH"
    entry = loads(json.dumps(raw)).entries[0]
    assert entry.manifest_url.endswith("avc.mpd")
    assert entry.alternate_manifest_urls == ("https://media.example/hevc.mpd",)
    assert entry.merge_manifests is True

    service_cls = _portable_import_service_class(loads(json.dumps(raw)))
    variants = service_cls.manifest_variants(
        None, entry.playback(), lambda _message: None
    )
    assert [variant.manifest_url for variant in variants] == [
        "https://media.example/hevc.mpd"
    ]
    assert all(not variant.merge_manifests for variant in variants)
    assert all(not variant.alternate_manifest_urls for variant in variants)


def test_foreign_export_never_looks_up_an_installed_service() -> None:
    class Registry:
        def __init__(self):
            self.lookups: list[str] = []
            self.built = None

        def for_id(self, value):
            self.lookups.append(value)
            raise AssertionError("third-party import queried the source service")

        def build(self, service_cls, _config, _store, *, globals_scope):
            del globals_scope
            self.built = service_cls(
                type(
                    "Context",
                    (),
                    {"settings": {}, "extras": {}, "helpers": type("H", (), {"ready": True})()},
                )()
            )
            return self.built

    class Host:
        config = object()
        settings_store = object()
        globals = object()
        vault = object()
        vaults = object()

        def __init__(self):
            self.registry = Registry()
            self.controllers = []
            self.screens = []

        def register_session(self, controller):
            self.controllers.append(controller)

        def push_screen(self, screen):
            self.screens.append(screen)

    class Controller:
        def __init__(self, _app, service, _engine):
            self.service = service

        @staticmethod
        def start_screen():
            return "portable"

    host = Host()
    document = loads(_unshackle_document(service="amazon"))
    with (
        patch("unidl.tui.app.Engine", return_value=object()),
        patch("unidl.tui.session.SessionController", Controller),
    ):
        assert UnidlApp.open_import(host, document)
    assert host.registry.lookups == []
    assert host.registry.built is not None
    assert host.registry.built.ID == "third-party-import"
    assert host.registry.built._EXPORT_SOURCE_ID == "amazon"
    assert host.registry.built._PORTABLE_IMPORT_FALLBACK is True


def test_generic_service_exposes_only_exported_manifest_variants() -> None:
    document = loads(_unshackle_document())
    service_cls = _portable_import_service_class(document)
    playback = document.entries[0].playback()
    assert service_cls.manifest_variants(None, playback, lambda _message: None) == []


def test_keyless_protected_export_cannot_fall_through_to_a_license() -> None:
    document = loads(
        _unshackle_document(
            tracks={
                "v": {
                    "type": "Video",
                    "url": "https://media.example/v.mpd",
                    "descriptor": "DASH",
                    "drm": [{"system": "Widevine", "pssh_b64": _b64(b"pssh")}],
                }
            }
        )
    )
    service_cls = _portable_import_service_class(document)
    playback = document.entries[0].playback()
    assert isinstance(playback.drm, DrmInfo)
    playback.drm.context["license_tracks"] = [object()]
    with pytest.raises(CdmError, match="No service or licence request was attempted"):
        service_cls.get_keys(None, playback)


def test_unshackle_legacy_flat_tracks_keep_direct_urls_and_dash_manifests() -> None:
    document = loads(
        json.dumps(
            {
                "Example Film (2024)": {
                    "VID | H.264 | SDR | 1920x1080 | 5000 kb/s": {
                        "type": "video",
                        "url": "https://media.example/video.mp4",
                        "descriptor": "DASH",
                        "keys": {
                            "00112233445566778899aabbccddeeff": "aa" * 16,
                        },
                    },
                    "AUD | AAC | 2.0 | en": {
                        "url": "https://media.example/audio.mp4",
                        "descriptor": "URL",
                    },
                    "SUB | SRT | en-US": {
                        "type": "text",
                        "url": "https://media.example/sub.srt",
                        "descriptor": "URL",
                    },
                }
            }
        )
    )
    assert document.source_format == "unshackle-legacy-tracks"
    entry = document.entries[0]
    assert entry.json_manifest is not None
    assert entry.json_manifest["video_tracks"][0]["url"].endswith("video.mp4")
    assert "manifest_url" not in entry.json_manifest["video_tracks"][0]
    assert entry.keys == ["00112233445566778899aabbccddeeff:" + "aa" * 16]
    assert entry.title is not None
    assert entry.title.year == "2024"


def test_unshackle_legacy_series_converts_nested_episode_tracks() -> None:
    document = loads(
        json.dumps(
            {
                "id": "series-1",
                "service": "AppleTVPlus",
                "name": "Example Series",
                "type": "series",
                "seasons": {
                    "1": {
                        "episodes": {
                            "2": {
                                "id": "episode-2",
                                "episode_name": "Second",
                                "tracks": {
                                    "videos": {
                                        "1080p": [
                                            {
                                                "url": "https://media.example/e2.m3u8",
                                                "manifest_url": "https://media.example/e2-master.m3u8",
                                                "codec": "H.264",
                                                "quality": "1080p",
                                            }
                                        ]
                                    },
                                    "audios": {
                                        "en - English": [
                                            {
                                                "url": "https://media.example/e2-audio.m3u8",
                                                "manifest_url": "https://media.example/e2-audio-master.m3u8",
                                            }
                                        ]
                                    },
                                    "subtitles": {
                                        "en - English": ["https://media.example/e2.vtt"]
                                    },
                                    "keys": [
                                        "00112233445566778899aabbccddeeff:" + "bb" * 16
                                    ],
                                },
                            }
                        }
                    }
                },
            }
        )
    )
    assert document.source_format == "unshackle-legacy-series"
    entry = document.entries[0]
    assert entry.title is not None
    assert entry.title.kind is TitleKind.EPISODE
    assert entry.title.season == 1
    assert entry.title.episode == 2
    assert entry.json_manifest is not None
    assert len(entry.json_manifest["video_tracks"]) == 1
    assert len(entry.json_manifest["audio_tracks"]) == 1
    assert len(entry.json_manifest["subtitle_tracks"]) == 1
    assert entry.keys == ["00112233445566778899aabbccddeeff:" + "bb" * 16]


def test_legacy_series_fills_missing_track_fields_from_group_labels() -> None:
    document = loads(
        json.dumps(
            {
                "service": "Example",
                "name": "Label fallback",
                "seasons": {
                    "1": {
                        "episodes": {
                            "1": {
                                "tracks": {
                                    "videos": {
                                        "1080p | H.264 | 23.976 FPS": [
                                            {"url": "https://media.example/v.mp4"}
                                        ]
                                    },
                                    "audios": {
                                        "AAC | 2.0 | en - English": [
                                            {"url": "https://media.example/a.m4a"}
                                        ]
                                    },
                                    "subtitles": {},
                                }
                            }
                        }
                    }
                },
            }
        )
    )
    manifest = document.entries[0].json_manifest
    assert manifest is not None
    video = manifest["video_tracks"][0]
    audio = manifest["audio_tracks"][0]
    assert video["codec"] == "H.264"
    assert video["resolution"] == "1080p"
    assert video["fps"] == "23.976"
    assert audio["codec"] == "AAC"
    assert audio["channels"] == "2.0"


def test_metadata_only_legacy_series_is_rejected() -> None:
    with pytest.raises(ExportError, match="no downloadable tracks"):
        loads(
            json.dumps(
                {
                    "id": "series-1",
                    "service": "AppleTVPlus",
                    "name": "Metadata only",
                    "type": "series",
                    "seasons": {"1": {"episodes": {"1": {"tracks": {}}}}},
                }
            )
        )
