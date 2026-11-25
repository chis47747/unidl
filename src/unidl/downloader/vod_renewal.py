"""Finite playback renewal behind the native delivery boundary."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from urllib.parse import urlsplit

from ..core.delivery import DeliverySource, MessageEvent, PlaybackSourceRenewal, SourceKind, VodSourceExpired
from ..core.vault import playready_kid_alias, split_pair
from . import api, cli
from .downloader import _resume_url_key
from .embedding import DownloadCancelled, DownloadRuntime, VodTransferAttempt, proxy_scope, transfer_auth_status
from .http_client import redirect_headers


class VodRenewalError(RuntimeError):
    """No authorized source could safely resume the selected VOD."""


def _digest(parts) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _semantic_shape(stream) -> tuple:
    role = stream.extra.get("subtitle_kind") or stream.role
    return (
        stream.manifest_type, stream.media_type, stream.codecs,
        stream.resolution, stream.frame_rate, stream.video_range, stream.is_3d,
        stream.bandwidth, stream.language, stream.channels, role,
        stream.extra.get("hls_instream_id"), bool(stream.extra.get("muxed_audio")),
        bool(stream.extra.get("audio_atmos")), stream.extra.get("_unidl_demux_media"),
    )


def _same_edition_track(original, fresh) -> int:
    if fresh.is_live or _semantic_shape(original) != _semantic_shape(fresh):
        return -1
    left, right = original.total_duration, fresh.total_duration
    if left and right and abs(left - right) > max(0.75, min(left, right) * 0.002):
        return -1
    stable_id = bool(original.id and original.id == fresh.id and original.group_id == fresh.group_id)
    same_url = bool(original.url and _resume_url_key(original.url) == _resume_url_key(fresh.url))
    if not (stable_id or same_url) and not any((
        original.resolution and original.codecs,
        original.language and (original.codecs or original.media_type in {"subtitle", "subtitles", "text"}),
        original.name and original.name == fresh.name,
    )):
        return -1
    # A named audio/text role must not silently become a different rendition
    # when the service does not supply a stable identifier.
    if not (stable_id or same_url) and original.media_type != "video" and original.name and original.name != fresh.name:
        return -1
    return (
        8 * stable_id + 4 * same_url + 2 * bool(original.group_id and original.group_id == fresh.group_id)
        + bool(original.name and original.name == fresh.name)
    )


def _match_selected(originals, candidates):
    chosen = []
    for original in originals:
        ranked = [(score, candidate) for candidate in candidates if (score := _same_edition_track(original, candidate)) >= 0]
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            return None
        chosen.append(ranked[0][1])
    return chosen if len({id(stream) for stream in chosen}) == len(chosen) else None


def _candidate_requests(original, renewed: PlaybackSourceRenewal, options):
    sources = ([renewed.source] if renewed.source is not None else []) + [
        DeliverySource.from_reference(url) for url in dict.fromkeys(renewed.manifest_urls) if url
    ]
    for source in sources:
        reference = source.reference if source.kind is SourceKind.REFERENCE else renewed.base_url
        if not reference or urlsplit(reference).scheme not in {"http", "https"}:
            continue
        headers = dict(renewed.headers) if renewed.headers is not None else redirect_headers(
            options.base_url or options.input, reference, dict(options.headers),
        )
        yield replace(
            original,
            source=source,
            policy=replace(original.policy, headers=headers, base_url=renewed.base_url),
        )


def _replace_keys(prior: list[str], incoming: tuple[str, ...]) -> list[str]:
    pairs = {}
    others = []
    for value in prior:
        if (pair := split_pair(value)) is not None:
            pairs[pair[0]] = value
        else:
            others.append(value)
    for value in incoming:
        pair = split_pair(value)
        if pair is None:
            raise VodRenewalError("VOD source renewal returned an invalid KID:key entry")
        pairs[pair[0]] = f"{pair[0]}:{pair[1]}"
    return [*others, *pairs.values()]


def _available_kids(keys: list[str], key_file: str | None, system: str) -> set[str]:
    values = [*cli.parse_keys(keys), *cli.parse_key_text_file(key_file)]
    kids = {key.kid for key in values if key.kid}
    if system.lower() == "playready":
        kids.update(alias for kid in tuple(kids) if (alias := playready_kid_alias(kid)))
    return kids


def _refresh_inventory(backend, plan, originals, last_selected, options, renewed, system, check):
    keys = _replace_keys(list(options.keys), renewed.keys)
    known = set(api.key_ids(last_selected))
    missing_keys = False
    for request in _candidate_requests(plan.manifest.request, renewed, options):
        check()
        headers = dict(request.policy.headers)
        with proxy_scope(request.policy.proxy, request.policy.use_system_proxy):
            try:
                candidates = backend.streams(backend.parse(request))
                selected = _match_selected(originals, candidates)
                if selected is None:
                    continue
                selected = [
                    cli._hydrate_stream(stream, headers, request.policy.no_probe, request.policy.base_url)
                    for stream in selected
                ]
                if not options.no_decrypt and not request.policy.no_probe:
                    parsed_keys = [*cli.parse_keys(keys), *cli.parse_key_text_file(options.key_text_file)]
                    for stream in selected:
                        if stream.encrypted and not api.stream_key_ids(stream):
                            cli._hydrate_selected_stream_key_ids(
                                stream, headers, max(1, options.http_request_timeout or 30), keys=parsed_keys,
                            )
            except (OSError, ValueError, RuntimeError, SystemExit):
                check()
                continue
        if any(_same_edition_track(original, fresh) < 0 for original, fresh in zip(originals, selected, strict=True)):
            continue
        introduced = set(api.key_ids(selected)) - known - {"0" * 32}
        if introduced and not options.no_decrypt and not introduced <= _available_kids(keys, options.key_text_file, system):
            missing_keys = True
            continue
        chosen = {id(stream) for stream in selected}
        return replace(
            options, input=backend._materialize(request), base_url=request.policy.base_url, headers=headers, keys=keys,
        ), [*selected, *(stream for stream in candidates if id(stream) not in chosen)], selected
    if missing_keys:
        raise VodRenewalError("VOD renewal introduced a KID that is not covered by the authorized keys")
    raise VodRenewalError("VOD renewal could not match all selected tracks of the same asset")


def run_renewable_vod(backend, plan, hooks, runtime, download_hooks, options, streams, selected, track_ids, has_artifacts):
    chosen = {id(stream) for stream in selected}
    originals = [stream for stream in streams if id(stream) in chosen]
    stable_ids = [track_ids[id(stream)] for stream in originals]
    cache_id = _digest(("vod-v1", hooks.vod_service_id, hooks.vod_asset_id, str(plan.output_dir), plan.save_name))
    resume_ids = [
        _digest((cache_id, index, _semantic_shape(stream), stream.id, stream.group_id, _resume_url_key(stream.original_url)))
        for index, stream in enumerate(originals)
    ]
    completed_positions = set()
    renewed_once = False
    last_selected = originals
    if options.downloader == "aria2c":
        hooks.emit(MessageEvent("VOD source renewal uses the native Python downloader for this task."))
        options = replace(options, downloader="python")

    def check():
        if hooks.cancellation.cancelled or runtime.stopped:
            raise DownloadCancelled("download canceled during VOD authorization")

    for round_index in range(129):
        check()
        generation = uuid.uuid4().hex
        mapped = {}
        # Retain stable picker ids and never mutate the old authorized
        # inventory. CLI hydration may adjust a clone's segments and labels.
        for stream, track_id, resume_id in zip(last_selected, stable_ids, resume_ids, strict=True):
            mapped[id(stream)] = replace(
                stream, segments=[replace(segment) for segment in stream.segments], extra=dict(stream.extra),
                vod_resume_id=resume_id, vod_resume_generation=generation,
            )
            track_ids[id(mapped[id(stream)])] = track_id
        active_streams = [mapped.get(id(stream), stream) for stream in streams]
        active_selected = [mapped[id(stream)] for stream in last_selected]
        child = DownloadRuntime()
        attempt = VodTransferAttempt(cache_id)
        child.vod_transfer = attempt
        token = runtime.register_closer(child.request_shutdown)
        try:
            with child.activate():
                try:
                    result = api.download(
                        options, active_streams, active_selected,
                        hooks=replace(download_hooks, runtime=child),
                    )
                except Exception as exc:
                    check()
                    if attempt.failure is None and (status := transfer_auth_status(exc)) is not None:
                        attempt.note_failure(status, "segment")
                    if attempt.failure is None:
                        raise
                    result = 1
        finally:
            runtime.unregister_closer(token)
            for stream in active_selected:
                track_ids.pop(id(stream), None)
        check()
        if result == 130 and attempt.failure is None:
            return 130
        if attempt.failure is None:
            return result
        if has_artifacts():
            raise VodRenewalError("VOD authorization expired after output was already published")
        if round_index == 128:
            break
        if renewed_once and not (attempt.completed - completed_positions):
            raise VodRenewalError("VOD authorization expired again without verified download progress")
        completed_positions.update(attempt.completed)
        renewed_once = True
        status, phase = attempt.failure
        hooks.emit(MessageEvent(f"VOD authorization: HTTP {status} during {phase}; asking the service for a fresh source."))
        hooks.pause.wait(lambda: hooks.cancellation.cancelled or runtime.stopped)
        check()
        event = VodSourceExpired(
            status, phase, hooks.vod_asset_id,
            tuple(replace(backend._describe(stream, index), track_id=track_id)
                  for index, (stream, track_id) in enumerate(zip(active_selected, stable_ids, strict=True), start=1)),
        )
        try:
            renewed = hooks.renew_vod_source(event)
        except Exception:
            check()
            raise VodRenewalError("VOD playback authorization renewal failed") from None
        check()
        if not isinstance(renewed, PlaybackSourceRenewal):
            raise VodRenewalError("The service did not renew VOD playback authorization")
        if renewed.asset_id != hooks.vod_asset_id:
            raise VodRenewalError("VOD renewal returned a different asset or edition; existing parts were kept")
        options, streams, last_selected = _refresh_inventory(
            backend, plan, originals, active_selected, options, renewed, hooks.vod_drm_system, check,
        )
        hooks.emit(MessageEvent("VOD authorization renewed; resuming the same selected tracks."))
    raise VodRenewalError("VOD playback reached the safety limit for authorization renewals")
