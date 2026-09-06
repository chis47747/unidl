"""Native implementation of UniDL's Core delivery contract.

The implementation keeps its rich stream objects and transitional integer exit
codes behind the typed Core boundary.  It is packaged inside UniDL and has no
runtime dependency on the former standalone downloader application.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ..core.delivery import (
    ArtifactEvent,
    DeliveryFailure,
    DeliveryHooks,
    DeliveryPlan,
    DeliveryResult,
    DeliverySource,
    DeliveryStage,
    DeliveryStatus,
    LiveKeyRequest,
    LiveSegmentDescriptor,
    MessageEvent,
    MessageLevel,
    OutputArtifact,
    ParsedManifest,
    ParseRequest,
    SourceKind,
    StageEvent,
    TrackDescriptor,
    TrackProgressEvent,
)
from ..core.secureio import (
    atomic_write_text,
    locked_path,
    private_directory,
    private_file,
)
from . import api
from .embedding import DownloadRuntime
from .http_client import close_http2_clients, get_global_http_client
from .models import SegmentInfo, StreamInfo
from .utils import looks_like_h266


class NativeManifestError(ValueError):
    """The bundled parser rejected a typed manifest request."""


def _contains_vvc_video(streams: Sequence[StreamInfo]) -> bool:
    return any(
        stream.media_type == "video"
        and looks_like_h266(stream.codecs, stream.url, stream.name)
        for stream in streams
    )


class NativeDownloaderBackend:
    """Run UniDL's bundled download engine behind the typed Core contract."""

    def __init__(self) -> None:
        self._runtime_lock = threading.RLock()
        self._active_runtime: DownloadRuntime | None = None

    def shutdown_active_download(self) -> None:
        """Hard-stop the currently embedded delivery, if there is one."""
        with self._runtime_lock:
            runtime = self._active_runtime
        if runtime is not None:
            runtime.shutdown()

    def _forget_runtime(self, runtime: DownloadRuntime) -> None:
        with self._runtime_lock:
            if self._active_runtime is runtime:
                self._active_runtime = None

    def parse(self, request: ParseRequest) -> ParsedManifest:
        source = self._materialize(request)
        policy = request.policy
        try:
            streams = api.load_streams(
                api.ParseOptions(
                    input=source,
                    headers=dict(policy.headers),
                    proxy=policy.proxy,
                    use_system_proxy=policy.use_system_proxy,
                    details=policy.details,
                    no_child_playlists=policy.no_child_playlists,
                    no_probe=policy.no_probe,
                    base_url=policy.base_url,
                    append_url_params=policy.append_url_params,
                    ad_keywords=list(policy.ad_keywords),
                    drop_video=policy.drop_video,
                    drop_audio=policy.drop_audio,
                    drop_subtitle=policy.drop_subtitle,
                )
            )
        except (SystemExit, api.DownloaderArgumentError) as exc:
            code = getattr(exc, "code", None)
            detail = f"exit code {code}" if isinstance(code, int) else str(code or exc)
            raise NativeManifestError(
                f"native downloader rejected manifest parser options ({detail})"
            ) from exc
        return self.adopt(request, streams)

    @staticmethod
    def key_ids(streams: Sequence[StreamInfo]) -> list[str]:
        """Return distinct content KIDs without exposing the CLI facade to Core."""
        return api.key_ids(streams)

    @staticmethod
    def audio_formats() -> list[str]:
        """Formats supported by the bundled native audio post-processor."""
        return api.audio_formats()

    def adopt(
        self,
        request: ParseRequest,
        streams: Sequence[StreamInfo],
    ) -> ParsedManifest:
        """Bind an already parsed native ladder to stable Core track IDs.

        This is the migration seam for UniDL's existing track picker. New Core
        callers use :meth:`parse`; both paths produce the same manifest shape.
        """
        state = tuple(streams)
        tracks = tuple(
            self._describe(stream, index)
            for index, stream in enumerate(state, start=1)
        )
        return ParsedManifest(request, tracks, state)

    def merge(self, manifests: Sequence[ParsedManifest]) -> ParsedManifest:
        """Merge authorized ladders into one runnable JSON-backed manifest.

        A service may return one authorized manifest per requested resolution.  The
        native parser normally owns each manifest separately, so simply concatenating
        its ``StreamInfo`` objects would make command export and a later re-parse
        disagree.  We therefore serialize the already parsed streams into UniDL's
        typed JSON manifest shape, then keep the native objects as the execution
        state.  No URL or host is manufactured here; every stream came from one of
        the service-provided manifests.
        """
        if not manifests:
            raise ValueError("cannot merge an empty manifest collection")
        first = manifests[0]
        for manifest in manifests[1:]:
            if manifest.request.policy != first.request.policy:
                raise ValueError(
                    "authorized manifest variants must share headers, proxy and "
                    "parser policy before their tracks can be merged"
                )
        all_streams: list[StreamInfo] = []
        seen: set[tuple[object, ...]] = set()
        for manifest in manifests:
            for stream in self.streams(manifest):
                # A provider may assign a different representation id on each
                # playback response even though the authorized media URL and its
                # properties are identical.  The id is therefore only an identity
                # fallback for URL-less records; including it unconditionally made
                # the same audio rendition appear once per requested profile.
                source_identity = (
                    ("url", str(stream.url or ""))
                    if stream.url
                    else (
                        "id",
                        str(stream.id or ""),
                        str(stream.group_id or ""),
                    )
                )
                fingerprint = (
                    str(stream.media_type or "").lower(),
                    source_identity,
                    str(stream.language or ""),
                    str(stream.role or ""),
                    str(stream.resolution or ""),
                    str(stream.codecs or ""),
                    int(stream.bandwidth or 0),
                    str(stream.channels or ""),
                    tuple(api.stream_key_ids(stream)),
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                all_streams.append(stream)
        if not all_streams:
            # Keep the source shape valid even when a candidate was an empty
            # manifest; the caller can report an empty ladder consistently.
            all_streams = []

        document: dict[str, object] = {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
        }
        buckets = {
            "video": document["video_tracks"],
            "audio": document["audio_tracks"],
            "subtitle": document["subtitle_tracks"],
        }
        for stream in all_streams:
            media = str(stream.media_type or "").lower()
            bucket = buckets.get(media)
            if not isinstance(bucket, list):
                continue
            bucket.append(self._json_track(stream))

        request = ParseRequest(
            DeliverySource.from_json(document),
            policy=first.request.policy,
            scratch_dir=first.request.scratch_dir,
        )
        return self.adopt(request, all_streams)

    def merge_segments(
        self,
        manifests: Sequence[ParsedManifest],
        *,
        duration: float = 0.0,
    ) -> ParsedManifest:
        """Join overlapping finite media windows by absolute segment sequence."""
        if not manifests:
            raise ValueError("cannot merge an empty manifest collection")
        first = manifests[0]
        for manifest in manifests[1:]:
            if manifest.request.policy != first.request.policy:
                raise ValueError(
                    "authorized timeline windows must share headers, proxy and parser policy"
                )

        groups: dict[tuple[object, ...], list[StreamInfo]] = {}
        group_order: list[tuple[object, ...]] = []
        for manifest in manifests:
            for stream in self.streams(manifest):
                key = self._timeline_stream_key(stream)
                if key not in groups:
                    groups[key] = []
                    group_order.append(key)
                groups[key].append(stream)

        merged_streams = [
            self._merge_timeline_streams(groups[key], duration=duration)
            for key in group_order
        ]
        document: dict[str, object] = {
            "video_tracks": [],
            "audio_tracks": [],
            "subtitle_tracks": [],
        }
        buckets = {
            "video": document["video_tracks"],
            "audio": document["audio_tracks"],
            "subtitle": document["subtitle_tracks"],
        }
        for stream in merged_streams:
            bucket = buckets.get(str(stream.media_type or "").lower())
            if isinstance(bucket, list):
                bucket.append(self._json_track(stream))
        request = ParseRequest(
            DeliverySource.from_json(document),
            policy=first.request.policy,
            scratch_dir=first.request.scratch_dir,
        )
        return self.adopt(request, merged_streams)

    @staticmethod
    def _timeline_stream_key(stream: StreamInfo) -> tuple[object, ...]:
        return (
            str(stream.media_type or "").lower(),
            str(stream.language or ""),
            str(stream.role or ""),
            str(stream.resolution or ""),
            str(stream.codecs or ""),
            int(stream.bandwidth or 0),
            str(stream.channels or ""),
            str(stream.extension or ""),
            str(stream.video_range or ""),
            bool(stream.extra.get("muxed_audio")),
            bool(stream.encrypted),
            str(stream.encryption_scheme or ""),
        )

    @staticmethod
    def _segment_identity(segment: SegmentInfo) -> tuple[object, ...]:
        if segment.index is not None:
            return ("sequence", int(segment.index))
        if segment.program_date_time:
            return ("time", str(segment.program_date_time))
        return ("url", str(segment.url), segment.byte_range)

    def _merge_timeline_streams(
        self,
        streams: Sequence[StreamInfo],
        *,
        duration: float,
    ) -> StreamInfo:
        if not streams:
            raise ValueError("cannot merge an empty timeline stream collection")
        base = streams[0]
        initial: SegmentInfo | None = None
        media: dict[tuple[object, ...], SegmentInfo] = {}
        for stream in streams:
            for segment in stream.segments:
                if segment.index == -1:
                    initial = initial or segment
                    continue
                media.setdefault(self._segment_identity(segment), segment)

        segments = list(media.values())
        if segments and all(segment.index is not None for segment in segments):
            segments.sort(key=lambda segment: int(segment.index or 0))
            sequences = [int(segment.index or 0) for segment in segments]
            missing = [
                (left, right)
                for left, right in zip(sequences, sequences[1:], strict=False)
                if right > left + 1
            ]
            if missing:
                left, right = missing[0]
                raise ValueError(
                    f"authorized timeline is incomplete between media sequences {left} and {right}"
                )

        target = max(0.0, float(duration or 0.0))
        if target > 0:
            selected: list[SegmentInfo] = []
            elapsed = 0.0
            for segment in segments:
                if elapsed >= target:
                    break
                selected.append(segment)
                elapsed += max(0.0, float(segment.duration or 0.0))
            segments = selected
            tolerance = max(
                1.0,
                float(base.extra.get("target_duration") or 0.0),
                max((float(segment.duration or 0.0) for segment in segments), default=0.0),
            )
            if elapsed + tolerance < target:
                raise ValueError(
                    f"authorized timeline is truncated: got {elapsed:.1f}s of {target:.1f}s"
                )

        if initial is not None:
            segments.insert(0, initial)
        total_duration = sum(
            max(0.0, float(segment.duration or 0.0))
            for segment in segments
            if segment.index != -1
        )
        extra = dict(base.extra)
        media_sequences = [
            int(segment.index)
            for segment in segments
            if segment.index is not None and segment.index != -1
        ]
        if media_sequences:
            extra["media_sequence"] = media_sequences[0]
        return replace(
            base,
            duration=total_duration or base.duration,
            is_live=False,
            segments=list(segments),
            extra=extra,
        )

    @staticmethod
    def _json_track(stream: StreamInfo) -> dict[str, object]:
        """Represent one native stream in the parser's portable JSON dialect."""
        record: dict[str, object] = {
            "url": stream.url,
            "id": stream.id,
            "group_id": stream.group_id,
            "name": stream.name,
            "language": stream.language,
            "role": stream.role,
            "bandwidth": stream.bandwidth,
            "codecs": stream.codecs,
            "resolution": stream.resolution,
            "frame_rate": stream.frame_rate,
            "channels": stream.channels,
            "extension": stream.extension,
            "video_range": stream.video_range,
            "duration": stream.total_duration,
            "size_bytes": stream.estimated_size_bytes,
            "encrypted": stream.encrypted,
            "encryption_scheme": stream.encryption_scheme,
            "is_live": stream.is_live,
        }
        kids = api.stream_key_ids(stream)
        if kids:
            record["kid"] = kids[0]
        if stream.segments:
            record["segments"] = [
                {
                    "url": segment.url,
                    "duration": segment.duration,
                    "index": segment.index,
                    "range": (
                        f"{segment.byte_range[0]}-{segment.byte_range[1]}"
                        if segment.byte_range
                        else None
                    ),
                    "encrypted": segment.encrypted,
                    "encryption_scheme": segment.encryption_scheme,
                    "kid": segment.key_id,
                }
                for segment in stream.segments
            ]
        # Only retain flags consumed by the native formatter; arbitrary service
        # metadata can contain non-JSON objects and is not needed to download.
        for key in ("audio_atmos", "muxed_audio", "dash_full_base_url_mode"):
            if key in stream.extra:
                record[key] = stream.extra[key]
        raw = stream.extra.get("raw")
        if isinstance(raw, dict) and isinstance(raw.get("deezer"), dict):
            record["deezer"] = dict(raw["deezer"])
        return {key: value for key, value in record.items() if value is not None}

    @staticmethod
    def streams(manifest: ParsedManifest) -> list[StreamInfo]:
        """Expose native tracks only to UniDL's transitional picker."""
        state = manifest.backend_state
        if not isinstance(state, tuple) or not all(
            isinstance(stream, StreamInfo) for stream in state
        ):
            raise TypeError("manifest was not parsed by this backend")
        return list(state)

    def command_line(self, plan: DeliveryPlan) -> str:
        options, streams, selected = self._execution(plan)
        return api.command_line(options, streams, selected)

    def run(self, plan: DeliveryPlan, hooks: DeliveryHooks) -> DeliveryResult:
        hooks.cancellation.raise_if_cancelled()
        hooks.emit(StageEvent(DeliveryStage.PREPARING, plan.save_name))
        options, streams, selected = self._execution(plan)
        hooks.emit(StageEvent(DeliveryStage.DOWNLOADING, plan.save_name))
        track_ids = {
            id(stream): track.track_id
            for track, stream in zip(
                plan.manifest.tracks,
                streams,
                strict=True,
            )
        }
        tracks_by_id = {track.track_id: track for track in plan.manifest.tracks}

        def request_live_key(request: api.LiveKeyRequest) -> str | None:
            track_id = track_ids.get(
                id(request.stream),
                str(getattr(request.stream, "id", "") or "unknown"),
            )
            segment = request.segment
            segment_index = getattr(segment, "index", None) if segment else None
            return hooks.request_live_key(
                LiveKeyRequest(
                    kid=request.kid,
                    track_id=track_id,
                    reason=request.reason,
                    segment_index=segment_index,
                    track=tracks_by_id.get(track_id),
                    segment=(
                        LiveSegmentDescriptor(
                            url=str(getattr(segment, "url", "") or ""),
                            index=segment_index,
                            duration=getattr(segment, "duration", None),
                            byte_range=getattr(segment, "byte_range", None),
                            encrypted=bool(getattr(segment, "encrypted", False)),
                            encryption_scheme=str(
                                getattr(segment, "encryption_scheme", "") or ""
                            ),
                            key_id=getattr(segment, "key_id", None),
                            key_uri=getattr(segment, "key_uri", None),
                            program_date_time=getattr(
                                segment,
                                "program_date_time",
                                None,
                            ),
                        )
                        if segment is not None
                        else None
                    ),
                    require_kid=request.require_kid,
                    force=request.force,
                    replace_existing=request.replace_existing,
                )
            )

        artifacts: list[OutputArtifact] = []

        def artifact_created(artifact: api.DownloadArtifact) -> None:
            output = OutputArtifact(
                artifact.path,
                kind=artifact.kind,
                track_id=(
                    track_ids.get(id(artifact.stream))
                    if artifact.stream is not None
                    else None
                ),
            )
            artifacts.append(output)
            hooks.emit(ArtifactEvent(output))

        def progress(update: api.DownloadProgress) -> None:
            track_id = track_ids.get(
                id(update.stream),
                str(getattr(update.stream, "id", "") or "unknown"),
            )
            hooks.emit(
                TrackProgressEvent(
                    track_id=track_id,
                    completed_segments=update.completed_segments,
                    total_segments=update.total_segments,
                    downloaded_bytes=update.downloaded_bytes,
                    total_bytes=update.total_bytes,
                    elapsed_seconds=update.elapsed_seconds,
                    speed_bytes_per_second=update.speed_bytes_per_second,
                    eta_seconds=update.eta_seconds,
                    live=update.live,
                    recorded_seconds=update.recorded_seconds,
                    duration_seconds=update.duration_seconds,
                    status=update.status,
                    done=update.done,
                )
            )

        def message(update: api.DownloadMessage) -> None:
            try:
                level = MessageLevel(update.level)
            except ValueError:
                level = MessageLevel.INFO
            hooks.emit(
                MessageEvent(
                    update.text,
                    level=level,
                    transient=update.transient,
                )
            )

        runtime = DownloadRuntime()
        with self._runtime_lock:
            self._active_runtime = runtime
        runtime.register_closer(get_global_http_client().close)
        runtime.register_closer(close_http2_clients)
        decryptor_close = getattr(options.hls_decryptor, "close", None)
        if callable(decryptor_close):
            runtime.register_closer(decryptor_close)
        download_hooks = api.DownloadHooks(
            live_key_provider=(request_live_key if plan.policy.live.enabled else None),
            artifact_created=artifact_created,
            progress=progress,
            message=message,
            cancel_requested=lambda: hooks.cancellation.cancelled or runtime.stopped,
            pause_requested=lambda: hooks.pause.paused,
            runtime=runtime,
            display_width=hooks.display_width,
            console_progress=False,
            console_output=False,
        )
        try:
            with runtime.activate():
                exit_code = api.download(
                    options,
                    streams,
                    selected,
                    hooks=download_hooks,
                )
        except (api.DownloadCancelled, KeyboardInterrupt):
            self._forget_runtime(runtime)
            return DeliveryResult(DeliveryStatus.CANCELLED, exit_code=130)
        except SystemExit as exc:
            self._forget_runtime(runtime)
            value = exc.code
            if value is None:
                exit_code = 0
            elif isinstance(value, int):
                exit_code = value
            else:
                exit_code = 1
            if exit_code == 0:
                return DeliveryResult(DeliveryStatus.SUCCEEDED, exit_code=0)
            return DeliveryResult(
                DeliveryStatus.FAILED,
                failure=DeliveryFailure(
                    DeliveryStage.DOWNLOADING,
                    str(value or f"Downloader exited with code {exit_code}"),
                    code=f"downloader_exit_{exit_code}",
                ),
                exit_code=exit_code,
            )
        except Exception as exc:  # implementation exceptions become Core data
            self._forget_runtime(runtime)
            if hooks.cancellation.cancelled or runtime.stopped:
                return DeliveryResult(DeliveryStatus.CANCELLED, exit_code=130)
            return DeliveryResult(
                DeliveryStatus.FAILED,
                failure=DeliveryFailure(
                    DeliveryStage.DOWNLOADING,
                    str(exc),
                    code=type(exc).__name__,
                ),
                exit_code=1,
            )
        self._forget_runtime(runtime)
        if exit_code == 130 or hooks.cancellation.cancelled or runtime.stopped:
            return DeliveryResult(DeliveryStatus.CANCELLED, exit_code=130)
        if exit_code != 0:
            return DeliveryResult(
                DeliveryStatus.FAILED,
                failure=DeliveryFailure(
                    DeliveryStage.DOWNLOADING,
                    f"Downloader exited with code {exit_code}",
                    code=f"downloader_exit_{exit_code}",
                ),
                exit_code=exit_code,
            )

        # Old/fake implementations may not publish artifacts yet. Keep an honest
        # directory fallback until every backend satisfies the new callback.
        if not artifacts:
            artifacts.append(OutputArtifact(plan.output_dir, kind="output_directory"))
        hooks.emit(StageEvent(DeliveryStage.COMPLETE, plan.save_name))
        return DeliveryResult(
            DeliveryStatus.SUCCEEDED,
            tuple(artifacts),
            exit_code=0,
        )

    def _execution(
        self,
        plan: DeliveryPlan,
    ) -> tuple[api.DownloadOptions, list[StreamInfo], list[StreamInfo]]:
        state = plan.manifest.backend_state
        if not isinstance(state, tuple) or not all(
            isinstance(stream, StreamInfo) for stream in state
        ):
            raise TypeError("manifest was not parsed by this backend")
        streams = list(state)
        positions = {
            track.track_id: stream
            for track, stream in zip(plan.manifest.tracks, streams, strict=True)
        }
        selected = [positions[track_id] for track_id in plan.selected_track_ids]
        parse = plan.manifest.request.policy
        policy = plan.policy
        live = policy.live
        mux_format = policy.mux_format
        muxer = policy.muxer
        vvc_vod = (
            not live.enabled
            and policy.mux is not False
            and _contains_vvc_video(selected)
        )
        output = policy.output
        if vvc_vod:
            # Keep command export and in-process execution on the same known-good
            # VVC path. The native FFmpeg MP4 muxer writes vvc1; mkvmerge may
            # serialize the same track as V_QUICKTIME.
            mux_format = "mp4"
            muxer = "ffmpeg"
            if output is not None and output.suffix:
                output = output.with_suffix(".mp4")
        extra_args: list[str] = []
        if policy.custom_range:
            extra_args += ["--custom-range", policy.custom_range]
        if policy.allow_hls_multi_ext_map:
            extra_args.append("--allow-hls-multi-ext-map")
        if policy.vgc:
            extra_args.append("--vgc")
        if policy.vgc_keep_opaque:
            extra_args.append("--vgc-keep-opaque")
        options = api.DownloadOptions(
            input=self._materialize(plan.manifest.request),
            headers=dict(parse.headers),
            proxy=parse.proxy,
            use_system_proxy=parse.use_system_proxy,
            details=parse.details,
            no_child_playlists=parse.no_child_playlists,
            no_probe=parse.no_probe,
            base_url=parse.base_url,
            append_url_params=parse.append_url_params,
            ad_keywords=list(parse.ad_keywords),
            drop_video=parse.drop_video,
            drop_audio=parse.drop_audio,
            drop_subtitle=parse.drop_subtitle,
            save_name=plan.save_name,
            save_dir=str(plan.output_dir),
            output=str(output) if output else None,
            keys=list(plan.keys),
            service_context=dict(plan.service_context),
            key_text_file=(
                str(policy.key_text_file) if policy.key_text_file else None
            ),
            workers=policy.workers,
            retries=policy.retries,
            concurrent_tracks=policy.concurrent_tracks,
            max_speed=policy.max_speed,
            http_request_timeout=policy.http_timeout,
            check_segments_count=policy.check_segments_count,
            no_resume=policy.resume is False,
            downloader=policy.downloader,
            mux=policy.mux,
            mux_format=mux_format,
            muxer=muxer,
            mux_imports=list(policy.mux_imports),
            chapters_file=(
                str(policy.chapters_file) if policy.chapters_file else None
            ),
            sub_format=policy.subtitle_format,
            auto_subtitle_fix=policy.auto_subtitle_fix,
            sub_only=policy.subtitle_only,
            audio_format=policy.audio_format,
            audio_metadata_file=(
                str(policy.audio_metadata_file) if policy.audio_metadata_file else None
            ),
            decode_audio_vivid=policy.decode_audio_vivid,
            audio_vivid_decoder=policy.audio_vivid_decoder,
            audio_vivid_decoder_args=policy.audio_vivid_decoder_args,
            no_decrypt=policy.no_decrypt,
            decrypter=policy.decrypter,
            custom_hls_method=policy.hls_method,
            custom_hls_key=policy.hls_key,
            custom_hls_iv=policy.hls_iv,
            hls_decryptor=policy.hls_decryptor,
            tmp_dir=str(policy.temp_dir) if policy.temp_dir else None,
            log_file_path=str(policy.log_file) if policy.log_file else None,
            write_meta_json=policy.write_meta_json,
            no_color=policy.no_color,
            keep_temp=policy.keep_temp,
            no_del_after_done=policy.keep_after_done,
            is_live=live.enabled,
            live_record_limit=live.record_limit,
            live_real_time_merge=live.real_time_merge,
            live_keep_segments=live.keep_segments,
            live_pipe_mux=live.pipe_mux,
            live_perform_as_vod=live.perform_as_vod,
            live_dvr_from_start=live.dvr_from_start,
            live_dvr_start_at=live.dvr_start_at,
            live_dvr_end_at=live.dvr_end_at,
            extra_args=extra_args,
        )
        return options, streams, selected
    @staticmethod
    def _describe(stream: StreamInfo, index: int) -> TrackDescriptor:
        identity = "\x1f".join(
            str(value or "")
            for value in (
                index,
                stream.manifest_type,
                stream.media_type,
                stream.id,
                stream.group_id,
                stream.url,
                stream.bandwidth,
                stream.resolution,
                stream.language,
            )
        )
        track_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return TrackDescriptor(
            track_id=track_id,
            manifest_type=str(stream.manifest_type or ""),
            media_type=str(stream.media_type or "unknown"),
            source_id=str(stream.id or ""),
            group_id=str(stream.group_id or ""),
            url=str(stream.url or ""),
            original_url=str(stream.original_url or ""),
            name=str(stream.name or ""),
            language=str(stream.language or ""),
            role=str(stream.role or ""),
            bandwidth=stream.bandwidth,
            codecs=str(stream.codecs or ""),
            resolution=str(stream.resolution or ""),
            frame_rate=stream.frame_rate,
            channels=str(stream.channels or ""),
            extension=str(stream.extension or ""),
            video_range=str(stream.video_range or ""),
            duration=stream.total_duration,
            size_bytes=stream.estimated_size_bytes,
            encrypted=bool(stream.encrypted),
            encryption_scheme=str(stream.encryption_scheme or ""),
            is_live=bool(stream.is_live),
            key_ids=tuple(api.stream_key_ids(stream)),
        )

    @staticmethod
    def _materialize(request: ParseRequest) -> str:
        source = request.source
        if source.kind is SourceKind.REFERENCE:
            return str(source.reference)
        scratch = private_directory(
            Path(request.scratch_dir or Path.cwd() / ".unidl-core")
        )
        if source.kind is SourceKind.INLINE_HLS:
            payload = str(source.inline_text or "")
            suffix = ".m3u8"
        elif source.kind is SourceKind.INLINE_DASH:
            payload = str(source.inline_text or "")
            suffix = ".mpd"
        else:
            payload = json.dumps(source.json_document, ensure_ascii=False, sort_keys=True)
            suffix = ".json"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        path = scratch / f"manifest-{digest}{suffix}"
        with locked_path(path):
            if path.is_symlink():
                raise RuntimeError(f"refusing manifest cache symlink: {path}")
            if path.exists() and not path.is_file():
                raise RuntimeError(f"manifest cache path is not a file: {path}")
            try:
                current = path.read_text(encoding="utf-8") if path.is_file() else None
            except OSError:
                current = None
            if current != payload:
                atomic_write_text(path, payload)
            else:
                private_file(path)
        return str(path)


__all__ = ["NativeDownloaderBackend", "NativeManifestError"]
