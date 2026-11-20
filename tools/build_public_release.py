"""Build and audit the public wheel and sdist from a version tag, without uploading."""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

_PUBLIC_INPUTS = (
    "src/unidl", "docs", "pyproject.toml", "MANIFEST.in", "LICENSE",
    "README.md", "README.zh-CN.md", "CHANGELOG.md", "requirements.txt",
    "requirements-dev.txt",
)
_BUNDLED_DEFAULTS = {"src/unidl/unidl.yaml", "unidl/unidl.yaml"}
_PRIVATE_DIRS = {"test", "tests", "tools", "tokens", "cookies", "logs", "cache", "downloads",
                 "download_commands", "cdm", "db", "__pycache__", "exports", ".git"}
_PRIVATE_EXTENSIONS = {".wvd", ".prd", ".mld", ".sqlite", ".sqlite3", ".db", ".pem", ".so", ".pyd", ".zip"}
_EGG_METADATA = {"PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
                 "requires.txt", "top_level.txt", "not-zip-safe", "zip-safe"}


def _forbidden(path: PurePosixPath) -> bool:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return (
        path.is_absolute() or ".." in path.parts or "\\" in str(path)
        or bool(parts & _PRIVATE_DIRS)
        or name == "agents.md"
        or (name == "unidl.yaml" and path.as_posix() not in _BUNDLED_DEFAULTS)
        or name == "conftest.py" or name.startswith(("test_", ".env"))
        or path.suffix.lower() in _PRIVATE_EXTENSIONS
    )


def _audit_artifact(path: Path, allowed: set[str]) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            files = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
        for name in files:
            parts = PurePosixPath(name).parts
            metadata = bool(parts and parts[0].endswith(".dist-info"))
            if _forbidden(PurePosixPath(name)) or (not metadata and f"src/{name}" not in allowed):
                raise ValueError(f"unexpected input in wheel: {name}")
        return
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            path_parts = PurePosixPath(member.name).parts
            relative = PurePosixPath(*path_parts[1:])
            name = relative.as_posix()
            metadata = name in {"PKG-INFO", "setup.cfg"} or (
                relative.parent == PurePosixPath("src/unidl.egg-info") and relative.name in _EGG_METADATA
            )
            if not member.isfile() or _forbidden(relative) or (name not in allowed and not metadata):
                raise ValueError(f"unexpected input in source distribution: {member.name}")


def build(tag: str, output: Path) -> None:
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:[a-z]+\d+)?", tag):
        raise ValueError("use an exact release tag, for example v2.6.6")
    repo = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], cwd=repo, text=True,
    ).strip()
    tree = subprocess.check_output(["git", "ls-tree", "-rz", "--name-only", commit], cwd=repo)
    allowed = {
        name for name in tree.decode("utf-8").split("\0") if name
        and any(name == root or name.startswith(f"{root}/") for root in _PUBLIC_INPUTS)
    }
    if bad := sorted(name for name in allowed if _forbidden(PurePosixPath(name))):
        raise ValueError(f"release tag contains a forbidden input: {bad[0]}")
    archive = subprocess.check_output(["git", "archive", commit, "--", *sorted(allowed)], cwd=repo)
    with TemporaryDirectory(prefix="unidl-public-release-") as stage:
        source = Path(stage) / "source"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(source, filter="data")
        with (source / "pyproject.toml").open("rb") as stream:
            version = str(tomllib.load(stream)["project"]["version"])
        if tag != f"v{version}":
            raise ValueError(f"{tag} does not match the package version {version}")
        dist = Path(stage) / "dist"
        subprocess.run(["uv", "build", "--out-dir", str(dist)], cwd=source, check=True)
        artifacts = sorted(item for item in dist.iterdir() if item.name.endswith((".whl", ".tar.gz")))
        if len(artifacts) != 2 or {item.suffix for item in artifacts} != {".gz", ".whl"}:
            raise ValueError("expected exactly one wheel and one source distribution")
        for item in artifacts:
            _audit_artifact(item, allowed)
        output.mkdir(parents=True, exist_ok=True)
        if any((output / item.name).exists() for item in artifacts):
            raise ValueError("output already contains this release; use an empty directory")
        for item in artifacts:
            shutil.copy2(item, output / item.name)
            print(f"Validated {output / item.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        build(args.tag, args.out_dir.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release build stopped: {exc}\n")
