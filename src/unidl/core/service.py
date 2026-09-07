"""The service plugin contract.

Capability declaration is the compatibility bridge for the existing script
corpus. A freshly ported service declares everything ``core`` and gets the
framework's auth store, CDM flow, track picker and naming for free. A service
that would rather keep its own implementation of any of those flips that one
capability to ``self`` and implements a coarser interface instead - because
"I'll handle it, don't ask me about the steps" needs less surface, not more.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any

import requests

from . import drm as drm_registry
from .brands import service_tag
from .cache import TokenStore
from .cdm import WIDEVINE, CdmError, DeviceFile, system_of
from .config import Config
from .cookies import CookieError, CookieStore, as_header
from .credentials import Credential, CredentialSlot, mask_account
from .flow import SCOPE_ROOT, Ask, Back, Choice, Field, FlowContext
from .helpers import Helper, HelperReport, HelperResolver, HelperRunner, load_module
from .naming import save_name_for
from .partner import PartnerAuthorization, PartnerAuthorizationResult
from .playback import DrmInfo, Playback
from .settings import (
    TRACK_SETTINGS,
    Setting,
    Settings,
    SettingsStore,
    cdm_choice,
    cdm_device_settings,
    cookie_profile_setting,
    drm_system_setting,
    live_settings,
    proxy_setting,
)
from .titles import Title

CORE = "core"
SELF = "self"

#: ``login_method`` values that mean "an account and a password". The rest of the
#: vocabulary in use across the services - ``tv``, ``tv_provider``, ``provider``,
#: ``otp``, ``anonymous``, ``free``, ``ip``, ``freebox`` - is a code, a provider or
#: no sign-in at all, and none of them reads a password. See
#: :meth:`Service.wants_credentials`.
ACCOUNT_METHODS = frozenset({"account", "credentials", "email", "password", "login"})


@dataclass(frozen=True)
class Capabilities:
    """Which side owns each piece of the basic machinery."""

    auth: str = CORE
    catalog: str = CORE
    drm: str = CORE
    tracks: str = CORE
    naming: str = CORE

    def is_self(self, name: str) -> bool:
        return getattr(self, name, CORE) == SELF

    def summary(self) -> str:
        owned = [name for name in ("auth", "catalog", "drm", "tracks", "naming") if self.is_self(name)]
        return "core" if not owned else "core, self: " + ", ".join(owned)

    def with_self(self, *names: str) -> Capabilities:
        return replace(self, **{name: SELF for name in names})


@dataclass
class AuthStatus:
    logged_in: bool = False
    label: str = "not signed in"
    detail: str = ""
    anonymous_ok: bool = False

    @property
    def usable(self) -> bool:
        return self.logged_in or self.anonymous_ok


@dataclass
class ServiceContext:
    """Everything a service is allowed to reach out to."""

    config: Config
    settings: Settings
    tokens: TokenStore
    service_id: str
    device_path: Path | None = None
    device_name: str = ""
    proxy: str | None = None
    helpers: HelperReport = field(default_factory=HelperReport)
    extras: dict[str, Any] = field(default_factory=dict)
    #: which named login this session is using. Selects the credential slot and
    #: the cookie file, so "my other account" is one word rather than a second
    #: copy of the configuration.
    profile: str = ""
    #: ids accepted before this service was renamed.  They are read-only
    #: compatibility paths; new state is always written under ``service_id``.
    legacy_service_ids: tuple[str, ...] = ()

    def credential(self, slot: str = "default") -> Credential:
        return self.config.credential(
            self.service_id, slot, legacy_ids=self.legacy_service_ids
        )

    # ---------------------------------------------------------------- cookies
    @property
    def cookie_store(self) -> CookieStore:
        return CookieStore(self.config.paths.cookies)

    def cookie_profile(self, profile: str = "") -> str:
        """The named cookie file for this request, never an arbitrary path.

        A caller's explicit profile wins, followed by the named-login profile a
        non-TUI caller supplied while building the service. Ordinary TUI sessions
        read the service setting live, so changing the cookie choice affects the
        next session the service creates without restarting unidl.
        """
        explicit = str(profile or "").strip()
        if explicit:
            return explicit
        if self.profile:
            return str(self.profile).strip()
        return str(self.settings.get("cookie_profile", "") or "").strip()

    def cookie_path(self, profile: str = "") -> Path | None:
        """The cookie file in use for this service, if there is one."""
        return self.cookie_store.path_for(
            self.service_id,
            self.cookie_profile(profile),
            legacy_service_ids=self.legacy_service_ids,
        )

    def cookies(self, profile: str = ""):
        """This service's cookie jar, or None. Raises nothing on a bad file.

        A broken cookie file is reported by :meth:`cookie_info` and by the sign-in
        flow, where there is somewhere to say it. Here it has to be None, because
        every ``session()`` call goes through this and a service that has never
        heard of cookies must not fail to build a session because of a file
        somebody dropped in the wrong folder.
        """
        try:
            return self.cookie_store.jar_for(
                self.service_id,
                self.cookie_profile(profile),
                legacy_service_ids=self.legacy_service_ids,
            )
        except CookieError:
            return None

    def cookie_info(self, profile: str = ""):
        return self.cookie_store.info(
            self.service_id,
            self.cookie_profile(profile),
            legacy_service_ids=self.legacy_service_ids,
        )

    def save_cookies(self, jar, profile: str = "") -> Path | None:
        """Write a session's cookies back, so a refreshed session survives."""
        return self.cookie_store.save(
            jar,
            self.service_id,
            self.cookie_profile(profile),
            legacy_service_ids=self.legacy_service_ids,
        )

    def cookie_header(self, url: str = "", profile: str = "") -> str:
        """A ``Cookie:`` header for a playback, or "" when there are none.

        The downloader takes headers, not a jar, so a service whose *media* sits
        behind the same session as its API has to pass them this way. Narrowed to
        the URL's host: sending a whole account's jar to a CDN is unnecessary, and
        a big enough jar trips request-size limits on some of them.
        """
        jar = self.cookies(profile)
        if jar is None:
            return ""
        host = ""
        if url:
            from urllib.parse import urlparse

            host = urlparse(url).hostname or ""
        return as_header(jar, host)

    # ---------------------------------------------------------------- helpers
    def helper(self, key: str) -> Path:
        """Path to a declared helper, or a clear error if it is unavailable."""
        resolved = self.helpers.resolved.get(key)
        if resolved is None:
            raise HelperMissing(f"{self.service_id} did not declare a helper named '{key}'")
        if not resolved.ok:
            hint = resolved.helper.install_hint
            message = f"{resolved.helper.label} is not available ({resolved.reason})"
            raise HelperMissing(f"{message}\n{hint}" if hint else message)
        return resolved.path  # type: ignore[return-value]

    def has_helper(self, key: str) -> bool:
        resolved = self.helpers.resolved.get(key)
        return bool(resolved and resolved.ok)

    def helper_module(self, key: str, name: str | None = None):
        return load_module(self.helper(key), name)

    def runner(self, log=None) -> HelperRunner:
        return HelperRunner(log=log, debug=bool(self.settings.get("debug", False)))

    def refresh_proxy(self) -> None:
        """Re-resolve the proxy after the choice was changed elsewhere.

        The same problem as :meth:`refresh_device`: this is a snapshot taken when the
        service was built, and the proxy can be changed from the settings screen while
        a session is open. Without this the session would keep using the route it was
        opened with while the screen showed the new one.
        """
        self.proxy = proxy_for(self.config, self.settings)

    def refresh_device(self, requested: str = "") -> None:
        """Re-resolve the CDM device after the choice was changed elsewhere.

        ``device_path`` and ``device_name`` are worked out when the service is
        built, and the device can be changed from the main screen while a session
        is open. Without this the screen kept naming the old device while the
        licence exchange - which resolves afresh - used the new one, so the two
        disagreed and only one of them was visible.

        ``requested`` is the service's own choice, passed in by
        :meth:`Service.refresh_device` because which system it applies to is a
        fact about the service, not about this context. Empty means no opinion and
        resolution falls through to the app-wide choice.
        """
        self.device_path = self.config.device_for(
            self.service_id, requested or None, legacy_ids=self.legacy_service_ids
        )
        self.device_name = self.config.device_name_for(
            self.service_id, requested or None, legacy_ids=self.legacy_service_ids
        )

    def helper_dir(self) -> Path:
        path = self.config.paths.helpers / self.service_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def subtitle_dir(self) -> Path:
        """Where this service stages subtitle files it fetched itself.

        For the services that hand UniDL a sidecar to mux rather than a track in
        the manifest. One folder per service under ``paths.subtitles``, created on
        demand: these are inputs to a download, and they used to live in a corner of
        the cache, which is a folder people delete without thinking.
        """
        path = self.config.paths.subtitles / self.service_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------------------------------------------------------------- network
    def session(
        self,
        *,
        user_agent: str = "",
        headers: dict[str, str] | None = None,
        cookies: bool = True,
    ) -> requests.Session:
        """A requests session with the proxy and any exported cookies applied.

        Cookies are attached by default. A service does not have to know that it
        is being signed in this way - which is the point, because for most of them
        cookies are not a different code path, they are the same requests with an
        account behind them. ``cookies=False`` is for the request that must be
        anonymous, like minting a guest token.
        """
        session = requests.Session()
        if user_agent:
            session.headers["User-Agent"] = user_agent
        if headers:
            session.headers.update(headers)
        if self.proxy:
            session.proxies.update({"http": self.proxy, "https": self.proxy})
        if cookies:
            jar = self.cookies()
            if jar is not None:
                session.cookies.update(jar)
        return session


def proxy_for(config: Config, settings: Settings) -> str | None:
    """The proxy this scope resolves to, or None for a direct connection.

    Narrowest first - this service's own setting, then the app-wide one - and empty
    at both levels means direct, which is the default. A proxy is something this
    project offers; nothing here turns one on by itself, not even for a service
    whose ``GEOFENCE`` says the current address is in the wrong country.

    The value is passed through :meth:`Config.proxy`, so a name from the ``proxies``
    section and a full URI are both accepted and neither has to be told apart here.
    """
    chosen = str(settings.inherited("proxy", "") or "").strip()
    return config.proxy(chosen or None)


class HelperMissing(RuntimeError):
    """Raised when a service reaches for a helper that is not installed."""


class Service:
    """Base class for every service.

    Subclasses override the entry points they support. ``home`` has a default
    implementation that assembles the top menu from whatever is available, so
    most services never write a menu at all.
    """

    ID: str = ""
    NAME: str = ""
    #: Service ids used before a rename.  They remain lookup aliases and are
    #: consulted only for reading existing state/configuration.
    LEGACY_IDS: tuple[str, ...] = ()
    #: The short uppercase service tag used in release names. Left empty means
    #: "derive one", so a service does not have to invent a tag, but
    #: setting it explicitly is how the real one gets recorded.
    TAG: str = ""
    ALIASES: tuple[str, ...] = ()
    TITLE_RE: str = ""
    GEOFENCE: tuple[str, ...] = ()
    #: Media offered by this platform.  ``video`` is the conservative default
    #: for existing services; audio catalogues and platforms that offer both
    #: declare their complete set explicitly so the home screen can group them
    #: without probing a network or resolving a title.
    MEDIA_TYPES: tuple[str, ...] = ("video",)
    DESCRIPTION: str = ""
    #: What this class is: a unidl service. Invariant - there is no other kind, and
    #: no way to register anything that is not a Service subclass from this package.
    #: It stays because it is what the checks name when they assert that, and an
    #: assertion that a thing is what it must be is still the assertion you want the
    #: day somebody tries to add a second kind.
    MODE: str = "native"

    USES: Capabilities = Capabilities()

    #: service-specific settings; the shared track settings are appended by core
    SETTINGS: list[Setting] = []
    CREDENTIALS: list[CredentialSlot] = []
    #: external binaries, modules and assets this service needs
    HELPERS: list[Helper] = []

    #: DRM systems this service can actually use, most preferred first. Declare
    #: more than one and the service gets its own DRM choice in its own settings,
    #: applying to it alone; declare one and that one is used whatever the
    #: app-wide setting says. Empty means "whatever is set app-wide", which is
    #: right for the services that only ever see Widevine.
    DRM_SYSTEMS: tuple[str, ...] = ()
    #: This service can be signed in with browser cookies. Declare it when the
    #: account state a person needs is carried by cookies - which is most web
    #: services, and the only route into the ones whose real sign-in involves a
    #: captcha, an SSO redirect or a device attestation. Core then offers the
    #: import flow, attaches the jar to every session, and reports the file in the
    #: auth status; the service itself usually needs no cookie code at all.
    USES_COOKIES: bool = False

    #: Cookie storage owner when it intentionally differs from ``ID``. This is
    #: still a managed subdirectory name, never a caller-provided path.
    COOKIE_SERVICE_ID: str = ""

    #: Keep core's separate cookie-import menu entry for services whose cookie
    #: route is independent of their normal sign-in. A service with an explicit
    #: sign-in-method setting can turn this off and route its single Sign in
    #: action through that setting instead.
    COOKIE_LOGIN_ACTION: bool = True

    #: which of the optional entry points this service implements
    SUPPORTS_URL: bool = True
    SUPPORTS_SEARCH: bool = False
    SUPPORTS_LIVE: bool = False
    SUPPORTS_LIBRARY: bool = False

    #: Service IDs allowed to issue an in-memory partner authorization to this
    #: receiver. Empty means this service is not a receiver.
    PARTNER_AUTHORIZATION_SOURCES: tuple[str, ...] = ()

    def __init__(self, ctx: ServiceContext):
        self.ctx = ctx
        self.settings = ctx.settings

    def fetch_chapters_enabled(self) -> bool:
        """Whether this run may request optional chapter metadata.

        Chapter acquisition is an app-wide policy, so service scopes inherit the
        ``fetch_chapters`` value from their parent global settings.  A few offline
        service checks use a plain dict instead of :class:`Settings`; keeping the
        small coercion here makes those checks and headless callers behave exactly
        like the TUI without requiring a settings object just to read one switch.
        """
        settings = getattr(self, "settings", None)
        # A persisted service scope may contain an obsolete key from an older
        # build.  When a real Settings parent declares the policy, read that
        # app-wide value directly so a stale per-service entry cannot silently
        # override the global switch.
        parent = getattr(settings, "parent", None)
        parent_specs = getattr(parent, "spec_by_key", {})
        if parent is not None and "fetch_chapters" in parent_specs:
            getter = getattr(parent, "get", None)
        else:
            getter = getattr(settings, "get", None)
        if not callable(getter):
            return True
        try:
            value = getter("fetch_chapters", True)
        except TypeError:
            # Tiny headless fakes sometimes expose a one-argument ``get``
            # callable rather than a full dict/Settings object.
            value = getter("fetch_chapters")
            if value is None:
                value = True
        if isinstance(value, str):
            return value.strip().casefold() not in {"", "0", "false", "no", "off"}
        return bool(value)

    # ------------------------------------------------------------------ meta
    @classmethod
    def tag(cls) -> str:
        """The service tag, declared or derived from the id."""
        return cls.TAG or service_tag(cls.ID)

    @classmethod
    def media_types(cls) -> tuple[str, ...]:
        """Return the platform-level media categories declared by the service.

        This is deliberately metadata, not a scan of one title's tracks: a
        video service may contain an audio track and an audio/video service may
        have a video-free catalogue. Invalid or empty declarations fall back to
        ``video`` so a malformed plugin never disappears from the home screen.
        """
        allowed = {"audio", "video"}
        values = tuple(
            value
            for value in (str(item).strip().lower() for item in getattr(cls, "MEDIA_TYPES", ()))
            if value in allowed
        )
        return tuple(dict.fromkeys(values)) or ("video",)

    def release_tag(self) -> str:
        """The tag written into downloaded release names.

        Most services have one stable tag. A merged service may expose several
        platform profiles while remaining one registry entry, so it can narrow
        this output-only tag without changing lookup, cache, or vault identity.
        """
        return type(self).tag()

    @classmethod
    def setting_specs(cls, config: Config | None = None) -> list[Setting]:
        """This service's settings, plus the shared track vocabulary.

        A service that can use more than one DRM system also gets its own copy of
        the DRM choice, so switching it here changes nothing for the other 149.
        Every native service also gets its own CDM choice - one per system it
        declares - for the same reason: which device answers *this* service is a
        narrower question than which device the application prefers, and the two
        answers should not have to be the same.

        ``config`` is what the device options are built from. Passed rather than
        reached for because this is a classmethod and the caller that has a
        config - :meth:`ServiceRegistry.build` - is the only one that needs real
        options; the checks that ask a class what settings it has do not.
        """
        specs = list(cls.SETTINGS)
        if cls.USES_COOKIES and not any(s.key == "cookie_profile" for s in specs):
            cookie_service_id = cls.COOKIE_SERVICE_ID or cls.ID
            legacy_cookie_ids = () if cls.COOKIE_SERVICE_ID else cls.LEGACY_IDS
            specs.append(cookie_profile_setting(cookie_service_id, config, legacy_cookie_ids))
        if len(cls.DRM_SYSTEMS) > 1 and not any(s.key == "drm_system" for s in specs):
            specs.append(drm_system_setting(list(cls.DRM_SYSTEMS)))
        declared = {s.key for s in specs}
        specs += [
            spec
            for spec in cdm_device_settings(list(cls.DRM_SYSTEMS), config)
            if spec.key not in declared
        ]
        if "proxy" not in declared:
            specs.append(proxy_setting())
        if cls.SUPPORTS_LIVE:
            # only where there is live to record. A recording length on a service
            # with no live channels is a setting that can never be read.
            declared = {s.key for s in specs}
            specs += [spec for spec in live_settings() if spec.key not in declared]
        declared = {s.key for s in specs}
        return specs + [spec for spec in TRACK_SETTINGS if spec.key not in declared]

    def cdm_choice(self, system: str = "") -> str:
        """The CDM named by this service's own settings, or "" for none.

        Narrower than the main screen's picker and narrower than ``cdm.default``,
        and read before both. It is not narrower than a device named on the
        playback itself, which is a decision about one request.
        """
        return cdm_choice(self.settings, system or self.drm_system())

    def _auto_match_cdm(self, system: str, requested: str | None) -> bool:
        """Whether an app-wide device may be replaced by a system match.

        Single-system services have always been pinned to their declared DRM.
        MonaLisa needs the same treatment at playback time for a mixed service:
        a mixed DRM service can also return Widevine, but a MonaLisa ticket is an unambiguous
        request for the local ``.mld`` and must not inherit the app-wide ``.wvd``.
        A device explicitly selected for this playback or this service remains
        authoritative, including when it is the wrong type and needs reporting.
        """
        return bool(self.drm_default()) or (
            system == drm_registry.MONALISA and not requested
        )

    def _device_for_drm(self, system: str, requested: str | None) -> Path | None:
        """Resolve the local device used by one DRM exchange."""
        return self.ctx.config.device_for(
            self.ID,
            requested,
            system=system,
            pinned=self._auto_match_cdm(system, requested),
            legacy_ids=self.LEGACY_IDS,
        )

    def cdm_name(self, system: str = "") -> str:
        """Short label for the CDM this service will actually use."""
        system = system or self.drm_system()
        requested = self.cdm_choice(system) or None
        remote = self.ctx.config.remote_cdm_for(
            self.ID,
            requested,
            system=system,
            legacy_ids=self.LEGACY_IDS,
        )
        if remote is not None:
            return str(remote.name)
        path = self._device_for_drm(system, requested)
        if path is not None:
            return path.stem
        return self.ctx.config.device_name_for(
            self.ID, requested, legacy_ids=self.LEGACY_IDS
        )

    def cdm_level(self, system: str = "") -> str:
        """The security level of the CDM this service will use - ``L1``, ``SL3000``.

        Here rather than in a service because more than one service has to know it
        *before* asking for a stream: a provider may hand over a manifest for a
        ladder it will then refuse to licence to a lower-level device, and the
        refusal names neither the device nor the ladder. Resolved through the same
        call that will pick the device for the licence, so the answer cannot
        disagree with what is used a moment later. Empty when nothing is resolvable,
        which callers should read as "no reason to assume a restriction".
        """
        system = system or self.drm_system()
        requested = self.cdm_choice(system) or None
        remote = self.ctx.config.remote_cdm_for(
            self.ID, requested, system=system, legacy_ids=self.LEGACY_IDS
        )
        if remote is not None:
            return str(getattr(remote, "level", "") or "").upper()
        path = self._device_for_drm(system, requested)
        return DeviceFile(path=path, system=system).level.upper() if path else ""

    def refresh_device(self) -> None:
        """Re-resolve the context's CDM, honouring this service's own choice."""
        self.ctx.refresh_device(self.cdm_choice())

    def drm_system(self) -> str:
        """Which DRM system applies to *this* service, right now.

        Narrowest first: this service's own setting, then the app-wide one, then
        Widevine. A service that declared exactly one system does not get a
        setting at all, and that one wins - it is a fact about the service, not a
        preference. Services call this when they need the answer before a
        playback exists, which is the case whenever the session token itself
        differs by system.
        """
        declared = self.drm_default()
        if declared:
            return declared
        chosen = self.settings.inherited("drm_system", WIDEVINE)
        return str(chosen or WIDEVINE).lower()

    @classmethod
    def drm_default(cls) -> str:
        """The system to use when nothing has been chosen anywhere.

        A single declared system is a statement of fact about the service, not a
        preference, so it wins over the app-wide setting. Several means the
        service can do either and the choice belongs to whoever is using it.
        """
        return cls.DRM_SYSTEMS[0] if len(cls.DRM_SYSTEMS) == 1 else ""

    @classmethod
    def matches_url(cls, text: str) -> bool:
        if not cls.TITLE_RE:
            return False
        import re

        return bool(re.search(cls.TITLE_RE, text, re.IGNORECASE))

    # ------------------------------------------------------------------ auth
    def auth_status(self) -> AuthStatus:
        """Cheap, non-network description of the current login."""
        return AuthStatus(anonymous_ok=True, label="no login required")

    @classmethod
    def accepts_partner_authorization(cls, source_service_id: str) -> bool:
        """Whether this service accepts one-time authorization from ``source``."""
        source = str(source_service_id or "").strip().lower()
        return source in cls.PARTNER_AUTHORIZATION_SOURCES

    def consume_partner_authorization(
        self, authorization: PartnerAuthorization
    ) -> PartnerAuthorizationResult:
        """Receive a core-routed partner authorization.

        Services remain isolated: producers emit the core contract and receivers
        opt in here. A receiver never imports or calls the producer.
        """
        del authorization
        return PartnerAuthorizationResult(
            self.ID,
            False,
            label=f"{self.NAME} does not support partner authorization",
        )

    @classmethod
    def supports_login(cls) -> bool:
        """True when this service implements an interactive sign-in.

        Derived from whether ``login`` was overridden rather than declared with a
        flag, so a service cannot claim a sign-in it does not have, or have one
        the menu never offers - which is exactly what happened while ``login``
        existed and nothing called it.

        Declaring ``USES_COOKIES`` counts, because core supplies the whole flow
        for that one: there is nothing for the service to override.
        """
        return cls.login is not Service.login or cls.USES_COOKIES

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Interactive login. May yield asks (device codes, slot choice).

        The default is the cookie import, for a service whose only sign-in is
        cookies. A service with its own sign-in overrides this and still gets the
        cookie route offered separately when it declares ``USES_COOKIES``.
        """
        if self.USES_COOKIES:
            yield from self.cookie_login(ctx)

    def begin_login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Sign in, collecting an account first if the config has none.

        Every menu goes through this rather than calling :meth:`login` straight,
        because "where does the account come from" is one question and it has one
        answer for the fifty-odd services whose sign-in is an account and a
        password. They declare that in :attr:`CREDENTIALS`, so the form can be
        offered from here without any of them knowing about it - and their own
        prompts, which take one value each in the bottom bar and remember nothing,
        never fire, because the credential is complete by the time they look.

        Only for the unambiguous case: exactly one declared slot, and that slot is
        a username and a password. A service with several accounts to choose
        between, or one that signs in with a device code, a token or cookies, is
        left to ask for itself - it knows something this does not.
        """
        slots = list(self.CREDENTIALS)
        if (
            len(slots) == 1
            and {"username", "password"} <= set(slots[0].fields)
            and self.wants_credentials()
        ):
            slot = slots[0]
            if not self.ctx.credential(slot.key).complete:
                yield from self.sign_in_details(
                    ctx, slot=slot.key, note=slot.help or "", label=slot.label
                )
        yield from self.login(ctx)

    def wants_credentials(self) -> bool:
        """Whether an account and a password are what this sign-in is about to use.

        A service that can sign in more than one way says so by declaring a
        ``login_method`` setting - seventeen of them do - and the value chosen there
        decides. ``account`` means a password; ``tv``, ``provider``, ``otp``,
        ``anonymous`` and the rest do not, and asking for a password before one of
        those is asking for something nothing will read. That is what used to happen:
        the form came up first, took anything at all because nothing checked it, and
        only then did the activation code appear.

        A service that declares no such setting has one way in, and this is True.
        """
        settings = self.settings
        get = getattr(settings, "get", None)
        if not callable(get):
            return True
        chosen = str(get("login_method", "") or "").strip().lower()
        return chosen in ACCOUNT_METHODS if chosen else True

    def sign_in_details(
        self,
        ctx: FlowContext,
        *,
        slot: str = "default",
        label: str = "",
        title: str = "",
        note: str = "",
        username_label: str = "Email or username",
        username_placeholder: str = "name@example.com",
        password_label: str = "Password",
        save: bool = True,
    ) -> Iterator[Ask]:
        """The account and password for ``slot``, asked for if they are not set.

        ``unidl.yaml`` stays the place a login lives, and this is the other way to
        put one there: a card in the middle of the screen, both fields at once, the
        password masked with ``^r`` to show it - and then written into the file, so
        it is asked once rather than every session.

        Use it with ``yield from``::

            credential = yield from self.sign_in_details(ctx)
            if not credential.complete:
                ctx.error("no account to sign in with")
                return
            client.sign_in(credential.username, credential.password)

        Nothing is asked when the file already answers, so a configured install
        behaves exactly as it did. What comes back is always a
        :class:`~unidl.core.credentials.Credential`: check ``complete`` rather than
        assuming, because backing out of the card is allowed.
        """
        credential = self.ctx.credential(slot)
        if credential.complete:
            return credential

        # the slot's own label when it has one - "US account", "Freebox" - because
        # with several to choose from, "Sign in to X" is not enough to go on
        which = label or (slot if slot and slot != "default" else "")
        answer = yield ctx.form(
            title or f"Sign in to {self.NAME}" + (f"  ·  {which}" if which else ""),
            [
                Field(
                    "username",
                    username_label,
                    placeholder=username_placeholder,
                    default=credential.username,
                ),
                Field("password", password_label, password=True),
            ],
            hint=note or "kept in unidl.yaml, so this is asked once",
        )
        if not isinstance(answer, dict):
            return credential

        values = {
            "username": str(answer.get("username") or "").strip(),
            "password": str(answer.get("password") or ""),
        }
        if not (values["username"] and values["password"]):
            ctx.warn("both an account and a password are needed to sign in")
            return credential

        if save:
            try:
                path = self.ctx.config.save_credential(self.ID, slot, values)
            except OSError as exc:
                # not fatal: the sign-in can still go ahead with what was typed,
                # it just will not be remembered
                ctx.warn(f"could not save this login to the config file: {exc}")
            else:
                ctx.log(f"login saved to {path}", "ok")
        return Credential(slot=slot, values=values)

    def saved_login(self) -> tuple[str, Credential] | None:
        """The account this service has in ``unidl.yaml``, if it has one.

        ``(slot, credential)`` for the single declared username/password slot -
        the same one :meth:`begin_login` fills in - so a menu can offer to forget
        it. None when the service has no such slot or nothing is saved.
        """
        slots = list(self.CREDENTIALS)
        if len(slots) != 1 or not {"username", "password"} <= set(slots[0].fields):
            return None
        credential = self.ctx.credential(slots[0].key)
        return (slots[0].key, credential) if credential.complete else None

    def forget_login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Delete the saved account and end its session, once confirmed.

        This remains separate from :meth:`logout`: ordinary sign-out must not edit a
        file the user may have written by hand. Explicitly deleting saved credentials
        does both, so a still-valid token cannot make the deleted account appear to
        remain active.
        """
        found = self.saved_login()
        if found is None:
            ctx.warn("there is no saved account for this service")
            return
        slot, credential = found
        sure = yield ctx.confirm(
            f"Delete saved credentials for {mask_account(credential.username)} "
            "and sign out?",
            default=False,
        )
        if not sure:
            return
        try:
            path = self.ctx.config.forget_credential(
                self.ID, slot, legacy_ids=self.LEGACY_IDS
            )
        except OSError as exc:
            ctx.error(f"could not edit the config file: {exc}")
            return
        if path is None:
            ctx.warn("there is no config file to edit")
            return
        self.logout()
        ctx.log(f"saved credentials removed from {path}; signed out", "ok")

    def logout(self) -> None:
        """Forget this service's login.

        The cookie file is removed here rather than in each service, because core
        is what put it there. A service with its own token cache overrides this and
        should call ``super().logout()`` so both go.

        The saved account in ``unidl.yaml`` is *not* touched: signing out ends a
        session, and the account is how the next one starts. :meth:`forget_login`
        is the one that deletes it, and it asks first.
        """
        if self.USES_COOKIES:
            self.ctx.cookie_store.remove(
                self.ID,
                self.ctx.cookie_profile(),
                legacy_service_ids=self.LEGACY_IDS,
            )

    # ----------------------------------------------------------------- cookies
    def cookie_login(self, ctx: FlowContext) -> Iterator[Ask]:
        """Use the selected cookie profile from this service's managed folder.

        Cookie files are deliberately placed outside the flow. Accepting an
        arbitrary path here would bypass the per-service boundary enforced by
        :class:`CookieStore` and make it too easy to authenticate with another
        service's account by accident.
        """
        profile = self.ctx.cookie_profile()
        directory = self.ctx.cookie_store.directory_for(self.ID)
        existing = self.ctx.cookie_info()
        if existing is not None:
            ctx.log(f"current cookies: {existing.line()}  ({existing.path})", "ok")

        ctx.log(f"{self.NAME} signs in with browser cookies.", "info")
        ctx.log(f"Place the browser export in {directory}.", "info")
        ctx.log(
            f"The selected profile is {profile or 'default'}.txt; no external path is accepted.",
            "info",
        )
        if self.ctx.cookie_path() is None:
            ctx.warn("no cookie file was found in the service cookie folder")
        else:
            ctx.log("using the cookie file already in place", "ok")
        # Keep this method a flow even though it has no user input anymore.
        yield from ()

    def cookie_status(self) -> AuthStatus:
        """Auth status derived from the cookie file alone.

        For a service whose whole sign-in is cookies, this is the answer. One that
        also has a token cache should check that first and fall back to this.
        """
        info = self.ctx.cookie_info()
        if info is None:
            return AuthStatus(logged_in=False, label="no cookies imported")
        if not info.count:
            return AuthStatus(
                logged_in=False, label=f"{info.path.name} could not be read"
            )
        return AuthStatus(logged_in=True, label=info.line(), detail=str(info.path))

    def _auth_status_quietly(self) -> AuthStatus:
        """``auth_status`` without letting it break the menu.

        The menu is assembled from this, so a service whose status probe throws -
        an unreadable token file, a cache in a shape it did not expect - would
        otherwise take the whole screen down instead of showing a sign-in entry,
        which is the one thing that would fix it.
        """
        try:
            return self.auth_status()
        except Exception as exc:  # noqa: BLE001
            return AuthStatus(logged_in=False, label=f"unreadable ({type(exc).__name__})")

    # --------------------------------------------------------------- browsing
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        """Default top menu, assembled from the declared entry points.

        Back inside a sub-flow returns to *this* menu rather than ending the
        session; only Back on the menu itself leaves the service. Sub-flows do
        not need to know that, they just let Back propagate.
        """
        while True:
            options: list[Choice] = []
            status = self._auth_status_quietly()

            # Sign-in leads when the service cannot be used yet. This is the first
            # thing a person sees on a service they have never opened, and putting
            # it below four entries that will all fail is how "the feature exists"
            # and "the feature is reachable" come apart.
            if self.supports_login() and not status.usable:
                options.append(
                    Choice("Sign in", "login", detail=status.label or "not signed in")
                )
            if self.SUPPORTS_URL:
                options.append(Choice("VOD - open a URL or content ID", "url"))
            if self.SUPPORTS_LIVE:
                options.append(Choice("Live TV", "live"))
            if self.SUPPORTS_SEARCH:
                options.append(Choice("Search", "search"))
            if self.SUPPORTS_LIBRARY:
                options.append(Choice("My library", "library"))
            options.append(Choice("Settings", "settings"))
            if self.supports_login() and status.usable:
                # After the browsing entries: signing in again is a repair, not
                # something anyone came here to do. A service that works
                # anonymously still offers it, because signing in is often what
                # unlocks the rest of the catalogue.
                label = "Sign in" if not status.logged_in else "Sign in again"
                options.append(Choice(label, "login", detail=status.label))
                if status.logged_in:
                    options.append(Choice("Sign out", "logout", detail=status.label))
            # Only when there is one to delete. The other way to delete it is to
            # edit unidl.yaml, which works and always will - this is here because
            # something the interface saved should be removable from the interface.
            saved = self.saved_login()
            if saved is not None:
                options.append(
                    Choice(
                        "Delete saved credentials",
                        "forget",
                        detail=f"{mask_account(saved[1].username)} in unidl.yaml",
                    )
                )
            # `type(self).login`, not `self.login`: the second is a bound method
            # and is never identical to the plain function, so this condition was
            # always true and every cookie service showed the entry twice.
            if (
                self.USES_COOKIES
                and self.COOKIE_LOGIN_ACTION
                and type(self).login is not Service.login
            ):
                # A service with its own sign-in *and* cookies gets both offered.
                # They are not alternatives to choose between abstractly: the real
                # sign-in is better when it works, and cookies are what you reach
                # for when it does not.
                cookies = self.ctx.cookie_info()
                options.append(
                    Choice(
                        "Sign in with browser cookies",
                        "cookies",
                        detail=cookies.line() if cookies else "no cookies imported",
                    )
                )

            # no title: the screen header already names the service.
            # scope=root puts this on the service's own screen; everything a
            # sub-flow asks for lands on the next screen instead.
            # Back here means "leave the service", so it is not caught.
            action = yield ctx.pick("", options, scope=SCOPE_ROOT)

            try:
                if action == "url":
                    # Back to the box, not to this menu. After a URL is dealt with,
                    # the step behind it is the box it was typed into - that is
                    # where the next link goes, and Back from the box itself is what
                    # returns here. A series URL has its own episode list to go back
                    # to; a film has nothing between the finished command and this
                    # prompt, which is why Back from it used to skip a level.
                    #
                    # Only when a person is answering: a rule table answers the same
                    # way every time, and "ask again" against one of those is a loop
                    # with no way out.
                    while True:
                        target = yield ctx.text("Enter URL or content ID")
                        text = str(target or "").strip()
                        if not text:
                            break  # nothing typed reads as "that is all"
                        try:
                            yield from self.open_url(ctx, text)
                        except Back:
                            # Delivery completion is represented by Back at the
                            # emit point. A title flow with no deeper picker lands
                            # here, at the box that opened it, not at the service
                            # menu one level above.
                            continue
                        if not ctx.interactive:
                            break
                elif action == "live":
                    yield from self.live(ctx)
                elif action == "search":
                    # Same shape as the URL box above: the step behind a search is
                    # the box you typed the search into, which is where the next
                    # term goes.
                    note: list[str] = []
                    while True:
                        query = yield ctx.text("Search", lines=note)
                        wanted = str(query or "").strip()
                        if not wanted:
                            break
                        try:
                            found = yield from self._did_anything(self.search(ctx, wanted))
                        except Back:
                            # Search results that do not own a deeper browse loop
                            # still return to the search box after delivery.
                            continue
                        # Carried on the next box rather than put in a panel of its
                        # own. Seventy services write "found nothing for X" as a log
                        # line and return, which scrolls past in a pane that may be
                        # collapsed while the screen goes back to the menu - so the
                        # answer to "did that work" was "the menu is back". A panel
                        # was the first fix and the wrong one: the box comes straight
                        # back, and mounting it is what clears a panel. Saying it
                        # *on* the box puts it where the next term is about to be
                        # typed, and it goes when something is found.
                        note = (
                            []
                            if found
                            else [
                                f"Nothing found for {wanted!r}.",
                                f"{self.NAME} has no match for that - try fewer words, "
                                "or the title in its original language.",
                            ]
                        )
                        if not ctx.interactive:
                            break
                elif action == "library":
                    yield from self.library(ctx)
                elif action == "login":
                    yield from self.begin_login(ctx)
                    after = self._auth_status_quietly()
                    if after.logged_in:
                        ctx.log(f"signed in: {after.label}", "ok")
                    elif after.anonymous_ok:
                        ctx.log(f"not signed in; anonymous browsing remains available: {after.label}", "info")
                    else:
                        ctx.warn(f"still not signed in: {after.label}")
                elif action == "cookies":
                    yield from self.cookie_login(ctx)
                elif action == "forget":
                    yield from self.forget_login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("signed out", "ok")
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except NotImplementedError as exc:
                ctx.warn(str(exc))
            except RuntimeError as exc:
                # A service saying no is not the end of the session. An expired
                # login, a licence the CDM was refused, a title this region does
                # not carry: that branch is over, this menu is not. It matters
                # because the advice these failures carry - "sign in again from
                # this service's menu" - can only be followed if the menu is
                # where you end up, and because losing a whole session to one
                # unavailable episode is a worse answer than saying so.
                #
                # ``RuntimeError``, not ``Exception``: it is the line between the
                # situation being wrong and the code being wrong. A TypeError or
                # an AttributeError still ends the session, with a panel and a
                # traceback, because that is a bug and a bug behind a friendly
                # menu stays a bug. Every service error class in the tree derives
                # from RuntimeError, and so do core's CdmError and HelperError.
                ctx.problem(
                    f"{self.NAME} could not finish that",
                    f"{type(exc).__name__}: {exc}",
                    "The service menu is still open - the rest of the session is fine.",
                )
                if self.settings is not None and self.settings.get("debug"):
                    import traceback

                    for line in traceback.format_exc().splitlines():
                        ctx.log(line)

    @staticmethod
    def _did_anything(flow: Iterator[Ask]) -> Iterator[Ask]:
        """Pass a sub-flow through, and return whether it produced anything.

        ``yield from`` would be shorter and cannot count. The answers have to be
        forwarded by hand - a sub-flow that never receives what its asks were
        answered with is a sub-flow that asks the same question for ever.

        "Anything" is one ask or one playback: a service that found something either
        shows a list or hands a title straight over, and one that found nothing does
        neither. That is a proxy rather than a declaration, but it is the only
        question core can ask without seventy services agreeing on a return value.
        """
        produced = 0
        answer = None
        try:
            while True:
                try:
                    ask = flow.send(answer)
                except StopIteration:
                    return produced > 0
                produced += 1
                answer = yield ask
        finally:
            flow.close()

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        raise NotImplementedError(f"{self.NAME} does not support opening URLs")

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        raise NotImplementedError(f"{self.NAME} does not support search")

    def live(self, ctx: FlowContext) -> Iterator[Ask]:
        raise NotImplementedError(f"{self.NAME} does not support live TV")

    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        raise NotImplementedError(f"{self.NAME} does not support a library")

    # --------------------------------------------------------------- playback
    def get_playback(self, title: Title) -> Playback:
        raise NotImplementedError

    def manifest_variants(
        self,
        playback: Playback,
        log: Callable[[str], None],
    ) -> list[Playback]:
        """Return additional authorized playbacks for a merged quality ladder.

        Core calls this only when ``playback.merge_manifests`` is true.  The
        service owns every API request and translates each selected service-level
        resolution/profile into a complete :class:`Playback`; Core then parses,
        merges and deduplicates their tracks. Returning URLs alone is deliberately
        unsupported because each track must retain its short-lived DRM
        authorization. Variants must nevertheless share delivery headers, proxy
        and parser policy: the native downloader has one transport policy per
        combined delivery and Core rejects incompatible variants instead of
        silently applying the primary manifest's policy to all of them.

        This is separate from Track output selection: that shared setting chooses
        from the combined ladder after it exists and must never be reused as an API
        profile or a licence-track selector.
        """
        del playback, log
        return []

    def manifest_segments(
        self,
        playback: Playback,
        log: Callable[[str], None],
    ) -> Iterable[Playback]:
        """Yield later authorized windows for a finite media timeline.

        Some catch-up APIs return only a short window around a requested play
        position. The service owns those follow-up API calls; Core parses each
        result immediately and the backend joins their absolute media sequence.
        """
        del playback, log
        return ()

    def save_name(self, title: Title) -> str:
        """Override only when ``USES.naming == 'self'``."""
        return save_name_for(title, self.name_templates())

    def name_templates(self) -> dict[str, str]:
        """The shapes the user has chosen for a file name, if any.

        Read here rather than inside naming so that module stays a pure function of
        its arguments: it is used by the checks and by the snapshot tool, neither of
        which has a settings scope.
        """
        # Asked for by name rather than called directly: a service's settings is a
        # Settings scope in the application and a plain dict in several checks and
        # harnesses, and a name is not worth an AttributeError at the last step of a
        # real download.
        read = getattr(self.settings, "inherited", None)
        if not callable(read):
            return {}
        return {
            "episode": str(read("name_template_episode", "") or ""),
            "movie": str(read("name_template_movie", "") or ""),
        }

    # -------------------------------------------------------------------- drm
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Where the Widevine challenge goes. **Every service implements its own.**

        There is deliberately no shared implementation. A licence request is one of a
        service's own calls: its URL, its headers, its token, its way of saying no.
        A default that posted to whatever URL it was handed worked for the simple
        cases and then hid the interesting ones - a refusal it could not explain,
        a header it did not know to send - behind code that belonged to nobody.

        Core still owns the *CDM*: it loads the device, builds this challenge and
        reads the keys out of the answer. Only the request in between is here.
        """
        raise CdmError(
            f"{self.NAME} does not implement get_license(). A service that needs a "
            "licence makes its own request - see its api module for where the other "
            "calls live."
        )

    def prepare_drm(self, playback: Playback, tracks, log: Callable[[str], None]) -> None:
        """Let a service choose exact init data before core opens a CDM.

        Most services leave this alone and core reads the manifest. It exists for
        services whose API contract deliberately selects a particular rendition's
        PSSH independently of the tracks downloaded later. Such a service may
        use a configured media playlist as the licence seed when its verified
        response covers the required content keys.

        ``tracks`` is Core's encrypted licence inventory: the full parsed ladder
        by default, or the selected encrypted tracks when the user explicitly
        enabled post-selection compatibility mode. The hook must not read shared
        output settings itself. It selects init data only; Core still owns the CDM
        and every network licence exchange still goes through this service's
        ``get_license``.
        """
        del playback, tracks, log

    def prepare_download(self, playback: Playback, log: Callable[[str], None]) -> None:
        """Materialize service-owned sidecars immediately before downloading.

        Command, list and export modes never call this hook. A service can keep
        remote subtitle references on ``Playback`` for reporting, then download
        only its selected references here when a file will actually be fetched.
        """
        del playback, log

    def release_playback(self, playback: Playback, log: Callable[[str], None]) -> None:
        """Release service-owned playback resources after one job.

        Most services return ordinary URLs and need no teardown. A service that
        creates a short-lived local transport can override this hook; it is
        called for downloads, commands, exports, skips, and failures alike.
        """
        del playback, log

    def live_key_pssh(
        self,
        playback: Playback,
        stream,
        segment,
        kid: str,
        log: Callable[[str], None],
    ) -> str | None:
        """Exact Widevine PSSH for a KID discovered during live recording.

        Returning ``None`` means automatic rotation is not supported for this
        event and lets the TUI ask for ``KEY`` / ``KID:KEY``. There is no generic
        KID-only fallback here: whether a licence server accepts one is a service
        fact, and guessing is precisely what this contract avoids.
        """
        del playback, stream, segment, kid, log
        return None

    def format_keys(self, playback: Playback, keys: list[str]) -> list[str]:
        """Format resolved keys for the downloader's manifest representation.

        DRM implementations may need one KID spelling internally and the media
        container may expose another. The default keeps the core contract
        unchanged; a service can override this when its exported manifest and
        licence response use different representations.
        """
        del playback
        return list(keys)

    def resolve_keys(
        self, playback: Playback, log: Callable[[str], None] | None = None
    ) -> list[str]:
        """Core DRM path. Only called when ``USES.drm == 'core'``.

        ``log`` is where the exchange reports what it is doing, and the engine
        passes its own so those lines reach the screen. It used to be discarded
        here, which hid the one thing worth watching on a PlayReady title: how many
        licence requests were made and which key ids came back.

        Which system runs is decided by the registry, not by a branch here. That
        matters beyond tidiness: this used to read ``if drm.is_playready``, which
        made "everything else" mean Widevine, so a third system would have been
        handed to pywidevine and failed somewhere deep with a message about a
        PSSH it was never given.
        """
        drm = playback.drm
        if drm is None or not drm.needs_license:
            return []

        system = drm_registry.require(drm.system or WIDEVINE)

        # What this playback asked for, else what this service's own settings say,
        # else - by passing nothing on - whatever is set app-wide. The service
        # setting is deliberately ahead of the main screen's picker: it names a
        # device *for this service*, which is a narrower statement.
        requested = drm.cdm or self.cdm_choice(system.id) or None

        # A remote CDM is checked before anything local. It needs no library on
        # this machine - that is most of the point of one - so the availability
        # check below would refuse a working setup, and the local resolver would
        # either fail on a name that is not a path or quietly fall through to
        # whatever other device happens to be sitting on disk.
        remote = self.ctx.config.remote_cdm_for(
            self.ID, requested, system=system.id, legacy_ids=self.LEGACY_IDS
        )
        if remote is not None:
            return self._remote_keys(system, remote, drm, log)

        if not system.available:
            raise CdmError(
                f"{system.label} is selected but not installed.\n    {system.install_hint}"
            )

        # Resolved here rather than reused from ctx: ctx.device_path was worked
        # out when the service was built, before anything knew which DRM system
        # this playback would use.
        device_path = self._device_for_drm(system.id, requested)
        if device_path is None:
            # A remote CDM for the *other* system resolves to no local path, and
            # "no CDM device configured" is a bad description of a machine with
            # one configured that simply cannot answer this request.
            chosen = self.ctx.config.device_name_for(
                self.ID, requested, legacy_ids=self.LEGACY_IDS
            )
            elsewhere = self.ctx.config.remote_cdm(chosen) if chosen else None
            if elsewhere is not None:
                other = drm_registry.get(elsewhere.system)
                raise CdmError(
                    f"{chosen} is a remote {other.label if other else elsewhere.system} "
                    f"CDM, and this playback needs {system.label}. Choose a "
                    f"{system.label} device in this service's settings, or with ^o "
                    f"on the main screen, or add a remote CDM for {system.label} to "
                    "unidl.yaml."
                )
            raise CdmError("No CDM device configured (set cdm.default in unidl.yaml)")
        found = system_of(device_path)
        if found and found != system.id:
            # Say this before the exchange. Handing a .wvd to pyplayready
            # produced "No PlayReady header was accepted: Could not load x.wvd",
            # which blames the headers for a device that was never going to work.
            other = drm_registry.get(found)
            raise CdmError(
                f"{system.label} is selected, but {device_path.name} is a "
                f"{other.label if other else found} device. Choose a "
                f"{system.suffix} device in this service's settings, or with ^o on "
                "the main screen, or point cdm.default at one."
            )

        init_data = drm_registry.init_data_for(drm, system.id)
        if not init_data:
            raise CdmError(system.missing_init)
        return system.get_keys(
            drm_registry.Exchange(
                device=device_path,
                init_data=init_data,
                drm=drm,
                service=self,
                log=log or (lambda _message: None),
            )
        )

    def _remote_keys(
        self, system, remote, drm: DrmInfo, log: Callable[[str], None] | None = None
    ) -> list[str]:
        """The same exchange, against a CDM on a server.

        The licence request itself still happens here, from this machine, with
        this service's headers and cookies - only the challenge and the licence
        parsing are done at the other end. That is what makes a remote CDM usable
        for a service whose licence server checks the caller: the licence server
        never sees the remote CDM's address.
        """
        if not system.remote_capable:
            raise CdmError(
                f"{remote.name} is a remote CDM, but {system.label} has no remote "
                "form - it decrypts locally, so there is nothing to ask a server for."
            )
        init_data = drm_registry.init_data_for(drm, system.id)
        if not init_data:
            raise CdmError(system.missing_init)
        return system.remote_keys(
            drm_registry.Exchange(
                device=None,
                remote=remote,
                init_data=init_data,
                drm=drm,
                service=self,
                log=log or (lambda _message: None),
            )
        )

    def get_license_soap(self, challenge: str, drm: DrmInfo) -> str:
        """Where the PlayReady challenge goes. **Every service implements its own.**

        The same rule as :meth:`get_license`, and for the same reason. PlayReady
        differs only in what travels: a SOAP envelope out, XML back.
        """
        raise CdmError(
            f"{self.NAME} does not implement get_license_soap(). A service that needs "
            "a PlayReady licence makes its own request."
        )

    def get_keys(self, playback: Playback) -> list[str]:
        """Coarse interface used when ``USES.drm == 'self'``."""
        raise NotImplementedError(f"{self.NAME} declares drm='self' but has no get_keys()")


# --------------------------------------------------------------------- registry


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, type[Service]] = {}
        self._replacement_modules: set[str] = set()
        # A developer reload happens while readiness/account workers may still
        # read the registry. Replacing a class is atomic; iterating a dictionary
        # whose size changes is not. The reloader holds this for its whole
        # transaction and ordinary readers take it only long enough to copy/read.
        self._lock = RLock()

    def register(self, service: type[Service]) -> type[Service]:
        service_id = str(service.ID or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", service_id):
            raise ValueError(
                f"{service.__name__} has invalid ID {service.ID!r}; use lower-case letters, "
                "numbers, underscores or hyphens"
            )
        with self._lock:
            previous = self._services.get(service_id)
            replacing = service.__module__ in self._replacement_modules
            if previous is not None and previous is not service and not replacing:
                raise ValueError(
                    f"duplicate service ID {service_id!r}: "
                    f"{previous.__module__}.{previous.__name__} and "
                    f"{service.__module__}.{service.__name__}"
                )
            self._services[service_id] = service
        return service

    def replace(self, service: type[Service]) -> type[Service]:
        """Explicit replacement seam for developer reloads and their checks."""
        service_id = str(service.ID or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", service_id):
            raise ValueError(f"{service.__name__} has invalid ID {service.ID!r}")
        with self._lock:
            if service_id not in self._services:
                raise KeyError(f"cannot replace unknown service {service_id!r}")
            self._services[service_id] = service
        return service

    @contextmanager
    def replacement_scope(self, modules: set[str]):
        """Allow decorators executed by a prepared developer reload to replace."""
        with self._lock:
            previous = set(self._replacement_modules)
            self._replacement_modules.update(modules)
            try:
                yield
            finally:
                self._replacement_modules = previous

    def validate(self, config: Config | None = None) -> None:
        """Reject declarations whose meaning depends on import or list order."""
        services = self.all()
        problems: list[str] = []
        identities: dict[str, list[type[Service]]] = {}
        aliases: dict[str, list[type[Service]]] = {}
        for service in services:
            for identity in (service.ID, service.NAME):
                identities.setdefault(str(identity).strip().lower(), []).append(service)
            for alias in service.ALIASES:
                aliases.setdefault(str(alias).strip().lower(), []).append(service)
            specs = service.setting_specs(config)
            keys = [spec.key for spec in specs]
            repeated = sorted({key for key in keys if keys.count(key) > 1})
            if repeated:
                problems.append(f"{service.ID}: duplicate setting key(s) {', '.join(repeated)}")

        for alias, owners in aliases.items():
            unique = list(dict.fromkeys(owners))
            if len(unique) < 2:
                continue
            exact = list(dict.fromkeys(identities.get(alias, [])))
            if len(exact) != 1:
                problems.append(
                    f"alias {alias!r} is ambiguous between "
                    + ", ".join(service.ID for service in unique)
                )
        if problems:
            raise ValueError("invalid service declarations: " + "; ".join(problems))

    def all(self) -> list[type[Service]]:
        with self._lock:
            services = list(self._services.values())
        return sorted(services, key=lambda s: s.NAME.lower())

    def get(self, key: str) -> type[Service] | None:
        """Resolve an id, an alias or a service tag.

        In that order, because a derived tag can collide with another service's
        and must never shadow an exact name. Among tags, a declared one wins over
        a derived one for the same reason.
        """
        key = key.strip().lower()
        if not key:
            return None
        with self._lock:
            if key in self._services:
                return self._services[key]
            services = list(self._services.values())
        named = [service for service in services if service.NAME.strip().lower() == key]
        if len(named) == 1:
            return named[0]
        for service in services:
            if key in {alias.lower() for alias in service.ALIASES}:
                return service
        declared = [s for s in services if s.TAG and s.TAG.lower() == key]
        if declared:
            return declared[0]
        derived = [s for s in services if s.tag().lower() == key]
        if derived:
            return sorted(derived, key=lambda s: s.NAME.lower())[0]
        return None

    def by_tag(self, tag: str) -> list[type[Service]]:
        """Every service answering to ``tag``, so a collision is visible."""
        wanted = (tag or "").strip().lower()
        return [s for s in self.all() if s.tag().lower() == wanted] if wanted else []

    def for_url(self, text: str) -> type[Service] | None:
        with self._lock:
            services = list(self._services.values())
        for service in services:
            if service.matches_url(text):
                return service
        return None

    def build(
        self,
        service_cls: type[Service],
        config: Config,
        store: SettingsStore,
        *,
        overrides: dict[str, Any] | None = None,
        globals_scope: Settings | None = None,
        profile: str = "",
    ) -> Service:
        settings = Settings(
            service_cls.ID,
            # with the config, so the CDM choices offer the devices that exist on
            # this machine rather than an empty list
            service_cls.setting_specs(config),
            store,
            overrides,
            parent=globals_scope,
            legacy_ids=service_cls.LEGACY_IDS,
        )
        ctx = ServiceContext(
            config=config,
            settings=settings,
            # A service's own folder under ``paths.tokens``, and nothing else. Its
            # own, because a hundred sessions in one directory is a pile rather than
            # a layout: "where is my service login" should be answerable by looking,
            # and a service cannot reach another's state by naming its file. And
            # only that, because ``cache`` is for what unidl can fetch again and
            # nothing outside the project is read at all.
            tokens=TokenStore(
                config.paths.tokens / service_cls.ID,
                legacy_dirs=[config.paths.tokens / legacy for legacy in service_cls.LEGACY_IDS],
            ),
            service_id=service_cls.ID,
            device_path=config.device_for(
                service_cls.ID, legacy_ids=service_cls.LEGACY_IDS
            ),
            device_name=config.device_name_for(
                service_cls.ID, legacy_ids=service_cls.LEGACY_IDS
            ),
            # Resolved here so there is one answer to "which route does this service
            # use": ctx.session() applies it to everything the service asks for, and
            # the same value goes onto every Playback, which is what carries it into
            # core's own manifest reads and into the download.
            proxy=proxy_for(config, settings),
            helpers=HelperResolver(config).report(
                service_cls.ID,
                service_cls.HELPERS,
                legacy_ids=service_cls.LEGACY_IDS,
            ),
            profile=profile,
            legacy_service_ids=service_cls.LEGACY_IDS,
        )
        service = service_cls(ctx)
        # Now that there is a service to ask, re-resolve: the device above was the
        # app-wide one, and this service's own settings may name another. Doing it
        # here rather than in every screen keeps one answer to "which CDM".
        service.refresh_device()
        return service

    def helper_report(self, service_cls: type[Service], config: Config) -> HelperReport:
        """Helper state without building the service, for the main screen."""
        return HelperResolver(config).report(
            service_cls.ID,
            service_cls.HELPERS,
            legacy_ids=service_cls.LEGACY_IDS,
        )


registry = ServiceRegistry()
