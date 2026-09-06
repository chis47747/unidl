"""External helper integration.

A sizeable minority of services cannot work with Python alone. Measured in the
existing script corpus:

* ``xfinity``  - ``java``/``javac`` + a unidbg runner (``SecClientRunner.java``,
  ``libsecclient.so``) driven through a JSON file bridge
* ``ytv`` / ``youtube`` - ``node`` to evaluate player JS, plus a Python helper
  module (``ytv_sabr``) loaded from a path at runtime
* ``appletv`` - a local Mescal/FairPlay signing module loaded by path
* ``skygo`` - ``adb`` to talk to a real device
* ``10play`` - ``subby`` for subtitle conversion, before subtitles were handed
  to UniDL
* ``dazn`` - ``curl``

The old scripts each hardcoded absolute paths and crashed mid-flow when
something was missing. Here a service *declares* what it needs, resolution is
centralised and configurable, and the UI can report a missing helper before the
user starts browsing rather than after they picked an episode.

Three kinds are supported:

``BINARY``  an executable found on PATH or at a configured path
``MODULE``  a Python module loaded from a file path at runtime
``ASSET``   a data file or directory (keystore, .so, .java source, cert)
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any


class HelperKind(str, Enum):
    BINARY = "binary"
    MODULE = "module"
    ASSET = "asset"


@dataclass(frozen=True)
class Helper:
    """A declaration of something external a service needs."""

    key: str
    label: str
    kind: HelperKind = HelperKind.BINARY
    #: executable names to try on PATH, in order (BINARY only)
    candidates: tuple[str, ...] = ()
    #: arguments that make the tool print its version
    version_args: tuple[str, ...] = ("--version",)
    required: bool = True
    #: Paths to try before giving up, for tools that install somewhere PATH does
    #: not see. Homebrew's openjdk is the case that forced this: it is not linked
    #: into PATH, and macOS ships a /usr/bin/java stub that exists, runs, and only
    #: says "no Java here" - so PATH lookup succeeded and every later step was
    #: skipped.
    extra_paths: tuple[str, ...] = ()
    #: Require the version probe to exit cleanly before accepting a binary. That
    #: is the difference between "there is a file with this name" and "this tool
    #: works", and the stub above is exactly that difference.
    must_run: bool = False
    #: shown when the helper cannot be found
    install_hint: str = ""
    #: what stops working without it, when not required
    degrades_to: str = ""

    def path_names(self) -> tuple[str, ...]:
        return self.candidates or (self.key,)


@dataclass
class ResolvedHelper:
    helper: Helper
    path: Path | None = None
    version: str = ""
    source: str = ""  # config | path | helper-dir
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None

    @property
    def key(self) -> str:
        return self.helper.key

    def describe(self) -> str:
        if not self.ok:
            return f"missing ({self.reason})" if self.reason else "missing"
        parts = [self.version or "found"]
        if self.source:
            parts.append(f"via {self.source}")
        return " · ".join(parts)


@dataclass
class HelperReport:
    """Aggregate state for one service."""

    resolved: dict[str, ResolvedHelper] = field(default_factory=dict)

    @property
    def missing_required(self) -> list[ResolvedHelper]:
        return [r for r in self.resolved.values() if r.helper.required and not r.ok]

    @property
    def missing_optional(self) -> list[ResolvedHelper]:
        return [r for r in self.resolved.values() if not r.helper.required and not r.ok]

    @property
    def ready(self) -> bool:
        return not self.missing_required

    def summary(self) -> str:
        if not self.resolved:
            return ""
        if self.ready:
            degraded = len(self.missing_optional)
            return "helpers ok" if not degraded else f"helpers ok ({degraded} optional missing)"
        names = ", ".join(r.helper.label for r in self.missing_required)
        return f"needs {names}"

    def blocking_message(self) -> str:
        lines = []
        for item in self.missing_required:
            line = f"{item.helper.label} is required but was not found"
            if item.helper.install_hint:
                line += f"\n    {item.helper.install_hint}"
            lines.append(line)
        return "\n".join(lines)


class HelperError(RuntimeError):
    pass


class HelperResolver:
    """Finds helpers. Resolution order, first hit wins:

    1. ``helpers.<service>.<key>`` in unidl.yaml (most specific)
    2. ``helpers.<key>`` in unidl.yaml (shared across services)
    3. ``PATH`` lookup of each candidate name (BINARY only)
    4. ``<paths.helpers>/<service>/<name>`` then ``<paths.helpers>/<name>``
    5. the locations the service itself declares in ``extra_paths``

    Step 5 is the bridge, and it is deliberately the last one: a service that needs
    something outside the package *says where it is*, in its own declaration, where
    it can be read. iq's signer is a 31MB tree of jars and a shared library and
    Optimum's Nagra harness is a 150MB build tree - neither can sensibly be copied
    into the helper directory, and one of them cannot be moved at all because its
    entry point imports its siblings. So they are named.

    Nothing is searched for. There is no directory this consults that a service or
    the configuration has not pointed it at, which is what makes "what does unidl
    reach outside itself for" a question with a written answer.
    """

    def __init__(self, config: Any):
        self.config = config
        #: What has already been looked up, per service and key. Resolution shells
        #: out - a PATH lookup, then ``--version``, then for a helper that must work
        #: a second run of it - so asking twice costs two subprocesses to learn the
        #: same thing. One resolver answering the same question twice is the case
        #: this covers; a new resolver is how a caller says "look again".
        self._resolved: dict[tuple[str, str], ResolvedHelper] = {}

    def forget(self) -> None:
        """Look everything up again next time. For "check again"."""
        self._resolved.clear()

    # ------------------------------------------------------------------ lookup
    def _configured(
        self,
        service_id: str,
        key: str,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> Path | None:
        section = (getattr(self.config, "raw", {}) or {}).get("helpers") or {}
        for name in (service_id, *legacy_ids):
            per_service = section.get(name)
            if isinstance(per_service, dict) and per_service.get(key):
                return _expand(per_service[key])
        value = section.get(key)
        if isinstance(value, (str, os.PathLike)):
            return _expand(value)
        return None

    def _helper_dir_candidates(
        self,
        service_id: str,
        names: Sequence[str],
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> list[Path]:
        root = getattr(self.config.paths, "helpers", None)
        if root is None:
            return []
        out: list[Path] = []
        for service in (service_id, *legacy_ids):
            for name in names:
                out.append(Path(root) / service / name)
        for name in names:
            out.append(Path(root) / name)
        return out

    def resolve(
        self,
        service_id: str,
        helper: Helper,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> ResolvedHelper:
        remembered = self._resolved.get((service_id, helper.key))
        if remembered is not None:
            return remembered
        found = self._resolve(service_id, helper, legacy_ids)
        self._resolved[(service_id, helper.key)] = found
        return found

    def _resolve(
        self,
        service_id: str,
        helper: Helper,
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> ResolvedHelper:
        configured = self._configured(service_id, helper.key, legacy_ids)
        if configured is not None:
            if configured.exists():
                if helper.kind is HelperKind.BINARY and helper.must_run and not self._runs(
                    helper, configured
                ):
                    return ResolvedHelper(
                        helper,
                        None,
                        reason=f"configured helper did not pass {' '.join(helper.version_args)}",
                    )
                return ResolvedHelper(helper, configured, self._version(helper, configured), "config")
            return ResolvedHelper(helper, None, reason=f"configured path does not exist: {configured}")

        if helper.kind is HelperKind.BINARY:
            for name in helper.path_names():
                found = shutil.which(name)
                if not found:
                    continue
                path = Path(found)
                version = self._version(helper, path)
                if helper.must_run and not self._runs(helper, path):
                    continue
                return ResolvedHelper(helper, path, version, "PATH")
        for candidate in self._helper_dir_candidates(
            service_id, helper.path_names(), legacy_ids
        ):
            if candidate.exists():
                if helper.kind is HelperKind.BINARY and helper.must_run and not self._runs(
                    helper, candidate
                ):
                    continue
                return ResolvedHelper(helper, candidate, self._version(helper, candidate), "helper dir")

        # Modules and assets may declare exceptional locations too, and this is
        # the last place anything is looked for. Ordinary machine-local runtimes
        # belong under the configured helper directory; ``extra_paths`` remains
        # only for helpers whose surrounding tree genuinely cannot be relocated.
        for candidate in _expand_all(helper.extra_paths):
            if not candidate.exists():
                continue
            if helper.kind is HelperKind.BINARY and helper.must_run and not self._runs(helper, candidate):
                continue
            return ResolvedHelper(
                helper, candidate, self._version(helper, candidate), "known location"
            )

        names = "/".join(helper.path_names())
        return ResolvedHelper(helper, None, reason=f"not found ({names})")

    def report(
        self,
        service_id: str,
        helpers: Sequence[Helper],
        legacy_ids: tuple[str, ...] | list[str] = (),
    ) -> HelperReport:
        return HelperReport(
            {helper.key: self.resolve(service_id, helper, legacy_ids) for helper in helpers}
        )

    def _runs(self, helper: Helper, path: Path) -> bool:
        """Does this thing actually work, rather than merely exist?"""
        if not helper.version_args:
            return True
        try:
            result = subprocess.run(  # noqa: S603 - path came from config or PATH
                [str(path), *helper.version_args], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _version(self, helper: Helper, path: Path) -> str:
        if helper.kind is not HelperKind.BINARY or not helper.version_args:
            return ""
        try:
            result = subprocess.run(  # noqa: S603 - path came from config or PATH
                [str(path), *helper.version_args],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        text = (result.stdout or result.stderr or "").strip().splitlines()
        return text[0][:60] if text else ""


def _expand(value: Any) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _expand_all(entries: Sequence[str]) -> list[Path]:
    """Expand each entry, resolving any glob in it.

    JDKs land in versioned folders under /Library/Java/JavaVirtualMachines, so the
    only way to name that location is with a wildcard. Newest first, since the
    folder names sort that way and a newer JDK is the better guess.
    """
    found: list[Path] = []
    for entry in entries:
        expanded = os.path.expandvars(str(entry))
        if "*" not in expanded and "?" not in expanded:
            found.append(Path(expanded).expanduser())
            continue
        path = Path(expanded).expanduser()
        try:
            anchor = Path(path.anchor) if path.is_absolute() else Path()
            pattern = str(path.relative_to(path.anchor)) if path.is_absolute() else str(path)
            found.extend(sorted(anchor.glob(pattern), reverse=True))
        except (OSError, ValueError):
            continue
    return found


# --------------------------------------------------------------------- running


@dataclass
class HelperResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class HelperRunner:
    """Runs helper processes with logging, timeouts and a JSON file bridge.

    The file bridge exists because that is how the xfinity SecClient runner
    already communicates: write ``runner_input.json``, run the process, read
    ``runner_output.json``.
    """

    def __init__(self, log: Callable[[str], None] | None = None, debug: bool = False):
        self.log = log or (lambda _line: None)
        self.debug = debug

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        stdin_text: str | None = None,
        check: bool = True,
    ) -> HelperResult:
        argv = [str(part) for part in argv]
        if self.debug:
            self.log(f"exec: {' '.join(argv)}")
        else:
            self.log(f"exec: {Path(argv[0]).name} ({len(argv) - 1} args)")

        merged_env = {**os.environ, **(env or {})} if env else None
        try:
            completed = subprocess.run(  # noqa: S603 - argv is assembled from resolved helpers
                argv,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise HelperError(f"{Path(argv[0]).name} timed out after {timeout}s") from exc
        except OSError as exc:
            raise HelperError(f"could not run {argv[0]}: {exc}") from exc

        result = HelperResult(argv, completed.returncode, completed.stdout or "", completed.stderr or "")
        if self.debug:
            for line in (result.stdout + result.stderr).splitlines()[:40]:
                self.log(f"  {line}")
        if check and not result.ok:
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            detail = tail[-1] if tail else f"exit {result.exit_code}"
            raise HelperError(f"{Path(argv[0]).name} failed: {detail}")
        return result

    def run_json(
        self,
        argv: Sequence[str],
        payload: dict[str, Any],
        *,
        input_file: Path,
        output_file: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 180,
    ) -> dict[str, Any]:
        """Write ``payload``, run the helper, read the result back."""
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_text(json.dumps(payload, indent=2), "utf-8")
        if output_file.exists():
            output_file.unlink()

        self.run(argv, cwd=cwd, env=env, timeout=timeout)

        if not output_file.exists():
            raise HelperError(f"helper produced no output at {output_file}")
        try:
            data = json.loads(output_file.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise HelperError(f"helper output was not valid JSON: {exc}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise HelperError(str(data["error"]))
        return data if isinstance(data, dict) else {"result": data}


def load_module(path: Path, name: str | None = None) -> ModuleType:
    """Import a Python module from an arbitrary file path.

    Used by services that depend on a helper module living outside the package
    (apple's local Mescal signer, ytv's SABR helper). Kept in one place so the
    failure message is consistent instead of an opaque ImportError.
    """
    path = _expand(path)
    if not path.exists():
        raise HelperError(f"helper module not found: {path}")
    module_name = name or f"unidl_helper_{path.stem}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise HelperError(f"could not load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise HelperError(f"helper module {path.name} failed to import: {exc}") from exc
    return module


# ------------------------------------------------------- common declarations

JAVA = Helper(
    key="java",
    label="Java runtime",
    candidates=("java",),
    version_args=("-version",),
    must_run=True,
    extra_paths=(
        "$JAVA_HOME/bin/java",
        "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/java",
        "/usr/local/opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/java",
        "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
    ),
    install_hint="brew install openjdk, or set helpers.java in unidl.yaml",
)
JAVAC = Helper(
    key="javac",
    label="Java compiler",
    candidates=("javac",),
    version_args=("-version",),
    # The same two rules as JAVA above, for the same reason. Without them macOS's
    # /usr/bin/javac stub was accepted: it exists, it runs, and all it says is that
    # there is no Java here - so resolution succeeded, the Homebrew JDK beside it
    # was never looked for, and the service that needs a compiler failed later with
    # a message about something else.
    must_run=True,
    extra_paths=(
        "$JAVA_HOME/bin/javac",
        "/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/javac",
        "/usr/local/opt/openjdk/libexec/openjdk.jdk/Contents/Home/bin/javac",
        "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/javac",
    ),
    install_hint="brew install openjdk, or set helpers.javac in unidl.yaml",
)
NODE = Helper(
    key="node",
    label="Node.js",
    candidates=("node", "nodejs"),
    install_hint="brew install node, or set helpers.node in unidl.yaml",
)
ADB = Helper(
    key="adb",
    label="Android Debug Bridge",
    candidates=("adb",),
    install_hint="brew install android-platform-tools, or set helpers.adb in unidl.yaml",
)
SUBBY = Helper(
    key="subby",
    label="subby subtitle converter",
    candidates=("subby",),
    required=False,
    degrades_to="subtitles stay in their original format",
    install_hint="pipx install subby, or set helpers.subby in unidl.yaml",
)
FFMPEG = Helper(
    key="ffmpeg",
    label="ffmpeg",
    candidates=("ffmpeg",),
    version_args=("-version",),
    install_hint="brew install ffmpeg",
)
MKVMERGE = Helper(
    key="mkvmerge",
    label="mkvmerge",
    candidates=("mkvmerge",),
    required=False,
    degrades_to="muxing falls back to ffmpeg",
    install_hint="brew install mkvtoolnix",
)

__all__ = [
    "ADB",
    "FFMPEG",
    "JAVA",
    "JAVAC",
    "MKVMERGE",
    "NODE",
    "SUBBY",
    "Helper",
    "HelperError",
    "HelperKind",
    "HelperReport",
    "HelperResolver",
    "HelperResult",
    "HelperRunner",
    "ResolvedHelper",
    "load_module",
]
