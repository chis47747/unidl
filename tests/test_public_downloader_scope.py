"""Keep public delivery independent of optional private service bindings."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from unidl.downloader import live_rules


def test_public_build_does_not_ship_optional_service_decryptors():
    for module in ("applemusic_decrypt", "qobuz", "youku", "deezer"):
        assert importlib.util.find_spec(f"unidl.downloader.{module}") is None


def test_public_live_rules_have_no_private_provider_dispatch():
    source = Path(live_rules.__file__).read_text().lower()
    assert "tencentvideo" not in source
    assert "yangshipin" not in source
    assert "_is_ysp" not in source


def test_iqiyi_separate_audio_mapping_is_retained():
    video = SimpleNamespace(media_type="video", group_id="iq-audio", extra={})
    audio = SimpleNamespace(media_type="audio", group_id="iq-audio", extra={})
    assert live_rules.iqiyi_separate_audio_mux_media_type(video, [video, audio]) == "video"
    assert live_rules.iqiyi_separate_audio_mux_media_type(video, [video]) is None


def test_youtube_live_fragment_rules_are_retained():
    stream = SimpleNamespace(is_live=True, manifest_type="sabr_ump", extension="mp4", extra={})
    assert live_rules.should_treat_live_stream_as_fragmented_mp4(stream)
    segment = SimpleNamespace(index=0)
    assert live_rules.live_pipe_media_part_contains_init(stream, segment)
