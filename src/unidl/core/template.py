"""Name templates: ``{title}.{season_episode?}.{quality?}.{platform?}``.

The shape of a file name used to be code. That was fine while there was one right
answer, and stopped being fine the moment somebody wanted the dynamic range before
the platform tag rather than after it - a preference no amount of argument settles,
because both orders are in use.

So the shape is a string now, and this renders it. Two rules, both borrowed from
unshackle's ``output_template`` because they are the two that matter:

* ``{name}`` is substituted, and an unknown name is left alone rather than raising:
  a template is typed by hand, and a typo should produce a visibly odd file name,
  not a session that dies at the last step.
* ``{name?}`` is *optional*. When its value is empty the field goes, and so does
  one separator beside it - which is what stops ``Show..S01E02`` and a trailing dot
  on every film that has no dynamic range.

Rendering is deliberately pure and knows nothing about titles or tracks: the caller
decides what the variables mean, which is what lets the same renderer produce the
title half of a name early and the release half later.
"""

from __future__ import annotations

import re

#: ``{name}`` or ``{name?}``
_FIELD = re.compile(r"\{(?P<name>[a-z_][a-z0-9_]*)(?P<optional>\?)?\}")

#: What a separator can be. A run of them collapses to the first.
SEPARATORS = ".-_ "

#: Characters a file name cannot hold, whatever the platform. Kept as a set of what
#: is refused rather than a whitelist: a template is allowed to contain a bracket or
#: a comma, and only the genuinely impossible is a problem.
ILLEGAL = set('/\\:*?"<>|')


def variables(template: str) -> list[str]:
    """Every field name in ``template``, in order, without duplicates."""
    seen: list[str] = []
    for match in _FIELD.finditer(str(template or "")):
        name = match.group("name")
        if name not in seen:
            seen.append(name)
    return seen


def validate(template: str, allowed: set[str] | frozenset[str]) -> list[str]:
    """Everything wrong with ``template``, as sentences. Empty means it is fine."""
    text = str(template or "")
    problems: list[str] = []
    if not text.strip():
        problems.append("it is empty, so there would be no file name")
        return problems
    unknown = [name for name in variables(text) if name not in allowed]
    if unknown:
        known = ", ".join(sorted(allowed))
        problems.append(
            f"{', '.join('{' + name + '}' for name in unknown)} "
            f"{'is not a name' if len(unknown) == 1 else 'are not names'} this can fill in. "
            f"Available: {known}"
        )
    # the fields taken out first: the "?" that marks one optional is part of the
    # syntax, and complaining about it made every correct template look wrong
    literal = _FIELD.sub("", text)
    bad = sorted(ILLEGAL & set(literal))
    if bad:
        problems.append(f"{' '.join(bad)} cannot appear in a file name")
    if not variables(text):
        problems.append("it has no {fields} in it, so every file would have the same name")
    return problems


def render(template: str, values: dict[str, object]) -> str:
    """``template`` with ``values`` put in, and empty optional fields taken out.

    The separator rule is the fiddly half. An optional field that is empty takes
    with it the separator on its right if it has one, otherwise the separator on its
    left - so ``a.{b?}.c`` becomes ``a.c`` and ``a.{b?}`` becomes ``a``, rather than
    ``a..c`` and ``a.``.
    """
    text = str(template or "")
    out: list[str] = []
    position = 0
    for match in _FIELD.finditer(text):
        out.append(text[position : match.start()])
        position = match.end()
        name = match.group("name")
        if name not in values:
            # not a field this caller knows: leave it as written, so a typo shows up
            # in the name instead of vanishing silently
            out.append(match.group(0))
            continue
        value = _clean(values.get(name))
        if value:
            out.append(value)
            continue
        if not match.group("optional"):
            # a required field with nothing in it collapses like an optional one;
            # the alternative is a file called "Show..2160p"
            continue
        # Empty and optional: it takes one separator with it, and which one is not
        # arbitrary. The one on its *left* belongs to it - it is what joined this
        # field to the one before - while the one on its right belongs to the field
        # after. So `{source}.{range?}-{tag?}` with no range reads `WEB-DL-GROUP`,
        # keeping the dash the group is written with; taking the right-hand one
        # instead produced `WEB-DL.GROUP`. The right one is the fallback, for a
        # field at the very start of a template that has nothing to its left.
        if out and out[-1] and out[-1][-1] in SEPARATORS:
            out[-1] = out[-1][:-1]
        elif position < len(text) and text[position] in SEPARATORS:
            position += 1
    out.append(text[position:])
    return tidy("".join(out))


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def tidy(text: str) -> str:
    """Collapse repeated separators and trim them off both ends.

    A safety net rather than the mechanism: the separator rule above handles the
    cases it can see, and this catches what two adjacent empty fields leave behind.
    """
    out = text
    for separator in SEPARATORS:
        doubled = separator * 2
        while doubled in out:
            out = out.replace(doubled, separator)
    return out.strip(SEPARATORS)


__all__ = ["ILLEGAL", "SEPARATORS", "render", "tidy", "validate", "variables"]
