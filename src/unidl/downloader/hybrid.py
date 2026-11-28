"""Dolby Vision RPU hybrid injection, L5/L6 metadata editing & HDR10+ -> DV P8.1 generation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from copy import copy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from .embedding import current_download_runtime, managed_run
from .utils import WINDOWS_INTERMEDIATE_PATH_LIMIT, bounded_path, safe_temp_root

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


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


def _find_tool(name: str, env_vars: tuple[str, ...] = ()) -> str | None:
    found = shutil.which(name) or shutil.which(f"{name}.exe")
    if found:
        return found
    candidates = [os.environ.get(var) for var in env_vars] + [
        str(Path.home() / name),
        str(Path.home() / f"{name}.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _tools(*, need_hdr10plus: bool = False) -> dict[str, str]:
    found: dict[str, str | None] = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "mkvmerge")}
    found["dovi_tool"] = _find_tool("dovi_tool", ("UNIDL_DOVI_TOOL", "DOVI_TOOL"))
    if need_hdr10plus:
        found["hdr10plus_tool"] = _find_tool("hdr10plus_tool", ("UNIDL_HDR10PLUS_TOOL", "HDR10PLUS_TOOL"))
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


def parse_cropdetect_output(stderr: str, *, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Compute ``(left, right, top, bottom)`` L5 active-area offsets from ffmpeg ``cropdetect`` output."""
    if width <= 0 or height <= 0 or not stderr:
        return None
    matches = [
        (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        for m in _CROP_RE.finditer(stderr)
    ]
    if not matches:
        return None
    crop_w, crop_h, crop_x, crop_y = Counter(matches).most_common(1)[0][0]
    left = max(0, crop_x)
    top = max(0, crop_y)
    right = max(0, width - crop_w - crop_x)
    bottom = max(0, height - crop_h - crop_y)
    # Guard against fully black sample frames producing nonsensical >50% crops.
    if left + right >= width // 2 or top + bottom >= height // 2:
        return None
    return (left, right, top, bottom)


def detect_l5_crop(
    path: Path,
    tools: dict[str, str],
    *,
    width: int,
    height: int,
    duration: float | None = None,
) -> tuple[int, int, int, int] | None:
    """Run bounded ffmpeg ``cropdetect`` to measure letterbox/pillarbox L5 active-area offsets."""
    if width <= 0 or height <= 0:
        return None
    seek = max(0.0, min(60.0, (duration or 0.0) * 0.2)) if duration and duration > 20 else 0.0
    argv = [
        tools["ffmpeg"],
        "-hide_banner",
        "-nostdin",
        "-ss",
        f"{seek:.2f}",
        "-t",
        "12",
        "-i",
        str(path),
        "-vf",
        "cropdetect=24:2:0",
        "-f",
        "null",
        "-",
    ]
    try:
        res = managed_run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return parse_cropdetect_output(res.stderr or "", width=width, height=height)


def probe_l6_luminance(stream_meta: dict[str, Any]) -> tuple[int, int] | None:
    """Extract ``(max_cll, max_fall)`` from ffprobe ``side_data_list`` if present."""
    for entry in stream_meta.get("side_data_list") or ():
        if not isinstance(entry, dict):
            continue
        max_cll = entry.get("max_content")
        max_fall = entry.get("max_average")
        if max_cll is not None and max_fall is not None:
            try:
                cll, fall = int(max_cll), int(max_fall)
                if cll > 0 or fall > 0:
                    return (max(0, cll), max(0, fall))
            except (TypeError, ValueError):
                continue
    return None


def _parse_rational(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        try:
            return float(text)
        except ValueError:
            return None


def probe_mastering_luminance(stream_meta: dict[str, Any]) -> tuple[int, int] | None:
    """Extract ``(max_nits, min_0001_nits)`` mastering display luminance from ffprobe side data."""
    for entry in stream_meta.get("side_data_list") or ():
        if not isinstance(entry, dict):
            continue
        max_lum = _parse_rational(entry.get("max_luminance"))
        min_lum = _parse_rational(entry.get("min_luminance"))
        if max_lum is not None and max_lum > 0:
            max_nits = max(1, int(round(max_lum)))
            min_units = max(1, int(round((min_lum or 0.0001) * 10000)))
            return (max_nits, min_units)
    return None


def _level6_payload(
    l6: tuple[int, int],
    mastering: tuple[int, int] | None = None,
) -> dict[str, int]:
    """Return a ``dovi_tool`` 2.x-compatible ``level6`` JSON object."""
    max_cll = max(0, int(l6[0]))
    max_fall = max(0, int(l6[1]))
    if mastering is not None:
        max_mdl = max(1, int(mastering[0]))
        min_mdl = max(1, int(mastering[1]))
    else:
        max_mdl = 4000 if max_cll > 1000 else 1000
        min_mdl = 1
    return {
        "max_display_mastering_luminance": max_mdl,
        "min_display_mastering_luminance": min_mdl,
        "max_content_light_level": max_cll,
        "max_frame_average_light_level": max_fall,
    }


def build_dovi_edit_config(
    *,
    crop: tuple[int, int, int, int] | None = None,
    l6: tuple[int, int] | None = None,
    mastering: tuple[int, int] | None = None,
    mode: int = 2,
) -> dict[str, Any]:
    """Build a ``dovi_tool`` ``--edit-config`` JSON payload with L5 crop and L6 luminance."""
    config: dict[str, Any] = {"mode": int(mode)}
    if crop is not None:
        left, right, top, bottom = (max(0, int(v)) for v in crop)
        config["active_area"] = {
            "crop": False,
            "presets": [
                {
                    "id": 0,
                    "left": left,
                    "right": right,
                    "top": top,
                    "bottom": bottom,
                }
            ],
            "edits": {"all": 0},
        }
    if l6 is not None:
        config["level6"] = _level6_payload(l6, mastering=mastering)
    return config


def build_dovi_generate_config(
    *,
    length: int,
    crop: tuple[int, int, int, int] | None = None,
    l6: tuple[int, int] = (1000, 400),
    mastering: tuple[int, int] | None = None,
    cm_version: str = "V29",
) -> dict[str, Any]:
    """Build a ``dovi_tool generate -j`` JSON payload compatible with dovi_tool 2.x."""
    payload: dict[str, Any] = {
        "cm_version": str(cm_version or "V29"),
        "length": max(1, int(length)),
        "level6": _level6_payload(l6, mastering=mastering),
    }
    if crop is not None:
        payload["level5"] = {
            "active_area_left_offset": max(0, int(crop[0])),
            "active_area_right_offset": max(0, int(crop[1])),
            "active_area_top_offset": max(0, int(crop[2])),
            "active_area_bottom_offset": max(0, int(crop[3])),
        }
    return payload


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


def _hybrid_one(
    hdr: Any,
    dv: Any,
    work: Path,
    tools: dict[str, str],
    *,
    auto_l5_l6: bool = True,
) -> Any:
    base_meta = _probe(Path(hdr.path), tools)
    donor_meta = _probe(Path(dv.path), tools)
    fps = _validate(base_meta, donor_meta)
    hdr_hevc, dv_hevc, rpu, injected = (work / name for name in ("hdr10.hevc", "dv.hevc", "rpu.bin", "hybrid.hevc"))
    output = bounded_path(
        Path(hdr.path).with_name(Path(hdr.path).name + ".hybrid.mkv"),
        max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT,
    )
    try:
        for source, target in ((hdr.path, hdr_hevc), (dv.path, dv_hevc)):
            _run([tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(target)], "HEVC extraction")
            _ensure(target)
        extract_cmd = [tools["dovi_tool"], "-m", "3"]
        if auto_l5_l6:
            width = int(base_meta.get("width") or 0)
            height = int(base_meta.get("height") or 0)
            try:
                dur = float(base_meta["duration"]) if base_meta.get("duration") else None
            except (TypeError, ValueError):
                dur = None
            crop = detect_l5_crop(Path(hdr.path), tools, width=width, height=height, duration=dur)
            l6 = probe_l6_luminance(base_meta)
            mastering = probe_mastering_luminance(base_meta)
            if crop is not None or l6 is not None:
                edit_cfg_path = work / "dovi_edit.json"
                edit_cfg_path.write_text(
                    json.dumps(build_dovi_edit_config(crop=crop, l6=l6, mastering=mastering, mode=3), indent=2),
                    encoding="utf-8",
                )
                extract_cmd = [tools["dovi_tool"], "--edit-config", str(edit_cfg_path)]
        extract_cmd.extend(["extract-rpu", str(dv_hevc), "-o", str(rpu)])
        _run(extract_cmd, "DV RPU extraction")
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


def generate_dv_from_hdr10p(
    hdr_track: Any,
    work: Path,
    tools: dict[str, str],
) -> Any:
    """Generate Dolby Vision Profile 8.1 RPU from an HDR10+ HEVC track and inject it."""
    base_meta = _probe(Path(hdr_track.path), tools)
    if base_meta.get("codec_name") != "hevc":
        raise RuntimeError("HDR10+ to Dolby Vision generation requires an HEVC source video")
    try:
        fps = Fraction(base_meta["r_frame_rate"])
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError("Could not determine video frame rate for DV generation") from exc

    hdr_hevc = work / "hdr10p.hevc"
    h10p_json = work / "hdr10plus.json"
    gen_cfg = work / "dovi_gen.json"
    rpu = work / "generated_rpu.bin"
    injected = work / "dv_p81.hevc"
    output = bounded_path(
        Path(hdr_track.path).with_name(Path(hdr_track.path).name + ".dv81.mkv"),
        max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT,
    )
    try:
        _run(
            [
                tools["ffmpeg"],
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(hdr_track.path),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-bsf:v",
                "hevc_mp4toannexb",
                "-f",
                "hevc",
                str(hdr_hevc),
            ],
            "HEVC extraction",
        )
        _ensure(hdr_hevc)
        _run(
            [tools["hdr10plus_tool"], "extract", str(hdr_hevc), "-o", str(h10p_json)],
            "HDR10+ metadata extraction",
        )
        _ensure(h10p_json)

        width = int(base_meta.get("width") or 0)
        height = int(base_meta.get("height") or 0)
        try:
            dur = float(base_meta["duration"]) if base_meta.get("duration") else None
        except (TypeError, ValueError):
            dur = None
        crop = detect_l5_crop(Path(hdr_track.path), tools, width=width, height=height, duration=dur)
        l6 = probe_l6_luminance(base_meta) or (1000, 400)
        mastering = probe_mastering_luminance(base_meta)
        frame_count = int(base_meta.get("nb_read_packets") or 0)
        if frame_count <= 0:
            try:
                h10p_doc = json.loads(h10p_json.read_text(encoding="utf-8"))
                frame_count = len(h10p_doc.get("SceneInfo") or ())
            except (OSError, ValueError):
                frame_count = 1
        gen_payload = build_dovi_generate_config(
            length=max(1, frame_count),
            crop=crop,
            l6=l6,
            mastering=mastering,
        )
        gen_cfg.write_text(json.dumps(gen_payload, indent=2), encoding="utf-8")

        _run(
            [
                tools["dovi_tool"],
                "generate",
                "-j",
                str(gen_cfg),
                "--hdr10plus-json",
                str(h10p_json),
                "-o",
                str(rpu),
            ],
            "Dolby Vision Profile 8.1 RPU generation",
        )
        _ensure(rpu)
        _run(
            [
                tools["dovi_tool"],
                "inject-rpu",
                "-i",
                str(hdr_hevc),
                "--rpu-in",
                str(rpu),
                "-o",
                str(injected),
            ],
            "Dolby Vision RPU injection",
        )
        _ensure(injected)
        _run(
            [
                tools["mkvmerge"],
                "-o",
                str(output),
                "--default-duration",
                f"0:{float(fps):.6f}fps",
                str(injected),
            ],
            "Dolby Vision Profile 8.1 remux",
        )
        _ensure(output)
        result = copy(hdr_track)
        result.path = output
        result.cleanup_paths = list(dict.fromkeys([*getattr(hdr_track, "cleanup_paths", []), hdr_track.path, output]))
        result.stream = replace(
            hdr_track.stream,
            video_range="DV+HDR10+",
            extension="mkv",
            encrypted=False,
            encryption_scheme=None,
        )
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
    temp_root = safe_temp_root(
        Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir()),
        max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT,
    )
    temp_root.mkdir(parents=True, exist_ok=True)
    replacements: dict[int, Any] = {}
    for base, dv in pairs:
        with tempfile.TemporaryDirectory(prefix="unidl-hybrid-", dir=str(temp_root)) as work:
            replacements[id(base)] = _hybrid_one(by_stream[id(base)], by_stream[id(dv)], Path(work), tools)
    return [replacements.get(id(item.stream), item) for item in tracks if id(item.stream) not in donors]


def process_generate_dv_tracks(
    tracks: list[Any],
    *,
    enabled: bool,
    temp_dir: str | Path | None = None,
) -> list[Any]:
    """Convert eligible HDR10+ video tracks (without DV) into Dolby Vision Profile 8.1."""
    if not enabled:
        return tracks
    candidates = [
        item
        for item in tracks
        if getattr(item.stream, "media_type", "") == "video"
        and not getattr(item.stream, "is_live", False)
        and _hevc(item.stream)
        and _range(item.stream) in {"HDR10+", "HDR10PLUS", "HDR10P"}
    ]
    if not candidates:
        return tracks
    tools = _tools(need_hdr10plus=True)
    temp_root = safe_temp_root(
        Path(temp_dir).expanduser() if temp_dir else Path(tempfile.gettempdir()),
        max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT,
    )
    temp_root.mkdir(parents=True, exist_ok=True)
    replacements: dict[int, Any] = {}
    for item in candidates:
        with tempfile.TemporaryDirectory(prefix="unidl-dvgen-", dir=str(temp_root)) as work:
            replacements[id(item.stream)] = generate_dv_from_hdr10p(item, Path(work), tools)
    return [replacements.get(id(item.stream), item) for item in tracks]


__all__ = [
    "build_dovi_edit_config",
    "detect_l5_crop",
    "generate_dv_from_hdr10p",
    "parse_cropdetect_output",
    "probe_l6_luminance",
    "process_generate_dv_tracks",
    "process_hybrid_tracks",
    "select_ingredients",
]
