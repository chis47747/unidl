from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlparse

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from unidl.downloader import cli, sample_aes, sample_aes_samples
from unidl.downloader.embedding import DownloadCancelled
from unidl.downloader.models import SegmentInfo, StreamInfo
from unidl.downloader.parsers.hls import _key_id_from_hls_key_uri, parse_hls
from unidl.downloader.postprocess import RawKey

KEY = "00112233445566778899aabbccddeeff"
OTHER_KEY = "102132435465768798a9bacbdcedef0f"
KID = "000000000000007b6336202020202020"
VIDEO_KID = "000000000000007b6331202020202020"
IV = bytes(range(16))


def audio_stream(**changes):
    values = dict(
        manifest_type="hls", media_type="audio", extension="aac", encrypted=True,
        encryption_scheme="SAMPLE-AES",
        segments=[SegmentInfo("https://invalid/1.aac", duration=1, index=9, encrypted=True,
                              encryption_scheme="SAMPLE-AES", key_id=KID, key_iv=IV)],
    )
    values.update(changes)
    return StreamInfo(**values)


def _download_fixture(stream, tmp_path, keys):
    args = SimpleNamespace(
        save_name="Sample", save_pattern=None, no_resume=True, no_color=True,
        force_ansi_console=False, no_decrypt=False, keep_temp=False, del_after_done=True,
        workers=1, retries=1, downloader="python", http_request_timeout=1,
        check_segments_count=True, tmp_dir=str(tmp_path), input="fixture.m3u8", repack=False,
        sub_format="srt", auto_subtitle_fix=True, decrypter="internal", vgc=False,
        vgc_keep_opaque=False, log_file_path=None, _unidown_task_temp_root=tmp_path / "task",
    )
    return cli._download_selected_stream(
        stream, 1, 1, [stream], args, headers={}, keys=keys, hls_crypto=None,
        max_speed=None, output_dir=tmp_path / "download", default_save_base=None,
        show_progress=False, status_printer=lambda message: None,
    )


@pytest.mark.parametrize("kind", range(7))
def test_apple_legacy_key_uri_maps_to_media_kid(kind):
    expected = (123).to_bytes(8, "big") + f"c{kind}".encode().ljust(8, b" ")
    assert _key_id_from_hls_key_uri(f"skd://itunes.apple.com/p123/c{kind}") == expected.hex()


@pytest.mark.parametrize("uri", [
    "skd://itunes.apple.com/p18446744073709551616/c0", "skd://itunes.apple.com/p-1/c0",
    "skd://itunes.apple.com/p123/c7", "skd://other.invalid/p123/c1",
    "skd://itunes.apple.com/p123/c1?x=1", "skd://itunes.apple.com/p123/c1#fragment",
])
def test_invalid_apple_key_identity_is_not_guessed(uri):
    assert _key_id_from_hls_key_uri(uri) is None


def test_generic_skd_uuid_still_works():
    assert _key_id_from_hls_key_uri(f"skd://{KID}") == KID


def test_playlist_preserves_rotation_clear_sections_iv_and_discontinuity():
    source = ('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:9\n'
              '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://itunes.apple.com/p123/c6",'
              f'KEYFORMAT="com.apple.streamingkeydelivery",IV=0x{IV.hex()}\n'
              '#EXTINF:1,\n1.aac\n#EXT-X-DISCONTINUITY\n'
              '#EXT-X-KEY:METHOD=NONE\n#EXTINF:1,\nclear.aac\n'
              '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://itunes.apple.com/p124/c6",'
              'KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:1,\n2.aac\n#EXT-X-ENDLIST\n')
    stream = parse_hls("https://invalid/list.m3u8", source)[0]
    assert stream.segments[0].key_id == KID
    assert stream.segments[0].key_iv == IV
    assert stream.segments[0].discontinuity_after
    assert not stream.segments[1].encrypted and stream.segments[1].key_id is None
    assert stream.segments[2].key_id != KID
    assert stream.segments[2].index == 11


@pytest.mark.parametrize("change", [
    {"encrypted": False}, {"manifest_type": "dash"}, {"extension": "m4s"},
    {"segments": [SegmentInfo("init.mp4", index=-1)]},
    {"segments": [SegmentInfo("1.ts", encrypted=True, encryption_scheme="AES-128")]},
    {"segments": [SegmentInfo("1.ts", encrypted=True, encryption_scheme="CBCS")]},
])
def test_legacy_detection_does_not_capture_other_encryption_paths(change):
    assert not sample_aes.uses_legacy_sample_aes(audio_stream(**change))


def test_selected_key_is_exact_and_conflicting_or_missing_keys_fail():
    segment = audio_stream().segments[0]
    keys = [RawKey(OTHER_KEY, VIDEO_KID), RawKey(KEY, KID)]
    assert sample_aes._segment_key(segment, keys) == bytes.fromhex(KEY)
    for bad in [[], keys[:1], [RawKey(KEY, KID), RawKey(OTHER_KEY, KID)]]:
        with pytest.raises(ValueError, match="unambiguous"):
            sample_aes._segment_key(segment, bad)
    assert sample_aes._segment_key(segment, [RawKey(KEY)]) == bytes.fromhex(KEY)
    segment.key_id = None
    with pytest.raises(ValueError, match="unambiguous"):
        sample_aes._segment_key(segment, keys)


def test_local_playlist_preserves_boundaries_without_writing_keys(tmp_path):
    stream = audio_stream(extension="eac3")
    stream.segments += [
        SegmentInfo("https://invalid/clear.aac", duration=2, index=10),
        SegmentInfo("https://invalid/2.aac", duration=1, index=11, encrypted=True,
                    encryption_scheme="SAMPLE-AES", key_id=VIDEO_KID, discontinuity_after=True),
    ]
    parts = []
    for index in range(3):
        path = tmp_path / f"part ' {index}.aac"
        path.write_bytes(b"fixture")
        parts.append(path)
    workdirs = []

    def ffmpeg(args, **kwargs):
        if "framehash" in args:
            Path(args[-1]).write_text("0, 0, 0, 1, 2048, synthetic-hash\n")
            return SimpleNamespace(returncode=0, stderr=b"")
        playlist = Path(args[args.index("-i") + 1])
        workdirs.append(playlist.parent)
        assert playlist.parent.stat().st_mode & 0o777 == 0o700
        text = playlist.read_text()
        assert "IV=" not in text and "SAMPLE-AES" not in text
        assert "#EXT-X-KEY:METHOD=NONE" in text
        assert "#EXT-X-DISCONTINUITY" in text
        assert "#EXT-X-MEDIA-SEQUENCE:9" in text
        assert "https:" not in text and "skd:" not in text
        assert args[args.index("-protocol_whitelist") + 1] == "file"
        assert KEY not in str(args) and OTHER_KEY not in str(args)
        assert not list(playlist.parent.glob("key-*.bin"))
        assert (playlist.parent / "part-1.eac3").read_bytes() == b"fixture"
        Path(args[-1]).write_bytes(b"decrypted")
        return SimpleNamespace(returncode=0)

    with (
        patch.object(sample_aes.shutil, "which", return_value="ffmpeg"),
        patch.object(sample_aes, "decrypt_eac3", return_value=b"clear") as decrypt,
        patch.object(sample_aes, "managed_run", ffmpeg),
    ):
        result = sample_aes.decrypt_sample_aes_parts(parts, stream, [RawKey(KEY, KID), RawKey(OTHER_KEY, VIDEO_KID)], tmp_path / "output.aac")
    assert [call.args for call in decrypt.call_args_list] == [
        (b"fixture", bytes.fromhex(KEY), IV),
        (b"fixture", bytes.fromhex(OTHER_KEY), (11).to_bytes(16, "big")),
    ]
    assert result.read_bytes() == b"decrypted"
    assert all(not p.exists() for p in workdirs)
    assert all(p.read_bytes() == b"fixture" for p in parts)


@pytest.mark.parametrize("failure", [RuntimeError("failed"), DownloadCancelled("cancelled")])
def test_failure_and_cancellation_remove_private_key_files_without_publishing(tmp_path, failure):
    part = tmp_path / "part.aac"
    part.write_bytes(b"fixture")
    output = tmp_path / "output.aac"
    output.write_bytes(b"previous")
    with (
        patch.object(sample_aes.shutil, "which", return_value="ffmpeg"),
        patch.object(sample_aes, "decrypt_aac", return_value=b"clear"),
        patch.object(sample_aes, "managed_run", side_effect=failure),
    ):
        with pytest.raises(type(failure)):
            sample_aes.decrypt_sample_aes_parts([part], audio_stream(), [RawKey(KEY, KID)], output)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("sample-aes-*"))


def test_download_retains_parts_until_sample_decryption_and_skips_ciphertext_assembly():
    stream = audio_stream()
    args = SimpleNamespace(no_decrypt=False, keep_temp=False, del_after_done=True)
    assert cli._download_parts_needed_for_postprocess(stream, None, None, args)
    assert cli._should_skip_assembled_download_output(stream, args, None)
    args.no_decrypt = True
    assert not cli._download_parts_needed_for_postprocess(stream, None, None, args)
    assert not cli._should_skip_assembled_download_output(stream, args, None)


def _encrypt_adts(clear: bytes, key: str, iv: bytes) -> bytes:
    # Synthetic fixture: AAC leaves the ADTS header + 16 bytes clear per frame.
    data = bytearray(clear)
    offset = 0
    while offset < len(data):
        assert data[offset] == 0xFF and data[offset + 1] & 0xF6 == 0xF0
        size = ((data[offset + 3] & 3) << 11) | (data[offset + 4] << 3) | (data[offset + 5] >> 5)
        header = 7 if data[offset + 1] & 1 else 9
        start = offset + header + 16
        count = max(0, (size - header - 16) // 16 * 16)
        encryptor = Cipher(algorithms.AES(bytes.fromhex(key)), modes.CBC(iv)).encryptor()
        data[start:start + count] = encryptor.update(bytes(data[start:start + count])) + encryptor.finalize()
        offset += size
    assert offset == len(data)
    return bytes(data)


@pytest.fixture
def encrypted_audio(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg not installed")
    clear = tmp_path / "clear.aac"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-c:a", "aac", "-f", "adts", str(clear)], check=True, capture_output=True)
    part = tmp_path / "encrypted.aac"
    part.write_bytes(_encrypt_adts(clear.read_bytes(), KEY, IV))
    return clear, part


def test_real_ffmpeg_decrypts_sample_aes_aac_to_original_packets(encrypted_audio, tmp_path):
    clear, part = encrypted_audio
    output = sample_aes.decrypt_sample_aes_parts([part], audio_stream(), [RawKey(KEY, KID)], tmp_path / "out.aac")
    assert output.read_bytes() == clear.read_bytes()


def test_real_ffmpeg_handles_multiple_audio_keys_and_ivs(encrypted_audio, tmp_path):
    clear, first = encrypted_audio
    second = tmp_path / "second.aac"
    second_iv = bytes(range(16, 32))
    second.write_bytes(_encrypt_adts(clear.read_bytes(), OTHER_KEY, second_iv))
    stream = audio_stream()
    stream.segments.append(SegmentInfo("https://invalid/2.aac", duration=1, index=10, encrypted=True,
                                      encryption_scheme="SAMPLE-AES", key_id=VIDEO_KID, key_iv=second_iv))
    output = sample_aes.decrypt_sample_aes_parts([first, second], stream, [RawKey(KEY, KID), RawKey(OTHER_KEY, VIDEO_KID)], tmp_path / "both.aac")
    assert output.read_bytes() == clear.read_bytes() * 2


def test_wrong_key_is_detected_before_publishing(encrypted_audio, tmp_path):
    _, part = encrypted_audio
    output = tmp_path / "wrong.aac"
    with pytest.raises(RuntimeError, match="decode validation failed"):
        sample_aes.decrypt_sample_aes_parts([part], audio_stream(), [RawKey(OTHER_KEY, KID)], output)
    assert not output.exists()
    assert not list(tmp_path.glob("sample-aes-*"))


def test_media_export_roundtrip_retains_sample_aes_crypto_fields():
    from unidl.downloader.backend import NativeDownloaderBackend
    from unidl.downloader.parsers.json_manifest import parse_json

    original = audio_stream()
    original.url = original.segments[0].url
    original.segments[0].key_uri = "skd://itunes.apple.com/p123/c6"
    document = NativeDownloaderBackend()._json_manifest([original], media_snapshot=True)
    restored = parse_json("https://invalid/export.json", json.dumps(document))[0]
    assert restored.segments[0].key_id == KID
    assert restored.segments[0].key_iv == IV
    assert restored.segments[0].index == 9
    assert restored.segments[0].key_uri == original.segments[0].key_uri
    assert sample_aes.uses_legacy_sample_aes(restored)


@pytest.mark.parametrize("media_type,extension", [("audio", "aac"), ("video", "ts")])
@pytest.mark.parametrize("frames,errors,accepted", [(200, 1, True), (24, 300, False), (0, 0, False)])
def test_decode_finishes_but_does_not_publish_widespread_corruption(
    tmp_path, media_type, extension, frames, errors, accepted,
):
    part = tmp_path / f"part.{extension}"
    part.write_bytes(b"fixture")
    output = tmp_path / f"output.{extension}"
    output.write_bytes(b"previous")
    messages = []
    calls = []

    def ffmpeg(args, **kwargs):
        calls.append(args)
        assert "-xerror" not in args
        if "framehash" in args:
            assert "repeat+error" in args and "explode" in args
            Path(args[-1]).write_text("0, 0, 0, 1, 2048, hash\n" * frames)
            return SimpleNamespace(returncode=0, stderr=b"Error submitting packet to decoder\n" * errors)
        Path(args[-1]).write_bytes(b"decrypted")
        return SimpleNamespace(returncode=0, stderr=b"")

    with (
        patch.object(sample_aes.shutil, "which", return_value="ffmpeg"),
        patch.object(sample_aes, "decrypt_aac", return_value=b"clear"),
        patch.object(sample_aes, "decrypt_ts", return_value=b"clear"),
        patch.object(sample_aes, "managed_run", ffmpeg),
    ):
        def decrypt():
            return sample_aes.decrypt_sample_aes_parts(
                [part], audio_stream(media_type=media_type, extension=extension),
                [RawKey(KEY, KID)], output, event_callback=messages.append,
            )
        if accepted:
            assert decrypt().read_bytes() == b"decrypted"
            assert any("warning:" in message for message in messages)
        else:
            with pytest.raises(RuntimeError, match="decode validation failed"):
                decrypt()
            assert output.read_bytes() == b"previous"
    assert len(calls) == 2
    assert not list(tmp_path.glob("sample-aes-*"))


def test_real_ffmpeg_uses_sliced_parts_without_reapplying_byte_offsets(encrypted_audio, tmp_path):
    clear, first = encrypted_audio
    second = tmp_path / "second.aac"
    second.write_bytes(first.read_bytes())
    size = first.stat().st_size
    stream = audio_stream(extra={"media_sequence": 9, "target_duration": 1})
    stream.segments[0].byte_range = (100, 100 + size - 1)
    stream.segments.append(SegmentInfo(
        "https://invalid/combined.aac", duration=1, index=10,
        byte_range=(100 + size, 100 + 2 * size - 1), encrypted=True,
        encryption_scheme="SAMPLE-AES", key_id=KID, key_iv=IV,
    ))
    output = sample_aes.decrypt_sample_aes_parts(
        [first, second], stream, [RawKey(KEY, KID)], tmp_path / "ranges.aac",
    )
    assert output.read_bytes() == clear.read_bytes() * 2


def test_incorrect_byte_range_part_is_not_decrypted(encrypted_audio, tmp_path):
    _, part = encrypted_audio
    stream = audio_stream()
    stream.segments[0].byte_range = (100, 101)
    with patch.object(sample_aes, "managed_run") as run:
        with pytest.raises(ValueError, match="byte range"):
            sample_aes.decrypt_sample_aes_parts([part], stream, [RawKey(KEY, KID)], tmp_path / "out.aac")
    run.assert_not_called()


def test_real_ffmpeg_continues_past_one_bad_audio_frame(encrypted_audio, tmp_path):
    clear, _ = encrypted_audio
    # Repeated clear packets make the error rate measurable without a long fixture.
    data = bytearray(clear.read_bytes() * 50)
    middle = len(clear.read_bytes()) * 25
    size = ((data[middle + 3] & 3) << 11) | (data[middle + 4] << 3) | (data[middle + 5] >> 5)
    data[middle + 7:middle + size] = b"\xff" * (size - 7)
    part = tmp_path / "damaged.aac"
    part.write_bytes(_encrypt_adts(bytes(data), KEY, IV))
    messages = []
    output = sample_aes.decrypt_sample_aes_parts(
        [part], audio_stream(), [RawKey(KEY, KID)], tmp_path / "out.aac",
        event_callback=messages.append,
    )
    assert output.read_bytes() == data
    assert any("warning:" in message for message in messages)


def test_native_download_path_decrypts_and_remuxes_aac(encrypted_audio, tmp_path):
    from unidl.downloader.postprocess import mux_files

    clear, part = encrypted_audio
    stream = audio_stream()
    stream.segments[0].url = str(part)
    result = _download_fixture(stream, tmp_path, [RawKey(KEY, KID)])
    assert result.path.read_bytes() == clear.read_bytes()
    assert not list((tmp_path / "task").rglob("key-*.bin"))
    assert result.temp_dir is None
    muxed = mux_files([result.path], tmp_path / "final.mkv", muxer="ffmpeg")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(muxed)], capture_output=True, check=True)
    assert json.loads(probe.stdout)["streams"][0]["codec_name"] == "aac"


@pytest.mark.parametrize("media", ["audio", "ac3", "eac3", "video", "both"])
@pytest.mark.parametrize("iv_mode", ["sequence", "fps"])
def test_bento4_sample_aes_matches_clear_decoded_frames(tmp_path, media, iv_mode):
    """An independent packager covers ID3 audio and H.264 NAL/PES boundaries."""
    from unidl.downloader.postprocess import mux_files

    if not all(shutil.which(tool) for tool in ("ffmpeg", "mp42hls")):
        pytest.skip("FFmpeg and Bento4 mp42hls required")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-g", "24", "-bf", "0", "-c:a", media if media in {"ac3", "eac3"} else "aac", str(source),
    ], check=True, capture_output=True)
    outputs = []
    for encrypted in (False, True):
        directory = tmp_path / ("encrypted" if encrypted else "clear")
        directory.mkdir()
        command = ["mp42hls", "--segment-duration", "1"]
        if media in {"audio", "ac3", "eac3"}:
            command += ["--video-track-id", "0", "--audio-format", "packed"]
        elif media == "video":
            command += ["--audio-track-id", "0"]
        if encrypted:
            command += ["--encryption-mode", "SAMPLE-AES", "--encryption-key",
                        KEY + (IV.hex() if iv_mode == "fps" else ""),
                        "--encryption-iv-mode", iv_mode]
        subprocess.run(command + [str(source)], cwd=directory, check=True, capture_output=True)
        playlist = directory / "stream.m3u8"
        if encrypted:
            stream = parse_hls(playlist.as_uri(), playlist.read_text())[0]
            if iv_mode == "fps":
                # Simulate an explicitly supplied out-of-band FairPlay IV.
                for segment in stream.segments:
                    segment.key_iv = IV
            parts = [Path(unquote(urlparse(segment.url).path)) for segment in stream.segments]
            for segment, part in zip(stream.segments, parts, strict=True):
                segment.url = str(part)
            result = _download_fixture(stream, tmp_path, [RawKey(KEY)])
            assert result.temp_dir is None
            assert not list((tmp_path / "task").rglob("key-*.bin"))
            output = mux_files([result.path], tmp_path / "final.mkv", muxer="ffmpeg")
        else:
            output = playlist
        report = directory / "frames.txt"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
            *(["-allowed_extensions", "ALL"] if not encrypted else []), "-i", str(output),
            "-map", "0:v?", "-map", "0:a?", "-fps_mode", "passthrough",
            "-f", "framehash", str(report),
        ], check=True, capture_output=True)
        frames = {}
        for line in report.read_text().splitlines():
            if not line.startswith("#"):
                fields = line.split(",")
                frames.setdefault(fields[0].strip(), []).append(fields[-1].strip())
        outputs.append(frames)
    assert outputs[0] and outputs[0] == outputs[1]


@pytest.mark.parametrize("extension", ["aac", "ac3", "ec3", "eac3"])
def test_packed_audio_media_playlist_is_not_classified_as_video(extension):
    source = ('#EXTM3U\n#EXT-X-TARGETDURATION:1\n'
              '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://itunes.apple.com/p123/c6"\n'
              f'#EXTINF:1,\nsegment.{extension}\n#EXT-X-ENDLIST\n')
    stream = parse_hls("https://invalid/audio.m3u8", source)[0]
    assert stream.media_type == "audio"
    assert sample_aes.uses_legacy_sample_aes(stream)


def _encrypt_nal(clear, key=KEY, iv=IV):
    data = bytearray(clear)
    encryptor = Cipher(algorithms.AES(bytes.fromhex(key)), modes.CBC(iv)).encryptor()
    for start in range(32, len(data) - 16, 160):
        data[start:start + 16] = encryptor.update(bytes(data[start:start + 16]))
    assert encryptor.finalize() == b""
    escaped = bytearray()
    zeroes = 0
    for byte in data:
        if zeroes == 2 and byte <= 3:
            escaped.append(3)
            zeroes = 0
        escaped.append(byte)
        zeroes = zeroes + 1 if byte == 0 else 0
    return bytes(escaped)


@pytest.mark.parametrize("length", [48, 49, 191, 192, 208, 209, 369])
def test_h264_pattern_boundaries_and_original_emulation_prevention(length):
    clear = b"\x65" + (b"\x01\x00\x00\x03\x01abc" * 50)[:length - 1]
    encrypted = _encrypt_nal(clear) if length > 48 else clear
    prefix = b"\x00\x00\x00\x01"
    assert sample_aes_samples.decrypt_h264(prefix + encrypted, bytes.fromhex(KEY), IV) == prefix + clear


def _transport_packet(pid, payload, *, begins=True, counter=0):
    assert 0 < len(payload) <= 184
    header = bytes([0x47, (pid >> 8) | (0x40 if begins else 0), pid & 255, 0x10 | counter])
    if len(payload) == 184:
        return header + payload
    size = 183 - len(payload)
    return header[:3] + bytes([0x30 | counter, size]) + (b"\x00" + b"\xff" * (size - 1) if size else b"") + payload


def _video_transport(payloads):
    # One-program PAT/PMT fixtures; the packet-level helper does not check CRC.
    pat = bytes.fromhex("00b00d0001c100000001e10000000000")
    pmt = bytes.fromhex("02b0120001c10000e101f000dbe101f00000000000")
    packets = [_transport_packet(0, b"\0" + pat), _transport_packet(256, b"\0" + pmt)]
    counter = 0
    for payload in payloads:
        pes = b"\x00\x00\x01\xe0" + (len(payload) + 8).to_bytes(2, "big") + b"\x80\x80\x05\x21\x00\x01\x00\x01" + payload
        for start in range(0, len(pes), 184):
            packets.append(_transport_packet(257, pes[start:start + 184], begins=start == 0, counter=counter))
            counter = (counter + 1) % 16
    return b"".join(packets)


def test_h264_nal_spans_pes_boundaries_and_preserves_transport_metadata():
    clear = b"\x65" + (b"\x01\x00\x00\x03\x01abc" * 50)
    encrypted = b"\x00\x00\x00\x01" + _encrypt_nal(clear)
    data = _video_transport([encrypted[:60], encrypted[60:]])
    output = sample_aes_samples.decrypt_ts(data, bytes.fromhex(KEY), IV)
    assert output[:376] == data[:376]
    assert len(output) == len(data)
    pes_packets = []
    for start in range(376, len(data), 188):
        packet = output[start:start + 188]
        assert packet[:3] == data[start:start + 3]
        assert packet[3] & 15 == data[start + 3] & 15
        offset = 5 + packet[4] if packet[3] & 0x20 else 4
        if packet[1] & 0x40:
            pes_packets.append(bytearray())
        pes_packets[-1].extend(packet[offset:])
    assert all(pes[6:14] == b"\x80\x80\x05\x21\x00\x01\x00\x01" for pes in pes_packets)
    assert all(int.from_bytes(pes[4:6], "big") == len(pes) - 6 for pes in pes_packets)
    assert b"".join(pes[14:] for pes in pes_packets) == b"\x00\x00\x00\x01" + clear


def test_aac_keeps_id3_and_resets_iv_per_frame(encrypted_audio):
    clear, encrypted = encrypted_audio
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x04test"
    assert sample_aes_samples.decrypt_aac(tag + encrypted.read_bytes(), bytes.fromhex(KEY), IV) == tag + clear.read_bytes()


@pytest.mark.parametrize("data", [b"bad", b"ID3", b"ID3\x04\x00\x00\x00\x00\x00\x20", b"\xff\xf1\x50\x80\x80\x1f\xfc"])
def test_truncated_audio_is_not_silently_dropped(data):
    with pytest.raises(ValueError):
        sample_aes_samples.decrypt_aac(data, bytes.fromhex(KEY), IV)


def test_decode_report_counts_ffmpeg_decoding_error_variant(tmp_path):
    report = tmp_path / "frames"
    report.write_text("0, 0, 0, 1, 2, hash\n")
    assert sample_aes._decode_report(report, b"[dec:h264] Decoding error: Invalid data\n") == (1, 1)


def test_aac_native_stage_binds_keys_and_ivs_before_ffmpeg(tmp_path):
    stream = audio_stream()
    stream.segments.append(SegmentInfo(
        "https://invalid/2.aac", duration=1, index=10, encrypted=True,
        encryption_scheme="SAMPLE-AES", key_id=VIDEO_KID,
    ))
    parts = [tmp_path / "first.aac", tmp_path / "second.aac"]
    for part in parts:
        part.write_bytes(b"encrypted")

    def run(args, **kwargs):
        if "framehash" in args:
            Path(args[-1]).write_text("0, 0, 0, 1, 2, hash\n")
        else:
            playlist = Path(args[args.index("-i") + 1])
            assert "METHOD=SAMPLE-AES" not in playlist.read_text()
            assert not list(playlist.parent.glob("key-*.bin"))
            assert all(p.read_bytes() == b"clear" for p in playlist.parent.glob("part-*.aac"))
            Path(args[-1]).write_bytes(b"clear")
        return SimpleNamespace(returncode=0, stderr=b"")

    with (
        patch.object(sample_aes.shutil, "which", return_value="ffmpeg"),
        patch.object(sample_aes, "decrypt_aac", return_value=b"clear") as decrypt,
        patch.object(sample_aes, "managed_run", run),
    ):
        sample_aes.decrypt_sample_aes_parts(parts, stream, [RawKey(KEY, KID), RawKey(OTHER_KEY, VIDEO_KID)], tmp_path / "out.aac")
    assert [call.args[1:] for call in decrypt.call_args_list] == [
        (bytes.fromhex(KEY), IV), (bytes.fromhex(OTHER_KEY), (10).to_bytes(16, "big")),
    ]


def test_native_sample_cancellation_does_not_publish_or_leave_workdir(encrypted_audio, tmp_path):
    _, part = encrypted_audio
    output = tmp_path / "out.aac"
    output.write_bytes(b"previous")
    runtime = SimpleNamespace(checkpoint=lambda: (_ for _ in ()).throw(DownloadCancelled("cancelled")))
    with patch.object(sample_aes_samples, "current_download_runtime", return_value=runtime):
        with pytest.raises(DownloadCancelled):
            sample_aes.decrypt_sample_aes_parts([part], audio_stream(), [RawKey(KEY, KID)], output)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("sample-aes-*"))


@pytest.mark.parametrize("damage", ["short", "sync", "error", "scrambled", "adaptation", "pes"])
def test_invalid_transport_cannot_be_reported_as_decrypted(damage):
    clear = b"\x65" + b"a" * 400
    data = bytearray(_video_transport([b"\x00\x00\x01" + _encrypt_nal(clear)]))
    if damage == "short":
        data.pop()
    elif damage == "sync":
        data[0] = 0
    elif damage == "error":
        data[1] |= 0x80
    elif damage == "scrambled":
        data[3] |= 0x80
    elif damage == "adaptation":
        data[3] |= 0x30
        data[4] = 255
    else:
        # Remove the PES start marker while leaving valid TS framing.
        data[376 + 4:376 + 7] = b"bad"
    with pytest.raises(ValueError):
        sample_aes_samples.decrypt_ts(bytes(data), bytes.fromhex(KEY), IV)


@pytest.fixture(params=[32000, 44100, 48000])
def encrypted_ac3(request, tmp_path):
    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        pytest.skip("FFmpeg and ffprobe required")
    clear = tmp_path / "clear.ac3"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate={request.param}:duration=0.2",
        "-c:a", "ac3", "-b:a", "192k", "-f", "ac3", str(clear),
    ], check=True, capture_output=True)
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_packets", "-show_entries", "packet=pos,size",
        "-of", "json", str(clear),
    ], check=True, capture_output=True)
    data = bytearray(clear.read_bytes())
    for packet in json.loads(probe.stdout)["packets"]:
        start, size = int(packet["pos"]), int(packet["size"])
        count = (size - 16) // 16 * 16
        encryptor = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(IV)).encryptor()
        data[start + 16:start + 16 + count] = encryptor.update(bytes(data[start + 16:start + 16 + count])) + encryptor.finalize()
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x04test"
    part = tmp_path / "packed.ac3"
    part.write_bytes(tag + data)
    return clear, part, tag


def test_packed_ac3_preserves_frames_and_id3(encrypted_ac3):
    clear, part, tag = encrypted_ac3
    assert sample_aes_samples.decrypt_ac3(part.read_bytes(), bytes.fromhex(KEY), IV) == tag + clear.read_bytes()


def test_packed_ac3_decrypts_remuxes_and_keeps_all_packets(encrypted_ac3, tmp_path):
    clear, part, _ = encrypted_ac3
    stream = audio_stream(extension="ac3", codecs="ac-3")
    output = sample_aes.decrypt_sample_aes_parts([part], stream, [RawKey(KEY, KID)], tmp_path / "out.ac3")
    assert output.read_bytes() == clear.read_bytes()


def test_packed_ac3_wrong_key_does_not_publish(encrypted_ac3, tmp_path):
    _, part, _ = encrypted_ac3
    output = tmp_path / "out.ac3"
    output.write_bytes(b"previous")
    with pytest.raises(RuntimeError, match="validation failed"):
        sample_aes.decrypt_sample_aes_parts(
            [part], audio_stream(extension="ac3"), [RawKey(OTHER_KEY, KID)], output,
        )
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("sample-aes-*"))


@pytest.mark.parametrize("data", [
    b"ID3", b"bad", b"\x0b\x77\0\0\0\x40\0",  # truncated valid header
    b"\x0b\x77\0\0\xc0\x40\0",  # reserved sample rate
    b"\x0b\x77\0\0\x3f\x40\0",  # reserved frame size
    b"\x0b\x77\0\0\0\x80\0",  # E-AC-3 is not AC-3
])
def test_invalid_packed_ac3_is_not_silently_skipped(data):
    with pytest.raises(ValueError):
        sample_aes_samples.decrypt_ac3(data, bytes.fromhex(KEY), IV)


def test_native_download_decrypts_packed_ac3_and_muxes_mkv(encrypted_ac3, tmp_path):
    from unidl.downloader.postprocess import mux_files

    clear, part, _ = encrypted_ac3
    stream = audio_stream(extension="ac3", codecs="ac-3")
    stream.segments[0].url = str(part)
    result = _download_fixture(stream, tmp_path, [RawKey(KEY, KID)])
    assert result.path.read_bytes() == clear.read_bytes()
    assert result.temp_dir is None
    assert not list((tmp_path / "task").rglob("key-*.bin"))
    muxed = mux_files([result.path], tmp_path / "final.mkv", muxer="ffmpeg")
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(muxed),
    ], check=True, capture_output=True)
    assert json.loads(probe.stdout)["streams"][0]["codec_name"] == "ac3"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-err_detect", "explode",
        "-i", str(muxed), "-f", "null", "-",
    ], check=True, capture_output=True)


def test_eac3_resets_iv_for_independent_and_dependent_syncframes():
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x04test"
    clear, encrypted = bytearray(tag), bytearray(tag)
    for stream_type, size in [(0, 86), (1, 70), (0, 10), (1, 46)]:
        frame = bytearray(b"\x0b\x77" + bytes(size - 2))
        frame[2:4] = ((stream_type << 14) | (size // 2 - 1)).to_bytes(2, "big")
        frame[4:6] = b"\x34\x80"  # 48 kHz, six audio blocks, bsid 16
        frame[7:] = bytes(range(size - 7))
        clear.extend(frame)
        count = max(0, (size - 16) // 16 * 16)
        cipher = Cipher(algorithms.AES(bytes.fromhex(KEY)), modes.CBC(IV)).encryptor()
        frame[16:16 + count] = cipher.update(bytes(frame[16:16 + count])) + cipher.finalize()
        encrypted.extend(frame)
    assert sample_aes_samples.decrypt_eac3(bytes(encrypted), bytes.fromhex(KEY), IV) == clear


@pytest.mark.parametrize("data", [
    b"ID3", b"bad", b"\x0b\x77\0\x2f\x34\x80\0",  # truncated syncframe
    b"\x0b\x77\0\0\x34\x80\0",  # frame smaller than its header
    b"\x0b\x77\xc0\x2f\x34\x80\0",  # reserved stream type
    b"\x0b\x77\0\x2f\xf4\x80\0",  # reserved sample rate
    b"\x0b\x77\0\x2f\x34\x88\0",  # unsupported bsid
    b"\x0b\x77\0\x2f\x34\x40\0",  # AC-3, not E-AC-3
])
def test_invalid_eac3_is_not_silently_skipped(data):
    with pytest.raises(ValueError):
        sample_aes_samples.decrypt_eac3(data, bytes.fromhex(KEY), IV)
