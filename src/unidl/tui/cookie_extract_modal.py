"""TUI modal for extracting Firefox browser cookies (and inspecting localStorage)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Select, Static

from ..core.browser_cookies import (
    BrowserExtractResult,
    discover_firefox_profiles,
    extract_firefox_cookies_to_store,
)
from ..core.cookies import CookieError, CookieStore
from ..core.i18n import phrase, tr
from .bidi import visual_markup
from .chrome import CloseMark, fit_modal_card
from .input import ClipboardInput as Input
from .vault_targets import VaultCheckbox


class FirefoxCookieExtractModal(ModalScreen[str | None]):
    """Extract cookies from a local Firefox profile into a service's CookieStore.

    Returns the saved cookie profile name (e.g. ``"default"``) when extraction
    succeeds, or ``None`` if cancelled.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("ctrl+s", "extract", "Extract", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        service_id: str = "",
        service_label: str = "",
        service_choices: Sequence[tuple[str, str]] = (),
        default_profile: str = "default",
    ) -> None:
        super().__init__()
        self.fixed_service_id = str(service_id or "").strip().lower()
        self.fixed_service_label = str(service_label or self.fixed_service_id).strip()
        self.service_choices = list(service_choices)
        self.default_profile = str(default_profile or "default").strip() or "default"
        self._profiles = discover_firefox_profiles()

    def compose(self) -> ComposeResult:
        with Vertical(id="cookie-extract-card", classes="proxy-editor-card"):
            with Horizontal(classes="modal-head"):
                yield Label(
                    tr("cookies.firefox.title", default="Extract cookies from Firefox"),
                    classes="proxy-editor-title",
                )
                yield CloseMark(id="cookie-extract-close")
            yield Label(
                tr(
                    "cookies.firefox.help",
                    default="Reads a temporary WAL-safe snapshot of Firefox cookies.sqlite and saves Netscape cookies for the selected service.",
                ),
                classes="proxy-editor-help",
            )
            with VerticalScroll(classes="proxy-editor-fields"):
                yield Label(tr("cookies.firefox.service", default="Target service"), classes="proxy-field-label")
                if self.fixed_service_id:
                    yield Static(
                        f"[$accent]{visual_markup(self.fixed_service_label)}[/]  [$dim]({self.fixed_service_id})[/]",
                        id="cookie-service-fixed",
                    )
                elif self.service_choices:
                    yield Select(
                        [(f"{label} ({sid})", sid) for sid, label in self.service_choices],
                        value=self.service_choices[0][0],
                        allow_blank=False,
                        compact=True,
                        id="cookie-service-select",
                    )
                else:
                    yield Input(placeholder="service_id", id="cookie-service-input")

                yield Label(tr("cookies.firefox.profile_dir", default="Firefox profile"), classes="proxy-field-label")
                if self._profiles:
                    options = [
                        (
                            f"{p.name}{' (default)' if p.is_default else ''} — {p.path.name}",
                            str(p.path),
                        )
                        for p in self._profiles
                    ]
                    yield Select(
                        options,
                        value=str(self._profiles[0].path),
                        allow_blank=False,
                        compact=True,
                        id="cookie-ff-select",
                    )
                else:
                    yield Static(
                        f"[$warn]{visual_markup(tr('cookies.firefox.no_profiles', default='No Firefox profile detected automatically — enter a profile folder below.'))}[/]",
                        id="cookie-ff-none",
                    )
                yield Label(
                    tr("cookies.firefox.custom_path", default="Custom Firefox profile path (optional override)"),
                    classes="proxy-field-label",
                )
                yield Input(placeholder="/path/to/firefox/profile", id="cookie-ff-path")

                yield Label(
                    tr("cookies.firefox.domains", default="Domains to match (comma-separated)"),
                    classes="proxy-field-label",
                )
                yield Input(placeholder="example.com, api.example.com", id="cookie-domains")

                yield Label(
                    tr("cookies.firefox.target_profile", default="Save as cookie profile name"),
                    classes="proxy-field-label",
                )
                yield Input(value=self.default_profile, placeholder="default", id="cookie-target-profile")

                yield VaultCheckbox(
                    tr("cookies.firefox.localstorage", default="Also inspect matching localStorage keys"),
                    value=False,
                    id="cookie-localstorage",
                )

            yield Static("", id="cookie-extract-error")
            with Horizontal(classes="proxy-editor-actions"):
                yield Button(
                    tr("cookies.firefox.extract", default="Extract cookies"),
                    variant="primary",
                    id="cookie-extract-run",
                )
                yield Button(phrase("Cancel"), id="cookie-extract-cancel")

    def on_mount(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)
        self.query_one("#cookie-domains", Input).focus()

    def on_resize(self) -> None:
        self._fit_modal()
        self.call_after_refresh(self._fit_modal)

    def _fit_modal(self) -> None:
        fit_modal_card(self, ".proxy-editor-card", ".proxy-editor-fields")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "cookie-domains":
            self.action_extract()
        elif event.input.id == "cookie-target-profile":
            self.action_extract()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cookie-extract-run":
            self.action_extract()
        else:
            self.action_cancel()

    def _fail(self, message: str, selector: str = "#cookie-domains") -> None:
        self.query_one("#cookie-extract-error", Static).update(f"[$bad]{visual_markup(message)}[/]")
        if self.query(selector):
            self.query_one(selector).focus()

    def _selected_service_id(self) -> str:
        if self.fixed_service_id:
            return self.fixed_service_id
        if self.query("#cookie-service-select"):
            return str(self.query_one("#cookie-service-select", Select).value or "").strip().lower()
        if self.query("#cookie-service-input"):
            return self.query_one("#cookie-service-input", Input).value.strip().lower()
        return ""

    def _selected_firefox_profile(self) -> Path | None:
        custom = self.query_one("#cookie-ff-path", Input).value.strip()
        if custom:
            return Path(custom).expanduser()
        if self.query("#cookie-ff-select"):
            raw = str(self.query_one("#cookie-ff-select", Select).value or "").strip()
            return Path(raw) if raw else None
        return None

    def action_extract(self) -> None:
        service_id = self._selected_service_id()
        if not service_id:
            self._fail(tr("cookies.firefox.need_service", default="Select or enter a target service ID."))
            return
        raw_domains = self.query_one("#cookie-domains", Input).value
        domains = [part.strip() for part in raw_domains.split(",") if part.strip()]
        if not domains:
            self._fail(
                tr("cookies.firefox.need_domains", default="Enter at least one domain, e.g. example.com."),
                "#cookie-domains",
            )
            return
        ff_profile = self._selected_firefox_profile()
        if ff_profile is None:
            self._fail(
                tr("cookies.firefox.need_profile", default="Select a Firefox profile or enter its folder path."),
                "#cookie-ff-path",
            )
            return
        profile_name = self.query_one("#cookie-target-profile", Input).value.strip() or "default"
        include_ls = self.query_one("#cookie-localstorage", Checkbox).value
        self.query_one("#cookie-extract-run", Button).disabled = True
        self.query_one("#cookie-extract-error", Static).update(
            f"[$dim]{visual_markup(tr('cookies.firefox.running', default='Extracting Firefox cookies…'))}[/]"
        )
        self._run_extract(service_id, domains, profile_name, ff_profile, include_ls)

    @work(thread=True, exclusive=True)
    def _run_extract(
        self,
        service_id: str,
        domains: list[str],
        profile_name: str,
        ff_profile: Path,
        include_ls: bool,
    ) -> None:
        store = CookieStore(self.app.config.paths.cookies)
        try:
            result = extract_firefox_cookies_to_store(
                store,
                service_id,
                domains,
                profile=profile_name,
                firefox_profile=ff_profile,
                include_localstorage=include_ls,
            )
        except CookieError as exc:
            try:
                self.app.call_from_thread(self._on_extract_error, str(exc))
            except RuntimeError:
                pass
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self.app.call_from_thread(self._on_extract_error, str(exc))
            except RuntimeError:
                pass
            return
        try:
            self.app.call_from_thread(self._on_extract_done, profile_name, result)
        except RuntimeError:
            pass

    def _on_extract_error(self, message: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#cookie-extract-run", Button).disabled = False
        self._fail(message)

    def _on_extract_done(self, profile_name: str, result: BrowserExtractResult) -> None:
        if not self.is_attached:
            return
        extra = ""
        if result.local_storage:
            total_keys = sum(len(v) for v in result.local_storage.values())
            extra = f" · localStorage: {total_keys}"
        self.notify(
            tr(
                "cookies.firefox.done",
                count=result.cookies_count,
                domains=", ".join(result.domains[:3]),
                profile=profile_name,
                default=f"Saved {result.cookies_count} cookie(s) ({', '.join(result.domains[:3])}) to {profile_name}.txt",
            )
            + extra,
            timeout=6,
        )
        self.dismiss(profile_name)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


__all__ = ["FirefoxCookieExtractModal"]
