"""Optional Dolby Vision RPU injection into a matching HDR10 base stream."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from copy import copy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from .embedding import current_download_runtime, managed_run


def _range(stream: Any) -> str:
    return str(getattr(stream, "video_range", "") or "").upper().replace(" ", "")


def _hevc(stream: Any) -> bool:
    return str(getattr(stream, "codecs", "") or "").lower().startswith(("hev", "hvc", "dvhe", "dvh1", "h.265", "h265"))


def _compatible(hdr: Any, dv: Any) -> bool:
    if any(getattr(s, "media_type", "") != "video" or getattr(s, "is_live", False) or not _hevc(s) for s in (hdr, dv)):
        return False
    if _range(hdr) not in {"HDR", "HDR10", "HDR10+"} or _range(dv) not in {"DV", "DOVI", "DOLBYVISION"}:
        return False
    if not getattr(hdr, "resolution", None) or not getattr(dv, "resolution", None):
        return False
    if hdr.frame_rate and dv.frame_rate and abs(hdr.frame_rate - dv.frame_rate) > 0.01:
        return False
    if hdr.total_duration and dv.total_duration and abs(hdr.total_duration - dv.total_duration) > 0.1:
        return False
    return True


def _pairs(streams: list[Any]) -> list[tuple[Any, Any]]:
    pairs = []
    used_donors: set[int] = set()
    for base in streams:
        candidates = [dv for dv in streams if id(dv) not in used_donors and _compatible(base, dv)]
        if candidates:
            donor = min(candidates, key=lambda s: (_resolution_height(getattr(s, "resolution", "")), getattr(s, "bandwidth", 0) or 0))
            used_donors.add(id(donor))
            pairs.append((base, donor))
    return pairs


def _resolution_height(value: object) -> int:
    text = str(value or "")
    try:
        return int(text.rsplit("x", 1)[1])
    except (IndexError, ValueError):
        return 0


def select_ingredients(streams: list[Any], selected: list[Any]) -> list[Any]:
    """Add only the matching DV/HDR ingredient for each selected video."""
    chosen = list(selected)
    for stream in selected:
        candidates = [s for s in streams if _compatible(stream, s) or _compatible(s, stream)]
        if candidates and not any(id(s) in {id(x) for x in chosen} for s in candidates):
            chosen.append(max(candidates, key=lambda s: getattr(s, "bandwidth", 0) or 0))
    selected_ids = {id(s) for s in chosen}
    return [s for s in streams if id(s) in selected_ids]


def _tools() -> dict[str, str]:
    found = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "mkvmerge")}
    dovi = shutil.which("dovi_tool") or shutil.which("dovi_tool.exe")
    for candidate in (os.environ.get("UNIDL_DOVI_TOOL"), os.environ.get("DOVI_TOOL"), str(Path.home() / "dovi_tool"), str(Path.home() / "dovi_tool.exe")):
        if not dovi and candidate and Path(candidate).is_file():
            dovi = candidate
    found["dovi_tool"] = dovi
    missing = [name for name, path in found.items() if not path]
    if missing:
        count = len(missing)
        noun = "tool" if count == 1 else "tools"
        raise RuntimeError(f"Dolby Vision hybrid: {count} required {noun} missing: {', '.join(missing)}")
    return found  # type: ignore[return-value]


def _run(argv: list[str], label: str) -> str:
    runtime = current_download_runtime()
    if runtime:
        runtime.checkpoint()
    result = managed_run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-800:]
        raise RuntimeError(f"{label} failed: {detail or result.returncode}")
    if runtime:
        runtime.checkpoint()
    return result.stdout


def _probe(path: Path, tools: dict[str, str]) -> dict[str, Any]:
    data = _run([tools["ffprobe"], "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_streams", "-of", "json", str(path)], "hybrid validation")
    try:
        return json.loads(data)["streams"][0]
    except (ValueError, KeyError, IndexError) as exc:
        raise RuntimeError("Hybrid could not inspect a source video") from exc


def _validate(base: dict[str, Any], donor: dict[str, Any]) -> Fraction:
    try:
        fps, donor_fps = Fraction(base["r_frame_rate"]), Fraction(donor["r_frame_rate"])
        if fps != donor_fps or int(base["nb_read_packets"]) != int(donor["nb_read_packets"]):
            raise ValueError("frame rate or frame count differs")
        if base.get("duration") and donor.get("duration") and abs(float(base["duration"]) - float(donor["duration"])) > float(1 / fps):
            raise ValueError("durations differ")
        if base.get("codec_name") != "hevc" or donor.get("codec_name") != "hevc":
            raise ValueError("both inputs must be HEVC")
        return fps
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as exc:
        raise RuntimeError(f"Unsafe DV/HDR10 hybrid pair: {exc}") from exc


def _ensure(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Hybrid tool did not produce {path.name}")


def _hybrid_one(hdr: Any, dv: Any, work: Path, tools: dict[str, str]) -> Any:
    fps = _validate(_probe(Path(hdr.path), tools), _probe(Path(dv.path), tools))
    hdr_hevc, dv_hevc, rpu, injected = (work / name for name in ("hdr10.hevc", "dv.hevc", "rpu.bin", "hybrid.hevc"))
    output = Path(hdr.path).with_name(Path(hdr.path).name + ".hybrid.mkv")
    try:
        for source, target in ((hdr.path, hdr_hevc), (dv.path, dv_hevc)):
            _run([tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(target)], "HEVC extraction")
            _ensure(target)
        _run([tools["dovi_tool"], "-m", "3", "extract-rpu", str(dv_hevc), "-o", str(rpu)], "DV RPU extraction")
        _ensure(rpu)
        _run([tools["dovi_tool"], "inject-rpu", "-i", str(hdr_hevc), "--rpu-in", str(rpu), "-o", str(injected)], "DV RPU injection")
        _ensure(injected)
        _run([tools["mkvmerge"], "-o", str(output), "--default-duration", f"0:{float(fps):.6f}fps", str(injected)], "hybrid video remux")
        _ensure(output)
        result = copy(hdr)
        result.path = output
        result.cleanup_paths = list(dict.fromkeys([*getattr(hdr, "cleanup_paths", []), *getattr(dv, "cleanup_paths", []), hdr.path, dv.path, output]))
        result.stream = replace(hdr.stream, video_range="DV+HDR10+" if _range(hdr.stream) == "HDR10+" else "DV+HDR10", extension="mkv", encrypted=False, encryption_scheme=None)
        return result
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def process_hybrid_tracks(tracks: list[Any], *, enabled: bool, temp_dir: str | Path | None = None) -> list[Any]:
    if not enabled:
        return tracks
    videos = [item.stream for item in tracks if getattr(item.stream, "media_type", "") == "video"]
    pairs = _pairs(videos)
    if not pairs:
        return tracks
    tools = _tools()
    by_stream = {id(item.stream): item for item in tracks}
    donors = {id(dv) for _, dv in pairs}
    if temp_dir:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    replacements: dict[int, Any] = {}
    for base, dv in pairs:
        with tempfile.TemporaryDirectory(prefix="unidl-hybrid-", dir=str(temp_dir) if temp_dir else None) as work:
            replacements[id(base)] = _hybrid_one(by_stream[id(base)], by_stream[id(dv)], Path(work), tools)
    return [replacements.get(id(item.stream), item) for item in tracks if id(item.stream) not in donors]


__all__ = ["process_hybrid_tracks", "select_ingredients"]
