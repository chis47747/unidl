"""Manual content-key entry.

This is deliberately a vault screen, not an import screen.  An export carries a
manifest, tracks and keys and can finish a download; this screen records only
explicit KID:key pairs.  It validates the whole paste before writing anything,
keeps each local write atomic, and never contacts a remote vault unless the person
selects that destination on this visit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..core import vaults
from ..core.i18n import phrase, tr
from ..core.vault import (
    KeyConflict,
    KeyConflictError,
    KeyWriteResult,
    PairParseError,
    parse_pairs,
)
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, refresh_locale_widgets
from .input import ClipboardInput as Input

_PAIR_IN_ERROR = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f-]{36})\s*[:：]\s*[0-9a-f]{32}(?![0-9a-f])"
)


def _safe_error(value: object) -> str:
    """A backend message suitable for a screen or notification."""
    text = " ".join(str(value or "").split())
    return _PAIR_IN_ERROR.sub("<key pair>", text)[:180]


def _masked_key(value: str) -> str:
    return f"{value[:6]}…{value[-6:]}" if len(value) > 12 else tr("vault.stored_key")


@dataclass(frozen=True)
class AddKeysResult:
    """What the parent search screen needs to refresh and report."""

    service: str
    reports: tuple[vaults.VaultWriteResult, ...]

    @property
    def local(self) -> KeyWriteResult:
        """Compatibility summary for callers written for the single local DB."""
        local = [report for report in self.reports if not report.remote]
        return KeyWriteResult(
            total=sum(report.added + report.existing for report in local),
            added=sum(report.added for report in local),
            existing=sum(report.existing for report in local),
            replaced=sum(report.replaced for report in local),
        )

    @property
    def remote(self) -> tuple[vaults.VaultWriteResult, ...]:
        return tuple(report for report in self.reports if report.remote)

    def summary(self) -> str:
        parts: list[str] = []
        for report in self.reports:
            if report.error:
                parts.append(
                    tr("vault.summary.failed", name=report.name, error=_safe_error(report.error))
                )
            elif report.skipped:
                parts.append(tr("vault.summary.skipped", name=report.name, skipped=report.skipped))
            else:
                result = tr("vault.summary.added", name=report.name, added=report.added)
                if report.existing:
                    result += tr("vault.summary.existing", count=report.existing)
                if report.replaced:
                    result += tr("vault.summary.replaced", count=report.replaced)
                parts.append(result)
        return " · ".join(parts) or tr("vault.summary.none")


class AddKeysScreen(Screen[AddKeysResult | None]):
    """Validate and store one or many manual KID:key pairs."""

    BINDINGS = [
        Binding("ctrl+b", "cancel", "Back", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("ctrl+s", "save", "Review and add", show=False, priority=True),
        Binding("up", "service_cursor(-1)", "Previous service", show=False),
        Binding("down", "service_cursor(1)", "Next service", show=False),
    ]

    def __init__(self, *, service_id: str = "", initial: str = "") -> None:
        super().__init__()
        self.service_id = service_id
        self.initial = initial
        self._vaults: vaults.Vaults | None = None
        self._descriptors: list[vaults.VaultDescriptor] = []
        self._write_targets: tuple[str, ...] = ()
        self._confirm_signature: tuple[str, str, str, tuple[str, ...]] | None = None
        self._busy = False
        self._service_rows: list[tuple[str, str, str]] = []
        self._visible_services: list[str] = []
        self._selected_service = ""

    def compose(self) -> ComposeResult:
        services = sorted(self.app.services, key=lambda service: service.NAME.lower())
        self._service_rows = [
            (
                service.ID,
                service.NAME,
                " ".join(
                    [service.ID, service.NAME, service.tag(), *service.ALIASES]
                ).casefold(),
            )
            for service in services
        ]
        self._service_rows.append(
            ("unknown", tr("vault.unknown_service"), "unknown manual not listed")
        )
        known = {service_id for service_id, _name, _search in self._service_rows}
        self._selected_service = self.service_id if self.service_id in known else ""

        yield Chrome(show_search=False, show_settings=False)
        yield Static(tr("vault.add.title"), id="masthead")
        yield Static(tr("vault.add.help"), id="subhead")
        with VerticalScroll(id="vault-add-body"):
            with Vertical(id="vault-add-card"):
                yield Label(tr("vault.field.service"), classes="vault-field-label")
                yield Input(
                    value=self._selected_service,
                    placeholder=tr("vault.placeholder.service"),
                    id="vault-service-query",
                )
                yield Static("", id="vault-service-state")
                yield OptionList(id="vault-service-list")
                yield Label(tr("vault.field.title"), classes="vault-field-label")
                yield Input(placeholder=tr("vault.placeholder.title"), id="vault-title")
                yield Label(tr("vault.field.keys"), classes="vault-field-label")
                yield TextArea(
                    self.initial,
                    soft_wrap=False,
                    tab_behavior="focus",
                    show_line_numbers=True,
                    placeholder=tr("vault.placeholder.keys"),
                    id="vault-pairs",
                )
                yield Static("", id="vault-validation")
                yield Label(tr("vault.field.destinations"), classes="vault-field-label")
                with Horizontal(id="vault-destination-row"):
                    yield Static("", id="vault-destination-state")
                    yield Button(tr("vault.choose_vaults"), id="vault-destination-choose")
                with Horizontal(id="vault-add-actions"):
                    yield Button(tr("vault.review_add"), variant="primary", id="vault-save")
                    yield Button(phrase("Cancel"), id="vault-cancel")
        yield KeyBar(
            ("^s", "review and add"),
            ("tab", "move between fields"),
            ("^b", "cancel"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self._vaults = self.app.vaults
        self._descriptors = [
            descriptor
            for descriptor in vaults.configured_vaults(self.app.config)
            if descriptor.writable
        ]
        configured = vaults.parse_targets(
            self.app.globals.get("vault_write_targets", "")
        )
        wanted = (
            {descriptor.name.casefold() for descriptor in self._descriptors}
            if configured is None
            else {name.casefold() for name in configured}
        )
        remote_enabled = bool(self.app.globals.get("remote_vault", False))
        self._write_targets = tuple(
            descriptor.name
            for descriptor in self._descriptors
            if descriptor.name.casefold() in wanted
            and (not descriptor.remote or remote_enabled)
        )
        self._update_destination_state()
        self._rebuild_services()
        target = "#vault-pairs" if self._selected_service else "#vault-service-query"
        self.query_one(target).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self.query_one("#masthead", Static).update(tr("vault.add.title"))
        self.query_one("#subhead", Static).update(tr("vault.add.help"))
        labels = list(self.query(".vault-field-label"))
        if len(labels) >= 4:
            labels[0].update(tr("vault.field.service"))
            labels[1].update(tr("vault.field.title"))
            labels[2].update(tr("vault.field.keys"))
            labels[3].update(tr("vault.field.destinations"))
        self.query_one("#vault-service-query", Input).placeholder = tr("vault.placeholder.service")
        self.query_one("#vault-title", Input).placeholder = tr("vault.placeholder.title")
        self.query_one("#vault-pairs", TextArea).placeholder = tr("vault.placeholder.keys")
        self.query_one("#vault-destination-choose", Button).label = tr("vault.choose_vaults")
        save = self.query_one("#vault-save", Button)
        save.label = (
            tr("vault.replace_add") if self._confirm_signature is not None else tr("vault.review_add")
        )
        self.query_one("#vault-cancel", Button).label = phrase("Cancel")
        if self._service_rows:
            service_id, _name, search = self._service_rows[-1]
            if service_id == "unknown":
                self._service_rows[-1] = ("unknown", tr("vault.unknown_service"), search)
        self._rebuild_services()
        self._update_destination_state()

    # ---------------------------------------------------------- service picker
    def _rebuild_services(self, needle: str | None = None) -> None:
        query = self.query_one("#vault-service-query", Input)
        value = query.value.strip() if needle is None else needle.strip()
        lowered = value.casefold()
        ranked: list[tuple[int, str, str]] = []
        for service_id, name, search in self._service_rows:
            if not lowered:
                rank = 0
            elif lowered == service_id.casefold() or lowered == name.casefold():
                rank = 0
            elif service_id.casefold().startswith(lowered) or name.casefold().startswith(lowered):
                rank = 1
            elif lowered in search:
                rank = 2
            else:
                continue
            ranked.append((rank, name.casefold(), service_id))
        ranked.sort()
        self._visible_services = [service_id for _rank, _name, service_id in ranked[:8]]

        option_list = self.query_one("#vault-service-list", OptionList)
        option_list.clear_options()
        names = {service_id: name for service_id, name, _search in self._service_rows}
        for service_id in self._visible_services:
            mark = f"  [$ok]{tr('vault.selected').lower()}[/]" if service_id == self._selected_service else ""
            option_list.add_option(
                Option(
                    f"  [$foreground]{visual_markup(names[service_id])}[/]  "
                    f"[$dim]{service_id}[/]{mark}",
                    id=service_id,
                )
            )
        if self._visible_services:
            option_list.highlighted = (
                self._visible_services.index(self._selected_service)
                if self._selected_service in self._visible_services
                else 0
            )
        else:
            option_list.add_option(Option(f"  [$dim]{tr('vault.no_platform_row')}[/]", disabled=True))
        # Mount at most eight rows and give the empty rows back immediately as
        # the query narrows. An exact selection is one row, leaving the key editor
        # and save controls visible without scrolling on an ordinary-height TUI.
        option_list.styles.height = max(1, min(6, len(self._visible_services)))

        state = self.query_one("#vault-service-state", Static)
        if self._selected_service and value.casefold() == self._selected_service.casefold():
            name = names.get(self._selected_service, self._selected_service)
            state.update(
                f"[$ok]{tr('vault.selected')}[/] {visual_markup(name)}  "
                f"[$dim]· {tr('vault.selected_hint')}[/]"
            )
        else:
            count = len(ranked)
            shown = min(8, count)
            state.update(
                f"[$dim]{tr('vault.matches_n', shown=shown, count=count)}[/]"
                if count
                else f"[$warn]{tr('vault.no_platform')}[/]"
            )

    def _choose_service(self, service_id: str, *, move: bool = True) -> None:
        if service_id not in {row[0] for row in self._service_rows}:
            return
        self._selected_service = service_id
        query = self.query_one("#vault-service-query", Input)
        with query.prevent(Input.Changed):
            query.value = service_id
            query.cursor_position = len(service_id)
        self._rebuild_services(service_id)
        self._reset_confirmation()
        if move:
            self.query_one("#vault-title", Input).focus()

    def _resolve_service(self) -> str:
        value = self.query_one("#vault-service-query", Input).value.strip()
        if self._selected_service and value.casefold() == self._selected_service.casefold():
            return self._selected_service
        exact = [
            service_id
            for service_id, name, _search in self._service_rows
            if value.casefold() in (service_id.casefold(), name.casefold())
        ]
        if len(exact) == 1:
            return exact[0]
        if value and len(self._visible_services) == 1:
            return self._visible_services[0]
        return ""

    # ------------------------------------------------------------ invalidation
    def _raw_signature(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (
            self.query_one("#vault-service-query", Input).value,
            self.query_one("#vault-title", Input).value,
            self.query_one("#vault-pairs", TextArea).text,
            self._write_targets,
        )

    def _reset_confirmation(self) -> None:
        if self._busy or self._confirm_signature is None:
            return
        # Input/TextArea post their Changed message after the value changes. A
        # person can press save before that queued message is dispatched; compare
        # the actual fields so that stale delivery does not erase the confirmation
        # which was just shown for those exact values.
        if self._raw_signature() == self._confirm_signature:
            return
        self._confirm_signature = None
        self.query_one("#vault-save", Button).label = tr("vault.review_add")

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._reset_confirmation()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "vault-service-query":
            if event.value.strip().casefold() != self._selected_service.casefold():
                self._selected_service = ""
            self._rebuild_services(event.value)
        self._reset_confirmation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "vault-service-query":
            return
        event.stop()
        option_list = self.query_one("#vault-service-list", OptionList)
        index = option_list.highlighted
        if index is not None and index < len(self._visible_services):
            self._choose_service(self._visible_services[index])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "vault-service-list":
            return
        event.stop()
        if event.option.id:
            self._choose_service(event.option.id)

    # ------------------------------------------------------- destination picker
    def _update_destination_state(self) -> None:
        state = self.query_one("#vault-destination-state", Static)
        if not self._write_targets:
            state.update(f"[$warn]{tr('vault.no_destination')}[/]")
            return
        kinds = {descriptor.name: descriptor.remote for descriptor in self._descriptors}
        labels = [
            f"{name} ({tr('vault.dest.remote') if kinds.get(name) else tr('vault.dest.local')})"
            for name in self._write_targets
        ]
        state.update(visual_markup(", ".join(labels)))

    def _pick_destinations(self) -> None:
        from .vault_targets import VaultTargetResult, VaultTargetScreen

        all_names = tuple(descriptor.name for descriptor in self._descriptors)
        current = None if self._write_targets == all_names else self._write_targets

        def finished(result: VaultTargetResult | None) -> None:
            if result is None:
                return
            self._write_targets = all_names if result.selected is None else result.selected
            self._update_destination_state()
            self._reset_confirmation()

        self.app.push_screen(
            VaultTargetScreen(
                self._descriptors,
                current,
                title=tr("vault.choose_title"),
                writable_only=True,
            ),
            finished,
        )

    def _selected_backends(self) -> list[vaults.Vault]:
        if self._vaults is None:
            return []
        return self._vaults.enabled(
            use_local=True,
            use_remote=True,
            local_names=self._write_targets,
            remote_names=self._write_targets,
        )

    # ----------------------------------------------------------------- submit
    def action_save(self) -> None:
        if self._busy:
            return
        service = self._resolve_service()
        if not service:
            self._problem(tr("vault.need_service"))
            self.query_one("#vault-service-query", Input).focus()
            return
        title = self.query_one("#vault-title", Input).value.strip()
        try:
            pairs = parse_pairs(self.query_one("#vault-pairs", TextArea).text)
        except PairParseError as exc:
            lines = "\n".join(
                tr("vault.line_error", line=issue.line, reason=issue.reason)
                for issue in exc.issues[:6]
            )
            extra = len(exc.issues) - 6
            self._problem(lines + (f"\n{tr('vault.and_more', count=extra)}" if extra > 0 else ""))
            self.query_one("#vault-pairs", TextArea).focus()
            return
        if not pairs:
            self._problem(tr("vault.need_pairs"))
            self.query_one("#vault-pairs", TextArea).focus()
            return
        backends = self._selected_backends()
        if not backends:
            self._problem(tr("vault.need_destination"))
            self.query_one("#vault-destination-choose", Button).focus()
            return

        conflicts: list[tuple[str, KeyConflict]] = []
        replace_names: list[str] = []
        new = 0
        existing = 0
        for backend in backends:
            if not isinstance(backend, vaults.LocalVault):
                continue
            try:
                preview = backend.vault.preview_pairs(pairs)
            except Exception as exc:  # noqa: BLE001 - another local DB may still be usable
                self._problem(tr("vault.inspect_failed", name=backend.name, error=_safe_error(exc)))
                return
            new += len(preview.new)
            existing += len(preview.existing)
            if preview.conflicts:
                replace_names.append(backend.name)
                conflicts.extend((backend.name, conflict) for conflict in preview.conflicts)
        signature = self._raw_signature()
        if conflicts and self._confirm_signature != signature:
            self._confirm_signature = signature
            self._show_conflicts(conflicts, new, existing)
            self.query_one("#vault-save", Button).label = tr("vault.replace_add")
            return

        self._set_busy(True)
        self._write(
            service,
            title,
            tuple(pairs),
            self._write_targets,
            tuple(replace_names),
        )

    def _show_conflicts(
        self,
        conflicts: list[tuple[str, KeyConflict]],
        new: int,
        existing: int,
    ) -> None:
        rows = [
            tr(
                "vault.conflict_header",
                new=new,
                existing=existing,
                conflicts=len(conflicts),
            )
        ]
        for backend, conflict in conflicts[:5]:
            old = ", ".join(_masked_key(key) for key in conflict.existing_keys)
            where = ", ".join(conflict.services) or tr("vault.unknown_service_label")
            rows.append(
                tr("vault.conflict_row", backend=backend, kid=conflict.kid, old=old, where=where)
            )
        if len(conflicts) > 5:
            rows.append(tr("vault.conflict_more", count=len(conflicts) - 5))
        rows.append(tr("vault.conflict_review"))
        self.query_one("#vault-validation", Static).update("\n".join(rows))
        self.query_one("#vault-validation", Static).set_class(True, "warning")

    def _problem(self, message: str) -> None:
        status = self.query_one("#vault-validation", Static)
        status.update(message)
        status.set_class(True, "error")
        status.set_class(False, "warning")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for selector in (
            "#vault-service-query",
            "#vault-service-list",
            "#vault-title",
            "#vault-pairs",
            "#vault-destination-choose",
            "#vault-save",
            "#vault-cancel",
        ):
            self.query_one(selector).disabled = busy
        if busy:
            status = self.query_one("#vault-validation", Static)
            status.update(tr("vault.writing"))
            status.set_class(False, "error")
            status.set_class(False, "warning")

    def action_service_cursor(self, delta: int) -> None:
        if getattr(self.focused, "id", "") not in (
            "vault-service-query",
            "vault-service-list",
        ):
            return
        option_list = self.query_one("#vault-service-list", OptionList)
        count = len(self._visible_services)
        if not count:
            return
        current = option_list.highlighted
        if current is None:
            current = -1 if delta > 0 else count
        option_list.highlighted = max(0, min(count - 1, current + delta))

    @work(thread=True, exclusive=True)
    def _write(
        self,
        service: str,
        title: str,
        pairs: tuple[str, ...],
        targets: tuple[str, ...],
        replace_names: tuple[str, ...],
    ) -> None:
        try:
            reports: list[vaults.VaultWriteResult] = []
            selected = self._selected_backends()
            replace = {name.casefold() for name in replace_names}
            for backend in selected:
                if not isinstance(backend, vaults.LocalVault):
                    continue
                try:
                    result = backend.vault.add_many(
                        service,
                        pairs,
                        title=title or None,
                        source="manual",
                        origin="tui",
                        replace_conflicts=backend.name.casefold() in replace,
                    )
                except KeyConflictError:
                    reports.append(
                        vaults.VaultWriteResult(
                            backend.name,
                            remote=False,
                            error=tr("vault.changed_after"),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - destinations are independent
                    reports.append(
                        vaults.VaultWriteResult(
                            backend.name,
                            remote=False,
                            error=str(exc),
                        )
                    )
                else:
                    reports.append(
                        vaults.VaultWriteResult(
                            backend.name,
                            remote=False,
                            added=result.added,
                            existing=result.existing,
                            replaced=result.replaced,
                        )
                    )
                finally:
                    backend.vault.close()
            if self._vaults is not None:
                reports.extend(
                    self._vaults.add_pairs_report(
                        service,
                        pairs,
                        use_local=False,
                        use_remote=True,
                        remote_names=targets,
                        title=title or None,
                        source="manual",
                        origin="tui",
                    )
                )
            result = AddKeysResult(service, tuple(reports))
        except Exception as exc:  # noqa: BLE001 - the screen must survive a vault failure
            self.app.call_from_thread(self._failed, exc)
        else:
            self.app.call_from_thread(self._finished, result)
        finally:
            # The Textual worker is short-lived; do not leave its thread-local
            # SQLite handle for garbage collection to discover after shutdown.
            if self._vaults is not None:
                self._vaults.close()

    def _failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self._problem(tr("vault.write_failed", error=_safe_error(exc)))

    def _finished(self, result: AddKeysResult) -> None:
        self._set_busy(False)
        self.dismiss(result)

    # ---------------------------------------------------------------- controls
    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "vault-save":
            self.action_save()
        elif event.button.id == "vault-destination-choose":
            self._pick_destinations()
        elif event.button.id == "vault-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        if self._busy:
            self.notify(tr("vault.busy"), timeout=3)
            return
        self.dismiss(None)

    def go_back(self) -> bool:
        self.action_cancel()
        return True


__all__ = ["AddKeysResult", "AddKeysScreen"]
