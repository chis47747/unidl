"""Durable VOD checkpoints, separate from disposable per-run work files.

Only completed pipeline stages are published. Readers check out independent
directory entries before releasing the record lock, so replacing a generation
never removes another invocation's input. Core transforms treat these inputs
as read-only and write/replace a different output; hard links avoid copying
multi-gigabyte media on the usual same-filesystem path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from ..core.secureio import atomic_write_text, locked_path, private_directory
from .downloader import DownloadResult
from .utils import WINDOWS_INTERMEDIATE_PATH_LIMIT, bounded_child_path, windows_long_path

PHASES = ("downloaded", "decrypted", "repacked", "audio", "transcoded", "ready")


@contextlib.contextmanager
def task_cache_lock(root: Path, checkpoint):
    """Serialize a task's cache lifecycle, including mux/commit and cleanup.

    The lock lives beside the task, not inside the directory being cleaned.
    Nonblocking OS locks keep cancellation responsive while another invocation
    is using the same cache. Windows locks one byte; POSIX uses flock.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.with_name("." + root.name + ".resume.lock")
    with lock.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()

            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        while True:
            checkpoint()
            try:
                acquire()
                break
            except (BlockingIOError, PermissionError):
                time.sleep(0.1)
            except OSError as exc:
                if os.name != "nt" or exc.errno not in {13, 36}:
                    raise
                time.sleep(0.1)
        try:
            yield
        finally:
            release()


def _signature(path: Path) -> dict:
    stat = path.stat()
    if not path.is_file() or path.is_symlink():
        raise OSError("checkpoint is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        digest.update(source.read(65536))
        if stat.st_size > 65536:
            source.seek(max(65536, stat.st_size - 65536))
            digest.update(source.read(65536))
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sample": digest.hexdigest()}


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(windows_long_path(source), windows_long_path(target))
    except OSError:
        # Cross-device/unsupported links use a bounded-memory copy, never read_bytes.
        shutil.copy2(windows_long_path(source), windows_long_path(target))


class TrackCheckpoint:
    def __init__(self, root: Path, identity: dict, work: Path, *, enabled: bool = True, keep: bool = False):
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]
        self.root = bounded_child_path(root, key, max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT - 55)
        self.record = self.root / "state.json"
        self.identity = identity
        self.work = work
        self.enabled = enabled
        self.keep = keep
        self.phase = ""
        self.path: Path | None = None
        self.result: DownloadResult | None = None
        self._source_name = ""
        self._snapshot: Path | None = None
        self._last_signature: dict | None = None

    def reached(self, phase: str) -> bool:
        return bool(self.phase) and PHASES.index(self.phase) >= PHASES.index(phase)

    def release_download_parts(self) -> bool:
        """Make an assembled download independent of its redundant fragments.

        Publish the smaller dependency set before the caller removes parts. Keep
        temp_dir as an optional cleanup hint, so an interrupted removal can be
        retried without making checkpoint restoration depend on those files.
        """
        if not self.enabled:
            return True
        if self.phase != "downloaded" or self.path is None or self._snapshot is None:
            return False
        with locked_path(self.record):
            try:
                data = json.loads(self.record.read_text("utf-8"))
                if (
                    data["identity"] != self.identity or data["phase"] != "downloaded"
                    or data["generation"] != self._snapshot.parent.name
                    or data["current_name"] != self._snapshot.name
                ):
                    return False
                signature = next(item["signature"] for item in data["files"] if item["name"] == data["current_name"])
                if _signature(self._snapshot) != signature or _signature(self.path) != signature:
                    return False
            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                return False
            if data.get("parts") or data.get("sections"):
                data["parts"] = []
                data["sections"] = []
                atomic_write_text(self.record, json.dumps(data, ensure_ascii=False, sort_keys=True))
        return True

    def restore(self, stream) -> None:
        if not self.enabled:
            return
        with locked_path(self.record):
            try:
                data = json.loads(self.record.read_text("utf-8"))
                if data["version"] != 1 or data["identity"] != self.identity or data["phase"] not in PHASES:
                    return
                generation = str(data["generation"])
                if Path(generation).name != generation or not generation.startswith("g-"):
                    return
                source = self.root / generation
                if source.is_symlink() or not source.is_dir():
                    return
                files = data["files"]
                if not isinstance(files, list) or not files:
                    return
                source_name = data["source_name"]
                if not isinstance(source_name, str) or Path(source_name).name != source_name or source_name in {".", ".."}:
                    return
                for item in files:
                    name = item["name"]
                    output_name = item.get("output_name", name)
                    if any(not isinstance(value, str) or Path(value).name != value or value in {".", ".."} for value in (name, output_name)):
                        return
                    if _signature(source / name) != item["signature"]:
                        return
                for field in ("parts", "sections"):
                    for item in data.get(field, []):
                        path = Path(item["path"])
                        # Auxiliary fragments must remain inside this task cache.
                        path.resolve().relative_to(self.root.parent.parent.resolve())
                        if path.stat().st_size != item["size"] or not path.is_file() or path.is_symlink():
                            return
                temp_dir = Path(data["temp_dir"]) if data.get("temp_dir") else None
                if temp_dir is not None:
                    temp_dir.resolve().relative_to(self.root.parent.parent.resolve())
                    if temp_dir.is_symlink():
                        return
                if data["current_name"] is not None and data["current_name"] not in {item["name"] for item in files}:
                    return
                # A checkout has unique names per track in the run's workspace.
                restored = {}
                for item in files:
                    target = bounded_child_path(self.work, item.get("output_name", item["name"]), max_length=WINDOWS_INTERMEDIATE_PATH_LIMIT)
                    if target.exists():
                        return
                    _link_or_copy(source / item["name"], target)
                    restored[item["name"]] = target
                self.phase = data["phase"]
                self._source_name = source_name
                self.path = restored.get(data["current_name"])
                if self.path is not None:
                    self._snapshot = source / data["current_name"]
                    self._last_signature = _signature(self.path)
                self.result = DownloadResult(
                    stream=stream,
                    path=self.work / self._source_name,
                    temp_dir=temp_dir,
                    parts=[Path(item["path"]) for item in data.get("parts", [])] or None,
                    sections=[Path(item["path"]) for item in data.get("sections", [])] or None,
                )
                if self.path is not None:
                    self.result.path = self.path if self.phase == "downloaded" else self.result.path
                # Restore only output-shape fields, never authentication/keys.
                for key, value in data.get("shape", {}).items():
                    if key in {"extension", "codecs", "channels"} and isinstance(value, (str, type(None))):
                        setattr(stream, key, value)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.phase = ""
                self.path = None
                self.result = None

    def save(self, phase: str, path: Path, result: DownloadResult) -> None:
        if not self.enabled:
            return
        # Never publish an absent/zero-length audio or video as a finished track.
        paths = [Path(path)] if Path(path).is_file() else []
        if not paths and (phase != "downloaded" or not result.parts):
            raise OSError(f"Missing {phase} track output")
        if paths and paths[0].stat().st_size == 0 and result.stream.media_type != "subtitle":
            raise OSError(f"Empty {phase} track output")
        with locked_path(self.record):
            generation = "g-" + uuid.uuid4().hex[:12]
            directory = private_directory(self.root / generation)
            try:
                files = []
                for item in paths:
                    target = directory / ("media" + item.suffix)
                    signature = _signature(item)
                    # No-op stages must not repeatedly copy a huge file when
                    # work and cache are on different filesystems.
                    unchanged = (
                        item == self.path and signature == self._last_signature
                        and self._snapshot is not None and self._snapshot.is_file()
                    )
                    _link_or_copy(self._snapshot if unchanged else item, target)
                    files.append({"name": target.name, "output_name": item.name, "signature": _signature(target)})
                auxiliary = {}
                for field in ("parts", "sections"):
                    values = getattr(result, field) or []
                    auxiliary[field] = [
                        {"path": str(item.resolve()), "size": item.stat().st_size}
                        for item in values
                    ] if phase == "downloaded" else []
                # When assembly is intentionally skipped, the verified parts are
                # themselves the download checkpoint; no fabricated media file.
                if not files and auxiliary["parts"]:
                    first = Path(auxiliary["parts"][0]["path"])
                    target = directory / "parts-anchor"
                    _link_or_copy(first, target)
                    files = [{"name": target.name, "signature": _signature(target)}]
                self._source_name = self._source_name or result.path.name
                payload = {
                    "version": 1, "identity": self.identity, "phase": phase,
                    "generation": generation, "files": files,
                    "current_name": files[0]["name"] if paths else None,
                    "source_name": self._source_name,
                    "temp_dir": str(result.temp_dir) if phase == "downloaded" and result.temp_dir else None,
                    "shape": {key: getattr(result.stream, key, None) for key in ("extension", "codecs", "channels")},
                    **auxiliary,
                }
                atomic_write_text(self.record, json.dumps(payload, ensure_ascii=False, sort_keys=True))
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self.phase = phase
            self.path = paths[0] if paths else None
            self.result = result
            self._snapshot = directory / files[0]["name"] if paths else None
            self._last_signature = files[0]["signature"] if paths else None
            if not self.keep:
                for previous in self.root.glob("g-*"):
                    if previous != directory and previous.is_dir() and not previous.is_symlink():
                        shutil.rmtree(previous, ignore_errors=True)
