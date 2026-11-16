"""Retain Channel 4 pages while their child browse or delivery is active."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import requests

from ...core.flow import Ask, Back, FlowContext, Pick

FLOW_ERRORS = (RuntimeError, requests.RequestException, OSError)


def browse_page(
    ctx: FlowContext, page: Ask, visit: Callable[[Any], Iterator[Ask]],
) -> Iterator[Ask]:
    while True:
        selected = yield page
        if selected is None or selected == []:
            return
        if isinstance(page, Pick):
            values = selected if page.multi and isinstance(selected, (list, tuple, set)) else [selected]
            indexes = [index for index, choice in enumerate(page.choices) if choice.value in values]
            if indexes:
                page.cursor = indexes[0]
                if page.multi:
                    page.preselected = indexes
        try:
            yield from visit(selected)
        except Back:
            pass
        except FLOW_ERRORS as exc:
            ctx.error(str(exc))
        if not ctx.interactive:
            return


def browse_input(
    ctx: FlowContext,
    title: str,
    visit: Callable[[FlowContext, str], Iterator[Ask]],
    initial: str | None = None,
) -> Iterator[Ask]:
    page = ctx.text(title)
    value = initial
    while True:
        if value is None:
            value = yield page
        wanted = str(value or "").strip()
        if not wanted:
            return
        page.default = wanted
        try:
            yield from visit(ctx, wanted)
        except Back:
            pass
        except FLOW_ERRORS as exc:
            ctx.error(str(exc))
        if not ctx.interactive:
            return
        value = None
