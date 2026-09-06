from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SegmentInfo:
    url: str
    duration: float | None = None
    index: int | None = None
    byte_range: tuple[int, int] | None = None
    allow_range_status_200: bool = False
    data: bytes | None = None
    encrypted: bool = False
    encryption_scheme: str | None = None
    key_id: str | None = None
    key_uri: str | None = None
    key_iv: bytes | None = None
    program_date_time: str | None = None
    gap: bool = False
    discontinuity_after: bool = False
    timeline_time: int | None = None
    timeline_presentation_time: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "duration": self.duration,
            "index": self.index,
            "timeline_time": self.timeline_time,
            "timeline_presentation_time": self.timeline_presentation_time,
            "byte_range": list(self.byte_range) if self.byte_range else None,
            "allow_range_status_200": self.allow_range_status_200,
            "inline_bytes": len(self.data) if self.data is not None else None,
            "encrypted": self.encrypted,
            "encryption_scheme": self.encryption_scheme,
            "key_id": self.key_id,
            "key_uri": self.key_uri,
            "key_iv": self.key_iv.hex() if self.key_iv else None,
            "program_date_time": self.program_date_time,
            "gap": self.gap,
            "discontinuity_after": self.discontinuity_after,
        }


@dataclass(slots=True)
class StreamInfo:
    manifest_type: str
    media_type: str = "unknown"
    url: str = ""
    original_url: str = ""
    id: str | None = None
    group_id: str | None = None
    name: str | None = None
    language: str | None = None
    role: str | None = None
    bandwidth: int | None = None
    codecs: str | None = None
    resolution: str | None = None
    frame_rate: float | None = None
    channels: str | None = None
    extension: str | None = None
    video_range: str | None = None
    duration: float | None = None
    size_bytes: int | None = None
    encrypted: bool = False
    encryption_scheme: str | None = None
    is_live: bool = False
    segments: list[SegmentInfo] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def segments_count(self) -> int:
        return sum(1 for segment in self.segments if segment.index != -1)

    @property
    def total_duration(self) -> float | None:
        segment_total = sum(segment.duration or 0 for segment in self.segments)
        if segment_total > 0:
            return segment_total
        return self.duration

    @property
    def estimated_size_bytes(self) -> int | None:
        if self.size_bytes is not None:
            return self.size_bytes
        duration = self.total_duration
        if self.bandwidth and duration:
            return int(self.bandwidth * duration / 8)
        return None

    def display_prefix(self) -> str:
        media_type = self.media_type.lower()
        if media_type == "audio":
            return "Aud"
        if media_type in {"subtitle", "subtitles", "text"}:
            return "Sub"
        if media_type == "video":
            return "Vid"
        return "Med"

    def format_line(self, index: int | None = None) -> str:
        from .utils import (
            compact_join,
            format_bitrate,
            format_frame_rate,
            format_size,
            format_time,
            pretty_codec,
        )

        prefix = self.display_prefix()
        if self.encrypted:
            prefix = f"{prefix} *{self.encryption_scheme or 'ENC'}"

        descriptor = self.role or (self.name if self.media_type == "video" else None)
        common_tail: list[str | None] = [
            self.segment_summary(),
            descriptor,
            format_time(self.total_duration),
            self.video_range if self.media_type == "video" else None,
            format_size(self.estimated_size_bytes),
            "Encrypted" if self.encrypted else None,
        ]

        if self.media_type == "audio":
            display_id = self.group_id or self.id
            if (
                self.manifest_type == "dash"
                and self.id
                and self.group_id
                and self.id != self.group_id
                and self.group_id.lower() in self.id.lower()
            ):
                display_id = self.id
            display_name = self.name if self.name not in {display_id, self.group_id, self.id} else None
            codec_label = pretty_codec(self.codecs, self.media_type)
            if self.extra.get("audio_atmos") and codec_label in {"AC-3", "E-AC-3"}:
                codec_label = "E-AC-3 Atmos"
            parts = [
                display_id,
                format_bitrate(self.bandwidth),
                display_name,
                codec_label,
                self.language,
                f"{self.channels}CH" if self.channels else None,
                *common_tail,
            ]
        elif self.media_type in {"subtitle", "subtitles", "text"}:
            display_name = self.name if self.name not in {self.group_id, self.id} else None
            parts = [
                self.group_id or self.id,
                self.language,
                display_name,
                pretty_codec(self.codecs, self.media_type),
                *common_tail,
            ]
        else:
            parts = [
                self.resolution,
                format_bitrate(self.bandwidth),
                format_frame_rate(self.frame_rate),
                pretty_codec(self.codecs, self.media_type),
                "Muxed Audio" if self.extra.get("muxed_audio") else None,
                *common_tail,
            ]

        line = f"{prefix} {compact_join(parts)}".strip()
        return f"[{index}] {line}" if index is not None else line

    def segment_summary(self) -> str | None:
        from .utils import format_segments

        count = self.segments_count
        if count <= 0:
            return None
        media_segments = [segment for segment in self.segments if segment.index != -1]
        byte_range_urls = {segment.url for segment in media_segments if segment.byte_range and segment.url}
        if (
            self.manifest_type in {"dash", "json", "direct"}
            and count > 1
            and media_segments
            and len(byte_range_urls) == 1
            and all(segment.byte_range for segment in media_segments)
            and not self.extra.get("dash_full_base_url_mode")
        ):
            return f"1 File / {count} Range" if count == 1 else f"1 File / {count} Ranges"
        return format_segments(count)

    def as_dict(self, include_segments: bool = False) -> dict[str, Any]:
        data = {
            "manifest_type": self.manifest_type,
            "media_type": self.media_type,
            "url": self.url,
            "original_url": self.original_url,
            "id": self.id,
            "group_id": self.group_id,
            "name": self.name,
            "language": self.language,
            "role": self.role,
            "bandwidth": self.bandwidth,
            "codecs": self.codecs,
            "resolution": self.resolution,
            "frame_rate": self.frame_rate,
            "channels": self.channels,
            "extension": self.extension,
            "video_range": self.video_range,
            "duration": self.total_duration,
            "size_bytes": self.estimated_size_bytes,
            "encrypted": self.encrypted,
            "encryption_scheme": self.encryption_scheme,
            "is_live": self.is_live,
            "segments_count": self.segments_count,
            "extra": self.extra,
        }
        if include_segments:
            data["segments"] = [segment.as_dict() for segment in self.segments]
        return data
