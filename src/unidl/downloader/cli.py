from __future__ import annotations

import argparse
import builtins
import contextlib
import contextvars
import copy
import hashlib
import json
import math
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlunparse

from . import __version__, display
from .applemusic_decrypt import decrypt_apple_music_fmp4_parts
from .audio import (
    audio_id3_metadata,
    audio_metadata_signature,
    load_audio_metadata_file,
    prepare_audio_cover,
    transcode_audio,
)
from .bbts import decrypt_bbts_file
from .cenc_fragment import CencFragmentKeyError, fragment_cenc_key_ids_from_bytes
from .chapters import ChapterFileError, load_chapters_file
from .console import Palette, color_enabled, paint
from .deezer import BF_CBC_STRIPE, decrypt_deezer_file, normalize_deezer_cipher
from .downloader import (
    DownloadError,
    HlsCrypto,
    ProgressUpdate,
    _default_tmp_root,
    _resume_url_key,
    _stream_resume_key,
    download_stream,
    fetch_segment_probe_bytes,
    prepare_dvr_sequence_windows,
    prepare_remote_stream_download_plan,
    set_allow_insecure_localhost_hls_keys,
)
from .embedding import (
    DownloadArtifact,
    DownloadCancelled,
    DownloadHooks,
    DownloadMessage,
    DownloadProgress,
    LiveKeyProvider,
    LiveKeyRequest,
    current_download_runtime,
    managed_popen,
    managed_run,
)
from .live import (
    LiveProgressUpdate,
    LiveRecordOptions,
    LiveSegmentBatch,
    _ensure_live_json_init_segments,
    parse_live_limit,
    record_live_stream,
    record_live_stream_group,
)
from .live_rules import (
    apply_audio_vivid_policy,
    child_request_header_warnings,
    child_url_error_tip,
    child_url_params,
    decrypt_tencentvideo_cenc_parts,
    iqiyi_separate_audio_mux_media_type,
    live_pipe_input_offsets_seconds,
    live_pipe_matroska_options,
    live_pipe_media_part_contains_init,
    live_pipe_mux_disabled_reason,
    mark_yangshipin_casting_streams,
    postprocess_audio_vivid,
    should_append_child_url_params,
    should_finalize_live_pipe_matroska_output,
    should_finalize_live_pipe_matroska_output_to_mp4,
    should_preserve_audio_vivid_source_container,
    should_treat_live_stream_as_fragmented_mp4,
    tencentvideo_separate_audio_custom_hls_applies,
    tencentvideo_separate_audio_mux_media_type,
    yangshipin_casting_live_pipe_map_specs,
)
from .loader import LoadError, normalize_headers
from .models import SegmentInfo
from .parser import parse_source
from .postprocess import (
    MuxInput,
    RawKey,
    _mp4_normalize_large_sample_durations,
    concat_media_files,
    decrypt_file,
    decrypt_fragmented_mp4_part,
    decrypt_fragmented_mp4_parts,
    decrypt_sections,
    fragmented_mp4_timing,
    mp4_tenc_default_kids,
    mp4_tenc_default_kids_from_bytes,
    mux_files,
    normalize_decrypted_mp4_init,
    parse_key_text_file,
    parse_keys,
    parse_mux_import,
    patch_mp4_tenc_default_kid,
    repackage_ffmpeg,
    restamp_fragmented_mp4_sequence,
    restamp_fragmented_mp4_timestamps,
    split_fragmented_mp4_init_media,
)
from .selection import SelectionOptions, select_streams
from .subtitles import SubtitleConversionError, convert_subtitle_file
from .utils import (
    compact_join,
    format_bitrate,
    format_frame_rate,
    format_size,
    format_time,
    looks_like_h266,
    pretty_codec,
    unique_path,
    video_codec_family,
)
from .vgc import VgcError, finalize_vgc_track, is_vgc_stream, prepare_vgc_streams
from .webm_decrypt import decrypt_webm_file, decrypt_webm_parts, select_webm_key, webm_key_ids, webm_key_ids_from_bytes
from .webm_live import ContinuousWebMWriter

try:
    import termios
except ImportError:
    class _TermiosUnavailable:
        error = OSError
        TCIFLUSH = 0
        TCSADRAIN = 0

        def tcgetattr(self, *_args):
            raise OSError("termios is unavailable")

        def tcsetattr(self, *_args):
            raise OSError("termios is unavailable")

        def tcflush(self, *_args):
            raise OSError("termios is unavailable")

    termios = _TermiosUnavailable()

try:
    import tty
except ImportError:
    class _TtyUnavailable:
        def setcbreak(self, *_args):
            raise OSError("tty is unavailable")

    tty = _TtyUnavailable()


_BUILTIN_PRINT = builtins.print
_OUTPUT_HOOKS: contextvars.ContextVar[DownloadHooks | None] = contextvars.ContextVar(
    "unidl_downloader_output_hooks",
    default=None,
)


@contextlib.contextmanager
def _embedding_output(hooks: DownloadHooks):
    """Route this execution's ordinary messages without replacing stdio.

    Context-local routing lets an embedded run coexist with Textual and with
    unrelated threads that still own the process terminal. Worker status paths
    receive an explicit callback below because executor threads do not inherit a
    caller's context automatically.
    """

    token = _OUTPUT_HOOKS.set(hooks)
    try:
        yield
    finally:
        _OUTPUT_HOOKS.reset(token)


def _message_level(text: str, *, stderr: bool = False) -> str:
    lowered = str(text or "").lower()
    if stderr or "error:" in lowered or "failed:" in lowered:
        return "error"
    if "warning:" in lowered or "note:" in lowered:
        return "warning"
    return "info"


def _publish_message(
    hooks: DownloadHooks,
    text: str,
    *,
    stderr: bool = False,
    transient: bool = False,
) -> None:
    if hooks.message is not None:
        hooks.message(
            DownloadMessage(
                str(text),
                level=_message_level(str(text), stderr=stderr),
                transient=transient,
            )
        )


def _terminal_size(fallback: tuple[int, int] = (80, 24)) -> os.terminal_size:
    size = shutil.get_terminal_size(fallback)
    hooks = _OUTPUT_HOOKS.get()
    if hooks is None or hooks.display_width is None:
        return size
    width = hooks.display_width()
    if not width:
        return size
    return os.terminal_size((max(40, int(width)), size.lines))


def print(*values, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
    """Module-local print that preserves CLI output and supports embedding."""

    hooks = _OUTPUT_HOOKS.get()
    target = sys.stdout if file is None else file
    console_stream = file is None or target is sys.stdout or target is sys.stderr
    if hooks is not None and console_stream:
        text = sep.join(str(value) for value in values)
        if end not in {"", "\n"}:
            text += end
        _publish_message(hooks, text, stderr=target is sys.stderr)
        if not hooks.console_output:
            return
    _BUILTIN_PRINT(*values, sep=sep, end=end, file=file, flush=flush)


def _embedding_message_emitter(
    args: argparse.Namespace,
    *,
    transient: bool = False,
) -> Callable[[str], None] | None:
    hooks = getattr(args, "embedding_hooks", None)
    if not isinstance(hooks, DownloadHooks):
        return None
    if hooks.message is None and hooks.console_output:
        return None

    def emit(text: str) -> None:
        _publish_message(hooks, text, transient=transient)
        if hooks.console_output:
            _BUILTIN_PRINT(text)

    return emit


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Handled before subcommand insertion so `unidl --version` is not rewritten
    # into `unidl list --version`.
    if argv and argv[0] in {"--version", "-V"}:
        print(f"unidl {__version__}")
        return 0
    if argv and argv[0] not in {"list", "download", "-h", "--help"}:
        argv.insert(0, "download" if _looks_like_download(argv) else "list")
    argv = _split_attached_short_flags(argv)
    argv = _normalize_bool_option_values(argv)
    parser = _build_parser()
    args = parser.parse_args(argv or None)

    try:
        if args.command == "download":
            return _download(args)
        return _list(args)
    except KeyboardInterrupt:
        print("\n" + paint("Cancelled.", Palette.yellow, color_enabled()), file=sys.stderr)
        if "args" in locals():
            _log_line(args, "Cancelled.")
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(130)
    except LoadError as exc:
        _print_load_error(exc, args.input if hasattr(args, "input") else None, colors=_colors(args))
        if "args" in locals():
            _log_line(args, f"Load error: {_short_error(exc)}")
        return 2
    except (DownloadError, RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        _print_task_error(exc, colors=_colors(args) if "args" in locals() else None)
        if "args" in locals():
            _log_line(args, f"Error: {_short_error(exc)}")
        return 1


_DOWNLOAD_EXAMPLES = """\
Examples:
  unidl list "https://example.com/video.mpd"
  unidl list input.m3u8 --details
  unidl download "https://example.com/video.mpd" -sv best -sa best
  unidl download input.mpd -sv 'res=1080:range=hdr' -sa 'lang=en:for=best' --save-name Movie
  unidl download input.mpd -sv best -sa best --key KID:KEY --save-name Movie
  unidl download live.m3u8 -sv best -sa best --live-record-limit 01:30:00
"""


def _build_parser(
    parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser = parser_class(
        prog="unidl",
        description="Unified HLS/DASH/ISM/direct media parser and downloader.",
        epilog=_DOWNLOAD_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"unidl {__version__}")
    subparsers = parser.add_subparsers(dest="command", parser_class=parser_class)

    list_parser = subparsers.add_parser("list", help="Parse input and list streams.")
    _add_common_parse_args(list_parser)
    list_parser.add_argument("--json", action="store_true", help="Print stream data as JSON.")
    list_parser.add_argument("--segments", action="store_true", help="Include segment URLs in JSON output.")
    list_parser.add_argument("--choose", action="store_true", help="Prompt with an interactive checklist after listing.")
    list_parser.add_argument("--no-color", "--no-ansi-color", dest="no_color", action="store_true", help="Disable ANSI colors.")
    list_parser.add_argument("--force-ansi-console", action="store_true", help="Force ANSI colors.")
    list_parser.add_argument("--ad-keyword", action="append", help="Drop media segments whose URL matches this regex. Can be repeated.")
    _add_filter_args(list_parser)

    download_parser = subparsers.add_parser(
        "download",
        help="Download one or more selected streams.",
        usage="unidl download [options] INPUT",
        epilog=_DOWNLOAD_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_parse_args(download_parser)
    _add_filter_args(download_parser)

    g_select = download_parser.add_argument_group("Track selection")
    g_output = download_parser.add_argument_group("Output and naming")
    g_net = download_parser.add_argument_group("Downloading and network")
    g_crypt = download_parser.add_argument_group("Decryption")
    g_text = download_parser.add_argument_group("Subtitles and audio export")
    g_mux = download_parser.add_argument_group("Muxing")
    g_live = download_parser.add_argument_group("Live recording")
    g_sabr = download_parser.add_argument_group("YouTube SABR/UMP")
    g_display = download_parser.add_argument_group("Display")
    g_parse = download_parser.add_argument_group("Parsing")

    g_select.add_argument("-s", "--select", help="Legacy selector: numeric list like 1,2, or 'best'/'best-av'.")
    g_display.add_argument("--no-color", "--no-ansi-color", dest="no_color", action="store_true", help="Disable ANSI colors.")
    g_display.add_argument("--force-ansi-console", action="store_true", help="Force ANSI colors and live progress rendering.")
    g_output.add_argument("-o", "--output", default=None, help="Output directory or final mux file when --mux is used.")
    g_output.add_argument("--save-dir", default=None, help="Set output directory, compatible with N_m3u8DL-RE.")
    g_output.add_argument("--name", dest="save_name", help="Base filename for a single downloaded track.")
    g_output.add_argument("--save-name", dest="save_name", help="Set output filename, compatible with N_m3u8DL-RE.")
    g_output.add_argument("--save-pattern", help="Track filename pattern, e.g. '<SaveName>_<Resolution>_<Bandwidth>_<MediaType>'.")
    g_output.add_argument("--task-start-at", metavar="yyyyMMddHHmmss", help="Wait until this local time before starting the task.")
    g_output.add_argument("--log-file-path", help="Write task log lines to this file.")
    g_output.add_argument("--write-meta-json", action="store_true", help="Write parsed stream metadata JSON next to the output.")
    g_net.add_argument("--workers", "--thread-count", dest="workers", type=int, default=16, help="Parallel segment workers.")
    g_net.add_argument("-mt", "--concurrent-download", action="store_true", help="Download selected video/audio/subtitle tracks concurrently.")
    g_net.add_argument("--retries", "--download-retry-count", dest="retries", type=int, default=5, help="Retry count for each segment. Default: 5.")
    g_net.add_argument("--http-request-timeout", type=int, default=30, help="HTTP request timeout in seconds. Default: 30.")
    g_net.add_argument("--check-segments-count", action=argparse.BooleanOptionalAction, default=True, help="Verify downloaded segment count matches the manifest. Default: true.")
    g_net.add_argument("--tmp-dir", help="Temporary segment cache directory. Default: <project>/tmp.")
    g_net.add_argument("--no-resume", action="store_true", help="Do not reuse previously downloaded segment parts.")
    g_net.add_argument("--downloader", choices=["auto", "python", "aria2c"], default="python", help="Segment downloader backend. Default: python. Use aria2c only when explicitly requested.")
    g_net.add_argument("-R", "--max-speed", help="Limit download speed, e.g. 15M, 100K, 2Mbps.")
    g_net.add_argument("--dash-full-base-url", action="store_true", help="For DASH SegmentList byte-range VOD, download the full underlying BaseURL media file instead of the MPD byte ranges.")
    g_net.add_argument("--keep-temp", action="store_true", help="Keep temporary segment directories.")
    g_net.add_argument("--del-after-done", dest="del_after_done", action="store_true", default=True, help="Delete temporary segment directories after a successful task. Default: true.")
    g_net.add_argument("--no-del-after-done", dest="del_after_done", action="store_false", help="Keep temporary segment directories after a successful task.")
    g_select.add_argument("--auto-select", action="store_true", help="Automatically select best video and best audio.")
    g_select.add_argument("--sub-only", action="store_true", help="Only select subtitle tracks.")
    g_text.add_argument("--sub-format", choices=["srt", "vtt", "raw", "SRT", "VTT", "RAW"], default="srt", help="Subtitle output format. Default: srt.")
    g_text.add_argument("--audio-format", type=str.lower, choices=["mp3", "flac", "alac", "m4a"], help="Export selected audio tracks as MP3, FLAC, ALAC in M4A, or a codec-preserving M4A; live audio is finalized after recording stops.")
    g_text.add_argument("--audio-metadata-file", help="Read audio metadata and cover configuration from a JSON sidecar file.")
    g_text.add_argument("--decode-audio-vivid", action="store_true", help="Inspect downloaded TS/MP4 audio and decode only confirmed Audio Vivid tracks.")
    g_text.add_argument("--audio-vivid-decoder", help="Audio Vivid decoder executable used by an enabled service audio policy.")
    g_text.add_argument("--audio-vivid-decoder-args", help="Decoder arguments containing {input} and {output}.")
    g_text.add_argument("--auto-subtitle-fix", action=argparse.BooleanOptionalAction, default=True, help="Clean and de-duplicate text subtitle cues during conversion. Default: true.")
    g_crypt.add_argument("--key", action="append", help="Raw decryption key as KID:KEY or KEY. Can be repeated.")
    g_crypt.add_argument("--key-text-file", help="Read raw keys from a text file, one KID:KEY or KEY per line.")
    g_crypt.add_argument(
        "--decrypter",
        "--decryption-engine",
        choices=["auto", "internal", "mp4decrypt", "packager", "MP4DECRYPT", "SHAKA_PACKAGER"],
        default="auto",
        help="Decryption engine. Compatibility aliases are accepted, but all decryption uses the internal engine.",
    )
    g_crypt.add_argument("--no-decrypt", action="store_true", help="Skip decryption even if stream is marked encrypted.")
    g_mux.add_argument("--repack", action="store_true", help="Run ffmpeg -c copy after decryption/download.")
    g_mux.add_argument("--mux", "-M", "--mux-after-done", action="store_true", help="Mux selected tracks into one output file.")
    g_mux.add_argument("--no-mux", "--skip-merge", dest="no_mux", action="store_true", help="Do not mux automatically when multiple tracks are selected.")
    g_mux.add_argument("--mux-import", action="append", help='Import external media during mux, e.g. --mux-import "path=sub.srt:lang=eng:name=English".')
    g_mux.add_argument("--chapters-file", help="Read a UniDL JSON chapter sidecar and embed it in the final video container.")
    g_mux.add_argument("--muxer", choices=["auto", "ffmpeg", "mkvmerge"], default="auto")
    g_mux.add_argument("--mux-format", choices=["mkv", "mp4", "ts"], help="Final mux container. Defaults to mkv for VOD and ts for live.")
    g_net.add_argument("--custom-range", help="Only download selected media segment range, e.g. 1-100,120-160.")
    g_crypt.add_argument("--custom-hls-method", choices=["AES_128", "AES_128_ECB", "BBTS", "CENC", "CHACHA20", "NONE", "SAMPLE_AES", "SAMPLE_AES_CTR", "UNKNOWN", "YOUKU_ECB"])
    g_crypt.add_argument("--custom-hls-key", help="Custom HLS key as FILE, HEX, or Base64.")
    g_crypt.add_argument("--custom-hls-iv", help="Custom HLS IV as FILE, HEX, or Base64.")
    g_crypt.add_argument("--allow-hls-multi-ext-map", action="store_true", help="Allow multiple EXT-X-MAP sections. Enabled by default in UniDL.")
    g_crypt.add_argument("--vgc", action="store_true", help="Enable VideoGuard/VGC bridge handling for Sky-style HLS streams.")
    g_crypt.add_argument("--vgc-keep-opaque", action="store_true", help="Keep raw VGC payloads even when they are not clear/playable media.")
    g_parse.add_argument("--ad-keyword", action="append", help="Drop media segments whose URL matches this regex. Can be repeated.")
    g_sabr.add_argument("--sabr-po-token", help="YouTube SABR/UMP content/session PO token for high-rendition requests.")
    g_sabr.add_argument("--sabr-po-token-file", help="Read YouTube SABR/UMP PO token bytes from a file.")
    g_sabr.add_argument("--sabr-playback-cookie", help="Optional YouTube SABR/UMP playback cookie field.")
    g_sabr.add_argument("--sabr-fk", help="Optional YouTube SABR/UMP fk field.")
    g_live.add_argument("--live-perform-as-vod", "--live-dvr-as-vod", dest="live_perform_as_vod", action="store_true", help="Download the current live/DVR window once as VOD instead of recording.")
    g_live.add_argument("--live-dvr-from-start", "--live-start-from-dvr", action=argparse.BooleanOptionalAction, default=False, help="Start live recording from the beginning of the current DVR window instead of the live edge.")
    g_live.add_argument("--live-dvr-start-at", "--live-dvr-start-offset", help="Start live recording at an offset from the current DVR window beginning, e.g. 01:10:00.")
    g_live.add_argument("--live-dvr-end-at", "--live-dvr-end-offset", help="Stop DVR recording at an offset from the current DVR window beginning. Requires --live-dvr-start-at.")
    g_live.add_argument("--live-real-time-merge", action=argparse.BooleanOptionalAction, default=False, help="Append live segments to output while recording.")
    g_live.add_argument("--live-keep-segments", action=argparse.BooleanOptionalAction, default=True, help="Keep temporary live segment files.")
    g_live.add_argument("--live-pipe-mux", action=argparse.BooleanOptionalAction, default=False, help="Mux live audio/video after recording; subtitle tracks are kept as sidecar files for TS pipe compatibility.")
    g_live.add_argument("--live-keep-track-files", action=argparse.BooleanOptionalAction, default=False, help="Keep per-track audio/video files when live pipe mux also writes a final output. Default: false.")
    g_live.add_argument("--live-record-limit", metavar="HH:mm:ss", help="Recording duration limit for live mode, e.g. 01:30:00, 300, 1h20m.")
    g_live.add_argument("--live-wait-time", type=int, help="Manually set live playlist refresh interval in seconds.")
    g_live.add_argument("--live-take-count", type=int, default=16, help="Initial live segments to record. Default: 16.")
    return parser


def _looks_like_download(argv: list[str]) -> bool:
    download_markers = {
        "-v",
        "--video",
        "-a",
        "--audio",
        "-vl",
        "--video-lang",
        "-al",
        "--audio-lang",
        "-sl",
        "--sub-lang",
        "--subtitle-lang",
        "-r",
        "--range",
        "-at",
        "--audio-type",
        "-sv",
        "--select-video",
        "-sa",
        "--select-audio",
        "-ss",
        "--select-subtitle",
        "--save-name",
        "--save-pattern",
        "--save-dir",
        "--task-start-at",
        "--log-file-path",
        "--write-meta-json",
        "--base-url",
        "--append-url-params",
        "--thread-count",
        "-mt",
        "--concurrent-download",
        "--download-retry-count",
        "--http-request-timeout",
        "--check-segments-count",
        "--no-check-segments-count",
        "--auto-select",
        "--sub-only",
        "--sub-format",
        "--audio-format",
        "--audio-metadata-file",
        "--decode-audio-vivid",
        "--audio-vivid-decoder",
        "--audio-vivid-decoder-args",
        "--auto-subtitle-fix",
        "--no-auto-subtitle-fix",
        "--key",
        "--key-text-file",
        "--retries",
        "--tmp-dir",
        "--no-resume",
        "--del-after-done",
        "--no-del-after-done",
        "--keep-temp",
        "--mux",
        "-M",
        "--mux-after-done",
        "--no-mux",
        "--skip-merge",
        "--mux-import",
        "--chapters-file",
        "--mux-format",
        "-R",
        "--max-speed",
        "--custom-range",
        "--custom-hls-method",
        "--custom-hls-key",
        "--custom-hls-iv",
        "--allow-hls-multi-ext-map",
        "--vgc",
        "--vgc-keep-opaque",
        "--ad-keyword",
        "--custom-proxy",
        "--use-system-proxy",
        "--no-use-system-proxy",
        "-dv",
        "--drop-video",
        "-da",
        "--drop-audio",
        "-ds",
        "--drop-subtitle",
        "--no-ansi-color",
        "--force-ansi-console",
        "--live-perform-as-vod",
        "--live-dvr-as-vod",
        "--live-dvr-from-start",
        "--live-start-from-dvr",
        "--no-live-dvr-from-start",
        "--live-dvr-start-at",
        "--live-dvr-start-offset",
        "--live-dvr-end-at",
        "--live-dvr-end-offset",
        "--live-real-time-merge",
        "--no-live-real-time-merge",
        "--live-keep-segments",
        "--no-live-keep-segments",
        "--live-pipe-mux",
        "--no-live-pipe-mux",
        "--live-keep-track-files",
        "--no-live-keep-track-files",
        "--live-record-limit",
        "--live-wait-time",
        "--live-take-count",
        # Download-only options the README shows without the subcommand.
        # Omitting them sent these through the list parser, which rejected
        # them with an argparse error instead of downloading.
        "-s",
        "--select",
        "-o",
        "--output",
        "--downloader",
        "--workers",
        "--no-decrypt",
        "--decrypter",
        "--decryption-engine",
        "--repack",
        "--muxer",
        "--dash-full-base-url",
        "--sabr-po-token",
        "--sabr-po-token-file",
        "--sabr-playback-cookie",
        "--sabr-fk",
    }
    return any(
        item in download_markers
        or item.startswith(
            (
                "--select=",
                "--output=",
                "--downloader=",
                "--workers=",
                "--thread-count=",
                "--decrypter=",
                "--decryption-engine=",
                "--muxer=",
                "--retries=",
                "--download-retry-count=",
                "--http-request-timeout=",
                "--task-start-at=",
                "--log-file-path=",
                "--sabr-po-token=",
                "--sabr-po-token-file=",
                "--sabr-playback-cookie=",
                "--sabr-fk=",
                "--key=",
                "--save-name=",
                "--save-pattern=",
                "--save-dir=",
                "--task-start-at=",
                "--log-file-path=",
                "--base-url=",
                "--thread-count=",
                "--download-retry-count=",
                "--http-request-timeout=",
                "--retries=",
                "--tmp-dir=",
                "--mux-format=",
                "--key-text-file=",
                "--mux-import=",
                "--chapters-file=",
                "--max-speed=",
                "--custom-range=",
                "--custom-hls-method=",
                "--custom-hls-key=",
                "--custom-hls-iv=",
                "--custom-proxy=",
                "--sub-format=",
                "--audio-format=",
                "--audio-metadata-file=",
                "--ad-keyword=",
                "--drop-video=",
                "--drop-audio=",
                "--drop-subtitle=",
                "--video=",
                "--audio=",
                "--video-lang=",
                "--audio-lang=",
                "--sub-lang=",
                "--subtitle-lang=",
                "--range=",
                "--audio-type=",
                "--select-video=",
                "--select-audio=",
                "--select-subtitle=",
                "--live-record-limit=",
                "--live-wait-time=",
                "--live-take-count=",
                "--live-dvr-from-start=",
                "--live-start-from-dvr=",
                "--live-dvr-start-at=",
                "--live-dvr-start-offset=",
                "--live-dvr-end-at=",
                "--live-dvr-end-offset=",
                "--live-real-time-merge=",
                "--live-keep-segments=",
                "--live-pipe-mux=",
                "--live-keep-track-files=",
            )
        )
        for item in argv[1:]
    )


def _normalize_bool_option_values(argv: list[str]) -> list[str]:
    bool_options = {
        "--live-real-time-merge": "--no-live-real-time-merge",
        "--live-keep-segments": "--no-live-keep-segments",
        "--live-pipe-mux": "--no-live-pipe-mux",
        "--live-keep-track-files": "--no-live-keep-track-files",
        "--live-dvr-from-start": "--no-live-dvr-from-start",
        "--use-system-proxy": "--no-use-system-proxy",
    }
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if "=" in token:
            key, value = token.split("=", 1)
            if key in bool_options:
                value = value.strip().lower()
                if value in {"true", "1", "yes", "y", "on"}:
                    normalized.append(key)
                    index += 1
                    continue
                if value in {"false", "0", "no", "n", "off"}:
                    normalized.append(bool_options[key])
                    index += 1
                    continue
        if token in bool_options and index + 1 < len(argv):
            value = argv[index + 1].strip().lower()
            if value in {"true", "1", "yes", "y", "on"}:
                normalized.append(token)
                index += 2
                continue
            if value in {"false", "0", "no", "n", "off"}:
                normalized.append(bool_options[token])
                index += 2
                continue
        normalized.append(token)
        index += 1
    return normalized


def _split_attached_short_flags(argv: list[str]) -> list[str]:
    """Recover common shell typos such as --save-name "Title"-mt."""
    value_options = {"--save-name", "--name"}
    normalized: list[str] = []
    expecting_value_for: str | None = None
    for token in argv:
        if expecting_value_for:
            if token.endswith("-mt") and token != "-mt" and "-mt" not in normalized and "--concurrent-download" not in normalized:
                value = token[:-3].rstrip()
                if value:
                    normalized.append(value)
                normalized.append("-mt")
            else:
                normalized.append(token)
            expecting_value_for = None
            continue
        normalized.append(token)
        if token in value_options:
            expecting_value_for = token
    return normalized


def _add_common_parse_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="Remote URL or local .m3u/.m3u8/.mpd/.dash/.ism/.mp3/.mp4 file.")
    parser.add_argument("-H", "--header", action="append", help="HTTP header, e.g. 'Authorization: Bearer ...'.")
    parser.add_argument("--custom-proxy", help="HTTP/HTTPS proxy URL, e.g. http://127.0.0.1:8888.")
    parser.add_argument(
        "--use-system-proxy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use system proxy settings.",
    )
    parser.add_argument("--no-probe", action="store_true", help="Do not call ffprobe for direct MP3/MP4 inputs.")
    parser.add_argument("--no-child-playlists", action="store_true", help="Do not fetch HLS child playlists for duration/segment counts.")
    parser.add_argument("--details", action="store_true", help="Fetch all HLS child playlists while listing/selecting. Slower on large masters.")
    parser.add_argument("--base-url", help="Override manifest BaseURL for resolving relative child playlists and segments.")
    parser.add_argument("--append-url-params", action="store_true", help="Append input URL query parameters to child playlists and segments.")


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--video", help="Video height filter, e.g. 2160,1080,540, best, all.")
    parser.add_argument("-a", "--audio", help="Audio bitrate filter in Kbps, e.g. 128,448, best, all.")
    parser.add_argument("-vl", "--video-lang", help="Video language filter, e.g. en,es.")
    parser.add_argument("-al", "--audio-lang", help="Audio language filter, e.g. en,es.")
    parser.add_argument("-sl", "--sub-lang", "--subtitle-lang", dest="subtitle_lang", help="Subtitle language filter, e.g. en,es.")
    parser.add_argument("-r", "--range", dest="video_range", help="Video range filter, e.g. hdr,sdr,dv.")
    parser.add_argument("-at", "--audio-type", help="Audio type filter, e.g. atmos,ddplus,ac3,aac. Also accepts atoms typo.")
    parser.add_argument("-sv", "--select-video", help='N_m3u8DL-style subset, e.g. best, all, res="1080":range=hdr:for=best.')
    parser.add_argument("-sa", "--select-audio", help='N_m3u8DL-style subset, e.g. all, lang=en:for=best, bwMin=128:bwMax=448.')
    parser.add_argument("-ss", "--select-subtitle", help='N_m3u8DL-style subset, e.g. all, lang=en:for=all, name="English".')
    parser.add_argument("-dv", "--drop-video", help="Regex filter for video streams to remove.")
    parser.add_argument("-da", "--drop-audio", help="Regex filter for audio streams to remove.")
    parser.add_argument("-ds", "--drop-subtitle", help="Regex filter for subtitle streams to remove.")


def _list(args: argparse.Namespace) -> int:
    colors = _colors(args)
    _configure_proxy(args)
    headers = normalize_headers(args.header)
    args.append_url_params = args.append_url_params or should_append_child_url_params(args.input)
    if args.append_url_params:
        _print_child_request_header_warnings(args.input, headers, colors)
    streams = parse_source(
        args.input,
        headers=headers,
        probe_direct=not args.no_probe,
        fetch_child_playlists=args.details and not args.no_child_playlists,
        base_url=args.base_url,
    )
    if args.append_url_params:
        _append_input_params(streams, args.input)
    _filter_ad_segments(streams, args.ad_keyword, args=args, colors=colors)
    streams = _sort_streams(_drop_streams(streams, args))

    if args.json:
        print(json.dumps([stream.as_dict(include_segments=args.segments) for stream in streams], ensure_ascii=False, indent=2))
        return 0

    filter_options = _selection_options(args)
    selected = select_streams(streams, filter_options) if filter_options.has_filters else []
    if args.choose and not selected:
        selected = _prompt_checklist(streams, colors=colors)
        if selected is None:
            print(paint("Cancelled.", Palette.yellow, colors))
            return 130
    elif selected and not args.choose:
        print(paint("Matched:", Palette.green, colors))
        _print_streams(selected, original_streams=streams, colors=colors)
    else:
        _print_streams(streams, colors=colors)
    _print_dash_byte_range_note(selected or streams, colors=colors)
    if selected:
        print("\n" + paint("Selected URLs:", Palette.green, colors))
        for stream in selected:
            if stream.url:
                print(f"{streams.index(stream) + 1}: {stream.url}")
    return 0


def _download(args: argparse.Namespace) -> int:
    _checkpoint_embedding(args)
    colors = _colors(args)
    # Validate a user-supplied sidecar before fetching or downloading media. Core
    # generated sidecars are already valid, but the standalone CLI also accepts
    # hand-written files and should fail before spending network time on a bad one.
    chapter_records = (
        load_chapters_file(args.chapters_file)
        if getattr(args, "chapters_file", None)
        else ()
    )
    if chapter_records and not getattr(args, "no_mux", False) and str(
        getattr(args, "mux_format", "") or ""
    ).casefold().lstrip(".") in {"ts", "m2ts"}:
        raise ChapterFileError(
            "Chapters cannot be embedded in an MPEG-TS output; choose MKV/MP4 or use --no-mux."
        )
    _wait_for_task_start(args, colors)
    if getattr(args, "live_pipe_mux", False) and not getattr(args, "live_real_time_merge", False):
        args.live_real_time_merge = True
        print(paint("Note:", Palette.yellow, colors) + " --live-pipe-mux enables --live-real-time-merge.")
    _configure_proxy(args)
    headers = normalize_headers(args.header)
    args.append_url_params = args.append_url_params or should_append_child_url_params(args.input)
    if args.append_url_params:
        _print_child_request_header_warnings(args.input, headers, colors)
    # api.load_streams() may hand us the already parsed, filtered and sorted
    # ladder so an embedding front-end can select tracks without a second
    # network parse. Indexes then line up with what the caller displayed.
    preparsed = getattr(args, "preparsed_streams", None)
    if preparsed is not None:
        streams = list(preparsed)
    else:
        streams = parse_source(
            args.input,
            headers=headers,
            probe_direct=not args.no_probe,
            fetch_child_playlists=args.details and not args.no_child_playlists,
            base_url=args.base_url,
        )
        if args.append_url_params:
            _append_input_params(streams, args.input)
        _filter_ad_segments(streams, args.ad_keyword, args=args, colors=colors)
        streams = _sort_streams(_drop_streams(streams, args))
    _apply_apple_music_stream_context(streams, args)
    _checkpoint_embedding(args)
    apply_audio_vivid_policy(streams, bool(getattr(args, "decode_audio_vivid", False)))
    filter_options = _selection_options(args)
    selected = select_streams(streams, filter_options) if filter_options.has_filters else []
    if args.sub_only and not selected:
        selected = [stream for stream in streams if stream.media_type in {"subtitle", "subtitles", "text"}]
    if args.auto_select and not selected:
        selected = _select_legacy(streams, "best-av")
    if args.select and not selected:
        selected = _select_legacy(streams, args.select)
    if not selected and len(streams) == 1:
        selected = streams
    if not selected:
        selected = _prompt_checklist(streams, colors=colors)
        if selected is None:
            print(paint("Cancelled.", Palette.yellow, colors))
            return 130
    if not selected:
        print(paint("No stream selected.", Palette.yellow, colors))
        return 1
    selected = _replace_direct_audio_with_matching_sabr_audio(selected, streams)
    hydrated = [_hydrate_stream(stream, headers=headers, no_probe=args.no_probe, base_url=args.base_url) for stream in selected]
    if chapter_records and not any(stream.media_type == "video" for stream in hydrated):
        raise ChapterFileError("Chapter metadata requires at least one selected video track.")
    if getattr(args, "dash_full_base_url", False):
        converted = _apply_dash_full_base_url_mode(hydrated, headers=headers, no_probe=args.no_probe, colors=colors)
        if not converted:
            print(paint("Warning:", Palette.yellow, colors) + " --dash-full-base-url did not match any selected DASH byte-range BaseURL streams.", file=sys.stderr)
    else:
        _print_dash_byte_range_note(hydrated, colors=colors)
    if args.append_url_params:
        _append_input_params(hydrated, args.input)
    _filter_ad_segments(hydrated, args.ad_keyword, args=args, colors=colors)
    mark_yangshipin_casting_streams(hydrated, headers)
    hydrated = _replace_youtube_json_live_direct_with_hls(hydrated, args, headers, colors=colors)
    _validate_audio_format_request(hydrated, args)
    if getattr(args, "audio_metadata_file", None):
        _apply_audio_metadata_file(hydrated, args.audio_metadata_file)
    if getattr(args, "vgc", False):
        set_allow_insecure_localhost_hls_keys(True)
        prepared = prepare_vgc_streams(hydrated)
        if prepared:
            print(paint("VGC:", Palette.yellow, colors) + f" enabled for {prepared} HLS track(s); standard HLS AES handling is preserved.")
            print(paint("VGC:", Palette.yellow, colors) + " localhost VGC key endpoints may use device-local TLS certificates.")
        else:
            print(paint("Warning:", Palette.yellow, colors) + " --vgc did not match any selected HLS tracks.", file=sys.stderr)
    else:
        set_allow_insecure_localhost_hls_keys(False)
    keys = [*parse_keys(args.key), *parse_key_text_file(args.key_text_file)]
    mux_imports = [parse_mux_import(value) for value in (args.mux_import or [])]
    hls_crypto = HlsCrypto(method="NONE") if args.no_decrypt else _custom_hls_crypto(args)
    _apply_custom_hls_crypto_to_streams(hydrated, hls_crypto, explicit_method=bool(getattr(args, "custom_hls_method", None)))
    _apply_sabr_request_options(hydrated, args)
    if not args.no_probe:
        for stream in hydrated:
            prepare_remote_stream_download_plan(
                stream,
                headers=headers,
                hls_crypto=_hls_crypto_for_stream(stream, hls_crypto),
                request_timeout=max(1, args.http_request_timeout),
            )
            _hydrate_selected_stream_key_ids(stream, headers=headers, request_timeout=max(1, args.http_request_timeout), keys=keys)
    print()
    _print_selected_download_summary(hydrated, original_streams=streams, colors=colors)
    _log_line(args, "Selected: " + ", ".join(stream.format_line() for stream in hydrated))

    max_speed = _parse_speed(args.max_speed)
    if (
        args.downloader == "aria2c"
        and not args.no_decrypt
        and (hls_crypto and _normalized_scheme(hls_crypto.method) != "NONE" or any(_stream_uses_hls_segment_crypto(stream) for stream in hydrated))
    ):
        print(paint("Note:", Palette.yellow, colors) + " HLS segment decryption uses the python downloader.", file=sys.stderr)
        args.downloader = "python"
    output = _output_target(args)
    output_dir = output.parent if _output_is_file_target(args, output) else output
    output_dir.mkdir(parents=True, exist_ok=True)
    default_save_base = _default_save_base(args.input, hydrated) if not args.save_name else None
    task_temp_root = _task_temp_root(args, hydrated, default_save_base)
    _migrate_legacy_task_temp_roots(args, hydrated, default_save_base, task_temp_root)
    args._unidown_task_temp_root = task_temp_root
    downloaded_paths: list[Path] = []
    downloaded_tracks: list[_DownloadedTrack] = []
    metadata_paths: list[Path] = []
    temp_dirs: list[Path] = []
    intermediate_paths: list[Path] = []
    if args.custom_range:
        for stream in hydrated:
            _apply_custom_range(stream, args.custom_range)
    prepare_dvr_sequence_windows(
        hydrated,
        headers=headers,
        request_timeout=max(1, getattr(args, "http_request_timeout", 30)),
        retries=args.retries,
        probe_network=not bool(args.custom_range),
    )
    if args.write_meta_json:
        meta_path = _write_meta_json(output_dir, args.save_name, streams, hydrated)
        metadata_paths.append(meta_path)
        print(f"{paint('Meta:', Palette.green, colors)} {meta_path}")
        _log_line(args, f"Meta: {meta_path}")

    mux_target = output if _output_is_file_target(args, output) else output_dir
    live_streams = [stream for stream in hydrated if stream.is_live and not args.live_perform_as_vod]
    task_started_at = time.monotonic()
    if live_streams:
        pipe_session = _start_live_pipe_mux_session(live_streams, mux_target, args.save_name, default_save_base, args, keys, colors, hls_crypto=hls_crypto, temp_dir=_task_temp_subdir(args, "pipe"))
        try:
            downloaded_tracks = _record_live_streams(live_streams, streams, args, headers, keys, output, output_dir, pipe_session=pipe_session, hls_crypto=hls_crypto)
        except BaseException:
            if pipe_session:
                pipe_session.close(cancelled=True)
            raise
        else:
            if pipe_session:
                pipe_session.close(cancelled=False)
        downloaded_tracks = _with_audio_vivid_companions(downloaded_tracks)
        downloaded_paths = [track.path for track in downloaded_tracks]
        temp_dirs = [task_temp_root]
        intermediate_paths = [path for track in downloaded_tracks for path in track.cleanup_paths]
        protected_paths = list(downloaded_paths)
        if pipe_session:
            _TaskStatusLine(
                pipe_session.output_path,
                task_started_at,
                colors,
                _embedding_message_emitter(args, transient=True),
            ).update("Muxed ✓", done=True)
            _log_line(args, f"Muxed: {pipe_session.output_path}")
            _log_line(args, f"Elapsed: {format_time(max(0.0, time.monotonic() - task_started_at)) or '0s'}")
            protected_paths = [
                pipe_session.output_path,
                *[track.path for track in downloaded_tracks if _is_subtitle_stream(track.stream)],
            ]
            if _has_subtitle_tracks(downloaded_tracks):
                print(paint("Live subtitles kept as sidecar files.", Palette.yellow, colors))
        elif _should_mux_after_download(args, mux_imports, downloaded_paths, live_streams):
            live_mux_tracks = _live_mux_tracks(downloaded_tracks, args)
            mux_output = _mux_output_path(
                mux_target,
                args.save_name,
                default_save_base,
                _mux_format_for_tracks(live_mux_tracks, args, live=True),
            )
            explicit_mux = getattr(args, "mux", False) or bool(mux_imports)
            if live_mux_tracks and (len(live_mux_tracks) > 1 or explicit_mux):
                task_line = _TaskStatusLine(
                    mux_output,
                    task_started_at,
                    colors,
                    _embedding_message_emitter(args, transient=True),
                )
                with task_line.spinning("Muxing {spinner}"):
                    muxed = mux_files(
                        _mux_inputs_for_tracks(live_mux_tracks),
                        mux_output,
                        muxer=_muxer_for_tracks(live_mux_tracks, args, live=True),
                        imports=mux_imports,
                        chapters_file=getattr(args, "chapters_file", None),
                        force_vvc_mp4=False,
                    )
                task_line.update("Muxed ✓", done=True)
                _log_line(args, f"Muxed: {muxed}")
                _log_line(args, f"Elapsed: {format_time(max(0.0, time.monotonic() - task_started_at)) or '0s'}")
                protected_paths = [muxed, *[track.path for track in downloaded_tracks if _is_subtitle_stream(track.stream)]]
            elif mux_imports:
                task_line = _TaskStatusLine(
                    mux_output,
                    task_started_at,
                    colors,
                    _embedding_message_emitter(args, transient=True),
                )
                with task_line.spinning("Muxing {spinner}"):
                    muxed = mux_files(
                        [],
                        mux_output,
                        muxer=args.muxer,
                        imports=mux_imports,
                        chapters_file=getattr(args, "chapters_file", None),
                    )
                task_line.update("Muxed ✓", done=True)
                _log_line(args, f"Muxed: {muxed}")
                _log_line(args, f"Elapsed: {format_time(max(0.0, time.monotonic() - task_started_at)) or '0s'}")
                protected_paths = [muxed, *[track.path for track in downloaded_tracks if _is_subtitle_stream(track.stream)]]
            if _has_subtitle_tracks(downloaded_tracks) and len(live_mux_tracks) != len(downloaded_tracks):
                print(paint("Live subtitles kept as sidecar files.", Palette.yellow, colors))
        _cleanup_intermediate_files(intermediate_paths, protected=protected_paths, args=args, colors=colors)
        if not getattr(args, "live_keep_segments", True):
            _cleanup_temp_dirs(temp_dirs, args, colors)
        _print_cleanup_summary(args, colors)
        _emit_download_artifacts(
            args,
            protected_paths,
            downloaded_tracks,
            metadata_paths,
        )
        return 0

    if args.concurrent_download and len(hydrated) > 1:
        track_workers = _concurrent_track_workers(args.workers, len(hydrated))
        print(paint("Concurrent download:", Palette.green, colors) + f" {len(hydrated)} tracks, {track_workers} workers/track")
        panel = _MultiDownloadProgress(hydrated, colors=colors, force=getattr(args, "force_ansi_console", False))
        if not _embedding_console_progress(args):
            panel.enabled = False
        pool = ThreadPoolExecutor(max_workers=len(hydrated))
        futures = {}
        completed: list[_DownloadedTrack] = []
        try:
            for offset, stream in enumerate(hydrated, start=1):
                context = contextvars.copy_context()
                futures[
                    pool.submit(
                        context.run,
                        _download_selected_stream,
                        stream,
                        offset,
                        len(hydrated),
                        streams,
                        args,
                        headers,
                        keys,
                        hls_crypto,
                        max_speed,
                        output_dir,
                        default_save_base,
                        False,
                        progress_callback=panel.progress_for(offset) if panel.enabled else None,
                        status_printer=panel.print if panel.enabled else None,
                        status_callback=panel.status_for(offset) if panel.enabled else None,
                        workers=track_workers,
                    )
                ] = offset
            for future in as_completed(futures):
                completed.append(future.result())
        except BaseException:
            _cancel_futures_now(pool, futures)
            raise
        else:
            pool.shutdown(wait=True)
        finally:
            panel.close()
        completed = sorted(completed, key=lambda item: item.offset)
        downloaded_tracks = completed
        downloaded_paths = [item.path for item in completed]
        temp_dirs = [task_temp_root]
        intermediate_paths = [path for item in completed for path in item.cleanup_paths]
    else:
        for offset, stream in enumerate(hydrated, start=1):
            _checkpoint_embedding(args)
            item = _download_selected_stream(
                stream,
                offset,
                len(hydrated),
                streams,
                args,
                headers,
                keys,
                hls_crypto,
                max_speed,
                output_dir,
                default_save_base,
                True,
            )
            downloaded_tracks.append(item)
            downloaded_paths.append(item.path)
            temp_dirs = [task_temp_root]
            intermediate_paths.extend(item.cleanup_paths)

    downloaded_tracks = _with_audio_vivid_companions(downloaded_tracks)
    downloaded_paths = [track.path for track in downloaded_tracks]
    intermediate_paths = [
        path for track in downloaded_tracks for path in track.cleanup_paths
    ]

    should_mux = _should_mux_after_download(args, mux_imports, downloaded_paths, hydrated)
    protected_paths = list(downloaded_paths)
    if should_mux:
        # Muxing needs another output-sized allocation. Release resumable
        # segment caches before checking free space and starting the muxer.
        _cleanup_temp_dirs(temp_dirs, args, colors)
        vvc_vod = _tracks_include_vvc(downloaded_tracks)
        mux_format = _mux_format_for_tracks(downloaded_tracks, args, live=False)
        mux_output = _mux_output_path(
            mux_target,
            args.save_name,
            default_save_base,
            mux_format,
            force_suffix=vvc_vod,
        )
        task_line = _TaskStatusLine(
            mux_output,
            task_started_at,
            colors,
            _embedding_message_emitter(args, transient=True),
        )
        with task_line.spinning("Muxing {spinner}"):
            muxed = mux_files(
                _mux_inputs_for_tracks(downloaded_tracks),
                mux_output,
                muxer=_muxer_for_tracks(downloaded_tracks, args, live=False),
                imports=mux_imports,
                chapters_file=getattr(args, "chapters_file", None),
                force_vvc_mp4=True,
            )
        task_line.update("Muxed ✓", done=True)
        _log_line(args, f"Muxed: {muxed}")
        _log_line(args, f"Elapsed: {format_time(max(0.0, time.monotonic() - task_started_at)) or '0s'}")
        protected_paths = [muxed]
    _cleanup_intermediate_files(intermediate_paths, protected=protected_paths, args=args, colors=colors)
    _cleanup_temp_dirs(temp_dirs, args, colors)
    _print_cleanup_summary(args, colors)
    _emit_download_artifacts(
        args,
        protected_paths,
        downloaded_tracks,
        metadata_paths,
    )
    return 0


def _print_status_line(label: str, value: object, label_color: str, colors: bool | None = None) -> None:
    print(f"{paint(label, label_color, colors)} {value}")


def _emit_status_line(emit: Callable[[str], None], label: str, value: object, label_color: str, colors: bool | None = None) -> None:
    emit(f"{paint(label, label_color, colors)} {value}")


def _new_decrypt_event_recorder(args: argparse.Namespace, context: str | None = None) -> tuple[list[str], Callable[[str], None]]:
    events: list[str] = []
    seen: set[str] = set()

    def record(message: str) -> None:
        cleaned = str(message).strip()
        if not cleaned or cleaned in seen:
            return
        if _is_decrypt_engine_event(cleaned):
            return
        seen.add(cleaned)
        events.append(cleaned)
        _log_line(args, f"Decrypt event: {_decrypt_event_display_value(cleaned, context)}")

    return events, record


def _emit_decrypt_events(events: Sequence[str], emit: Callable[[str], None], colors: bool | None, context: str | None = None) -> None:
    for message in events:
        if _is_decrypt_engine_event(message):
            continue
        label, value = _decrypt_event_label_value(message)
        value = _decrypt_event_display_value(value, context)
        _emit_status_line(emit, label, value, _decrypt_event_color(label), colors)


def _flush_track_decrypt_events(
    status: _TrackStatusReporter,
    events: Sequence[str],
    emit: Callable[[str], None],
    colors: bool | None,
    context: str | None = None,
) -> None:
    if not events:
        return
    status.finish()
    _emit_decrypt_events(events, emit, colors, context)


def _decrypt_event_display_value(value: str, context: str | None = None) -> str:
    return f"{context} | {value}" if context else value


def _decrypt_event_context(index: int | None, stream) -> str:
    prefix = stream.display_prefix()
    if index is not None:
        prefix = f"{index}. {prefix}"
    if stream.media_type == "video":
        details = compact_join([stream.resolution, format_bitrate(stream.bandwidth), pretty_codec(stream.codecs, stream.media_type)])
    elif stream.media_type == "audio":
        details = compact_join([stream.language, format_bitrate(stream.bandwidth), pretty_codec(stream.codecs, stream.media_type)])
    elif _is_subtitle_stream(stream):
        details = compact_join([stream.language, stream.name, pretty_codec(stream.codecs, stream.media_type)])
    else:
        details = compact_join([stream.language, stream.id, pretty_codec(stream.codecs, stream.media_type)])
    return f"{prefix} {details}".strip()


def _decrypt_event_label_value(message: str) -> tuple[str, str]:
    if ":" not in message:
        return "Decrypt:", message
    head, value = message.split(":", 1)
    normalized = head.strip().lower()
    if normalized in {"engine", "fallback", "failed"}:
        return f"Decrypt {normalized}:", value.strip()
    if normalized.startswith("decrypt "):
        return f"{head.strip()}:", value.strip()
    return "Decrypt:", message


def _is_decrypt_engine_event(message: str) -> bool:
    if ":" not in message:
        return False
    head, _value = message.split(":", 1)
    return head.strip().lower() == "engine"


def _decrypt_event_color(label: str) -> str:
    normalized = label.lower()
    if "failed" in normalized:
        return Palette.red
    if "fallback" in normalized:
        return Palette.yellow
    return Palette.blue


def _print_elapsed_line(args: argparse.Namespace, started_at: float, colors: bool | None = None) -> None:
    elapsed = max(0.0, time.monotonic() - started_at)
    elapsed_text = format_time(elapsed) or "0s"
    _print_status_line("Elapsed:", elapsed_text, Palette.green, colors)
    _log_line(args, f"Elapsed: {elapsed_text}")


class _TrackStatusReporter:
    spinner_frames = "⣾⣽⣻⢿⡿⣟⣯⣷"
    spinner = spinner_frames[0]

    def __init__(
        self,
        index: int,
        stream,
        emit: Callable[[str], None],
        colors: bool | None = None,
        callback: Callable[[str], None] | None = None,
        force: bool = False,
    ):
        self.index = index
        self.stream = stream
        self.emit = emit
        self.colors = colors
        self.callback = callback
        self.path: Path | None = None
        self.last_line: str | None = None
        self.inline = callback is None and (force or sys.stdout.isatty())
        self.rendered = False

    def set_path(self, path: Path) -> None:
        self.path = path

    def update(self, *states: str) -> None:
        if self.path is None:
            return
        line = _format_track_status_line(self.index, self.stream, self.path, states, self.colors)
        if line == self.last_line:
            return
        self.last_line = line
        if self.callback:
            self.callback(line)
        elif self.inline:
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
            self.rendered = True
        else:
            self.emit(line)

    def finish(self) -> None:
        if self.inline and self.rendered:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.rendered = False

    def spinning(self, *states: str):
        return _TrackStatusSpinner(self, states)


class _TrackStatusSpinner:
    interval = 0.12

    def __init__(self, reporter: _TrackStatusReporter, states: Sequence[str]):
        self.reporter = reporter
        self.states = tuple(states)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        self._render(0)
        if self.reporter.callback or self.reporter.inline:
            context = contextvars.copy_context()
            self.thread = threading.Thread(
                target=context.run,
                args=(self._run,),
                daemon=True,
            )
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)

    def _run(self) -> None:
        frame_index = 1
        while not self.stop_event.wait(self.interval):
            self._render(frame_index)
            frame_index += 1

    def _render(self, frame_index: int) -> None:
        frame = self.reporter.spinner_frames[frame_index % len(self.reporter.spinner_frames)]
        self.reporter.update(*_spinner_states(self.states, frame))


class _TaskStatusLine:
    spinner_frames = "⣾⣽⣻⢿⡿⣟⣯⣷"
    spinner = spinner_frames[0]

    def __init__(
        self,
        output_path: Path,
        started_at: float,
        colors: bool | None = None,
        callback: Callable[[str], None] | None = None,
    ):
        self.output_path = output_path
        self.started_at = started_at
        self.colors = colors
        self.callback = callback
        self.enabled = callback is None and sys.stdout.isatty()
        self.rendered = False

    def update(self, status: str, *, done: bool = False) -> None:
        elapsed = format_time(max(0.0, time.monotonic() - self.started_at)) or "0s"
        status_text = _color_track_status_state(status, self.colors)
        width = _terminal_size((120, 24)).columns
        # The embedded TUI owns a separate elapsed-time field.  Keeping the
        # duration out of this replaceable mux/decrypt row leaves room for the
        # output path and avoids making every animation frame recompute it.
        line = _format_task_status_line(
            self.output_path,
            status_text,
            elapsed,
            self.colors,
            width,
            include_elapsed=self.callback is None,
        )
        if self.callback is not None:
            self.callback(line)
            return
        if self.enabled:
            sys.stdout.write("\r\033[K" + line)
            if done:
                sys.stdout.write("\n")
            sys.stdout.flush()
            self.rendered = not done
            return
        print(line)

    def spinning(self, status: str):
        return _TaskStatusSpinner(self, status)


def _format_task_status_line(
    output_path: Path,
    status_text: str,
    elapsed: str,
    colors: bool | None = None,
    width: int | None = None,
    *,
    include_elapsed: bool = True,
) -> str:
    safe_width = max(24, (width if width is not None else _terminal_size((120, 24)).columns) - 1)
    prefix = f"{paint('Task', Palette.green, colors)} : "
    suffix = f" | {status_text}"
    if include_elapsed:
        suffix += f" | Time Elapsed: {elapsed}"
    path_width = safe_width - _visible_text_len(prefix) - _visible_text_len(suffix)
    path_text = _ellipsize(str(output_path), max(1, path_width))
    return _ellipsize(f"{prefix}{path_text}{suffix}", safe_width)


class _TaskStatusSpinner:
    interval = 0.12

    def __init__(self, line: _TaskStatusLine, status: str):
        self.line = line
        self.status = status
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        self._render(0)
        # Embedded hosts do not own stdout, so ``enabled`` is false even though
        # their message callback is the live status transport.  The callback
        # still needs the same spinner cadence as the standalone terminal;
        # otherwise muxing is reduced to one static "Muxing" frame in the TUI.
        if self.line.enabled or self.line.callback:
            context = contextvars.copy_context()
            self.thread = threading.Thread(
                target=context.run,
                args=(self._run,),
                daemon=True,
            )
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)

    def _run(self) -> None:
        frame_index = 1
        while not self.stop_event.wait(self.interval):
            self._render(frame_index)
            frame_index += 1

    def _render(self, frame_index: int) -> None:
        frame = self.line.spinner_frames[frame_index % len(self.line.spinner_frames)]
        self.line.update(_spinner_text(self.status, frame))


def _spinner_states(states: Sequence[str], frame: str) -> tuple[str, ...]:
    if not states:
        return (frame,)
    rendered = tuple(_spinner_text(state, frame) for state in states)
    if any("{spinner}" in state for state in states):
        return rendered
    return (*rendered[:-1], f"{rendered[-1]} {frame}")


def _spinner_text(text: str, frame: str) -> str:
    return text.replace("{spinner}", frame)


def _format_track_status_line(index: int, stream, path: Path, states: Sequence[str], colors: bool | None = None) -> str:
    prefix = f"{index:>3}. {stream.display_prefix():<3} : "
    status = " | ".join(_color_track_status_state(state, colors) for state in states)
    tail = f" | {status}" if status else ""
    path_text = _short_status_path(path, max_len=78)
    label_color = Palette.cyan
    if stream.media_type == "audio":
        label_color = Palette.green
    elif _is_subtitle_stream(stream):
        label_color = Palette.magenta
    return f"{paint(prefix, label_color, colors)}{path_text}{tail}"


def _color_track_status_state(state: str, colors: bool | None = None) -> str:
    normalized = state.strip().lower()
    if not normalized:
        return state
    if "warning" in normalized:
        return paint(state, Palette.yellow, colors)
    if "error" in normalized or "failed" in normalized:
        return paint(state, Palette.red, colors)
    if any(word in normalized for word in ("decrypting", "converting", "transcoding", "repacking", "recording", "muxing")):
        return paint(state, Palette.cyan, colors)
    if "✓" in state or any(word in normalized for word in ("downloaded", "decrypted", "converted", "recorded", "repacked", "muxed")):
        return paint(state, Palette.green, colors)
    return paint(state, Palette.muted, colors)


def _short_status_path(path: Path, max_len: int = 78) -> str:
    text = str(path)
    if len(text) <= max_len:
        return text
    keep_left = max(12, (max_len - 1) // 2)
    keep_right = max(12, max_len - keep_left - 1)
    return f"{text[:keep_left]}…{text[-keep_right:]}"


def _stream_original_index(all_streams, stream, offset: int) -> int:
    try:
        return all_streams.index(stream) + 1
    except ValueError:
        return offset


def _wait_for_task_start(args: argparse.Namespace, colors: bool | None = None) -> None:
    value = getattr(args, "task_start_at", None)
    if not value:
        return
    start_ts = _parse_task_start_at(value)
    remaining = start_ts - time.time()
    if remaining <= 0:
        return
    target_label = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ts))
    message = f"waiting until {target_label} before starting task ({format_time(remaining)})."
    _print_labeled_text("Task start", message, Palette.blue, colors)
    _log_line(args, f"Task start: {message}")
    while True:
        _check_embedding_cancelled(args)
        remaining = start_ts - time.time()
        if remaining <= 0:
            break
        _runtime_sleep(min(1.0, remaining))


def _parse_task_start_at(value: str) -> float:
    text = str(value).strip()
    if not re.fullmatch(r"\d{14}", text):
        raise ValueError("--task-start-at must use yyyyMMddHHmmss, for example 20260504183000.")
    try:
        parsed = time.strptime(text, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError("--task-start-at must be a valid local time in yyyyMMddHHmmss format.") from exc
    return time.mktime(parsed)


@dataclass(slots=True)
class _DownloadedTrack:
    offset: int
    stream: object
    path: Path
    temp_dir: Path | None
    cleanup_paths: list[Path]


def _embedding_cancel_callback(args) -> Callable[[], bool] | None:
    hooks = getattr(args, "embedding_hooks", None)
    return getattr(hooks, "cancel_requested", None)


def _embedding_pause_callback(args) -> Callable[[], bool] | None:
    hooks = getattr(args, "embedding_hooks", None)
    return getattr(hooks, "pause_requested", None)


def _runtime_sleep(seconds: float) -> None:
    """Use the embedded runtime's interruptible wait when one is active."""
    runtime = current_download_runtime()
    if runtime is None:
        time.sleep(max(0.0, float(seconds)))
    else:
        if runtime.wait(seconds):
            runtime.checkpoint()


def _wait_if_paused(args) -> None:
    """Hold workers in place without cancelling the current delivery."""
    paused = _embedding_pause_callback(args)
    if paused is None:
        return
    while paused():
        _check_embedding_cancelled(args)
        _runtime_sleep(0.1)


def _checkpoint_embedding(args) -> None:
    """Cancel immediately, or wait here while the host has paused transfer."""
    _check_embedding_cancelled(args)
    _wait_if_paused(args)


def _embedding_console_progress(args) -> bool:
    hooks = getattr(args, "embedding_hooks", None)
    return bool(getattr(hooks, "console_progress", True))


def _check_embedding_cancelled(args) -> None:
    callback = _embedding_cancel_callback(args)
    if callback is not None and callback():
        raise DownloadCancelled("download cancelled")


def _embedding_vod_progress(
    args,
    downstream: Callable[[ProgressUpdate], None] | None,
) -> Callable[[ProgressUpdate], None] | None:
    hooks = getattr(args, "embedding_hooks", None)
    report = getattr(hooks, "progress", None)
    if (
        report is None
        and _embedding_cancel_callback(args) is None
        and _embedding_pause_callback(args) is None
    ):
        return downstream

    def progress(update: ProgressUpdate) -> None:
        if not update.done:
            _checkpoint_embedding(args)
        elapsed = max(0.001, float(update.elapsed_seconds or 0.0))
        speed = max(0.0, float(update.downloaded_bytes or 0) / elapsed)
        eta = None
        if update.total_bytes is not None and speed > 0:
            eta = max(0.0, float(update.total_bytes - update.downloaded_bytes) / speed)
        if report is not None:
            report(
                DownloadProgress(
                    stream=update.stream,
                    completed_segments=max(0, int(update.completed_segments or 0)),
                    total_segments=max(0, int(update.total_segments or 0)),
                    downloaded_bytes=max(0, int(update.downloaded_bytes or 0)),
                    total_bytes=(
                        max(0, int(update.total_bytes))
                        if update.total_bytes is not None
                        else None
                    ),
                    elapsed_seconds=elapsed,
                    speed_bytes_per_second=speed,
                    eta_seconds=eta,
                    status="Done" if update.done else "Downloading",
                    done=bool(update.done),
                )
            )
        if downstream is not None:
            downstream(update)
        if not update.done:
            _checkpoint_embedding(args)

    return progress


def _embedding_live_progress(
    args,
    downstream: Callable[[LiveProgressUpdate], None] | None,
) -> Callable[[LiveProgressUpdate], None] | None:
    hooks = getattr(args, "embedding_hooks", None)
    report = getattr(hooks, "progress", None)
    if (
        report is None
        and _embedding_cancel_callback(args) is None
        and _embedding_pause_callback(args) is None
    ):
        return downstream
    started = time.monotonic()
    samples: dict[int, tuple[int, float, float]] = {}

    def progress(update: LiveProgressUpdate) -> None:
        terminal = str(update.status or "").strip().lower() in {
            "done",
            "error",
            "failed",
            "stopped",
            "cancelled",
            "canceled",
        }
        if not update.done and not terminal:
            _checkpoint_embedding(args)
        now = time.monotonic()
        downloaded = max(0, int(update.downloaded_bytes or 0))
        previous = samples.get(id(update.stream))
        speed = max(0.0, float(update.speed_bytes_per_second or 0.0))
        if previous is not None:
            previous_bytes, previous_time, previous_speed = previous
            delta_bytes = max(0, downloaded - previous_bytes)
            delta_time = max(0.001, now - previous_time)
            if delta_bytes > 0:
                instant = delta_bytes / delta_time
                speed = instant if previous_speed <= 0 else previous_speed * 0.45 + instant * 0.55
            elif previous_speed > 0 and now - previous_time < 2.0 and not update.done:
                speed = previous_speed
        samples[id(update.stream)] = (downloaded, now, speed)
        elapsed = max(0.001, now - started)
        recorded = max(0.0, float(update.recorded_seconds or 0.0))
        duration = (
            max(0.0, float(update.limit_seconds))
            if update.limit_seconds is not None
            else (
                max(recorded, float(update.available_seconds or 0.0))
                if update.available_seconds is not None
                else None
            )
        )
        total_segments = (
            max(int(update.segments_count or 0), int(update.available_segments or 0))
            if update.available_segments is not None
            else None
        )
        if update.limit_seconds is not None and update.segments_count and recorded:
            average = recorded / update.segments_count
            total_segments = max(
                int(update.segments_count),
                int(math.ceil(update.limit_seconds / average)),
            )
        eta = None
        if duration is not None and recorded > 0:
            recording_rate = recorded / elapsed
            if recording_rate > 0:
                eta = max(0.0, (duration - recorded) / recording_rate)
        if report is not None:
            report(
                DownloadProgress(
                    stream=update.stream,
                    completed_segments=max(0, int(update.segments_count or 0)),
                    total_segments=total_segments,
                    downloaded_bytes=downloaded,
                    total_bytes=None,
                    elapsed_seconds=elapsed,
                    speed_bytes_per_second=speed,
                    eta_seconds=eta,
                    live=True,
                    recorded_seconds=recorded,
                    duration_seconds=duration,
                    status="Done" if update.done else str(update.status or "Waiting"),
                    done=bool(update.done),
                )
            )
        if downstream is not None:
            downstream(update)
        if not update.done and not terminal:
            _checkpoint_embedding(args)

    return progress


def _emit_download_artifacts(
    args,
    paths: Sequence[Path],
    tracks: Sequence[_DownloadedTrack],
    metadata_paths: Sequence[Path] = (),
) -> None:
    hooks = getattr(args, "embedding_hooks", None)
    emit = getattr(hooks, "artifact_created", None)
    if emit is None:
        return
    tracks_by_path = {Path(track.path): track for track in tracks}
    emitted: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if path in emitted:
            continue
        emitted.add(path)
        track = tracks_by_path.get(path)
        kind = (
            "subtitle"
            if track is not None and _is_subtitle_stream(track.stream)
            else "media"
        )
        emit(
            DownloadArtifact(
                path=path,
                kind=kind,
                stream=track.stream if track is not None else None,
            )
        )
    for raw_path in metadata_paths:
        path = Path(raw_path)
        if path in emitted:
            continue
        emitted.add(path)
        emit(DownloadArtifact(path=path, kind="metadata"))


@dataclass(frozen=True, slots=True)
class _CompletedTrackCache:
    path: Path
    cleanup_paths: list[Path]


@dataclass(slots=True)
class _ChecklistResult:
    selected: list[int]
    cursor: int
    offset: int
    visible_rows: int


_COMPLETED_TRACK_CACHE_VERSION = 1


def _completed_track_cache_identity(stream, filename: str | None, args: argparse.Namespace) -> dict:
    return {
        "stream_key": _stream_resume_key(stream),
        "filename": filename,
        "save_pattern": getattr(args, "save_pattern", None),
        "no_decrypt": bool(getattr(args, "no_decrypt", False)),
        "repack": bool(getattr(args, "repack", False)),
        "audio_format": (getattr(args, "audio_format", None) or "").lower() or None,
        "audio_metadata": audio_metadata_signature(stream) if getattr(args, "audio_format", None) else None,
        "sub_format": (getattr(args, "sub_format", "srt") or "srt").lower(),
        "vgc": bool(getattr(args, "vgc", False)),
        "vgc_keep_opaque": bool(getattr(args, "vgc_keep_opaque", False)),
    }


def _completed_track_cache_key(stream, filename: str | None, args: argparse.Namespace) -> str:
    payload = _completed_track_cache_identity(stream, filename, args)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def _completed_track_cache_path(stream, filename: str | None, args: argparse.Namespace) -> Path:
    key = _completed_track_cache_key(stream, filename, args)
    return _task_temp_subdir(args, "completed") / f"{key}.json"


def _load_completed_track_cache(stream, filename: str | None, args: argparse.Namespace) -> _CompletedTrackCache | None:
    if getattr(args, "no_resume", False):
        return None
    marker_path = _completed_track_cache_path(stream, filename, args)
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != _COMPLETED_TRACK_CACHE_VERSION:
        return None
    identity = _completed_track_cache_identity(stream, filename, args)
    if data.get("identity") != identity:
        return None
    raw_path = data.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    if int(data.get("size", -1)) != int(stat.st_size):
        return None
    if int(data.get("mtime_ns", -1)) != int(stat.st_mtime_ns):
        return None
    cleanup_paths: list[Path] = []
    raw_cleanup = data.get("cleanup_paths")
    if isinstance(raw_cleanup, list):
        for value in raw_cleanup:
            if isinstance(value, str) and value:
                cleanup_paths.append(Path(value).expanduser())
    return _CompletedTrackCache(path=path, cleanup_paths=cleanup_paths)


def _write_completed_track_cache(
    stream,
    filename: str | None,
    args: argparse.Namespace,
    path: Path,
    cleanup_paths: Sequence[Path],
) -> None:
    if getattr(args, "no_resume", False):
        return
    output_path = Path(path).expanduser()
    try:
        stat = output_path.stat()
    except OSError:
        return
    if not output_path.is_file():
        return
    marker_path = _completed_track_cache_path(stream, filename, args)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _COMPLETED_TRACK_CACHE_VERSION,
        "identity": _completed_track_cache_identity(stream, filename, args),
        "path": str(output_path),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "cleanup_paths": [str(Path(item).expanduser()) for item in cleanup_paths],
        "written_at": time.time(),
    }
    temp_path = marker_path.with_suffix(".json.tmp")
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp_path, marker_path)
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class _ProgressTailWidths:
    segment: int
    size: int
    speed: int


def _download_selected_stream(
    stream,
    offset: int,
    total: int,
    all_streams,
    args,
    headers,
    keys,
    hls_crypto,
    max_speed,
    output_dir: Path,
    default_save_base: str | None,
    show_progress: bool,
    progress_callback: Callable[[ProgressUpdate], None] | None = None,
    status_printer: Callable[[str], None] | None = None,
    status_callback: Callable[[str], None] | None = None,
    workers: int | None = None,
) -> _DownloadedTrack:
    _checkpoint_embedding(args)
    hls_crypto = _hls_crypto_for_stream(stream, hls_crypto)
    colors = _colors(args)
    name = _track_save_name(args.save_name or default_save_base, args.save_pattern, stream, offset, total)
    name = _audio_source_download_name(name, stream, args)
    progress_display = progress_callback or (
        _DownloadProgress(
            stream,
            colors=colors,
            force=getattr(args, "force_ansi_console", False),
        )
        if show_progress and _embedding_console_progress(args)
        else None
    )
    progress = _embedding_vod_progress(args, progress_display)
    emit = status_printer or _embedding_message_emitter(args) or print
    status_callback = status_callback or _embedding_message_emitter(
        args,
        transient=True,
    )
    original_index = _stream_original_index(all_streams, stream, offset)
    status = _TrackStatusReporter(original_index, stream, emit, colors, status_callback, force=getattr(args, "force_ansi_console", False))
    cached = _load_completed_track_cache(stream, name, args)
    if cached is not None:
        cached_path = cached.path
        cleanup_paths = list(cached.cleanup_paths)
        deezer = _deezer_transport_context(stream)
        if deezer and not args.no_decrypt and not deezer.get("decrypted"):
            with status.spinning("Cached ✓", "Decrypting Deezer {spinner}"):
                cached_path = decrypt_deezer_file(
                    cached_path,
                    str(deezer["track_id"]),
                    deezer["cipher"],
                    extension=str(deezer.get("extension") or ""),
                )
            deezer["decrypted"] = True
            status.update("Cached ✓", "Decrypted ✓")
        audio_result = postprocess_audio_vivid(
            cached_path,
            stream,
            decoder=getattr(args, "audio_vivid_decoder", None),
            decoder_args=getattr(args, "audio_vivid_decoder_args", None),
        )
        if audio_result.warning:
            emit(f"{paint('Audio:', Palette.yellow, colors)} {audio_result.warning}")
            _log_line(args, f"Audio: {audio_result.warning}")
        elif audio_result.decoded_path is not None:
            cached_path = audio_result.path
            if cached_path not in cleanup_paths:
                cleanup_paths.append(cached_path)
        status.set_path(cached_path)
        status.update("Downloaded ✓", "Cached ✓")
        status.finish()
        _log_line(args, f"Cached: {cached_path}")
        return _DownloadedTrack(
            offset=offset,
            stream=stream,
            path=cached_path,
            temp_dir=None,
            cleanup_paths=cleanup_paths,
        )
    if stream.encrypted and not args.no_decrypt:
        _ensure_hls_segment_crypto_ready([stream], hls_crypto)
        if _stream_uses_bbts(stream):
            _bbts_key_hex(keys, hls_crypto, stream)
    if progress_display is None or isinstance(progress_display, _DownloadProgress) and not progress_display.enabled:
        label = "Downloading:" if show_progress else "Queued:"
        emit(f"\n{paint(label, Palette.blue, colors)} {_format_numbered_line(original_index - 1, stream, checked=True, colors=colors)}")
    try:
        effective_workers = workers or args.workers
        if getattr(args, "vgc", False) and _is_subtitle_stream(stream):
            effective_workers = 1
        result = download_stream(
            stream,
            output_dir=output_dir,
            filename=name,
            headers=headers,
            workers=effective_workers,
            retries=args.retries,
            keep_temp=args.keep_temp or _stream_needs_fragment_parts(stream) or (_stream_uses_webm_container(stream) and stream.encrypted and not args.no_decrypt),
            downloader=args.downloader,
            progress=progress,
            temp_dir=_task_temp_subdir(args, "vod"),
            resume=not args.no_resume,
            max_speed=max_speed,
            hls_crypto=hls_crypto,
            request_timeout=max(1, args.http_request_timeout),
            check_segments_count=getattr(args, "check_segments_count", True),
            assemble_output=not _should_skip_assembled_download_output(stream, args, hls_crypto),
            gate=lambda: _checkpoint_embedding(args),
        )
    except (DownloadCancelled, KeyboardInterrupt):
        if isinstance(progress_display, _DownloadProgress):
            progress_display.cancel()
        raise
    except Exception:
        if isinstance(progress_display, _DownloadProgress):
            progress_display.close()
        raise
    else:
        if isinstance(progress_display, _DownloadProgress):
            progress_display.finish()
    current_path = result.path
    cleanup_paths = [current_path]
    deezer = _deezer_transport_context(stream)
    if deezer and not args.no_decrypt and not deezer.get("decrypted"):
        with status.spinning("Downloaded ✓", "Decrypting Deezer {spinner}"):
            current_path = decrypt_deezer_file(
                current_path,
                str(deezer["track_id"]),
                deezer["cipher"],
                extension=str(deezer.get("extension") or ""),
            )
        deezer["decrypted"] = True
        status.set_path(current_path)
        status.update("Downloaded ✓", "Deezer clear ✓")
        _log_line(args, f"Deezer transport decrypted: {current_path}")
    if _is_sabr_stream(stream):
        _apply_sniffed_sabr_container_from_path(stream, current_path)
    status.set_path(current_path)
    status.update("Downloaded ✓")
    _log_line(args, f"Downloaded: {current_path}")
    result_temp_dir = result.temp_dir
    tencentvideo_cenc = False
    if not args.no_decrypt and result.parts:
        with status.spinning("Downloaded ✓", "Decrypting {spinner}"):
            compatible_path = decrypt_tencentvideo_cenc_parts(
                stream,
                result.parts,
                stream.segments,
                keys,
                current_path.with_suffix(f".dec{current_path.suffix}"),
                temp_dir=_task_temp_subdir(args, "postprocess"),
            )
        if compatible_path is not None:
            current_path = compatible_path
            tencentvideo_cenc = True
            if current_path not in cleanup_paths:
                cleanup_paths.append(current_path)
            clear_count = int(stream.extra.get("tencentvideo_cenc_clear_fragments") or 0)
            encrypted_count = int(stream.extra.get("tencentvideo_cenc_encrypted_fragments") or 0)
            emit(
                f"{paint('Tencent:', Palette.yellow, colors)} "
                f"TV CENC sample entries handled: {clear_count} clear, {encrypted_count} decrypted."
            )
            status.set_path(current_path)
            status.update("Downloaded ✓", "Decrypted ✓")
            _log_line(
                args,
                f"Tencent Video CENC compatibility: {clear_count} clear, {encrypted_count} decrypted",
            )
    if _should_finalize_clear_hls_sections(stream, result, hls_crypto):
        with status.spinning("Downloaded ✓", "Finalizing {spinner}"):
            current_path = _finalize_clear_hls_sections(stream, result, current_path)
        status.update("Downloaded ✓", "Finalized ✓")
        _log_line(args, f"Finalized: {current_path}")
    if not _download_parts_needed_for_postprocess(stream, result, hls_crypto, args):
        result_temp_dir = _cleanup_track_temp_dir(result_temp_dir, args, colors)

    if _stream_uses_bbts(stream) and not args.no_decrypt:
        previous_path = current_path
        with status.spinning("Downloaded ✓", "Decrypting {spinner}"):
            current_path = decrypt_bbts_file(
                current_path,
                current_path.with_suffix(".dec.ts"),
                _bbts_key_hex(keys, hls_crypto, stream),
            )
        _cleanup_replaced_intermediate(previous_path, current_path, args, colors)
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.update("Downloaded ✓", "Decrypted ✓")
        _log_line(args, f"Decrypted: {current_path}")
    elif _apple_music_foothill_context(args) and not args.no_decrypt:
        previous_path = current_path
        decrypt_context = _decrypt_event_context(original_index, stream)
        decrypt_events, decrypt_event = _new_decrypt_event_recorder(args, decrypt_context)
        foothill = _apple_music_foothill_context(args)
        try:
            with status.spinning("Downloaded ✓", "Decrypting {spinner}"):
                current_path = decrypt_apple_music_fmp4_parts(
                    result.parts or [],
                    stream.segments,
                    helper_path=str(foothill["helper"]),
                    context_keys=foothill["contexts"],
                    default_context_key=foothill["default_context"],
                    stream_type="audio" if foothill["track_type"] == 0 else "video",
                    output_path=current_path.with_suffix(f".dec{current_path.suffix}"),
                    event_callback=decrypt_event,
                )
        except Exception:
            _flush_track_decrypt_events(status, decrypt_events, emit, colors, decrypt_context)
            raise
        _flush_track_decrypt_events(status, decrypt_events, emit, colors, decrypt_context)
        _cleanup_replaced_intermediate(previous_path, current_path, args, colors)
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.update("Downloaded ✓", "Decrypted ✓")
        _log_line(args, f"Decrypted: {current_path}")
    elif _stream_uses_webm_container(stream) and _stream_needs_external_decryption(stream, hls_crypto) and not args.no_decrypt:
        previous_path = current_path
        decrypt_context = _decrypt_event_context(original_index, stream)
        decrypt_events, decrypt_event = _new_decrypt_event_recorder(args, decrypt_context)
        try:
            with status.spinning("Downloaded ✓", "Decrypting {spinner}"):
                current_path = _decrypt_webm_output(
                    current_path,
                    stream,
                    result,
                    keys,
                    args.decrypter,
                    current_path.with_suffix(".dec.webm"),
                    event_callback=decrypt_event,
                )
        except Exception:
            _flush_track_decrypt_events(status, decrypt_events, emit, colors, decrypt_context)
            raise
        _flush_track_decrypt_events(status, decrypt_events, emit, colors, decrypt_context)
        _cleanup_replaced_intermediate(previous_path, current_path, args, colors)
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.update("Downloaded ✓", "Decrypted ✓")
        _log_line(args, f"Decrypted: {current_path}")
    elif _stream_needs_external_decryption(stream, hls_crypto) and not args.no_decrypt and not tencentvideo_cenc:
        previous_path = current_path
        decrypt_context = _decrypt_event_context(original_index, stream)
        decrypt_events, decrypt_event = _new_decrypt_event_recorder(args, decrypt_context)
        try:
            with status.spinning("Downloaded ✓", "Decrypting {spinner}"):
                stream_type = "audio" if stream.media_type == "audio" else "video"
                decrypter = _decrypter_for_stream(args.decrypter, stream)
                if _should_decrypt_fragmented_parts(stream, result):
                    current_path = decrypt_fragmented_mp4_parts(
                        result.parts or [],
                        stream.segments,
                        keys=keys,
                        stream_type=stream_type,
                        output_path=current_path.with_suffix(f".dec{current_path.suffix}"),
                        expected_kids=_stream_key_ids(stream),
                        temp_dir=_task_temp_subdir(args, "postprocess"),
                        event_callback=decrypt_event,
                        restamp_timestamps=_stream_uses_json_dvr_sequence_fragments(stream),
                    )
                elif result.sections:
                    if _stream_sections_need_whole_file_decryption(stream):
                        current_path = decrypt_file(
                            current_path,
                            keys=keys,
                            decrypter=decrypter,
                            stream_type=stream_type,
                            output_path=current_path.with_suffix(f".dec{current_path.suffix}"),
                            expected_kids=_stream_key_ids(stream),
                            event_callback=decrypt_event,
                        )
                    else:
                        restamp_sections = _stream_sections_need_timestamp_restamp(stream)
                        section_fragment_durations = _stream_section_fragment_durations(stream) if restamp_sections else None
                        current_path = decrypt_sections(
                            result.sections,
                            keys=keys,
                            decrypter=decrypter,
                            stream_type=stream_type,
                            output_path=current_path.with_suffix(f".dec{current_path.suffix}"),
                            expected_kids=_stream_key_ids(stream),
                            restamp_timestamps=restamp_sections,
                            section_durations=[sum(durations) for durations in section_fragment_durations] if section_fragment_durations else None,
                            section_fragment_durations=section_fragment_durations,
                            normalize_large_composition_offsets=restamp_sections and stream.media_type == "video",
                            event_callback=decrypt_event,
                        )
                else:
                    current_path = decrypt_file(
                        current_path,
                        keys=keys,
                        decrypter=decrypter,
                        stream_type=stream_type,
                        expected_kids=_stream_key_ids(stream),
                        event_callback=decrypt_event,
                    )
                _normalize_hls_video_sample_timing(current_path, stream)
        except Exception:
            _flush_track_decrypt_events(status, decrypt_events, emit, colors, decrypt_context)
            raise
        _flush_track_decrypt_events(status, decrypt_events, emit, colors, decrypt_context)
        _cleanup_replaced_intermediate(previous_path, current_path, args, colors)
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.update("Downloaded ✓", "Decrypted ✓")
        _log_line(args, f"Decrypted: {current_path}")
    if is_vgc_stream(stream):
        try:
            with status.spinning("Downloaded ✓", "Checking VGC {spinner}"):
                current_path = finalize_vgc_track(current_path, stream, allow_opaque=getattr(args, "vgc_keep_opaque", False))
        except VgcError as exc:
            raise RuntimeError(str(exc)) from exc
        if getattr(args, "vgc_keep_opaque", False):
            status.update("Downloaded ✓", "VGC raw kept")
            _log_line(args, f"VGC raw kept: {current_path}")
        else:
            status.update("Downloaded ✓", "VGC clear ✓")
            _log_line(args, f"VGC clear: {current_path}")
    if args.repack:
        previous_path = current_path
        with status.spinning("Downloaded ✓", "Decrypted ✓", "Repacking {spinner}"):
            current_path = repackage_ffmpeg(current_path)
        _cleanup_replaced_intermediate(previous_path, current_path, args, colors)
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.update("Downloaded ✓", "Decrypted ✓", "Repacked ✓")
        _log_line(args, f"Repacked: {current_path}")
    audio_result = postprocess_audio_vivid(
        current_path,
        stream,
        decoder=getattr(args, "audio_vivid_decoder", None),
        decoder_args=getattr(args, "audio_vivid_decoder_args", None),
    )
    if audio_result.warning:
        emit(f"{paint('Audio:', Palette.yellow, colors)} {audio_result.warning}")
        _log_line(args, f"Audio: {audio_result.warning}")
    elif audio_result.decoded_path is not None:
        current_path = audio_result.path
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.set_path(current_path)
        status.update("Downloaded ✓", "Audio Vivid WAV ✓")
        _log_line(args, f"Audio Vivid WAV: {audio_result.decoded_path}")
    audio_format = _audio_format_for_stream(stream, args)
    if audio_format:
        audio_label = audio_format.upper()
        previous_path = current_path
        with status.spinning("Downloaded ✓", f"Finalizing {audio_label} {{spinner}}"):
            current_path = _transcode_audio_output(current_path, result.path, stream, args, headers)
        _cleanup_replaced_intermediate(previous_path, current_path, args, colors)
        if current_path not in cleanup_paths:
            cleanup_paths.append(current_path)
        status.set_path(current_path)
        status.update("Downloaded ✓", f"{audio_label} ✓")
        _log_line(args, f"{audio_label}: {current_path}")
    current_path, subtitle_cleanup = _convert_subtitle_output(current_path, stream, args, colors, emit, status)
    for path in subtitle_cleanup:
        if path not in cleanup_paths:
            cleanup_paths.append(path)
    _write_completed_track_cache(stream, name, args, current_path, cleanup_paths)
    result_temp_dir = _cleanup_track_temp_dir(result_temp_dir, args, colors)
    status.finish()
    return _DownloadedTrack(offset=offset, stream=stream, path=current_path, temp_dir=result_temp_dir, cleanup_paths=cleanup_paths)


def _should_decrypt_fragmented_parts(stream, result) -> bool:
    if not _stream_needs_fragment_parts(stream):
        return False
    if stream.manifest_type == "hls" and result.sections:
        return False
    if not result.parts:
        return False
    if _fragmented_stream_can_decrypt_as_whole_file(stream):
        return False
    return True


def _should_skip_assembled_download_output(stream, args, hls_crypto) -> bool:
    if getattr(args, "keep_temp", False) or not getattr(args, "del_after_done", True):
        return False
    if getattr(args, "no_decrypt", False):
        return False
    if not _stream_needs_external_decryption(stream, hls_crypto):
        return False
    if _stream_uses_webm_container(stream):
        return False
    if not _stream_can_decrypt_from_parts_without_assembled_output(stream):
        return False
    return True


def _stream_can_decrypt_from_parts_without_assembled_output(stream) -> bool:
    if not _stream_needs_fragment_parts(stream):
        return False
    if _fragmented_stream_can_decrypt_as_whole_file(stream):
        return False
    if stream.manifest_type == "hls" and _stream_has_multiple_init_sections(stream):
        return False
    return True


def _stream_has_multiple_init_sections(stream) -> bool:
    return sum(1 for segment in getattr(stream, "segments", []) or [] if getattr(segment, "index", None) == -1) > 1


def _fragmented_stream_can_decrypt_as_whole_file(stream) -> bool:
    if stream.manifest_type != "dash":
        return False
    if getattr(stream, "is_live", False):
        return False
    if _stream_uses_webm_container(stream):
        return False
    segments = list(getattr(stream, "segments", []) or [])
    if sum(1 for segment in segments if segment.index == -1) != 1:
        return False
    media_segments = [segment for segment in segments if segment.index != -1]
    if not media_segments:
        return False

    declared_kids: list[str] = []
    extra = getattr(stream, "extra", {}) or {}
    for value in extra.get("key_ids") or []:
        kid = _normalize_kid_text(value)
        if kid and kid not in declared_kids:
            declared_kids.append(kid)
    kid = _normalize_kid_text(extra.get("key_id"))
    if kid and kid not in declared_kids:
        declared_kids.append(kid)

    segment_kids: list[str] = []
    for segment in media_segments:
        kid = _normalize_kid_text(getattr(segment, "key_id", None))
        if kid and kid not in segment_kids:
            segment_kids.append(kid)

    all_kids = list(dict.fromkeys([*declared_kids, *segment_kids]))
    if len(all_kids) != 1:
        return False
    return all(kid == all_kids[0] for kid in segment_kids)


def _stream_uses_json_fragmented_mp4_sequence(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "json":
        return False
    if not getattr(stream, "encrypted", False):
        return False
    if _stream_uses_webm_container(stream):
        return False
    scheme = (getattr(stream, "encryption_scheme", None) or "").upper().replace("-", "_")
    if scheme not in {"ENC", "CENC", "CBCS", "SAMPLE_AES"}:
        return False
    segments = list(getattr(stream, "segments", []) or [])
    if not segments or not any(getattr(segment, "index", None) == -1 for segment in segments):
        return False
    media_segments = [segment for segment in segments if getattr(segment, "index", None) != -1]
    if not media_segments:
        return False
    media_urls = [getattr(segment, "url", "") or "" for segment in media_segments]
    if len({url for url in media_urls if url}) == 1 and all(getattr(segment, "byte_range", None) for segment in media_segments):
        return False
    extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
    candidates = [getattr(segment, "url", "") or "" for segment in segments[:8]]
    extra = getattr(stream, "extra", None)
    raw = extra.get("raw") if isinstance(extra, dict) else None
    if isinstance(raw, dict):
        candidates.extend(str(raw.get(key) or "") for key in ("codec", "codecs", "mime_type", "mimeType"))
    text = " ".join(candidates).lower()
    looks_like_fmp4 = extension in _FRAGMENTED_MP4_EXTS or any(f".{suffix}" in text for suffix in _FRAGMENTED_MP4_EXTS) or "mp4" in text or "dash" in text
    if not looks_like_fmp4:
        return False
    return _json_fragmented_mp4_sequence_prefers_part_decryption(stream, media_segments)


def _json_fragmented_mp4_sequence_prefers_part_decryption(stream, media_segments: list[SegmentInfo]) -> bool:
    if getattr(stream, "is_live", False):
        return True
    if len(media_segments) >= _JSON_FMP4_PART_DECRYPT_MIN_SEGMENTS:
        return True
    try:
        estimated_size = stream.estimated_size_bytes
    except Exception:
        estimated_size = getattr(stream, "size_bytes", None)
    if estimated_size is not None and estimated_size >= _JSON_FMP4_PART_DECRYPT_MIN_BYTES:
        return True
    segment_kids = {
        kid
        for kid in (_normalize_kid_text(getattr(segment, "key_id", None)) for segment in media_segments)
        if kid
    }
    return len(segment_kids) > 1


def _should_decrypt_webm_parts(stream, result) -> bool:
    if not result.parts:
        return False
    return not _webm_parts_are_single_continuous_file(stream)


def _decrypt_webm_output(
    current_path: Path,
    stream,
    result,
    keys: list[RawKey],
    decrypter: str,
    output_path: Path,
    event_callback: Callable[[str], None] | None = None,
) -> Path:
    expected_kids = _stream_key_ids(stream)
    try:
        if event_callback:
            event_callback("engine: internal WebM/EBML")
        webm_key = select_webm_key(keys, webm_key_ids(current_path) or expected_kids)
        if _should_decrypt_webm_parts(stream, result):
            return decrypt_webm_parts(result.parts, webm_key, output_path)
        return decrypt_webm_file(current_path, webm_key, output_path)
    except (RuntimeError, ValueError) as exc:
        if event_callback:
            event_callback(f"failed: internal WebM/EBML: {_short_error(exc)}")
        raise RuntimeError(f"WebM internal decryption failed: {_short_error(exc)}") from exc


def _download_parts_needed_for_postprocess(stream, result, hls_crypto, args) -> bool:
    if getattr(args, "no_decrypt", False):
        return False
    if _stream_uses_webm_container(stream) and _stream_needs_external_decryption(stream, hls_crypto):
        return _should_decrypt_webm_parts(stream, result)
    if not _stream_needs_external_decryption(stream, hls_crypto):
        return False
    if _should_decrypt_fragmented_parts(stream, result):
        return True
    if result.sections and not _stream_sections_need_whole_file_decryption(stream):
        return True
    return False


def _webm_parts_are_single_continuous_file(stream) -> bool:
    segments = list(getattr(stream, "segments", []) or [])
    if len(segments) == 1 and getattr(segments[0], "url", None):
        return True
    urls = {getattr(segment, "url", None) for segment in segments if getattr(segment, "url", None)}
    return len(urls) == 1 and bool(urls) and all(getattr(segment, "byte_range", None) for segment in segments)


def _stream_needs_fragment_parts(stream) -> bool:
    if not stream.encrypted:
        return False
    if _stream_uses_webm_container(stream):
        return False
    if _stream_uses_json_fragmented_mp4_sequence(stream):
        return True
    if _stream_uses_json_dvr_sequence_fragments(stream):
        return True
    if should_treat_live_stream_as_fragmented_mp4(stream):
        return bool(stream.segments and any(segment.index == -1 for segment in stream.segments))
    if stream.manifest_type not in {"hls", "dash", "ism"}:
        return False
    scheme = (stream.encryption_scheme or "").upper()
    if scheme not in {"CBCS", "CENC", "SAMPLE-AES", "SAMPLE_AES"}:
        return False
    return bool(stream.segments and any(segment.index == -1 for segment in stream.segments))


def _stream_uses_json_dvr_sequence_fragments(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "json":
        return False
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    if not extra.get("json_dvr_sequence"):
        return False
    scheme = (getattr(stream, "encryption_scheme", None) or "").upper().replace("-", "_")
    if scheme not in {"ENC", "CENC", "CBCS", "SAMPLE_AES"}:
        return False
    if any(getattr(segment, "index", None) == -1 for segment in getattr(stream, "segments", []) or []):
        return True
    raw = extra.get("raw")
    if not isinstance(raw, dict):
        return False
    init_range = raw.get("init_range") or raw.get("initRange") or raw.get("initialization_range") or raw.get("initializationRange")
    return _parse_json_byte_range(init_range) is not None


def _stream_sections_need_timestamp_restamp(stream) -> bool:
    if stream.manifest_type != "hls":
        return False
    if stream.media_type not in {"audio", "video"}:
        return False
    if _stream_uses_webm_container(stream):
        return False
    return bool(stream.segments and sum(1 for segment in stream.segments if segment.index == -1) > 1)


def _stream_sections_need_whole_file_decryption(stream) -> bool:
    # DTS-like fMP4 streams are handled as one continuous file so internal
    # decryption sees the complete init/media layout.
    return _stream_uses_dts_audio(stream)


def _should_finalize_clear_hls_sections(stream, result, hls_crypto) -> bool:
    if getattr(stream, "manifest_type", None) != "hls":
        return False
    if getattr(stream, "encrypted", False):
        return False
    if _stream_needs_external_decryption(stream, hls_crypto):
        return False
    if not result.sections or len(result.sections) <= 1:
        return False
    if not _stream_sections_need_timestamp_restamp(stream):
        return False
    return _stream_uses_fragmented_mp4_sections(stream)


def _stream_uses_fragmented_mp4_sections(stream) -> bool:
    extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
    if extension in _FRAGMENTED_MP4_EXTS:
        return True
    for segment in getattr(stream, "segments", []) or []:
        url = getattr(segment, "url", None)
        if not url:
            continue
        suffix = Path(urlparse(str(url)).path).suffix.lower().lstrip(".")
        if suffix in _FRAGMENTED_MP4_EXTS:
            return True
    return False


def _finalize_clear_hls_sections(stream, result, current_path: Path) -> Path:
    section_fragment_durations = _stream_section_fragment_durations(stream)
    finalized_sections = restamp_fragmented_mp4_sequence(
        result.sections or [],
        start_decode_time=0,
        section_durations=[sum(durations) for durations in section_fragment_durations] if section_fragment_durations else None,
        section_fragment_durations=section_fragment_durations,
        normalize_large_composition_offsets=getattr(stream, "media_type", None) == "video",
    )
    temp_output = unique_path(current_path.with_name(f"{current_path.stem}.final{current_path.suffix}"))
    try:
        concat_media_files(finalized_sections, temp_output)
        temp_output.replace(current_path)
    finally:
        try:
            temp_output.unlink(missing_ok=True)
        except OSError:
            pass
    return current_path


def _normalize_hls_video_sample_timing(path: Path, stream) -> None:
    if getattr(stream, "manifest_type", None) != "hls":
        return
    if getattr(stream, "media_type", None) != "video":
        return
    if _stream_uses_webm_container(stream):
        return
    _mp4_normalize_large_sample_durations(path)


def _stream_section_durations(stream) -> list[float]:
    return [sum(durations) for durations in _stream_section_fragment_durations(stream)]


def _stream_section_fragment_durations(stream) -> list[list[float]]:
    sections: list[list[float]] = []
    current_parts: list[float] = []
    seen_section = False
    for segment in getattr(stream, "segments", []) or []:
        if getattr(segment, "index", None) == -1:
            if seen_section:
                sections.append(current_parts)
            seen_section = True
            current_parts = []
            continue
        if seen_section:
            duration = float(getattr(segment, "duration", None) or 0)
            current_parts.append(duration)
    if seen_section:
        sections.append(current_parts)
    return sections


def _stream_uses_webm_container(stream) -> bool:
    extension = (getattr(stream, "extension", None) or "").lower()
    if extension:
        return extension == "webm"
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    mime_type = ""
    if isinstance(raw, dict):
        mime_type = str(raw.get("mime_type") or raw.get("mimeType") or "").lower()
    if mime_type:
        return "webm" in mime_type
    codec = pretty_codec(getattr(stream, "codecs", None), getattr(stream, "media_type", None) or "unknown")
    return codec in {"VP8", "VP9"}


def _sniff_media_container_from_bytes(data: bytes) -> str | None:
    sample = bytes(data[:4096]).lstrip(b"\x00")
    if sample.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if len(sample) >= 8 and sample[4:8] in {b"ftyp", b"styp", b"moov", b"moof"}:
        return "mp4"
    return None


def _sniff_media_container_from_path(path: Path, max_bytes: int = 4096) -> str | None:
    try:
        with Path(path).open("rb") as source:
            return _sniff_media_container_from_bytes(source.read(max(1, max_bytes)))
    except OSError:
        return None


def _apply_sniffed_sabr_container(stream, container: str | None) -> bool:
    if not container or not _is_sabr_stream(stream):
        return False
    if container not in {"webm", "mp4"}:
        return False
    changed = (getattr(stream, "extension", None) or "").lower() != container
    stream.extension = container
    if container == "webm" and getattr(stream, "media_type", None) == "video":
        if not pretty_codec(getattr(stream, "codecs", None), "video"):
            stream.codecs = "vp09"
            changed = True
    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict):
        extra["sabr_detected_container"] = container
    return changed


def _apply_sniffed_sabr_container_from_path(stream, path: Path) -> bool:
    return _apply_sniffed_sabr_container(stream, _sniff_media_container_from_path(path))


def _is_sabr_ump_live_stream(stream) -> bool:
    if not getattr(stream, "is_live", False):
        return False
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    return getattr(stream, "manifest_type", None) == "sabr_ump" or bool(extra.get("sabr_ump"))


def _stream_uses_dts_audio(stream) -> bool:
    if getattr(stream, "media_type", None) != "audio":
        return False
    codec_label = pretty_codec(getattr(stream, "codecs", None), "audio")
    if codec_label in {"DTS", "DTS-HD", "DTS:X"}:
        return True
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if raw_codec.startswith(("dts", "dtsh", "dtsl", "dtse", "dtsx")):
        return True
    url = (getattr(stream, "url", None) or "").lower()
    return bool(re.search(r"(?:^|[^a-z0-9])(?:dtsx|dts-x|dts_x)(?:[^a-z0-9]|$)", url))


def _stream_should_remux_live_pipe_fragments(stream) -> bool:
    if not _stream_needs_fragment_parts(stream):
        return False
    if _stream_prefers_matroska_live_pipe(stream):
        return False
    return _stream_can_remux_live_pipe_fragment_to_ts(stream)


def _stream_can_remux_live_pipe_fragment_to_ts(stream) -> bool:
    codec = pretty_codec(getattr(stream, "codecs", None), getattr(stream, "media_type", None) or "unknown")
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if getattr(stream, "media_type", None) == "video":
        return codec == "H.264" or raw_codec.startswith(("avc1", "avc3", "h264"))
    if getattr(stream, "media_type", None) == "audio":
        if codec in {"AAC", "HE-AAC", "AC-3", "E-AC-3", "E-AC-3 Atmos"}:
            return True
        return raw_codec.startswith(("mp4a", "ac-3", "ec-3", "aac"))
    return False


def _stream_prefers_matroska_live_pipe(stream) -> bool:
    if _stream_uses_webm_container(stream):
        return True
    if getattr(stream, "media_type", None) != "video":
        return False
    codec = pretty_codec(getattr(stream, "codecs", None), "video")
    if codec in {"H.265", "H.266"}:
        return True
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if raw_codec.startswith(("hvc1", "hev1", "hevc", "h265", "dvh1", "dvhe", "vvc1", "vvi1", "h266", "h.266", "vvc")):
        return True
    video_range = (getattr(stream, "video_range", None) or "").upper()
    return "DV" in video_range


_HLS_SEGMENT_CRYPTO_SCHEMES = {"AES_128", "AES_128_ECB", "CHACHA20", "YOUKU_ECB"}
_AUDIO_CODEC_LABELS = {"AAC", "HE-AAC", "AC-3", "E-AC-3", "E-AC-3 Atmos", "Opus"}
_FRAGMENTED_MP4_EXTS = {"m4s", "mp4", "m4a", "m4v", "cmfv", "cmfa", "mp4a", "mp4v"}
_JSON_FMP4_PART_DECRYPT_MIN_SEGMENTS = 512
_JSON_FMP4_PART_DECRYPT_MIN_BYTES = 1024 * 1024 * 1024


def _normalized_scheme(value: str | None) -> str:
    return (value or "").upper().replace("-", "_")


def _stream_hls_segment_crypto_scheme(stream) -> str | None:
    if getattr(stream, "manifest_type", None) != "hls":
        return None
    candidates = [getattr(stream, "encryption_scheme", None)]
    candidates.extend(getattr(segment, "encryption_scheme", None) for segment in getattr(stream, "segments", []) or [])
    for value in candidates:
        scheme = _normalized_scheme(value)
        if scheme in _HLS_SEGMENT_CRYPTO_SCHEMES:
            return scheme
    return None


def _stream_uses_hls_segment_crypto(stream) -> bool:
    return _stream_hls_segment_crypto_scheme(stream) is not None


def _stream_uses_bbts(stream) -> bool:
    candidates = [getattr(stream, "encryption_scheme", None)]
    candidates.extend(getattr(segment, "encryption_scheme", None) for segment in getattr(stream, "segments", []) or [])
    if any(_normalized_scheme(value) == "BBTS" for value in candidates):
        return True
    extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
    if extension == "bbts":
        return True
    for segment in getattr(stream, "segments", []) or []:
        path = urlparse(getattr(segment, "url", "")).path.lower()
        if Path(path).suffix.lower() == ".bbts":
            return True
    return False


def _bbts_key_hex(keys, hls_crypto: HlsCrypto | None = None, stream=None) -> str:
    if hls_crypto and hls_crypto.key:
        if len(hls_crypto.key) != 16:
            raise ValueError("BBTS stream needs a 16-byte key; pass --custom-hls-key as 32 hex characters.")
        return hls_crypto.key.hex()
    if not keys:
        raise ValueError("BBTS stream needs --key KID:KEY, --key KEY, or --custom-hls-key KEY.")
    expected = set(_stream_key_ids(stream)) if stream is not None else set()
    if expected:
        for item in keys:
            if item.kid and item.kid.lower().replace("-", "") in expected:
                return item.key
    return keys[0].key


def _stream_has_muxed_audio(stream) -> bool:
    if getattr(stream, "media_type", None) != "video":
        return False
    if getattr(stream, "extra", {}).get("muxed_audio"):
        return True
    if getattr(stream, "extra", {}).get("audio_id"):
        return False
    return pretty_codec(getattr(stream, "codecs", None), "audio") in _AUDIO_CODEC_LABELS


def _live_pipe_map_specs(stream, input_index: int) -> list[str]:
    if mapped := yangshipin_casting_live_pipe_map_specs(stream, input_index):
        return mapped
    if _stream_has_muxed_audio(stream):
        return [f"{input_index}:v?", f"{input_index}:a?"]
    return [f"{input_index}:0"]


def _stream_uses_native_mpegts(stream) -> bool:
    extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
    if extension in {"ts", "bbts"}:
        return True
    for segment in getattr(stream, "segments", []) or []:
        path = urlparse(getattr(segment, "url", "")).path.lower()
        if Path(path).suffix.lower() in {".ts", ".bbts"}:
            return True
    return False


def _stream_uses_hls_fragmented_mp4(stream) -> bool:
    if getattr(stream, "manifest_type", None) != "hls":
        return False
    if _stream_uses_webm_container(stream) or _stream_uses_native_mpegts(stream):
        return False
    extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
    if extension in _FRAGMENTED_MP4_EXTS:
        return True
    for segment in getattr(stream, "segments", []) or []:
        path = urlparse(getattr(segment, "url", "")).path.lower()
        if Path(path).suffix.lower().lstrip(".") in _FRAGMENTED_MP4_EXTS:
            return True
    return _stream_has_init_segment(stream)


def _live_pipe_raw_audio_input_format(stream) -> str | None:
    if getattr(stream, "manifest_type", None) != "hls":
        return None
    if getattr(stream, "media_type", None) != "audio":
        return None
    if _stream_uses_native_mpegts(stream) or _stream_needs_fragment_parts(stream):
        return None
    extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
    if extension in _FRAGMENTED_MP4_EXTS or _stream_has_init_segment(stream):
        return None
    codec = pretty_codec(getattr(stream, "codecs", None), "audio")
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if extension in {"aac", "adts"} or codec in {"AAC", "HE-AAC"} or raw_codec.startswith(("mp4a", "aac")):
        return "aac"
    if extension == "ac3" or codec == "AC-3" or raw_codec.startswith("ac-3"):
        return "ac3"
    if extension == "eac3" or codec in {"E-AC-3", "E-AC-3 Atmos"} or raw_codec.startswith("ec-3"):
        return "eac3"
    if extension == "mp3" or codec == "MP3" or raw_codec.startswith("mp3"):
        return "mp3"
    return None


def _sabr_live_pipe_audio_input_format(stream) -> str | None:
    if not _is_sabr_ump_live_stream(stream):
        return None
    if getattr(stream, "media_type", None) != "audio":
        return None
    if _stream_uses_webm_container(stream):
        return None
    codec = pretty_codec(getattr(stream, "codecs", None), "audio")
    raw_codec = (getattr(stream, "codecs", None) or "").lower()
    if codec in {"AAC", "HE-AAC"} or raw_codec.startswith(("mp4a", "aac")):
        return "mpegts"
    return None


def _stream_has_init_segment(stream) -> bool:
    return any(getattr(segment, "index", None) == -1 for segment in getattr(stream, "segments", []) or [])


def _stream_has_hls_key_source(stream, hls_crypto: HlsCrypto | None) -> bool:
    if hls_crypto and _normalized_scheme(hls_crypto.method) == "NONE":
        return False
    if hls_crypto and hls_crypto.key:
        return True
    if hls_crypto and hls_crypto.decryptor is not None:
        return True
    return any(getattr(segment, "key_uri", None) for segment in getattr(stream, "segments", []) or [])


def _stream_hls_crypto_can_decrypt(stream, hls_crypto: HlsCrypto | None) -> bool:
    return _stream_uses_hls_segment_crypto(stream) and _stream_has_hls_key_source(stream, hls_crypto)


def _stream_needs_external_decryption(stream, hls_crypto: HlsCrypto | None) -> bool:
    if not getattr(stream, "encrypted", False):
        return False
    if is_vgc_stream(stream):
        return False
    if _stream_uses_hls_segment_crypto(stream):
        return False
    return True


def _apple_music_foothill_context(args):
    raw = getattr(args, "service_context", None)
    if not isinstance(raw, dict):
        return None
    music = raw.get("apple_music")
    if not isinstance(music, dict):
        return None
    helper = str(music.get("foothill_helper") or "").strip()
    contexts = music.get("foothill_context_keys")
    if not helper or not isinstance(contexts, dict):
        return None
    normalized = {
        str(key): str(value)
        for key, value in contexts.items()
        if str(key).strip() and str(value).strip()
    }
    try:
        track_type = int(music.get("foothill_track_type") or 0)
    except (TypeError, ValueError):
        track_type = 0
    if track_type not in {0, 1}:
        track_type = 0
    default_context = str(music.get("foothill_default_context_key") or "").strip()
    if not default_context:
        default_context = next(iter(normalized.values()), "")
    return {
        "helper": helper,
        "contexts": normalized,
        "default_context": default_context,
        "track_type": track_type,
    } if normalized else None


def _deezer_transport_context(stream) -> dict[str, object] | None:
    """Read the Deezer transport marker carried by a JSON audio track."""
    extra = getattr(stream, "extra", None)
    raw = extra.get("raw") if isinstance(extra, dict) else None
    value = raw.get("deezer") if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        return None
    track_id = str(value.get("track_id") or value.get("trackId") or "").strip()
    cipher = normalize_deezer_cipher(value.get("cipher"))
    if not track_id or cipher not in {"NONE", BF_CBC_STRIPE}:
        return None
    value["track_id"] = track_id
    value["cipher"] = cipher
    value["extension"] = str(value.get("extension") or "").strip().lower().lstrip(".")
    value["decrypted"] = bool(value.get("decrypted"))
    return value


def _apply_apple_music_stream_context(streams, args) -> None:
    """Restore Apple Music's audio/video type after parsing its ranged fMP4 URL."""
    raw = getattr(args, "service_context", None)
    music = raw.get("apple_music") if isinstance(raw, dict) else None
    if not isinstance(music, dict):
        return
    try:
        track_type = int(music.get("foothill_track_type") or 0)
    except (TypeError, ValueError):
        return
    if track_type not in {0, 1}:
        return
    for stream in streams:
        if getattr(stream, "media_type", None) in {"subtitle", "subtitles", "text"}:
            continue
        if getattr(stream, "manifest_type", None) != "hls":
            continue
        stream.media_type = "audio" if track_type == 0 else "video"
        if track_type == 0:
            stream.extra["apple_music_audio_only"] = True


def _ensure_hls_segment_crypto_ready(streams, hls_crypto: HlsCrypto | None) -> None:
    missing = [stream for stream in streams if getattr(stream, "encrypted", False) and _stream_uses_hls_segment_crypto(stream) and not _stream_hls_crypto_can_decrypt(stream, hls_crypto)]
    if not missing:
        return
    scheme = _stream_hls_segment_crypto_scheme(missing[0]) or "AES-128"
    raise ValueError(f"{scheme.replace('_', '-')} HLS stream needs playlist KEY URI or --custom-hls-key.")


def _start_live_pipe_mux_session(live_streams, output: Path, save_name: str | None, default_save_base: str | None, args, keys, colors, hls_crypto=None, temp_dir: Path | None = None):
    if not getattr(args, "live_pipe_mux", False):
        return None
    _ensure_live_json_init_segments(live_streams)
    pipe_streams = [stream for stream in live_streams if not _is_subtitle_stream(stream)]
    if not pipe_streams:
        print(paint("Note:", Palette.yellow, colors) + " live pipe mux has no audio/video tracks; subtitles will stay as sidecar files.")
        return None
    if any(is_vgc_stream(stream) for stream in pipe_streams):
        args._live_pipe_mux_disabled_note = "VGC live tracks are recorded as track files; pipe mux is disabled until the bridge returns clear media."
        args._live_pipe_mux_disabled_mux_format = "mkv"
        return None
    combo_disabled_note = _live_pipe_mux_combination_disabled_note(pipe_streams)
    if combo_disabled_note:
        args._live_pipe_mux_disabled_note = combo_disabled_note
        args._live_pipe_mux_disabled_mux_format = "mkv"
        return None
    disabled_note = _live_pipe_mux_disabled_note(pipe_streams)
    if disabled_note:
        args._live_pipe_mux_disabled_note = disabled_note
        args._live_pipe_mux_disabled_mux_format = "mkv"
        return None
    if any(getattr(stream, "encrypted", False) for stream in pipe_streams) and getattr(args, "no_decrypt", False):
        raise ValueError("Encrypted live pipe mux cannot be used with --no-decrypt.")
    _ensure_hls_segment_crypto_ready(pipe_streams, hls_crypto)
    encrypted = [stream for stream in pipe_streams if _stream_needs_external_decryption(stream, hls_crypto)]
    if encrypted and not keys:
        raise ValueError("Encrypted live pipe mux needs --key and cannot be used with --no-decrypt.")
    if not hasattr(os, "mkfifo"):
        raise RuntimeError("--live-pipe-mux needs POSIX named pipes; this platform is not supported yet.")
    output_path = _live_pipe_output_path(output, save_name, default_save_base, streams=pipe_streams, mux_format=getattr(args, "mux_format", None))
    notes = []
    if output_path.suffix.lower() == ".mkv" and not getattr(args, "mux_format", None):
        notes.append("HEVC/DV/WebM live pipe mux uses MKV to avoid TS bitstream filter errors.")
    if (
        output_path.suffix.lower() == ".mkv"
        and should_finalize_live_pipe_matroska_output_to_mp4(pipe_streams, "matroska")
        and _live_pipe_finalize_to_mp4_allowed(args)
    ):
        notes.append("KAN/MedOne HEVC live pipe mux finalizes to MP4 after recording stops for player compatibility.")
    if any(_stream_uses_webm_container(stream) for stream in pipe_streams):
        notes.append("VP9/WebM live pipe mux uses the internal continuous WebM writer.")
    input_offsets = live_pipe_input_offsets_seconds(pipe_streams, args)
    return _LivePipeMuxSession(
        pipe_streams,
        output_path,
        keys=keys,
        args=args,
        colors=colors,
        hls_crypto=hls_crypto,
        temp_dir=temp_dir,
        input_offsets_seconds=input_offsets,
        notes=notes,
    )


def _live_pipe_mux_combination_disabled_note(streams) -> str | None:
    if not any(_is_sabr_ump_live_stream(stream) for stream in streams):
        return None
    has_webm_video = any(
        getattr(stream, "media_type", None) == "video" and _stream_uses_webm_container(stream)
        for stream in streams
    )
    has_fragmented_aac_audio = any(_sabr_live_pipe_audio_input_format(stream) == "mpegts" for stream in streams)
    if has_webm_video and has_fragmented_aac_audio:
        return (
            "YouTube SABR live VP9/WebM plus AAC/fMP4 pipe mux is disabled; recording tracks first "
            "avoids short or missing audio while preserving SABR live output."
        )
    return None


def _live_pipe_mux_disabled_note(streams) -> str | None:
    notes = []
    for stream in streams:
        note = live_pipe_mux_disabled_reason(stream)
        if note and note not in notes:
            notes.append(note)
    return " ".join(notes) if notes else None


def _live_pipe_output_path(output: Path, save_name: str | None, default_save_base: str | None, streams=None, mux_format: str | None = None) -> Path:
    requested = (mux_format or "").lower().lstrip(".")
    if requested:
        target_format = requested
    elif streams and any(_stream_prefers_matroska_live_pipe(stream) for stream in streams):
        target_format = "mkv"
    else:
        target_format = "ts"
    if target_format == "mp4":
        target_format = "mkv"
    target = _mux_output_path(output, save_name, default_save_base, target_format)
    expected_suffix = f".{target_format}"
    if target.suffix.lower() != expected_suffix:
        target = target.with_suffix(expected_suffix)
        target = unique_path(target)
    return target


def _live_pipe_finalize_to_mp4_allowed(args) -> bool:
    requested = (getattr(args, "mux_format", None) or "").lower().lstrip(".")
    return requested not in {"mkv", "matroska"}


def _live_pipe_stop_on_interrupt_enabled(pipe_session) -> bool:
    return bool(
        pipe_session
        and should_finalize_live_pipe_matroska_output(
            getattr(pipe_session, "streams", []),
            getattr(pipe_session, "output_container", None),
        )
    )


def _copy_file_to_output(path: Path, output) -> None:
    with path.open("rb") as source:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def _live_pipe_part_contains_init(stream, segment, path: Path) -> bool:
    if not live_pipe_media_part_contains_init(stream, segment):
        return False
    if not path.exists():
        return True
    return _mp4_path_has_top_level_box(path, b"moov")


def _mp4_path_has_top_level_box(path: Path, box_type: bytes) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    try:
        with path.open("rb") as file:
            position = 0
            while position + 8 <= size:
                file.seek(position)
                header = file.read(8)
                if len(header) < 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                current_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = file.read(8)
                    if len(extended) < 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = size - position
                if box_size < header_size:
                    return False
                if current_type == box_type:
                    return True
                position += box_size
    except OSError:
        return False
    return False


def _prepare_live_pipe_temp_dir(output_path: Path, temp_dir: Path | None) -> Path:
    if temp_dir is None:
        return Path(tempfile.mkdtemp(prefix="unidown_live_pipe_", dir=str(output_path.parent)))
    pipe_dir = Path(temp_dir).expanduser()
    if pipe_dir.exists():
        shutil.rmtree(pipe_dir, ignore_errors=True)
    pipe_dir.mkdir(parents=True, exist_ok=True)
    return pipe_dir


class _LiveKeyValidationError(RuntimeError):
    pass


def _live_pipe_segment_marker(segment: SegmentInfo) -> tuple[str, object] | None:
    if getattr(segment, "index", None) is not None and segment.index >= 0:
        return ("index", segment.index)
    if getattr(segment, "timeline_time", None) is not None:
        return ("timeline", segment.timeline_time)
    if getattr(segment, "program_date_time", None):
        return ("program", segment.program_date_time)
    return None


class _LivePipeMuxSession:
    writer_queue_size = 4

    def __init__(
        self,
        streams,
        output_path: Path,
        keys,
        args,
        colors: bool | None = None,
        hls_crypto=None,
        temp_dir: Path | None = None,
        input_offsets_seconds: dict[int, float] | None = None,
        notes: Sequence[str] | None = None,
    ):
        self.streams = list(streams)
        self.output_path = output_path
        self.keys = keys
        self.args = args
        self.colors = colors
        self.hls_crypto = hls_crypto
        self.notes = list(notes or [])
        self.input_offsets_seconds = dict(input_offsets_seconds or {})
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.pipe_dir = _prepare_live_pipe_temp_dir(output_path, temp_dir)
        self.pipe_paths: dict[int, Path] = {}
        self.output_container = self._output_container()
        self.pipe_input_formats: dict[int, str] = {id(stream): self._pipe_input_format(stream) for stream in self.streams}
        self.fragment_decrypters: dict[int, str] = {
            id(stream): _live_pipe_fragment_decrypter(args.decrypter, stream, self.pipe_input_formats[id(stream)])
            for stream in self.streams
        }
        self.writers = {}
        self.webm_live_writers: dict[int, ContinuousWebMWriter] = {}
        self.writer_queues: dict[int, queue.Queue[LiveSegmentBatch | None]] = {}
        self.writer_threads: list[threading.Thread] = []
        self.writer_error: BaseException | None = None
        self.current_init: dict[int, Path] = {}
        self.current_decrypted_init: dict[int, Path] = {}
        self.patched_inits: dict[tuple[int, str], Path] = {}
        self.active_patch_kids: dict[int, str] = {}
        self.detected_pipe_part_kids: dict[tuple[int, str], list[str]] = {}
        self.initial_stream_kids: dict[int, set[str]] = {id(stream): set(_stream_key_ids(stream)) for stream in self.streams}
        self.last_enqueued_kids: dict[int, str | None] = {
            id(stream): self._initial_live_pipe_key_id(stream) for stream in self.streams
        }
        self.rotation_sync_stream_ids = {
            id(stream) for stream in self.streams if getattr(stream, "media_type", None) in {"video", "audio"}
        }
        self.rotation_markers: set[tuple[str, object]] = set()
        self.rotation_ready: dict[tuple[str, object], set[int]] = {}
        self.rotation_released: set[tuple[str, object]] = set()
        self.rotation_condition = threading.Condition()
        self.validated_fragment_kids: set[tuple[int, str]] = set()
        self.next_decode_times: dict[int, int | None] = {}
        self.source_decode_times: dict[int, int] = {}
        self.source_timescales: dict[int, int] = {}
        self.sent_pipe_init: set[int] = set()
        self.decrypt_events_seen: set[str] = set()
        self.decrypt_event_lock = threading.Lock()
        self.stream_ids = {id(stream) for stream in self.streams}
        self.stream_segment_counters: dict[int, int] = {}
        self.lock = threading.Lock()
        self.process_lock = threading.Lock()
        self.counter = 0
        self.process: subprocess.Popen | None = None
        self.ffmpeg_stderr: str | None = None
        self.finalize_error: str | None = None
        self.finalize_succeeded = False
        self.closed = False
        try:
            self.output_path.unlink()
        except FileNotFoundError:
            pass
        self._create_pipes()
        self._start_ffmpeg()
        self._start_writer_threads()

    def has_stream(self, stream) -> bool:
        return id(stream) in self.stream_ids

    def write_batch(self, batch: LiveSegmentBatch) -> None:
        self._register_rotation_boundaries([batch])
        self._enqueue_batch(batch)

    def write_group(self, batches: list[LiveSegmentBatch]) -> None:
        self._register_rotation_boundaries(batches)
        for batch in batches:
            self._enqueue_batch(batch)

    def _enqueue_batch(self, batch: LiveSegmentBatch) -> None:
        if _is_subtitle_stream(batch.stream):
            return
        if not self.has_stream(batch.stream):
            return
        self._ensure_ffmpeg_running()
        if self.writer_error:
            raise RuntimeError(self._ffmpeg_exit_message("live pipe mux stopped while writing media", terminate=True)) from self.writer_error
        writer_queue = self.writer_queues[id(batch.stream)]
        while True:
            self._ensure_ffmpeg_running()
            if self.writer_error:
                raise RuntimeError(self._ffmpeg_exit_message("live pipe mux stopped while writing media", terminate=True)) from self.writer_error
            try:
                writer_queue.put(batch, timeout=0.2)
                return
            except queue.Full:
                continue

    def close(self, cancelled: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        self._wait_for_writer_queues(timeout=5 if cancelled else 20)
        for writer_queue in self.writer_queues.values():
            self._stop_writer_queue(writer_queue, timeout=1 if cancelled else 5)
        for thread in self.writer_threads:
            thread.join(timeout=5)
        for writer in list(self.writers.values()):
            try:
                writer.close()
            except OSError:
                pass
        self.writers.clear()
        self.webm_live_writers.clear()
        for thread in self.writer_threads:
            if thread.is_alive():
                thread.join(timeout=5)
        stderr = self._collect_ffmpeg_stderr(terminate=self.writer_error is not None, timeout=8 if cancelled else 20)
        self._finalize_live_pipe_output(cancelled=cancelled)
        if getattr(self.args, "del_after_done", True) and not getattr(self.args, "keep_temp", False):
            shutil.rmtree(self.pipe_dir, ignore_errors=True)
        if not cancelled and self.writer_error is not None and not self._writer_error_is_clean_eof():
            message = self._ffmpeg_exit_message("live pipe mux stopped while writing media")
            raise RuntimeError(message) from self.writer_error
        if not cancelled and self.process and self.process.returncode not in {0, None} and not self._nonzero_ffmpeg_exit_is_finalized_output():
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"live pipe mux failed with status {self.process.returncode}{detail}")

    def _finalize_live_pipe_output(self, cancelled: bool = False) -> None:
        if not should_finalize_live_pipe_matroska_output(getattr(self, "streams", []), getattr(self, "output_container", None)):
            return
        self.finalize_succeeded = False
        try:
            if not self.output_path.exists() or self.output_path.stat().st_size <= 0:
                return
        except OSError:
            return
        try:
            self._finalize_matroska_output(timeout=8 if cancelled else 30)
        except RuntimeError as exc:
            self.finalize_error = str(exc)
            print(
                paint("Warning:", Palette.yellow, getattr(self, "colors", None))
                + f" live pipe output finalization failed; keeping raw output: {exc}",
                file=sys.stderr,
            )

    def _nonzero_ffmpeg_exit_is_finalized_output(self) -> bool:
        if not should_finalize_live_pipe_matroska_output(getattr(self, "streams", []), getattr(self, "output_container", None)):
            return False
        if not self.finalize_succeeded:
            return False
        try:
            return self.output_path.exists() and self.output_path.stat().st_size > 0
        except OSError:
            return False

    def _finalize_matroska_output(self, timeout: int = 30) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpeg not found")
        finalize_to_mp4 = should_finalize_live_pipe_matroska_output_to_mp4(
            getattr(self, "streams", []),
            getattr(self, "output_container", None),
        ) and _live_pipe_finalize_to_mp4_allowed(self.args)
        suffix = self.output_path.suffix or ".mkv"
        temp_base = f".{self.output_path.name}.{os.getpid()}.{time.monotonic_ns()}.finalize"
        temp_mp4_path = self.output_path.with_name(f"{temp_base}.mp4")
        temp_hevc_path = self.output_path.with_name(f"{temp_base}.hevc")
        temp_mkv_path = self.output_path.with_name(f"{temp_base}{suffix}")
        temp_compatible_mp4_path = self.output_path.with_name(f"{temp_base}.compatible.mp4")
        try:
            self._run_live_pipe_finalize_ffmpeg(
                [
                    executable,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-fflags",
                    "+genpts+igndts",
                    "-i",
                    str(self.output_path),
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    str(temp_mp4_path),
                ],
                timeout=timeout,
                stage="MP4 timestamp rebuild",
            )
            self._ensure_live_pipe_finalize_output(temp_mp4_path, "ffmpeg produced an empty finalized MP4")
            try:
                used_mkvmerge = self._finalize_matroska_output_with_mkvmerge(
                    temp_mp4_path,
                    temp_hevc_path,
                    temp_mkv_path,
                    executable,
                    timeout,
                )
            except RuntimeError:
                used_mkvmerge = False
                for temp_path in (temp_hevc_path, temp_mkv_path):
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            if not used_mkvmerge:
                self._run_live_pipe_finalize_ffmpeg(
                    [
                        executable,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(temp_mp4_path),
                        "-map",
                        "0",
                        "-c",
                        "copy",
                        "-avoid_negative_ts",
                        "make_zero",
                        "-f",
                        "matroska",
                        str(temp_mkv_path),
                    ],
                    timeout=timeout,
                    stage="Matroska timestamp finalize",
                )
            self._ensure_live_pipe_finalize_output(temp_mkv_path, "ffmpeg produced an empty finalized MKV")
            if finalize_to_mp4:
                self._finalize_matroska_output_to_mp4(
                    temp_mkv_path,
                    temp_compatible_mp4_path,
                    executable,
                    timeout,
                )
            else:
                temp_mkv_path.replace(self.output_path)
        except OSError as exc:
            raise RuntimeError(str(exc)) from exc
        finally:
            for temp_path in (temp_mp4_path, temp_hevc_path, temp_mkv_path, temp_compatible_mp4_path):
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.finalize_error = None
        self.finalize_succeeded = True

    def _finalize_matroska_output_to_mp4(
        self,
        finalized_mkv_path: Path,
        temp_mp4_path: Path,
        ffmpeg_executable: str,
        timeout: int,
    ) -> None:
        self._run_live_pipe_finalize_ffmpeg(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(finalized_mkv_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-bsf:v",
                "hevc_metadata=colour_primaries=9:transfer_characteristics=18:matrix_coefficients=9",
                "-tag:v",
                "hvc1",
                "-movflags",
                "+faststart",
                str(temp_mp4_path),
            ],
            timeout=timeout,
            stage="MP4 HEVC compatibility finalize",
        )
        self._ensure_live_pipe_finalize_output(temp_mp4_path, "ffmpeg produced an empty compatible MP4")
        old_output_path = self.output_path
        final_mp4_path = unique_path(old_output_path.with_suffix(".mp4"))
        temp_mp4_path.replace(final_mp4_path)
        self.output_path = final_mp4_path
        self.output_container = "mp4"
        try:
            old_output_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _finalize_matroska_output_with_mkvmerge(
        self,
        temp_mp4_path: Path,
        temp_hevc_path: Path,
        temp_mkv_path: Path,
        ffmpeg_executable: str,
        timeout: int,
    ) -> bool:
        mkvmerge = shutil.which("mkvmerge")
        if not mkvmerge:
            return False
        self._run_live_pipe_finalize_ffmpeg(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(temp_mp4_path),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-bsf:v",
                "hevc_mp4toannexb",
                "-f",
                "hevc",
                str(temp_hevc_path),
            ],
            timeout=timeout,
            stage="HEVC Annex-B extraction",
        )
        self._ensure_live_pipe_finalize_output(temp_hevc_path, "ffmpeg produced an empty HEVC elementary stream")
        args = [
            mkvmerge,
            "-q",
            "-o",
            str(temp_mkv_path),
            "--default-duration",
            f"0:{self._live_pipe_finalize_video_fps():g}fps",
            str(temp_hevc_path),
            "--no-video",
            str(temp_mp4_path),
        ]
        self._run_live_pipe_finalize_process(args, timeout=timeout, stage="Matroska HEVC header finalize")
        return True

    def _run_live_pipe_finalize_ffmpeg(self, args: list[str], timeout: int, stage: str) -> None:
        self._run_live_pipe_finalize_process(args, timeout=timeout, stage=stage)

    def _run_live_pipe_finalize_process(self, args: list[str], timeout: int, stage: str) -> None:
        try:
            result = managed_run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{stage} timed out") from exc
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            message = f"{stage} exited with status {result.returncode}"
            if detail:
                message += f": {detail}"
            raise RuntimeError(message)

    def _ensure_live_pipe_finalize_output(self, path: Path, message: str) -> None:
        if not path.exists() or path.stat().st_size <= 0:
            raise RuntimeError(message)

    def _live_pipe_finalize_video_fps(self) -> float:
        for stream in getattr(self, "streams", []) or []:
            if getattr(stream, "media_type", None) != "video":
                continue
            try:
                value = float(getattr(stream, "frame_rate", None) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        return 50.0

    def _wait_for_writer_queues(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        queues = list(self.writer_queues.values())
        while any(getattr(writer_queue, "unfinished_tasks", 0) for writer_queue in queues):
            if self.writer_error is not None or time.monotonic() >= deadline:
                break
            _runtime_sleep(0.05)

    def _stop_writer_queue(self, writer_queue: queue.Queue[LiveSegmentBatch | None], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                writer_queue.put(None, timeout=0.1)
                return
            except queue.Full:
                if time.monotonic() >= deadline:
                    self._set_writer_error(TimeoutError("live pipe writer did not drain before stop"))
                    return

    def _writer_error_is_clean_eof(self) -> bool:
        return (
            isinstance(self.writer_error, BrokenPipeError)
            and self.process is not None
            and self.process.returncode == 0
            and self.output_path.exists()
        )

    def _create_pipes(self) -> None:
        for index, stream in enumerate(self.streams, start=1):
            path = self.pipe_dir / f"{index:02d}_{stream.display_prefix().lower()}.fifo"
            os.mkfifo(path)
            self.pipe_paths[id(stream)] = path

    def _start_ffmpeg(self) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpeg not found; --live-pipe-mux needs ffmpeg.")
        args = [executable, "-hide_banner", "-y"]
        if not all(self.pipe_input_formats[id(stream)] == "mpegts" for stream in self.streams):
            args.extend(["-fflags", "+genpts"])
        args.extend(["-loglevel", "error"])
        for stream in self.streams:
            args.extend([
                "-thread_queue_size",
                "1024",
            ])
            input_offset = self.input_offsets_seconds.get(id(stream), 0.0)
            if input_offset > 0.001:
                args.extend(["-itsoffset", f"{input_offset:.3f}"])
            input_format = self.pipe_input_formats[id(stream)]
            if input_format == "mp4":
                args.extend(["-probesize", "32768", "-analyzeduration", "0"])
            elif input_format == "mpegts" and _sabr_live_pipe_audio_input_format(stream) == "mpegts":
                args.extend(["-probesize", "1048576", "-analyzeduration", "1000000"])
            args.extend(["-f", input_format])
            args.extend([
                "-i",
                str(self.pipe_paths[id(stream)]),
            ])
        for index, stream in enumerate(self.streams):
            for spec in _live_pipe_map_specs(stream, index):
                args.extend(["-map", spec])
        args.extend([
            "-strict",
            "unofficial",
            "-c",
            "copy",
            "-ignore_unknown",
            "-copy_unknown",
            "-flush_packets",
            "1",
        ])
        if self._should_use_shortest():
            args.append("-shortest")
        if self.output_container == "mpegts":
            args.extend(["-f", "mpegts", "-mpegts_flags", "+resend_headers"])
        else:
            args.extend(live_pipe_matroska_options(self.streams, self.output_container))
            args.extend(["-f", self.output_container])
        args.append(str(self.output_path))
        # Keep Ctrl-C with the recorder. FFmpeg must see FIFO EOF so it can
        # write the Matroska trailer and indexes before exiting normally.
        self.process = managed_popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def _should_use_shortest(self) -> bool:
        return not any(_is_sabr_ump_live_stream(stream) for stream in self.streams)

    def _start_writer_threads(self) -> None:
        for stream in self.streams:
            writer_queue: queue.Queue[LiveSegmentBatch | None] = queue.Queue(maxsize=self.writer_queue_size)
            self.writer_queues[id(stream)] = writer_queue
            thread = threading.Thread(target=self._writer_loop, args=(stream, writer_queue), daemon=True)
            thread.start()
            self.writer_threads.append(thread)

    def _writer_loop(self, stream, writer_queue: queue.Queue[LiveSegmentBatch | None]) -> None:
        while True:
            batch = writer_queue.get()
            try:
                if batch is None:
                    writer = self.writers.pop(id(stream), None)
                    if writer is not None:
                        writer.close()
                    return
                payload = self._payload_items(batch)
                if not payload:
                    continue
                writer = self._writer_for(stream)
                for _sequence, path, segment in payload:
                    self._wait_for_rotation_boundary(stream, segment)
                    self._write_payload_path(stream, writer, path, segment)
            except BaseException as exc:
                self._set_writer_error(exc)
                return
            finally:
                writer_queue.task_done()

    def _set_writer_error(self, exc: BaseException) -> None:
        with self.process_lock:
            if self.writer_error is None:
                self.writer_error = exc
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
        rotation_condition = getattr(self, "rotation_condition", None)
        if rotation_condition is not None:
            with rotation_condition:
                rotation_condition.notify_all()

    def _writer_for(self, stream):
        key = id(stream)
        writer = self.writers.get(key)
        if writer is None:
            writer = self.pipe_paths[key].open("wb", buffering=0)
            self.writers[key] = writer
        return writer

    def _write_payload_path(self, stream, writer, path: Path, segment: SegmentInfo | None = None) -> None:
        if self._uses_continuous_webm_pipe(stream):
            webm_writer = self.webm_live_writers.get(id(stream))
            if webm_writer is None:
                webm_writer = ContinuousWebMWriter(writer)
                self.webm_live_writers[id(stream)] = webm_writer
            duration_seconds = None
            if segment is not None and getattr(stream, "media_type", None) != "video":
                duration_seconds = getattr(segment, "duration", None)
            webm_writer.write_fragment(path, duration_seconds=duration_seconds)
        else:
            with path.open("rb") as file:
                shutil.copyfileobj(file, writer, length=1024 * 1024)
        writer.flush()

    def _uses_continuous_webm_pipe(self, stream) -> bool:
        return self.pipe_input_formats.get(id(stream)) == "matroska,webm"

    def _live_pipe_part_is_empty(self, path: Path) -> bool:
        try:
            return path.exists() and path.stat().st_size == 0
        except OSError:
            return False

    def _batch_has_media(self, batch: LiveSegmentBatch) -> bool:
        return any(segment.index != -1 for segment in batch.segments)

    def _ensure_ffmpeg_running(self) -> None:
        if self.process and self.process.poll() is not None:
            raise RuntimeError(self._ffmpeg_exit_message("live pipe mux stopped"))

    def _ffmpeg_exit_message(self, prefix: str, terminate: bool = False) -> str:
        stderr = self._collect_ffmpeg_stderr(terminate=terminate, timeout=5)
        code = self.process.poll() if self.process else None
        details = []
        if self.writer_error is not None:
            details.append(str(self.writer_error))
        if stderr:
            details.append(stderr)
        detail = f": {' | '.join(details)}" if details else ""
        status = f" with status {code}" if code is not None else ""
        return f"{prefix}{status}{detail}"

    def _collect_ffmpeg_stderr(self, terminate: bool = False, timeout: int = 20) -> str:
        if not self.process:
            return ""
        with self.process_lock:
            if self.ffmpeg_stderr is not None:
                return self.ffmpeg_stderr
            try:
                if terminate and self.process.poll() is None:
                    self.process.terminate()
                _, raw = self.process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, raw = self.process.communicate()
            self.ffmpeg_stderr = (raw or b"").decode("utf-8", errors="replace").strip()
            return self.ffmpeg_stderr

    def _payload_items(self, batch: LiveSegmentBatch) -> list[tuple[int, Path, SegmentInfo]]:
        self._hydrate_pipe_batch_segment_key_ids(batch)
        stream_key = id(batch.stream)
        pipe_format = self.pipe_input_formats.get(stream_key, "mpegts")
        fragment_decrypter = self.fragment_decrypters.get(stream_key, "internal")
        hls_segment_decrypted = _stream_hls_crypto_can_decrypt(batch.stream, self.hls_crypto)
        payload: list[tuple[int, Path, SegmentInfo]] = []
        for segment, part in zip(batch.segments, batch.parts, strict=False):
            if self._live_pipe_part_is_empty(part):
                continue
            segment_needs_fragment_decryption = segment.encrypted and not hls_segment_decrypted
            if segment.index == -1:
                self.current_init[stream_key] = part
                self.current_decrypted_init.pop(stream_key, None)
                self.patched_inits = {
                    key: value for key, value in self.patched_inits.items() if key[0] != stream_key
                }
                decrypted_init = None
                if segment_needs_fragment_decryption:
                    if pipe_format in {"nut", "matroska,webm"} and _stream_uses_webm_container(batch.stream):
                        if pipe_format == "matroska,webm":
                            payload.append((-1, self._decrypt_pipe_webm_fragment(batch.stream, part, segment), segment))
                        continue
                    decrypted_init = self._decrypt_pipe_init(batch.stream, part)
                    self.current_decrypted_init[stream_key] = decrypted_init
                if pipe_format == "matroska,webm":
                    if decrypted_init is not None:
                        payload.append((-1, decrypted_init, segment))
                    elif not segment_needs_fragment_decryption:
                        payload.append((-1, part, segment))
                    continue
                if pipe_format == "mp4":
                    if decrypted_init is not None:
                        payload.append((-1, decrypted_init, segment))
                        self.sent_pipe_init.add(stream_key)
                    elif not segment_needs_fragment_decryption:
                        payload.append((-1, part, segment))
                        self.sent_pipe_init.add(stream_key)
                continue
            sequence = self.stream_segment_counters.get(stream_key, 0)
            self.stream_segment_counters[stream_key] = sequence + 1
            if pipe_format == "matroska,webm":
                source_part = part
                if segment_needs_fragment_decryption:
                    source_part = self._decrypt_pipe_webm_fragment(batch.stream, part, segment)
                payload.append((sequence, source_part, segment))
                continue
            if pipe_format == "nut":
                source_part = part
                remux_init = None
                if segment_needs_fragment_decryption:
                    if _stream_uses_webm_container(batch.stream):
                        source_part = self._decrypt_pipe_webm_fragment(batch.stream, part, segment)
                    else:
                        init_path = self.current_init.get(stream_key)
                        if _is_sabr_ump_live_stream(batch.stream) and _live_pipe_part_contains_init(batch.stream, segment, part):
                            init_path = self._extract_pipe_self_contained_mp4_init(batch.stream, part)
                        if init_path is None:
                            raise RuntimeError("Encrypted live pipe fragment appeared before init segment.")
                        source_part = self._decrypt_pipe_fragment(batch.stream, part, init_path, decrypter=fragment_decrypter, segment=segment)
                        source_part = self._restamp_pipe_fragment(batch.stream, source_part, segment)
                        if not _live_pipe_part_contains_init(batch.stream, segment, source_part):
                            remux_init = self.current_decrypted_init.get(stream_key, init_path)
                elif not _stream_uses_webm_container(batch.stream):
                    init_path = self.current_init.get(stream_key)
                    if not _live_pipe_part_contains_init(batch.stream, segment, source_part):
                        remux_init = init_path
                nut_path = self._remux_pipe_fragment_to_nut(batch.stream, source_part, remux_init)
                payload.append((sequence, nut_path, segment))
                continue
            if pipe_format == "mp4":
                if segment_needs_fragment_decryption:
                    init_path = self.current_init.get(stream_key)
                    if _is_sabr_ump_live_stream(batch.stream) and _live_pipe_part_contains_init(batch.stream, segment, part):
                        init_path = self._extract_pipe_self_contained_mp4_init(batch.stream, part)
                    if init_path is None:
                        raise RuntimeError("Encrypted live pipe fragment appeared before init segment.")
                    decrypted = self._decrypt_pipe_fragment(batch.stream, part, init_path, decrypter=fragment_decrypter, segment=segment)
                    restamped = self._restamp_pipe_fragment(batch.stream, decrypted, segment)
                    payload.append((sequence, restamped, segment))
                else:
                    payload.append((sequence, self._restamp_pipe_fragment(batch.stream, part, segment), segment))
                continue
            if pipe_format == "mpegts" and _sabr_live_pipe_audio_input_format(batch.stream) == "mpegts":
                source_part = part
                if segment_needs_fragment_decryption:
                    init_path = self.current_init.get(stream_key)
                    if _live_pipe_part_contains_init(batch.stream, segment, part):
                        init_path = self._extract_pipe_self_contained_mp4_init(batch.stream, part)
                    if init_path is None:
                        raise RuntimeError("Encrypted live pipe fragment appeared before init segment.")
                    source_part = self._decrypt_pipe_fragment(batch.stream, part, init_path, decrypter=fragment_decrypter, segment=segment)
                payload.append((sequence, self._remux_pipe_fragment_to_ts(batch.stream, source_part, None), segment))
                continue
            if pipe_format in {"aac", "ac3", "eac3", "mp3"}:
                payload.append((sequence, part, segment))
                continue
            if not segment_needs_fragment_decryption and _stream_uses_native_mpegts(batch.stream):
                payload.append((sequence, part, segment))
                continue
            if not segment_needs_fragment_decryption:
                remux_init = None if _live_pipe_part_contains_init(batch.stream, segment, part) else self.current_init.get(stream_key)
                ts_path = self._remux_pipe_fragment_to_ts(batch.stream, part, remux_init)
                payload.append((sequence, ts_path, segment))
                continue
            init_path = self.current_init.get(stream_key)
            if _is_sabr_ump_live_stream(batch.stream) and _live_pipe_part_contains_init(batch.stream, segment, part):
                init_path = self._extract_pipe_self_contained_mp4_init(batch.stream, part)
            if init_path is None:
                raise RuntimeError("Encrypted live pipe fragment appeared before init segment.")
            decrypted = self._decrypt_pipe_fragment(batch.stream, part, init_path, decrypter=fragment_decrypter, segment=segment)
            restamped = self._restamp_pipe_fragment(batch.stream, decrypted, segment)
            remux_init = None if _live_pipe_part_contains_init(batch.stream, segment, restamped) else self.current_decrypted_init.get(stream_key, init_path)
            decrypted = self._remux_pipe_fragment_to_ts(batch.stream, restamped, remux_init)
            payload.append((sequence, decrypted, segment))
        return payload

    def _register_rotation_boundaries(self, batches: list[LiveSegmentBatch]) -> None:
        self._ensure_rotation_sync_state()
        media_markers_by_stream: dict[int, set[tuple[str, object]]] = {}
        changed_markers: set[tuple[str, object]] = set()
        for batch in batches:
            self._hydrate_pipe_batch_segment_key_ids(batch)
            stream_key = id(batch.stream)
            if stream_key not in self.rotation_sync_stream_ids:
                continue
            markers: set[tuple[str, object]] = set()
            previous_kid = self.last_enqueued_kids.get(stream_key)
            for segment in batch.segments:
                marker = _live_pipe_segment_marker(segment)
                if segment.index == -1 or marker is None:
                    continue
                markers.add(marker)
                kid = _normalize_kid_text(getattr(segment, "key_id", None))
                if kid and previous_kid and kid != previous_kid:
                    changed_markers.add(marker)
                if kid:
                    previous_kid = kid
            self.last_enqueued_kids[stream_key] = previous_kid
            media_markers_by_stream[stream_key] = markers
        if not self.rotation_sync_stream_ids.issubset(media_markers_by_stream):
            return
        common_markers = set.intersection(
            *(media_markers_by_stream[stream_key] for stream_key in self.rotation_sync_stream_ids)
        )
        with self.rotation_condition:
            self.rotation_markers.update(changed_markers.intersection(common_markers))

    def _wait_for_rotation_boundary(self, stream, segment: SegmentInfo) -> None:
        marker = _live_pipe_segment_marker(segment)
        if marker is None:
            return
        self._ensure_rotation_sync_state()
        with self.rotation_condition:
            if marker not in self.rotation_markers or marker in self.rotation_released:
                return
            ready = self.rotation_ready.setdefault(marker, set())
            ready.add(id(stream))
            if self.rotation_sync_stream_ids.issubset(ready):
                self.rotation_released.add(marker)
                self.rotation_condition.notify_all()
                return
            while marker not in self.rotation_released and self.writer_error is None:
                self.rotation_condition.wait(timeout=0.25)

    def _ensure_rotation_sync_state(self) -> None:
        if not hasattr(self, "rotation_sync_stream_ids"):
            self.rotation_sync_stream_ids = {
                id(stream) for stream in self.streams if getattr(stream, "media_type", None) in {"video", "audio"}
            }
        if not hasattr(self, "last_enqueued_kids"):
            initial = getattr(self, "initial_stream_kids", {})
            self.last_enqueued_kids = {
                id(stream): self._initial_live_pipe_key_id(stream)
                or next(iter(initial.get(id(stream), set())), None)
                for stream in self.streams
            }
        if not hasattr(self, "rotation_markers"):
            self.rotation_markers = set()
        if not hasattr(self, "rotation_ready"):
            self.rotation_ready = {}
        if not hasattr(self, "rotation_released"):
            self.rotation_released = set()
        if not hasattr(self, "rotation_condition"):
            self.rotation_condition = threading.Condition()

    def _hydrate_pipe_batch_segment_key_ids(self, batch: LiveSegmentBatch) -> None:
        if not self._should_detect_pipe_part_key_ids(batch.stream):
            return
        if not hasattr(self, "detected_pipe_part_kids"):
            self.detected_pipe_part_kids = {}
        for segment, part in zip(batch.segments, batch.parts, strict=False):
            if getattr(segment, "index", None) == -1:
                continue
            kids = self._detect_pipe_part_key_ids(batch.stream, part, segment)
            if not kids:
                continue
            current = _normalize_kid_text(getattr(segment, "key_id", None))
            if current not in kids:
                segment.key_id = kids[0]
            _add_stream_key_ids(batch.stream, kids)
            kid = _normalize_kid_text(getattr(segment, "key_id", None))
            if kid and self.keys and not _has_raw_key_for_kid(self.keys, kid):
                _prompt_for_live_key(
                    self.keys,
                    kid,
                    batch.stream,
                    segment,
                    self.colors,
                    reason="Live fragment KID detected",
                    provider=_live_key_provider(self.args),
                )

    def _should_detect_pipe_part_key_ids(self, stream) -> bool:
        return (
            getattr(stream, "manifest_type", None) == "hls"
            and bool(getattr(stream, "is_live", False))
            and _stream_uses_hls_fragmented_mp4(stream)
            and _stream_needs_external_decryption(stream, getattr(self, "hls_crypto", None))
            and len(_stream_key_ids(stream)) > 1
        )

    def _detect_pipe_part_key_ids(self, stream, part: Path, segment: SegmentInfo) -> list[str]:
        cache_key = (id(stream), str(part))
        cached = getattr(self, "detected_pipe_part_kids", {}).get(cache_key)
        if cached is not None:
            return cached
        try:
            data = part.read_bytes()
            kids = fragment_cenc_key_ids_from_bytes(data)
            if not kids and _live_pipe_part_contains_init(stream, segment, part):
                kids = mp4_tenc_default_kids_from_bytes(data)
        except OSError:
            return []
        normalized: list[str] = []
        for kid in kids or []:
            cleaned = _normalize_kid_text(kid)
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        self.detected_pipe_part_kids[cache_key] = normalized
        return normalized

    def _initial_live_pipe_key_id(self, stream) -> str | None:
        if self._should_detect_pipe_part_key_ids(stream):
            return None
        return _stream_current_media_key_id(stream)

    def _pipe_input_format(self, stream) -> str:
        if self.output_container == "mpegts" and _stream_should_remux_live_pipe_fragments(stream):
            return "mpegts"
        if self.output_container == "matroska" and _stream_uses_webm_container(stream):
            return "matroska,webm"
        sabr_audio_format = _sabr_live_pipe_audio_input_format(stream)
        if self.output_container == "matroska" and sabr_audio_format:
            return sabr_audio_format
        if self.output_container == "matroska" and should_treat_live_stream_as_fragmented_mp4(stream):
            return "nut"
        if _stream_uses_hls_fragmented_mp4(stream):
            return "mpegts"
        raw_audio_format = _live_pipe_raw_audio_input_format(stream)
        if raw_audio_format:
            return raw_audio_format
        extension = (getattr(stream, "extension", None) or "").lower().lstrip(".")
        if extension in _FRAGMENTED_MP4_EXTS:
            return "mp4"
        if extension == "ts":
            return "mpegts"
        if getattr(stream, "manifest_type", None) in {"dash", "ism"}:
            return "mp4"
        for segment in getattr(stream, "segments", []) or []:
            path = urlparse(segment.url).path.lower()
            suffix = Path(path).suffix.lstrip(".")
            if suffix in _FRAGMENTED_MP4_EXTS:
                return "mp4"
            if suffix == "ts":
                return "mpegts"
        return "mpegts"

    def _output_container(self) -> str:
        suffix = self.output_path.suffix.lower()
        if suffix == ".mkv":
            return "matroska"
        return "mpegts"

    def _decrypt_pipe_init(self, stream, part: Path) -> Path:
        self._report_decrypt_event("engine: internal fMP4 init metadata", stream)
        output = self._pipe_work_path(stream, part, "init.dec.mp4")
        return normalize_decrypted_mp4_init(part, output)

    def _decrypt_pipe_webm_fragment(self, stream, part: Path, segment=None) -> Path:
        output = self._pipe_work_path(stream, part, "frag.dec.webm")
        expected_kids = _stream_key_ids_with_segment(stream, segment, None)
        webm_key = select_webm_key(self.keys, webm_key_ids(part) or expected_kids)
        return decrypt_webm_file(part, webm_key, output, validate=False)

    def _decrypt_pipe_fragment(self, stream, part: Path, init_path: Path, decrypter: str | None = None, segment=None) -> Path:
        patch_kid = self._candidate_patch_kid(stream, segment)
        while True:
            try:
                return self._decrypt_pipe_fragment_once(stream, part, init_path, decrypter="internal", segment=segment, patch_kid=patch_kid)
            except CencFragmentKeyError as exc:
                if not self.keys:
                    raise
                prompt_kid = _normalize_kid_text(exc.kid) or patch_kid
                replacement = _prompt_for_live_key(
                    self.keys,
                    prompt_kid,
                    stream,
                    segment,
                    self.colors,
                    reason="Live fragment KID changed",
                    require_kid=False,
                    force=True,
                    replace_existing=True,
                    provider=_live_key_provider(self.args),
                )
                replacement_kid = _normalize_kid_text(replacement.kid) or prompt_kid
                if replacement_kid:
                    self.active_patch_kids[id(stream)] = replacement_kid
                patch_kid = replacement_kid
            except _LiveKeyValidationError:
                if not self.keys:
                    raise
                known_kid = _normalize_kid_text(getattr(segment, "key_id", None)) if segment is not None else None
                prompt_kid = patch_kid or known_kid
                replacement = _prompt_for_live_key(
                    self.keys,
                    prompt_kid,
                    stream,
                    segment,
                    self.colors,
                    reason="Live key validation failed",
                    require_kid=prompt_kid is None,
                    force=True,
                    replace_existing=True,
                    provider=_live_key_provider(self.args),
                )
                replacement_kid = _normalize_kid_text(replacement.kid) or prompt_kid
                if replacement_kid:
                    self.active_patch_kids[id(stream)] = replacement_kid
                patch_kid = replacement_kid

    def _decrypt_pipe_fragment_once(
        self,
        stream,
        part: Path,
        init_path: Path,
        decrypter: str | None = None,
        segment=None,
        patch_kid: str | None = None,
    ) -> Path:
        output = self._pipe_work_path(stream, part, "frag.dec.mp4")
        decrypter = "internal"
        effective_init = self._init_for_kid(stream, init_path, patch_kid)
        decrypted = decrypt_fragmented_mp4_part(
            part,
            effective_init,
            keys=self.keys,
            stream_type="audio" if stream.media_type == "audio" else "video",
            output_path=output,
            expected_kids=_stream_key_ids_with_segment(stream, segment, patch_kid),
            decrypter=decrypter,
            event_callback=lambda message: self._report_decrypt_event(message, stream),
            default_constant_iv=getattr(segment, "key_iv", None),
        )
        self._validate_decrypted_pipe_fragment(stream, decrypted, effective_init, segment, patch_kid)
        return decrypted

    def _report_decrypt_event(self, message: str, stream=None) -> None:
        cleaned = str(message).strip()
        if not cleaned:
            return
        if _is_decrypt_engine_event(cleaned):
            return
        context = _decrypt_event_context(None, stream) if stream is not None else None
        seen_key = _decrypt_event_display_value(cleaned, context)
        if not hasattr(self, "decrypt_event_lock"):
            self.decrypt_event_lock = threading.Lock()
        if not hasattr(self, "decrypt_events_seen"):
            self.decrypt_events_seen = set()
        with self.decrypt_event_lock:
            if seen_key in self.decrypt_events_seen:
                return
            self.decrypt_events_seen.add(seen_key)
        label, value = _decrypt_event_label_value(cleaned)
        value = _decrypt_event_display_value(value, context)
        if sys.stdout.isatty() or getattr(getattr(self, "args", None), "force_ansi_console", False):
            sys.stdout.write("\n")
            sys.stdout.flush()
        _print_status_line(label, value, _decrypt_event_color(label), getattr(self, "colors", None))
        if hasattr(self, "args"):
            _log_line(self.args, f"Decrypt event: {seen_key}")

    def _validate_decrypted_pipe_fragment(self, stream, part: Path, init_path: Path, segment=None, patch_kid: str | None = None) -> None:
        kid = _normalize_kid_text(patch_kid) or _normalize_kid_text(getattr(segment, "key_id", None))
        if not kid:
            return
        stream_key = id(stream)
        validation_key = (stream_key, kid)
        if validation_key in self.validated_fragment_kids:
            return
        if kid in self.initial_stream_kids.get(stream_key, set()):
            return
        if getattr(stream, "media_type", None) != "video":
            return
        executable = shutil.which("ffmpeg")
        if not executable:
            return
        joined = self._pipe_work_path(stream, part, f"{kid}.validate.mp4")
        with joined.open("wb") as target:
            _copy_file_to_output(init_path, target)
            _copy_file_to_output(part, target)
        args = [
            executable,
            "-hide_banner",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(joined),
            "-frames:v",
            "3",
            "-f",
            "null",
            "-",
        ]
        result = managed_run(args, capture_output=True)
        try:
            joined.unlink()
        except OSError:
            pass
        if result.returncode:
            detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise _LiveKeyValidationError(f"decrypted live video fragment failed validation for KID {kid}: {detail}")
        self.validated_fragment_kids.add(validation_key)

    def _candidate_patch_kid(self, stream, segment=None) -> str | None:
        segment_kid = _normalize_kid_text(getattr(segment, "key_id", None)) if segment is not None else None
        if segment_kid:
            return segment_kid
        return self.active_patch_kids.get(id(stream))

    def _init_for_kid(self, stream, init_path: Path, kid: str | None) -> Path:
        kid = _normalize_kid_text(kid)
        if not kid:
            return init_path
        current_kids = mp4_tenc_default_kids(init_path)
        if not current_kids or kid in current_kids:
            return init_path
        key = (id(stream), kid)
        cached = self.patched_inits.get(key)
        if cached and cached.exists():
            return cached
        patched = patch_mp4_tenc_default_kid(init_path, kid, self._pipe_work_path(stream, init_path, f"{kid}.init.mp4"))
        self.patched_inits[key] = patched
        return patched

    def _extract_pipe_self_contained_mp4_init(self, stream, part: Path) -> Path | None:
        init_path = self._pipe_work_path(stream, part, "init.mp4")
        media_path = self._pipe_work_path(stream, part, "media.mp4")
        extracted, _media = split_fragmented_mp4_init_media(part, init_path, media_path)
        try:
            media_path.unlink(missing_ok=True)
        except OSError:
            pass
        if extracted is None:
            return None
        stream_key = id(stream)
        self.current_init[stream_key] = extracted
        self.current_decrypted_init.pop(stream_key, None)
        self.patched_inits = {
            key: value for key, value in self.patched_inits.items() if key[0] != stream_key
        }
        return extracted

    def _restamp_pipe_fragment(self, stream, part: Path, segment=None) -> Path:
        output = self._pipe_work_path(stream, part, "frag.time.mp4")
        key = id(stream)
        with self.lock:
            source_time, source_timescale, source_duration = fragmented_mp4_timing(part)
            target_time = self.next_decode_times.get(key)
            preserve_source_time = False
            if source_time is not None:
                previous_source_time = self.source_decode_times.get(key)
                previous_timescale = self.source_timescales.get(key)
                expected_seconds = _pipe_expected_fragment_seconds(stream, segment, source_duration, source_timescale)
                if previous_source_time is None:
                    target_time = source_time
                    preserve_source_time = True
                else:
                    source_delta = _pipe_source_delta_seconds(
                        source_time,
                        previous_source_time,
                        source_timescale,
                        previous_timescale,
                    )
                    if _pipe_should_preserve_source_time(source_delta, expected_seconds):
                        target_time = source_time
                        preserve_source_time = True
            restamped, next_decode_time = restamp_fragmented_mp4_timestamps(
                part,
                next_decode_time=target_time,
                output_path=output,
                min_duration_seconds=None if preserve_source_time else _pipe_min_fragment_duration(stream, segment),
            )
            if source_time is not None:
                self.source_decode_times[key] = source_time
            if source_timescale:
                self.source_timescales[key] = source_timescale
            self.next_decode_times[key] = next_decode_time
        return restamped

    def _remux_pipe_fragment_to_ts(self, stream, part: Path, init_path: Path | None = None) -> Path:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpeg not found; live pipe mux needs ffmpeg.")
        output = self._pipe_work_path(stream, part, "pipe.ts")
        input_path = part
        if init_path is not None:
            input_path = self._pipe_work_path(stream, part, "with-init.mp4")
            with input_path.open("wb") as target:
                _copy_file_to_output(init_path, target)
                _copy_file_to_output(part, target)
        map_args: list[str] = []
        for spec in _live_pipe_map_specs(stream, 0):
            map_args.extend(["-map", spec])
        args = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+genpts",
            "-copyts",
            "-i",
            str(input_path),
            *map_args,
            "-c",
            "copy",
            "-avoid_negative_ts",
            "disabled",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            "-mpegts_flags",
            "+resend_headers",
            str(output),
        ]
        result = managed_run(args, capture_output=True)
        if result.returncode:
            detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"live pipe fragment remux failed with status {result.returncode}: {detail}")
        return output

    def _remux_pipe_fragment_to_nut(self, stream, part: Path, init_path: Path | None = None) -> Path:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpeg not found; live pipe mux needs ffmpeg.")
        output = self._pipe_work_path(stream, part, "pipe.nut")
        input_path = part
        if init_path is not None:
            input_path = self._pipe_work_path(stream, part, "with-init.mp4")
            with input_path.open("wb") as target:
                _copy_file_to_output(init_path, target)
                _copy_file_to_output(part, target)
        map_args: list[str] = []
        for spec in _live_pipe_map_specs(stream, 0):
            map_args.extend(["-map", spec])
        args = [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(input_path),
            *map_args,
            "-c",
            "copy",
            "-f",
            "nut",
            str(output),
        ]
        result = managed_run(args, capture_output=True)
        if result.returncode:
            detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"live pipe fragment NUT remux failed with status {result.returncode}: {detail}")
        return output

    def _pipe_work_path(self, stream, part: Path, suffix: str) -> Path:
        with self.lock:
            self.counter += 1
            index = self.counter
        media_type = getattr(stream, "media_type", "media") or "media"
        path = self.pipe_dir / f"{index:08d}_{media_type}_{part.stem}.{suffix}"
        return path


def _record_live_streams(live_streams, all_streams, args, headers, keys, output, output_dir, pipe_session=None, hls_crypto=None) -> list[_DownloadedTrack]:
    colors = _colors(args)
    limit_seconds = parse_live_limit(args.live_record_limit)
    dvr_start_offset = _live_dvr_start_offset(args)
    dvr_end_offset = _live_dvr_end_offset(args)
    _validate_live_dvr_range(live_streams, dvr_start_offset, dvr_end_offset)
    if not args.no_decrypt:
        _ensure_hls_segment_crypto_ready(live_streams, hls_crypto)
    panel_limit_seconds = _effective_live_record_limit(limit_seconds, dvr_start_offset, dvr_end_offset)
    panel = _LiveRecordProgress(live_streams, colors=colors, force=getattr(args, "force_ansi_console", False), limit_seconds=panel_limit_seconds)
    if not _embedding_console_progress(args):
        panel.enabled = False
    progress_display = panel.progress_for_stream if panel.enabled else (_silent_live_progress if len(live_streams) > 1 else None)
    progress_callback = _embedding_live_progress(args, progress_display)
    options = LiveRecordOptions(
        limit_seconds=limit_seconds,
        wait_seconds=args.live_wait_time,
        take_count=_live_record_take_count(args, pipe_session),
        keep_segments=args.live_keep_segments,
        real_time_merge=args.live_real_time_merge,
        workers=args.workers,
        retries=args.retries,
        request_timeout=max(1, getattr(args, "http_request_timeout", 30)),
        check_segments_count=getattr(args, "check_segments_count", True),
        temp_dir=_task_temp_subdir(args, "live"),
        dvr_from_start=getattr(args, "live_dvr_from_start", False),
        dvr_start_offset=dvr_start_offset,
        dvr_end_offset=dvr_end_offset,
        colors=colors,
        progress=progress_callback,
        cancel_requested=_embedding_cancel_callback(args),
        pause_requested=_embedding_pause_callback(args),
        segment_sink=pipe_session.write_batch if pipe_session else None,
        group_segment_sink=pipe_session.write_group if pipe_session else None,
        segment_key_observer=(
            None
            if args.no_decrypt
            else lambda stream, segments: _ensure_live_segment_keys(
                keys,
                stream,
                segments,
                colors,
                provider=_live_key_provider(args),
            )
        ),
        stream_transform=_live_stream_transform(args),
        hls_crypto=hls_crypto,
        stop_on_interrupt=_live_stop_on_interrupt_enabled(pipe_session, args, live_streams),
        assemble_output=_live_assemble_output_selector(pipe_session, args),
    )
    _print_live_recording_summary(live_streams, args, pipe_session, colors=colors, show_progress=panel.enabled)
    results = []
    previous_progress_panel = _set_active_live_progress_panel(panel if panel.enabled else None)
    try:
        track_name_total = 2 if pipe_session and len(live_streams) == 1 else len(live_streams)
        names = [_track_save_name(args.save_name, args.save_pattern, stream, offset, track_name_total) for offset, stream in enumerate(live_streams, start=1)]
        if len(live_streams) > 1:
            group_results = record_live_stream_group(
                live_streams,
                output_dir,
                names,
                headers,
                options,
            )
            results = list(enumerate(group_results, start=1))
        else:
            for offset, stream in enumerate(live_streams, start=1):
                name = names[offset - 1]
                results.append((offset, record_live_stream(stream, output_dir, name, headers, options)))
    finally:
        panel.close()
        _set_active_live_progress_panel(previous_progress_panel)

    tracks: list[_DownloadedTrack] = []
    for offset, result in sorted(results, key=lambda item: item[0]):
        current_path = result.path
        track_file_skipped = _live_pipe_track_file_skipped(pipe_session, result.stream, args) and not current_path.exists()
        if _is_sabr_stream(result.stream):
            _apply_sniffed_sabr_container_from_path(result.stream, current_path)
        original_index = _stream_original_index(all_streams, result.stream, offset)
        decrypt_context = _decrypt_event_context(original_index, result.stream)
        status = _TrackStatusReporter(
            original_index,
            result.stream,
            _embedding_message_emitter(args) or print,
            colors,
            _embedding_message_emitter(args, transient=True),
            force=getattr(args, "force_ansi_console", False),
        )
        status.set_path(pipe_session.output_path if track_file_skipped and pipe_session else current_path)
        status.update(*(("Recorded ✓", "Piped ✓") if track_file_skipped else ("Recorded ✓",)))
        if track_file_skipped and pipe_session:
            _log_line(args, f"Recorded: {result.stream.media_type or 'track'} piped to {pipe_session.output_path} ({result.segments_count} segments, {int(result.recorded_seconds)}s)")
        else:
            _log_line(args, f"Recorded: {current_path} ({result.segments_count} segments, {int(result.recorded_seconds)}s)")
        cleanup_paths = [current_path]
        if pipe_session and pipe_session.has_stream(result.stream):
            pass
        elif _stream_uses_bbts(result.stream) and not args.no_decrypt:
            with status.spinning("Recorded ✓", "Decrypting {spinner}"):
                current_path = decrypt_bbts_file(
                    current_path,
                    current_path.with_suffix(".dec.ts"),
                    _bbts_key_hex(keys, hls_crypto, result.stream),
                )
            cleanup_paths.append(current_path)
            status.update("Recorded ✓", "Decrypted ✓")
            _log_line(args, f"Decrypted: {current_path}")
        elif _stream_uses_webm_container(result.stream) and _stream_needs_external_decryption(result.stream, hls_crypto) and not args.no_decrypt:
            decrypt_events, decrypt_event = _new_decrypt_event_recorder(args, decrypt_context)
            try:
                with status.spinning("Recorded ✓", "Decrypting {spinner}"):
                    current_path = _decrypt_webm_output(
                        current_path,
                        result.stream,
                        result,
                        keys,
                        args.decrypter,
                        current_path.with_suffix(".dec.webm"),
                        event_callback=decrypt_event,
                    )
            except Exception:
                _flush_track_decrypt_events(status, decrypt_events, print, colors, decrypt_context)
                raise
            _flush_track_decrypt_events(status, decrypt_events, print, colors, decrypt_context)
            cleanup_paths.append(current_path)
            status.update("Recorded ✓", "Decrypted ✓")
            _log_line(args, f"Decrypted: {current_path}")
        elif _stream_needs_external_decryption(result.stream, hls_crypto) and not args.no_decrypt:
            decrypt_events, decrypt_event = _new_decrypt_event_recorder(args, decrypt_context)
            try:
                with status.spinning("Recorded ✓", "Decrypting {spinner}"):
                    stream_type = "audio" if result.stream.media_type == "audio" else "video"
                    if _live_result_needs_fragment_decryption(result):
                        current_path = decrypt_fragmented_mp4_parts(
                            result.parts or [],
                            result.segments or [],
                            keys=keys,
                            stream_type=stream_type,
                            output_path=current_path.with_suffix(f".dec{current_path.suffix}"),
                            expected_kids=_stream_key_ids(result.stream),
                            temp_dir=_task_temp_subdir(args, "postprocess"),
                            event_callback=decrypt_event,
                            restamp_timestamps=_stream_uses_json_dvr_sequence_fragments(result.stream),
                        )
                    else:
                        current_path = decrypt_file(
                            current_path,
                            keys=keys,
                            decrypter=_decrypter_for_stream(args.decrypter, result.stream),
                            stream_type=stream_type,
                            expected_kids=_stream_key_ids(result.stream),
                            event_callback=decrypt_event,
                        )
            except Exception:
                _flush_track_decrypt_events(status, decrypt_events, print, colors, decrypt_context)
                raise
            _flush_track_decrypt_events(status, decrypt_events, print, colors, decrypt_context)
            cleanup_paths.append(current_path)
            status.update("Recorded ✓", "Decrypted ✓")
            _log_line(args, f"Decrypted: {current_path}")
        if is_vgc_stream(result.stream):
            try:
                with status.spinning("Recorded ✓", "Checking VGC {spinner}"):
                    current_path = finalize_vgc_track(current_path, result.stream, allow_opaque=getattr(args, "vgc_keep_opaque", False))
            except VgcError as exc:
                raise RuntimeError(str(exc)) from exc
            if getattr(args, "vgc_keep_opaque", False):
                status.update("Recorded ✓", "VGC raw kept")
                _log_line(args, f"VGC raw kept: {current_path}")
            else:
                status.update("Recorded ✓", "VGC clear ✓")
                _log_line(args, f"VGC clear: {current_path}")
        if args.repack:
            with status.spinning("Recorded ✓", "Decrypted ✓", "Repacking {spinner}"):
                current_path = repackage_ffmpeg(current_path)
            cleanup_paths.append(current_path)
            status.update("Recorded ✓", "Decrypted ✓", "Repacked ✓")
            _log_line(args, f"Repacked: {current_path}")
        audio_result = postprocess_audio_vivid(
            current_path,
            result.stream,
            decoder=getattr(args, "audio_vivid_decoder", None),
            decoder_args=getattr(args, "audio_vivid_decoder_args", None),
        )
        if audio_result.warning:
            print(f"{paint('Audio:', Palette.yellow, colors)} {audio_result.warning}")
            _log_line(args, f"Audio: {audio_result.warning}")
        elif audio_result.decoded_path is not None:
            current_path = audio_result.path
            if current_path not in cleanup_paths:
                cleanup_paths.append(current_path)
            status.set_path(current_path)
            status.update("Recorded ✓", "Audio Vivid WAV ✓")
            _log_line(args, f"Audio Vivid WAV: {audio_result.decoded_path}")
        audio_format = _audio_format_for_stream(result.stream, args)
        if audio_format:
            audio_label = audio_format.upper()
            with status.spinning("Recorded ✓", f"Finalizing {audio_label} {{spinner}}"):
                current_path = _transcode_audio_output(current_path, result.path, result.stream, args, headers)
            if current_path not in cleanup_paths:
                cleanup_paths.append(current_path)
            status.set_path(current_path)
            status.update("Recorded ✓", f"{audio_label} ✓")
            _log_line(args, f"{audio_label}: {current_path}")
        current_path, subtitle_cleanup = _convert_subtitle_output(current_path, result.stream, args, colors, print, status)
        cleanup_paths.extend(path for path in subtitle_cleanup if path not in cleanup_paths)
        status.finish()
        if not current_path.exists() and _live_track_output_file_required(pipe_session, result.stream, args):
            raise RuntimeError(f"Recorded output is missing before mux: {current_path}")
        tracks.append(_DownloadedTrack(offset=offset, stream=result.stream, path=current_path, temp_dir=result.temp_dir, cleanup_paths=cleanup_paths))
    return tracks


def _live_record_take_count(args, pipe_session=None) -> int:
    take_count = max(1, int(getattr(args, "live_take_count", 16) or 16))
    if not pipe_session:
        return take_count
    if (
        getattr(args, "live_dvr_from_start", False)
        or getattr(args, "live_dvr_start_at", None)
        or getattr(args, "live_dvr_end_at", None)
    ):
        return take_count
    return min(take_count, 4)


def _live_stop_on_interrupt_enabled(pipe_session, args, streams=None) -> bool:
    selected = list(streams or [])
    has_subtitles = any(_is_subtitle_stream(stream) for stream in selected)
    has_audio = any(getattr(stream, "media_type", None) == "audio" for stream in selected)
    has_video = any(getattr(stream, "media_type", None) == "video" for stream in selected)
    return bool(
        has_subtitles
        or (has_audio and not has_video)
        or bool(getattr(args, "audio_format", None))
        or getattr(args, "vgc", False)
        or _live_pipe_stop_on_interrupt_enabled(pipe_session)
    )


def _live_assemble_output_selector(pipe_session, args):
    if not pipe_session:
        return True
    if getattr(args, "live_keep_track_files", False):
        return True
    return lambda stream: not pipe_session.has_stream(stream)


def _live_stream_transform(args):
    if not getattr(args, "append_url_params", False) and not getattr(args, "vgc", False):
        return None

    def transform(refreshed):
        if getattr(args, "append_url_params", False):
            _append_input_params(refreshed, args.input)
        if getattr(args, "vgc", False):
            prepare_vgc_streams(refreshed)

    return transform


def _live_pipe_track_file_skipped(pipe_session, stream, args) -> bool:
    return bool(pipe_session and pipe_session.has_stream(stream) and not getattr(args, "live_keep_track_files", False))


def _live_track_output_file_required(pipe_session, stream, args) -> bool:
    return not _live_pipe_track_file_skipped(pipe_session, stream, args)


def _silent_live_progress(update: LiveProgressUpdate) -> None:
    return None


def _live_dvr_hint(streams, args) -> str | None:
    window = _live_dvr_window_seconds(streams)
    if not window:
        return None
    edge_window = _live_edge_take_seconds(streams, getattr(args, "live_take_count", 16))
    if edge_window and window <= edge_window * 1.25:
        return None
    duration = _format_live_dvr_duration(window)
    start_offset = _live_dvr_start_offset(args)
    end_offset = _live_dvr_end_offset(args)
    if start_offset is not None:
        if end_offset is not None:
            selected = max(0.0, end_offset - start_offset)
            return (
                f"recording DVR range {_format_live_dvr_duration(start_offset)}-"
                f"{_format_live_dvr_duration(end_offset)} ({_format_live_dvr_duration(selected)} of {duration})."
            )
        remaining = max(0.0, window - start_offset)
        return f"recording from {_format_live_dvr_duration(start_offset)} into the current DVR window ({_format_live_dvr_duration(remaining)} remaining of {duration})."
    if getattr(args, "live_dvr_from_start", False):
        return f"recording from the current DVR window beginning ({duration} available)."
    return f"current manifest exposes about {duration} of replay window; use --live-dvr-from-start true or --live-dvr-start-at HH:mm:ss."


def _live_dvr_start_offset(args) -> float | None:
    value = getattr(args, "live_dvr_start_at", None)
    if value is None:
        return None
    try:
        offset = parse_live_limit(value)
    except Exception as exc:
        raise ValueError(f"Invalid --live-dvr-start-at value: {value}") from exc
    if offset is None:
        return None
    if offset < 0:
        raise ValueError("--live-dvr-start-at must be zero or greater.")
    return offset


def _live_dvr_end_offset(args) -> float | None:
    value = getattr(args, "live_dvr_end_at", None)
    if value is None:
        return None
    try:
        offset = parse_live_limit(value)
    except Exception as exc:
        raise ValueError(f"Invalid --live-dvr-end-at value: {value}") from exc
    if offset is None:
        return None
    if offset < 0:
        raise ValueError("--live-dvr-end-at must be zero or greater.")
    return offset


def _validate_live_dvr_range(streams, start_offset: float | None, end_offset: float | None) -> None:
    if end_offset is not None and start_offset is None:
        raise ValueError("--live-dvr-end-at requires --live-dvr-start-at.")
    if start_offset is None:
        return
    if end_offset is not None and end_offset <= start_offset:
        raise ValueError("--live-dvr-end-at must be greater than --live-dvr-start-at.")
    window = _live_dvr_window_seconds(streams)
    if window and start_offset >= window:
        raise ValueError(
            f"--live-dvr-start-at {_format_live_dvr_duration(start_offset)} is outside the current DVR window "
            f"({_format_live_dvr_duration(window)} available)."
        )
    if window and end_offset is not None and end_offset > window:
        raise ValueError(
            f"--live-dvr-end-at {_format_live_dvr_duration(end_offset)} is outside the current DVR window "
            f"({_format_live_dvr_duration(window)} available)."
        )


def _validate_live_dvr_start_offset(streams, start_offset: float | None) -> None:
    _validate_live_dvr_range(streams, start_offset, None)


def _effective_live_record_limit(limit_seconds: float | None, start_offset: float | None, end_offset: float | None) -> float | None:
    if start_offset is None or end_offset is None:
        return limit_seconds
    dvr_limit = max(0.0, end_offset - start_offset)
    if limit_seconds is None:
        return dvr_limit
    return min(limit_seconds, dvr_limit)


def _live_dvr_window_seconds(streams) -> float | None:
    candidates = [stream for stream in streams if not _is_subtitle_stream(stream)] or list(streams)
    values = [_live_stream_window_seconds(stream) for stream in candidates]
    values = [value for value in values if value and value > 0]
    if not values:
        return None
    return min(values)


def _live_stream_window_seconds(stream) -> float | None:
    extra = getattr(stream, "extra", {}) or {}
    for key in ("time_shift_buffer_depth", "dvr_window_seconds"):
        value = extra.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    media_duration = sum((segment.duration or 0) for segment in getattr(stream, "segments", []) or [] if getattr(segment, "index", None) != -1)
    if media_duration > 0:
        return media_duration
    duration = getattr(stream, "total_duration", None)
    if duration:
        return float(duration)
    return None


def _live_edge_take_seconds(streams, take_count: int | None) -> float | None:
    take_count = take_count or 16
    durations: list[float] = []
    for stream in streams:
        media_segments = [segment for segment in getattr(stream, "segments", []) or [] if getattr(segment, "index", None) != -1 and segment.duration]
        if media_segments:
            sample = media_segments[-min(len(media_segments), max(1, take_count)) :]
            durations.append(sum(segment.duration or 0 for segment in sample))
            continue
        target_duration = (getattr(stream, "extra", {}) or {}).get("target_duration")
        if isinstance(target_duration, (int, float)) and target_duration > 0:
            durations.append(float(target_duration) * take_count)
    if not durations:
        return None
    return max(durations)


def _format_live_dvr_duration(seconds: float) -> str:
    seconds_int = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _live_result_needs_fragment_decryption(result) -> bool:
    if not result.parts or not result.segments:
        return False
    return any(segment.index == -1 for segment in result.segments)


def _validate_audio_format_request(streams, args: argparse.Namespace) -> None:
    output_format = (getattr(args, "audio_format", None) or "").lower()
    if getattr(args, "audio_metadata_file", None) and not output_format:
        raise ValueError("--audio-metadata-file requires --audio-format.")
    if not output_format:
        return
    audio_streams = [stream for stream in streams if getattr(stream, "media_type", None) == "audio"]
    if not audio_streams:
        raise ValueError(f"--audio-format {output_format} needs at least one selected audio track.")
    live_audio = any(getattr(stream, "is_live", False) for stream in audio_streams) and not getattr(args, "live_perform_as_vod", False)
    if live_audio and any(getattr(stream, "media_type", None) == "video" for stream in streams):
        raise ValueError(f"--audio-format {output_format} supports audio-only live selections; drop the live video track.")
    if live_audio and getattr(args, "live_pipe_mux", False):
        raise ValueError(f"--audio-format {output_format} live recording cannot be combined with --live-pipe-mux.")
    if getattr(args, "no_decrypt", False) and any(getattr(stream, "encrypted", False) for stream in audio_streams):
        raise ValueError(f"--audio-format {output_format} cannot transcode encrypted audio with --no-decrypt.")
    if getattr(args, "no_decrypt", False) and any(
        (_deezer_transport_context(stream) or {}).get("cipher") == BF_CBC_STRIPE
        for stream in audio_streams
    ):
        raise ValueError(f"--audio-format {output_format} cannot transcode Deezer stripe audio with --no-decrypt.")


def _apply_audio_metadata_file(streams, input_path: str | Path) -> dict:
    metadata = load_audio_metadata_file(input_path)
    for stream in streams:
        if getattr(stream, "media_type", None) != "audio":
            continue
        extra = getattr(stream, "extra", None)
        if not isinstance(extra, dict):
            extra = {}
            stream.extra = extra
        extra["audio_metadata"] = copy.deepcopy(metadata)
    return metadata


def _audio_format_for_stream(stream, args: argparse.Namespace) -> str | None:
    if getattr(stream, "media_type", None) != "audio":
        return None
    return (getattr(args, "audio_format", None) or "").lower() or None


def _stream_is_mp3_audio(stream) -> bool:
    if getattr(stream, "media_type", None) != "audio":
        return False
    codec = str(getattr(stream, "codecs", None) or "").strip().lower()
    extension = str(getattr(stream, "extension", None) or "").strip().lower().lstrip(".")
    return pretty_codec(codec, "audio") == "MP3" or codec.startswith(("mp3", "mpa")) or extension == "mp3"


def _stream_is_alac_audio(stream) -> bool:
    if getattr(stream, "media_type", None) != "audio":
        return False
    return "alac" in _stream_audio_codec_hint(stream)


def _stream_is_flac_audio(stream) -> bool:
    if getattr(stream, "media_type", None) != "audio":
        return False
    return "flac" in _stream_audio_codec_hint(stream)


def _stream_audio_codec_hint(stream) -> str:
    codec = str(getattr(stream, "codecs", None) or "").strip().lower()
    if codec:
        return codec
    hints = " ".join(
        str(getattr(stream, field, "") or "").lower()
        for field in ("name", "id", "group_id", "url", "original_url")
    )
    return hints


def _audio_source_download_name(name: str | None, stream, args: argparse.Namespace) -> str | None:
    if not name or not _audio_format_for_stream(stream, args) or _stream_is_mp3_audio(stream):
        return name
    path = Path(name)
    if path.suffix.lower() == ".mp3":
        return str(path.with_suffix(""))
    return name


def _transcode_audio_output(
    current_path: Path,
    source_path: Path,
    stream,
    args: argparse.Namespace,
    headers: dict[str, str] | None,
) -> Path:
    output_format = _audio_format_for_stream(stream, args)
    if not output_format:
        return current_path
    output_suffix = {"mp3": ".mp3", "flac": ".flac", "alac": ".m4a", "m4a": ".m4a"}.get(
        output_format,
        f".{output_format}",
    )
    desired_path = Path(source_path).with_suffix(output_suffix)
    replace_source = desired_path.exists() and desired_path.resolve() == current_path.resolve()
    if replace_source:
        stage_dir = _task_temp_subdir(args, "postprocess")
        output_path = unique_path(stage_dir / f"{desired_path.stem}.tagged{output_suffix}")
    else:
        output_path = unique_path(desired_path)
    converted = transcode_audio(
        current_path,
        output_format=output_format,
        output_path=output_path,
        metadata=audio_id3_metadata(stream),
        cover_path=_audio_cover_path(stream, args, headers),
        copy_audio=(
            output_format == "mp3" and _stream_is_mp3_audio(stream)
        ) or (
            output_format == "alac" and _stream_is_alac_audio(stream)
        ) or (
            output_format == "flac" and _stream_is_flac_audio(stream)
        ) or output_format == "m4a",
    )
    if replace_source:
        os.replace(converted, desired_path)
        return desired_path
    return converted


def _audio_cover_path(stream, args: argparse.Namespace, headers: dict[str, str] | None) -> Path | None:
    return prepare_audio_cover(
        stream,
        _task_temp_subdir(args, "postprocess") / "covers",
        headers=headers,
        timeout=max(1, getattr(args, "http_request_timeout", 30)),
        retries=max(1, getattr(args, "retries", 3)),
    )


def _convert_subtitle_output(
    current_path: Path,
    stream,
    args: argparse.Namespace,
    colors: bool | None,
    emit: Callable[[str], None],
    status: _TrackStatusReporter | None = None,
) -> tuple[Path, list[Path]]:
    if not _is_subtitle_stream(stream):
        return current_path, []
    sub_format = (getattr(args, "sub_format", "srt") or "srt").lower()
    if sub_format == "raw":
        return current_path, []
    try:
        if status is not None:
            with status.spinning("Downloaded ✓", "Converting {spinner}"):
                converted = convert_subtitle_file(
                    current_path,
                    output_format=sub_format,
                    auto_fix=getattr(args, "auto_subtitle_fix", True),
                    duration=getattr(stream, "duration", None),
                )
        else:
            converted = convert_subtitle_file(
                current_path,
                output_format=sub_format,
                auto_fix=getattr(args, "auto_subtitle_fix", True),
                duration=getattr(stream, "duration", None),
            )
    except SubtitleConversionError as exc:
        if status is not None:
            status.update("Downloaded ✓", "Conversion warning")
        emit(f"{paint('Subtitle conversion warning:', Palette.yellow, colors)} {_short_error(exc)}")
        _log_line(args, f"Subtitle conversion warning: {_short_error(exc)}")
        return current_path, []
    if converted == current_path:
        if status is not None:
            status.update("Downloaded ✓")
        return current_path, []
    try:
        converted_is_empty = converted.stat().st_size == 0
    except OSError:
        converted_is_empty = False
    if converted_is_empty:
        if status is not None:
            status.update("Downloaded ✓", "No subtitle cues")
        else:
            _emit_status_line(emit, "Subtitle empty:", converted, Palette.yellow, colors)
        _log_line(args, f"Subtitle has no cues: {converted}")
        return converted, [current_path]
    if status is not None:
        status.update("Downloaded ✓", "Converted ✓")
    else:
        _emit_status_line(emit, "Subtitle converted:", converted, Palette.green, colors)
    _log_line(args, f"Subtitle converted: {converted}")
    return converted, [current_path]


def _concurrent_track_workers(workers: int, track_count: int) -> int:
    return max(1, int(workers or 1))


class _LiveRecordProgress:
    def __init__(self, streams, colors: bool | None = None, force: bool = False, limit_seconds: float | None = None):
        self.streams = []
        seen_streams: set[int] = set()
        for stream in streams:
            stream_id = id(stream)
            if stream_id in seen_streams:
                continue
            seen_streams.add(stream_id)
            self.streams.append(stream)
        self.colors = colors
        self.enabled = force or sys.stdout.isatty()
        self.limit_seconds = limit_seconds
        self.lock = threading.Lock()
        self.updates: dict[int, LiveProgressUpdate] = {}
        self.rendered_lines = 0
        self.started = False
        self.closed = False
        self.suspended = False
        self.last_render = 0.0
        self.speed_samples: dict[int, tuple[int, float, float]] = {}

    def progress_for_stream(self, update: LiveProgressUpdate) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self.lock:
            if self.closed:
                return
            update = self._with_speed(update, now)
            self.updates[id(update.stream)] = update
            if self.suspended:
                return
            if not update.done and update.new_segments == 0 and now - self.last_render < 0.25:
                return
            self.last_render = now
            self._render_locked()

    def _with_speed(self, update: LiveProgressUpdate, now: float) -> LiveProgressUpdate:
        key = id(update.stream)
        downloaded = max(0, int(update.downloaded_bytes or 0))
        previous = self.speed_samples.get(key)
        speed = 0.0
        sample_time = now
        if previous is not None:
            previous_bytes, previous_time, previous_speed = previous
            delta_bytes = max(0, downloaded - previous_bytes)
            delta_time = max(0.001, now - previous_time)
            sample_time = previous_time
            if delta_bytes > 0:
                instant = delta_bytes / delta_time
                speed = instant if previous_speed <= 0 else previous_speed * 0.45 + instant * 0.55
                sample_time = now
            elif previous_speed > 0 and now - previous_time < 2.0 and not update.done:
                speed = previous_speed
        self.speed_samples[key] = (max(downloaded, previous[0] if previous else 0), sample_time, speed)
        return replace(update, downloaded_bytes=downloaded, speed_bytes_per_second=speed)

    def close(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            self.closed = True
            if self.started:
                sys.stdout.write("\n")
                sys.stdout.flush()

    def suspend(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            if self.closed or self.suspended:
                return
            _clear_terminal_block(self.rendered_lines)
            self.rendered_lines = 0
            self.started = False
            self.suspended = True

    def resume(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            if self.closed or not self.suspended:
                return
            self.suspended = False
            if self.updates:
                self._render_locked()

    def _render_locked(self) -> None:
        width = max(32, _terminal_size((120, 30)).columns)
        rows = []
        for stream in self.streams:
            update = self.updates.get(id(stream))
            if update is None:
                media_segments = [segment for segment in stream.segments if segment.index != -1]
                update = LiveProgressUpdate(
                    stream=stream,
                    recorded_seconds=0,
                    limit_seconds=self.limit_seconds,
                    segments_count=0,
                    new_segments=0,
                    path=Path(""),
                    available_seconds=sum(segment.duration or 0 for segment in media_segments),
                    available_segments=len(media_segments),
                    status="Waiting",
                )
            rows.append(update)
        title_width = _live_progress_title_width(rows, width)
        lines = [_live_progress_line(update, self.colors, width, title_width) for update in rows]
        if not self.started:
            self.started = True
        self.rendered_lines = _rewrite_terminal_block(lines, self.rendered_lines)


def _live_progress_title_width(updates: list[LiveProgressUpdate], width: int) -> int:
    safe_width = max(32, width - 1)
    titles = [_live_progress_title(update.stream) for update in updates]
    tails = [_live_progress_tail(update) for update in updates]
    longest_title = max([len(title) for title in titles], default=12)
    longest_tail = max([len(tail) for tail in tails], default=26)
    preferred = min(longest_title, 36 if safe_width >= 100 else 26)
    return max(8, min(preferred, safe_width - longest_tail - 2))


def _live_progress_line(update: LiveProgressUpdate, colors: bool | None, width: int, title_width: int) -> str:
    safe_width = max(32, width - 1)
    title = _ellipsize(_live_progress_title(update.stream), title_width).ljust(title_width)
    tail = _ellipsize(_live_progress_tail(update), max(8, safe_width - title_width - 1))
    tail = _color_live_status(tail, update, colors)
    color = Palette.cyan
    if update.stream.media_type == "audio":
        color = Palette.green
    elif _is_subtitle_stream(update.stream):
        color = Palette.magenta
    return f"{paint(title, color, colors)} {tail}"


def _live_progress_tail(update: LiveProgressUpdate) -> str:
    if update.limit_seconds is not None:
        total = update.limit_seconds
        average = _live_average_segment_seconds(update)
        if average:
            total_segments = max(update.segments_count, int(math.ceil(update.limit_seconds / average)))
        else:
            total_segments = max(update.available_segments or 0, update.segments_count)
        percent = min(100.0, update.recorded_seconds / update.limit_seconds * 100) if update.limit_seconds else 0.0
    else:
        total = max(update.recorded_seconds, update.available_seconds or 0)
        total_segments = max(update.segments_count, update.available_segments or 0)
        percent = min(100.0, update.recorded_seconds / total * 100) if total else 0.0
    status = _live_status_label(update)
    speed = max(0.0, float(update.speed_bytes_per_second or 0.0))
    speed_text = f"{format_size(int(speed)) or '0B'}/s"
    return (
        f"{_format_live_clock(update.recorded_seconds)}/{_format_live_clock(total)} "
        f"{update.segments_count}/{total_segments} "
        f"{status:<9} {percent:3.0f}% {speed_text:>10}"
    )


def _live_status_label(update: LiveProgressUpdate) -> str:
    return "Done" if update.done else update.status


def _color_live_status(tail: str, update: LiveProgressUpdate, colors: bool | None) -> str:
    status = _live_status_label(update)
    padded = f"{status:<9}"
    if padded not in tail:
        return tail
    color = _live_status_color(status)
    return tail.replace(padded, paint(status, color, colors) + " " * max(0, 9 - len(status)), 1)


def _live_status_color(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "recording":
        return Palette.cyan
    if normalized in {"adbreak", "ad", "ad-break"}:
        return Palette.yellow
    if normalized in {"done", "waiting"}:
        return Palette.green if normalized == "done" else Palette.yellow
    if normalized in {"error", "failed"}:
        return Palette.red
    if normalized in {"stopped", "cancelled", "canceled"}:
        return Palette.yellow
    return Palette.muted


def _live_average_segment_seconds(update: LiveProgressUpdate) -> float | None:
    if update.segments_count and update.recorded_seconds:
        return update.recorded_seconds / update.segments_count
    if update.available_segments and update.available_seconds:
        return update.available_seconds / update.available_segments
    durations = [segment.duration or 0 for segment in update.stream.segments if segment.index != -1]
    durations = [duration for duration in durations if duration > 0]
    if durations:
        return sum(durations) / len(durations)
    return None


def _live_progress_title(stream) -> str:
    prefix = stream.display_prefix()
    if stream.media_type == "video":
        body = compact_join([stream.resolution, format_bitrate(stream.bandwidth), format_frame_rate(stream.frame_rate)])
    elif stream.media_type == "audio":
        body = compact_join([format_bitrate(stream.bandwidth), stream.language, _live_role_label(stream)])
    elif _is_subtitle_stream(stream):
        body = compact_join([stream.language, _live_subtitle_codec(stream), _live_role_label(stream) or "Subtitle"])
    else:
        body = compact_join([stream.language, stream.name or stream.group_id or stream.id])
    return f"{prefix} {body}".strip() if body else stream.format_line()


def _live_subtitle_codec(stream) -> str | None:
    raw = (stream.codecs or "").split(",", 1)[0].strip()
    if raw:
        lowered = raw.lower()
        if lowered.startswith("stpp"):
            return "stpp"
        if lowered.startswith(("wvtt", "vtt", "webvtt")):
            return "wvtt"
    return pretty_codec(stream.codecs, stream.media_type)


def _live_role_label(stream) -> str | None:
    role = stream.role or stream.name
    if not role:
        return None
    text = str(role).replace("_", " ").strip()
    if text.lower() in {"main", "default"}:
        return None
    return text


def _format_live_clock(seconds: float | None) -> str:
    seconds_int = int(max(0, round(seconds or 0)))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}h{minutes:02d}m{secs:02d}s"
    return f"{minutes:02d}m{secs:02d}s"


class _MultiDownloadProgress:
    spinner = "⣾⣽⣻⢿⡿⣟⣯⣷"

    def __init__(self, streams, colors: bool | None = None, force: bool = False):
        self.streams = {offset: stream for offset, stream in enumerate(streams, start=1)}
        self.colors = colors
        self.enabled = force or sys.stdout.isatty()
        self.lock = threading.Lock()
        self.updates: dict[int, ProgressUpdate] = {}
        self.status_lines: dict[int, str] = {}
        self.frames: dict[int, int] = {}
        self.rendered_lines = 0
        self.started = False
        self.closed = False
        self.last_render = 0.0
        self.sampler = _SpeedSampler()

    def progress_for(self, offset: int) -> Callable[[ProgressUpdate], None]:
        def _progress(update: ProgressUpdate) -> None:
            self.update(offset, update)

        return _progress

    def status_for(self, offset: int) -> Callable[[str], None]:
        def _status(line: str) -> None:
            self.status(offset, line)

        return _status

    def status(self, offset: int, line: str) -> None:
        if not self.enabled:
            return
        with self.lock:
            if self.closed:
                return
            self.status_lines[offset] = line
            self._render_locked()

    def update(self, offset: int, update: ProgressUpdate) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self.lock:
            if self.closed:
                return
            update = _coalesce_progress_update(self.updates.get(offset), update)
            self.updates[offset] = update
            if not update.done and now - self.last_render < 0.12:
                return
            self.last_render = now
            self._render_locked()

    def print(self, message: str) -> None:
        with self.lock:
            if self.enabled and self.started:
                _clear_terminal_block(self.rendered_lines)
                self.rendered_lines = 0
                self.started = False
            print(message)
            if self.enabled and not self.closed and self.updates:
                self._render_locked()

    def close(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            self.closed = True
            if self.started:
                sys.stdout.write("\n")
                sys.stdout.flush()

    def _render_locked(self) -> None:
        width = max(64, _terminal_size((120, 30)).columns)
        rows = []
        progress_rows = []
        for offset, stream in self.streams.items():
            status_line = self.status_lines.get(offset)
            if status_line:
                rows.append((offset, status_line, None))
                continue
            update = self.updates.get(offset)
            if update is None:
                update = ProgressUpdate(
                    stream=stream,
                    completed_segments=0,
                    total_segments=max(1, len(stream.segments) or stream.segments_count or 1),
                    downloaded_bytes=0,
                    total_bytes=None,
                    elapsed_seconds=0.001,
                )
            frame = self.frames.get(offset, 0)
            rows.append((offset, update, frame))
            progress_rows.append((offset, update, frame))
            self.frames[offset] = frame + 1
        updates = [update for _, update, _ in progress_rows]
        tail_widths = _progress_tail_widths(updates)
        title_width, bar_width = _multi_progress_columns(updates, width, tail_widths=tail_widths)
        lines = []
        for _offset, row, frame in rows:
            if isinstance(row, str):
                lines.append(_ellipsize(row, max(1, width - 1)))
                continue
            lines.append(
                _progress_one_line(
                    row,
                    row.done,
                    self.colors,
                    frame or 0,
                    width,
                    title_width=title_width,
                    bar_width=bar_width,
                    tail_widths=tail_widths,
                    speed=self.sampler.observe(row),
                )
            )
        if not self.started:
            sys.stdout.write("\n")
            self.started = True
        # Leave the finished bars on screen. The single-track renderer keeps its
        # final state too, and this is where the per-track sizes matter most.
        self.rendered_lines = _rewrite_terminal_block(lines, self.rendered_lines)


class _DownloadProgress:
    spinner = "⣾⣽⣻⢿⡿⣟⣯⣷"

    def __init__(self, stream, colors: bool | None = None, force: bool = False):
        self.stream = stream
        self.colors = colors
        self.enabled = force or sys.stdout.isatty()
        self.started = False
        self.last_render = 0.0
        self.last_update: ProgressUpdate | None = None
        self.frame = 0
        self.rendered_lines = 0
        self.sampler = _SpeedSampler()

    def __call__(self, update: ProgressUpdate) -> None:
        update = _coalesce_progress_update(self.last_update, update)
        self.last_update = update
        if not self.enabled:
            return
        now = time.monotonic()
        if not update.done and now - self.last_render < 0.12:
            return
        self.last_render = now
        self._render(update)

    def finish(self) -> None:
        if not self.enabled:
            return
        if self.last_update and not self.last_update.done:
            self._render(self.last_update, force_done=True)
        if self.started:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.started = False
            self.rendered_lines = 0

    def close(self) -> None:
        if self.enabled and self.started:
            sys.stdout.flush()

    def cancel(self) -> None:
        if self.enabled and self.started:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _render(self, update: ProgressUpdate, force_done: bool = False) -> None:
        done = update.done or force_done
        lines = self._lines(update, done=done)
        if not self.started:
            sys.stdout.write("\n")
            self.started = True
        self.rendered_lines = _rewrite_terminal_block(lines, self.rendered_lines)

    def _lines(self, update: ProgressUpdate, done: bool) -> list[str]:
        width = min(120, max(44, _terminal_size((120, 30)).columns))
        line = _progress_one_line(update, done, self.colors, self.frame, width, speed=self.sampler.observe(update))
        self.frame += 1
        return [line]


class _SpeedSampler:
    """Exponentially-weighted recent throughput, keyed by stream.

    A whole-run average keeps reporting the historic rate through a stall and
    then jumps when transfer resumes; sampling the delta between renders makes
    a stall visible right away and keeps the ETA honest.
    """

    def __init__(self) -> None:
        self._samples: dict[int, tuple[int, float, float]] = {}

    def observe(self, update: ProgressUpdate, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        key = id(update.stream)
        downloaded = max(0, int(update.downloaded_bytes or 0))
        previous = self._samples.get(key)
        speed = 0.0
        sample_time = now
        if previous is None:
            # Nothing to diff against yet; the run average is the best guess.
            speed = downloaded / max(update.elapsed_seconds, 0.001)
        else:
            previous_bytes, previous_time, previous_speed = previous
            delta_bytes = max(0, downloaded - previous_bytes)
            delta_time = max(0.001, now - previous_time)
            sample_time = previous_time
            if delta_bytes > 0:
                instant = delta_bytes / delta_time
                speed = instant if previous_speed <= 0 else previous_speed * 0.45 + instant * 0.55
                sample_time = now
            elif previous_speed > 0 and now - previous_time < 2.0 and not update.done:
                speed = previous_speed
            downloaded = max(downloaded, previous_bytes)
        self._samples[key] = (downloaded, sample_time, speed)
        return speed


def _progress_one_line(
    update: ProgressUpdate,
    done: bool,
    colors: bool | None,
    frame: int,
    width: int,
    title_width: int | None = None,
    bar_width: int | None = None,
    tail_widths: _ProgressTailWidths | None = None,
    speed: float | None = None,
) -> str:
    safe_width = max(32, width - 1)
    percent = _progress_percent(update)
    if speed is None:
        speed = update.downloaded_bytes / max(update.elapsed_seconds, 0.001)
    eta = _format_eta(_estimate_remaining_seconds(update, speed))
    spinner = "✓" if done else _DownloadProgress.spinner[frame % len(_DownloadProgress.spinner)]
    spinner_text = paint(spinner, Palette.green if done else Palette.cyan, colors)
    title = _progress_title(update.stream)
    tail = _progress_tail(update, tail_widths or _progress_tail_widths([update]), percent=percent, speed=speed, eta=eta)
    if title_width is not None and bar_width is not None:
        title_text = _ellipsize(title, max(1, title_width)).ljust(title_width)
        tail_width = max(8, safe_width - title_width - bar_width - 4)
        tail = _ellipsize(tail, tail_width)
        return f"{paint(title_text, Palette.cyan, colors)} {_progress_bar(percent, bar_width, colors=colors, done=done)} {tail} {spinner_text}"
    min_title = 8
    min_bar = 8
    available = safe_width - len(tail) - 4
    if available < min_title + min_bar:
        tail = _ellipsize(tail, max(10, safe_width - min_title - min_bar - 4))
        available = safe_width - len(tail) - 4
    bar_width = max(min_bar, min(24, available - min_title))
    title_width = max(1, available - bar_width)
    return f"{paint(_ellipsize(title, title_width), Palette.cyan, colors)} {_progress_bar(percent, bar_width, colors=colors, done=done)} {tail} {spinner_text}"


def _coalesce_progress_update(previous: ProgressUpdate | None, current: ProgressUpdate) -> ProgressUpdate:
    if previous is None:
        return current
    if previous.done and not current.done:
        return previous
    total_segments = max(previous.total_segments, current.total_segments)
    if current.done:
        completed_segments = total_segments
        downloaded_bytes = current.downloaded_bytes
        total_bytes = current.total_bytes if current.total_bytes is not None else previous.total_bytes
    else:
        completed_segments = max(previous.completed_segments, current.completed_segments)
        downloaded_bytes = max(previous.downloaded_bytes, current.downloaded_bytes)
        total_bytes = current.total_bytes if current.total_bytes is not None else previous.total_bytes
    return ProgressUpdate(
        stream=current.stream,
        completed_segments=min(total_segments, completed_segments),
        total_segments=total_segments,
        downloaded_bytes=downloaded_bytes,
        total_bytes=total_bytes,
        elapsed_seconds=max(previous.elapsed_seconds, current.elapsed_seconds),
        done=current.done or previous.done,
    )


def _multi_progress_columns(updates: list[ProgressUpdate], width: int, tail_widths: _ProgressTailWidths | None = None) -> tuple[int, int]:
    safe_width = max(32, width - 1)
    titles = [_progress_title(update.stream) for update in updates]
    tail_widths = tail_widths or _progress_tail_widths(updates)
    tails = [_progress_tail(update, tail_widths) for update in updates]
    longest_title = max([len(title) for title in titles], default=12)
    longest_tail = max([len(tail) for tail in tails], default=32)
    preferred_title = min(longest_title, 34 if safe_width >= 110 else 26)
    bar_width = 24 if safe_width >= 100 else 18
    title_width = min(preferred_title, max(10, safe_width - longest_tail - bar_width - 4))
    if title_width < 10:
        title_width = min(preferred_title, 10)
    bar_width = max(8, min(bar_width, safe_width - title_width - longest_tail - 4))
    if safe_width - title_width - bar_width - 4 < 8:
        title_width = max(6, safe_width - bar_width - 12)
    return max(6, title_width), max(8, bar_width)


def _progress_tail(update: ProgressUpdate, widths: _ProgressTailWidths | None = None, *, percent: float | None = None, speed: float | None = None, eta: str | None = None) -> str:
    percent = _progress_percent(update) if percent is None else percent
    speed = update.downloaded_bytes / max(update.elapsed_seconds, 0.001) if speed is None else speed
    eta = _format_eta(_estimate_remaining_seconds(update, speed)) if eta is None else eta
    total_bytes = update.total_bytes
    size_text = _progress_size(update.downloaded_bytes, total_bytes)
    segment_text = f"{update.completed_segments}/{update.total_segments}"
    speed_text = f"{format_size(int(speed)) or '0B'}/s"
    percent_text = f"{int(percent):3d}%"
    widths = widths or _progress_tail_widths([update])
    return f"{percent_text} {segment_text:>{widths.segment}} {size_text:>{widths.size}} {speed_text:>{widths.speed}} ETA {eta}"


def _progress_tail_widths(updates: list[ProgressUpdate]) -> _ProgressTailWidths:
    segment_width = 9
    size_width = 17
    speed_width = 10
    for update in updates:
        segment_width = max(segment_width, len(f"{max(update.completed_segments, update.total_segments)}/{update.total_segments}"))
        size_width = max(size_width, len(_progress_size(update.downloaded_bytes, update.total_bytes)))
        if update.total_bytes:
            size_width = max(size_width, len(_progress_size(update.total_bytes, update.total_bytes)))
        speed = update.downloaded_bytes / max(update.elapsed_seconds, 0.001)
        speed_width = max(speed_width, len(f"{format_size(int(speed)) or '0B'}/s"))
    return _ProgressTailWidths(segment=segment_width, size=size_width, speed=speed_width)


def _progress_percent(update: ProgressUpdate) -> float:
    if update.total_segments and update.completed_segments >= update.total_segments:
        return 100.0
    if update.total_bytes and update.total_bytes > 0 and update.downloaded_bytes > 0:
        if update.done or update.downloaded_bytes <= update.total_bytes:
            return min(100.0, max(0.0, update.downloaded_bytes / update.total_bytes * 100))
    total_segments = max(1, update.total_segments)
    return min(100.0, update.completed_segments / total_segments * 100)


def _short_resolution(resolution: str | None) -> str | None:
    """1920x1080 -> 1080p, so the title leaves more room for the bar."""
    if not resolution or "x" not in resolution:
        return resolution
    height = resolution.rsplit("x", 1)[-1].strip()
    return f"{height}p" if height.isdigit() else resolution


def _progress_title(stream) -> str:
    prefix = stream.display_prefix()
    if stream.media_type == "audio":
        codec = pretty_codec(stream.codecs, stream.media_type)
        if stream.extra.get("audio_atmos") and codec in {"AC-3", "E-AC-3"}:
            codec = "Atmos"
        body = compact_join([stream.language, format_bitrate(stream.bandwidth), codec], separator=" ")
    elif stream.media_type in {"subtitle", "subtitles", "text"}:
        body = compact_join(
            [stream.language, stream.role or stream.name or stream.group_id or stream.id], separator=" "
        )
    else:
        body = compact_join(
            [
                _short_resolution(stream.resolution),
                stream.video_range if stream.video_range and stream.video_range != "SDR" else None,
                pretty_codec(stream.codecs, stream.media_type),
            ],
            separator=" ",
        )
    return f"{prefix} {body}".strip() if body else stream.format_line()


def _progress_bar(percent: float, width: int, colors: bool | None = None, done: bool = False) -> str:
    filled = int(width * max(0.0, min(100.0, percent)) / 100)
    if filled >= width:
        bar = "━" * width
        return paint(bar, Palette.green if done else Palette.cyan, colors)
    if filled <= 0:
        return paint("─" * width, Palette.muted, colors)
    return paint("━" * filled, Palette.green if done else Palette.cyan, colors) + paint("─" * (width - filled), Palette.muted, colors)


def _progress_size(downloaded: int, total: int | None) -> str:
    downloaded_text = format_size(downloaded) or "0B"
    if total and total > 0 and downloaded <= total:
        return f"{downloaded_text}/{format_size(total)}"
    return downloaded_text


def _estimate_remaining_seconds(update: ProgressUpdate, speed: float) -> float | None:
    if update.done:
        return 0
    if update.total_segments and update.completed_segments >= update.total_segments:
        return 0
    if update.total_bytes and speed > 0 and update.downloaded_bytes <= update.total_bytes:
        remaining = max(0, update.total_bytes - update.downloaded_bytes)
        return remaining / speed
    if update.completed_segments > 0:
        seconds_per_segment = update.elapsed_seconds / update.completed_segments
        return max(0, update.total_segments - update.completed_segments) * seconds_per_segment
    return None


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(0, round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _print_stream_summary(title: str, streams, original_streams=None, checked: bool = False, colors: bool | None = None) -> None:
    width = _summary_width()
    original_streams = original_streams or streams
    counts = _stream_type_counts(streams)
    if original_streams is not streams:
        noun = "selected" if title.lower() == "selected" else "tracks"
        summary = f"{title}: {len(streams)} {noun} / {len(original_streams)} total"
    else:
        summary = f"{title}: {len(streams)} tracks"
    summary = f"{summary} | {counts['video']} video | {counts['audio']} audio | {counts['subtitle']} subtitle"
    print(paint(_ellipsize(summary, width), Palette.green, colors))
    print(paint(_ellipsize(_source_type_line(streams), width), Palette.blue, colors))
    _print_streams(streams, original_streams=original_streams, checked=checked, colors=colors, compact=True)


def _print_selected_download_summary(streams, original_streams=None, colors: bool | None = None) -> None:
    original_streams = original_streams or streams
    counts = _stream_type_counts(streams)
    if original_streams is not streams:
        tracks = f"{len(streams)} selected / {len(original_streams)} total"
    else:
        tracks = f"{len(streams)} tracks"
    tracks = f"{tracks} | {counts['video']} video | {counts['audio']} audio | {counts['subtitle']} subtitle"
    print(paint("Selected tracks:", Palette.green, colors))
    _print_summary_field("Tracks", tracks, Palette.green, colors)
    _print_summary_field("Source", _source_type_value(streams), Palette.blue, colors)
    print(f"  {paint('Streams:', Palette.blue, colors)}")
    for index, stream in enumerate(streams):
        original_index = original_streams.index(stream) if stream in original_streams else index
        line = _format_numbered_line(original_index, stream, checked=True, colors=colors, width=_summary_width())
        print("    " + line.lstrip())
    _print_selected_key_ids(streams, colors=colors, indent=2)


def _print_live_recording_summary(live_streams, args, pipe_session=None, colors: bool | None = None, show_progress: bool = False) -> None:
    print()
    print(paint("Live recording:", Palette.green, colors))
    dvr_hint = _live_dvr_hint(live_streams, args)
    if dvr_hint:
        _print_summary_field("DVR", dvr_hint, Palette.yellow, colors)
    if pipe_session:
        _print_summary_field("Pipe mux", str(pipe_session.output_path), Palette.green, colors)
        for note in getattr(pipe_session, "notes", []) or []:
            _print_summary_field("Note", note, Palette.yellow, colors)
    elif getattr(args, "_live_pipe_mux_disabled_note", None):
        _print_summary_field("Pipe mux", "disabled; muxing after recording.", Palette.yellow, colors)
        _print_summary_field("Note", args._live_pipe_mux_disabled_note, Palette.yellow, colors)
    if show_progress:
        print()
        print(paint("Progress:", Palette.green, colors))


def _print_streams(streams, original_streams=None, checked: bool = False, colors: bool | None = None, compact: bool = False) -> None:
    if not streams:
        print("No streams found.")
        return
    for line in _stream_table_lines(streams, original_streams, checked=checked, colors=colors):
        print(line)


def _stream_numbering(streams, original_streams=None) -> dict[int, int]:
    """Map each stream to its 1-based position in the full, unfiltered list."""
    original_streams = original_streams if original_streams is not None else streams
    positions = {id(stream): index for index, stream in enumerate(original_streams, start=1)}
    numbering: dict[int, int] = {}
    for fallback, stream in enumerate(streams, start=1):
        numbering[id(stream)] = positions.get(id(stream), fallback)
    return numbering


def _stream_table_lines(streams, original_streams=None, checked: bool = False, colors: bool | None = None, indent: str = "  ") -> list[str]:
    return display.render_stream_table(
        streams,
        width=_summary_width(),
        colors=colors,
        numbers=_stream_numbering(streams, original_streams),
        checked=streams if checked else (),
        indent=indent,
    )


def _select_legacy(streams, selector: str | None):
    if not selector:
        return []
    selector = selector.strip().lower()
    if selector == "best":
        best = _best_by_type(streams, "video") or _best_by_type(streams, "audio") or (streams[0] if streams else None)
        return [best] if best else []
    if selector in {"best-av", "av"}:
        selected = []
        video = _best_by_type(streams, "video")
        audio = _best_by_type(streams, "audio")
        if video:
            selected.append(video)
        if audio:
            selected.append(audio)
        return selected
    selected = []
    for token in selector.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            index = int(token)
        except ValueError:
            raise SystemExit(f"Invalid --select token: {token}") from None
        if index < 1 or index > len(streams):
            raise SystemExit(f"Stream index out of range: {index}")
        selected.append(streams[index - 1])
    return selected


def _is_trickplay_stream(stream) -> bool:
    """Thumbnail-tile / trick-play tracks, which are not playable renditions.

    DASH flags them via extra["trick_mode"]; HLS names the role "TrickPlay".
    """
    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict) and extra.get("trick_mode"):
        return True
    role = getattr(stream, "role", None)
    return bool(role and "trick" in str(role).lower())


def _best_by_type(streams, media_type: str):
    candidates = [stream for stream in streams if stream.media_type == media_type]
    if not candidates:
        return None
    # A thumbnail sprite grid reports a taller coded height than the real
    # ladder (1280x1440 for 4x8 tiles), so it would win on height. Keep it as a
    # fallback only, in case a manifest offers nothing else.
    playable = [stream for stream in candidates if not _is_trickplay_stream(stream)]
    candidates = playable or candidates
    if media_type == "video":
        return max(candidates, key=lambda stream: (_stream_height(stream), stream.bandwidth or 0, stream.segments_count or 0))
    return max(candidates, key=lambda stream: (stream.bandwidth or 0, stream.segments_count or 0))


def _replace_direct_audio_with_matching_sabr_audio(selected, all_streams):
    if not any(_is_sabr_selection(stream) and getattr(stream, "media_type", None) == "video" for stream in selected):
        return selected
    sabr_audio = [
        stream
        for stream in all_streams
        if getattr(stream, "media_type", None) == "audio"
        and _is_sabr_selection(stream)
    ]
    if not sabr_audio:
        return selected
    replaced = []
    used: set[int] = set()
    for stream in selected:
        if getattr(stream, "media_type", None) != "audio" or _is_sabr_stream(stream):
            replaced.append(stream)
            continue
        match = _matching_sabr_audio_for_direct(stream, sabr_audio, used)
        if match is None:
            replaced.append(stream)
            continue
        replaced.append(match)
        used.add(id(match))
    return replaced


def _matching_sabr_audio_for_direct(stream, candidates, used: set[int]):
    stream_id = str(getattr(stream, "id", None) or getattr(stream, "group_id", None) or "")
    matches = [
        candidate
        for candidate in candidates
        if id(candidate) not in used
        and str(getattr(candidate, "id", None) or getattr(candidate, "group_id", None) or "") == stream_id
    ]
    if not matches:
        return None
    role = str(getattr(stream, "role", None) or "").lower()
    if role:
        role_matches = [candidate for candidate in matches if str(getattr(candidate, "role", None) or "").lower() == role]
        if role_matches:
            return role_matches[0]
    language = str(getattr(stream, "language", None) or "").lower()
    if language:
        language_matches = [candidate for candidate in matches if str(getattr(candidate, "language", None) or "").lower() == language]
        if language_matches:
            return language_matches[0]
    return matches[0]


def _replace_youtube_json_live_direct_with_hls(streams, args: argparse.Namespace, headers: dict[str, str] | None, colors: bool | None = None):
    if not streams or not any(_youtube_json_live_hls_manifest_url(stream) for stream in streams):
        return streams
    hls_cache: dict[str, list] = {}
    replaced_hls_urls: set[str] = set()
    replacements = 0
    skipped_audio = 0
    output = []
    for stream in streams:
        hls_url = _youtube_json_live_hls_manifest_url(stream)
        if hls_url and getattr(stream, "media_type", None) == "video":
            hls_streams = hls_cache.get(hls_url)
            if hls_streams is None:
                hls_streams = _load_youtube_json_live_hls_streams(hls_url, headers, getattr(args, "no_probe", False))
                hls_cache[hls_url] = hls_streams
            replacement = _matching_youtube_hls_live_variant(stream, hls_streams)
            if replacement is not None:
                output.append(_merge_youtube_json_live_metadata(stream, replacement))
                replaced_hls_urls.add(hls_url)
                replacements += 1
                continue
        output.append(stream)

    if replaced_hls_urls:
        filtered = []
        for stream in output:
            hls_url = _youtube_json_live_hls_manifest_url(stream)
            if hls_url in replaced_hls_urls and getattr(stream, "media_type", None) == "audio":
                skipped_audio += 1
                continue
            filtered.append(stream)
        output = filtered

    if replacements:
        detail = f"using HLS live playlist for {replacements} YouTube JSON direct video track{'s' if replacements != 1 else ''}"
        if skipped_audio:
            detail += f"; skipped {skipped_audio} direct audio track{'s' if skipped_audio != 1 else ''} already muxed in HLS"
        print(paint("YouTube live:", Palette.yellow, colors) + f" {detail}.")
        _log_line(args, f"YouTube live: {detail}.")
    return output


def _youtube_json_live_hls_manifest_url(stream) -> str | None:
    if getattr(stream, "manifest_type", None) != "json":
        return None
    if not getattr(stream, "is_live", False):
        return None
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict):
        return None
    hls_url = extra.get("hls_manifest_url") or extra.get("hlsManifestUrl")
    if not isinstance(hls_url, str) or not hls_url.strip():
        return None
    media_segments = [segment for segment in getattr(stream, "segments", []) or [] if getattr(segment, "index", None) != -1]
    if len(media_segments) != 1:
        return None
    if _selected_init_range(stream) is not None:
        return None
    if _youtube_json_live_direct_has_sequence(stream):
        return None
    if not any(_looks_like_youtube_live_direct_url(url) for url in _stream_candidate_urls(stream)):
        return None
    return hls_url.strip()


def _youtube_json_live_direct_has_sequence(stream) -> bool:
    for url in _stream_candidate_urls(stream):
        parsed = urlparse(url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if params.get("sq") or params.get("min_sq") or params.get("mindsq") or params.get("maxdsq"):
            return True
    return False


def _stream_candidate_urls(stream) -> list[str]:
    urls: list[str] = []

    def add(value) -> None:
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)

    add(getattr(stream, "url", None))
    extra = getattr(stream, "extra", None)
    if isinstance(extra, dict):
        all_urls = extra.get("all_urls")
        if isinstance(all_urls, list):
            for value in all_urls:
                add(value)
        elif isinstance(all_urls, dict):
            for value in all_urls.values():
                add(value)
    for segment in getattr(stream, "segments", []) or []:
        add(getattr(segment, "url", None))
    return urls


def _looks_like_youtube_live_direct_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host.endswith("googlevideo.com"):
        return False
    if not parsed.path.rstrip("/").endswith("/videoplayback"):
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    source = (params.get("source") or "").lower()
    return params.get("live") == "1" and source in {"yt_live_broadcast", "yt_tv_broadcast"}


def _load_youtube_json_live_hls_streams(hls_url: str, headers: dict[str, str] | None, no_probe: bool):
    try:
        return parse_source(
            hls_url,
            headers=headers,
            probe_direct=not no_probe,
            fetch_child_playlists=True,
        )
    except Exception:
        return []


def _matching_youtube_hls_live_variant(source, candidates):
    videos = [
        stream
        for stream in candidates
        if getattr(stream, "manifest_type", None) == "hls"
        and getattr(stream, "media_type", None) == "video"
        and getattr(stream, "is_live", False)
    ]
    if not videos:
        return None
    videos.sort(key=lambda stream: _youtube_hls_variant_match_score(source, stream))
    return videos[0]


def _youtube_hls_variant_match_score(source, candidate) -> tuple[int, int, int, int]:
    source_size = _resolution_tuple(getattr(source, "resolution", None))
    candidate_size = _resolution_tuple(getattr(candidate, "resolution", None))
    if source_size and candidate_size:
        resolution_score = abs(source_size[0] - candidate_size[0]) + abs(source_size[1] - candidate_size[1])
    elif getattr(source, "resolution", None) == getattr(candidate, "resolution", None):
        resolution_score = 0
    else:
        resolution_score = 100_000
    source_codec = video_codec_family(getattr(source, "codecs", None))
    candidate_codec = video_codec_family(getattr(candidate, "codecs", None))
    codec_score = 0 if source_codec and source_codec == candidate_codec else 1
    bandwidth_score = abs(int(getattr(candidate, "bandwidth", None) or 0) - int(getattr(source, "bandwidth", None) or 0))
    muxed_score = 0 if _stream_has_muxed_audio(candidate) else 1
    return (resolution_score, codec_score, bandwidth_score, muxed_score)


def _resolution_tuple(value: str | None) -> tuple[int, int] | None:
    match = re.search(r"(\d{2,5})\s*x\s*(\d{2,5})", str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _video_codec_family(value: str | None) -> str:
    return video_codec_family(value)


def _merge_youtube_json_live_metadata(source, hls_stream):
    source_extra = dict(getattr(source, "extra", {}) or {})
    hls_extra = dict(getattr(hls_stream, "extra", {}) or {})
    merged_extra = {
        **source_extra,
        **hls_extra,
        "youtube_json_live_hls_replacement": True,
        "json_live_original_url": getattr(source, "url", None),
        "json_live_original_id": getattr(source, "id", None),
    }
    return replace(hls_stream, extra=merged_extra)


def _is_sabr_selection(stream) -> bool:
    extra = getattr(stream, "extra", None)
    return _is_sabr_stream(stream) and isinstance(extra, dict)


_is_sabr_dvr_vod_selection = _is_sabr_selection


def _stream_height(stream) -> int:
    text = str(getattr(stream, "resolution", "") or "")
    match = re.search(r"(\d{2,5})\s*x\s*(\d{2,5})", text)
    if match:
        return int(match.group(2))
    match = re.search(r"(\d{3,5})\s*p\b", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


def _prompt_checklist(streams, colors: bool | None = None):
    if not streams:
        return []
    if _can_use_terminal_checklist():
        return _prompt_terminal_checklist(streams, colors=colors)
    _print_streams(streams, colors=colors)
    return _prompt_numeric_checklist(streams)


def _can_use_terminal_checklist() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_terminal_checklist(streams, colors: bool | None = None):
    try:
        return _prompt_terminal_checklist_curses(streams, colors=colors)
    except (OSError, termios.error):
        try:
            return _prompt_inline_checklist(streams, colors=colors)
        except (OSError, termios.error):
            _print_streams(streams, colors=colors)
            return _prompt_numeric_checklist(streams)


def _prompt_inline_checklist(streams, colors: bool | None = None):
    fd = sys.stdin.fileno()
    original_settings = termios.tcgetattr(fd)
    # Nothing is pre-checked: the picker only opens when the CLI named no
    # tracks, so any seed would be us guessing on the user's behalf.
    selected: set[int] = set()
    cursor = 0
    offset = 0
    rendered_lines = 0
    message = ""

    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except (OSError, termios.error):
        pass
    sys.stdout.write(_terminal_alternate_enter_sequence() + _terminal_keypad_enter_sequence() + "\x1b[?25l")
    sys.stdout.flush()
    try:
        tty.setcbreak(fd)
        while True:
            height = _terminal_size((100, 24)).lines
            visible_rows = max(1, min(len(streams), max(5, height - 7)))
            offset = _clamp_offset(cursor, offset, visible_rows, len(streams))
            lines = _inline_checklist_lines(streams, selected, cursor, offset, visible_rows, message, colors)
            rendered_lines = _rewrite_terminal_block(lines, rendered_lines)
            action = _terminal_key_action(fd)
            if action == "ctrl-c":
                raise KeyboardInterrupt
            if action == "cancel":
                return None
            if action == "enter":
                if not selected:
                    message = "No tracks checked. Press Space to check, q to cancel."
                    continue
                return [streams[index] for index in sorted(selected)]

            message = ""
            if action == "up":
                cursor = max(0, cursor - 1)
            elif action == "down":
                cursor = min(len(streams) - 1, cursor + 1)
            elif action == "pageup":
                cursor = max(0, cursor - visible_rows)
            elif action == "pagedown":
                cursor = min(len(streams) - 1, cursor + visible_rows)
            elif action == "home":
                cursor = 0
            elif action == "end":
                cursor = len(streams) - 1
            elif action == "space":
                if cursor in selected:
                    selected.remove(cursor)
                else:
                    selected.add(cursor)
            elif action == "all":
                selected = set() if len(selected) == len(streams) else set(range(len(streams)))
            elif action == "none":
                selected.clear()
            elif action == "esc":
                message = "Press q to cancel."
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)
        sys.stdout.write("\x1b[?25h" + _terminal_keypad_exit_sequence() + _terminal_alternate_exit_sequence())
        sys.stdout.flush()


def _inline_checklist_lines(
    streams,
    selected: set[int],
    cursor: int,
    offset: int,
    visible_rows: int,
    message: str,
    colors: bool | None,
) -> list[str]:
    width = _terminal_size((100, 24)).columns
    safe_width = max(1, width - 1)
    end = min(len(streams), offset + visible_rows)
    header = _checklist_header(streams, len(selected))
    source = _source_type_line(streams)
    status = message or (f"Showing {offset + 1}-{end} of {len(streams)}" if len(streams) > visible_rows else "")
    lines = [
        paint(_ellipsize(header, safe_width), Palette.green, colors),
        paint(_ellipsize(source, safe_width), Palette.blue, colors),
        paint(_ellipsize(_CHECKLIST_CONTROLS, safe_width), Palette.muted, colors),
        paint(_ellipsize(status, safe_width), Palette.yellow if message else Palette.muted, colors),
    ]
    for index in range(offset, end):
        stream = streams[index]
        lines.append(_format_checklist_line(index, stream, index in selected, index == cursor, width, colors))
    return lines


_CHECKLIST_CONTROLS = (
    "j/k move · b/f page · g/G top/bottom · Space check · a all · n none · Enter confirm · q cancel"
)


def _format_checklist_line(
    index: int,
    stream,
    checked: bool,
    active: bool,
    width: int,
    colors: bool | None,
    body: str | None = None,
) -> str:
    prefix_width = 11
    safe_width = max(1, width - 1)
    pointer = paint(">", Palette.cyan, colors) if active else " "
    number = paint(f"{index + 1:>3}.", Palette.cyan if active else Palette.muted, colors)
    checkbox = paint("[x]", Palette.green, colors) if checked else paint("[ ]", Palette.muted, colors)
    if body is None:
        body = _color_stream_text(stream, stream.format_line(), colors)
    body = _ellipsize(body, max(1, safe_width - prefix_width))
    return f"{pointer} {number} {checkbox} {body}"


def _checklist_header(streams, selected_count: int) -> str:
    counts = _stream_type_counts(streams)
    return (
        f"Select tracks: {selected_count} selected / {len(streams)} total "
        f"| {counts['video']} video | {counts['audio']} audio | {counts['subtitle']} subtitle"
    )


def _source_type_line(streams) -> str:
    labels: list[str] = []
    for stream in streams:
        label = _manifest_type_label(getattr(stream, "manifest_type", None))
        if label and label not in labels:
            labels.append(label)
    if not labels:
        labels.append("Unknown")
    prefix = "Source" if len(labels) == 1 else "Sources"
    return f"{prefix}: {', '.join(labels)}"


def _source_type_value(streams) -> str:
    line = _source_type_line(streams)
    if ":" not in line:
        return line
    return line.split(":", 1)[1].strip()


def _manifest_type_label(manifest_type: str | None) -> str:
    value = (manifest_type or "").lower()
    if value == "dash":
        return "DASH"
    if value in {"hls", "m3u8"}:
        return "HLS"
    if value == "m3u":
        return "M3U"
    if value == "ism":
        return "Smooth Streaming (ISM)"
    if value == "json":
        return "JSON direct URLs"
    if value == "sabr_ump":
        return "YouTube SABR/UMP"
    if value in {"direct", "media"}:
        return "Direct media"
    return value.upper() if value else "Unknown"


def _stream_type_counts(streams) -> dict[str, int]:
    counts = {"video": 0, "audio": 0, "subtitle": 0}
    for stream in streams:
        media_type = getattr(stream, "media_type", "") or ""
        if media_type == "video":
            counts["video"] += 1
        elif media_type == "audio":
            counts["audio"] += 1
        elif media_type in {"subtitle", "subtitles", "text"}:
            counts["subtitle"] += 1
    return counts


def _terminal_key_action(fd: int) -> str:
    data = os.read(fd, 1)
    if not data:
        return "ignore"
    if data == b"\x03":
        return "ctrl-c"
    if data in {b"\r", b"\n"}:
        return "enter"
    if data == b" ":
        return "space"
    if data in {b"q", b"Q"}:
        return "cancel"
    if data in {b"k", b"K"}:
        return "up"
    if data in {b"j", b"J"}:
        return "down"
    if data in {b"b", b"B"}:
        return "pageup"
    if data in {b"f", b"F"}:
        return "pagedown"
    if data == b"\x10":
        return "up"
    if data == b"\x0e":
        return "down"
    if data == b"\x02":
        return "pageup"
    if data == b"\x06":
        return "pagedown"
    if data in {b"g"}:
        return "home"
    if data in {b"G"}:
        return "end"
    if data == b"a":
        return "all"
    if data in {b"n", b"N"}:
        return "none"
    if data == b"\x1b":
        return _map_escape_sequence(_read_terminal_escape_sequence(fd))
    if data == b"\x9b":
        return _map_escape_sequence("[" + _read_terminal_escape_sequence(fd, timeout=0.35))
    return "ignore"


def _terminal_keypad_enter_sequence() -> str:
    return "\x1b[?1h\x1b="


def _terminal_keypad_exit_sequence() -> str:
    return "\x1b[?1l\x1b>"


def _terminal_alternate_enter_sequence() -> str:
    return "\x1b[?1049h"


def _terminal_alternate_exit_sequence() -> str:
    return "\x1b[?1049l"


def _read_terminal_escape_sequence(fd: int, timeout: float = 0.35) -> str:
    chunks: list[bytes] = []
    while len(chunks) < 8:
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            break
        item = os.read(fd, 1)
        if not item:
            break
        chunks.append(item)
        char = item.decode("latin1", errors="ignore")
        if char.isalpha() or char in {"~", "^", "$"}:
            break
        timeout = 0.12
    return b"".join(chunks).decode("latin1", errors="ignore")


def _rewrite_terminal_block(lines: list[str], previous_lines: int) -> int:
    sys.stdout.write("\x1b[?7l")
    try:
        if previous_lines:
            if previous_lines > 1:
                sys.stdout.write(f"\x1b[{previous_lines - 1}A")
            sys.stdout.write("\r")
        count = max(previous_lines, len(lines))
        for index in range(count):
            text = lines[index] if index < len(lines) else ""
            sys.stdout.write("\r\x1b[2K" + text)
            if index < count - 1:
                sys.stdout.write("\n")
        if count > len(lines):
            sys.stdout.write(f"\x1b[{count - len(lines)}A\r")
    finally:
        sys.stdout.write("\x1b[?7h")
        sys.stdout.flush()
    return len(lines)


def _clear_terminal_block(previous_lines: int) -> None:
    if previous_lines <= 0:
        return
    sys.stdout.write("\x1b[?7l")
    try:
        if previous_lines > 1:
            sys.stdout.write(f"\x1b[{previous_lines - 1}A")
        sys.stdout.write("\r")
        for index in range(previous_lines):
            sys.stdout.write("\r\x1b[2K")
            if index < previous_lines - 1:
                sys.stdout.write("\n")
        if previous_lines > 1:
            sys.stdout.write(f"\x1b[{previous_lines - 1}A")
        sys.stdout.write("\r")
    finally:
        sys.stdout.write("\x1b[?7h")
        sys.stdout.flush()


def _prompt_terminal_checklist_curses(streams, colors: bool | None = None):
    try:
        import curses
    except ImportError:
        raise OSError("curses is unavailable") from None

    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, termios.error):
        pass
    sys.stdout.write(_terminal_alternate_enter_sequence())
    sys.stdout.flush()
    try:
        result = curses.wrapper(_run_curses_checklist, streams, colors)
    except curses.error:
        raise OSError("curses checklist failed") from None
    finally:
        sys.stdout.write(_terminal_alternate_exit_sequence())
        sys.stdout.flush()
    if result is None:
        return None
    _print_checklist_snapshot(streams, result.selected, result.cursor, result.offset, result.visible_rows, colors=colors)
    return [streams[index] for index in result.selected]


def _run_curses_checklist(stdscr, streams, colors: bool | None = None):
    import curses

    _init_curses_default_background(stdscr, colors)
    try:
        curses.noecho()
        curses.cbreak()
        curses.flushinp()
    except curses.error:
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    selected: set[int] = set()
    cursor = 0
    offset = 0
    message = ""

    while True:
        height, width = stdscr.getmaxyx()
        visible_rows = max(1, height - 5)
        offset = _clamp_offset(cursor, offset, visible_rows, len(streams))
        _draw_curses_checklist(stdscr, streams, selected, cursor, offset, visible_rows, width, message, colors)
        key = stdscr.getch()
        action = _curses_key_action(stdscr, key)
        if action == "ctrl-c":
            raise KeyboardInterrupt
        if action == "cancel":
            return None
        if action == "enter":
            if not selected:
                message = "No tracks checked. Press Space to check, q to cancel."
                continue
            return _ChecklistResult(selected=sorted(selected), cursor=cursor, offset=offset, visible_rows=visible_rows)

        message = ""
        if action == "up":
            cursor = max(0, cursor - 1)
        elif action == "down":
            cursor = min(len(streams) - 1, cursor + 1)
        elif action == "pageup":
            cursor = max(0, cursor - visible_rows)
        elif action == "pagedown":
            cursor = min(len(streams) - 1, cursor + visible_rows)
        elif action == "home":
            cursor = 0
        elif action == "end":
            cursor = len(streams) - 1
        elif action == "space":
            if cursor in selected:
                selected.remove(cursor)
            else:
                selected.add(cursor)
        elif action == "all":
            selected = set() if len(selected) == len(streams) else set(range(len(streams)))
        elif action == "none":
            selected.clear()
        elif action == "esc":
            message = "Press q to cancel."


def _print_checklist_snapshot(streams, selected: list[int] | set[int], cursor: int, offset: int, visible_rows: int, colors: bool | None) -> None:
    selected_set = set(selected)
    if not streams:
        return
    terminal_height = _terminal_size((100, 24)).lines
    rows = max(1, min(len(streams), visible_rows, max(5, terminal_height - 7)))
    offset = _clamp_offset(cursor, offset, rows, len(streams))
    lines = _inline_checklist_lines(streams, selected_set, cursor, offset, rows, "", colors)
    for line in lines:
        print(line)


_CURSES_PAIR_IDS = {
    "muted": 1,
    "red": 2,
    "green": 3,
    "yellow": 4,
    "blue": 5,
    "magenta": 6,
    "cyan": 7,
}


def _init_curses_default_background(stdscr, colors: bool | None = None) -> None:
    import curses

    if colors is False:
        return
    try:
        curses.start_color()
    except curses.error:
        return
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    try:
        stdscr.bkgd(" ", curses.color_pair(0))
    except curses.error:
        pass
    if not curses.has_colors():
        return
    color_values = _curses_color_values()
    for name, pair_id in _CURSES_PAIR_IDS.items():
        try:
            curses.init_pair(pair_id, color_values[name], -1)
        except curses.error:
            pass


def _curses_color_values() -> dict[str, int]:
    import curses

    if getattr(curses, "COLORS", 0) >= 256:
        return {
            "muted": 245,
            "red": 160,
            "green": 35,
            "yellow": 178,
            "blue": 33,
            "magenta": 133,
            "cyan": 37,
        }
    return {
        "muted": curses.COLOR_WHITE,
        "red": curses.COLOR_RED,
        "green": curses.COLOR_GREEN,
        "yellow": curses.COLOR_YELLOW,
        "blue": curses.COLOR_BLUE,
        "magenta": curses.COLOR_MAGENTA,
        "cyan": curses.COLOR_CYAN,
    }


def _curses_attr(style: str | None, colors: bool | None = None, bold: bool = False) -> int:
    import curses

    attr = curses.A_BOLD if bold else 0
    if colors is False or not style:
        return attr
    style = style.strip()
    if style.startswith("bold_"):
        attr |= curses.A_BOLD
        style = style.removeprefix("bold_")
    pair_id = _CURSES_PAIR_IDS.get(style)
    if not pair_id:
        return attr
    try:
        return attr | curses.color_pair(pair_id)
    except curses.error:
        return attr


def _prompt_numeric_checklist(streams):
    print("\nUse numbers to choose tracks. Example: 1,3,8  |  all  |  q to cancel  |  empty to skip")
    try:
        raw = input("Choose: ").strip().lower()
    except EOFError:
        return []
    if not raw:
        return []
    if raw in {"q", "quit", "cancel"}:
        return None
    if raw == "all":
        return streams
    selected = []
    for token in raw.replace(",", " ").split():
        try:
            index = int(token) - 1
        except ValueError:
            raise SystemExit(f"Invalid track number: {token}") from None
        if index < 0 or index >= len(streams):
            raise SystemExit(f"Track number out of range: {token}")
        selected.append(streams[index])
    return selected


def _clamp_offset(cursor: int, offset: int, visible_rows: int, total: int) -> int:
    if cursor < offset:
        return cursor
    if cursor >= offset + visible_rows:
        return cursor - visible_rows + 1
    max_offset = max(0, total - visible_rows)
    return min(offset, max_offset)


def _draw_curses_checklist(stdscr, streams, selected: set[int], cursor: int, offset: int, visible_rows: int, width: int, message: str, colors: bool | None = None) -> None:
    import curses

    end = min(len(streams), offset + visible_rows)
    header = _checklist_header(streams, len(selected))
    source = _source_type_line(streams)
    controls = _CHECKLIST_CONTROLS
    if len(streams) > visible_rows:
        window = f"Showing {offset + 1}-{end} of {len(streams)}"
    else:
        window = ""

    stdscr.erase()
    _safe_addnstr(stdscr, 0, 0, header, width - 1, _curses_attr("green", colors, bold=True))
    _safe_addnstr(stdscr, 1, 0, source, width - 1, _curses_attr("blue", colors, bold=True))
    _safe_addnstr(stdscr, 2, 0, controls, width - 1, curses.A_DIM | _curses_attr("muted", colors))
    status = message or window
    status_attr = _curses_attr("yellow" if message else "muted", colors, bold=bool(message))
    if not message:
        status_attr |= curses.A_DIM
    _safe_addnstr(stdscr, 3, 0, status, width - 1, status_attr)
    for index in range(offset, end):
        row = 4 + index - offset
        active = index == cursor
        checked = index in selected
        _safe_addnstr_segments(
            stdscr,
            row,
            0,
            _curses_checklist_line_spans(index, streams[index], checked, active, width),
            width - 1,
            colors,
            base_bold=active or checked,
        )
    stdscr.refresh()


def _curses_checklist_line_spans(
    index: int, stream, checked: bool, active: bool, width: int, body: str | None = None
) -> list[tuple[str, str | None]]:
    safe_width = max(1, width - 1)
    prefix_width = 11
    marker = "x" if checked else " "
    body = _ellipsize(stream.format_line() if body is None else body, max(1, safe_width - prefix_width))
    return [
        (">" if active else " ", "cyan" if active else None),
        (" ", None),
        (f"{index + 1:>3}.", "cyan" if active else "muted"),
        (" ", None),
        (f"[{marker}]", "green" if checked else "muted"),
        (" ", None),
        *_stream_text_style_spans(stream, body),
    ]


def _stream_text_style_spans(stream, line: str) -> list[tuple[str, str | None]]:
    spans: list[tuple[str, str | None]] = [(line, None)]
    prefix = stream.display_prefix()
    prefix_style = _stream_prefix_style(prefix)
    if line.startswith(prefix):
        spans = [(prefix, prefix_style), (line[len(prefix):], None)]
    spans = _style_literal_once(spans, "Encrypted", "yellow")
    if " *" in line:
        scheme_start = line.find(" *")
        scheme_end = line.find(" ", scheme_start + 2)
        if scheme_end > scheme_start:
            spans = _style_literal_once(spans, line[scheme_start:scheme_end], "yellow")
    if getattr(stream, "media_type", None) == "audio":
        spans = _style_regex_spans(spans, r"(?<![A-Za-z0-9+])E-AC-3 Atmos(?![A-Za-z0-9+])", "bold_blue")
    for label, style in (
        ("DV+HDR10+", "bold_magenta"),
        ("DV+HDR10", "bold_magenta"),
        ("HDR10+", "bold_yellow"),
        ("HDR10", "yellow"),
        ("HLG", "green"),
        ("DV", "bold_magenta"),
        ("SDR", "muted"),
    ):
        spans = _style_regex_spans(spans, rf"(?<![A-Za-z0-9+]){re.escape(label)}(?![A-Za-z0-9+])", style)
    return [(text, style) for text, style in spans if text]


def _stream_prefix_style(prefix: str) -> str:
    if prefix == "Vid":
        return "cyan"
    if prefix == "Aud":
        return "green"
    if prefix == "Sub":
        return "magenta"
    return "blue"


def _style_literal_once(spans: list[tuple[str, str | None]], needle: str, style: str) -> list[tuple[str, str | None]]:
    escaped = re.escape(needle)
    return _style_regex_spans(spans, escaped, style, count=1)


def _style_regex_spans(spans: list[tuple[str, str | None]], pattern: str, style: str, count: int = 0) -> list[tuple[str, str | None]]:
    output: list[tuple[str, str | None]] = []
    styled = 0
    regex = re.compile(pattern)
    for text, existing_style in spans:
        if existing_style:
            output.append((text, existing_style))
            continue
        position = 0
        for match in regex.finditer(text):
            if count and styled >= count:
                break
            if match.start() > position:
                output.append((text[position:match.start()], None))
            output.append((match.group(0), style))
            position = match.end()
            styled += 1
        output.append((text[position:], None))
    return output


def _safe_addnstr_segments(stdscr, y: int, x: int, spans: list[tuple[str, str | None]], width: int, colors: bool | None, base_bold: bool = False) -> None:
    if width <= 0:
        return
    column = x
    remaining = width
    for text, style in spans:
        if remaining <= 0:
            break
        if not text:
            continue
        chunk = text[:remaining]
        _safe_addnstr(stdscr, y, column, chunk, remaining, _curses_attr(style, colors, bold=base_bold))
        column += len(chunk)
        remaining -= len(chunk)


def _curses_key_action(stdscr, key: int) -> str:
    import curses

    direct_actions = {
        3: "ctrl-c",
        10: "enter",
        13: "enter",
        curses.KEY_ENTER: "enter",
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_PPAGE: "pageup",
        curses.KEY_NPAGE: "pagedown",
        curses.KEY_HOME: "home",
        curses.KEY_END: "end",
        ord(" "): "space",
        ord("q"): "cancel",
        ord("Q"): "cancel",
        ord("k"): "up",
        ord("K"): "up",
        ord("j"): "down",
        ord("J"): "down",
        ord("g"): "home",
        ord("G"): "end",
        ord("a"): "all",
        ord("n"): "none",
        ord("N"): "none",
    }
    if key in direct_actions:
        return direct_actions[key]
    if key == 27:
        return _map_escape_sequence(_read_curses_escape_sequence(stdscr))
    return "ignore"


def _read_curses_escape_sequence(stdscr) -> str:
    chars: list[str] = []
    stdscr.timeout(120)
    try:
        while len(chars) < 8:
            key = stdscr.getch()
            if key == -1:
                break
            if 0 <= key <= 255:
                char = chr(key)
                chars.append(char)
                if char.isalpha() or char in {"~", "^", "$"}:
                    break
            else:
                break
    finally:
        stdscr.timeout(-1)
    return "".join(chars)


def _map_escape_sequence(sequence: str) -> str:
    sequence = sequence.lstrip("\x1b")
    if not sequence:
        return "esc"
    application_cursor = {
        "OA": "up",
        "OB": "down",
        "OC": "right",
        "OD": "left",
        "OH": "home",
        "OF": "end",
    }
    if sequence in application_cursor:
        return application_cursor[sequence]
    if not sequence.startswith("["):
        return "ignore"
    body = sequence[1:]
    if not body:
        return "ignore"
    final = body[-1]
    if final in {"A", "B", "C", "D", "H", "F"}:
        return {
            "A": "up",
            "B": "down",
            "C": "right",
            "D": "left",
            "H": "home",
            "F": "end",
        }[final]
    if final in {"~", "^", "$", "u"}:
        code = body[:-1].split(";", 1)[0]
        return {
            "1": "home",
            "4": "end",
            "5": "pageup",
            "6": "pagedown",
            "7": "home",
            "8": "end",
        }.get(code, "ignore")
    if body.isdigit():
        return {
            "1": "home",
            "4": "end",
            "5": "pageup",
            "6": "pagedown",
            "7": "home",
            "8": "end",
        }.get(body, "ignore")
    return "ignore"


def _safe_addnstr(stdscr, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    import curses

    if width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Width helpers count terminal cells, not code points, so CJK text (which
# occupies two cells per glyph) does not overrun computed column widths.
_ellipsize = display.ellipsize
_visible_text_len = display.visible_len
_truncate_visible_text = display.truncate
_pad_visible_text = display.pad


def _summary_width() -> int:
    return max(72, _terminal_size((120, 24)).columns - 1)


def _print_labeled_text(label: str, text: str, label_color: str, colors: bool | None = None) -> None:
    width = _summary_width()
    prefix = f"{label}: "
    painted_prefix = paint(f"{label}:", label_color, colors) + " "
    available = max(20, width - len(prefix))
    wrapped = textwrap.wrap(
        str(text),
        width=available,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]
    print(painted_prefix + wrapped[0])
    indent = " " * len(prefix)
    for line in wrapped[1:]:
        print(indent + line)


def _print_summary_field(
    label: str,
    text: object,
    label_color: str,
    colors: bool | None = None,
    indent: int = 2,
    label_width: int = 9,
    file=None,
) -> None:
    target = sys.stdout if file is None else file
    width = _summary_width()
    label_text = f"{label}:"
    label_cell = f"{label_text:<{label_width}}"
    plain_prefix = " " * indent + label_cell + " "
    painted_prefix = " " * indent + paint(label_cell, label_color, colors) + " "
    available = max(20, width - len(plain_prefix))
    wrapped = textwrap.wrap(
        str(text),
        width=available,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]
    print(painted_prefix + wrapped[0], file=target)
    continuation = " " * len(plain_prefix)
    for line in wrapped[1:]:
        print(continuation + line, file=target)


def _format_numbered_line(index: int, stream, checked: bool = False, colors: bool | None = None, width: int | None = None) -> str:
    marker = "x" if checked else " "
    number = paint(f"{index + 1:>3}.", Palette.muted, colors)
    checkbox = paint(f"[{marker}]", Palette.green if checked else Palette.muted, colors)
    body = stream.format_line()
    if width is not None:
        prefix_width = 9
        body = _ellipsize(body, max(1, width - prefix_width))
    return f"{number} {checkbox} {_color_stream_text(stream, body, colors)}"


def _color_stream_line(stream, colors: bool | None = None) -> str:
    return _color_stream_text(stream, stream.format_line(), colors)


_color_stream_text = display.colorize_stream_text


def _sort_streams(streams):
    order = {"video": 0, "audio": 1, "subtitle": 2, "subtitles": 2, "text": 2}
    return sorted(
        streams,
        key=lambda stream: (
            order.get(stream.media_type, 9),
            1 if stream.media_type == "audio" and getattr(stream, "manifest_type", None) == "sabr_ump" else 0,
            -(stream.bandwidth or 0),
            stream.language or "",
            stream.group_id or "",
            stream.name or "",
        ),
    )


def _drop_streams(streams, args: argparse.Namespace):
    patterns = {
        "video": getattr(args, "drop_video", None),
        "audio": getattr(args, "drop_audio", None),
        "subtitle": getattr(args, "drop_subtitle", None),
        "subtitles": getattr(args, "drop_subtitle", None),
        "text": getattr(args, "drop_subtitle", None),
    }
    kept = []
    for stream in streams:
        pattern = patterns.get(stream.media_type)
        if pattern and re.search(pattern, _stream_match_text(stream), flags=re.IGNORECASE):
            continue
        kept.append(stream)
    return kept


def _stream_match_text(stream) -> str:
    return " ".join(
        str(part)
        for part in [
            stream.format_line(),
            stream.id,
            stream.group_id,
            stream.name,
            stream.language,
            stream.role,
            stream.codecs,
            stream.resolution,
            stream.video_range,
            stream.url,
        ]
        if part
    )


def _stream_key_ids(stream) -> list[str]:
    kids = []
    for key in (stream.extra.get("key_ids") or []):
        if key and key not in kids:
            kids.append(key)
    key = stream.extra.get("key_id")
    if key and key not in kids:
        kids.append(key)
    for segment in stream.segments:
        if segment.key_id and segment.key_id not in kids:
            kids.append(segment.key_id)
    return kids


def _stream_current_media_key_id(stream) -> str | None:
    for segment in getattr(stream, "segments", []) or []:
        if getattr(segment, "index", None) == -1:
            continue
        kid = _normalize_kid_text(getattr(segment, "key_id", None))
        if kid:
            return kid
    return _normalize_kid_text(getattr(stream, "extra", {}).get("key_id"))


def _add_stream_key_ids(stream, kids: list[str]) -> None:
    if not isinstance(getattr(stream, "extra", None), dict):
        stream.extra = {}
    existing = list(stream.extra.get("key_ids") or [])
    for kid in kids:
        cleaned = _normalize_kid_text(kid)
        if cleaned and cleaned not in existing:
            existing.append(cleaned)
    if existing:
        stream.extra["key_ids"] = existing
        stream.extra.setdefault("key_id", existing[0])


_LIVE_KEY_PROMPT_LOCK = threading.Lock()
_ACTIVE_LIVE_PROGRESS_LOCK = threading.Lock()
_ACTIVE_LIVE_PROGRESS_PANEL: _LiveRecordProgress | None = None


def _set_active_live_progress_panel(panel: _LiveRecordProgress | None) -> _LiveRecordProgress | None:
    global _ACTIVE_LIVE_PROGRESS_PANEL
    with _ACTIVE_LIVE_PROGRESS_LOCK:
        previous = _ACTIVE_LIVE_PROGRESS_PANEL
        _ACTIVE_LIVE_PROGRESS_PANEL = panel
        return previous


def _active_live_progress_panel() -> _LiveRecordProgress | None:
    with _ACTIVE_LIVE_PROGRESS_LOCK:
        return _ACTIVE_LIVE_PROGRESS_PANEL


def _stream_key_ids_with_segment(stream, segment=None, extra_kid: str | None = None) -> list[str]:
    kids = _stream_key_ids(stream)
    for kid in [getattr(segment, "key_id", None) if segment is not None else None, extra_kid]:
        normalized = _normalize_kid_text(kid)
        if normalized and normalized not in kids:
            kids.append(normalized)
    return kids


def _live_key_provider(args) -> LiveKeyProvider | None:
    hooks = getattr(args, "embedding_hooks", None)
    return getattr(hooks, "live_key_provider", None)


def _ensure_live_segment_keys(
    keys: list[RawKey],
    stream,
    segments: list[SegmentInfo],
    colors: bool | None = None,
    provider: LiveKeyProvider | None = None,
) -> None:
    if not keys:
        return
    for segment in segments:
        if not getattr(segment, "encrypted", False):
            continue
        kid = _normalize_kid_text(getattr(segment, "key_id", None))
        if not kid or _has_raw_key_for_kid(keys, kid):
            continue
        _prompt_for_live_key(
            keys,
            kid,
            stream,
            segment,
            colors,
            reason="Live key changed",
            provider=provider,
        )


def _prompt_for_live_key(
    keys: list[RawKey],
    kid: str | None,
    stream,
    segment=None,
    colors: bool | None = None,
    reason: str = "Live key changed",
    require_kid: bool = False,
    force: bool = False,
    replace_existing: bool = False,
    provider: LiveKeyProvider | None = None,
) -> RawKey:
    kid = _normalize_kid_text(kid)
    existing_key = _raw_key_for_kid(keys, kid)
    if kid and not force and existing_key is not None:
        return existing_key
    with _LIVE_KEY_PROMPT_LOCK:
        progress_panel = _active_live_progress_panel()
        if progress_panel is not None:
            progress_panel.suspend()
        try:
            return _prompt_for_live_key_locked(
                keys,
                kid,
                stream,
                segment=segment,
                colors=colors,
                reason=reason,
                require_kid=require_kid,
                force=force,
                replace_existing=replace_existing,
                provider=provider,
            )
        finally:
            if progress_panel is not None:
                progress_panel.resume()


def _prompt_for_live_key_locked(
    keys: list[RawKey],
    kid: str | None,
    stream,
    segment=None,
    colors: bool | None = None,
    reason: str = "Live key changed",
    require_kid: bool = False,
    force: bool = False,
    replace_existing: bool = False,
    provider: LiveKeyProvider | None = None,
) -> RawKey:
    kid = _normalize_kid_text(kid)
    existing_key = _raw_key_for_kid(keys, kid)
    if kid and not force and existing_key is not None:
        return existing_key
    if provider is not None:
        supplied = provider(
            LiveKeyRequest(
                kid=kid,
                stream=stream,
                segment=segment,
                reason=reason,
                require_kid=require_kid,
                force=force,
                replace_existing=replace_existing,
            )
        )
        raw = str(supplied or "").strip()
        if not raw:
            raise RuntimeError(
                f"{reason}: the embedding application returned no key for "
                f"live KID {kid or 'unknown'}."
            )
        try:
            parsed = parse_keys([raw])
        except ValueError as exc:
            raise RuntimeError(
                f"{reason}: the embedding application returned an invalid live key."
            ) from exc
        if not parsed:
            raise RuntimeError(
                f"{reason}: the embedding application returned no valid live key."
            )
        key = parsed[0]
        if key.kid is None and kid and not require_kid:
            key = RawKey(kid=kid, key=key.key)
        if require_kid and key.kid is None:
            raise RuntimeError(
                f"{reason}: a hidden live key change requires KID:KEY."
            )
        if (
            kid
            and key.kid
            and _normalize_kid_text(key.kid) != kid
            and not require_kid
        ):
            raise RuntimeError(
                f"{reason}: the embedding application returned a key for a different KID."
            )
        if replace_existing and key.kid:
            key_kid = _normalize_kid_text(key.kid)
            keys[:] = [
                current
                for current in keys
                if _normalize_kid_text(current.kid) != key_kid
            ]
        keys.append(key)
        return key
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{reason}: stream now needs KID {kid or 'unknown'}, but stdin is not interactive. "
            "Restart with the new --key KID:KEY."
        )
    title = _progress_title(stream)
    print(file=sys.stderr)
    print(paint("Live key rotation:", Palette.green, colors), file=sys.stderr)
    _print_summary_field("Stream", title, Palette.cyan, colors, file=sys.stderr)
    if segment is not None and getattr(segment, "index", None) is not None:
        _print_summary_field("Segment", segment.index, Palette.blue, colors, file=sys.stderr)
    _print_summary_field("New KID", kid or "unknown", Palette.yellow, colors, file=sys.stderr)
    if reason != "Live key changed":
        _print_summary_field("Reason", reason, Palette.yellow, colors, file=sys.stderr)
    action = "Enter the matching KEY to continue; leave empty to stop."
    if require_kid or not kid:
        action = "Enter KID:KEY to continue; leave empty to stop."
    _print_summary_field("Action", action, Palette.muted, colors, file=sys.stderr)
    prompt = "  " + paint(f"{'KEY:':<9}", Palette.yellow, colors) + " "
    while True:
        raw = _read_interactive_prompt_line(prompt)
        if raw == "":
            raise RuntimeError(f"{reason}: no key was entered.")
        raw = raw.strip()
        if not raw:
            raise RuntimeError(f"{reason}: recording stopped because no new key was entered.")
        parsed = parse_keys([raw])
        if not parsed:
            print(paint("Invalid key format.", Palette.red, colors), file=sys.stderr)
            continue
        key = parsed[0]
        if key.kid is None and kid and not require_kid:
            key = RawKey(kid=kid, key=key.key)
        if require_kid and key.kid is None:
            print(paint("Please enter the key as KID:KEY for this hidden key change.", Palette.yellow, colors), file=sys.stderr)
            continue
        if kid and key.kid and _normalize_kid_text(key.kid) != kid and not require_kid:
            print(paint("Entered KID does not match the stream's new KID.", Palette.red, colors), file=sys.stderr)
            continue
        if replace_existing and key.kid:
            key_kid = _normalize_kid_text(key.kid)
            keys[:] = [existing for existing in keys if _normalize_kid_text(existing.kid) != key_kid]
        keys.append(key)
        added_kid = _normalize_kid_text(key.kid) or kid
        added_value = f"{added_kid}:{key.key}" if added_kid else key.key
        _print_summary_field("Added", added_value, Palette.green, colors, file=sys.stderr)
        print(file=sys.stderr, flush=True)
        return key


def _read_interactive_prompt_line(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    if not sys.stdin.isatty():
        return sys.stdin.readline()
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return sys.stdin.readline()
    _prepare_stdin_for_text_prompt(fd)
    chars: list[str] = []
    while True:
        try:
            data = os.read(fd, 1)
        except InterruptedError:
            continue
        if not data:
            return "".join(chars)
        try:
            char = data.decode(errors="replace")
        except UnicodeDecodeError:
            continue
        if char == "\x03":
            raise KeyboardInterrupt
        if char in {"\r", "\n"}:
            return "".join(chars)
        if char in {"\x7f", "\b"}:
            if chars:
                chars.pop()
            continue
        chars.append(char)


def _prepare_stdin_for_text_prompt(fd: int) -> None:
    try:
        attrs = termios.tcgetattr(fd)
    except (OSError, termios.error):
        return
    cooked = list(attrs)
    cooked[6] = list(attrs[6])
    for flag_name in ("BRKINT", "ICRNL", "IXON"):
        flag = getattr(termios, flag_name, 0)
        if flag:
            cooked[0] |= flag
    for flag_name in ("IGNBRK", "IGNCR", "INLCR"):
        flag = getattr(termios, flag_name, 0)
        if flag:
            cooked[0] &= ~flag
    flag = getattr(termios, "OPOST", 0)
    if flag:
        cooked[1] |= flag
    for flag_name in ("ICANON", "ECHO", "ISIG", "IEXTEN"):
        flag = getattr(termios, flag_name, 0)
        if flag:
            cooked[3] |= flag
    for flag_name in ("ECHOCTL",):
        flag = getattr(termios, flag_name, 0)
        if flag:
            cooked[3] &= ~flag
    if hasattr(termios, "VMIN"):
        cooked[6][termios.VMIN] = 1
    if hasattr(termios, "VTIME"):
        cooked[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, cooked)
    except (OSError, termios.error):
        return
    try:
        sys.stdout.write("\x1b[?25h" + _terminal_keypad_exit_sequence())
        sys.stdout.flush()
    except OSError:
        pass


def _has_raw_key_for_kid(keys: list[RawKey], kid: str | None) -> bool:
    return _raw_key_for_kid(keys, kid) is not None


def _raw_key_for_kid(keys: list[RawKey], kid: str | None) -> RawKey | None:
    kid = _normalize_kid_text(kid)
    if not kid:
        return keys[0] if keys else None
    for key in keys:
        if key.kid is None or _normalize_kid_text(key.kid) == kid:
            return key
    return None


def _normalize_kid_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip().lower().replace("-", "")
    if len(cleaned) != 32 or any(char not in "0123456789abcdef" for char in cleaned):
        return None
    return cleaned


def _print_selected_key_ids(streams, colors: bool | None = None, indent: int = 0) -> None:
    rows = []
    for stream in streams:
        kids = _stream_key_ids(stream)
        if kids:
            rows.append((stream, kids))
    if not rows:
        return
    width = _summary_width()
    print(" " * indent + paint("Keys:", Palette.blue, colors))
    row_indent = " " * (indent + 2)
    for stream, kids in rows:
        title = _progress_title(stream)
        kid_text = ", ".join(kids)
        label = "KID:"
        title_width = width - len(row_indent) - len(" | ") - len(label) - 1 - len(kid_text)
        if title_width >= 16:
            print(f"{row_indent}{paint(_ellipsize(title, title_width), Palette.cyan, colors)} | {paint(label, Palette.yellow, colors)} {kid_text}")
        else:
            print(f"{row_indent}{paint(_ellipsize(title, max(1, width - len(row_indent))), Palette.cyan, colors)}")
            for kid in kids:
                print(f"{row_indent}  {paint(label, Palette.yellow, colors)} {kid}")


def _append_input_params(streams, input_url: str) -> None:
    params = child_url_params(input_url)
    if not params:
        return
    for stream in streams:
        stream.url = _append_params(stream.url, params)
        stream.segments = [_segment_with_url(segment, _append_params(segment.url, params)) for segment in stream.segments]


def _segment_with_url(segment, url: str):
    segment.url = url
    return segment


def _append_params(url: str, params: list[tuple[str, str]]) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    existing_keys = {key for key, _ in existing}
    merged = [*existing, *[(key, value) for key, value in params if key not in existing_keys]]
    return urlunparse(parsed._replace(query=urlencode(merged, doseq=True)))


def _configure_proxy(args: argparse.Namespace) -> None:
    custom_proxy = getattr(args, "custom_proxy", None)
    if custom_proxy:
        os.environ["HTTP_PROXY"] = custom_proxy
        os.environ["HTTPS_PROXY"] = custom_proxy
        os.environ["http_proxy"] = custom_proxy
        os.environ["https_proxy"] = custom_proxy
    elif getattr(args, "use_system_proxy", True) is False:
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"


def _colors(args: argparse.Namespace) -> bool:
    if getattr(args, "no_color", False):
        return False
    if getattr(args, "force_ansi_console", False):
        return True
    return color_enabled()


def _print_load_error(exc: Exception, source: str | None, colors: bool | None = None) -> None:
    print(paint("Failed to load manifest:", Palette.red, colors), exc, file=sys.stderr)
    source = source or ""
    if "$" in source or any(token in str(exc) for token in ("-zsh", "-bash", "-fish")):
        print(
            paint("Hint:", Palette.yellow, colors)
            + " your URL may contain '$'. In zsh/bash, double quotes still expand $0, $1, etc. "
            "Use single quotes or escape it, for example:",
            file=sys.stderr,
        )
        print("  unidl list 'http://example.com/3$0Cg.../file.ism/manifest'", file=sys.stderr)
        print('  unidl list "http://example.com/3\\$0Cg.../file.ism/manifest"', file=sys.stderr)


def _print_task_error(exc: Exception, colors: bool | None = None) -> None:
    label = paint("Error:", Palette.red, colors)
    print(f"{label} {_short_error(exc)}", file=sys.stderr)
    if isinstance(exc, DownloadError) and exc.url:
        print(f"URL: {_ellipsize(exc.url, 180)}", file=sys.stderr)
        rule_tip = child_url_error_tip(exc.url, str(exc))
        if rule_tip:
            print(paint("Tip:", Palette.yellow, colors) + f" {rule_tip}", file=sys.stderr)
        elif _looks_like_vgc_bridge_error(exc):
            print(paint("Tip:", Palette.yellow, colors) + " VGC bridge manifest is readable, but the Android VideoGuard service did not return media bytes for this segment.", file=sys.stderr)
        elif _looks_like_missing_url_params_error(exc):
            print(paint("Tip:", Palette.yellow, colors) + " retry with --append-url-params so the manifest query is copied to init and media segments.", file=sys.stderr)
        elif _looks_like_missing_youtube_gvs_po_token_error(exc):
            print(
                paint("Tip:", Palette.yellow, colors)
                + " this Android Googlevideo URL likely requires a GVS PO token. "
                "Regenerate the manifest with a valid pot= parameter for the same YouTube client/session; lowering --workers will not fix it.",
                file=sys.stderr,
            )
        elif "SABR/UMP" in str(exc):
            return
        else:
            print(paint("Tip:", Palette.yellow, colors) + " retry with lower concurrency, for example --workers 4 --retries 8.", file=sys.stderr)
    elif isinstance(exc, subprocess.CalledProcessError):
        print(f"Command exited with status {exc.returncode}.", file=sys.stderr)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return "external command failed"
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _looks_like_missing_url_params_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "http error 497" in text or "invalid request type" in text


def _looks_like_missing_youtube_gvs_po_token_error(exc: Exception) -> bool:
    if not isinstance(exc, DownloadError) or not exc.url or "http error 403" not in str(exc).lower():
        return False
    parsed = urlparse(exc.url)
    host = (parsed.hostname or "").lower()
    if not host.endswith("googlevideo.com") or not parsed.path.rstrip("/").endswith("/videoplayback"):
        return False
    params = parse_qs(parsed.query)
    client = str((params.get("c") or [""])[0]).strip().upper()
    return client == "ANDROID" and not (params.get("pot") or [""])[0].strip()


def _looks_like_vgc_bridge_error(exc: DownloadError) -> bool:
    text = f"{exc.url or ''} {exc}".lower()
    return "hls_mobile" in text and "v~1-0-0" in text and ("timed out" in text or "timeout" in text)


def _print_child_request_header_warnings(input_url: str, headers: dict[str, str], colors: bool | None) -> None:
    for warning in child_request_header_warnings(input_url, headers):
        print(paint("Warning:", Palette.yellow, colors) + f" {warning}", file=sys.stderr)


def _log_line(args: argparse.Namespace, message: str) -> None:
    path = getattr(args, "log_file_path", None)
    if not path:
        return
    log_path = Path(path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


def _write_meta_json(output_dir: Path, save_name: str | None, streams, selected) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    base = _save_name_base(save_name) if save_name else "unidl"
    path = output_dir / f"{_safe_output_name(base)}.meta.json"
    data = {
        "streams": [stream.as_dict(include_segments=False) for stream in streams],
        "selected": [stream.as_dict(include_segments=True) for stream in selected],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _task_temp_root(args: argparse.Namespace, streams, default_save_base: str | None) -> Path:
    base_root = Path(args.tmp_dir).expanduser() if getattr(args, "tmp_dir", None) else _default_tmp_root()
    label = args.save_name or _input_temp_label(getattr(args, "input", "unidl"))
    safe_label = _safe_output_name(_save_name_base(str(label)))[:80] or "unidl"
    digest = hashlib.sha1(_task_temp_payload(args, streams).encode("utf-8")).hexdigest()[:12]
    return base_root / f"{safe_label}_{digest}"


def _legacy_task_temp_root(args: argparse.Namespace, streams, default_save_base: str | None) -> Path:
    base_root = Path(args.tmp_dir).expanduser() if getattr(args, "tmp_dir", None) else _default_tmp_root()
    label = args.save_name or default_save_base or _input_temp_label(getattr(args, "input", "unidl"))
    safe_label = _safe_output_name(_save_name_base(str(label)))[:80] or "unidl"
    digest = hashlib.sha1(_legacy_task_temp_payload(args, streams).encode("utf-8")).hexdigest()[:12]
    return base_root / f"{safe_label}_{digest}"


def _migrate_legacy_task_temp_roots(args: argparse.Namespace, streams, default_save_base: str | None, target: Path) -> None:
    if target.exists():
        return
    legacy = _legacy_task_temp_root(args, streams, default_save_base)
    if legacy == target or not legacy.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.rename(target)
    except OSError:
        return


def _task_temp_subdir(args: argparse.Namespace, name: str) -> Path:
    root = getattr(args, "_unidown_task_temp_root", None)
    if root is None:
        base_root = Path(args.tmp_dir).expanduser() if getattr(args, "tmp_dir", None) else _default_tmp_root()
        root = base_root / "unidown_task"
    return Path(root).expanduser() / name


def _task_temp_payload(args: argparse.Namespace, streams) -> str:
    payload = {
        "input": _resume_url_key(getattr(args, "input", None)),
        "save_name": getattr(args, "save_name", None),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_task_temp_payload(args: argparse.Namespace, streams) -> str:
    payload = {
        "input": getattr(args, "input", None),
        "save_name": getattr(args, "save_name", None),
        "streams": [_legacy_stream_temp_fingerprint(stream) for stream in streams],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_stream_temp_fingerprint(stream) -> dict:
    segments = getattr(stream, "segments", []) or []
    first = segments[0] if segments else None
    last = segments[-1] if segments else None
    return {
        "manifest_type": getattr(stream, "manifest_type", None),
        "media_type": getattr(stream, "media_type", None),
        "id": getattr(stream, "id", None),
        "group_id": getattr(stream, "group_id", None),
        "name": getattr(stream, "name", None),
        "language": getattr(stream, "language", None),
        "bandwidth": getattr(stream, "bandwidth", None),
        "resolution": getattr(stream, "resolution", None),
        "url": getattr(stream, "url", None),
        "original_url": getattr(stream, "original_url", None),
        "segments_count": len(segments),
        "first_segment": getattr(first, "url", None),
        "last_segment": getattr(last, "url", None),
    }


def _stream_temp_fingerprint(stream) -> dict:
    segments = getattr(stream, "segments", []) or []
    first = segments[0] if segments else None
    last = segments[-1] if segments else None
    return {
        "manifest_type": getattr(stream, "manifest_type", None),
        "media_type": getattr(stream, "media_type", None),
        "id": getattr(stream, "id", None),
        "group_id": getattr(stream, "group_id", None),
        "name": getattr(stream, "name", None),
        "language": getattr(stream, "language", None),
        "bandwidth": getattr(stream, "bandwidth", None),
        "resolution": getattr(stream, "resolution", None),
        "url": _resume_url_key(getattr(stream, "url", None)),
        "original_url": _resume_url_key(getattr(stream, "original_url", None)),
        "segments_count": len(segments),
        "first_segment": _resume_url_key(getattr(first, "url", None)),
        "last_segment": _resume_url_key(getattr(last, "url", None)),
    }


def _input_temp_label(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        name = Path(unquote(parsed.path)).name
        return name or parsed.netloc or "unidl"
    return Path(value).expanduser().name or "unidl"


def _parse_speed(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip().lower()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b(?:ps|/s)?|ps)?$", text)
    if not match:
        raise ValueError(f"Invalid --max-speed value: {value}")
    amount = float(match.group(1))
    unit = match.group(2)
    multiplier = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[unit]
    return max(1, int(amount * multiplier))


def _custom_hls_crypto(args: argparse.Namespace) -> HlsCrypto | None:
    method = getattr(args, "custom_hls_method", None)
    key_value = getattr(args, "custom_hls_key", None)
    iv_value = getattr(args, "custom_hls_iv", None)
    decryptor = getattr(args, "hls_decryptor", None)
    if not method and not key_value and not iv_value and decryptor is None:
        return None
    method = method or "AES_128"
    key = _decode_custom_bytes(key_value, "custom HLS key") if key_value else None
    iv = _decode_custom_bytes(iv_value, "custom HLS IV") if iv_value else None
    return HlsCrypto(method=method, key=key, iv=iv, decryptor=decryptor)


def _hls_crypto_for_stream(stream, hls_crypto: HlsCrypto | None) -> HlsCrypto | None:
    if hls_crypto is None:
        return None
    if not tencentvideo_separate_audio_custom_hls_applies(stream, hls_crypto.method):
        return None
    return hls_crypto


def _apply_custom_hls_crypto_to_streams(streams, hls_crypto: HlsCrypto | None, *, explicit_method: bool = False) -> None:
    if not hls_crypto:
        return
    scheme = _normalized_scheme(hls_crypto.method)
    if scheme in {"", "NONE", "UNKNOWN"}:
        return
    label = _display_hls_scheme(scheme)
    for stream in streams:
        if getattr(stream, "manifest_type", None) != "hls":
            continue
        if _hls_crypto_for_stream(stream, hls_crypto) is None:
            continue
        if not explicit_method and getattr(stream, "encrypted", False):
            continue
        if not explicit_method and not hls_crypto.key:
            continue
        stream.encrypted = True
        stream.encryption_scheme = label
        for segment in getattr(stream, "segments", []) or []:
            if explicit_method or not segment.encrypted:
                segment.encrypted = True
                segment.encryption_scheme = label


def _apply_sabr_request_options(streams, args: argparse.Namespace) -> None:
    sabr_streams = [stream for stream in streams if _is_sabr_stream(stream)]
    if not sabr_streams:
        return
    po_token = _sabr_po_token_arg(args)
    po_token_file = getattr(args, "sabr_po_token_file", None)
    selected_audio_ids = {str(stream.id) for stream in streams if getattr(stream, "media_type", None) == "audio" and getattr(stream, "id", None)}
    for stream in sabr_streams:
        extra = getattr(stream, "extra", None)
        if not isinstance(extra, dict):
            continue
        has_embedded_request_body = bool(extra.get("sabr_request_body"))
        if po_token and not has_embedded_request_body:
            extra["sabr_po_token"] = po_token
            if po_token_file:
                extra["sabr_po_token_source"] = f"override_file:{Path(po_token_file).expanduser()}"
                extra["sabr_po_token_status"] = "override"
            else:
                extra["sabr_po_token_source"] = "override_arg:--sabr-po-token"
                extra["sabr_po_token_status"] = "override"
        elif po_token:
            extra.setdefault("sabr_po_token", po_token)
            extra.setdefault(
                "sabr_po_token_source",
                f"ignored_override_file:{Path(po_token_file).expanduser()}" if po_token_file else "ignored_override_arg:--sabr-po-token",
            )
        if getattr(args, "sabr_playback_cookie", None):
            extra["sabr_playback_cookie"] = args.sabr_playback_cookie
        if getattr(args, "sabr_fk", None):
            extra["sabr_fk"] = args.sabr_fk
        request_audio = _sabr_audio_formats_for_selected_audio(extra.get("sabr_audio_formats"), selected_audio_ids)
        if request_audio:
            extra["sabr_request_audio_formats"] = request_audio


def _is_sabr_stream(stream) -> bool:
    return getattr(stream, "manifest_type", None) == "sabr_ump" or bool(getattr(stream, "extra", {}).get("sabr_ump"))


def _sabr_po_token_arg(args: argparse.Namespace) -> bytes | str | None:
    token_file = getattr(args, "sabr_po_token_file", None)
    if token_file:
        token = Path(token_file).expanduser().read_bytes()
        return token if token else None
    token = getattr(args, "sabr_po_token", None)
    return token.strip() if isinstance(token, str) and token.strip() else None


def _sabr_audio_formats_for_selected_audio(value, selected_audio_ids: set[str]) -> list[dict]:
    if not selected_audio_ids or not isinstance(value, list):
        return []
    matches = []
    for item in value:
        if not isinstance(item, dict):
            continue
        itag = str(item.get("itag") or item.get("id") or "")
        if itag in selected_audio_ids:
            matches.append(item)
    return matches


def _display_hls_scheme(scheme: str) -> str:
    return {
        "AES_128": "AES-128",
        "AES_128_ECB": "AES-128-ECB",
        "YOUKU_ECB": "YOUKU-ECB",
        "SAMPLE_AES": "SAMPLE-AES",
        "SAMPLE_AES_CTR": "SAMPLE-AES-CTR",
    }.get(scheme, scheme.replace("_", "-"))


def _decode_custom_bytes(value: str | None, label: str) -> bytes:
    if not value:
        return b""
    path = Path(value).expanduser()
    if path.exists():
        raw = path.read_bytes().strip()
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return raw
        if text:
            return _decode_custom_bytes(text, label)
        return raw
    cleaned = value.strip().replace("0x", "").replace(" ", "")
    if cleaned and len(cleaned) % 2 == 0 and all(char in "0123456789abcdefABCDEF" for char in cleaned):
        return bytes.fromhex(cleaned)
    try:
        import base64 as _base64

        return _base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError(f"Invalid {label}; expected file path, hex, or base64.") from exc


def _apply_custom_range(stream, range_expr: str) -> None:
    if _apply_custom_range_to_dvr_sequence(stream, range_expr):
        return
    if not stream.segments:
        return
    selected_media = _custom_range_indexes(range_expr, stream.segments_count)
    if selected_media is None:
        return
    filtered = []
    current_init = None
    init_included = False
    media_position = 0
    for segment in stream.segments:
        if segment.index == -1:
            current_init = segment
            init_included = False
            continue
        media_position += 1
        if media_position not in selected_media:
            continue
        if current_init is not None and not init_included:
            filtered.append(current_init)
            init_included = True
        filtered.append(segment)
    stream.segments = filtered
    stream.duration = sum(segment.duration or 0 for segment in stream.segments) or stream.duration


def _apply_custom_range_to_dvr_sequence(stream, range_expr: str) -> bool:
    extra = getattr(stream, "extra", None)
    if not isinstance(extra, dict):
        return False
    if getattr(stream, "manifest_type", None) == "sabr_ump" and extra.get("sabr_dvr_as_vod"):
        start_key = "sabr_dvr_min_sequence"
        end_key = "sabr_dvr_end_sequence"
    elif getattr(stream, "manifest_type", None) == "json" and extra.get("json_dvr_sequence"):
        start_key = "json_dvr_sequence_start"
        end_key = "json_dvr_sequence_end"
    else:
        return False
    start = _int_value(extra.get(start_key))
    if start is None:
        return False
    bounded_total = _dvr_sequence_custom_range_total(extra, start, end_key)
    selected = _custom_range_indexes(range_expr, bounded_total)
    if not selected:
        return False
    first_offset = min(selected) - 1
    last_offset = max(selected) - 1
    extra[start_key] = start + first_offset
    extra[end_key] = start + last_offset
    segment_duration = _float_value(extra.get("json_dvr_segment_duration"))
    if segment_duration is not None:
        stream.duration = segment_duration * len(selected)
    if getattr(stream, "segments", None):
        stream.segments = []
    return True


def _dvr_sequence_custom_range_total(extra: dict, start: int, end_key: str) -> int:
    end = _int_value(extra.get(end_key))
    if end is not None and end >= start:
        return end - start + 1
    for key in ("json_dvr_sequence_end_hint", "sabr_dvr_sequence_end_hint"):
        hint = _int_value(extra.get(key))
        if hint is not None and hint >= start:
            return hint - start + 1
    return 1_000_000_000


def _int_value(value) -> int | None:
    try:
        if value in {None, ""}:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_value(value) -> float | None:
    try:
        if value in {None, ""}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _filter_ad_segments(streams, patterns: list[str] | None, args: argparse.Namespace | None = None, colors: bool | None = None) -> int:
    if not patterns:
        return 0
    try:
        regexes = [re.compile(pattern) for pattern in patterns if pattern]
    except re.error as exc:
        raise ValueError(f"Invalid --ad-keyword regex: {exc}") from exc
    if not regexes:
        return 0
    removed = 0
    for stream in streams:
        if not stream.segments:
            continue
        before = len(stream.segments)
        stream.segments = [
            segment
            for segment in stream.segments
            if not any(regex.search(segment.url) for regex in regexes)
        ]
        stream.segments = _strip_orphan_init_segments(stream.segments)
        count = before - len(stream.segments)
        if not count:
            continue
        removed += count
        _refresh_stream_segment_state(stream)
        stream.extra["ad_filter_removed_segments"] = stream.extra.get("ad_filter_removed_segments", 0) + count
    if removed:
        print(f"{paint('Ad segments filtered:', Palette.yellow, colors)} {removed}")
        if args is not None:
            _log_line(args, f"Ad segments filtered: {removed}")
    return removed


def _strip_orphan_init_segments(segments: list) -> list:
    filtered = []
    for index, segment in enumerate(segments):
        if segment.index != -1:
            filtered.append(segment)
            continue
        has_media = False
        for next_segment in segments[index + 1 :]:
            if next_segment.index == -1:
                break
            has_media = True
            break
        if has_media:
            filtered.append(segment)
    return filtered


def _refresh_stream_segment_state(stream) -> None:
    stream.duration = sum(segment.duration or 0 for segment in stream.segments) or stream.duration
    if stream.segments:
        stream.encrypted = any(segment.encrypted for segment in stream.segments)
        stream.encryption_scheme = next((segment.encryption_scheme for segment in stream.segments if segment.encryption_scheme), stream.encryption_scheme)


def _custom_range_indexes(expr: str, total: int) -> set[int] | None:
    if not expr or not total:
        return None
    indexes: set[int] = set()
    for raw in re.split(r"[,;]", expr):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start = int(start_raw) if start_raw else 1
            end = int(end_raw) if end_raw else total
        else:
            start = end = int(token)
        start = max(1, start)
        end = min(total, end)
        if end < start:
            continue
        indexes.update(range(start, end + 1))
    return indexes


def _selection_options(args: argparse.Namespace) -> SelectionOptions:
    return SelectionOptions(
        video=getattr(args, "video", None),
        audio=getattr(args, "audio", None),
        video_lang=getattr(args, "video_lang", None),
        audio_lang=getattr(args, "audio_lang", None),
        subtitle_lang=getattr(args, "subtitle_lang", None),
        video_range=getattr(args, "video_range", None),
        audio_type=getattr(args, "audio_type", None),
        select_video=getattr(args, "select_video", None),
        select_audio=getattr(args, "select_audio", None),
        select_subtitle=getattr(args, "select_subtitle", None),
    )


def _output_target(args: argparse.Namespace) -> Path:
    if args.output:
        return Path(args.output).expanduser()
    if args.save_dir:
        return Path(args.save_dir).expanduser()
    return Path("downloads")


def _output_is_file_target(args: argparse.Namespace, output: Path) -> bool:
    return bool(args.output and output.suffix)


def _track_save_name(save_name: str | None, save_pattern: str | None, stream, offset: int, total: int) -> str | None:
    if save_pattern:
        return _render_save_pattern(save_pattern, save_name, stream, offset, total)
    if not save_name:
        return None
    if _is_subtitle_stream(stream):
        subtitle_suffix = _subtitle_filename_suffix(stream)
        base = _save_name_base(save_name)
        prefix = stream.display_prefix().lower()
        if total == 1:
            return f"{base}_{prefix}_{subtitle_suffix}" if subtitle_suffix else save_name
        suffix = f"{prefix}_{subtitle_suffix}" if subtitle_suffix else prefix
        return f"{base}_{offset:02d}_{suffix}"
    if total == 1:
        return save_name
    prefix = stream.display_prefix().lower()
    return f"{_save_name_base(save_name)}_{offset:02d}_{prefix}"


def _default_save_base(input_source: str, streams) -> str:
    primary = _primary_output_stream(streams)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    title = _stream_title(primary) or _input_stem(input_source)
    if _is_generic_source_name(title):
        title = _stream_specific_name(primary) or title
    parts = [
        title,
        primary.resolution if primary else None,
        _filename_token(format_bitrate(primary.bandwidth) if primary else None),
        primary.video_range.lower() if primary and primary.video_range else None,
        timestamp,
    ]
    tokens = [_filename_token(part) for part in parts if part]
    return "_".join(tokens) or f"unidown_{timestamp}"


def _primary_output_stream(streams):
    return (
        next((stream for stream in streams if stream.media_type == "video"), None)
        or next((stream for stream in streams if stream.media_type == "audio"), None)
        or (streams[0] if streams else None)
    )


def _stream_title(stream) -> str | None:
    if not stream:
        return None
    extra = getattr(stream, "extra", {}) or {}
    return extra.get("title") or extra.get("name") or None


def _stream_specific_name(stream) -> str | None:
    if not stream:
        return None
    for value in (stream.name, stream.id, stream.group_id):
        if value and not _is_generic_source_name(value):
            return value
    return stream.name or stream.id or stream.group_id


def _input_stem(input_source: str) -> str:
    parsed = urlparse(input_source)
    path = unquote(parsed.path) if parsed.scheme else input_source
    name = Path(path).name or "unidl"
    suffix = Path(name).suffix
    return Path(name).stem if suffix else name


def _is_generic_source_name(value: str | None) -> bool:
    if not value:
        return True
    stem = Path(str(value)).stem.lower()
    return stem in {"", "index", "master", "playlist", "manifest", "output"}


def _filename_token(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("/", "_").replace("\\", "_").replace(":", "_")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._-")


def _subtitle_filename_suffix(stream) -> str:
    for language in _subtitle_filename_language_candidates(stream):
        token = _filename_token(language)
        if token:
            return token
    return ""


def _subtitle_filename_language_candidates(stream) -> list[str]:
    languages: list[str] = []
    language = getattr(stream, "language", None)
    if language:
        languages.append(str(language).strip())
    for url in _subtitle_language_urls(stream):
        language = _subtitle_language_from_url(url)
        if language:
            languages.append(language)
    return languages


def _subtitle_language_urls(stream) -> list[str]:
    urls = []
    for value in (getattr(stream, "url", None), getattr(stream, "original_url", None)):
        if value:
            urls.append(str(value))
    for segment in getattr(stream, "segments", []) or []:
        url = getattr(segment, "url", None)
        if url:
            urls.append(str(url))
    return urls


_SUBTITLE_URL_LANGUAGE_CODES = {
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
    "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
    "da", "de", "dv", "dz",
    "ee", "el", "en", "eo", "es", "et", "eu",
    "fa", "ff", "fi", "fj", "fo", "fr", "fy",
    "ga", "gd", "gl", "gn", "gu", "gv",
    "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
    "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
    "ja", "jv",
    "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
    "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
    "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
    "oc", "oj", "om", "or", "os",
    "pa", "pi", "pl", "ps", "pt",
    "qu",
    "rm", "rn", "ro", "ru", "rw",
    "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw",
    "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz",
    "ve", "vi", "vo",
    "wa", "wo",
    "xh",
    "yi", "yo",
    "za", "zh", "zu",
    "cmn", "yue", "eng", "spa", "fra", "fre", "deu", "ger", "ita", "por", "jpn", "kor", "zho", "chi", "und",
}


def _subtitle_language_from_url(url: str) -> str | None:
    parsed = urlparse(str(url))
    path = unquote(parsed.path or str(url))
    for match in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{2,3}(?:[-_][A-Za-z]{4})?(?:[-_](?:[A-Za-z]{2}|\d{3}))?)(?![A-Za-z0-9])", path):
        candidate = match.group(1).replace("_", "-")
        primary = candidate.split("-", 1)[0].lower()
        if primary in _SUBTITLE_URL_LANGUAGE_CODES:
            return candidate
    return None


def _render_save_pattern(pattern: str, save_name: str | None, stream, offset: int, total: int) -> str:
    values = {
        "SaveName": _save_name_base(save_name) if save_name else (stream.name or stream.id or stream.display_prefix().lower()),
        "Id": stream.id or "",
        "Codecs": pretty_codec(stream.codecs, stream.media_type) or stream.codecs or "",
        "Language": stream.language or "",
        "Resolution": stream.resolution or "",
        "Bandwidth": str(stream.bandwidth or ""),
        "MediaType": stream.media_type or "",
        "Channels": stream.channels or "",
        "FrameRate": format_frame_rate(stream.frame_rate) or "",
        "VideoRange": stream.video_range or "",
        "GroupId": stream.group_id or "",
        "Ext": _default_extension_for_pattern(stream),
        "Index": f"{offset:02d}",
    }
    rendered = pattern
    for key, value in values.items():
        rendered = rendered.replace(f"<{key}>", str(value))
    rendered = _safe_output_name(rendered)
    if total > 1 and "<Index>" not in pattern and "<MediaType>" not in pattern and "<Id>" not in pattern:
        rendered = f"{rendered}_{offset:02d}"
    return rendered


def _default_extension_for_pattern(stream) -> str:
    extension = (stream.extension or "").lower()
    if extension == "bbts":
        return "ts"
    if extension in {"ts", "mp3", "m4a", "mp4", "vtt", "srt", "ttml", "aac", "ac3", "eac3", "webm"}:
        return extension
    if stream.media_type in {"subtitle", "subtitles", "text"}:
        codec = (stream.codecs or "").lower()
        if extension in {"m4s", "m4v", "mov"} and any(token in codec for token in ("wvtt", "stpp", "ttml")):
            return "mp4"
        return "vtt" if extension == "webvtt" else "ttml"
    return "mp4"


def _safe_output_name(value: str) -> str:
    value = value.strip().replace("/", "_").replace("\\", "_").replace(":", "_")
    value = re.sub(r"_+", "_", value)
    return value.strip("._ ") or "stream"


def _save_name_base(save_name: str) -> str:
    path = Path(save_name)
    known_suffixes = {".aac", ".ac3", ".bbts", ".eac3", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".srt", ".ts", ".ttml", ".vtt", ".webm"}
    return path.stem if path.suffix.lower() in known_suffixes else path.name


def _decrypter_for_stream(decrypter: str, stream) -> str:
    return _normalize_decrypter_name(decrypter)


def _live_pipe_fragment_decrypter(decrypter: str, stream=None, pipe_format: str | None = None) -> str:
    return _normalize_decrypter_name(decrypter)


def _pipe_expected_fragment_seconds(stream, segment, source_duration: int | None = None, source_timescale: int | None = None) -> float | None:
    if source_duration and source_timescale:
        return max(0.0, source_duration / source_timescale)
    if not segment:
        return None
    duration = getattr(segment, "duration", None)
    if duration is None or duration <= 0:
        return None
    return float(duration)


def _pipe_source_delta_seconds(
    source_time: int,
    previous_source_time: int,
    source_timescale: int | None,
    previous_timescale: int | None,
) -> float | None:
    if not source_timescale or source_timescale <= 0:
        return None
    if previous_timescale and previous_timescale != source_timescale:
        return None
    return (source_time - previous_source_time) / source_timescale


def _pipe_should_preserve_source_time(source_delta: float | None, expected_seconds: float | None) -> bool:
    if source_delta is None:
        return False
    if source_delta < -0.25:
        return False
    if not expected_seconds or expected_seconds <= 0:
        return source_delta < 10
    return source_delta <= max(expected_seconds * 3, expected_seconds + 3)


def _pipe_min_fragment_duration(stream, segment) -> float | None:
    if not segment or stream.media_type != "video":
        return None
    duration = getattr(segment, "duration", None)
    if duration is None or duration <= 0:
        return None
    return float(duration)


def _normalize_decrypter_name(value: str) -> str:
    lookup = {
        "auto": "internal",
        "mp4decrypt": "internal",
        "packager": "internal",
        "MP4DECRYPT": "internal",
        "SHAKA_PACKAGER": "internal",
    }
    return lookup.get(value, value or "internal")


def _cancel_futures_now(pool: ThreadPoolExecutor, futures) -> None:
    for future in futures:
        future.cancel()
    pool.shutdown(wait=current_download_runtime() is not None, cancel_futures=True)


def _selected_mux_format(args: argparse.Namespace, live: bool = False) -> str:
    if live and getattr(args, "_live_pipe_mux_disabled_mux_format", None):
        return args._live_pipe_mux_disabled_mux_format
    return getattr(args, "mux_format", None) or ("ts" if live else "mkv")


def _should_mux_after_download(args: argparse.Namespace, mux_imports: list, downloaded_paths: list[Path], selected_streams) -> bool:
    if should_preserve_audio_vivid_source_container(selected_streams):
        return False
    if _has_detected_audio_vivid(selected_streams) and not shutil.which("mkvmerge"):
        return False
    if getattr(args, "no_mux", False):
        return False
    if getattr(args, "mux", False) or mux_imports:
        return True
    if getattr(args, "chapters_file", None) and any(
        getattr(stream, "media_type", None) == "video" for stream in selected_streams
    ):
        return True
    if getattr(args, "mux_format", None) and not _only_subtitle_streams(selected_streams):
        return True
    if getattr(args, "audio_format", None) and not any(
        getattr(stream, "media_type", None) == "video" for stream in selected_streams
    ):
        return False
    if len(downloaded_paths) <= 1:
        return _single_vod_stream_needs_container_mux(selected_streams)
    return not _only_subtitle_streams(selected_streams)


def _has_detected_audio_vivid(streams) -> bool:
    return any(
        isinstance(getattr(stream, "extra", None), dict)
        and bool(stream.extra.get("audio_vivid_detected"))
        for stream in streams or []
    )


def _muxer_for_tracks(
    tracks: list[_DownloadedTrack],
    args: argparse.Namespace,
    *,
    live: bool = False,
) -> str:
    streams = [track.stream for track in tracks]
    if not live and _tracks_include_vvc(tracks):
        # Current mkvmerge versions can identify Tencent VVC as V_QUICKTIME.
        # FFmpeg's MP4 muxer writes the ISO VVC sample entry (vvc1), which is
        # the portable form consumed by ffprobe and downstream players.
        return "ffmpeg"
    if _has_detected_audio_vivid(streams) or any(
        isinstance(getattr(stream, "extra", None), dict)
        and bool(stream.extra.get("audio_vivid_companion"))
        for stream in streams
    ):
        return "mkvmerge"
    return getattr(args, "muxer", "auto")


def _mux_format_for_tracks(
    tracks: list[_DownloadedTrack],
    args: argparse.Namespace,
    *,
    live: bool,
) -> str:
    if not live and _tracks_include_vvc(tracks):
        return "mp4"
    if _muxer_for_tracks(tracks, args, live=live) == "mkvmerge":
        return "mkv"
    return _selected_mux_format(args, live=live)


def _tracks_include_vvc(tracks: list[_DownloadedTrack]) -> bool:
    """Return whether the final output selection contains a VVC video track."""

    return any(
        getattr(track.stream, "media_type", None) == "video"
        and looks_like_h266(
            getattr(track.stream, "codecs", None),
            getattr(track.stream, "url", None),
            getattr(track.stream, "name", None),
        )
        for track in tracks
    )


def _single_vod_stream_needs_container_mux(streams) -> bool:
    if len(streams) != 1:
        return False
    stream = streams[0]
    if _is_subtitle_stream(stream) or getattr(stream, "is_live", False):
        return False
    return getattr(stream, "manifest_type", None) == "hls" and _stream_uses_native_mpegts(stream)


def _only_subtitle_streams(streams) -> bool:
    return bool(streams) and all(stream.media_type in {"subtitle", "subtitles", "text"} for stream in streams)


def _is_subtitle_stream(stream) -> bool:
    return getattr(stream, "media_type", None) in {"subtitle", "subtitles", "text"}


def _has_subtitle_tracks(tracks: list[_DownloadedTrack]) -> bool:
    return any(_is_subtitle_stream(track.stream) for track in tracks)


def _with_audio_vivid_companions(
    tracks: list[_DownloadedTrack],
) -> list[_DownloadedTrack]:
    expanded = list(tracks)
    known_paths = {track.path.resolve() for track in tracks if track.path.exists()}
    for track in tracks:
        extra = getattr(track.stream, "extra", None)
        if not isinstance(extra, dict):
            continue
        raw_path = str(extra.get("audio_vivid_wav") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_file() or path.resolve() in known_paths:
            continue
        stream = copy.copy(track.stream)
        stream.media_type = "audio"
        stream.codecs = "pcm_s16le"
        stream.extension = "wav"
        stream.id = f"{getattr(track.stream, 'id', None) or track.offset}-audio-vivid"
        stream.name = "Audio Vivid PCM"
        stream.channels = None
        stream.url = str(path)
        stream.original_url = str(path)
        stream.segments = []
        stream.extra = {
            "audio_vivid_companion": True,
            "source": "audio-vivid-policy",
        }
        expanded.append(
            _DownloadedTrack(
                offset=len(expanded) + 1,
                stream=stream,
                path=path,
                temp_dir=None,
                cleanup_paths=[path],
            )
        )
        known_paths.add(path.resolve())
    return expanded


def _live_mux_tracks(tracks: list[_DownloadedTrack], args: argparse.Namespace) -> list[_DownloadedTrack]:
    mux_format = _selected_mux_format(args, live=True).lower()
    if mux_format == "ts" or getattr(args, "live_pipe_mux", False) or getattr(args, "live_real_time_merge", False):
        return [track for track in tracks if not _is_subtitle_stream(track.stream)]
    return tracks


def _mux_inputs_for_tracks(tracks: list[_DownloadedTrack]) -> list[MuxInput]:
    tracks = [track for track in tracks if not _empty_subtitle_track(track)]
    selected_streams = [track.stream for track in tracks]
    starts = [_dash_mux_start(track.stream) for track in tracks]
    known_starts = [start for start in starts if start is not None]
    base_start = min(known_starts) if known_starts else None
    return [
        _mux_input_for_track(
            track,
            _mux_delay_ms(start, base_start),
            trim_start_ms=_dash_mux_preroll_trim_ms(track.stream),
            selected_streams=selected_streams,
        )
        for track, start in zip(tracks, starts, strict=False)
    ]


def _empty_subtitle_track(track: _DownloadedTrack) -> bool:
    if not _is_subtitle_stream(track.stream):
        return False
    try:
        return track.path.stat().st_size == 0
    except OSError:
        return False


def _mux_input_for_track(
    track: _DownloadedTrack,
    delay_ms: int | None = None,
    trim_start_ms: int | None = None,
    selected_streams=None,
) -> MuxInput:
    stream = track.stream
    return MuxInput(
        path=track.path,
        language=_mux_language(stream),
        name=_mux_track_name(stream),
        default=_mux_default_flag(stream),
        forced=_mux_forced_flag(stream),
        delay_ms=delay_ms,
        trim_start_ms=trim_start_ms,
        media_type=getattr(stream, "media_type", None),
        track_type_filter=(
            tencentvideo_separate_audio_mux_media_type(stream)
            or iqiyi_separate_audio_mux_media_type(stream, selected_streams)
        ),
        codecs=getattr(stream, "codecs", None),
    )


def _dash_mux_start(stream) -> float | None:
    period_start = _dash_mux_period_start(stream)
    presentation_start = _dash_mux_presentation_start(stream)
    if presentation_start is None:
        return period_start
    return (period_start or 0.0) + max(0.0, presentation_start)


def _dash_mux_period_start(stream) -> float | None:
    if getattr(stream, "manifest_type", None) != "dash" or getattr(stream, "is_live", False):
        return None
    extra = getattr(stream, "extra", {}) or {}
    value = extra.get("period_start")
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    if extra.get("period_id") or extra.get("period_ids"):
        return 0.0
    return None


def _mux_delay_ms(start: float | None, base_start: float | None) -> int | None:
    if start is None or base_start is None:
        return None
    delay = start - base_start
    if delay <= 0.001:
        return None
    return int(round(delay * 1000))


def _dash_mux_preroll_trim_ms(stream) -> int | None:
    start = _dash_mux_presentation_start(stream)
    if start is None or start >= -0.001:
        return None
    return int(round(abs(start) * 1000))


def _dash_mux_presentation_start(stream) -> float | None:
    if getattr(stream, "manifest_type", None) != "dash" or getattr(stream, "is_live", False):
        return None
    extra = getattr(stream, "extra", {}) or {}
    value = extra.get("dash_presentation_start")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mux_language(stream) -> str | None:
    language = (stream.language or "").strip()
    if not language or language.lower() in {"und", "unknown"}:
        return None
    return _normalize_mux_language(language)


_MUX_LANGUAGE_ALIASES = {
    "cantonese": "yue",
    "sp": "es",
    "iw": "he",
    "in": "id",
    "ji": "yi",
    "jp": "ja",
    "cz": "cs",
    "gr": "el",
    "zh-chs": "zh-Hans",
    "zh-cht": "zh-Hant",
}


def _normalize_mux_language(language: str) -> str:
    normalized = language.strip()
    alias = _MUX_LANGUAGE_ALIASES.get(normalized.lower())
    if alias:
        return alias
    return normalized


def _mux_track_name(stream) -> str | None:
    role = _mux_role_label(stream)
    if stream.media_type in {"subtitle", "subtitles", "text"}:
        return compact_join([stream.language, role or stream.name])
    if stream.media_type == "audio":
        return compact_join([stream.language, role, f"{stream.channels}CH" if stream.channels else None])
    return None


def _mux_role_label(stream) -> str | None:
    role = stream.role or stream.name
    if not role:
        return None
    role = str(role).replace("_", " ").strip()
    if role.lower() in {"main", "default"}:
        return None
    return role


def _mux_default_flag(stream) -> bool | None:
    if _truthy_stream_flag(stream, "default"):
        return True
    if stream.media_type in {"subtitle", "subtitles", "text"}:
        return False
    return None


def _mux_forced_flag(stream) -> bool | None:
    if _truthy_stream_flag(stream, "forced"):
        return True
    text = " ".join(str(part or "") for part in [stream.role, stream.name, stream.group_id, stream.id]).lower()
    if "forced" in text:
        return True
    if stream.media_type in {"subtitle", "subtitles", "text"}:
        return False
    return None


def _truthy_stream_flag(stream, name: str) -> bool:
    values = [stream.extra.get(name), stream.extra.get(f"is_{name}"), stream.extra.get(f"{name}_track")]
    if name == "default":
        values.extend([stream.extra.get("default_track"), stream.extra.get("isDefault")])
    if name == "forced":
        values.extend([stream.extra.get("forced_track"), stream.extra.get("isForced")])
    values.append(stream.role)
    for value in values:
        if isinstance(value, bool):
            if value:
                return True
            continue
        if value is None:
            continue
        normalized = str(value).strip().lower().replace("-", " ")
        if normalized in {"1", "true", "yes", "y", "on", name}:
            return True
        if name in normalized.split():
            return True
    return False


def _mux_output_path(
    output: Path,
    save_name: str | None,
    default_save_base: str | None = None,
    mux_format: str = "mkv",
    *,
    force_suffix: bool = False,
) -> Path:
    if output.suffix and not output.is_dir():
        target = output
        if force_suffix:
            target = target.with_suffix(f".{mux_format.lstrip('.').lower()}")
        return unique_path(target)
    suffix = f".{mux_format.lstrip('.').lower()}"
    if save_name:
        base = _save_name_base(save_name)
        return unique_path(output / f"{base}{suffix}")
    return unique_path(output / f"{default_save_base or time.strftime('unidown_%Y%m%d_%H%M%S')}{suffix}")


def _cleanup_intermediate_files(paths: list[Path], protected: list[Path], args: argparse.Namespace, colors: bool | None = None) -> None:
    if getattr(args, "keep_temp", False) or not getattr(args, "del_after_done", True):
        return
    protected_keys = {_path_key(path) for path in protected}
    unique_paths: list[Path] = []
    seen = set()
    for path in paths:
        candidate = Path(path).expanduser()
        key = _path_key(candidate)
        if key in seen or key in protected_keys:
            continue
        seen.add(key)
        unique_paths.append(candidate)
    cleaned = 0
    failed: list[Path] = []
    for path in unique_paths:
        if not path.exists():
            continue
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError:
            failed.append(path)
            continue
        if path.exists():
            failed.append(path)
            continue
        cleaned += 1
    if cleaned:
        _add_cleanup_summary(args, intermediate_files=cleaned)
    if failed:
        print(f"{paint('Intermediate cleanup warning:', Palette.yellow, colors)} {len(failed)} file{'s' if len(failed) != 1 else ''} could not be removed.", file=sys.stderr)
        for path in failed[:3]:
            print(f"  {path}", file=sys.stderr)
        _log_line(args, "Intermediate cleanup warning: " + ", ".join(str(path) for path in failed))


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)


_CLEANUP_SUMMARY_LOCK = threading.Lock()


def _cleanup_replaced_intermediate(previous_path: Path, current_path: Path, args: argparse.Namespace, colors: bool | None = None) -> bool:
    if getattr(args, "keep_temp", False) or not getattr(args, "del_after_done", True):
        return False
    previous = Path(previous_path).expanduser()
    current = Path(current_path).expanduser()
    if _path_key(previous) == _path_key(current):
        return False
    if not previous.exists() or not previous.is_file():
        return False
    try:
        previous.unlink()
    except OSError:
        print(f"{paint('Intermediate cleanup warning:', Palette.yellow, colors)} could not remove {previous}", file=sys.stderr)
        _log_line(args, f"Intermediate cleanup warning: {previous}")
        return False
    if previous.exists():
        print(f"{paint('Intermediate cleanup warning:', Palette.yellow, colors)} could not remove {previous}", file=sys.stderr)
        _log_line(args, f"Intermediate cleanup warning: {previous}")
        return False
    _add_cleanup_summary(args, intermediate_files=1)
    return True


def _cleanup_track_temp_dir(temp_dir: Path | None, args: argparse.Namespace, colors: bool | None = None) -> Path | None:
    if temp_dir is None:
        return None
    if getattr(args, "keep_temp", False) or not getattr(args, "del_after_done", True):
        return temp_dir
    path = Path(temp_dir).expanduser()
    if not path.exists():
        return None
    try:
        shutil.rmtree(path)
    except OSError:
        print(f"{paint('Temp cleanup warning:', Palette.yellow, colors)} could not remove {path}", file=sys.stderr)
        _log_line(args, f"Temp cleanup warning: {path}")
        return path
    if path.exists():
        print(f"{paint('Temp cleanup warning:', Palette.yellow, colors)} could not remove {path}", file=sys.stderr)
        _log_line(args, f"Temp cleanup warning: {path}")
        return path
    _add_cleanup_summary(args, temp_dirs=1)
    return None


def _cleanup_temp_dirs(temp_dirs: list[Path], args: argparse.Namespace, colors: bool | None = None) -> None:
    if getattr(args, "keep_temp", False) or not getattr(args, "del_after_done", True):
        return
    unique_dirs = []
    seen = set()
    for temp_dir in temp_dirs:
        resolved = Path(temp_dir).expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique_dirs.append(resolved)
    cleaned = 0
    failed: list[Path] = []
    for temp_dir in unique_dirs:
        if not temp_dir.exists():
            continue
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            failed.append(temp_dir)
            continue
        if temp_dir.exists():
            failed.append(temp_dir)
            continue
        cleaned += 1
    if cleaned:
        _add_cleanup_summary(args, temp_dirs=cleaned)
    if failed:
        print(f"{paint('Temp cleanup warning:', Palette.yellow, colors)} {len(failed)} director{'y' if len(failed) == 1 else 'ies'} could not be removed.", file=sys.stderr)
        for temp_dir in failed[:3]:
            print(f"  {temp_dir}", file=sys.stderr)
        _log_line(args, "Temp cleanup warning: " + ", ".join(str(path) for path in failed))


def _add_cleanup_summary(args: argparse.Namespace, *, intermediate_files: int = 0, temp_dirs: int = 0) -> None:
    with _CLEANUP_SUMMARY_LOCK:
        summary = getattr(args, "_unidown_cleanup_summary", None)
        if not isinstance(summary, dict):
            summary = {"intermediate_files": 0, "temp_dirs": 0}
            args._unidown_cleanup_summary = summary
        summary["intermediate_files"] = int(summary.get("intermediate_files", 0)) + max(0, int(intermediate_files or 0))
        summary["temp_dirs"] = int(summary.get("temp_dirs", 0)) + max(0, int(temp_dirs or 0))


def _print_cleanup_summary(args: argparse.Namespace, colors: bool | None = None) -> None:
    summary = getattr(args, "_unidown_cleanup_summary", None)
    if not isinstance(summary, dict):
        return
    intermediate_files = max(0, int(summary.get("intermediate_files", 0) or 0))
    temp_dirs = max(0, int(summary.get("temp_dirs", 0) or 0))
    if not intermediate_files and not temp_dirs:
        return
    parts = []
    if intermediate_files:
        parts.append(f"{intermediate_files} intermediate file{'s' if intermediate_files != 1 else ''}")
    if temp_dirs:
        parts.append(f"{temp_dirs} temp director{'y' if temp_dirs == 1 else 'ies'}")
    text = ", ".join(parts)
    _print_status_line("Cleaned:", text, Palette.green, colors)
    _log_line(args, f"Cleaned: {text}")
    summary["intermediate_files"] = 0
    summary["temp_dirs"] = 0


def _apply_dash_full_base_url_mode(streams, headers: dict[str, str], no_probe: bool, colors: bool | None = None) -> int:
    converted = 0
    for stream in streams:
        if _apply_dash_full_base_url_stream(stream, headers=headers, no_probe=no_probe):
            converted += 1
            manifest_duration = format_time(stream.extra.get("dash_manifest_duration"))
            full_duration = format_time(stream.duration)
            manifest_size = format_size(stream.extra.get("dash_manifest_range_bytes"))
            full_size = format_size(stream.size_bytes)
            detail = " -> ".join(part for part in [f"MPD ranges {manifest_duration or '?'}", manifest_size, f"full BaseURL {full_duration or '?'}", full_size] if part)
            print(paint("DASH full BaseURL:", Palette.yellow, colors) + f" {stream.id or stream.name or stream.url} ({detail})")
    return converted


def _apply_dash_full_base_url_stream(stream, headers: dict[str, str], no_probe: bool) -> bool:
    full_url = _dash_full_base_url_candidate(stream)
    if not full_url:
        return False

    encrypted = stream.encrypted
    encryption_scheme = stream.encryption_scheme
    key_ids = _stream_key_ids(stream)
    key_id = key_ids[0] if key_ids else None
    manifest_duration = stream.total_duration
    manifest_range_bytes = _stream_byte_range_bytes(stream)
    original_extra = dict(stream.extra or {})

    direct_streams = parse_source(full_url, headers=headers, probe_direct=not no_probe, fetch_child_playlists=False)
    if not direct_streams:
        return False
    detail = direct_streams[0]

    stream.url = detail.url or full_url
    stream.name = stream.name or detail.name
    stream.bandwidth = detail.bandwidth or stream.bandwidth
    stream.codecs = detail.codecs or stream.codecs
    stream.resolution = detail.resolution or stream.resolution
    stream.frame_rate = detail.frame_rate or stream.frame_rate
    stream.channels = detail.channels or stream.channels
    stream.extension = detail.extension or stream.extension
    stream.duration = detail.duration or stream.duration
    stream.size_bytes = detail.size_bytes or stream.size_bytes
    stream.encrypted = encrypted
    stream.encryption_scheme = encryption_scheme
    stream.extra = original_extra
    stream.extra.update(
        {
            "dash_full_base_url": full_url,
            "dash_full_base_url_mode": True,
            "dash_manifest_duration": manifest_duration,
            "dash_manifest_range_bytes": manifest_range_bytes,
        }
    )
    if key_ids:
        stream.extra["key_ids"] = key_ids
        stream.extra["key_id"] = key_id

    stream.segments = detail.segments
    for segment in stream.segments:
        segment.encrypted = encrypted
        segment.encryption_scheme = encryption_scheme
        segment.key_id = segment.key_id or key_id
        if segment.duration is None:
            segment.duration = stream.duration
    return True


def _dash_full_base_url_candidate(stream) -> str | None:
    if getattr(stream, "manifest_type", None) != "dash" or getattr(stream, "is_live", False):
        return None
    media_segments = [segment for segment in getattr(stream, "segments", []) or [] if getattr(segment, "index", None) != -1]
    if not media_segments or not all(getattr(segment, "byte_range", None) for segment in media_segments):
        return None
    urls = {segment.url for segment in media_segments if segment.url}
    if len(urls) != 1:
        return None
    return next(iter(urls))


def _stream_byte_range_bytes(stream) -> int | None:
    total = 0
    found = False
    for segment in getattr(stream, "segments", []) or []:
        byte_range = getattr(segment, "byte_range", None)
        if not byte_range:
            continue
        start, end = byte_range
        total += max(0, end - start + 1)
        found = True
    return total if found else None


def _print_dash_byte_range_note(streams, colors: bool | None = None) -> None:
    if not any(_dash_full_base_url_candidate(stream) for stream in streams):
        return
    print(
        paint("Note:", Palette.yellow, colors)
        + " DASH byte-range VOD references one media file as multiple ranges; default downloads only the MPD ranges. Use --dash-full-base-url to download the full underlying BaseURL media."
    )


def _hydrate_stream(stream, headers: dict[str, str], no_probe: bool, base_url: str | None = None):
    if stream.segments:
        return stream
    if stream.manifest_type not in {"hls", "m3u"}:
        return stream
    detail_streams = parse_source(
        stream.url,
        headers=headers,
        probe_direct=not no_probe,
        fetch_child_playlists=True,
        base_url=base_url,
    )
    if not detail_streams:
        return stream
    detail = detail_streams[0]
    stream.segments = detail.segments
    stream.duration = detail.duration
    stream.extension = detail.extension or stream.extension
    if detail.segments:
        stream.encrypted = detail.encrypted
        stream.encryption_scheme = detail.encryption_scheme
    else:
        stream.encrypted = detail.encrypted or stream.encrypted
        stream.encryption_scheme = detail.encryption_scheme or stream.encryption_scheme
    stream.is_live = detail.is_live
    stream.extra.update(detail.extra or {})
    _filter_hls_segments_for_requested_asset(stream)
    return stream


_JSON_SELECTED_TRACK_KID_PROBE_SIZES = (4 * 1024, 16 * 1024, 64 * 1024, 256 * 1024)
_JSON_SELECTED_TRACK_KID_INIT_RANGE_PREFERENCE_THRESHOLD = 32 * 1024


def _hydrate_selected_stream_key_ids(
    stream,
    headers: dict[str, str],
    request_timeout: int,
    keys: list[RawKey] | None = None,
) -> None:
    if _is_sabr_stream(stream):
        if _should_probe_selected_sabr_stream_key_ids(stream):
            _hydrate_selected_sabr_stream_key_ids(stream, headers=headers, request_timeout=request_timeout, keys=keys)
        return
    if _stream_key_ids(stream):
        return
    source_url = _selected_probe_url(stream)
    if not source_url:
        return
    init_range = _selected_init_range(stream)
    for probe_range in _selected_key_id_probe_ranges(stream):
        try:
            init_data = fetch_segment_probe_bytes(
                SegmentInfo(url=source_url, index=-1, byte_range=probe_range),
                headers=headers,
                request_timeout=request_timeout,
            )
        except Exception:
            if not init_range:
                return
            continue
        kids = webm_key_ids_from_bytes(init_data) if _stream_uses_webm_container(stream) else mp4_tenc_default_kids_from_bytes(init_data)
        kids = [_normalize_kid_text(kid) for kid in kids]
        kids = [kid for kid in kids if kid]
        if kids:
            _apply_detected_stream_key_ids(stream, kids)
            return


def _hydrate_selected_sabr_stream_key_ids(
    stream,
    *,
    headers: dict[str, str],
    request_timeout: int,
    keys: list[RawKey] | None = None,
) -> None:
    if not keys:
        return
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    if not extra.get("sabr_ump"):
        return
    try:
        from .sabr_ump import probe_sabr_ump_key_bytes

        probe_data = probe_sabr_ump_key_bytes(
            stream,
            headers=headers,
            request_timeout=request_timeout,
            retries=2,
        )
        if not probe_data:
            return
        _apply_sniffed_sabr_container(stream, _sniff_media_container_from_bytes(probe_data))
        if _stream_uses_webm_container(stream):
            kids = webm_key_ids_from_bytes(probe_data)
        else:
            fragment_kids = fragment_cenc_key_ids_from_bytes(probe_data)
            kids = fragment_kids or mp4_tenc_default_kids_from_bytes(probe_data)
    except Exception:
        return
    kids = [_normalize_kid_text(kid) for kid in kids]
    kids = [kid for kid in kids if kid]
    if kids:
        _apply_detected_stream_key_ids(stream, kids, replace=True)
        stream.extra["sabr_kid_source"] = "sabr_media_probe"


def _should_probe_selected_sabr_stream_key_ids(stream) -> bool:
    if not _stream_key_ids(stream):
        return True
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    source = str(extra.get("sabr_kid_source") or extra.get("kid_source") or "").strip().lower()
    if not source:
        return False
    if source.startswith("pending") or source in {"direct_init", "direct_init_segment", "init_segment"}:
        return True
    if "sabr" in source and any(token in source for token in ("media", "probe", "fragment", "header")):
        return False
    return False


def _selected_key_id_probe_ranges(stream) -> list[tuple[int, int]]:
    init_range = _selected_init_range(stream)
    manifest_type = getattr(stream, "manifest_type", None)
    if manifest_type not in {"direct", "json"}:
        return [init_range] if init_range else []
    if not _selected_probe_url(stream):
        return [init_range] if init_range else []
    fallback_ranges = [(0, size - 1) for size in _JSON_SELECTED_TRACK_KID_PROBE_SIZES]
    if init_range:
        init_size = init_range[1] - init_range[0] + 1
        if init_size <= _JSON_SELECTED_TRACK_KID_INIT_RANGE_PREFERENCE_THRESHOLD:
            ranges = [init_range, *fallback_ranges]
        else:
            ranges = [*fallback_ranges, init_range]
        return list(dict.fromkeys(ranges))
    return fallback_ranges


def _selected_init_range(stream) -> tuple[int, int] | None:
    if getattr(stream, "manifest_type", None) not in {"direct", "json"}:
        return None
    raw = getattr(stream, "extra", {}).get("raw") if isinstance(getattr(stream, "extra", None), dict) else None
    if not isinstance(raw, dict):
        return None
    return _parse_json_byte_range(raw.get("init_range") or raw.get("initRange") or raw.get("initialization_range") or raw.get("initializationRange"))


def _selected_probe_url(stream) -> str | None:
    for segment in getattr(stream, "segments", []) or []:
        url = getattr(segment, "url", None)
        if url:
            return url
    return getattr(stream, "url", None) or None


_selected_json_key_id_probe_ranges = _selected_key_id_probe_ranges
_selected_json_init_range = _selected_init_range
_selected_json_probe_url = _selected_probe_url


def _parse_json_byte_range(value) -> tuple[int, int] | None:
    if isinstance(value, dict):
        start = _int_like(value.get("start"))
        end = _int_like(value.get("end"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _int_like(value[0])
        end = _int_like(value[1])
    elif isinstance(value, str) and "-" in value:
        left, right = value.split("-", 1)
        start = _int_like(left)
        end = _int_like(right)
    else:
        return None
    if start is None or end is None or end < start:
        return None
    return start, end


def _int_like(value) -> int | None:
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _apply_detected_stream_key_ids(stream, kids: list[str], *, replace: bool = False) -> None:
    unique = list(dict.fromkeys(kids))
    if not unique:
        return
    if not isinstance(getattr(stream, "extra", None), dict):
        stream.extra = {}
    existing = [] if replace else list(getattr(stream, "extra", {}).get("key_ids") or [])
    for kid in unique:
        if kid not in existing:
            existing.append(kid)
    stream.extra["key_ids"] = existing
    stream.extra["key_id"] = existing[0]
    stream.encrypted = True
    if not stream.encryption_scheme:
        stream.encryption_scheme = "CENC" if not _stream_uses_webm_container(stream) else "ENC"
    for segment in getattr(stream, "segments", []) or []:
        segment.encrypted = True
        segment.key_id = existing[0] if replace else segment.key_id or existing[0]
        if not segment.encryption_scheme:
            segment.encryption_scheme = stream.encryption_scheme


def _filter_hls_segments_for_requested_asset(stream) -> None:
    if stream.manifest_type != "hls" or not stream.segments:
        return
    asset_id = _requested_asset_id(stream.original_url)
    if not asset_id:
        return
    if _is_subtitle_stream(stream):
        matching_segments = [segment for segment in stream.segments if _hls_segment_matches_requested_asset(segment, asset_id)]
        if not matching_segments or len(matching_segments) == len(stream.segments):
            return
        removed = len(stream.segments) - len(matching_segments)
        stream.segments = matching_segments
        stream.duration = sum(segment.duration or 0 for segment in stream.segments) or stream.duration
        stream.extra["asset_filter_id"] = asset_id
        stream.extra["asset_filter_removed_segments"] = removed
        return
    groups = _segment_init_groups(stream.segments)
    matching = [group for group in groups if any(_hls_segment_matches_requested_asset(segment, asset_id) for segment in group)]
    if not matching or len(matching) == len(groups):
        return
    stream.segments = [segment for group in matching for segment in group]
    stream.duration = sum(segment.duration or 0 for segment in stream.segments) or stream.duration
    stream.encrypted = any(segment.encrypted for segment in stream.segments)
    stream.encryption_scheme = next((segment.encryption_scheme for segment in stream.segments if segment.encryption_scheme), stream.encryption_scheme)
    stream.extra["asset_filter_id"] = asset_id
    stream.extra["asset_filter_removed_sections"] = len(groups) - len(matching)


def _hls_segment_matches_requested_asset(segment, asset_id: str) -> bool:
    # Apple HLS uses _HLS_prefill query values pointing at the next asset.  Those
    # prefetch hints must not pull the previous recap asset into the selected
    # program, so match only the actual URL path.
    token = f"P{asset_id}_"
    path = unquote(urlparse(getattr(segment, "url", "") or "").path)
    return token in path


def _requested_asset_id(url: str | None) -> str | None:
    if not url:
        return None
    values = parse_qs(urlparse(url).query).get("id")
    if not values:
        return None
    value = values[0].strip()
    return value if value.isdigit() else None


def _segment_init_groups(segments) -> list[list]:
    groups: list[list] = []
    current: list = []
    for segment in segments:
        if segment.index == -1 and current:
            groups.append(current)
            current = []
        current.append(segment)
    if current:
        groups.append(current)
    return groups
