"""Reference scaffold for a native UniDL service.

This package is documentation-by-example. It is deliberately excluded from the
normal service scan and therefore never appears in the platform list until a
provider has been implemented and registered. Replace the ``example.invalid``
API calls and placeholder identifiers before using it. A build may register the
class with ``@registry.register``; a user-installed copy can instead be enabled
through **Settings → Services → Register a service**, whose loader discovers
the class after restart.

The split is intentional:

* :mod:`api` owns HTTP, authentication headers, response validation and provider
  payload parsing.
* this module owns the UniDL Flow, settings and the :class:`~unidl.core.playback.Playback`
  handoff.
* Core owns manifest parsing, track selection, CDM challenge creation, key
  extraction, downloading, decryption, subtitles and muxing.
"""

from __future__ import annotations

from collections.abc import Iterator

from ...core.flow import Ask, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import Capabilities, Service, ServiceContext
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_MANIFEST_PROFILE = Setting(
    key="manifest_profile",
    label="Manifest profile",
    options=[
        Option("hd", "HD source profile"),
        Option("uhd", "UHD source profile"),
    ],
    default="hd",
    help=(
        "Provider profile used to request a manifest. This is separate from "
        "the shared Track output selection."
    ),
    resets_session=True,
)
_LICENSE_PROFILE = Setting(
    key="license_profile",
    label="License profile",
    options=[
        Option("default", "Provider default license context"),
        Option("uhd", "UHD license context"),
    ],
    default="default",
    help=(
        "Service-owned license context. It is not the shared output-track "
        "quality, codec or range selector."
    ),
    resets_session=True,
)


class Example(Service):
    """A copyable skeleton for a VOD/search service.

    The class intentionally has no ``@registry.register`` decorator. A build
    may add that decorator (or call ``registry.register(Example)``); a user
    installation can leave it absent and enable the package from the Services
    manager after copying it into the services directory.
    """

    ID = "example"
    NAME = "Example Service"
    TAG = "EX"
    ALIASES = ("example", "exampletv")
    TITLE_RE = r"(?:^|\.)example\.invalid/"
    GEOFENCE = ()
    DESCRIPTION = "Reference service template; replace the example API before use."
    MEDIA_TYPES = ("video",)

    USES = Capabilities()
    DRM_SYSTEMS = ("widevine",)
    SETTINGS = [_MANIFEST_PROFILE, _LICENSE_PROFILE]

    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIVE = False
    SUPPORTS_LIBRARY = False

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        # ServiceContext.session supplies the selected proxy and managed cookies.
        # No request is made until a flow calls one of the API methods.
        self.api = api.ExampleApi(
            session=ctx.session(user_agent=api.USER_AGENT),
            base_url=api.EXAMPLE_ORIGIN,
        )

    # ---------------------------------------------------------------- browsing
    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        """Resolve one provider URL or content ID and emit a Playback."""
        try:
            item = self.api.resolve(target)
        except api.ExampleApiError as exc:
            ctx.error(str(exc))
            return
        if item is None:
            ctx.warn(f"Example Service found no title for {target}")
            return
        yield ctx.emit(self._playback(item))

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        """Search through the API and keep the normal Core picker semantics."""
        try:
            ctx.status(f"Searching Example Service for {query}")
            hits = self.api.search(query)
        except api.ExampleApiError as exc:
            ctx.error(str(exc))
            return
        if not hits:
            ctx.warn(f"Example Service found nothing for {query}")
            return
        chosen = yield ctx.pick(
            f"Example Service  ·  {query}",
            [
                Choice(
                    item.title,
                    item,
                    detail=item.year or "",
                    tags=(item.kind,),
                )
                for item in hits
            ],
        )
        if chosen is not None:
            yield ctx.emit(self._playback(chosen))

    # --------------------------------------------------------------- playback
    def _playback(self, item: api.ExampleItem) -> Playback:
        """Translate one fully authorized API item into the Core contract."""
        if not item.manifest_url:
            raise api.ExampleApiError(f"Example title {item.id!r} has no manifest URL")

        kind = {
            "movie": TitleKind.MOVIE,
            "episode": TitleKind.EPISODE,
            "track": TitleKind.TRACK,
        }.get(item.kind, TitleKind.MOVIE)
        title = Title(
            id=item.id,
            kind=kind,
            name=item.title,
            year=item.year,
            season=item.season,
            episode=item.episode,
            episode_name=item.episode_name,
            service=self.ID,
            data={"example_item_id": item.id},
        )

        drm = None
        if item.encrypted:
            if not item.license_url:
                raise api.ExampleApiError(
                    f"Example title {item.id!r} is encrypted but returned no license URL"
                )
            drm = DrmInfo(
                system=self.drm_system(),
                license_url=item.license_url,
                headers=dict(item.license_headers),
                context={
                    "title_id": item.id,
                    "license_profile": str(self.settings.get("license_profile") or "default"),
                },
            )

        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=item.manifest_url,
            headers=dict(item.headers),
            proxy=self.ctx.proxy,
            drm=drm,
            note="Example Service playback scaffold",
        )

    # ------------------------------------------------------------------- DRM
    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Send a Widevine challenge through this service's own API client.

        Core creates the challenge and extracts keys.  The service owns only the
        provider URL, headers, body and response transport.  A real service with
        PlayReady should implement ``get_license_soap`` separately rather than
        sending a SOAP request through a shared default implementation.
        """
        if not drm.license_url:
            raise api.ExampleApiError("Example license URL is missing")
        return self.api.post_license(
            drm.license_url,
            challenge,
            headers=drm.headers,
        )


__all__ = ["Example", "api"]
