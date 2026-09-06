"""Global search.

One input, one result surface. What you type is classified rather than
mode-switched, because clicking is the primary interface and a mode toggle is
one more thing to discover:

* 32 hex characters, or ``kid:key``  -> a key vault lookup
* an http(s) URL                     -> the service whose pattern matches it
* anything else                      -> service names, titles in the vault, and
  an availability lookup that answers "which of these 150 services has it"

Results are grouped into sections. Searching a service hands the term to that
service's own ``search`` flow, so there is one search implementation, not two.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ..core import vaults
from ..core.i18n import cell_width, tr
from ..core.service import Service
from ..core.vault import KeyRecord, hex_ids, normalize_hex, playready_kid_alias, split_pair
from .bidi import visual_markup
from .chrome import Chrome, KeyBar, StatusChip, refresh_locale_widgets
from .input import ClipboardInput as Input
from .justwatch_screen import JustWatchScreen, regions_from, search_region_from
from .vault_targets import VaultSearchPicker, VaultServicePicker

#: row kinds that do something when you pick them
_ACTIONABLE = {"service", "key", "availability", "more", "remote_search"}

#: How many rows a section shows before the rest go behind one "load more" row.
#: There are 155 services and the vault holds thousands of keys: listing either in
#: full pushes every other section off the screen, and the answer to "which
#: service did I mean" is in the first few rows or it is not in the list at all.
SECTION_LIMIT = 5

#: The name column, and the smallest gap after it. Longest service name today is
#: "Mediaset Infinity Espana" at twenty-four, and one longer than the column
#: overruns it rather than being run into what follows: "Mediaset Infinity
#: Espanasearch" would be one word, and the row exists to say two things.
NAME_WIDTH = 28
NAME_GAP = 2


def _column(text: str) -> str:
    """``text`` padded to the name column, with the gap guaranteed."""
    return f"{text}{' ' * max(NAME_GAP, NAME_WIDTH - cell_width(text))}"


@dataclass
class _Row:
    kind: str  # "service" | "key" | "remote_search" | "availability" | "header" | "more"
    service: type[Service] | None = None
    record: KeyRecord | None = None
    #: for "more": the section it belongs to, and how many rows it is hiding
    section: str = ""
    hidden: int = 0
    vault_name: str = ""


def _rank(service: type[Service], needle: str) -> int:
    """How well ``service`` answers ``needle``: 0 is exact, 3 is not at all.

    Every name the service answers to counts, including its tag - "dsnp" is what
    a shared command calls Disney+, and matching only NAME/ID/ALIASES meant the
    one service the query named was not the one at the top.
    """
    names = [service.NAME.lower(), service.ID.lower(), service.tag().lower()]
    names.extend(alias.lower() for alias in service.ALIASES)
    if any(name == needle for name in names):
        return 0
    if any(name.startswith(needle) for name in names):
        return 1
    if any(needle in name for name in names):
        return 2
    return 3


class GlobalSearchScreen(Screen):
    BINDINGS = [
        Binding("b", "app.global_back", "Back", show=False),
        Binding("ctrl+b", "app.global_back", "Back", show=False),
        Binding("ctrl+f", "app.global_search", "Search", show=False),
        Binding("ctrl+s", "app.global_settings", "Settings", show=False),
        Binding("escape", "app.global_quit", "Quit", show=False),
        Binding("enter", "noop", "Open", show=True),
        Binding("ctrl+y", "copy_key", "Copy key", show=True),
        Binding("ctrl+n", "add_keys", "Add keys", show=True),
    ]

    def action_noop(self) -> None:
        """Footer-only hint; Enter is handled by the focused widget."""

    def __init__(self, query: str = "", service: Service | None = None):
        super().__init__()
        self._query = query
        #: when set, this is a key search inside one service rather than a
        #: global search across services
        self._service = service
        self._rows: list[_Row] = []
        #: what the list currently answers, so a section can be expanded without
        #: re-reading the input
        self._needle = query
        #: sections the user asked to see in full. Cleared when the query changes:
        #: a new question starts folded again.
        self._expanded: set[str] = set()
        #: Remote lookup is explicit and asynchronous. Results are kept separate
        #: from local rows so typing never causes a network request.
        self._remote_query = ""
        self._remote_vault = ""
        #: Empty means the explicit all-platform scope; a tag means the user
        #: narrowed this request before it touched a remote backend.
        self._remote_service = ""
        self._remote_records: list[KeyRecord] = []
        self._remote_status = ""
        self._remote_busy = False
        self._search_target_signature = ""

    @property
    def scoped(self) -> bool:
        return self._service is not None

    @property
    def service_id(self) -> str | None:
        return self._service.ID if self._service is not None else None

    @property
    def local_vault_names(self) -> tuple[str, ...] | None:
        """Local backends visible to home-vault search."""
        return vaults.parse_targets(self.app.globals.get("vault_search_targets", ""))

    @property
    def remote_vault_descriptors(self) -> list[vaults.VaultDescriptor]:
        """Search-capable remote backends selected in the home settings."""
        selected = vaults.parse_targets(
            self.app.globals.get("vault_search_targets", "")
        )
        descriptors = [
            descriptor
            for descriptor in vaults.configured_vaults(self.app.config)
            if descriptor.remote and descriptor.searchable and descriptor.enabled
        ]
        if selected is None:
            return descriptors
        wanted = {name.casefold() for name in selected}
        return [descriptor for descriptor in descriptors if descriptor.name.casefold() in wanted]

    def _search_target_signature_value(self) -> str:
        return str(self.app.globals.get("vault_search_targets", "") or "")

    @property
    def service(self) -> Service | None:
        """The service scope, exposed for the app's shared Add Keys action."""
        return self._service

    @property
    def key_candidate(self) -> str:
        """A complete pair already pasted into search, otherwise an empty form."""
        value = self.query_one("#query", Input).value.strip() if self.is_mounted else self._query
        return value if split_pair(value) else ""

    def compose(self) -> ComposeResult:
        yield Chrome(show_search=False, show_settings=False)
        yield Static("", id="masthead")
        with Horizontal(id="statusline"):
            yield StatusChip("add_keys", id="vault-add-chip")
            yield Static("·", classes="chip-sep")
            yield Static("", id="subhead", classes="pill chip-static")
        with Vertical(id="body"):
            yield Input(value=self._query, placeholder=self._placeholder(), id="query")
            yield OptionList(id="service-table")
        yield KeyBar(
            ("^n", "add keys"),
            ("enter", "open"),
            ("^y", "copy key"),
            ("^b", "back"),
            ("esc", "quit"),
        )

    def on_mount(self) -> None:
        self._search_target_signature = self._search_target_signature_value()
        self._render_chrome_copy()
        self.rebuild(self._query)
        self.query_one("#query", Input).focus()

    def relocalize(self) -> None:
        refresh_locale_widgets(self)
        self._render_chrome_copy()
        self.rebuild(self._needle)

    def _placeholder(self) -> str:
        return tr("search.placeholder_scoped") if self.scoped else tr("search.placeholder")

    def _render_chrome_copy(self) -> None:
        self.query_one("#vault-add-chip", StatusChip).update(tr("search.add_keys"))
        found = self.query("#query")
        if found:
            found.first(Input).placeholder = self._placeholder()
        self._render_masthead()

    def _render_masthead(self) -> None:
        if self.scoped:
            count = self.app.vaults.count_for_local(
                self.service_id,
                local_names=self.local_vault_names,
            )
            self.query_one("#masthead", Static).update(
                f"{tr('search.keys_title')}  [$dim]{self._service.NAME}[/]"
            )
            self.query_one("#subhead", Static).update(
                f"[$dim]{tr('search.subhead_scoped', count=count)}[/]"
            )
            return
        stats = self.app.vaults.stats_local(local_names=self.local_vault_names)
        remote_count = len(self.remote_vault_descriptors)
        remote_note = tr("search.subhead_remote", count=remote_count) if remote_count else ""
        self.query_one("#masthead", Static).update(tr("search.title"))
        self.query_one("#subhead", Static).update(
            f"[$dim]{tr('search.subhead', services=len(self.app.services), keys=stats['keys'])}"
            f"{remote_note}[/]"
        )

    def on_screen_resume(self, _event) -> None:
        signature = self._search_target_signature_value()
        if signature == self._search_target_signature:
            return
        self._search_target_signature = signature
        self._reset_remote_search()
        self._render_masthead()
        self.rebuild(self._needle)

    # ------------------------------------------------------------- classifying
    def rebuild(self, needle: str = "") -> None:
        option_list = self.query_one("#service-table", OptionList)
        option_list.clear_options()
        self._rows = []
        needle = needle.strip()
        if needle != self._remote_query and self._remote_busy:
            self._remote_busy = False
        if needle != self._remote_query:
            self._remote_records = []
            self._remote_status = ""
            self._remote_vault = ""
            self._remote_service = ""
        self._needle = needle

        # Inside a service the question is narrower: which key, in this service.
        if self.scoped:
            if needle:
                records = self.app.vaults.find_local(
                    needle,
                    service=self.service_id,
                    limit=60,
                    local_names=self.local_vault_names,
                )
            else:
                records = self.app.vaults.by_service_local(
                    self.service_id,
                    limit=60,
                    local_names=self.local_vault_names,
                )
            rows = [_Row("key", record=record) for record in records]
            self._add_section(option_list, "keys", rows)
            if not rows:
                message = tr("search.no_key_matches") if needle else tr("search.no_keys_yet")
                option_list.add_option(Option(f"  [$dim]{message}[/]", disabled=True))
            self._focus_first(option_list)
            return

        if not needle:
            self._add_section(option_list, "services", [
                _Row("service", service=service) for service in self.app.services
            ])
            self._focus_first(option_list)
            return

        # a URL routes straight to the owning service
        if needle.lower().startswith(("http://", "https://")):
            matched = self.app.registry.for_url(needle)
            rows = [_Row("service", service=matched)] if matched else []
            self._add_section(
                option_list,
                "url_match" if rows else "url_none",
                rows,
            )
            self._focus_first(option_list)
            return

        key_query = normalize_hex(needle) or split_pair(needle) or hex_ids(needle)
        key_rows = [
            _Row("key", record=record)
            for record in self.app.vaults.find_local(
                needle,
                limit=40,
                local_names=self.local_vault_names,
            )
        ] if self._vault_search_enabled else []

        # A query that names a hex id is unambiguous: it can only mean a key. That
        # includes one with a label still attached - "KID: 39a1…" is what a paste
        # out of a log looks like, and offering service names for it is noise.
        if key_query:
            self._add_remote_search_row(option_list, needle)
            if self._remote_records and self._remote_query == needle:
                self._add_section(
                    option_list,
                    "remote_result",
                    [_Row("key", record=record) for record in self._remote_records],
                    heading=tr(
                        "search.section.remote_result",
                        vault=f"{self._remote_vault}{self._remote_scope_note()}",
                    ),
                )
            self._add_section(option_list, "local_keys", key_rows)
            if (
                self._vault_search_enabled
                and not key_rows
                and not self._remote_records
            ):
                message = (
                    tr("search.no_local_key")
                    if self.remote_vault_descriptors
                    else tr("search.no_key_id")
                )
                option_list.add_option(Option(f"[dim]  {message}[/dim]", disabled=True))
            self._focus_first(option_list)
            return

        # First, because before "which service" the question is usually "who has
        # this at all", and with 155 services that is not something you can answer
        # by reading a list. Offered rather than run: a lookup is a request per
        # region, so it does not belong on a keystroke.
        self._add_section(option_list, "availability", [_Row("availability")])
        # Then one list of services, best answer first. Two sections - the ones
        # whose name matched, then the ones that merely support search - said the
        # same thing twice and put the service you named below a heading you were
        # not reading. Ranking carries that distinction now, and the row itself
        # says whether it will search or just open.
        lowered = needle.lower()
        # A service that cannot search is not something to search in, so it is
        # only here if the query named it - in which case opening it is still what
        # you asked for, and the row says "open" rather than "search".
        candidates = [
            service
            for service in self.app.services
            if service.SUPPORTS_SEARCH or _rank(service, lowered) < 3
        ]
        ordered = sorted(
            candidates, key=lambda service: (_rank(service, lowered), service.NAME.lower())
        )
        self._add_section(
            option_list,
            "search_in",
            [_Row("service", service=service) for service in ordered],
            heading=tr("search.section.search_in", query=visual_markup(needle)),
        )
        self._add_section(option_list, "local_keys", key_rows)
        self._focus_first(option_list)

    @property
    def _vault_search_enabled(self) -> bool:
        selected = vaults.parse_targets(
            self.app.globals.get("vault_search_targets", "")
        )
        return selected is None or bool(selected)

    def _add_remote_search_row(self, option_list: OptionList, needle: str) -> None:
        descriptors = self.remote_vault_descriptors
        if not self._vault_search_enabled:
            self._rows.append(_Row("header"))
            option_list.add_option(
                Option(f"  [$dim]{tr('search.vault_off')}[/]", disabled=True)
            )
            return
        if not descriptors:
            return
        name = self._remote_vault or ""
        self._rows.append(_Row("header"))
        self._rows.append(_Row("remote_search", vault_name=name))
        option_list.add_option(Option(f"  [$dim]{tr('search.remote_vault')}[/]", disabled=True))
        # Put this option directly in the list because _add_section would add a
        # second header, while this action must remain the first result.
        option_list.add_option(Option(self._remote_search_label(descriptors)))

    def _remote_search_label(self, descriptors: list[vaults.VaultDescriptor]) -> str:
        scope = self._remote_scope_note()
        if self._remote_busy:
            return f"    [$accent]{tr('search.searching_remote')}[/]  [$dim]{self._remote_vault}{scope}…[/]"
        if self._remote_status:
            return (
                f"    [$accent]{tr('search.search_remote_again')}[/]  "
                f"[$dim]{self._remote_vault}{scope} · {self._remote_status}[/]"
            )
        if len(descriptors) == 1:
            return f"    [$accent]{tr('search.search_remote')}[/]  [$dim]{descriptors[0].name}[/]"
        return (
            f"    [$accent]{tr('search.search_remote')}[/]  "
            f"[$dim]{tr('search.search_remote_n', count=len(descriptors))}[/]"
        )

    def _reset_remote_search(self) -> None:
        self._remote_query = ""
        self._remote_vault = ""
        self._remote_service = ""
        self._remote_records = []
        self._remote_status = ""
        self._remote_busy = False

    def _add_section(
        self,
        option_list: OptionList,
        section: str,
        rows: list[_Row],
        *,
        heading: str | None = None,
    ) -> None:
        """Draw one section, folded to :data:`SECTION_LIMIT` unless it was opened.

        The count goes in the heading whether it is folded or not, so "5 of 155"
        is visible without having to notice that a row is missing.
        """
        if not rows:
            return
        title = heading or tr(f"search.section.{section}")
        shown = len(rows) if section in self._expanded else min(SECTION_LIMIT, len(rows))
        self._rows.append(_Row("header"))
        count = (
            f"  [$gutter]{tr('search.n_of_n', shown=shown, total=len(rows))}[/]"
            if shown < len(rows)
            else ""
        )
        option_list.add_option(Option(f"  [$dim]{title}[/]{count}", disabled=True))
        for row in rows[:shown]:
            self._rows.append(row)
            option_list.add_option(Option(self._row_markup(row)))
        hidden = len(rows) - shown
        if hidden:
            row = _Row("more", section=section, hidden=hidden)
            self._rows.append(row)
            option_list.add_option(Option(self._row_markup(row)))

    def _row_markup(self, row: _Row) -> str:
        if row.kind == "more":
            return (
                f"    [$accent]{tr('search.load_more')}[/]  "
                f"[$gutter]{tr('search.more_n', count=row.hidden)}[/]"
            )
        if row.kind == "remote_search":
            return self._remote_search_label(self.remote_vault_descriptors)
        if row.kind == "availability":
            # both regions named, because they are two different settings: one
            # catalogue is searched for the title, then many are asked who has it
            searched = search_region_from(self.app.globals)
            regions = regions_from(self.app.globals)
            return (
                f"    {_column(tr('search.where_watch'))}"
                f"[$gutter]{tr('search.justwatch_checks', region=searched, count=len(regions))}[/]"
            )
        if row.kind == "service":
            service = row.service
            name = service.NAME.removesuffix(" (script)")
            what = tr("search.action.search") if service.SUPPORTS_SEARCH else tr("search.action.open")
            return f"    {_column(name)}[$gutter]{what}[/]"
        record = row.record
        # the title came off a network or out of a command file, so it is escaped
        # and put in visual order; the ids are hex and need neither
        title = visual_markup(record.title or tr("search.unknown_title"))
        provenance = record.created_at[:10]
        if record.source == "remote-search":
            title = tr("search.remote_result_title")
            provenance = f"{visual_markup(record.service)} · {visual_markup(record.origin or 'remote')}"
        if not self.scoped:
            provenance = (
                provenance
                if record.source == "remote-search"
                else f"{visual_markup(record.service)} · {provenance}"
            )
        return (
            f"    [$dim]{record.kid}[/][$gutter]:[/][$accent]{record.key}[/]\n"
            f"        [$dim]{title}[/]  [$gutter]{provenance}[/]"
        )

    @staticmethod
    def _focus_first(option_list: OptionList) -> None:
        for index in range(option_list.option_count):
            if not option_list.get_option_at_index(index).disabled:
                option_list.highlighted = index
                return

    # ------------------------------------------------------------------ events
    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        # a different question, folded again
        self._expanded.clear()
        if event.value.strip() != self._remote_query:
            self._reset_remote_search()
        self.rebuild(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        row = self._first_actionable()
        if row is not None:
            self._activate(row)
        else:
            self.query_one("#service-table", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        row = self._row_at(event.option_index)
        if row is not None:
            self._activate(row)

    def _row_at(self, index: int | None) -> _Row | None:
        if index is None or index >= len(self._rows):
            return None
        row = self._rows[index]
        return row if row.kind in _ACTIONABLE else None

    def _first_actionable(self) -> _Row | None:
        """What Enter in the field means.

        Normally the first row that does something, which is the availability
        lookup - for a title, "who has this" is the question. But a query that
        *names* a service exactly is not a title: "dsnp" is Disney+, and running a
        JustWatch search for the string "dsnp" finds nothing. So an exact match
        wins, wherever it sits in the list.

        "Load more" never wins: it is not an answer to what was typed.
        """
        needle = self._needle.strip().lower()
        rows = [row for row in self._rows if row.kind in _ACTIONABLE and row.kind != "more"]
        if needle:
            exact = next(
                (
                    row
                    for row in rows
                    if row.kind == "service"
                    and row.service is not None
                    and _rank(row.service, needle) == 0
                ),
                None,
            )
            if exact is not None:
                return exact
        return next(iter(rows), None)

    def _expand(self, section: str) -> None:
        """Show the rest of one section, leaving the cursor where the row was."""
        option_list = self.query_one("#service-table", OptionList)
        position = option_list.highlighted
        self._expanded.add(section)
        self.rebuild(self._needle)
        if position is not None and position < option_list.option_count:
            # the first row that was behind the fold is now where the fold was
            option_list.highlighted = position

    def _remote_query_kids(self) -> tuple[str, ...]:
        needle = self.query_one("#query", Input).value.strip()
        pair = split_pair(needle)
        if pair:
            kid = pair[0]
        else:
            kid = normalize_hex(needle)
            if kid is None:
                named = hex_ids(needle)
                kid = named[0] if named else None
        if not kid:
            return ()
        alias = playready_kid_alias(kid)
        return tuple(dict.fromkeys(value for value in (kid, alias) if value))

    def _activate_remote_search(self) -> None:
        if self._remote_busy:
            return
        descriptors = self.remote_vault_descriptors
        kids = self._remote_query_kids()
        if not descriptors or not kids:
            return
        if len(descriptors) == 1:
            self._choose_remote_scope(descriptors[0].name, kids)
            return

        def picked(name: str | None) -> None:
            if name:
                self._choose_remote_scope(name, kids)

        self.app.push_screen(VaultSearchPicker(descriptors), picked)

    def _remote_scope_note(self) -> str:
        if self._remote_service:
            return f" · {self._remote_service}"
        return f" · {tr('search.all_platforms')}" if self._remote_vault else ""

    def _remote_service_map(self, name: str) -> dict[str, str]:
        """The selected backend's local-id to remote-tag aliases, if any."""
        for spec in self.app.config.vault_specs():
            if str(spec.get("name") or "").strip().casefold() != name.casefold():
                continue
            raw = spec.get("service_map")
            if not isinstance(raw, dict):
                return {}
            return {
                str(source).strip().casefold(): str(target).strip().casefold()
                for source, target in raw.items()
                if str(source).strip() and str(target).strip()
            }
        return {}

    def _remote_service_options(
        self,
        name: str,
        services: tuple[str, ...],
    ) -> list[tuple[str, str]]:
        """Make remote service tags readable without inventing new namespaces."""
        mapping = self._remote_service_map(name)
        names: dict[str, list[str]] = {service: [] for service in services}
        for candidate in self.app.services:
            for local_id in (candidate.ID, *candidate.ALIASES):
                tag = mapping.get(local_id.casefold(), local_id.casefold())
                if tag in names and candidate.NAME not in names[tag]:
                    names[tag].append(candidate.NAME)
        options: list[tuple[str, str]] = []
        for service in services:
            matches = names[service]
            label = matches[0] if matches else service
            if len(matches) > 1:
                label = f"{label} + {len(matches) - 1}"
            options.append((service, label))
        return options

    def _choose_remote_scope(self, name: str, kids: tuple[str, ...]) -> None:
        scopes = self.app.vaults.remote_search_services(name)
        if not scopes:
            self._begin_remote_search(name, kids)
            return
        if len(scopes) == 1:
            # "All" and this one tag issue the identical request, so there is
            # no decision or load reduction to ask the user to make.
            self._begin_remote_search(name, kids, service=scopes[0])
            return

        def picked(service: str | None) -> None:
            if service is not None:
                self._begin_remote_search(name, kids, service=service or None)

        self.app.push_screen(
            VaultServicePicker(name, self._remote_service_options(name, scopes)),
            picked,
        )

    def _begin_remote_search(
        self,
        name: str,
        kids: tuple[str, ...],
        *,
        service: str | None = None,
    ) -> None:
        query = self.query_one("#query", Input).value.strip()
        self._remote_query = query
        self._remote_vault = name
        self._remote_service = service or ""
        self._remote_records = []
        self._remote_status = ""
        self._remote_busy = True
        self.rebuild(query)
        self._search_remote(name, kids, query, service)

    @work(thread=True, exclusive=True, group="remote-vault-search")
    def _search_remote(
        self,
        name: str,
        kids: tuple[str, ...],
        query: str,
        service: str | None,
    ) -> None:
        try:
            found = self.app.vaults.search_remote(kids, name=name, service=service)
        except Exception as exc:  # noqa: BLE001 - backend failures belong in the result row
            message = " ".join(str(exc).split())[:160] or tr("search.remote_failed")
            self.app.call_from_thread(
                self._finish_remote_search,
                name,
                service,
                query,
                [],
                message,
            )
            return
        self.app.call_from_thread(
            self._finish_remote_search,
            name,
            service,
            query,
            found,
            "",
        )

    def _finish_remote_search(
        self,
        name: str,
        service: str | None,
        query: str,
        found: list[vaults.VaultSearchResult],
        error: str,
    ) -> None:
        # A slow answer for an old input must never appear under a new KID.
        if query != self.query_one("#query", Input).value.strip():
            return
        self._remote_query = query
        self._remote_vault = name
        self._remote_service = service or ""
        self._remote_busy = False
        self._remote_records = [
            KeyRecord(
                result.kid,
                result.key,
                result.service,
                title=None,
                source="remote-search",
                origin=result.vault,
            )
            for result in found
        ]
        self._remote_status = error or (tr("search.no_key_found") if not found else "")
        self.rebuild(query)

    def _activate(self, row: _Row) -> None:
        if row.kind == "more":
            self._expand(row.section)
            return
        if row.kind == "remote_search":
            self._activate_remote_search()
            return
        if row.kind == "availability":
            query = self.query_one("#query", Input).value.strip()
            if query:
                self.app.push_screen(JustWatchScreen(query))
            return
        if row.kind == "service" and row.service is not None:
            query = self.query_one("#query", Input).value.strip()
            self.app.pop_screen()
            if query.lower().startswith(("http://", "https://")):
                self.app.open_service(row.service, target=query)
            elif row.service.SUPPORTS_SEARCH and query and not normalize_hex(query):
                self.app.open_service(row.service, search=query)
            else:
                self.app.open_service(row.service)
            return
        if row.kind == "key" and row.record is not None:
            self._copy(row.record)

    def action_copy_key(self) -> None:
        row = self._row_at(self.query_one("#service-table", OptionList).highlighted)
        if row is not None and row.kind == "key" and row.record is not None:
            self._copy(row.record)

    def action_add_keys(self) -> None:
        self.app.action_add_keys()

    def _copy(self, record: KeyRecord) -> None:
        detail = f"{record.service} · {record.title or tr('search.unknown_title')}"
        try:
            # App.copy_text, not copy_to_clipboard: the latter is OSC 52 only,
            # which several macOS terminals ignore, so this said "Copied" and
            # left the clipboard untouched. copy_text also goes through pbcopy.
            self.app.copy_text(record.pair())
            self.notify(f"{tr('search.copied', pair=record.pair())}\n{detail}", timeout=4)
        except Exception:
            self.notify(
                f"{record.pair()}\n{detail}",
                title=tr("search.notify.key_title"),
                timeout=8,
            )

    def go_back(self) -> bool:
        return False
