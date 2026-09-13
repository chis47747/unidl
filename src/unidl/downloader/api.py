"""Programmatic API for UniDL's native downloader.

This is a thin facade over :mod:`unidl.downloader.cli`. It deliberately contains
no download, decrypt or mux logic of its own: it builds the same ``argparse``
namespace as the transitional command renderer and calls the same execution
entry point, so there is exactly one implementation of downloader behaviour.

Intended use by a front-end:

    streams = api.load_streams(api.ParseOptions(input=url, headers=hdrs))
    chosen  = my_ui.pick(streams)
    api.download(streams, chosen, api.DownloadOptions(input=url, save_name="X"))

Passing the already parsed ladder into :func:`download` avoids a second network
parse and keeps selection indexes aligned with whatever the front-end showed.
"""

from __future__ import annotations

import argparse
import contextlib
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import cli
from .embedding import (
    DownloadArtifact,
    DownloadCancelled,
    DownloadHooks,
    DownloadMessage,
    DownloadProgress,
    DownloadRuntime,
    LiveKeyProvider,
    LiveKeyRequest,
)
from .loader import normalize_headers
from .models import StreamInfo
from .parser import parse_source

__all__ = [
    "DownloadArtifact",
    "DownloadCancelled",
    "DownloadHooks",
    "DownloadMessage",
    "DownloaderArgumentError",
    "DownloadProgress",
    "DownloadRuntime",
    "DownloadOptions",
    "LiveKeyProvider",
    "LiveKeyRequest",
    "ParseOptions",
    "audio_formats",
    "build_argv",
    "option_choices",
    "command_line",
    "download",
    "key_ids",
    "load_streams",
    "stream_key_ids",
]


def _header_args(headers: dict[str, str] | None) -> list[str]:
    return [arg for name, value in (headers or {}).items() for arg in ("-H", f"{name}: {value}")]


@dataclass
class ParseOptions:
    input: str
    headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None
    use_system_proxy: bool = True
    details: bool = False
    no_child_playlists: bool = False
    no_probe: bool = False
    base_url: str | None = None
    append_url_params: bool = False
    ad_keywords: list[str] = field(default_factory=list)
    drop_video: str | None = None
    drop_audio: str | None = None
    drop_subtitle: str | None = None

    def _drop_args(self) -> list[str]:
        argv: list[str] = []
        for flag, value in (("-dv", self.drop_video), ("-da", self.drop_audio), ("-ds", self.drop_subtitle)):
            if value:
                argv += [flag, value]
        return argv

    def argv(self) -> list[str]:
        argv = ["list", self.input, *_header_args(self.headers), *self._drop_args()]
        if self.proxy:
            argv += ["--custom-proxy", self.proxy]
        if not self.use_system_proxy:
            argv.append("--no-use-system-proxy")
        if self.details:
            argv.append("--details")
        if self.no_child_playlists:
            argv.append("--no-child-playlists")
        if self.no_probe:
            argv.append("--no-probe")
        if self.base_url:
            argv += ["--base-url", self.base_url]
        if self.append_url_params:
            argv.append("--append-url-params")
        for keyword in self.ad_keywords:
            argv += ["--ad-keyword", keyword]
        return argv


@dataclass
class DownloadOptions(ParseOptions):
    save_name: str = ""
    save_dir: str | None = None
    output: str | None = None
    keys: list[str] = field(default_factory=list)
    key_text_file: str | None = None
    service_context: dict[str, object] = field(default_factory=dict)

    workers: int | None = None
    retries: int | None = None
    concurrent_tracks: bool = True
    max_speed: str | None = None
    http_request_timeout: int | None = None
    check_segments_count: bool | None = None
    no_resume: bool = False
    downloader: str | None = None

    mux: bool | None = None
    mux_format: str | None = None
    muxer: str | None = None
    mux_imports: list[str] = field(default_factory=list)
    #: Neutral UniDL JSON chapter document, converted for the selected muxer.
    chapters_file: str | None = None

    sub_format: str | None = None
    auto_subtitle_fix: bool | None = None
    sub_only: bool = False
    audio_format: str | None = None
    #: JSON sidecar with ID3 tags and cover art; requires audio_format
    audio_metadata_file: str | None = None
    decode_audio_vivid: bool = False
    audio_vivid_decoder: str | None = None
    audio_vivid_decoder_args: str | None = None

    no_decrypt: bool = False
    decrypter: str | None = None
    custom_hls_method: str | None = None
    custom_hls_key: str | None = None
    custom_hls_iv: str | None = None
    #: In-process stateful decryptor; deliberately omitted from rendered argv.
    hls_decryptor: object | None = None

    tmp_dir: str | None = None
    log_file_path: str | None = None
    write_meta_json: bool = False
    no_color: bool = True
    keep_temp: bool = False
    no_del_after_done: bool = False

    # live
    is_live: bool = False
    live_record_limit: str | None = None
    live_real_time_merge: bool | None = None
    live_keep_segments: bool | None = None
    live_pipe_mux: bool | None = None
    live_perform_as_vod: bool = False
    live_dvr_from_start: bool | None = None
    live_dvr_start_at: str | None = None
    live_dvr_end_at: str | None = None

    #: raw passthrough for anything not modelled above
    extra_args: list[str] = field(default_factory=list)

    def argv(self) -> list[str]:
        argv = ["download", self.input, *_header_args(self.headers), *self._drop_args()]
        if self.save_name:
            argv += ["--save-name", self.save_name]
        if self.save_dir:
            argv += ["--save-dir", self.save_dir]
        if self.output:
            argv += ["-o", self.output]
        for key in self.keys:
            argv += ["--key", key]
        if self.key_text_file:
            argv += ["--key-text-file", self.key_text_file]

        if self.workers is not None:
            argv += ["--workers", str(self.workers)]
        if self.retries is not None:
            argv += ["--retries", str(self.retries)]
        if self.concurrent_tracks:
            argv.append("-mt")
        if self.max_speed:
            argv += ["-R", self.max_speed]
        if self.http_request_timeout is not None:
            argv += ["--http-request-timeout", str(self.http_request_timeout)]
        if self.check_segments_count is True:
            argv.append("--check-segments-count")
        elif self.check_segments_count is False:
            argv.append("--no-check-segments-count")
        if self.no_resume:
            argv.append("--no-resume")
        if self.downloader:
            argv += ["--downloader", self.downloader]

        if self.mux is True:
            argv.append("--mux")
        elif self.mux is False:
            argv.append("--no-mux")
        if self.mux_format:
            argv += ["--mux-format", self.mux_format]
        if self.muxer:
            argv += ["--muxer", self.muxer]
        for value in self.mux_imports:
            argv += ["--mux-import", value]
        if self.chapters_file:
            argv += ["--chapters-file", self.chapters_file]

        if self.sub_format:
            argv += ["--sub-format", self.sub_format]
        if self.auto_subtitle_fix is True:
            argv.append("--auto-subtitle-fix")
        elif self.auto_subtitle_fix is False:
            argv.append("--no-auto-subtitle-fix")
        if self.sub_only:
            argv.append("--sub-only")
        if self.audio_format:
            argv += ["--audio-format", self.audio_format]
        if self.audio_metadata_file and self.audio_format:
            # the flag is rejected without a format, so pairing them here keeps
            # a caller from building an argv that cannot run
            argv += ["--audio-metadata-file", self.audio_metadata_file]
        if self.decode_audio_vivid:
            argv.append("--decode-audio-vivid")
        if self.audio_vivid_decoder:
            argv += ["--audio-vivid-decoder", self.audio_vivid_decoder]
        if self.audio_vivid_decoder_args:
            argv.append(f"--audio-vivid-decoder-args={self.audio_vivid_decoder_args}")

        if self.no_decrypt:
            argv.append("--no-decrypt")
        if self.decrypter:
            argv += ["--decrypter", self.decrypter]
        if self.custom_hls_method:
            argv += ["--custom-hls-method", self.custom_hls_method]
        if self.custom_hls_key:
            argv += ["--custom-hls-key", self.custom_hls_key]
        if self.custom_hls_iv:
            argv += ["--custom-hls-iv", self.custom_hls_iv]

        if self.tmp_dir:
            argv += ["--tmp-dir", self.tmp_dir]
        if self.log_file_path:
            argv += ["--log-file-path", self.log_file_path]
        if self.write_meta_json:
            argv.append("--write-meta-json")
        if self.no_color:
            argv.append("--no-color")
        if self.keep_temp:
            argv.append("--keep-temp")
        if self.no_del_after_done:
            argv.append("--no-del-after-done")

        if self.proxy:
            argv += ["--custom-proxy", self.proxy]
        if not self.use_system_proxy:
            argv.append("--no-use-system-proxy")
        if self.details:
            argv.append("--details")
        if self.no_child_playlists:
            argv.append("--no-child-playlists")
        if self.no_probe:
            argv.append("--no-probe")
        if self.base_url:
            argv += ["--base-url", self.base_url]
        if self.append_url_params:
            argv.append("--append-url-params")
        for keyword in self.ad_keywords:
            argv += ["--ad-keyword", keyword]

        if self.live_record_limit:
            argv += ["--live-record-limit", self.live_record_limit]
        for flag, value in (
            ("--live-real-time-merge", self.live_real_time_merge),
            ("--live-keep-segments", self.live_keep_segments),
            ("--live-pipe-mux", self.live_pipe_mux),
            ("--live-dvr-from-start", self.live_dvr_from_start),
        ):
            if value is not None:
                argv += [flag, "true" if value else "false"]
        if self.live_perform_as_vod:
            argv.append("--live-perform-as-vod")
        if self.live_dvr_start_at:
            argv += ["--live-dvr-start-at", self.live_dvr_start_at]
        if self.live_dvr_end_at:
            argv += ["--live-dvr-end-at", self.live_dvr_end_at]

        argv += list(self.extra_args)
        return argv


def audio_formats() -> list[str]:
    """Which values ``--audio-format`` accepts, read off the parser itself.

    Exposed so a caller can offer exactly what this build supports rather than
    hard-coding a list that goes stale the moment a codec is added or removed.
    """
    return option_choices("--audio-format")


def option_choices(flag: str) -> list[str]:
    """The declared choices for one download option, or an empty list.

    The options live on the ``download`` subparser rather than the top-level one,
    so this walks into the subparsers to find them.
    """
    parser = cli._build_parser()
    parsers = [parser]
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            parsers.extend(choices.values())
    for candidate in parsers:
        for action in candidate._actions:
            if flag in getattr(action, "option_strings", ()):
                return [str(choice) for choice in (action.choices or [])]
    return []


class DownloaderArgumentError(ValueError):
    """Typed API options could not be represented by the native parser."""


class _ApiArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DownloaderArgumentError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise DownloaderArgumentError(message or f"argument parser exited with {status}")
        return super().exit(status, message)


def _namespace(argv: Sequence[str]):
    normalized = cli._split_attached_short_flags(list(argv))
    normalized = cli._normalize_bool_option_values(normalized)
    return cli._build_parser(parser_class=_ApiArgumentParser).parse_args(normalized)


def load_streams(options: ParseOptions) -> list[StreamInfo]:
    """Parse a manifest and return the ladder exactly as ``download`` sees it.

    The pipeline mirrors ``cli._download`` step for step (parse, optional URL
    param propagation, ad filtering, drop patterns, sort) so the resulting
    order is the one selection indexes refer to.
    """
    args = _namespace(options.argv())
    cli._configure_proxy(args)
    headers = normalize_headers(args.header)
    args.append_url_params = args.append_url_params or cli.should_append_child_url_params(args.input)
    streams = parse_source(
        args.input,
        headers=headers,
        probe_direct=not args.no_probe,
        fetch_child_playlists=args.details and not args.no_child_playlists,
        base_url=args.base_url,
    )
    if args.append_url_params:
        cli._append_input_params(streams, args.input)
    cli._filter_ad_segments(streams, args.ad_keyword, args=args, colors=False)
    return cli._sort_streams(cli._drop_streams(streams, args))


def build_argv(
    options: DownloadOptions,
    streams: Sequence[StreamInfo] | None = None,
    selected: Sequence[StreamInfo] | None = None,
) -> list[str]:
    """The equivalent shell command, for display or export."""
    argv = options.argv()
    indexes = _selection_indexes(streams, selected)
    if indexes:
        argv += ["--select", ",".join(str(i) for i in indexes)]
    return argv


def command_line(
    options: DownloadOptions,
    streams: Sequence[StreamInfo] | None = None,
    selected: Sequence[StreamInfo] | None = None,
) -> str:
    return "unidl " + " ".join(
        shlex.quote(part) for part in build_argv(options, streams, selected)
    )


def stream_key_ids(stream: StreamInfo) -> list[str]:
    """Normalized KIDs declared by a parsed stream.

    Exposed so an embedding front-end can consult a key vault before running a
    license exchange, instead of reaching into cli internals.
    """
    return list(cli._stream_key_ids(stream))


def key_ids(streams: Sequence[StreamInfo]) -> list[str]:
    """Distinct KIDs across several streams, in first-seen order."""
    seen: dict[str, None] = {}
    for stream in streams:
        for kid in stream_key_ids(stream):
            seen.setdefault(kid, None)
    return list(seen)


def _selection_indexes(
    streams: Sequence[StreamInfo] | None,
    selected: Sequence[StreamInfo] | None,
) -> list[int]:
    if not streams or not selected:
        return []
    positions = {id(stream): index for index, stream in enumerate(streams, start=1)}
    return sorted(positions[id(stream)] for stream in selected if id(stream) in positions)


def download(
    options: DownloadOptions,
    streams: Sequence[StreamInfo] | None = None,
    selected: Sequence[StreamInfo] | None = None,
    *,
    hooks: DownloadHooks | None = None,
) -> int:
    """Run a download. Returns the native engine's exit code (0 on success).

    When ``streams`` is supplied it is used verbatim instead of re-parsing, and
    ``selected`` must be members of it.
    """
    argv = options.argv()
    indexes = _selection_indexes(streams, selected)
    if indexes:
        argv += ["--select", ",".join(str(i) for i in indexes)]
    args = _namespace(argv)
    args.embedding_hooks = hooks or DownloadHooks()
    args.service_context = dict(options.service_context)
    # Stateful service-owned HLS decryptors cannot be represented in argv. The
    # typed embedding API carries the live object alongside serializable options.
    args.hls_decryptor = options.hls_decryptor
    cancel_requested = args.embedding_hooks.cancel_requested
    if cancel_requested is not None and cancel_requested():
        raise DownloadCancelled("download cancelled before it started")
    if streams is not None:
        args.preparsed_streams = list(streams)
    runtime = args.embedding_hooks.runtime
    runtime_scope = runtime.activate() if runtime is not None else contextlib.nullcontext()
    with runtime_scope, cli._embedding_output(args.embedding_hooks):
        return cli._download(args)
