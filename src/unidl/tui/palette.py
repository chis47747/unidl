"""Command palette.

With 152 services plus three tiers of settings, a searchable action list beats
memorising shortcuts and surfaces every shortcut and command behind
``Ctrl+P`` / ``?``.
"""

from __future__ import annotations

from functools import partial

from textual.command import DiscoveryHit, Hit, Hits, Provider

from ..core.i18n import tr


class UnidlCommands(Provider):
    """Services, global actions and settings, all searchable."""

    def _entries(self) -> list[tuple[str, str, object]]:
        app = self.app
        order = ["dark", "light"]
        current = app.theme_preference
        nxt = order[(order.index(current) + 1) % len(order)]
        entries: list[tuple[str, str, object]] = [
            (tr("palette.search"), tr("palette.search_help"), app.action_global_search),
            (
                tr("palette.add_keys"),
                tr("palette.add_keys_help"),
                app.action_add_keys,
            ),
            (tr("palette.settings"), tr("palette.settings_help"), app.action_global_settings),
            (
                tr("palette.theme"),
                tr("palette.theme_help", current=current, following="", next=nxt),
                app.action_toggle_theme,
            ),
            (tr("palette.back"), tr("palette.back_help"), app.action_global_back),
            (tr("palette.quit"), tr("palette.quit_help"), app.action_global_quit),
        ]
        for service in app.services:
            features = ", ".join(
                name
                for name, on in (
                    ("vod", service.SUPPORTS_URL),
                    ("live", service.SUPPORTS_LIVE),
                    ("search", service.SUPPORTS_SEARCH),
                )
                if on
            )
            entries.append(
                (
                    tr("palette.open", name=service.NAME),
                    f"{service.ID} · {features}",
                    partial(app.open_service, service),
                )
            )
        return entries

    async def discover(self) -> Hits:
        for title, help_text, runnable in self._entries():
            yield DiscoveryHit(title, runnable, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for title, help_text, runnable in self._entries():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title), runnable, help=help_text)
