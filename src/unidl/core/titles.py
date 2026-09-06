"""Title model.

Live TV is a first-class citizen here: ``CHANNEL`` and ``PROGRAM`` sit at the
same level as ``MOVIE`` and ``EPISODE``, not as a special case bolted on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TitleKind(str, Enum):
    MOVIE = "movie"
    EPISODE = "episode"
    EXTRA = "extra"
    CLIP = "clip"
    CHANNEL = "channel"
    PROGRAM = "program"
    #: audio: a radio programme or podcast episode, and a live radio station.
    #: Peers of the video kinds rather than a flag on them, because a station is
    #: a channel in every respect except that there is no picture.
    TRACK = "track"
    STATION = "station"

    @property
    def is_live(self) -> bool:
        return self in (TitleKind.CHANNEL, TitleKind.PROGRAM, TitleKind.STATION)

    @property
    def is_audio(self) -> bool:
        return self in (TitleKind.TRACK, TitleKind.STATION)


@dataclass
class Title:
    """One downloadable thing.

    ``data`` is the service's private payload; core never inspects it.
    """

    id: str
    kind: TitleKind
    name: str
    year: str | None = None
    season: int | None = None
    episode: int | None = None
    episode_name: str | None = None
    language: str | None = None
    duration: float | None = None

    # live / EPG
    channel: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    # audio. ID3 has fields video has no use for, and a radio programme is the
    # only place they mean anything, so they live here rather than in `data`
    # where core would not be allowed to look at them.
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    genre: str | None = None
    publisher: str | None = None
    synopsis: str | None = None
    cover_url: str | None = None

    service: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.kind.is_live

    @property
    def is_audio(self) -> bool:
        return self.kind.is_audio

    def audio_tags(self) -> dict[str, Any]:
        """ID3 tags for this title, in the shape UniDL's sidecar expects.

        Only what is actually known: an empty tag is worse than a missing one,
        because a player shows it as an empty field rather than falling back.
        """
        tags: dict[str, Any] = {
            "title": self.episode_name or self.name,
            "artist": self.artist or self.channel,
            "album": self.album or (self.name if self.episode_name else None),
            "album_artist": self.artist or self.channel,
            "date": self.year or (self.starts_at.strftime("%Y-%m-%d") if self.starts_at else None),
            "track": self.track_number or self.episode,
            "genre": self.genre,
            "publisher": self.publisher,
            "comment": self.synopsis,
        }
        tags = {name: value for name, value in tags.items() if value not in (None, "")}
        if self.cover_url:
            tags["cover"] = {"url": self.cover_url}
        return tags

    def label(self) -> str:
        """Human label for pickers and queue rows."""
        if self.kind is TitleKind.EPISODE and self.episode:
            head = (
                f"S{self.season:02d}E{self.episode:02d}"
                if self.season is not None
                else f"E{self.episode:02d}"
            )
            return f"{head} {self.episode_name}" if self.episode_name else head
        if self.kind is TitleKind.PROGRAM:
            when = self.starts_at.strftime("%H:%M") if self.starts_at else ""
            return f"{when} {self.name}".strip()
        if self.kind is TitleKind.MOVIE and self.year:
            return f"{self.name} ({self.year})"
        return self.episode_name or self.name

    def full_label(self) -> str:
        """Label including the series name, for flat lists like the queue."""
        if self.kind is TitleKind.EPISODE:
            return f"{self.name} {self.label()}".strip()
        if self.kind is TitleKind.PROGRAM and self.channel:
            return f"{self.channel} - {self.label()}"
        return self.label()
