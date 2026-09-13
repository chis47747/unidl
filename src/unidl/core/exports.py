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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .chapters import Chapter
from .lyrics import Lyrics
from .playback import DrmInfo, Playback
from .secureio import atomic_write_text
from .titles import Title, TitleKind

#: The document format. Read strictly: a file from a later version may describe
#: something this build would silently get wrong, and "it downloaded the wrong
#: thing" is a worse outcome than "it refused to open the file".
VERSION = 1

#: what the file is, for anyone who opens it in an editor
KIND = "unidl-export"

#: Marks in the exported ladder, the same two the track card uses.
TAKEN = "+"
LEFT = "-"


class ExportError(RuntimeError):
    """A file that is not an export, or not one this build can read."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _title_document(title: Title) -> dict[str, Any]:
    """A title as plain JSON.

    ``data`` is the service's private payload and is carried whole or not at all:
    half of it is worse than none, because the half that survived looks complete.
    Nothing on the import side reads it - no service code runs there - so losing
    it costs the export nothing but the ability to say what it was.
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
    if title.data and _json_safe(title.data):
        document["data"] = title.data
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
    manifest_base_url: str = ""
    json_manifest: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    proxy: str = ""
    is_live: bool = False
    note: str = ""
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
    lyrics: Lyrics | None = None
    audio_codec_hint: str = ""

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
            manifest_url=self.manifest_url or None,
            manifest_base_url=self.manifest_base_url or None,
            json_manifest=self.json_manifest,
            headers=dict(self.headers),
            proxy=self.proxy or None,
            is_live=self.is_live,
            drm=drm,
            keys=list(self.keys),
            note=self.note,
            chapters=list(self.chapters),
            lyrics=self.lyrics,
            audio_codec_hint=self.audio_codec_hint,
        )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "save_name": self.save_name,
            "title": _title_document(self.title) if self.title is not None else {},
            "keys": list(self.keys),
        }
        if self.manifest_url:
            document["manifest_url"] = self.manifest_url
        if self.manifest_base_url:
            document["manifest_base_url"] = self.manifest_base_url
        if self.json_manifest is not None and _json_safe(self.json_manifest):
            document["json_manifest"] = self.json_manifest
        if self.headers:
            document["headers"] = dict(self.headers)
        if self.proxy:
            document["proxy"] = self.proxy
        if self.is_live:
            document["is_live"] = True
        if self.note:
            document["note"] = self.note
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
        if self.lyrics is not None:
            document["lyrics"] = self.lyrics.as_document()
        if self.audio_codec_hint:
            document["audio_codec_hint"] = self.audio_codec_hint
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
    return Entry(
        save_name=_text(document.get("save_name")),
        title=_title_from(document.get("title") or {}),
        manifest_url=manifest,
        manifest_base_url=_text(document.get("manifest_base_url")),
        json_manifest=json_manifest if isinstance(json_manifest, dict) else None,
        headers={str(k): str(v) for k, v in (document.get("headers") or {}).items()},
        proxy=_text(document.get("proxy")),
        is_live=bool(document.get("is_live")),
        note=_text(document.get("note")),
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
        lyrics=lyrics,
        audio_codec_hint=_text(document.get("audio_codec_hint")),
    )


@dataclass
class Document:
    """One export file: a service, and the titles resolved from it in one run."""

    service: str = ""
    service_name: str = ""
    created: str = ""
    app: str = ""
    entries: list[Entry] = field(default_factory=list)

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


def entry_for(playback: Playback, tracks: Any = None) -> Entry:
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
    if tracks is not None:
        chosen = {id(stream) for stream in (tracks.selected or [])}
        for stream in tracks.streams:
            mark = TAKEN if id(stream) in chosen else LEFT
            rows.append(f"{mark} {stream.format_line()}")
        summary = tracks.summary()
        parsed = getattr(tracks, "manifest", None)
        request = getattr(parsed, "request", None)
        source = getattr(request, "source", None)
        if getattr(source, "json_document", None) is not None:
            # A multi-profile ladder is represented by the native backend as one
            # synthetic typed JSON manifest. Export that runnable source rather
            # than only the primary service URL, which would silently discard the
            # additional resolutions on import.
            json_manifest = dict(source.json_document)
            manifest_url = ""
        elif getattr(source, "reference", None):
            manifest_url = _text(source.reference)
    return Entry(
        save_name=playback.save_name,
        title=playback.title,
        manifest_url=manifest_url,
        manifest_base_url=_text(playback.manifest_base_url),
        json_manifest=json_manifest,
        headers=dict(playback.headers),
        proxy=_text(playback.proxy),
        is_live=bool(playback.is_live),
        note=_text(playback.note),
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
        lyrics=playback.lyrics,
        audio_codec_hint=playback.audio_codec_hint,
    )


def dumps(document: Document) -> str:
    """The file's text. Indented, because it is meant to be readable."""
    return json.dumps(document.as_document(), indent=2, ensure_ascii=False) + "\n"


def loads(text: str) -> Document:
    """Read a document, or say exactly why it cannot be read."""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExportError(f"not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ExportError("not an export: the file does not describe one")
    if _text(raw.get("kind")) != KIND:
        raise ExportError("not a unidl export")
    version = raw.get("version")
    if version != VERSION:
        raise ExportError(
            f"made by a different version of the format ({version!r}, this build reads {VERSION})"
        )
    service = _text(raw.get("service"))
    if not service:
        raise ExportError("does not say which service it came from")
    titles = raw.get("titles")
    if not isinstance(titles, list) or not titles:
        raise ExportError("has no titles in it")
    return Document(
        service=service,
        service_name=_text(raw.get("service_name")) or service,
        created=_text(raw.get("created")),
        app=_text(raw.get("app")),
        entries=[_entry_from(entry) for entry in titles],
    )


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
    return loads(text)


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


def file_name(document: Document, when: datetime | None = None) -> str:
    """What to call the file: the first title, and when the run happened."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    first = document.entries[0].save_name if document.entries else ""
    stem = first or document.service or "export"
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
