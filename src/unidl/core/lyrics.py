"""Neutral timed-lyrics model shared by services, the TUI, and exports."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Any


def _local(name: str) -> str:
    return str(name).rsplit("}", 1)[-1]


def _clean_text(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", str(value or "")).strip()


def _time_ms(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    multiplier = 1_000.0
    if text.endswith("ms"):
        text, multiplier = text[:-2], 1.0
    elif text.endswith("s"):
        text = text[:-1]
    try:
        parts = [float(part) for part in text.split(":")]
    except ValueError:
        return None
    if not parts or any(not math.isfinite(part) or part < 0 for part in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return round(seconds * multiplier)


@dataclass(frozen=True, slots=True)
class LyricWord:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None

    def shifted(self, offset_ms: int) -> LyricWord:
        return replace(
            self,
            start_ms=max(0, self.start_ms + offset_ms) if self.start_ms is not None else None,
            end_ms=max(0, self.end_ms + offset_ms) if self.end_ms is not None else None,
        )


@dataclass(frozen=True, slots=True)
class LyricLine:
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    agent: str = ""
    section: str = ""
    words: tuple[LyricWord, ...] = ()

    def shifted(self, offset_ms: int) -> LyricLine:
        return replace(
            self,
            start_ms=max(0, self.start_ms + offset_ms) if self.start_ms is not None else None,
            end_ms=max(0, self.end_ms + offset_ms) if self.end_ms is not None else None,
            words=tuple(word.shifted(offset_ms) for word in self.words),
        )


@dataclass(frozen=True, slots=True)
class Lyrics:
    id: str = ""
    source_type: str = "lyrics"
    language: str = ""
    timing: str = "static"
    ttml: str = ""
    lines: tuple[LyricLine, ...] = ()
    spatial_offset_ms: int = 0
    applied_offset_ms: int = 0

    @property
    def plain_text(self) -> str:
        return "\n".join(line.text for line in self.lines if line.text).strip()

    @property
    def synchronized(self) -> bool:
        return any(line.start_ms is not None for line in self.lines)

    @property
    def word_timed(self) -> bool:
        return any(line.words for line in self.lines)

    def with_spatial_offset(self) -> Lyrics:
        if not self.spatial_offset_ms or self.applied_offset_ms:
            return self
        return replace(
            self,
            lines=tuple(line.shifted(self.spatial_offset_ms) for line in self.lines),
            applied_offset_ms=self.spatial_offset_ms,
        )

    def lrc_text(self) -> str:
        rows: list[str] = []
        if self.language:
            rows.append(f"[lang:{self.language}]")
        for line in self.lines:
            if line.start_ms is None:
                rows.append(line.text)
                continue
            minutes, remainder = divmod(line.start_ms, 60_000)
            seconds = remainder / 1_000
            rows.append(f"[{minutes:02d}:{seconds:05.2f}]{line.text}")
        return "\n".join(rows).rstrip() + "\n"

    def as_document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "language": self.language,
            "timing": self.timing,
            "ttml": self.ttml,
            "spatial_offset_ms": self.spatial_offset_ms,
            "applied_offset_ms": self.applied_offset_ms,
            "lines": [
                {
                    "text": line.text,
                    "start_ms": line.start_ms,
                    "end_ms": line.end_ms,
                    "agent": line.agent,
                    "section": line.section,
                    "words": [
                        {
                            "text": word.text,
                            "start_ms": word.start_ms,
                            "end_ms": word.end_ms,
                        }
                        for word in line.words
                    ],
                }
                for line in self.lines
            ],
        }

    @classmethod
    def from_document(cls, value: Any) -> Lyrics:
        if not isinstance(value, dict):
            raise TypeError("lyrics must be an object")
        ttml = str(value.get("ttml") or "").strip()
        if ttml:
            lyrics = parse_ttml(
                ttml,
                lyrics_id=str(value.get("id") or ""),
                source_type=str(value.get("source_type") or "lyrics"),
            )
        else:
            raw_lines = value.get("lines")
            if not isinstance(raw_lines, list):
                raise ValueError("lyrics contain neither TTML nor lines")

            def optional_int(raw: Any) -> int | None:
                if raw is None:
                    return None
                try:
                    return int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError("lyrics timing must be milliseconds") from exc

            lines: list[LyricLine] = []
            for raw_line in raw_lines:
                if not isinstance(raw_line, dict):
                    raise ValueError("lyrics lines must be objects")
                text = _clean_text(raw_line.get("text"))
                if not text:
                    continue
                raw_words = raw_line.get("words") or []
                if not isinstance(raw_words, list):
                    raise ValueError("lyric words must be a list")
                words = tuple(
                    LyricWord(
                        _clean_text(raw_word.get("text")),
                        optional_int(raw_word.get("start_ms")),
                        optional_int(raw_word.get("end_ms")),
                    )
                    for raw_word in raw_words
                    if isinstance(raw_word, dict) and _clean_text(raw_word.get("text"))
                )
                lines.append(
                    LyricLine(
                        text,
                        optional_int(raw_line.get("start_ms")),
                        optional_int(raw_line.get("end_ms")),
                        str(raw_line.get("agent") or ""),
                        str(raw_line.get("section") or ""),
                        words,
                    )
                )
            if not lines:
                raise ValueError("lyrics contain no lines")
            lyrics = cls(
                id=str(value.get("id") or ""),
                source_type=str(value.get("source_type") or "lyrics"),
                language=str(value.get("language") or ""),
                timing=str(value.get("timing") or "static"),
                lines=tuple(lines),
                spatial_offset_ms=int(value.get("spatial_offset_ms") or 0),
                applied_offset_ms=int(value.get("applied_offset_ms") or 0),
            )
        applied = int(value.get("applied_offset_ms") or 0)
        if applied and not lyrics.applied_offset_ms:
            lyrics = replace(
                lyrics,
                lines=tuple(line.shifted(applied) for line in lyrics.lines),
                applied_offset_ms=applied,
            )
        return lyrics


def parse_ttml(ttml: str, *, lyrics_id: str = "", source_type: str = "lyrics") -> Lyrics:
    """Parse provider-style TTML without discarding its original document."""
    text = str(ttml or "").strip()
    if not text:
        raise ValueError("lyrics TTML is empty")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"lyrics TTML is invalid: {exc}") from exc

    language = next(
        (str(value) for name, value in root.attrib.items() if _local(name) == "lang"),
        "",
    )
    timing = next(
        (str(value) for name, value in root.attrib.items() if _local(name) == "timing"),
        "",
    ).casefold()
    spatial_offset_ms = 0
    for node in root.iter():
        if _local(node.tag) != "audio":
            continue
        attrs = {_local(name): value for name, value in node.attrib.items()}
        if str(attrs.get("role") or "").casefold() != "spatial":
            continue
        spatial_offset_ms = _time_ms(attrs.get("lyricOffset")) or 0
        break

    body = next((node for node in root.iter() if _local(node.tag) == "body"), None)
    lines: list[LyricLine] = []
    if body is not None:
        for section in body.iter():
            if _local(section.tag) != "div":
                continue
            section_attrs = {_local(name): value for name, value in section.attrib.items()}
            section_name = str(section_attrs.get("songPart") or "")
            for node in section:
                if _local(node.tag) != "p":
                    continue
                attrs = {_local(name): value for name, value in node.attrib.items()}
                line_text = _clean_text("".join(node.itertext()))
                if not line_text:
                    continue
                words: list[LyricWord] = []
                for span in node.iter():
                    if span is node or _local(span.tag) != "span":
                        continue
                    span_attrs = {_local(name): value for name, value in span.attrib.items()}
                    word_text = _clean_text("".join(span.itertext()))
                    start = _time_ms(span_attrs.get("begin"))
                    end = _time_ms(span_attrs.get("end"))
                    if word_text and (start is not None or end is not None):
                        words.append(LyricWord(word_text, start, end))
                lines.append(
                    LyricLine(
                        line_text,
                        _time_ms(attrs.get("begin")),
                        _time_ms(attrs.get("end")),
                        str(attrs.get("agent") or ""),
                        section_name,
                        tuple(words),
                    )
                )

    if not lines:
        raise ValueError("lyrics TTML contains no lyric lines")
    inferred_timing = "word" if any(line.words for line in lines) else (
        "line" if any(line.start_ms is not None for line in lines) else "static"
    )
    return Lyrics(
        id=str(lyrics_id),
        source_type=str(source_type or "lyrics"),
        language=language,
        timing=timing or inferred_timing,
        ttml=text,
        lines=tuple(lines),
        spatial_offset_ms=spatial_offset_ms,
    )


def timestamp(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "--:--"
    total_seconds = max(0, int(milliseconds)) // 1_000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


__all__ = ["LyricLine", "LyricWord", "Lyrics", "parse_ttml", "timestamp"]
