"""A resolved title as a file: the manifest, what was in it, and the keys.

The expensive half of a download is not the bytes. It is the account, the CDM and
the one licence request the service will count - and all of that is spent before a
single segment is fetched. Once it is spent, everything needed to fetch the file
is knowable: where the manifest is, what is in it, and, when protected, which key
opens it.

An export is that knowledge, written down. It is what lets a standard title be
finished on a machine with no account, CDM or source-service package, handed to
someone who has none of them, or picked up again without asking the service for
anything a second time. When the source service is installed, Import may retain
its custom download preparation and lifecycle hooks.

What it carries
    The manifest input, the request headers that go with it, the title as core
    understands it, the release name it was given, the DRM system and its init
    data, the KID:key pairs, and the ladder as it stood - marked with what was
    taken. One file per run, so a season picked at once is one file.

What it does not carry
    Cookies, tokens, an account, or anything else that would let the reader sign
    in as you. The keys are in it, which is the point, and the manifest link
    usually carries a signed token of its own - so the file is as sensitive as the
    keys it holds, and the card that writes it says so.

What it does not decide
    Which tracks the reader takes. The manifest is read again on import and the
    reader's own quality and codec settings choose, exactly as they would on a
    normal run - so a 4K export can be taken at 1080p. The keys are matched by
    key id, and a track whose key is not in the file is reported rather than
    quietly downloaded undecryptable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .attachments import Attachment
from .chapters import Chapter
from .lyrics import Lyrics
from .manifest_sources import (
    BUNDLE_MARKER,
    ManifestSourceError,
    merge_source_metadata,
    source_bundle,
    source_manifest,
    source_metadata,
    subtitle_source_identity,
    validate_bundle,
    with_subtitle_sources,
)
from .naming import clean_part
from .output_paths import safe_folder_component
from .playback import DrmInfo, Playback, SubtitleReference
from .secureio import atomic_write_text
from .titles import Title, TitleKind

#: The document format. Read strictly: a file from a later version may describe
#: something this build would silently get wrong, and "it downloaded the wrong
#: thing" is a worse outcome than "it refused to open the file".
VERSION = 3

# A complete batch of source manifests must fit this bound. Never replace
# an earlier valid export with a truncated document or a partial batch.
MAX_EXPORT_BYTES = 32 * 1024 * 1024

#: what the file is, for anyone who opens it in an editor
KIND = "unidl-export"

#: Marks in the exported ladder, the same two the track card uses.
TAKEN = "+"
LEFT = "-"

#: Per-service setting for the manifest representation written by native export.
EXPORT_MANIFEST_TYPE_KEY = "export_manifest_type"
MASTER_MANIFEST = "master"
MEDIA_MANIFEST = "media"
EXPORT_MANIFEST_TYPES = (MASTER_MANIFEST, MEDIA_MANIFEST)


class ExportError(RuntimeError):
    """A file that is not an export, or not one this build can read."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_manifest_type(value: Any) -> str:
    """Return a supported native export mode, preserving the legacy default."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in EXPORT_MANIFEST_TYPES else MASTER_MANIFEST


def _title_document(title: Title) -> dict[str, Any]:
    """A title as plain JSON.

    Service-private ``data`` is intentionally omitted.  It commonly contains
    large, short-lived provider responses (Amazon playback envelopes, widget
    pages and expiry/correlation metadata) that the import path never reads.
    Older exports may still contain ``data`` and remain readable through
    :func:`_title_from`, but new exports carry only stable title metadata.
    """
    document: dict[str, Any] = {
        "id": _text(title.id),
        "kind": title.kind.value if isinstance(title.kind, TitleKind) else _text(title.kind),
        "name": _text(title.name),
        "service": _text(title.service),
    }
    for name in (
        "year",
        "season",
        "episode",
        "episode_name",
        "language",
        "duration",
        "channel",
        "artist",
        "album",
        "track_number",
        "genre",
        "publisher",
        "synopsis",
        "cover_url",
    ):
        value = getattr(title, name, None)
        if value not in (None, ""):
            document[name] = value
    for name in ("starts_at", "ends_at"):
        when = getattr(title, name, None)
        if isinstance(when, datetime):
            document[name] = when.isoformat()
    return document


def _title_from(document: dict[str, Any]) -> Title:
    """A title back out of JSON, with anything unreadable left unset."""
    try:
        kind = TitleKind(str(document.get("kind") or "movie"))
    except ValueError:
        kind = TitleKind.MOVIE
    title = Title(
        id=_text(document.get("id")),
        kind=kind,
        name=_text(document.get("name")),
        service=_text(document.get("service")),
    )
    for name in (
        "year",
        "episode_name",
        "language",
        "channel",
        "artist",
        "album",
        "genre",
        "publisher",
        "synopsis",
        "cover_url",
    ):
        if document.get(name) not in (None, ""):
            setattr(title, name, _text(document.get(name)))
    for name in ("season", "episode", "track_number"):
        value = document.get(name)
        if isinstance(value, int):
            setattr(title, name, value)
    if isinstance(document.get("duration"), (int, float)):
        title.duration = float(document["duration"])
    for name in ("starts_at", "ends_at"):
        raw = document.get(name)
        if raw:
            try:
                setattr(title, name, datetime.fromisoformat(str(raw)))
            except ValueError:
                pass
    data = document.get("data")
    if isinstance(data, dict):
        title.data = data
    return title


@dataclass
class Entry:
    """One title in an export."""

    save_name: str = ""
    title: Title | None = None
    manifest_url: str = ""
    #: Additional already-authorized manifests belonging to the same exported
    #: title. Third-party exporters commonly store one DASH/ISM URL per codec or
    #: range. They are parsed and merged by the generic import context; they are
    #: never handed to an installed service for re-authorization.
    alternate_manifest_urls: tuple[str, ...] = ()
    merge_manifests: bool = False
    manifest_base_url: str = ""
    json_manifest: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    proxy: str = ""
    is_live: bool = False
    note: str = ""
    requested: str = ""
    returned: str = ""
    drm_system: str = ""
    pssh: str = ""
    wrm_header: str = ""
    #: Non-licence DRM states must survive the round trip too. ``clear`` prevents
    #: an explicit clear playback from becoming a licence request, while a raw
    #: HLS key covers services that resolved AES-128 before exporting. Ordinary
    #: playlist AES-128 needs neither field: the downloader follows its key URI.
    drm_clear: bool = False
    hls_key: str = ""
    hls_iv: str = ""
    hls_method: str = ""
    #: "kid:key", exactly the strings the vault and UniDL use
    keys: list[str] = field(default_factory=list)
    #: the ladder as it stood, each row marked taken or left
    tracks: list[str] = field(default_factory=list)
    summary: str = ""
    #: Optional service/API chapter timeline.  This is additive within export
    #: version 1: older readers ignore the field and newer readers retain it.
    chapters: list[Chapter] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    lyrics: Lyrics | None = None
    audio_codec_hint: str = ""
    #: Automatic output rules captured with the title so import can preserve
    #: subtitle/audio/video-only batch intent.
    selection: dict[str, Any] = field(default_factory=dict)
    #: Read-only provenance for legacy segment snapshots. Not serialized.
    legacy_source_path: str = ""
    legacy_entry_index: int = 0
    #: Exact title half before track-derived release fields were appended.
    #: Optional for compatibility with exports written before this was retained.
    release_name_base: str = ""

    def key_ids(self) -> list[str]:
        """The key ids this entry can open, lower-case hex, no dashes."""
        found: list[str] = []
        for pair in self.keys:
            kid = str(pair).partition(":")[0].strip().lower().replace("-", "")
            if kid and kid not in found:
                found.append(kid)
        return found

    def label(self) -> str:
        """What to call this entry on a screen."""
        return self.save_name or (self.title.name if self.title is not None else "untitled")

    def playback(self) -> Playback:
        """The playback this entry stands for, with its keys already in it.

        Keys first is the whole mechanism: ``Engine.resolve_keys`` returns them
        untouched when a playback arrives carrying them, so an import reaches the
        download without a licence request, a CDM or a signed-in account - and
        without one line of the download path knowing an import happened.
        """
        title = self.title if self.title is not None else Title(id="", kind=TitleKind.MOVIE, name="")
        drm = None
        if any(
            (
                self.drm_system,
                self.pssh,
                self.wrm_header,
                self.drm_clear,
                self.hls_key,
                self.hls_iv,
                self.hls_method,
            )
        ):
            drm = DrmInfo(
                system=self.drm_system or None,
                pssh=self.pssh or None,
                wrm_header=self.wrm_header or None,
                clear=self.drm_clear,
                hls_key=self.hls_key or None,
                hls_iv=self.hls_iv or None,
                hls_method=self.hls_method or None,
            )
        return Playback(
            title=title,
            save_name=self.save_name,
            release_name_base=self.release_name_base,
            manifest_url=self.manifest_url or None,
            alternate_manifest_urls=tuple(self.alternate_manifest_urls),
            merge_manifests=self.merge_manifests,
            imported_manifest_urls=(
                tuple(dict.fromkeys((self.manifest_url, *self.alternate_manifest_urls)))
                if self.merge_manifests and self.manifest_url and self.alternate_manifest_urls and self.json_manifest is None
                else ()
            ),
            manifest_base_url=self.manifest_base_url or None,
            json_manifest=(
                source_bundle([{"type": "reference", "url": self.legacy_source_path,
                                "export_entry": self.legacy_entry_index}])
                if self.legacy_source_path else self.json_manifest
            ),
            headers=dict(self.headers),
            proxy=self.proxy or None,
            is_live=self.is_live,
            drm=drm,
            keys=list(self.keys),
            note=self.note,
            requested=self.requested,
            returned=self.returned,
            subtitle_references=[
                SubtitleReference.from_document(item)
                for item in (self.json_manifest or {}).get("subtitle_sources", [])
            ],
            chapters=list(self.chapters),
            attachments=list(self.attachments),
            lyrics=self.lyrics,
            audio_codec_hint=self.audio_codec_hint,
            output_scope=_text(self.selection.get("output_scope")),
            output_types=tuple(
                str(value).strip().lower()
                for value in (self.selection.get("output_types") or [])
                if str(value).strip()
            ),
            video_selection=_text(self.selection.get("video_selection")),
            audio_selection=_text(self.selection.get("audio_selection")),
            default_audio=_text(self.selection.get("default_audio")),
            default_subtitle=_text(self.selection.get("default_subtitle")),
            subtitle_selection=_text(self.selection.get("subtitle_selection")),
        )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "save_name": self.save_name,
            "title": _title_document(self.title) if self.title is not None else {},
            "keys": list(self.keys),
        }
        if self.manifest_url:
            document["manifest_url"] = self.manifest_url
        if self.release_name_base:
            document["release_name_base"] = self.release_name_base
        if self.alternate_manifest_urls:
            document["alternate_manifest_urls"] = list(self.alternate_manifest_urls)
        if self.merge_manifests:
            document["merge_manifests"] = True
        if self.manifest_base_url:
            document["manifest_base_url"] = self.manifest_base_url
        if self.json_manifest is not None:
            document["json_manifest"] = source_manifest(self.json_manifest)
        if self.headers:
            document["headers"] = dict(self.headers)
        if self.proxy:
            document["proxy"] = self.proxy
        if self.is_live:
            document["is_live"] = True
        if self.note:
            document["note"] = self.note
        if self.requested:
            document["requested"] = self.requested
        if self.returned:
            document["returned"] = self.returned
        drm: dict[str, Any] = {}
        if self.drm_system:
            drm["system"] = self.drm_system
        if self.pssh:
            drm["pssh"] = self.pssh
        if self.wrm_header:
            drm["wrm_header"] = self.wrm_header
        if self.drm_clear:
            drm["clear"] = True
        if self.hls_key:
            drm["hls_key"] = self.hls_key
        if self.hls_iv:
            drm["hls_iv"] = self.hls_iv
        if self.hls_method:
            drm["hls_method"] = self.hls_method
        if drm:
            document["drm"] = drm
        if self.tracks:
            document["tracks"] = list(self.tracks)
        if self.summary:
            document["summary"] = self.summary
        if self.chapters:
            document["chapters"] = [chapter.as_document() for chapter in self.chapters]
        if self.attachments:
            document["attachments"] = [attachment.as_document() for attachment in self.attachments]
        if self.lyrics is not None:
            document["lyrics"] = self.lyrics.as_document()
        if self.audio_codec_hint:
            document["audio_codec_hint"] = self.audio_codec_hint
        if self.selection:
            document["selection"] = dict(self.selection)
        return document


def _entry_from(document: dict[str, Any]) -> Entry:
    if not isinstance(document, dict):
        raise ExportError("one of the titles in this file is not a title")
    drm = document.get("drm") if isinstance(document.get("drm"), dict) else {}
    manifest = _text(document.get("manifest_url"))
    json_manifest = document.get("json_manifest")
    if not manifest and not isinstance(json_manifest, dict):
        raise ExportError(
            f"{_text(document.get('save_name')) or 'a title'} in this file has no manifest to fetch"
        )
    chapter_documents = document.get("chapters") or []
    if not isinstance(chapter_documents, list):
        raise ExportError("chapters must be a list")
    try:
        chapters = [Chapter.from_document(chapter) for chapter in chapter_documents]
    except (TypeError, ValueError) as exc:
        raise ExportError(f"invalid chapter metadata ({exc})") from exc
    try:
        lyrics = Lyrics.from_document(document["lyrics"]) if document.get("lyrics") else None
    except (TypeError, ValueError) as exc:
        raise ExportError(f"invalid lyrics metadata ({exc})") from exc
    attachment_documents = document.get("attachments") or []
    if not isinstance(attachment_documents, list):
        raise ExportError("attachments must be a list")
    try:
        attachments = [Attachment.from_document(item) for item in attachment_documents]
    except (TypeError, ValueError) as exc:
        raise ExportError(f"invalid attachment metadata ({exc})") from exc
    return Entry(
        save_name=_text(document.get("save_name")),
        release_name_base=_text(document.get("release_name_base")),
        title=_title_from(document.get("title") or {}),
        manifest_url=manifest,
        alternate_manifest_urls=tuple(
            str(url)
            for url in (document.get("alternate_manifest_urls") or [])
            if str(url).strip()
        ),
        merge_manifests=bool(document.get("merge_manifests")),
        manifest_base_url=_text(document.get("manifest_base_url")),
        json_manifest=json_manifest if isinstance(json_manifest, dict) else None,
        headers={str(k): str(v) for k, v in (document.get("headers") or {}).items()},
        proxy=_text(document.get("proxy")),
        is_live=bool(document.get("is_live")),
        note=_text(document.get("note")),
        requested=_text(document.get("requested")),
        returned=_text(document.get("returned")),
        drm_system=_text(drm.get("system")),
        pssh=_text(drm.get("pssh")),
        wrm_header=_text(drm.get("wrm_header")),
        drm_clear=bool(drm.get("clear")),
        hls_key=_text(drm.get("hls_key")),
        hls_iv=_text(drm.get("hls_iv")),
        hls_method=_text(drm.get("hls_method")),
        keys=[str(key) for key in (document.get("keys") or [])],
        tracks=[str(row) for row in (document.get("tracks") or [])],
        summary=_text(document.get("summary")),
        chapters=chapters,
        attachments=attachments,
        lyrics=lyrics,
        audio_codec_hint=_text(document.get("audio_codec_hint")),
        selection=dict(document.get("selection") or {}) if isinstance(document.get("selection"), dict) else {},
    )


@dataclass
class Document:
    """One export file: a service, and the titles resolved from it in one run."""

    service: str = ""
    service_name: str = ""
    created: str = ""
    app: str = ""
    entries: list[Entry] = field(default_factory=list)
    #: Parser identity used only while importing. Native documents keep KIND;
    #: adapters set their own value so the UI can force the generic downloader
    #: even when the source service happens to be installed.
    source_format: str = KIND

    @property
    def is_native(self) -> bool:
        return self.source_format == KIND

    @property
    def keys(self) -> int:
        return sum(len(entry.keys) for entry in self.entries)

    def add(self, entry: Entry) -> None:
        """Add or replace a title. Same save name means the same title again."""
        for index, existing in enumerate(self.entries):
            if existing.save_name and existing.save_name == entry.save_name:
                self.entries[index] = entry
                return
        self.entries.append(entry)

    def playbacks(self) -> list[Playback]:
        return [entry.playback() for entry in self.entries]

    def label(self) -> str:
        """One line: what is in here."""
        if not self.entries:
            return f"{self.service_name or self.service}  ·  nothing in it"
        first = self.entries[0].label()
        count = len(self.entries)
        titles = first if count == 1 else f"{first}  +{count - 1} more"
        return f"{self.service_name or self.service}  ·  {titles}"

    def as_document(self) -> dict[str, Any]:
        return {
            "kind": KIND,
            "version": VERSION,
            "service": self.service,
            "service_name": self.service_name,
            "created": self.created or datetime.now().isoformat(timespec="seconds"),
            "app": self.app,
            "titles": [entry.as_document() for entry in self.entries],
        }


def _playback_metadata(playback: Playback) -> dict[str, str]:
    return source_metadata({
        "url": playback.manifest_url or playback.manifest_base_url or "",
        "requested": playback.requested, "returned": playback.returned, "note": playback.note,
    })


def _subtitle_sources(playback: Playback, tracks: Any) -> list[dict[str, Any]]:
    """Export all available API subtitles, not only the selected languages."""
    origins = [playback]
    for origin in getattr(tracks, "origins", {}).values():
        if all(origin is not item for item in origins):
            origins.append(origin)
    visible = [*getattr(tracks, "streams", []), *getattr(tracks, "sidecars", [])]
    selected = {id(stream) for stream in getattr(tracks, "selected", [])}
    rows: dict[str, SubtitleReference] = {}
    for origin in origins:
        for reference in origin.subtitle_references:
            matching = [
                stream for stream in visible
                if stream.extra.get("subtitle_reference_url") == reference.url
                and stream.language == reference.language
                and (stream.extra.get("subtitle_kind") or stream.group_id or "normal") == reference.kind
                and (
                    not stream.extra.get("portable_subtitle_reference")
                    or subtitle_source_identity(stream.extra["portable_subtitle_reference"]) == subtitle_source_identity(reference.as_document())
                )
            ]
            # Shared headers already live on Entry. Only explicit subtitle
            # overrides should outrank caller-supplied refreshed headers.
            headers = dict(reference.headers)
            suffix = Path(urlsplit(reference.url).path).suffix.lower().lstrip(".")
            source_format = suffix if suffix in {"srt", "vtt", "webvtt", "ttml", "dfxp", "ass", "ssa"} else ""
            item = replace(
                reference, headers=headers,
                format=reference.format or source_format or next((stream.extension for stream in matching if stream.extension), ""),
                selected=any(id(stream) in selected for stream in matching) if matching else reference.selected,
                sources=tuple(merge_source_metadata(reference.sources or [_playback_metadata(origin)])),
            )
            identity = subtitle_source_identity(item.as_document())
            previous = rows.get(identity)
            if previous is not None:
                item = replace(item, selected=item.selected or previous.selected,
                               original=item.original or previous.original,
                               sources=tuple(merge_source_metadata(previous.sources, item.sources)))
            rows[identity] = item
    # Generic imports keep native subtitle records rather than service sidecars.
    # Preserve their provenance even after an export is imported and re-exported.
    result = [item.as_document() for item in rows.values()]
    for stream in visible:
        reference = stream.extra.get("portable_subtitle_reference")
        if reference is None and stream.extra.get("service_sidecar") == "subtitle" and stream.url:
            if any(
                item["url"] == (stream.extra.get("subtitle_reference_url") or stream.url)
                and item["language"] == (stream.language or "und")
                and item["kind"] == (stream.extra.get("subtitle_kind") or stream.group_id or "normal")
                for item in result
            ):
                continue
            reference = SubtitleReference(
                url=str(stream.extra.get("subtitle_reference_url") or stream.url),
                language=stream.language or "und",
                kind=stream.extra.get("subtitle_kind") or stream.group_id or "normal",
                name=stream.name or "", format=stream.extension or "",
                original=bool(stream.extra.get("subtitle_original")),
                sources=tuple(merge_source_metadata(stream.extra.get("source_metadata"), [_playback_metadata(playback)])),
            ).as_document()
        if reference and not any(
            subtitle_source_identity(item) == subtitle_source_identity(reference) for item in result
        ):
            result.append({**reference, "selected": id(stream) in selected})
    return result


def _master_references(playback: Playback, tracks: Any) -> tuple[str, ...]:
    """Recover actual authorized references, never synthesized API requests.

    The backend permits a merge only when parse policies agree. If provenance is
    absent, an origin is inline/JSON, or request context differs, retain the
    original source bundle instead of silently dropping part of the inventory.
    """
    if not getattr(tracks, "merged_profiles", False) or playback.is_live or playback.merge_manifest_segments:
        return ()
    parsed = getattr(tracks, "manifest", None)
    source = getattr(getattr(parsed, "request", None), "source", None)
    document = getattr(source, "json_document", None)
    if document and BUNDLE_MARKER in document:
        records = document.get("sources", [])
        # Runtime dedup may remove every representation from a variant. Its
        # provenance must survive even when no remaining object points to it.
        origin_count = len({id(origin) for origin in getattr(tracks, "origins", {}).values()})
        if any(record.get("metadata") for record in records) or len(records) > origin_count:
            return ()
    if playback.split_muxed_audio or any(stream.extra.get("_unidl_demux_media") for stream in tracks.streams):
        return ()  # The source bundle retains the service-opted-in separation policy.
    origins = getattr(tracks, "origins", {})
    urls: list[str] = []
    labels: dict[str, dict[str, str]] = {}
    for stream in tracks.streams:
        origin = origins.get(id(stream))
        if origin is None or origin.inline_manifest or origin.json_manifest is not None:
            return ()
        url = str(origin.manifest_url or "")
        metadata = _playback_metadata(origin)
        if url in labels and labels[url] != metadata:
            return ()  # Identical URLs may belong to independently authorized profiles.
        labels[url] = metadata
        if urlsplit(url).scheme not in {"http", "https"}:
            return ()
        if (
            origin.headers != playback.headers or origin.proxy != playback.proxy
            or origin.manifest_base_url != playback.manifest_base_url
        ):
            return ()
        if url not in urls:
            urls.append(url)
    if playback.manifest_url in urls:
        urls.remove(playback.manifest_url)
        urls.insert(0, playback.manifest_url)
    return tuple(urls)


def entry_for(
    playback: Playback,
    tracks: Any = None,
    *,
    media_manifest: dict[str, Any] | None = None,
    selection: dict[str, Any] | None = None,
) -> Entry:
    """Everything about ``playback`` worth writing down, as one entry.

    The ladder is stored the way the command file stores it - one row per stream,
    marked taken or left - rather than as structured track objects. Those are
    UniDL's, they change shape between versions, and the import reads the
    manifest again anyway: what the rows are for is telling a reader what they are
    getting before they spend an hour fetching it.
    """
    drm = playback.drm
    rows: list[str] = []
    summary = ""
    manifest_url = _text(playback.manifest_url)
    json_manifest = playback.json_manifest
    if playback.inline_manifest:
        text = str(playback.inline_manifest)
        json_manifest = source_bundle([{
            "type": "hls" if text.lstrip().startswith("#EXTM3U") else "dash",
            "text": text, "base_url": playback.manifest_base_url or "",
        }])
        manifest_url = ""
    alternate_manifest_urls = tuple(playback.imported_manifest_urls[1:])
    merge_manifests = bool(alternate_manifest_urls)
    if tracks is not None:
        chosen = {id(stream) for stream in (tracks.selected or [])}
        for stream in [*tracks.streams, *getattr(tracks, "sidecars", [])]:
            mark = TAKEN if id(stream) in chosen else LEFT
            rows.append(f"{mark} {stream.format_line()}")
        summary = tracks.summary()
        parsed = getattr(tracks, "manifest", None)
        request = getattr(parsed, "request", None)
        source = getattr(request, "source", None)
        master_urls = _master_references(playback, tracks) if media_manifest is None else ()
        if master_urls:
            manifest_url, *alternate_urls = master_urls
            alternate_manifest_urls = tuple(alternate_urls)
            merge_manifests = bool(alternate_manifest_urls)
            json_manifest = None
            origins = [playback, *getattr(tracks, "origins", {}).values()]
            records = [
                {"type": "reference", "url": url,
                 "metadata": next((_playback_metadata(origin) for origin in origins if origin.manifest_url == url), {})}
                for url in master_urls
            ]
            if any(set(record["metadata"]) - {"url"} for record in records):
                json_manifest = source_bundle(records)
                manifest_url, alternate_manifest_urls, merge_manifests = "", (), False
        elif getattr(source, "json_document", None) is not None:
            # A multi-profile ladder is represented by the native backend as one
            # synthetic typed JSON manifest. Export that runnable source rather
            # than only the primary service URL, which would silently discard the
            # additional resolutions on import.
            json_manifest = dict(source.json_document)
            manifest_url = ""
            alternate_manifest_urls = ()
            merge_manifests = False
        elif getattr(source, "reference", None):
            manifest_url = _text(source.reference)
        elif getattr(source, "inline_text", None):
            json_manifest = source_bundle([{
                "type": "hls" if source.kind.value == "inline_hls" else "dash",
                "text": source.inline_text,
                "base_url": request.policy.base_url or "",
            }])
            manifest_url = ""
    if media_manifest is not None:
        # Media mode deliberately replaces the short-lived entry manifest with
        # original media playlist/MPD/ISM text. Keep request headers and
        # resolved keys on the Entry, but never retain the master as a fallback:
        # doing so would make an expired URL look usable again on import.
        json_manifest = dict(media_manifest)
        manifest_url = ""
        alternate_manifest_urls = ()
        merge_manifests = False
    subtitles = _subtitle_sources(playback, tracks)
    if subtitles:
        if json_manifest is None:
            json_manifest = source_bundle([
                {"type": "reference", "url": url, "metadata": _playback_metadata(playback)}
                for url in dict.fromkeys((manifest_url, *alternate_manifest_urls)) if url
            ])
        json_manifest = with_subtitle_sources(json_manifest, subtitles)
        manifest_url, alternate_manifest_urls, merge_manifests = "", (), False
    return Entry(
        save_name=playback.save_name,
        release_name_base=playback.release_name_base,
        title=playback.title,
        manifest_url=manifest_url,
        alternate_manifest_urls=alternate_manifest_urls,
        merge_manifests=merge_manifests,
        manifest_base_url=_text(playback.manifest_base_url),
        json_manifest=json_manifest,
        headers=dict(playback.headers),
        proxy=_text(playback.proxy),
        is_live=bool(playback.is_live),
        note=_text(playback.note),
        requested=_text(playback.requested),
        returned=_text(playback.returned),
        drm_system=_text(drm.system) if drm is not None else "",
        pssh=_text(drm.pssh) if drm is not None else "",
        wrm_header=_text(drm.wrm_header) if drm is not None else "",
        drm_clear=bool(drm.clear) if drm is not None else False,
        hls_key=_text(drm.hls_key) if drm is not None else "",
        hls_iv=_text(drm.hls_iv) if drm is not None else "",
        hls_method=_text(drm.hls_method) if drm is not None else "",
        keys=list(playback.keys),
        tracks=rows,
        summary=summary,
        chapters=list(playback.chapters),
        attachments=list(playback.attachments),
        lyrics=playback.lyrics,
        audio_codec_hint=playback.audio_codec_hint,
        selection=dict(selection or {}),
    )


def _bounded_json(document: dict[str, Any], *, pretty: bool) -> str:
    encoder = json.JSONEncoder(
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    size = 1  # final newline
    with StringIO() as buffer:
        for chunk in encoder.iterencode(document):
            size += len(chunk.encode("utf-8"))
            if size > MAX_EXPORT_BYTES:
                raise ExportError("export size limit exceeded")
            buffer.write(chunk)
        buffer.write("\n")
        return buffer.getvalue()


def dumps(document: Document) -> str:
    """Source manifests only, bounded before an atomic file replacement."""
    try:
        raw = document.as_document()
        try:
            return _bounded_json(raw, pretty=True)
        except ExportError:
            return _bounded_json(raw, pretty=False)
    except ManifestSourceError as exc:
        raise ExportError(str(exc)) from exc
    except ExportError as exc:
        raise ExportError(
            f"Export exceeds the {MAX_EXPORT_BYTES // (1024 * 1024)} MiB safety limit. "
            "Export fewer titles or use master manifest mode. No export file was written or replaced."
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ExportError("Export contains unsupported JSON data; no export file was written or replaced.") from exc


def loads(text: str) -> Document:
    """Read a document, or say exactly why it cannot be read."""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExportError(f"not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ExportError("not an export: the file does not describe one")
    if _text(raw.get("kind")) != KIND:
        # Keep foreign schemas outside this module. A converter returns the same
        # neutral Document model consumed by the TUI and native downloader, while
        # source_format prevents an installed service from being entered.
        from .third_party_exports import load as load_third_party

        return load_third_party(raw)
    version = raw.get("version")
    if type(version) is not int or version not in {1, 2, VERSION}:
        raise ExportError(
            f"made by a different version of the format ({version!r}, this build reads {VERSION})"
        )
    service = _text(raw.get("service"))
    if not service:
        raise ExportError("does not say which service it came from")
    titles = raw.get("titles")
    if not isinstance(titles, list) or not titles:
        raise ExportError("has no titles in it")
    document = Document(
        service=service,
        service_name=_text(raw.get("service_name")) or service,
        created=_text(raw.get("created")),
        app=_text(raw.get("app")),
        entries=[_entry_from(entry) for entry in titles],
    )
    if version == VERSION:
        for entry in document.entries:
            if entry.json_manifest is not None:
                try:
                    source_manifest(entry.json_manifest)
                    if BUNDLE_MARKER in entry.json_manifest:
                        validate_bundle(entry.json_manifest)
                except ManifestSourceError as exc:
                    raise ExportError(str(exc)) from exc
    return document


def write(path: Path, document: Document) -> Path:  # noqa: D417 - documented below
    """Write the file, readable only by its owner.

    The same treatment ``unidl.yaml`` and the token tree get, for the same reason:
    this holds content keys and, usually, the Authorization header that fetches the
    manifest. It is a file made to be handed to someone - deliberately - and that
    is a choice its owner should have to make rather than one the file system makes
    for them.
    """
    atomic_write_text(path, dumps(document))
    # A write inside the same clock tick as the last read would otherwise be
    # invisible to `scan`, which trusts the timestamp to tell it what changed.
    forget(path)
    return path


def read(path: Path) -> Document:
    try:
        text = Path(path).read_text("utf-8")
    except OSError as exc:
        raise ExportError(f"could not be read ({exc})") from exc
    document = loads(text)
    for index, entry in enumerate(document.entries):
        if entry.json_manifest is not None:
            try:
                source_manifest(entry.json_manifest)
            except ManifestSourceError:
                entry.legacy_source_path = str(Path(path).resolve())
                entry.legacy_entry_index = index
    return document


#: Parsed documents, keyed by the file's identity rather than by its name: a file
#: that has not been touched cannot have changed, and one that has gets a new key
#: and is read again. Bounded by :data:`_CACHE_LIMIT` because this is a folder, not
#: a database, and because the whole point is to avoid work on a folder small
#: enough to be listed on one screen.
_CACHE_LIMIT = 200
_cache: dict[tuple[str, int, int], tuple[Document | None, str]] = {}


def _stamp(path: Path) -> tuple[str, int, int] | None:
    """What identifies this file's contents, or None if it cannot be measured."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path), int(info.st_mtime_ns), int(info.st_size))


def forget(path: Path | None = None) -> None:
    """Drop what was remembered about ``path``, or about everything.

    Only needed by something that writes a file and then reads it back inside the
    same tick, before the timestamp it was cached against could differ.
    """
    if path is None:
        _cache.clear()
        return
    for key in [key for key in _cache if key[0] == str(path)]:
        _cache.pop(key, None)


def scan(folder: Path) -> list[tuple[Path, Document | None, str]]:
    """Every export under ``folder``, newest first, with why any of them cannot be read.

    A file that cannot be read is still listed, with its reason. A folder that
    silently drops the file you just put in it is a folder you stop trusting.

    Parsing is remembered per file, because this is called far more often than the
    folder changes: the main screen counts what is waiting to be imported every time
    it refreshes, and every unchanged file was being read off disk and parsed again
    to answer a question whose answer was already known.
    """
    root = Path(folder)
    if not root.is_dir():
        return []

    def when(path: Path) -> float:
        # This folder is one people drop files into and take them out of, so a
        # name from the listing may already be gone by the time it is measured.
        # Sorting must not be the thing that raises.
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    found: list[tuple[Path, Document | None, str]] = []
    seen: set[tuple[str, int, int]] = set()
    for path in sorted(root.rglob("*.json"), key=when, reverse=True):
        stamp = _stamp(path)
        if stamp is not None and stamp in _cache:
            document, problem = _cache[stamp]
            seen.add(stamp)
            found.append((path, document, problem))
            continue
        try:
            entry: tuple[Document | None, str] = (read(path), "")
        except ExportError as exc:
            entry = (None, str(exc))
        if stamp is not None:
            if len(_cache) >= _CACHE_LIMIT:
                _cache.clear()
            _cache[stamp] = entry
            seen.add(stamp)
        found.append((path, entry[0], entry[1]))
    # a file that was replaced or removed leaves its old stamp behind, and the
    # entry it keeps alive is a whole parsed document
    for key in [key for key in _cache if key[0].startswith(str(root)) and key not in seen]:
        _cache.pop(key, None)
    return found


def file_name(document: Document, when: datetime | None = None, *, batch: bool = False) -> str:
    """Name a batch for its series, never for the first episode in it."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    first = document.entries[0].save_name if document.entries else ""
    stem = first or document.service or "export"
    if batch or len(document.entries) > 1:
        names = {
            entry.title.name.strip().casefold(): entry.title.name
            for entry in document.entries
            if entry.title is not None and entry.title.name.strip()
        }
        complete = all(entry.title is not None and entry.title.name.strip() for entry in document.entries)
        series = next(iter(names.values())) if complete and len(names) == 1 else ""
        stem = clean_part(series) if series else f"{clean_part(document.service) or 'export'}.Batch"
        stem = safe_folder_component(stem, fallback="export.Batch")
    return f"{stem}_{stamp}.json"


__all__ = [
    "KIND",
    "LEFT",
    "TAKEN",
    "VERSION",
    "Document",
    "Entry",
    "ExportError",
    "dumps",
    "entry_for",
    "file_name",
    "forget",
    "loads",
    "read",
    "scan",
    "write",
]
