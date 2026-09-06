# Requirements and installation

## Supported runtime

UniDL requires **Python 3.11 or newer** and runs on supported releases of
Windows, macOS and Linux. A 64-bit Python build is recommended because CDM
libraries and media tools are commonly distributed for 64-bit platforms.

The Python dependencies are declared in `pyproject.toml`. The repository also
ships `requirements.txt` as a convenient, minimum-version installation list and
`requirements-dev.txt` for the test and lint toolchain. `pip` resolves the
transitive dependencies automatically.

## Install from a published package

When a UniDL release is published to PyPI, install it into a virtual environment
with:

~~~console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install unidl
~~~

The `unidl` command is installed by the package entry point. The package name is
also available to tools that use `python -m pip`; using the interpreter's pip
avoids accidentally installing into a different Python environment.

## Install from a source checkout

For a normal user install from a checked-out release:

~~~console
python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .
~~~

For development, install the editable package and its checks:

~~~console
python -m pip install -e '.[dev]'
~~~

The equivalent requirements-file form is:

~~~console
python -m pip install -r requirements-dev.txt
~~~

`uv` users can create the same environment with:

~~~console
uv sync --extra dev
~~~

The current source checkout is therefore package-installable now; a plain
`pip install unidl` command becomes the shortest route once the project is
published to the Python package index.

## Required external tools

Python packages do not include native media binaries or private device data.

- **FFmpeg and ffprobe** are strongly recommended and are required for several
  muxing, audio conversion, subtitle conversion and media-probing paths.
- **mkvmerge** is optional. It is used when selected or when it gives a better
  Matroska result; FFmpeg remains the fallback where possible.
- **aria2c** is optional and is used only when the user selects that segment
  backend.
- **curl** is optional for services that explicitly declare it as a helper or
  for transport fallbacks.

Install these with the operating system's package manager and verify them with:

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

Read-only checks are useful before opening a service:

~~~console
unidl --config ./unidl.yaml services
unidl --config ./unidl.yaml cdm --check
~~~
