# Publishing UniDL

GitHub and PyPI are separate services. A GitHub login controls repository
operations; it does not select the PyPI account that owns a package. UniDL is
uploaded with a PyPI API token or with a GitHub Actions trusted publisher.

## Local release with a PyPI token

1. Sign in to the intended account at <https://pypi.org/> and create an API
   token from **Account settings → API tokens**. For a package that does not
   exist yet, an account-scoped token is needed for the first upload. After the
   first release, rotate it and create a project-scoped token for subsequent
   releases.
2. Build and validate from the checkout:

   ~~~console
   uv build --clear
   uvx --from twine twine check dist/*
   ~~~

3. Provide the token only through the current shell environment, never in a
   committed file, YAML, command export or chat message:

   ~~~console
   read -r -s UV_PUBLISH_TOKEN
   export UV_PUBLISH_TOKEN
   uv publish dist/*
   unset UV_PUBLISH_TOKEN
   ~~~

   `uv publish` sends both the wheel and source archive to
   `https://upload.pypi.org/legacy/`. It rejects a duplicate filename, so bump
   the version in `pyproject.toml` for every new release. Verify the result at
   <https://pypi.org/project/unidl/> and in a clean virtual environment with
   `python -m pip install unidl`.

The equivalent Twine command is:

~~~console
python -m pip install twine
python -m twine upload dist/*
~~~

Twine reads `TWINE_USERNAME=__token__` and `TWINE_PASSWORD=pypi-...`; use the
same no-file secret handling as above. Do not put a PyPI token in `.pypirc` or
the repository.

## Switching GitHub accounts

The GitHub CLI can keep more than one account for `github.com`. Add the new
account without deleting the existing credential, then make it active for the
repository work:

~~~console
gh auth login --hostname github.com --git-protocol https --web
gh auth status --hostname github.com
gh auth switch --hostname github.com --user NEW_GITHUB_USERNAME
~~~

Check the active identity before creating a private repository or pushing:

~~~console
gh auth status --active --hostname github.com
gh repo view --json nameWithOwner,visibility
~~~

`gh auth switch` changes the active GitHub account only. It does not change the
PyPI account or the token used by `uv publish`. Keep the project repository
private until its contents and runtime-data exclusions have been reviewed.

## GitHub Actions trusted publishing

For unattended releases, configure a **trusted publisher** for the intended
PyPI project, repository and release workflow in PyPI account settings. The
workflow then requests an OIDC token and publishes without storing a long-lived
PyPI secret. Its job needs `id-token: write` permission and should build the
same wheel and sdist, run `twine check`, and publish only on a version tag.

This path still uses the PyPI account that configured the trusted publisher; the
GitHub account that owns the repository is a separate identity. A first release
can be made locally with the account token, after which trusted publishing can
be enabled and the account token revoked.
