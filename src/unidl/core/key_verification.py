"""Selected-VOD key preflight and bounded vault fallback, independent of the TUI."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from ..downloader import api
from ..downloader.embedding import DownloadCancelled
from ..downloader.key_probe import KeyProbe, Verdict, key_token, stream_token
from ..downloader.postprocess import RawKey
from .cdm import CdmError
from .i18n import tr
from .vault import playready_kid_alias, split_pair


@dataclass
class VerificationState:
    sources: dict = field(default_factory=dict)
    verified: set[str] = field(default_factory=set)
    token: tuple | None = None
    sample_sizes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VaultSource:
    name: str
    remote: bool


def state(playback) -> VerificationState:
    value = getattr(playback, "_key_verification_state", None)
    if value is None:
        value = VerificationState()
        playback._key_verification_state = value
    return value


def enabled(playback, settings) -> bool:
    return bool(settings and settings.get("verify_content_keys", False)) and not (
        playback.is_live or getattr(playback, "_key_verification_download", True) is False
        or "--no-decrypt" in playback.extra_args
    )


def record_sources(playback, found, sources):
    current = state(playback)
    current.sources.update({f"{kid}:{key}": VaultSource(sources[kid].name, sources[kid].remote)
                            for kid, key in found.items() if kid in sources})


def verified_writes(playback) -> list[str]:
    current = state(playback)
    for value in playback.keys:
        pair = split_pair(value)
        if pair:
            current.sources.setdefault(f"{pair[0]}:{pair[1]}", "cdm")
    return [value for value in playback.keys if value in current.verified]


def sample_size(playback, origin, segment):
    """Reuse response-size evidence; never retain media bytes on queued jobs."""
    current = getattr(playback, "_key_verification_state", None)
    if current is None:
        return None
    size = current.sample_sizes.get(KeyProbe._identity(segment.url, origin.headers, origin.proxy))
    if not size:
        return None
    if segment.byte_range:
        start, end = segment.byte_range
        return end - start + 1 if 0 <= start <= end < size else None
    return size


def _spellings(playback, kid):
    alias = playready_kid_alias(kid) if getattr(playback.drm, "system", None) == "playready" else None
    return tuple(dict.fromkeys((kid, alias))) if alias else (kid,)


def _find_pair(playback, kid, values):
    wanted = _spellings(playback, kid)
    return next((pair for value in values if (pair := split_pair(value)) and pair[0] in wanted), None)


def _replace_key(playback, kid, key, *, spellings=None):
    wanted = spellings or _spellings(playback, kid)
    old = next((pair for value in playback.keys if (pair := split_pair(value)) and pair[0] in wanted), None)
    playback.keys = [value for value in playback.keys if not ((pair := split_pair(value)) and pair[0] in wanted)]
    playback.keys.append(f"{old[0] if old else kid}:{key}")


class _LicenseOnlySettings:
    def __init__(self, original):
        self.original = original

    def get(self, key, fallback=None):
        if key in {"local_vault", "remote_vault"}:
            return False
        return self.original.get(key, fallback)

    scoped = get

    def __getattr__(self, key):
        return getattr(self.original, key)


def verify_selected_keys(engine, playback, tracks, settings, *, service=None, cancel=None):
    if not enabled(playback, settings) or tracks is None:
        return
    raw = [RawKey(pair[1], pair[0]) for value in playback.keys if (pair := split_pair(value))]
    token = (tuple((id(stream), stream_token(stream, raw, api.stream_key_ids(stream))) for stream in tracks.selected), key_token(raw))
    if state(playback).token == token:
        return
    probe = KeyProbe(cancel=cancel)

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("key verification cancelled")
    # One representative per origin, KID, scheme and codec. Unselected profiles
    # never enter this inventory, even when licensing happened before selection.
    origins: dict[int, tuple[object, dict]] = {}
    for stream in tracks.selected:
        if stream.is_live or not stream.encrypted or stream.media_type not in {"video", "audio"}:
            continue
        origin = tracks.origins.get(id(stream), playback)
        groups = origins.setdefault(id(origin), (origin, {}))[1]
        for kid in api.stream_key_ids(stream):
            if kid == "0" * 32:
                continue
            representatives = groups.setdefault(kid, {})
            representatives.setdefault((stream.media_type, stream.codecs, stream.encryption_scheme), stream)

    def judge(origin, kid, key, streams):
        results = [probe.verify(stream, kid, key, aliases=_spellings(origin, kid)[1:],
                                headers=origin.headers, proxy=origin.proxy) for stream in streams]
        if any(result.status == "failed" for result in results):
            return Verdict("failed", "encrypted samples failed to decode")
        if results and all(result.status == "passed" for result in results):
            return Verdict("passed", "encrypted media decoded successfully")
        return next((result for result in results if result.status == "unknown"), Verdict("unknown", "no sample"))

    def accept(origin, kid, key, result):
        _replace_key(playback, kid, key, spellings=_spellings(origin, kid))
        if origin is not playback:
            _replace_key(origin, kid, key)
        if result.status == "passed":
            for target in (playback, origin):
                pair = _find_pair(origin, kid, target.keys)
                if pair:
                    state(target).verified.add(f"{pair[0]}:{pair[1]}")
            engine.log(tr("key_verify.passed", kid=kid))
        else:
            engine.log(tr("key_verify.unknown", kid=kid, reason=result.reason))

    for origin, groups in origins.values():
        pending = {}
        for kid, representatives in groups.items():
            check_cancel()
            streams = list(representatives.values())
            pair = _find_pair(origin, kid, playback.keys)
            if pair is None:
                engine.log(tr("key_verify.unknown", kid=kid, reason="no matching KID:key candidate"))
                continue
            source = state(origin).sources.get(f"{pair[0]}:{pair[1]}", state(playback).sources.get(f"{pair[0]}:{pair[1]}", "supplied"))
            state(origin).sources.setdefault(f"{pair[0]}:{pair[1]}", source)
            result = judge(origin, kid, pair[1], streams)
            if result.status != "failed":
                accept(origin, kid, pair[1], result)
                continue
            if isinstance(source, str):
                raise CdmError(tr("key_verify.failed_terminal", kid=kid))
            engine.log(tr("key_verify.retry", kid=kid, source=source.name))
            rejected = {pair[1]}
            local, remote, local_names, remote_names, *_ = engine.vault_targets(settings)
            candidates = engine.vaults.key_candidates(
                pair[0], service.ID if service is not None else playback.title.service, use_local=local, use_remote=remote,
                local_names=local_names, remote_names=remote_names,
                skip_remote={source.name} if source.remote else set(),
            )
            for key, backend in candidates:
                check_cancel()
                if key in rejected:
                    continue
                result = judge(origin, kid, key, streams)
                if result.status != "failed":
                    state(origin).sources[f"{pair[0]}:{key}"] = VaultSource(backend.name, backend.remote)
                    accept(origin, kid, key, result)
                    break
                rejected.add(key)
                engine.log(tr("key_verify.retry", kid=kid, source=backend.name))
            else:
                pending[kid] = (streams, rejected)
        if pending:
            if service is None or getattr(service, "_NO_LICENSE_IMPORT", False) or origin.drm is None:
                raise CdmError(tr("key_verify.no_fallback"))
            # A single service-owned licence pass for the failing inventory.
            # It never reuses poisoned vault_keys or another profile's auth.
            retry = replace(origin, keys=[], drm=replace(origin.drm, context=dict(origin.drm.context)))
            retry.drm.context.pop("vault_keys", None)
            retry._key_verification_download = True
            selected = list({id(stream): stream for streams, _ in pending.values() for stream in streams}.values())
            inventory = type(tracks)(streams=selected, selected=selected)
            engine.log(tr("key_verify.license"))
            fresh = engine.resolve_keys(retry, service, inventory, _LicenseOnlySettings(settings), license_tracks=inventory)
            for kid, (streams, rejected) in pending.items():
                pair = _find_pair(origin, kid, fresh)
                if pair is None or pair[1] in rejected:
                    raise CdmError(tr("key_verify.failed_terminal", kid=kid))
                result = judge(origin, kid, pair[1], streams)
                if result.status == "failed":
                    raise CdmError(tr("key_verify.failed_terminal", kid=kid))
                state(origin).sources[f"{pair[0]}:{pair[1]}"] = "cdm"
                accept(origin, kid, pair[1], result)
        if service is not None and state(origin).verified:
            engine.store_keys(origin, service, settings)
    # Native execution in this same process must not repeat the TUI preflight;
    # a saved command has no marker and will validate again when executed.
    final_keys = [RawKey(pair[1], pair[0]) for value in playback.keys if (pair := split_pair(value))]
    for stream in tracks.selected:
        stream.extra["_key_verification_token"] = stream_token(stream, final_keys, api.stream_key_ids(stream))
    state(playback).token = (tuple((id(stream), stream.extra["_key_verification_token"]) for stream in tracks.selected), key_token(final_keys))
    state(playback).sample_sizes = dict(getattr(probe, "resource_sizes", {}))
