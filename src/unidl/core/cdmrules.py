"""Which CDM answers a licence request, decided by the resolution being taken.

One device is rarely the right answer for everything. An L1 is what a service
hands 1080p and above to, and it is also the device with a request budget, a
provisioning that gets revoked, and a queue in front of it when it is remote. An
L3 opens the rest without spending any of that. Choosing between them by hand
before every title is a step nobody remembers to take, and forgetting it is not
visible until a 2160p licence comes back refused.

So the choice is stated once, as rules: *1080p and above uses this device,
below that uses that one*. At licence time the resolution actually being taken
is already known - it is the number that goes into the file name - and the rule
that matches decides.

The rules are built by picking, never typed: see :mod:`unidl.tui.cdm_rules_screen`.
What is stored is the canonical text below, one rule per line, because a settings
file somebody opens should be readable:

    >=1080  alpha_l1
    <1080   beta_l3

Ordering is not the order they were added. They are kept and shown in the order
they will be tried - exact matches first, then the ``>=`` rules from the highest
threshold down, then the ``<`` rules from the lowest up - so "first match wins" is
something you can see on the screen rather than a rule about a rule.

A rule naming a device for another DRM system is skipped rather than tried: a
``.prd`` cannot answer a Widevine challenge, and skipping is what makes "the L1 for
HD" and "this PlayReady endpoint for HD" two rules that coexist instead of two
rules that fight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import naming

#: Comparisons a rule can make, longest first: ``>=`` has to be recognised before
#: ``>`` or the threshold is parsed as ``=1080``.
OPS = (">=", "<=", ">", "<", "=")

#: "whatever the resolution is". Its own comparison rather than a threshold low
#: enough to always pass, because this is the rule that expresses the other half of
#: the problem: *this* device for Widevine and *that* one for PlayReady, at every
#: quality. A rule for the other system is skipped, so two of these coexist and
#: each system takes its own - which is not something a number can say.
ANY = "*"

#: The thresholds the picker offers. Standard release heights, which is what
#: :func:`unidl.core.naming.quality_of` reports and what the file name carries -
#: the two agree by construction, so a rule about "1080p" fires on exactly the
#: titles whose name says ``1080p``.
HEIGHTS = (2160, 1440, 1080, 720, 480)

_RULE_RE = re.compile(r"^\s*(>=|<=|=|>|<)?\s*(\d{2,5})\s*[pP]?\s+(.+?)\s*$")
_ANY_RE = re.compile(r"^\s*(?:any|\*)\s+(.+?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    """One comparison and the device that answers when it holds."""

    op: str
    height: int
    device: str

    def matches(self, height: int) -> bool:
        if self.op == ANY:
            # including a manifest that never said: "any resolution" is a statement
            # about the device, and making it depend on reading a number would put
            # the one rule that cannot be wrong at the mercy of a missing attribute
            return True
        if height <= 0:
            return False
        if self.op == ">=":
            return height >= self.height
        if self.op == ">":
            return height > self.height
        if self.op == "<=":
            return height <= self.height
        if self.op == "<":
            return height < self.height
        return height == self.height

    def label(self) -> str:
        """The comparison in words, for a screen rather than a config file."""
        return condition_label(self.op, self.height)

    def as_text(self) -> str:
        """The stored form. Two spaces, so the device name starts a column."""
        if self.op == ANY:
            return f"any  {self.device}"
        return f"{self.op}{self.height}  {self.device}"


def condition_label(op: str, height: int) -> str:
    """``">=", 1080`` as "1080p and above"."""
    if op == ANY:
        return "any resolution"
    if op == ">=":
        return f"{height}p and above"
    if op == ">":
        return f"above {height}p"
    if op == "<=":
        return f"{height}p and below"
    if op == "<":
        return f"below {height}p"
    return f"exactly {height}p"


def condition_text(op: str, height: int) -> str:
    """The comparison as :func:`parse_line` accepts it: ``"any"`` or ``">=1080"``.

    So that a screen offering conditions can hand one back as a token and let this
    module be the only thing that knows the syntax.
    """
    return "any" if op == ANY else f"{op}{height}"


def conditions() -> list[tuple[str, int]]:
    """Every comparison the picker offers, in the order it offers them.

    "Any resolution" leads, because "this device for Widevine, that one for
    PlayReady" is the simplest thing anyone comes here to say and it needs no
    number at all. Then the thresholds, highest first and downwards, because that
    is the order the thing being protected is thought about: the top of the ladder
    is what needs the good device. ``>=`` before ``<`` before ``=`` for the same
    reason - "and above" is the rule people actually want, and an exact height is
    the special case.
    """
    rows: list[tuple[str, int]] = [(ANY, 0)]
    rows += [(">=", height) for height in HEIGHTS]
    rows += [("<", height) for height in HEIGHTS]
    rows += [("=", height) for height in HEIGHTS]
    return rows


def parse(value: Any) -> list[Rule]:
    """The rules in ``value``, in the order they will be tried.

    Unparseable lines are dropped rather than raised on: this value can be edited
    by hand in settings.json, and a typo there must not stop the app from starting
    a download. :func:`problems` is what reports them, on the screen that owns them.
    """
    rules: list[Rule] = []
    for line in str(value or "").splitlines():
        rule = parse_line(line)
        if rule is not None:
            rules.append(rule)
    return order(rules)


def parse_line(line: str) -> Rule | None:
    """One line as a :class:`Rule`, or ``None`` if it is not one.

    The device name is the whole rest of the line. Real device files are called
    things like ``[L3] OnePlus, CPH2493`` - a separator narrower than "end of
    line" would cut half of them in two.
    """
    catch_all = _ANY_RE.match(str(line or ""))
    if catch_all:
        device = catch_all.group(1).strip()
        return Rule(ANY, 0, device) if device else None
    match = _RULE_RE.match(str(line or ""))
    if not match:
        return None
    op, height, device = match.groups()
    device = device.strip()
    if not device:
        return None
    # A bare number means that exact height, the way a digit key does in the
    # config file this borrows its vocabulary from.
    return Rule(op or "=", int(height), device)


def dump(rules: Iterable[Rule]) -> str:
    """The canonical stored text for ``rules``, in evaluation order."""
    return "\n".join(rule.as_text() for rule in order(rules))


def order(rules: Iterable[Rule]) -> list[Rule]:
    """``rules`` in the order they are tried: most specific first.

    Exact heights lead, because a rule about one height is a statement about that
    height and nothing else. Then the upper bounds from the top down, so
    ``>=2160`` is consulted before ``>=1080`` and a 4K stream does not match the
    1080p rule first. Then the lower bounds from the bottom up, for the mirror
    reason. "Any resolution" is last, because a catch-all that came first would be
    the only rule that ever ran. Ties keep the order they arrived in.
    """
    def sort_key(pair: tuple[int, Rule]) -> tuple[int, int, int]:
        index, rule = pair
        if rule.op == "=":
            return (0, -rule.height, index)
        if rule.op in (">=", ">"):
            return (1, -rule.height, index)
        if rule.op == ANY:
            return (3, 0, index)
        return (2, rule.height, index)

    return [rule for _, rule in sorted(enumerate(rules), key=sort_key)]


def choose(
    rules: Iterable[Rule],
    height: int,
    *,
    system: str = "",
    systems: dict[str, str] | None = None,
) -> Rule | None:
    """The first rule that matches ``height`` and can serve ``system``.

    A rule whose device is known to belong to another DRM system is skipped: it
    could not answer this challenge, and skipping is what lets one Widevine rule
    and one PlayReady rule sit at the same threshold.

    A device this install cannot see is *not* skipped. It may be a name declared
    in unidl.yaml that lives outside the search paths, and the resolver downstream
    knows how to look for those - while guessing "gone" here would silently ignore
    a rule the user can see on the screen.
    """
    known = systems or {}
    for rule in order(rules):
        if system:
            owner = known.get(rule.device, "")
            if owner and owner != system:
                continue
        if rule.matches(height):
            return rule
    return None


def height_of(streams: object) -> int:
    """The release height of the best video among ``streams``, in scan lines.

    Deliberately the same number the file name is built from: a rule that fired on
    a different reading of the same manifest than the name shows would be
    impossible to check by looking.
    """
    tag = naming.quality_of(streams)
    digits = tag[:-1] if tag.endswith("p") else tag
    return int(digits) if digits.isdigit() else 0


def known_devices(config: Any) -> list[Any]:
    """Every CDM this install can see, local and remote, as ``DeviceFile`` rows.

    Read live rather than cached, for the same reason the picker reads the folder
    live: a device dropped in five minutes ago has to be choosable, and one that
    was deleted has to stop being.

    Order is declared-in-config first, then the search paths, then the remote
    endpoints, deduplicated by name - which is the order a name resolves in, so the
    row shown for a name is the device that name will reach.
    """
    if config is None:
        return []
    from .drm import DeviceFile, discover, remote_devices
    from .drm import system_of as _system_of

    try:
        declared = [
            DeviceFile(path=path, system=_system_of(path), origin=str(path.parent))
            for path in config.devices.values()
        ]
        rows = [*declared, *discover(config.cdm_roots()), *remote_devices(config.remote_cdms)]
    except Exception:  # noqa: BLE001 - a bad search path must not close a screen
        return []
    found: list[Any] = []
    seen: set[str] = set()
    for device in rows:
        if device.name in seen:
            continue
        seen.add(device.name)
        found.append(device)
    return found


def device_systems(config: Any) -> dict[str, str]:
    """``{device name: drm system id}`` for everything this install can see."""
    return {device.name: device.system for device in known_devices(config)}


def slot(rule: Rule, systems: dict[str, str] | None = None) -> tuple[str, int, str]:
    """What makes a rule unique: its comparison *and* the system it can serve.

    Two rules at one threshold are not a mistake when they name devices for
    different DRM systems - that is exactly how "the L1 for HD, and this PlayReady
    endpoint for HD" is written, and each is reached by the exchange it belongs to.
    Two at one threshold for the *same* system are a mistake, because only the
    first can ever run.
    """
    return (rule.op, rule.height, (systems or {}).get(rule.device, ""))


def problems(
    rules: Iterable[Rule],
    names: Iterable[str],
    systems: dict[str, str] | None = None,
) -> list[str]:
    """What is wrong with ``rules``, given the devices that exist.

    Reported rather than corrected. A rule pointing at a device that is not
    plugged in today is still the rule the user meant tomorrow, and deleting it
    for them is the one outcome they cannot undo.
    """
    known = set(names)
    found: list[str] = []
    seen: set[tuple[str, int, str]] = set()
    for rule in order(rules):
        if rule.device not in known:
            found.append(f"{rule.label()}: no device called {rule.device} was found")
        where = slot(rule, systems)
        if where in seen:
            found.append(f"{rule.label()}: already answered by a rule above it")
        seen.add(where)
    return found


def summary(rules: Iterable[Rule]) -> str:
    """One line for a settings row: how many, and what the first one says."""
    ordered = order(rules)
    if not ordered:
        return "none - one device for everything"
    first = ordered[0]
    if len(ordered) == 1:
        return f"{first.label()} -> {first.device}"
    return f"{len(ordered)} rules · {first.label()} -> {first.device} first"


__all__ = [
    "ANY",
    "HEIGHTS",
    "OPS",
    "Rule",
    "choose",
    "condition_label",
    "condition_text",
    "conditions",
    "device_systems",
    "dump",
    "height_of",
    "known_devices",
    "order",
    "parse",
    "parse_line",
    "problems",
    "slot",
    "summary",
]
