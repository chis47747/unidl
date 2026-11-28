# Requirements and installation

[English](requirements.md) | [简体中文](zh-CN/requirements.md)

## Supported runtime

UniDL requires **Python 3.11 or newer** and runs on supported releases of
Windows, macOS and Linux. A 64-bit Python build is recommended because CDM
libraries and media tools are commonly distributed for 64-bit platforms.

The Python dependencies are declared in `pyproject.toml`. The repository also
ships `requirements.txt` as a convenient, minimum-version installation list and
`requirements-dev.txt` for the test and lint toolchain. `pip` resolves the
transitive dependencies automatically.

## Install from PyPI

Install the published package into a virtual environment:

~~~console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install unidl
~~~

Using `python -m pip` ensures the `unidl` entry point is installed into the
active interpreter's environment.

## Install from a source checkout

For a regular install from a local checkout:

~~~console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .
~~~

For development, install editable mode with test and lint tools:

~~~console
python -m pip install -e '.[dev]'
~~~

The equivalent requirements-file form is:

~~~console
python -m pip install -r requirements-dev.txt
~~~

With `uv`:

~~~console
uv sync --extra dev
~~~

## Required external tools

Python packages do not include native media binaries or private device data.

UniDL checks the operating-system `PATH` first. If a command is not installed
there, it also checks the project-level `binaries/` directory. This lets a
portable checkout keep tools such as `ffmpeg`, `ffprobe`, `mkvmerge`, and
`aria2c` beside the project without overriding a newer system installation.
On Unix-like systems, files placed there must be executable; on Windows, use
the normal `.exe`/`.cmd` extension.

- **FFmpeg and ffprobe** are strongly recommended and are required for several
  muxing, audio conversion, subtitle conversion and media-probing paths.
- **pymonalisa** and **wasmtime** are included for the MonaLisa local-ticket DRM
  system; a `.mld` device still needs its referenced WebAssembly module.
- **mkvmerge** is optional. It is used when selected or when it gives a better
  Matroska result; FFmpeg remains the fallback where possible.
- **MP4Box** (GPAC) is optional and is used when MP4Box muxing is selected or
  for stereoscopic 3D VOD outputs.
- **dovi_tool** is optional and is used for Dolby Vision + HDR10 hybrid output
  (including automatic L5 active-area crop and L6 luminance metadata) and
  HDR10+-to-DV Profile 8.1 generation.
- **hdr10plus_tool** is optional and is only needed when `generate_dv_from_hdr10p`
  is enabled to extract HDR10+ dynamic metadata into Dolby Vision Profile 8.1.
- **shaka-packager** is optional for packaging/decryption paths or services that
  use it.
- **aria2c** is optional and is used only when the user selects that segment
  backend.
- **curl** is optional for services that explicitly declare it as a helper or
  for transport fallbacks.

Install these with your operating system's package manager—or directly inside
the TUI from **Readiness → Install tools** (click the header button or press `i`
to review and confirm platform install commands)—and verify them with:

~~~console
ffmpeg -version
ffprobe -version
~~~

The TUI reports missing declared helpers before a service starts playback. A
service may have additional provider-specific requirements; those belong in its
service documentation and helper declaration.

## Runtime data

UniDL creates its configured state directories when needed. CDM device files,
browser cookies, credentials, access/refresh tokens, key vault databases,
helpers, signed URLs and exported commands are runtime data and are not supplied
by the Python package. Put them in the paths configured by `unidl.yaml`; keep
private material outside version control.

After installation, start the interface with `unidl`. Use an explicit YAML file
when it is not in the current directory:

~~~console
unidl
unidl --config ./unidl.yaml
~~~

Read-only checks and Git service repository refreshes are also available from
the CLI:

~~~console
unidl --config ./unidl.yaml services
unidl --config ./unidl.yaml cdm --check
unidl --config ./unidl.yaml schedule list
unidl --config ./unidl.yaml refresh-services          # add --force to bypass TTL
~~~
