"""BBC Sounds: the audio half of bbc.py, as its own client.

Kept separate from iPlayer deliberately. Sounds is a different catalogue with
different endpoints, no DRM and no video, and the only thing the two share is a
hostname.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

BASE = "https://rms.api.bbc.co.uk"
SELECTOR = "https://open.live.bbc.co.uk/mediaselector/6/select"

ENDPOINTS = {
    "episode": f"{BASE}/v2/experience/inline/play/{{pid}}",
    "container": f"{BASE}/v2/programmes/playable",
    "live_catalog": f"{BASE}/v2/experience/inline/listen/sign-in?continue_listening_control=true",
    "live_token": f"{BASE}/v2/sign/token/{{service_id}}",
    "live_media": (f"{SELECTOR}/version/3.0/mediaset/pc/cvid/urn:bbc:pips:pid:{{service_id}}/format/json/cors/1"),
    "audio_media": f"{SELECTOR}/version/2.0/mediaset/audio-syndication/vpid/{{vpid}}/format/json",
}

USER_AGENT = "BBCSounds/3.1.0"
PAGE_SIZE = 30
TIMEOUT = 20


class SoundsError(RuntimeError):
    """A BBC Sounds request failed, or returned something unusable."""


@dataclass
class AudioSource:
    """One playable audio stream, and what it is."""

    url: str
    bitrate: int = 0
    encoding: str = "AAC"
    #: a progressive MP3 file rather than a manifest, which needs no re-encoding
    direct_file: bool = False

    @property
    def quality(self) -> str:
        return f"{self.bitrate}kbps {self.encoding}" if self.bitrate else self.encoding


@dataclass
class SoundsItem:
    """A programme episode or a live station, flattened out of the API payload."""

    id: str
    programme: str
    episode: str = ""
    album: str = ""
    network: str = ""
    synopsis: str = ""
    release_date: str = ""
    image_url: str = ""
    duration: float | None = None
    live: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedInput:
    kind: str  # episode | container | live
    value: str


_PID = re.compile(r"^[a-z0-9]{8,}$", re.IGNORECASE)


def parse_input(text: str) -> ParsedInput | None:
    """Work out what a URL, a PID or a ``live:`` reference points at."""
    raw = (text or "").strip()
    if not raw:
        return None

    if raw.lower().startswith("live:"):
        value = raw.split(":", 1)[1].strip()
        return ParsedInput("live", value) if value else None

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"}:
            return None
        if (parsed.hostname or "").lower() not in {"bbc.co.uk", "www.bbc.co.uk", "bbc.com", "www.bbc.com"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0] != "sounds":
            return None
        tail = parts[1:]
        if not tail:
            return None
        section, *rest = tail
        if section == "play" and rest:
            target = rest[-1] if rest[0] == "live" else rest[0]
            if rest[0] == "live" or target.startswith("live:"):
                value = target.removeprefix("live:")
                return ParsedInput("live", value) if value else None
            return ParsedInput("episode", target)
        if section in {"brand", "series"} and rest:
            return ParsedInput("container", rest[0])
        return None

    if _PID.fullmatch(raw):
        # a bare pid is ambiguous; the episode endpoint is the common case and
        # fails cleanly, at which point the caller can try it as a container
        return ParsedInput("episode", raw)
    return None


class SoundsApi:
    def __init__(self, session: requests.Session | None = None, proxy: str | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    # ------------------------------------------------------------------ fetch
    def _json(self, url: str, **kwargs: Any) -> dict:
        try:
            response = self.session.get(url, timeout=TIMEOUT, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise SoundsError(f"BBC Sounds request failed: {exc}") from exc
        except ValueError as exc:
            raise SoundsError(f"BBC Sounds returned something that is not JSON: {exc}") from exc

    @staticmethod
    def _playables(payload: dict, module_id: str | None = None) -> list[dict]:
        modules = payload.get("data") or []
        if module_id is not None:
            modules = [module for module in modules if module.get("id") == module_id]
        else:
            # the play area first, then whatever else the page carries
            modules = sorted(modules, key=lambda module: module.get("id") != "aod_play_area")
        found = []
        for module in modules:
            for item in module.get("data") or []:
                if item.get("type") == "playable_item":
                    found.append(item)
        return found

    # --------------------------------------------------------------- catalogue
    def episode(self, pid: str) -> SoundsItem:
        items = self._playables(self._json(ENDPOINTS["episode"].format(pid=pid)))
        if not items:
            raise SoundsError(f"No BBC Sounds episode found for {pid}")
        return self._item(items[0])

    def container(self, container_id: str, offset: int = 0, limit: int = PAGE_SIZE) -> tuple[list[SoundsItem], int]:
        payload = self._json(
            ENDPOINTS["container"],
            params={
                "container": container_id,
                "sort": "sequential",
                "type": "episode",
                "experience": "domestic",
                "limit": limit,
                "offset": offset,
            },
        )
        items = [self._item(item) for item in payload.get("data") or [] if item.get("type") == "playable_item"]
        return items, int(payload.get("total") or len(items))

    def stations(self) -> list[SoundsItem]:
        payload = self._json(ENDPOINTS["live_catalog"])
        return [
            self._item(item, live=True) for item in self._playables(payload, module_id="listen_live") if item.get("id")
        ]

    # ---------------------------------------------------------------- playback
    def source_for(self, item: SoundsItem) -> AudioSource:
        """The best stream for an episode.

        A progressive download is preferred where the API offers one: it is
        already an MP3, so taking it avoids a re-encode and whatever that costs.
        """
        url, bitrate = self._download_variant(item.raw)
        if url:
            return AudioSource(url=url, bitrate=bitrate, encoding="MP3", direct_file=True)
        if not item.id:
            raise SoundsError("This BBC Sounds item has no playable audio id")
        payload = self._json(ENDPOINTS["audio_media"].format(vpid=item.id))
        source = self._best_connection(payload.get("media"))
        if source is None:
            raise SoundsError("BBC Sounds returned no playable audio stream")
        return source

    def live_source_for(self, service_id: str) -> AudioSource:
        token = self._json(ENDPOINTS["live_token"].format(service_id=service_id)).get("token")
        if not token:
            raise SoundsError("BBC Sounds did not return a live radio token")
        payload = self._json(
            ENDPOINTS["live_media"].format(service_id=service_id),
            headers={"Authorization": f"Bearer {token}"},
        )
        source = self._best_connection(payload.get("media"))
        if source is None:
            raise SoundsError("BBC Sounds returned no playable live stream")
        return source

    # ------------------------------------------------------------------ shapes
    def _item(self, raw: dict, *, live: bool = False) -> SoundsItem:
        titles = raw.get("titles") or {}
        network = raw.get("network") or {}
        container = raw.get("container") or {}
        synopses = raw.get("synopses") or {}

        if live:
            programme = str(network.get("short_title") or raw.get("id") or "BBC Radio")
            episode = str(titles.get("primary") or "Live")
            album = programme
        else:
            programme = str(titles.get("primary") or container.get("title") or raw.get("id") or "")
            episode = self._episode_title(titles, programme, raw)
            album = str(container.get("title") or programme)

        return SoundsItem(
            id=str(raw.get("id") or ""),
            programme=programme,
            episode=episode,
            album=album,
            network=str(network.get("short_title") or ""),
            synopsis=str(synopses.get("short") or synopses.get("medium") or ""),
            release_date=str((raw.get("release") or {}).get("date") or ""),
            image_url=str(raw.get("image_url") or ""),
            duration=(raw.get("duration") or {}).get("value"),
            live=live,
            raw=raw,
        )

    @staticmethod
    def _episode_title(titles: dict, programme: str, raw: dict) -> str:
        for key in ("entity_title", "tertiary", "secondary"):
            value = str(titles.get(key) or "").strip()
            if value and value != programme:
                return value
        return programme or str(raw.get("id") or "")

    @staticmethod
    def _download_variant(raw: dict) -> tuple[str, int]:
        variants = (raw.get("download") or {}).get("quality_variants") or {}
        for name in ("high", "medium", "low"):
            variant = variants.get(name) or {}
            if variant.get("file_url"):
                try:
                    bitrate = int(variant.get("bitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                return str(variant["file_url"]), bitrate
        return "", 0

    @staticmethod
    def _best_connection(media: list[dict] | None) -> AudioSource | None:
        """Highest bitrate audio, over HTTPS, preferring HLS.

        The media selector returns video entries too on some mediasets, so the
        kind is filtered rather than assumed.
        """

        def bitrate_of(entry: dict) -> int:
            try:
                return int(entry.get("bitrate") or 0)
            except (TypeError, ValueError):
                return 0

        def priority_of(connection: dict) -> int:
            try:
                return int(connection.get("priority", 99))
            except (TypeError, ValueError):
                return 99

        audio = [entry for entry in media or [] if entry.get("kind") == "audio"]
        for entry in sorted(audio, key=bitrate_of, reverse=True):
            connections = sorted(
                entry.get("connection") or [],
                key=lambda connection: (
                    str(connection.get("protocol") or "").lower() != "https",
                    str(connection.get("transferFormat") or "").lower() != "hls",
                    priority_of(connection),
                ),
            )
            for connection in connections:
                href = connection.get("href")
                transfer = str(connection.get("transferFormat") or "").lower()
                if href and transfer in ("hls", "dash"):
                    return AudioSource(
                        url=str(href),
                        bitrate=bitrate_of(entry),
                        encoding=str(entry.get("encoding") or "AAC").upper(),
                    )
        return None


__all__ = [
    "ENDPOINTS",
    "PAGE_SIZE",
    "AudioSource",
    "ParsedInput",
    "SoundsApi",
    "SoundsError",
    "SoundsItem",
    "parse_input",
]
