from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlunparse

from .utils import join_uri, pretty_codec

_LIVE_DASH_ROOTS = {"dayone", "pdx-nitro"}
_IQIYI_AUDIO_GROUP = "iq-audio"
_YSP_CASTING_AAC_STREAM_ID = 0x102
_TENCENTVIDEO_CENC_SCHEMES = {"CENC", "SAMPLE_AES", "SAMPLE_AES_CENC", "SAMPLE_AES_CTR"}
_TENCENTVIDEO_PIFF_SAMPLE_ENCRYPTION_UUID = bytes.fromhex("a2394f525a9b4f14a2446c427c648df4")
_TENCENTVIDEO_FRAGMENT_PREFIX_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class AudioVividPolicyResult:
    path: Path
    codecs: tuple[str, ...] = ()
    decoded_path: Path | None = None
    replace_track: bool = False
    warning: str = ""


@dataclass(frozen=True, slots=True)
class _TencentVideoCencLayout:
    init_index: int
    init_path: Path
    metadata: Any
    fragments: tuple[tuple[int, Path, object, str], ...]


def apply_audio_vivid_policy(streams, enabled: bool) -> None:
    if not enabled:
        return
    for stream in streams or []:
        extra = getattr(stream, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
            stream.extra = extra
        extra["audio_vivid_policy"] = True


def tencentvideo_separate_audio_mux_media_type(stream) -> str | None:
    """Select only the requested elementary track from Tencent's TV HLS inputs.

    A Tencent TV video HLS is physically muxed video/AAC.  When the service
    emits a separate Dolby, HiFi or Audio Vivid HLS, the AAC is the source
    soundtrack being replaced rather than another requested output track.
    The explicit JSON source marker keeps this policy limited to that picker.
    """

    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict):
        return None
    if extra.get("title_source") != "tencentvideo_tv_separate_audio":
        return None
    if extra.get("source") != "json":
        return None
    media_type = str(getattr(stream, "media_type", None) or "").strip().lower()
    return media_type if media_type in {"video", "audio"} else None


def iqiyi_separate_audio_mux_media_type(stream, selected_streams) -> str | None:
    """Drop iQ's embedded AAC when an independent iQ audio track is selected.

    iQ's generated HLS master gives its replacement audio group the stable
    ``iq-audio`` id. Its video TS still physically contains the fallback AAC,
    so muxing the whole video input together with DD+ (or separate AAC) adds a
    duplicate audio stream and can invalidate per-track metadata. Keep the
    embedded AAC when video is selected alone, and filter only an explicitly
    paired iQ selection.
    """

    media_type = str(getattr(stream, "media_type", None) or "").strip().lower()
    if media_type not in {"video", "audio"} or not _is_iqiyi_audio_group_track(stream):
        return None
    has_separate_audio = any(
        str(getattr(candidate, "media_type", None) or "").strip().lower() == "audio"
        and _is_iqiyi_audio_group_track(candidate)
        for candidate in selected_streams or []
    )
    return media_type if has_separate_audio else None


def _is_iqiyi_audio_group_track(stream) -> bool:
    extra = getattr(stream, "extra", None)
    audio_id = extra.get("audio_id") if isinstance(extra, dict) else None
    groups = {
        str(getattr(stream, "group_id", None) or "").strip(),
        str(audio_id or "").strip(),
    }
    return _IQIYI_AUDIO_GROUP in groups


def tencentvideo_separate_audio_custom_hls_applies(stream, scheme: str) -> bool:
    """Keep a TV VINFO ChaCha key off the independent replacement audio HLS.

    Tencent's key and nonce protect the selected video transport stream.  The
    separately selected Dolby/HiFi/Audio Vivid HLS is an independent clear
    source, so applying the same custom ChaCha transform corrupts that audio.
    The JSON source marker makes the exception exact and leaves every ordinary
    custom-HLS invocation unchanged.
    """

    if str(scheme or "").strip().upper() != "CHACHA20":
        return True
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict):
        return True
    if extra.get("title_source") != "tencentvideo_tv_separate_audio":
        return True
    if extra.get("source") != "json":
        return True
    return str(getattr(stream, "media_type", None) or "").strip().lower() == "video"


def mark_yangshipin_casting_streams(streams, headers) -> None:
    """Tag YSP casting streams from their authenticated request contract.

    The high-bitrate receiver returns media from CCTV's TP4K/TPGQ domains,
    shared hostnames that alone do not identify Yangshipin.  Its signed VDN
    request headers do, so retain only that provenance for the live pipe rules.
    """
    values = {
        str(name).strip().lower(): str(value).strip()
        for name, value in (headers or {}).items()
        if str(name).strip()
    }
    signed = (
        values.get("user-agent") == "cctv_app_tv"
        and values.get("referer") == "api.cctv.cn"
        and all(
            values.get(name)
            for name in ("uid", "appid", "appsign", "apprandomstr")
        )
    )
    if not signed:
        return
    for stream in streams or []:
        if not any(
            _is_ysp_casting_media_host(urlparse(url).netloc)
            for url in _stream_urls(stream)
        ):
            continue
        extra = getattr(stream, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
            stream.extra = extra
        extra["yangshipin_casting_source"] = True


def is_yangshipin_catchup_cdn_url(url: str) -> bool:
    """Whether a URL belongs to Yangshipin's catch-up media CDN.

    Child TS URLs inherit the authorization in their parent playlist and do
    not repeat its query parameters.  Keep the transport rule scoped to the
    dedicated playback host and path so those children use the same policy.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return host == "tlivecloud-playback-cdn.ysp.cctv.cn" and parsed.path.lower().startswith("/tcloud.cctv.com/")


def should_prefer_yangshipin_catchup_curl(url: str) -> bool:
    """Whether an authorized Yangshipin catch-up playlist needs curl transport.

    ``tlivecloud-playback`` intermittently closes a valid HTTP/1.1 client
    connection before returning a response.  Curl's transport retries recover
    the same signed manifest and its TS parts reliably.  The path and replay
    parameters make this specific to Yangshipin catch-up, rather than every
    CCTV CDN URL.
    """
    if not is_yangshipin_catchup_cdn_url(url):
        return False
    parsed = urlparse(url)
    values = {name.lower(): value for name, value in parse_qsl(parsed.query, keep_blank_values=True)}
    return values.get("from") == "player" and all(values.get(name) for name in ("pid", "starttime", "svrtime"))


def yangshipin_catchup_parallelism(url: str, workers: int) -> tuple[int, int] | None:
    """Return the replay CDN's deliberately conservative connection limits."""
    if not is_yangshipin_catchup_cdn_url(url):
        return None
    count = max(1, int(workers or 1))
    # Two streams materially improve a five-segment replay, while the shared
    # adaptive limiter backs off to one when this CDN starts closing requests.
    return min(count, 2), min(count, 2)


def postprocess_audio_vivid(
    path: str | Path,
    stream,
    *,
    decoder: str | Path | None = None,
    decoder_args: str | None = None,
) -> AudioVividPolicyResult:
    from .avs3 import (
        Avs3Error,
        decode_mp4_audio_vivid,
        decode_mpeg_ts_audio_vivid,
        inspect_mp4,
        inspect_mpeg_ts,
    )

    source = Path(path)
    if not _has_audio_vivid_policy(stream):
        return AudioVividPolicyResult(source)
    suffix = source.suffix.lower()
    try:
        if suffix in {".ts", ".m2ts", ".mts"}:
            inspection = inspect_mpeg_ts(source)
            codecs = tuple(dict.fromkeys(item.codec for item in inspection.streams))
            has_vivid = "audio-vivid" in codecs
            decode = decode_mpeg_ts_audio_vivid
        elif suffix in {".mp4", ".m4a", ".mov"}:
            inspection = inspect_mp4(source)
            codecs = tuple(dict.fromkeys(item.codec for item in inspection.audio_tracks))
            has_vivid = "audio-vivid" in codecs
            decode = decode_mp4_audio_vivid
        else:
            return AudioVividPolicyResult(source)
    except Avs3Error as exc:
        return AudioVividPolicyResult(source, warning=f"could not inspect audio codecs: {exc}")
    if not has_vivid:
        audio_codecs = [codec for codec in codecs if codec in {"aac", "e-ac-3", "ac-3"}]
        if getattr(stream, "media_type", None) == "audio" and audio_codecs:
            stream.codecs = audio_codecs[0]
        return AudioVividPolicyResult(source, codecs=codecs)

    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict):
        extra["audio_vivid_detected"] = True
        # Keep the source(s) separate until decoding has succeeded. This also
        # prevents a missing optional decoder from handing av3a back to FFmpeg.
        extra["audio_vivid_preserve_source_container"] = True

    decoded = source.with_name(f"{source.stem}.audio-vivid.wav")
    try:
        decode(
            source,
            decoded,
            decoder=decoder,
            decoder_args=decoder_args,
        )
    except Avs3Error as exc:
        return AudioVividPolicyResult(
            source,
            codecs=codecs,
            warning=f"Audio Vivid was preserved but could not be decoded: {exc}",
        )
    replace = getattr(stream, "media_type", None) == "audio"
    if isinstance(extra, dict):
        # The downloader adds this WAV as a companion mux input for a muxed
        # video track, so the unknown av3a entry never reaches the muxer.
        extra["audio_vivid_preserve_source_container"] = False
        extra["audio_vivid_wav"] = str(decoded)
    return AudioVividPolicyResult(
        decoded if replace else source,
        codecs=codecs,
        decoded_path=decoded,
        replace_track=replace,
    )


def should_preserve_audio_vivid_source_container(streams) -> bool:
    return any(
        isinstance(getattr(stream, "extra", None), dict)
        and bool(stream.extra.get("audio_vivid_preserve_source_container"))
        for stream in streams or []
    )


def join_dash_base_uri(base_uri: str, reference: str) -> str:
    joined = join_uri(base_uri, reference)
    return _preserve_live_dash_prefix(base_uri, joined)


def should_skip_dash_period(period_id: str | None, period_base: str, is_live: bool) -> bool:
    return _is_dayone_ad_period(period_id, period_base, is_live)


def child_url_params(input_url: str) -> list[tuple[str, str]]:
    parsed = urlparse(input_url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if not params:
        return []
    if _is_dazn_host(parsed.netloc):
        return [(key, value) for key, value in params if key.lower() not in {"start", "end"}]
    return params


def should_append_child_url_params(input_url: str) -> bool:
    parsed = urlparse(input_url)
    if not _is_dazn_host(parsed.netloc):
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return bool(params.get("dazn-token"))


def child_request_header_warnings(input_url: str, headers: dict[str, str] | None = None) -> list[str]:
    required = child_url_required_headers(input_url)
    missing = [name for name in required if not _has_header(headers, name)]
    parsed = urlparse(input_url)
    if _is_dazn_host(parsed.netloc) and "user-agent" in {name.lower() for name in missing}:
        return [
            "DAZN media token is bound to User-Agent; pass the exact User-Agent used to create the token with -H \"User-Agent: ...\"."
        ]
    mismatch = _dazn_user_agent_mismatch(input_url, headers)
    if mismatch:
        return [
            "DAZN media token does not match the supplied User-Agent; pass the exact User-Agent used to create the token."
        ]
    if not missing:
        return []
    return [f"media token requires header(s): {', '.join(missing)}"]


def child_url_error_tip(url: str, error_text: str) -> str | None:
    parsed = urlparse(url)
    if _is_hulu_japan_live_media_host(parsed.netloc) and "403" in error_text:
        return (
            "Hulu Japan live CDN rejected the init/media authorization. Regenerate the live playback source and "
            "verify that the current Japanese network exit is accepted by the live CDN; lowering workers or "
            "increasing retries will not fix a persistent 403."
        )
    if not _is_dazn_host(parsed.netloc):
        return None
    normalized_error = error_text.lower().replace("-", " ")
    if "unauthorized 667" in normalized_error:
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if not params.get("dazn-token"):
            return "DAZN init/media request is missing its authentication query; retry with --append-url-params or update to a build that appends DAZN parameters automatically."
        return "DAZN rejected the init/media authorization; verify that the token is current and authorizes this media path."
    if "forbidden 688" not in normalized_error:
        return None
    required = {name.lower() for name in child_url_required_headers(url)}
    if "user-agent" in required:
        return "DAZN rejected the media token signature; pass the exact User-Agent used to create the token with -H \"User-Agent: ...\"."
    return None


def child_url_required_headers(input_url: str) -> list[str]:
    parsed = urlparse(input_url)
    if not _is_dazn_host(parsed.netloc):
        return []
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    token = params.get("dazn-token")
    if not token:
        return []
    payload = _jwt_payload(token)
    headers = payload.get("headers") if isinstance(payload, dict) else None
    required = [str(header).strip() for header in headers if str(header).strip()] if isinstance(headers, list) else []
    if isinstance(payload, dict) and payload.get("ua") and "user-agent" not in {name.lower() for name in required}:
        required.append("user-agent")
    return required


def live_pipe_input_offsets_seconds(streams, args) -> dict[int, float]:
    if not _is_proxad_oqee_live(streams):
        return {}
    starts = {
        id(stream): start
        for stream in streams
        if getattr(stream, "media_type", None) in {"video", "audio"}
        for start in [_dash_initial_presentation_start(stream, args)]
        if start is not None
    }
    if len(starts) < 2:
        return {}
    base_start = min(starts.values())
    return {
        stream_id: offset
        for stream_id, start in starts.items()
        for offset in [start - base_start]
        if offset > 0.25
    }


def should_treat_live_stream_as_fragmented_mp4(stream) -> bool:
    if _is_sabr_ump_live_stream(stream):
        return not _stream_uses_webm_container(stream)
    return _is_youtube_json_direct_live_stream(stream) and not _stream_uses_webm_container(stream)


def tencentvideo_clear_cenc_fragment_compatibility(stream, part_paths, segments) -> bool:
    """Recognize Tencent TV's CENC-signalled but clear-sample HLS variant.

    The TV VINFO playlist can advertise Widevine/CENC while each media
    fragment selects a clear ``avc1`` sample-description entry from the init
    segment.  Those fragments have no ``senc``/auxiliary encryption data, so
    sending them through the generic fragment decryptor only produces a false
    "unsupported layout" error.  Keep this rule deliberately conservative:
    it requires a Tencent media URL, a CENC HLS stream, a protected and a
    clear init entry, and every downloaded media fragment to select the clear
    entry without any fragment encryption boxes.
    """

    layout = _analyze_tencentvideo_cenc_parts(stream, part_paths, segments)
    if layout is None or any(kind != "clear" for _index, _path, _segment, kind in layout.fragments):
        return False
    extra = _tencentvideo_rule_extra(stream)
    extra["tencentvideo_cenc_compatibility"] = "clear_sample_entry"
    extra["tencentvideo_cenc_init_index"] = layout.init_index
    return True


def decrypt_tencentvideo_cenc_parts(
    stream,
    part_paths,
    segments,
    keys,
    output_path: str | Path,
    *,
    temp_dir: str | Path | None = None,
) -> Path | None:
    """Handle Tencent TV HLS that switches between clear and CENC entries.

    The first section of an otherwise protected Tencent TV asset can select a
    clear sample entry, then later fragments switch to the protected entry and
    carry ordinary ``senc`` metadata.  The generic fragment pipeline aborts on
    the first clear fragment before it reaches the decryptable ones.  This
    rule copies only the proven-clear fragments and delegates every protected
    fragment to the existing CENC implementation.
    """

    layout = _analyze_tencentvideo_cenc_parts(stream, part_paths, segments)
    if layout is None:
        return None
    kinds = {kind for _index, _path, _segment, kind in layout.fragments}
    if "clear" not in kinds:
        return None
    if "encrypted" in kinds and not keys:
        raise ValueError("Tencent Video mixed CENC fragments need a matching Widevine key")
    if kinds == {"clear"}:
        output = assemble_tencentvideo_clear_cenc_parts(
            part_paths,
            output_path,
            init_index=layout.init_index,
        )
        extra = _tencentvideo_rule_extra(stream)
        extra["tencentvideo_cenc_compatibility"] = "clear_sample_entry"
        extra["tencentvideo_cenc_clear_fragments"] = len(layout.fragments)
        extra["tencentvideo_cenc_encrypted_fragments"] = 0
        return output

    from .cenc_fragment import decrypt_cenc_fragment
    from .postprocess import normalize_decrypted_mp4_bytes

    parts = [Path(path) for path in (part_paths or [])]
    output = Path(output_path)
    if output in parts:
        raise ValueError("Tencent Video CENC output must be separate from downloaded parts")
    output.parent.mkdir(parents=True, exist_ok=True)
    work_parent = Path(temp_dir).expanduser() if temp_dir else output.parent
    work_parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"{output.stem}_tencentvideo_", dir=str(work_parent)))
    temporary = output.with_name(f".{output.name}.tencentvideo.tmp")
    by_index = {
        index: (path, segment, kind)
        for index, path, segment, kind in layout.fragments
    }
    expected_kids = _tencentvideo_stream_key_ids(stream)
    clear_count = 0
    encrypted_count = 0
    try:
        with temporary.open("wb") as target:
            for index, part in enumerate(parts):
                if index == layout.init_index:
                    target.write(normalize_decrypted_mp4_bytes(part.read_bytes()))
                    continue
                fragment = by_index.get(index)
                if fragment is None:
                    raise RuntimeError(f"Tencent Video CENC part {index} has no matching media segment")
                _path, segment, kind = fragment
                if kind == "clear":
                    clear_count += 1
                    with part.open("rb") as source:
                        shutil.copyfileobj(source, target)
                    continue
                encrypted_count += 1
                clear_fragment = work_dir / f"{index:08d}.clear.mp4"
                segment_kid = str(getattr(segment, "key_id", None) or "").strip()
                fragment_kids = [segment_kid] if segment_kid else expected_kids
                decrypted = decrypt_cenc_fragment(
                    part,
                    keys,
                    clear_fragment,
                    fragment_kids,
                    init_path=layout.init_path,
                    default_constant_iv=getattr(segment, "key_iv", None),
                    init_metadata=layout.metadata,
                )
                if decrypted is None:
                    raise RuntimeError(
                        f"Tencent Video protected CENC fragment {index} has an unsupported layout"
                    )
                with decrypted.open("rb") as source:
                    shutil.copyfileobj(source, target)
                decrypted.unlink(missing_ok=True)
        temporary.replace(output)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    extra = _tencentvideo_rule_extra(stream)
    extra["tencentvideo_cenc_compatibility"] = "mixed_sample_entries"
    extra["tencentvideo_cenc_clear_fragments"] = clear_count
    extra["tencentvideo_cenc_encrypted_fragments"] = encrypted_count
    return output


def _analyze_tencentvideo_cenc_parts(stream, part_paths, segments) -> _TencentVideoCencLayout | None:
    if not _is_tencentvideo_cenc_stream(stream):
        return None
    parts = [Path(path) for path in (part_paths or [])]
    segment_list = list(segments or [])
    if not parts or len(parts) != len(segment_list):
        return None
    init_indices = [
        index
        for index, segment in enumerate(segment_list)
        if getattr(segment, "index", None) == -1
    ]
    # Multiple-period/multi-init HLS needs a section-aware rule.  Leave it to
    # the normal decryptor until that variant is observed and described.
    if len(init_indices) != 1:
        return None
    init_index = init_indices[0]
    init_path = parts[init_index]
    try:
        from .cenc_fragment import parse_cenc_init_metadata

        metadata = parse_cenc_init_metadata(
            init_path.read_bytes(),
            _tencentvideo_stream_key_ids(stream),
        )
    except (OSError, ValueError, TypeError):
        return None
    states_by_track = metadata.sample_entry_encrypted_by_track
    if not metadata.schemes.intersection({b"cenc", b"cens"}) or not states_by_track:
        return None
    if not any(not state for states in states_by_track.values() for state in states):
        return None

    fragments: list[tuple[int, Path, object, str]] = []
    for index, (path, segment) in enumerate(zip(parts, segment_list, strict=False)):
        if index == init_index:
            continue
        try:
            prefix = _read_tencentvideo_fragment_prefix(path)
        except OSError:
            return None
        if prefix is None:
            return None
        tracks = _tencentvideo_fragment_tracks(prefix)
        if not tracks:
            return None
        selected_states: list[bool] = []
        for track_id, sample_description_index in tracks:
            states = states_by_track.get(track_id)
            if states is None and len(states_by_track) == 1:
                states = next(iter(states_by_track.values()))
            if (
                not states
                or sample_description_index < 1
                or sample_description_index > len(states)
            ):
                return None
            selected_states.append(states[sample_description_index - 1])
        has_encryption = _tencentvideo_fragment_is_encrypted(prefix)
        if has_encryption and all(selected_states):
            kind = "encrypted"
        elif not has_encryption and not any(selected_states):
            kind = "clear"
        else:
            return None
        fragments.append((index, path, segment, kind))
    if not fragments:
        return None
    return _TencentVideoCencLayout(
        init_index=init_index,
        init_path=init_path,
        metadata=metadata,
        fragments=tuple(fragments),
    )


def _tencentvideo_rule_extra(stream) -> dict:
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict):
        stream.extra = {}
        extra = stream.extra
    return extra


def assemble_tencentvideo_clear_cenc_parts(
    part_paths,
    output_path: str | Path,
    *,
    init_index: int = 0,
) -> Path:
    """Assemble parts accepted by the Tencent clear-sample compatibility rule."""

    parts = [Path(path) for path in (part_paths or [])]
    if not parts:
        raise ValueError("Tencent Video clear CENC compatibility has no downloaded parts")
    output = Path(output_path)
    if output in parts:
        raise ValueError("Tencent Video CENC output must be separate from downloaded parts")
    if init_index < 0 or init_index >= len(parts):
        raise ValueError("Tencent Video clear CENC compatibility has an invalid init index")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tencentvideo.tmp")
    from .postprocess import normalize_decrypted_mp4_bytes

    try:
        with temporary.open("wb") as target:
            for index, part in enumerate(parts):
                with part.open("rb") as source:
                    if index == init_index:
                        target.write(normalize_decrypted_mp4_bytes(source.read()))
                    else:
                        shutil.copyfileobj(source, target)
        temporary.replace(output)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return output


def live_pipe_media_part_contains_init(stream, segment=None) -> bool:
    if _is_sabr_ump_live_stream(stream):
        if _stream_uses_webm_container(stream):
            return False
        return segment is not None and getattr(segment, "index", None) != -1
    if not _is_youtube_json_direct_live_stream(stream):
        return False
    if segment is not None and getattr(segment, "index", None) == -1:
        return False
    return not getattr(segment, "byte_range", None)


def live_pipe_mux_disabled_reason(stream) -> str | None:
    schemes = [getattr(stream, "encryption_scheme", None)]
    schemes.extend(
        getattr(segment, "encryption_scheme", None)
        for segment in getattr(stream, "segments", []) or []
    )
    if _is_audio_vivid_policy_muxed_hls(stream):
        return (
            "Audio Vivid-enabled muxed live TS is recorded before muxing so its PMT can distinguish "
            "AAC, E-AC-3 and Audio Vivid without sending av3a to FFmpeg."
        )
    if _is_youtube_json_direct_live_stream(stream) and _stream_uses_webm_container(stream):
        return (
            "YouTube JSON VP9/WebM live pipe mux is disabled for now; recording the tracks separately "
            "avoids short video output while the continuous WebM live muxer is hardened."
        )
    return None


def live_pipe_matroska_options(streams, output_container: str | None) -> list[str]:
    """Return Matroska live-pipe options, including YSP's AAC probe exception.

    FFmpeg's Matroska ``-live 1`` mode commits the output header before a FIFO
    MPEG-TS input has necessarily exposed an AAC ADTS configuration. YSP's
    ordinary live feeds are muxed H.264/AAC TS, so that turns a valid AAC track
    into an unknown-samplerate failure. The normal cluster limits still flush
    live output promptly; this only omits the premature Matroska mode for YSP.
    """
    if output_container != "matroska":
        return []
    options: list[str] = []
    if not any(_is_ysp_muxed_live_ts(stream) for stream in streams or []):
        options.extend(["-live", "1"])
    options.extend([
        "-cluster_time_limit",
        "1000",
        "-cluster_size_limit",
        str(64 * 1024),
    ])
    return options


def yangshipin_casting_live_pipe_map_specs(stream, input_index: int) -> list[str] | None:
    """Map the usable AAC PID from a Yangshipin high-bitrate live TS.

    The casting CDN's AAC rendition carries an undecodable companion PID
    (usually ``0x101``) before its actual AAC PID (``0x102``). Stream indexes
    are not stable: FFmpeg may expose the companion as MP3, unknown, or omit it
    while probing the FIFO. Map the MPEG-TS stream id instead of an audio
    ordinal so the real AAC remains selected in every segment.

    Audio Vivid is not routed here because its existing policy disables
    real-time pipe muxing before this point.
    """
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict) or not extra.get("yangshipin_casting_source"):
        return None
    if not _is_ysp_muxed_live_ts(stream):
        return None
    return [f"{input_index}:v?", f"{input_index}:i:{_YSP_CASTING_AAC_STREAM_ID}?"]


def should_finalize_live_pipe_matroska_output(streams, output_container: str | None) -> bool:
    if output_container != "matroska":
        return False
    return any(_is_kan_medone_dash_hevc_live_stream(stream) for stream in streams or [])


def should_finalize_live_pipe_matroska_output_to_mp4(streams, output_container: str | None) -> bool:
    if output_container != "matroska":
        return False
    return any(_is_kan_medone_dash_hevc_live_stream(stream) for stream in streams or [])


def should_repeat_live_media_request(stream) -> bool:
    if not _is_youtube_json_direct_live_stream(stream):
        return False
    media_segments = [
        segment
        for segment in getattr(stream, "segments", []) or []
        if getattr(segment, "index", None) != -1
    ]
    return len(media_segments) == 1 and bool(getattr(media_segments[0], "url", None))


def live_media_retry_attempts(stream, default_retries: int) -> int:
    retries = max(1, int(default_retries or 1))
    if _is_pluto_takedown_slate_hls_stream(stream) or _is_hulu_japan_live_dash_stream(stream):
        return 1
    return retries


def live_media_retry_grace_enabled(stream) -> bool:
    if _is_vgc_stream(stream):
        return False
    return not (_is_pluto_takedown_slate_hls_stream(stream) or _is_hulu_japan_live_dash_stream(stream))


def repeated_live_media_duration_seconds(stream, fallback_seconds: float | int | None = None) -> float:
    for segment in getattr(stream, "segments", []) or []:
        duration = getattr(segment, "duration", None)
        if duration and duration > 0:
            return float(duration)
    if fallback_seconds:
        try:
            parsed = float(fallback_seconds)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return 3.0


def _is_youtube_json_direct_live_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "json":
        return False
    if not getattr(stream, "is_live", False):
        return False
    if not _json_stream_has_init_range(stream):
        return False
    return any(_is_youtube_live_media_url(url) for url in _stream_urls(stream))


def _is_sabr_ump_live_stream(stream) -> bool:
    if not getattr(stream, "is_live", False):
        return False
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    return getattr(stream, "manifest_type", None) == "sabr_ump" or bool(extra.get("sabr_ump"))


def _is_vgc_stream(stream) -> bool:
    extra = getattr(stream, "extra", None)
    return isinstance(extra, dict) and bool(extra.get("vgc"))


def _is_tencentvideo_cenc_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls":
        return False
    if getattr(stream, "media_type", None) != "video" or not getattr(stream, "encrypted", False):
        return False
    schemes = [getattr(stream, "encryption_scheme", None)]
    schemes.extend(
        getattr(segment, "encryption_scheme", None)
        for segment in getattr(stream, "segments", []) or []
    )
    if not any(str(value or "").upper().replace("-", "_") in _TENCENTVIDEO_CENC_SCHEMES for value in schemes):
        return False
    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict) and extra.get("tencentvideo_tv"):
        return True
    return any(_is_tencentvideo_media_url(url) for url in _stream_urls(stream))


def _is_tencentvideo_media_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not (host == "qq.com" or host.endswith(".qq.com") or host == "gtimg.com" or host.endswith(".gtimg.com")):
        return False
    path = parsed.path.lower()
    return ".m3u8" in path or ".mp4" in path or "/vod/" in path


def _tencentvideo_stream_key_ids(stream) -> list[str]:
    values: list[str] = []
    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict):
        raw_values = [extra.get("key_id"), *(extra.get("key_ids") or [])]
        for value in raw_values:
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
    for segment in getattr(stream, "segments", []) or []:
        text = str(getattr(segment, "key_id", None) or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _read_tencentvideo_fragment_prefix(path: Path) -> bytes | None:
    with path.open("rb") as source:
        data = source.read(_TENCENTVIDEO_FRAGMENT_PREFIX_BYTES)
        if len(data) < 8:
            return None
        boxes = list(_tencentvideo_mp4_boxes(data))
        moof = next((item for item in boxes if item[2] == b"moof"), None)
        if moof is None:
            return None
        moof_end = moof[0] + moof[1]
        source.seek(moof_end)
        mdat_header = source.read(8)
    if len(mdat_header) < 8 or mdat_header[4:8] != b"mdat":
        return None
    mdat_size = int.from_bytes(mdat_header[:4], "big")
    if mdat_size not in {0} and mdat_size < 8:
        return None
    return data[:moof_end]


def _tencentvideo_fragment_is_encrypted(data: bytes) -> bool:
    for moof_position, moof_size, box_type, moof_header in _tencentvideo_mp4_boxes(data):
        if box_type != b"moof":
            continue
        moof_end = moof_position + moof_size
        for position, size, child_type, header_size in _tencentvideo_mp4_boxes(
            data, moof_position + moof_header, moof_end
        ):
            if child_type != b"traf":
                continue
            traf_end = position + size
            for child_position, child_size, child_box_type, child_header in _tencentvideo_mp4_boxes(
                data, position + header_size, traf_end
            ):
                if child_box_type in {b"senc", b"saiz", b"saio", b"sgpd", b"sbgp"}:
                    return True
                if child_box_type == b"uuid":
                    uuid_start = child_position + child_header
                    uuid_end = uuid_start + len(_TENCENTVIDEO_PIFF_SAMPLE_ENCRYPTION_UUID)
                    if uuid_end <= child_position + child_size and bytes(data[uuid_start:uuid_end]) == _TENCENTVIDEO_PIFF_SAMPLE_ENCRYPTION_UUID:
                        return True
    return False


def _tencentvideo_fragment_tracks(data: bytes) -> list[tuple[int, int]]:
    tracks: list[tuple[int, int]] = []
    for moof_position, moof_size, box_type, moof_header in _tencentvideo_mp4_boxes(data):
        if box_type != b"moof":
            continue
        moof_end = moof_position + moof_size
        for position, size, child_type, header_size in _tencentvideo_mp4_boxes(
            data, moof_position + moof_header, moof_end
        ):
            if child_type != b"traf":
                continue
            traf_end = position + size
            tfhd: tuple[int, int] | None = None
            has_trun = False
            for child_position, child_size, child_box_type, child_header in _tencentvideo_mp4_boxes(
                data, position + header_size, traf_end
            ):
                if child_box_type == b"trun":
                    has_trun = True
                elif child_box_type == b"tfhd":
                    tfhd = _tencentvideo_parse_tfhd(data, child_position, child_size, child_header)
            if tfhd is not None and has_trun:
                tracks.append(tfhd)
    return tracks


def _tencentvideo_parse_tfhd(
    data: bytes, position: int, size: int, header_size: int
) -> tuple[int, int] | None:
    payload = position + header_size
    end = position + size
    if payload + 8 > end:
        return None
    flags = int.from_bytes(data[payload + 1 : payload + 4], "big")
    cursor = payload + 4
    track_id = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    if flags & 0x000001:
        cursor += 8
    sample_description_index = 1
    if flags & 0x000002:
        if cursor + 4 > end:
            return None
        sample_description_index = int.from_bytes(data[cursor : cursor + 4], "big")
    return track_id, sample_description_index


def _tencentvideo_mp4_boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    position = start
    while position + 8 <= end:
        size = int.from_bytes(data[position : position + 4], "big")
        box_type = bytes(data[position + 4 : position + 8])
        header_size = 8
        if size == 1:
            if position + 16 > end:
                return
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            return
        yield position, size, box_type, header_size
        position += size


def _is_kan_medone_dash_hevc_live_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "dash" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    codec = pretty_codec(getattr(stream, "codecs", None), "video")
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if codec != "H.265" and not raw_codec.startswith(("hvc1", "hev1", "hevc", "h265")):
        return False
    return any(_is_kan_medone_live_url(url) for url in _stream_urls(stream))


def _is_pluto_takedown_slate_hls_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    return any(_is_pluto_takedown_slate_url(url) for url in _stream_urls(stream))


def _is_hulu_japan_live_dash_stream(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "dash" or not getattr(stream, "is_live", False):
        return False
    return any(_is_hulu_japan_live_media_host(urlparse(url).netloc) for url in _stream_urls(stream))


def _is_hulu_japan_live_media_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].lower()
    return normalized.startswith("live") and normalized.endswith(".happyon-cdn.jp")


def _has_audio_vivid_policy(stream) -> bool:
    extra = getattr(stream, "extra", None)
    return isinstance(extra, dict) and bool(extra.get("audio_vivid_policy"))


def _is_audio_vivid_policy_muxed_hls(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    extra = getattr(stream, "extra", None)
    if (
        not isinstance(extra, dict)
        or not extra.get("muxed_audio")
        or not extra.get("audio_vivid_policy")
    ):
        return False
    if (getattr(stream, "extension", None) or "").lower().lstrip(".") != "ts":
        return False
    return True


def _is_ysp_muxed_live_ts(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls" or not getattr(stream, "is_live", False):
        return False
    if getattr(stream, "media_type", None) != "video":
        return False
    if (getattr(stream, "extension", None) or "").lower().lstrip(".") != "ts":
        return False
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict) or not extra.get("muxed_audio"):
        return False
    return bool(extra.get("yangshipin_casting_source")) or any(
        _is_ysp_live_media_host(urlparse(url).netloc) for url in _stream_urls(stream)
    )


def _is_ysp_live_media_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].lower()
    return normalized == "ysp.cctv.cn" or normalized.endswith(".ysp.cctv.cn")


def _is_ysp_casting_media_host(host: str) -> bool:
    normalized = (host or "").split(":", 1)[0].lower()
    return normalized in {
        "live-tpgq.cctv.cn",
        "liveali-tpgq.cctv.cn",
        "live-tp4k.cctv.cn",
        "liveali-tp4k.cctv.cn",
    }


def _json_stream_has_init_range(stream) -> bool:
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    if isinstance(raw, dict):
        if raw.get("init_range") or raw.get("initRange") or raw.get("initialization_range") or raw.get("initializationRange"):
            return True
    return any(getattr(segment, "index", None) == -1 for segment in getattr(stream, "segments", []) or [])


def _stream_urls(stream) -> list[str]:
    urls: list[str] = []

    def add(value) -> None:
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)

    add(getattr(stream, "url", None))
    add(getattr(stream, "original_url", None))
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    all_urls = extra.get("all_urls")
    if isinstance(all_urls, list):
        for value in all_urls:
            add(value)
    elif isinstance(all_urls, dict):
        for value in all_urls.values():
            add(value)
    for segment in (getattr(stream, "segments", []) or [])[:3]:
        add(getattr(segment, "url", None))
    return urls


def _is_youtube_live_media_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "googlevideo.com" not in host:
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    source = params.get("source", "").lower()
    client = params.get("c", "").lower()
    return source in {"yt_tv_broadcast", "yt_live_broadcast"} or "tvhtml5" in client


def _is_kan_medone_live_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return (
        host == "kancdn.medonecdn.net"
        or host == "cdn.avcom-bezeq.tv"
        or path.endswith("/kancdn-live/live/kan_4k/live_drm.livx")
        or "/kancdn-live/live/kan_4k/live_drm.livx" in path
    )


def _is_pluto_takedown_slate_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host != "plutotv.net" and not host.endswith(".plutotv.net"):
        return False
    return "takedownslates" in path and "/hls/" in path


def _stream_uses_webm_container(stream) -> bool:
    extension = (getattr(stream, "extension", None) or "").lower()
    if extension == "webm":
        return True
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    mime_type = ""
    if isinstance(raw, dict):
        mime_type = str(raw.get("mime_type") or raw.get("mimeType") or "").lower()
    if "webm" in mime_type:
        return True
    codec = (getattr(stream, "codecs", None) or "").lower()
    return "vp9" in codec or "vp8" in codec or "opus" in codec


def _preserve_live_dash_prefix(base_uri: str, joined_uri: str) -> str:
    base = urlparse(base_uri)
    joined = urlparse(joined_uri)
    if base.scheme not in {"http", "https"} or joined.scheme not in {"http", "https"}:
        return joined_uri
    if base.netloc != joined.netloc:
        return joined_uri

    base_parts = [part for part in base.path.split("/") if part]
    joined_parts = [part for part in joined.path.split("/") if part]
    protected_prefix = _live_dash_protected_prefix(base_parts)
    if not protected_prefix or not joined_parts:
        return joined_uri
    if joined_parts[: len(protected_prefix)] == protected_prefix:
        return joined_uri
    common_len = _common_prefix_length(protected_prefix, joined_parts)
    if common_len < len(protected_prefix) and common_len < len(joined_parts) and joined_parts[common_len] in _LIVE_DASH_ROOTS:
        path = "/" + "/".join([*protected_prefix, *joined_parts[common_len:]])
        return urlunparse(joined._replace(path=_preserve_trailing_slash(path, joined.path)))
    if joined_parts[0] in _LIVE_DASH_ROOTS:
        path = "/" + "/".join([*protected_prefix, *joined_parts])
        return urlunparse(joined._replace(path=_preserve_trailing_slash(path, joined.path)))
    if len(joined_parts) > 1 and joined_parts[0] == protected_prefix[0] and joined_parts[1] in _LIVE_DASH_ROOTS:
        path = "/" + "/".join([*protected_prefix, *joined_parts[1:]])
        return urlunparse(joined._replace(path=_preserve_trailing_slash(path, joined.path)))
    return joined_uri


def _live_dash_protected_prefix(parts: list[str]) -> list[str]:
    for index, part in enumerate(parts):
        if part in _LIVE_DASH_ROOTS:
            return parts[:index]
    for marker in (("v1", "dash"), ("live", "clients", "dash")):
        marker_len = len(marker)
        for index in range(0, len(parts) - marker_len + 1):
            if tuple(parts[index : index + marker_len]) == marker:
                return parts[:index]
    return []


def _common_prefix_length(left: list[str], right: list[str]) -> int:
    count = 0
    for left_part, right_part in zip(left, right, strict=False):
        if left_part != right_part:
            break
        count += 1
    return count


def _preserve_trailing_slash(path: str, source_path: str) -> str:
    if source_path.endswith("/") and not path.endswith("/"):
        return path + "/"
    return path


def _is_dayone_ad_period(period_id: str | None, period_base: str, is_live: bool) -> bool:
    if not is_live:
        return False
    parsed = urlparse(period_base)
    path_parts = [part.lower() for part in parsed.path.split("/") if part]
    return "dayone" in path_parts and "_" in (period_id or "")


def _is_dazn_host(host: str | None) -> bool:
    normalized = (host or "").lower()
    return normalized == "indazn.com" or normalized.endswith(".indazn.com") or normalized == "dazn.com" or normalized.endswith(".dazn.com")


def _has_header(headers: dict[str, str] | None, name: str) -> bool:
    normalized = name.lower()
    return any(str(key).lower() == normalized for key in (headers or {}))


def _header_value(headers: dict[str, str] | None, name: str) -> str | None:
    normalized = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == normalized:
            return str(value)
    return None


def _dazn_user_agent_mismatch(input_url: str, headers: dict[str, str] | None) -> bool:
    payload = _dazn_token_payload(input_url)
    expected = payload.get("ua") if isinstance(payload, dict) else None
    actual = _header_value(headers, "user-agent")
    if not isinstance(expected, str) or not expected or not actual:
        return False
    return hashlib.sha1(actual.encode("utf-8")).hexdigest().lower() != expected.lower()


def _dazn_token_payload(input_url: str) -> dict:
    parsed = urlparse(input_url)
    if not _is_dazn_host(parsed.netloc):
        return {}
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    token = params.get("dazn-token")
    return _jwt_payload(token) if token else {}


def _jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_proxad_oqee_live(streams) -> bool:
    return any(_is_proxad_oqee_stream(stream) for stream in streams)


def _is_proxad_oqee_stream(stream) -> bool:
    urls = [
        getattr(stream, "url", None),
        getattr(stream, "original_url", None),
        (getattr(stream, "extra", {}) or {}).get("dash_template_base_uri"),
    ]
    urls.extend(segment.url for segment in (getattr(stream, "segments", []) or [])[:3])
    for value in urls:
        if not isinstance(value, str) or not value:
            continue
        parsed = urlparse(value)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "api-proxad.oqee.net" and "/playlist/v1/live/" in path:
            return True
        if host.endswith(".stream.proxad.net") or host == "media4.stream.proxad.net":
            return True
    return False


def _dash_initial_presentation_start(stream, args) -> float | None:
    extra = getattr(stream, "extra", {}) or {}
    if not extra.get("dash_index_is_timeline"):
        return None
    timescale = _positive_int(extra.get("dash_timescale"))
    if not timescale:
        return None
    media_segments = [
        segment
        for segment in getattr(stream, "segments", []) or []
        if getattr(segment, "index", None) is not None and segment.index >= 0
    ]
    media_segments = _initial_media_segments(media_segments, args)
    if not media_segments:
        return None
    first_presentation_time = getattr(media_segments[0], "timeline_presentation_time", None)
    if first_presentation_time is not None:
        try:
            return float(first_presentation_time)
        except (TypeError, ValueError):
            pass
    first_time = getattr(media_segments[0], "timeline_time", None)
    if first_time is None:
        first_time = getattr(media_segments[0], "index", None)
    if first_time is None or first_time < 0:
        return None
    try:
        period_start = float(extra.get("period_start") or 0.0)
        presentation_offset = float(extra.get("dash_presentation_time_offset") or 0.0)
    except (TypeError, ValueError):
        period_start = 0.0
        presentation_offset = 0.0
    return period_start + float(first_time) / timescale - presentation_offset


def _initial_media_segments(segments: list, args) -> list:
    if not segments:
        return []
    dvr_start = _parse_live_offset(getattr(args, "live_dvr_start_at", None))
    if getattr(args, "live_dvr_from_start", False) or dvr_start is not None:
        return _segments_from_offset(segments, dvr_start)
    take_count = int(getattr(args, "live_take_count", 0) or 0)
    if take_count > 0 and len(segments) > take_count:
        return segments[-take_count:]
    return segments


def _segments_from_offset(segments: list, offset: float | None) -> list:
    if not offset or offset <= 0:
        return segments
    elapsed = 0.0
    for index, segment in enumerate(segments):
        duration = float(getattr(segment, "duration", None) or 0.0)
        if elapsed + duration > offset:
            return segments[index:]
        elapsed += duration
    return []


def _parse_live_offset(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    if ":" in value:
        parts = [float(part) for part in value.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    return None


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
