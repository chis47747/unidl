from pathlib import Path
from types import SimpleNamespace

from unidl.core.engine import Engine, TrackSet
from unidl.downloader import hybrid
from unidl.downloader.models import StreamInfo


def _track(path: Path, value: str, bandwidth: int):
    stream = StreamInfo(
        manifest_type="dash",
        media_type="video",
        resolution="3840x2160",
        video_range=value,
        codecs="hev1.2.4.L150",
        bandwidth=bandwidth,
    )
    return SimpleNamespace(offset=1, stream=stream, path=path, cleanup_paths=[])


def test_hybrid_disabled_keeps_tracks():
    tracks = [_track(Path("hdr.mkv"), "HDR10", 100), _track(Path("dv.mkv"), "DV", 90)]
    assert hybrid.process_hybrid_tracks(tracks, enabled=False) == tracks


def test_hybrid_requires_matching_ranges(monkeypatch, tmp_path):
    tracks = [_track(tmp_path / "hdr.mkv", "HDR10", 100), _track(tmp_path / "sdr.mkv", "SDR", 90)]
    assert hybrid.process_hybrid_tracks(tracks, enabled=True, temp_dir=tmp_path) == tracks


def test_hybrid_combines_matching_pair(monkeypatch, tmp_path):
    hdr = _track(tmp_path / "hdr.mkv", "HDR10", 100)
    dv = _track(tmp_path / "dv.mkv", "DV", 90)
    monkeypatch.setattr(hybrid.shutil, "which", lambda name: "/bin/tool")
    probe = {"r_frame_rate": "24/1", "nb_read_packets": "10", "width": "3840", "height": "2160", "codec_name": "hevc"}
    monkeypatch.setattr(hybrid, "_probe", lambda source, tools: probe)
    def fake_run(args, label):
        target = Path(args[args.index("-o") + 1]) if "-o" in args else Path(args[-1])
        target.write_bytes(b"output")
        return ""
    monkeypatch.setattr(hybrid, "_run", fake_run)
    result = hybrid.process_hybrid_tracks([hdr, dv], enabled=True, temp_dir=tmp_path)
    assert len(result) == 1
    assert result[0].stream.video_range == "DV+HDR10"
    assert result[0].path.exists()


def test_core_stages_matching_companion_only():
    dv = StreamInfo(manifest_type="dash", media_type="video", resolution="1920x1080", video_range="DV", codecs="hev1", bandwidth=900)
    hdr = StreamInfo(manifest_type="dash", media_type="video", resolution="1920x1080", video_range="HDR10", codecs="hev1", bandwidth=1000)
    sdr = StreamInfo(manifest_type="dash", media_type="video", resolution="1280x720", video_range="SDR", codecs="avc1", bandwidth=500)
    selected = Engine.ensure_hybrid_tracks(TrackSet(streams=[dv, hdr, sdr]), [dv])
    assert selected == [dv, hdr]


def test_core_uses_lowest_resolution_dv_layer_for_hdr_base():
    hdr = StreamInfo(manifest_type="dash", media_type="video", resolution="3840x2160", video_range="HDR10", codecs="hev1", bandwidth=3000)
    dv4k = StreamInfo(manifest_type="dash", media_type="video", resolution="3840x2160", video_range="DV", codecs="dvhe", bandwidth=2500)
    dv720 = StreamInfo(manifest_type="dash", media_type="video", resolution="1280x720", video_range="DV", codecs="dvhe", bandwidth=400)
    tracks = TrackSet(streams=[hdr, dv4k, dv720], merged_profiles=True)
    assert Engine.ensure_hybrid_tracks(tracks, [hdr]) == [hdr, dv720]


def test_missing_dovi_reports_count(monkeypatch):
    monkeypatch.setattr(hybrid.shutil, "which", lambda name: None if name.startswith("dovi_tool") else "/bin/tool")
    monkeypatch.setattr(hybrid.Path, "is_file", lambda self: False)
    try:
        hybrid._tools()
    except RuntimeError as exc:
        assert "1 required tool missing: dovi_tool" in str(exc)
    else:
        raise AssertionError("missing dovi_tool was not reported")
