from __future__ import annotations

from .models import StreamInfo


def ism_segment_index(start_time: int, sequence: int, is_live: bool) -> int:
    return int(start_time) if is_live else int(sequence)


def ism_segment_extra(stream_timescale: int, is_live: bool) -> dict[str, int | bool]:
    return {
        "ism_timescale": int(stream_timescale),
        "ism_index_is_timeline": bool(is_live),
    }


def stream_uses_ism_timeline_index(stream: StreamInfo) -> bool:
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    return bool(extra.get("ism_index_is_timeline"))


def stream_ism_timescale(stream: StreamInfo) -> int | None:
    extra = getattr(stream, "extra", {}) if isinstance(getattr(stream, "extra", None), dict) else {}
    value = extra.get("ism_timescale")
    try:
        timescale = int(value)
    except (TypeError, ValueError):
        return None
    return timescale if timescale > 0 else None
