from __future__ import annotations

from urllib.error import URLError

from unidl.core import update


def test_check_for_updates_uses_the_highest_public_version(monkeypatch) -> None:
    def fake_json_get(url: str, *, timeout: float):
        assert timeout == update.DEFAULT_TIMEOUT
        if url == update.PYPI_JSON_URL:
            return {"info": {"version": "2.0.4"}}
        return {
            "tag_name": "v2.0.3",
            "name": "UniDL 2.0.3",
            "body": "Bug fixes [not markup]",
            "html_url": "https://github.com/chis47747/unidl/releases/tag/v2.0.3",
        }

    # Keep this fixture's positive-update scenario stable after each release bump.
    monkeypatch.setattr(update, "__version__", "2.0.3")
    monkeypatch.setattr(update, "_json_get", fake_json_get)
    update.clear_update_cache()
    found = update.check_for_updates()

    assert found.latest_version == "2.0.4"
    assert found.pypi_version == "2.0.4"
    assert found.github_version == "2.0.3"
    assert found.update_available
    assert found.release_notes == "Bug fixes [not markup]"


def test_partial_failure_never_claims_current(monkeypatch) -> None:
    def fake_json_get(url: str, *, timeout: float):
        if url == update.PYPI_JSON_URL:
            return {"info": {"version": "2.0.3"}}
        raise URLError("offline")

    monkeypatch.setattr(update, "_json_get", fake_json_get)
    update.clear_update_cache()
    found = update.check_for_updates()

    assert found.checked
    assert found.error
    assert not found.current
    assert not found.update_available
