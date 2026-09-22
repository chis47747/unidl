from unidl.core.delivery import DeliverySource, ParsePolicy, ParseRequest
from unidl.downloader.backend import NativeDownloaderBackend
from unidl.downloader.models import StreamInfo


def _manifest(*streams: StreamInfo):
    downloader = NativeDownloaderBackend()
    request = ParseRequest(
        DeliverySource.from_json({"video_tracks": [], "audio_tracks": []}),
        ParsePolicy(),
    )
    return downloader.adopt(request, list(streams))


def test_profile_merge_deduplicates_signed_urls_for_same_audio():
    downloader = NativeDownloaderBackend()
    first = _manifest(
        StreamInfo(
            manifest_type="dash",
            media_type="audio",
            url="https://cdn.test/a?token=one",
            language="en",
            codecs="mp4a.40.2",
            channels="2",
            bandwidth=128000,
            encrypted=True,
            encryption_scheme="CENC",
            extra={"key_ids": ["a" * 32]},
        )
    )
    second = _manifest(
        StreamInfo(
            manifest_type="dash",
            media_type="audio",
            url="https://cdn.test/a?token=two",
            language="en",
            codecs="mp4a.40.2",
            channels="2",
            bandwidth=128000,
            encrypted=True,
            encryption_scheme="CENC",
            extra={"key_ids": ["a" * 32]},
        )
    )
    merged = downloader.merge([first, second])
    assert len(downloader.streams(merged)) == 1
    assert downloader.streams(merged)[0].url.endswith("token=one")


def test_profile_merge_keeps_hdr_and_sdr_video_tracks_even_when_urls_match():
    downloader = NativeDownloaderBackend()
    common = dict(
        manifest_type="dash",
        media_type="video",
        url="https://cdn.test/video.m4v",
        resolution="3840x2160",
        codecs="hev1.2.4.L150",
        bandwidth=12000000,
        encrypted=True,
        encryption_scheme="CENC",
    )
    hdr = StreamInfo(**common, video_range="HDR10")
    sdr = StreamInfo(**common, video_range="SDR")
    merged = downloader.merge([_manifest(hdr), _manifest(sdr)])
    assert {stream.video_range for stream in downloader.streams(merged)} == {"HDR10", "SDR"}


def test_profile_merge_groups_all_video_before_audio_and_subtitles():
    downloader = NativeDownloaderBackend()
    streams = [
        StreamInfo(manifest_type="dash", media_type="video", url="v1", bandwidth=9),
        StreamInfo(manifest_type="dash", media_type="audio", url="a1", bandwidth=100),
        StreamInfo(manifest_type="dash", media_type="video", url="v2", bandwidth=8),
        StreamInfo(manifest_type="dash", media_type="subtitle", url="s1", bandwidth=1),
    ]
    merged = downloader.merge([_manifest(*streams)])
    assert [stream.media_type for stream in downloader.streams(merged)] == [
        "video", "video", "audio", "subtitle"
    ]
