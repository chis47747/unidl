"""The Flow protocol - the only interface between a service and the UI.

A service flow is a generator. It yields *asks* ("show the user this list") and
receives answers back. It never imports a UI framework, never prints, never
calls ``input()``. This keeps 150+ services free of UI concerns, makes them
unit-testable by feeding scripted answers, and lets the same flow run
non-interactively by auto-answering.

    def browse(self, ctx):
        seasons = self.api.seasons(show_id)
        picked = yield ctx.pick("Season", [Choice(s.label, s) for s in seasons])
        for ep in self.api.episodes(picked):
            yield ctx.emit(self.get_playback(ep))

Navigation mirrors the old scripts' ``BackRequest`` / ``QuitRequest``: the
driver throws :class:`Back` or :class:`Quit` into the generator, so a flow can
catch ``Back`` to pop up one level or let it bubble out to exit the flow.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .partner import PartnerAuthorization, PartnerAuthorizationError
from .qr import QrPresentation


class Back(Exception):
    """User asked to go back one step."""


class Quit(Exception):
    """User asked to leave the flow entirely."""


@dataclass
class Choice:
    label: str
    value: Any = None
    detail: str = ""
    tags: tuple[str, ...] = ()
    #: Optional semantic TUI palette role (``accent``, ``manifest``, ``warn``...).
    #: Core stores the hint but does not interpret it, keeping services and flows
    #: independent from concrete terminal colours.
    style: str = ""
    disabled: bool = False
    #: Picking this moves the list rather than selecting anything - a "next page"
    #: entry. Worth saying out loud, because the driver counts a multi-select's
    #: answer to work out how big a batch is about to happen, and an entry that
    #: is not a title made that count wrong for the whole run.
    navigates: bool = False
    #: ``(text, semantic palette role)`` spans to emphasise inside the label.
    #: Unlike ``style``, these never colour the whole row. Track selection uses
    #: this for the few format badges that carry useful visual meaning while
    #: ordinary SDR and encryption text stays in the normal foreground colour.
    highlights: tuple[tuple[str, str], ...] = ()


# --------------------------------------------------------------------------- scope

#: Which screen an ask belongs on. The UI has four, each answering one question,
#: and an ask says which of them it is part of rather than the UI guessing from
#: nesting depth. See docs/ui-design.md.
SCOPE_ROOT = "root"
"""The service's own top menu - screen 2."""

SCOPE_FLOW = "flow"
"""The service doing its work: titles, seasons, channels - screen 3."""

SCOPE_DELIVERY = "delivery"
"""Track choice and download-or-export - screen 4."""


# --------------------------------------------------------------------------- asks


@dataclass
class Ask:
    """Base for anything that needs an answer from the user."""

    title: str = ""
    scope: str = SCOPE_FLOW


@dataclass
class Pick(Ask):
    choices: list[Choice] = field(default_factory=list)
    multi: bool = False
    hint: str = ""
    #: index of the choice to start the cursor on
    cursor: int = 0
    #: indexes checked up front (multi only), e.g. the auto-selected tracks
    preselected: list[int] = field(default_factory=list)
    #: Optional service/API chapters shown next to a track-picker title.
    #: Empty means the badge is omitted; Core does not fetch this itself.
    chapters: tuple[Any, ...] = ()
    #: Optional lyrics model shown from the delivery picker with the ``l`` key.
    lyrics: Any = None
    #: Optional presentation-only context for the picker.  The flow contract
    #: keeps this opaque so Core does not learn about album art or other UI
    #: details; the TUI may use it when a delivery type has a richer layout.
    preview: Any = None


class ManualLines:
    """Rows describing something the user has to do by hand, somewhere else.

    Shared by the two asks that can carry them, because the two halves of a manual
    step are the same content with a different ending: a device code is confirmed
    elsewhere and polled for, an authorisation address is pasted back here. Either
    way what goes on the screen is a code, a link, a name to look for - and the UI
    has to put it where it can be read and copied rather than in the log.

    ``lines`` is declared by each ask (they have different defaults and orders), so
    it is deliberately not annotated here.
    """

    @property
    def actions(self) -> list[tuple[str, str]]:
        """The ``(label, value)`` entries, in the order the service gave them."""
        found: list[tuple[str, str]] = []
        for item in self.lines:  # type: ignore[attr-defined]
            if isinstance(item, tuple) and len(item) == 2:
                found.append((str(item[0]), str(item[1])))
        return found

    def as_text(self) -> list[str]:
        """Every entry flattened to one string, for the log and for scripts."""
        return [
            f"{item[0]}:  {item[1]}" if isinstance(item, tuple) and len(item) == 2 else str(item)
            for item in self.lines  # type: ignore[attr-defined]
        ]


@dataclass
class Panel(ManualLines):
    """Something finished, put where it can be read and copied off the screen.

    Deliberately **not** an :class:`Ask`. The two are easy to confuse because they
    look the same - a bordered card in the middle of the screen with a value on
    its own row - but they end differently: an ask stops the flow until it is
    answered, and this does not stop it at all. That distinction is the whole
    reason this type exists. The one artefact that needs it is the UniDL command
    in "save the command only" mode, and ten titles picked at once must produce ten
    commands without stopping ten times to have each one dismissed.

    Same ``lines`` shape as the manual asks, so the same renderer draws it and the
    same keys copy out of it.
    """

    title: str = ""
    lines: list[str | tuple[str, str]] = field(default_factory=list)
    #: one dim row under the values: what this is, and how to move on from it
    hint: str = ""


@dataclass
class TextAsk(ManualLines, Ask):
    """One field to type into, and optionally what to go and do first.

    ``lines`` is for a sign-in that is finished in a browser and pasted back: the
    address to open belongs on the screen next to the field, not in the log.
    """

    placeholder: str = ""
    default: str = ""
    password: bool = False
    lines: list[str | tuple[str, str]] = field(default_factory=list)


@dataclass
class Field:
    """One value in a :class:`Form`."""

    key: str
    label: str
    placeholder: str = ""
    default: str = ""
    #: masked while typing, and revealable on the screen that draws it
    password: bool = False


@dataclass
class Form(ManualLines, Ask):
    """Several values at once, answered as a ``{key: value}`` dict.

    For the login that is a username and a password: two things that are one
    thought, and asking them as two consecutive prompts made the second one
    arrive after the first had scrolled away. Drawn as one card in the middle of
    the screen, like every other step a person has to do something about.

    ``lines`` is the same shape as :class:`Await`'s - prose for context, and
    ``(label, value)`` pairs for anything that has to be acted on first.
    """

    fields: list[Field] = field(default_factory=list)
    lines: list[str | tuple[str, str]] = field(default_factory=list)
    hint: str = ""


@dataclass
class Confirm(Ask):
    default: bool = True


@dataclass
class TableAsk(Ask):
    """Grid selection of exactly one row, used for EPG / channel listings.

    Single-select on purpose. There was a ``multi`` flag here that only ever
    changed the hint line - the renderer answered with one value either way, and
    a row cursor has nothing for space to toggle - so it advertised something
    that did not work. If a grid ever needs multi-select it wants a real
    implementation, not a flag.
    """

    columns: list[str] = field(default_factory=list)
    rows: list[Sequence[str]] = field(default_factory=list)
    values: list[Any] = field(default_factory=list)
    row_styles: list[str] = field(default_factory=list)


@dataclass
class Await(ManualLines, Ask):
    """Show something, then wait for it to be confirmed somewhere else.

    Every device-code sign-in has this shape: a code and a URL are displayed, the
    person goes and enters them on a phone or a TV, and the only way to know it
    happened is to keep asking. Nothing is typed here, so it is not a question -
    but it is a point the flow stops at, which is why it is an ask.

    ``poll`` is called repeatedly on the flow's own thread until it returns
    something other than ``None``, which becomes the answer. Back or a timeout
    raises :class:`Back`, so a sign-in the user gave up on lands wherever backing
    out normally would.

    Each entry in ``lines`` is either prose or a ``(label, value)`` pair, and the
    difference is what the UI leads with. A pair is something the person has to
    *act on* - a code to type, a link to open, a name to pick out of a list - so
    it gets the middle of the screen and a way to copy it. Prose is the context
    around it. Which is which is the service's call: this is not only about
    device codes, and a service that needs a person to do something else entirely
    says so by naming its own labels.
    """

    lines: list[str | tuple[str, str]] = field(default_factory=list)
    #: called every ``interval`` seconds; a non-None return is the answer
    poll: Any = None
    timeout: float = 180.0
    interval: float = 3.0
    hint: str = ""
    #: shown while waiting, with the remaining seconds appended
    waiting_note: str = "waiting for confirmation"
    #: Optional image/payload rendered as a QR above the manual action rows.
    #: Kept separate so a base64 image never enters logs, copy actions or markup.
    qr: QrPresentation | None = None


@dataclass
class SettingsAsk(Ask):
    """Open the service's settings panel mid-flow."""

    fields: list[Any] = field(default_factory=list)


@dataclass
class Suspend(Ask):
    """Run something with the terminal handed back to it.

    Needed by the legacy runner: an unported script owns stdin/stdout and draws
    its own menus, which cannot happen inside a full-screen app. The presenter
    drops out of application mode, runs ``work``, then restores the UI.
    """

    work: Any = None  # Callable[[], Any]
    note: str = ""


@dataclass
class Emit(Ask):
    """Not a question: hand a finished Playback to the driver."""

    playback: Any = None


@dataclass
class PartnerHandoff(Ask):
    """Deliver a one-time authorization to its target service without rendering it."""

    authorization: PartnerAuthorization | None = None


# --------------------------------------------------------------------------- ctx


class Sink(Protocol):
    def log(self, message: str, level: str = "info") -> None: ...
    def status(self, message: str) -> None: ...
    def batch(self, total: int) -> None: ...
    def problem(self, headline: str, detail: str = "", hint: str = "") -> None: ...


class _NullSink:
    def log(self, message: str, level: str = "info") -> None:  # noqa: D102
        pass

    def status(self, message: str) -> None:  # noqa: D102
        pass

    def batch(self, total: int) -> None:  # noqa: D102
        pass

    def problem(  # noqa: D102
        self, headline: str, detail: str = "", hint: str = ""
    ) -> None:
        pass


class FlowContext:
    """Handed to a service flow. Builds asks and carries side channels.

    ``log`` and ``status`` are fire-and-forget: they do not go through the
    generator, so service code can report progress without a yield.
    """

    def __init__(
        self,
        settings: Any = None,
        sink: Sink | None = None,
        answers: dict | None = None,
        *,
        interactive: bool = False,
    ):
        self.settings = settings
        self._sink: Sink = sink or _NullSink()
        #: pre-seeded answers for non-interactive runs, keyed by ask title
        self.answers = answers or {}
        #: True when a person is answering, which the interface sets and a headless
        #: walk does not. It is not about what a flow may ask - every ask works
        #: either way - but about whether "ask again" makes sense: an entry point
        #: that returns to its own prompt so the next URL can go in is right for a
        #: human and an infinite loop for a rule table that answers the same way
        #: every time.
        self.interactive = interactive

    # side channels ------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        self._sink.log(message, level)

    def status(self, message: str) -> None:
        self._sink.status(message)

    def warn(self, message: str) -> None:
        self._sink.log(message, "warning")

    def error(self, message: str) -> None:
        self._sink.log(message, "error")

    def problem(self, headline: str, detail: str = "", hint: str = "") -> None:
        """Report a failure where it cannot be missed, and carry on.

        The difference from :meth:`error` is only how loud it is: that writes a
        line into the log, this puts a panel on the screen as well. Use it for the
        failure a person has to act on - an expired session, a refused licence -
        and keep ``log``/``warn`` for the running commentary.

        It does not end anything. What happens next is the caller's decision.
        """
        self._sink.problem(headline, detail, hint)

    def batch(self, total: int) -> None:
        """Declare that ``total`` playbacks are about to be emitted.

        Call it right before a loop of :meth:`emit`. Only the flow knows how many
        are coming - the driver sees them one at a time - so without this the UI
        can count what has finished but not what it is counting towards.

        Unlike :meth:`log` and :meth:`status` this is **not** fire-and-forget: it
        may raise :class:`Back` when the user declines a large batch, which
        unwinds the flow the same way backing out of a list does. Treat it as a
        suspension point.
        """
        self._sink.batch(total)

    # ask builders -------------------------------------------------------
    def pick(
        self,
        title: str,
        choices: Sequence[Choice] | Sequence[str],
        *,
        multi: bool = False,
        hint: str = "",
        cursor: int = 0,
        preselected: Sequence[int] | None = None,
        scope: str = SCOPE_FLOW,
        chapters: Sequence[Any] = (),
        lyrics: Any = None,
        preview: Any = None,
    ) -> Pick:
        normalized = [c if isinstance(c, Choice) else Choice(str(c), c) for c in choices]
        return Pick(
            title=title,
            scope=scope,
            choices=normalized,
            multi=multi,
            hint=hint,
            cursor=cursor,
            preselected=list(preselected or []),
            chapters=tuple(chapters or ()),
            lyrics=lyrics,
            preview=preview,
        )

    def text(
        self,
        title: str,
        *,
        placeholder: str = "",
        default: str = "",
        password: bool = False,
        lines: Sequence[str | tuple[str, str]] | None = None,
        scope: str = SCOPE_FLOW,
    ) -> TextAsk:
        """Ask for one value, optionally after something has to be done elsewhere.

        ``lines`` follows :meth:`wait_for`: ``(label, value)`` pairs for what has to
        be acted on - the page to open before there is anything to paste - and plain
        strings for context. Passing them puts the field in the same centred panel
        as the values instead of leaving the address in the log.
        """
        # scope, like pick and confirm: a question asked while a delivery is being
        # set up belongs on that screen, not back on the one before it
        return TextAsk(
            title=title,
            placeholder=placeholder,
            default=default,
            password=password,
            lines=list(lines or []),
            scope=scope,
        )

    def form(
        self,
        title: str,
        fields: Sequence[Field],
        *,
        lines: Sequence[str | tuple[str, str]] | None = None,
        hint: str = "",
        scope: str = SCOPE_FLOW,
    ) -> Form:
        """Ask for several values in one card. Answered with a dict.

        Use it when the values only make sense together - an account and its
        password - and :meth:`text` when there is genuinely one thing to type.
        """
        return Form(
            title=title,
            fields=list(fields),
            lines=list(lines or []),
            hint=hint,
            scope=scope,
        )

    def confirm(self, title: str, *, default: bool = True, scope: str = SCOPE_FLOW) -> Confirm:
        return Confirm(title=title, default=default, scope=scope)

    def table(
        self,
        title: str,
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
        values: Sequence[Any],
        *,
        row_styles: Sequence[str] | None = None,
    ) -> TableAsk:
        return TableAsk(
            title=title,
            columns=list(columns),
            rows=[list(r) for r in rows],
            values=list(values),
            row_styles=list(row_styles or []),
        )

    def wait_for(
        self,
        title: str,
        lines: Sequence[str | tuple[str, str]],
        poll,
        *,
        timeout: float = 180.0,
        interval: float = 3.0,
        hint: str = "",
        qr: QrPresentation | None = None,
        scope: str = SCOPE_FLOW,
    ) -> Await:
        """Display ``lines`` and poll until something comes back.

        For anything that finishes on another device or in a browser, not only a
        device code. The lines are what the person needs, shown as given, so a
        service decides what is worth putting on screen.

        Write the parts that have to be acted on as ``(label, value)`` pairs and
        the rest as plain strings::

            yield ctx.wait_for(
                "Activate on another device",
                [
                    ("Open", challenge.url),
                    ("Code", challenge.code),
                    "The page signs you in; nothing needs typing here.",
                ],
                poll=...,
            )

        The pairs are what the UI puts in the middle of the screen and offers to
        copy. Plain strings still work and still show, which is why every service
        written before this kept rendering.  Pass ``qr=QrPresentation(...)`` for
        a QR challenge; never put an image data URI in ``lines`` because those
        rows are deliberately copyable and may be recorded by another presenter.
        """
        return Await(
            title=title,
            lines=list(lines),
            poll=poll,
            timeout=timeout,
            interval=interval,
            hint=hint,
            qr=qr,
            scope=scope,
        )

    def suspend(self, work, *, title: str = "", note: str = "") -> Suspend:
        """Hand the terminal to ``work`` for the duration of the call."""
        return Suspend(title=title, work=work, note=note)

    def settings_request(self, title: str = "Settings") -> SettingsAsk:
        """Open the settings panel mid-flow, then return to where we were."""
        return SettingsAsk(title=title)

    def emit(self, playback: Any) -> Emit:
        return Emit(title=getattr(playback, "save_name", ""), playback=playback)

    def partner_handoff(self, authorization: PartnerAuthorization) -> PartnerHandoff:
        """Ask the driver to route an in-memory authorization to another service."""
        return PartnerHandoff(
            title=f"Authorize {authorization.provider}",
            authorization=authorization,
        )


Flow = Iterator[Ask]


# --------------------------------------------------------------------------- driver


class Presenter(Protocol):
    """Renders an ask and blocks until the user answers.

    Implemented by the Textual UI (marshalling to the event loop) and by
    headless presenters for scripted / non-interactive runs.
    """

    def present(self, ask: Ask) -> Any: ...


class AutoPresenter:
    """Headless presenter: answers from a rule table instead of a human.

    How a service flow gets exercised without a terminal - see
    docs/writing-a-service.md. There is no download CLI for this to serve, and
    the check scripts drive the real TUI instead, so this exists for whoever is
    writing a new service and wants to walk their generator without one.
    ``rules`` maps an ask title to an answer or a callable.
    """

    def __init__(self, rules: dict[str, Any] | None = None, *, pick_all: bool = True):
        self.rules = rules or {}
        self.pick_all = pick_all

    def present(self, ask: Ask) -> Any:
        if isinstance(ask, Suspend):
            # nothing to suspend without a UI; just run it
            return ask.work() if callable(ask.work) else None
        if isinstance(ask, PartnerHandoff):
            raise PartnerAuthorizationError(
                "headless partner handoff needs a presenter with a service resolver"
            )
        for key, value in self.rules.items():
            if key.lower() in ask.title.lower():
                return value(ask) if callable(value) else value
        if isinstance(ask, Pick):
            enabled = [c for c in ask.choices if not c.disabled]
            if not enabled:
                raise Back()
            if ask.multi:
                if ask.preselected:
                    return [ask.choices[i].value for i in ask.preselected if 0 <= i < len(ask.choices)]
                return [c.value for c in enabled] if self.pick_all else [enabled[0].value]
            return enabled[0].value
        if isinstance(ask, Confirm):
            return ask.default
        if isinstance(ask, TextAsk):
            if ask.default:
                return ask.default
            raise Back()
        if isinstance(ask, TableAsk):
            if not ask.values:
                raise Back()
            return ask.values[0]
        if isinstance(ask, Await):
            # One attempt, not a wait: there is nobody here to confirm anything on
            # another device, and a headless walk that blocked for three minutes
            # would look like a hang.
            result = ask.poll() if callable(ask.poll) else None
            if result is None:
                raise Back()
            return result
        return None


def run_flow(
    flow: Flow,
    presenter: Presenter,
    *,
    on_emit: Callable[[Any], None] | None = None,
) -> list[Any]:
    """Drive a service flow to completion.

    Returns everything the flow emitted. ``Back`` raised by the presenter is
    thrown into the generator so the flow can handle it; if the flow does not,
    the flow ends. This is a plain synchronous loop, so it must run off the UI
    thread when driven by Textual.
    """
    emitted: list[Any] = []
    answer: Any = None
    # generator.throw(type) is deprecated since 3.12, so carry an instance
    pending_exc: BaseException | None = None

    while True:
        try:
            if pending_exc is not None:
                exc, pending_exc = pending_exc, None
                ask = flow.throw(exc)
            else:
                ask = flow.send(answer)
        except (StopIteration, Back):
            return emitted
        except Quit:
            raise

        answer = None
        if isinstance(ask, Emit):
            emitted.append(ask.playback)
            if on_emit:
                # The same two signals the presenter can raise, handled the same
                # way. Without this, a Back out of a delivery-scope question was
                # reported as "the service stopped with an error: Back", and a Quit
                # left the flow generator open with its cleanup unrun.
                try:
                    on_emit(ask.playback)
                except Back:
                    pending_exc = Back()
                except Quit:
                    flow.close()
                    raise
            continue

        try:
            answer = presenter.present(ask)
        except Back:
            pending_exc = Back()
        except Quit:
            flow.close()
            raise
