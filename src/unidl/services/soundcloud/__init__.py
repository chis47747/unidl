"""SoundCloud tracks, albums, playlists and pairing-code account access."""

from __future__ import annotations

import time
from collections.abc import Iterator

from ...core.flow import SCOPE_ROOT, Ask, Back, Choice, FlowContext
from ...core.playback import DrmInfo, Playback
from ...core.service import AuthStatus, Capabilities, Service, registry
from ...core.settings import Option, Setting
from ...core.titles import Title, TitleKind
from . import api

_AUDIO_QUALITY = Setting(
    "audio_quality",
    "SoundCloud source quality",
    options=[
        Option("auto", "Auto · highest available for downloads"),
        Option("best", "High · AAC up to 256 Kbps"),
        Option("standard", "Standard · AAC up to 160 Kbps"),
    ],
    default="auto",
    help=(
        "Selects a native SoundCloud transcoding. High quality is normally AAC 256 Kbps "
        "when the account and track provide it; this is not lossless audio."
    ),
)

_AUDIO_FORMAT = Setting(
    "audio_format",
    "SoundCloud audio output",
    options=[
        Option("auto", "Auto · AAC/MP3 to MP3"),
        Option("source", "Keep the source format"),
        Option("alac", "ALAC in M4A"),
        Option("flac", "FLAC (lossless container)"),
        Option("m4a", "M4A (preserve source codec when possible)"),
        Option("mp3", "MP3 320 Kbps"),
    ],
    default="auto",
    help=(
        "FLAC and ALAC are final UniDL conversions from SoundCloud's AAC/MP3 source. "
        "They do not create native lossless or spatial audio."
    ),
)


@registry.register
class SoundCloud(Service):
    ID = "soundcloud"
    NAME = "SoundCloud"
    MEDIA_TYPES = ("audio",)
    TAG = "SC"
    ALIASES = ("soundcloud.com", "on.soundcloud.com", "sc")
    TITLE_RE = r"(?:www\.|m\.|on\.)?soundcloud\.com/"
    DESCRIPTION = "SoundCloud tracks, albums, playlists, profiles and native audio streams."

    USES = Capabilities()
    SETTINGS = [_AUDIO_QUALITY, _AUDIO_FORMAT]
    SUPPORTS_URL = True
    SUPPORTS_SEARCH = True
    SUPPORTS_LIBRARY = True
    SUPPORTS_LIVE = False
    TOKEN_FILE = api.TOKEN_FILE

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._state = api.TokenState.from_cache(self.ctx.tokens.read(self.TOKEN_FILE))
        self._api: api.SoundCloudApi | None = None

    # ------------------------------------------------------------------ auth
    def _save_state(self, state: api.TokenState) -> None:
        self.ctx.tokens.write(self.TOKEN_FILE, state.to_cache())

    def auth_status(self) -> AuthStatus:
        if self._state.logged_in:
            detail = "refreshable pairing session" if self._state.refresh_token else "pairing session"
            return AuthStatus(
                True,
                self._state.username or "SoundCloud account",
                f"{detail} · {self.TOKEN_FILE}",
            )
        return AuthStatus(False, "Pairing-code sign-in required", self.TOKEN_FILE)

    def login(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self._new_api()
        try:
            ctx.status("Requesting a SoundCloud TV code")
            challenge = client.begin_pairing()
        except api.SoundCloudError as exc:
            ctx.error(str(exc))
            return

        next_poll = 0.0

        def poll():
            nonlocal next_poll
            now = time.monotonic()
            if now < next_poll:
                return None
            result = client.poll_pairing(challenge)
            next_poll = now + challenge.interval
            return result

        try:
            state = yield ctx.wait_for(
                "Sign in to SoundCloud",
                [("Code", challenge.code), ("Open", challenge.verification_uri)],
                poll,
                timeout=float(challenge.expires_in),
                interval=1.0,
                hint="Open the activation page, enter the TV code, and approve this session.",
            )
            if not isinstance(state, api.TokenState) or not state.logged_in:
                ctx.error("SoundCloud pairing sign-in completed without usable tokens")
                return
            self._state = state
            self._api = client
            profile = client.profile()
        except api.SoundCloudError as exc:
            ctx.error(str(exc))
            return
        ctx.log(f"SoundCloud sign-in completed · {profile.username}", "ok")

    def logout(self) -> None:
        self.ctx.tokens.remove(self.TOKEN_FILE)
        device_id = self._state.device_id
        self._state = api.TokenState(device_id=device_id)
        self._api = None
        super().logout()

    def _new_api(self) -> api.SoundCloudApi:
        client = api.SoundCloudApi(
            self.ctx.session(user_agent=api.USER_AGENT, cookies=False),
            self._state,
            save_state=self._save_state,
        )
        self._api = client
        return client

    def client(self) -> api.SoundCloudApi:
        client = self._api or self._new_api()
        if self._state.logged_in:
            return client
        if self._state.refresh_token:
            client.refresh()
            return client
        raise api.SoundCloudAuthError("SoundCloud is not signed in; choose Sign in and approve the pairing code first")

    # --------------------------------------------------------------- navigation
    def home(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            status = self.auth_status()
            choices = [
                Choice("Browse home recommendations", "browse"),
                Choice("Open a track, album or playlist URL", "url"),
                Choice("Search", "search"),
                Choice("My library", "library"),
                Choice("Settings", "settings"),
            ]
            if status.logged_in:
                choices.append(Choice("Sign in again", "login", detail=status.label))
                choices.append(Choice("Sign out", "logout", detail=status.label))
            else:
                choices.insert(0, Choice("Sign in", "login", detail=status.label))
            action = yield ctx.pick("", choices, scope=SCOPE_ROOT)
            try:
                if action == "login":
                    yield from self.login(ctx)
                elif action == "logout":
                    self.logout()
                    ctx.log("signed out", "ok")
                elif action == "browse":
                    yield from self.browse(ctx)
                elif action == "url":
                    yield from self._url_prompt(ctx)
                elif action == "search":
                    yield from self._search_prompt(ctx)
                elif action == "library":
                    yield from self.library(ctx)
                elif action == "settings":
                    yield ctx.settings_request()
            except Back:
                continue
            except api.SoundCloudError as exc:
                ctx.error(str(exc))

    def _url_prompt(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            target = yield ctx.text(
                "SoundCloud URL",
                placeholder="https://soundcloud.com/artist/track",
            )
            wanted = str(target or "").strip()
            if not wanted:
                return
            try:
                yield from self.open_url(ctx, wanted)
            except Back:
                pass
            except api.SoundCloudError as exc:
                ctx.error(str(exc))
            if not getattr(ctx, "interactive", False):
                return

    def _search_prompt(self, ctx: FlowContext) -> Iterator[Ask]:
        while True:
            query = yield ctx.text(
                "Search SoundCloud",
                placeholder="artist, track, album or playlist",
            )
            wanted = str(query or "").strip()
            if not wanted:
                return
            try:
                yield from self.search(ctx, wanted)
            except Back:
                pass
            if not getattr(ctx, "interactive", False):
                return

    # --------------------------------------------------------------- catalogue
    def browse(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        ctx.status("Loading SoundCloud home recommendations")
        sections = client.home_sections()
        if not sections:
            ctx.warn("SoundCloud returned no home recommendations")
            return
        choices = [Choice(section.title, section, detail=f"{len(section.items)} item(s)") for section in sections]
        while True:
            try:
                selected = yield ctx.pick("SoundCloud home", choices)
            except Back:
                return
            if not isinstance(selected, api.HomeSection):
                return
            try:
                yield from self._browse_hits(ctx, client, selected.title, selected.items)
            except Back:
                pass
            if not getattr(ctx, "interactive", False):
                return

    def open_url(self, ctx: FlowContext, target: str) -> Iterator[Ask]:
        client = self.client()
        ctx.status("Resolving SoundCloud URL")
        hit = client.resolve(api.parse_target(target))
        yield from self._open_hit(ctx, client, hit)

    def search(self, ctx: FlowContext, query: str) -> Iterator[Ask]:
        wanted = str(query or "").strip()
        if not wanted:
            return
        try:
            client = self.client()
            ctx.status(f"Searching SoundCloud for {wanted}")
            results = client.search(wanted)
        except api.SoundCloudError as exc:
            ctx.error(str(exc))
            return
        if not results:
            ctx.warn(f"SoundCloud found no results for {wanted!r}")
            return
        yield from self._browse_hits(ctx, client, f"SoundCloud search · {wanted}", results)

    def _browse_hits(
        self,
        ctx: FlowContext,
        client: api.SoundCloudApi,
        title: str,
        items: tuple[api.SearchHit, ...],
    ) -> Iterator[Ask]:
        choices = [Choice(item.title, item, detail=item.detail, tags=(item.kind,)) for item in items]
        while True:
            try:
                selected = yield ctx.pick(title, choices)
            except Back:
                return
            if not isinstance(selected, api.SearchHit):
                return
            try:
                yield from self._open_hit(ctx, client, selected)
            except Back:
                pass
            except api.SoundCloudError as exc:
                ctx.error(str(exc))
            if not getattr(ctx, "interactive", False):
                return

    def _open_hit(
        self,
        ctx: FlowContext,
        client: api.SoundCloudApi,
        hit: api.SearchHit,
    ) -> Iterator[Ask]:
        if hit.kind == "track":
            try:
                track = api.track_from_json(hit.raw)
            except api.SoundCloudError:
                track = client.track(hit.urn)
            if not track.transcodings:
                track = client.track(hit.urn)
            yield from self._pick_tracks(ctx, client, track.title, (track,), track.cover_url, collection=track)
            return
        if hit.kind in {"album", "playlist"}:
            playlist = client.playlist(hit.urn)
            yield from self._pick_tracks(
                ctx,
                client,
                playlist.title,
                playlist.tracks,
                playlist.cover_url,
                collection=playlist,
            )
            return
        if hit.kind == "user":
            try:
                user = api.user_from_json(hit.raw)
            except api.SoundCloudError:
                user = client.user(hit.urn)
            yield from self._open_user(ctx, client, user)
            return
        raise api.SoundCloudError(f"Unsupported SoundCloud result type: {hit.kind}")

    def _open_user(
        self,
        ctx: FlowContext,
        client: api.SoundCloudApi,
        user: api.User,
    ) -> Iterator[Ask]:
        choices = [
            Choice("Tracks", "tracks", detail=f"{user.tracks_count} posted" if user.tracks_count else "Posted tracks"),
            Choice("Albums and singles", "albums"),
            Choice("Playlists", "playlists"),
        ]
        while True:
            try:
                category = yield ctx.pick(user.username, choices)
            except Back:
                return
            if category not in {"tracks", "albums", "playlists"}:
                return
            ctx.status(f"Loading {user.username} · {category}")
            items = client.user_items(user.urn, category)
            if not items:
                ctx.warn(f"{user.username} returned no {category}")
            else:
                try:
                    yield from self._browse_hits(ctx, client, f"{user.username} · {category}", items)
                except Back:
                    pass
            if not getattr(ctx, "interactive", False):
                return

    def library(self, ctx: FlowContext) -> Iterator[Ask]:
        client = self.client()
        choices = [
            Choice("Tracks and reposts", "tracks"),
            Choice("Playlists and albums", "playlists"),
            Choice("Recently played", "recent"),
        ]
        while True:
            try:
                category = yield ctx.pick("SoundCloud library", choices)
            except Back:
                return
            if category not in {"tracks", "playlists", "recent"}:
                return
            ctx.status(f"Loading SoundCloud library · {category}")
            items = client.library_items(category)
            if not items:
                ctx.warn(f"SoundCloud returned no {category}")
            else:
                try:
                    yield from self._browse_hits(ctx, client, f"SoundCloud library · {category}", items)
                except Back:
                    pass
            if not getattr(ctx, "interactive", False):
                return

    def _pick_tracks(
        self,
        ctx: FlowContext,
        client: api.SoundCloudApi,
        title: str,
        tracks: tuple[api.Track, ...],
        cover_url: str,
        *,
        collection: api.Playlist | api.Track | None = None,
    ) -> Iterator[Ask]:
        playable = tuple(track for track in tracks if not track.blocked and track.has_full_stream)
        if not playable:
            ctx.warn(f"{title} returned no full-length tracks for this account; previews are not downloaded")
            return
        quality = str(self.settings.get("audio_quality") or "auto")
        choices = [
            Choice(
                track.label,
                track,
                detail=" · ".join(filter(None, (track.artist, self._track_quality(track, quality)))),
                tags=("explicit",) if track.explicit else (),
            )
            for track in playable
        ]
        preview = {
            "cover_source": cover_url or playable[0].cover_url,
            "cover_headers": {"User-Agent": api.USER_AGENT},
            "proxy": self.ctx.proxy,
            "tags": self._preview_tags(title, playable, collection),
        }
        while True:
            try:
                picked = yield ctx.pick(
                    f"{title} · {len(playable)} track(s)",
                    choices,
                    multi=True,
                    hint="Space selects tracks; Enter continues.",
                    preview=preview,
                )
            except Back:
                return
            selected = picked if isinstance(picked, list) else [picked]
            values = [value for value in selected if isinstance(value, api.Track)]
            if not values:
                return
            try:
                if len(values) > 1:
                    ctx.batch(len(values))
                for track in values:
                    ctx.status(f"Resolving SoundCloud audio · {track.title}")
                    yield ctx.emit(self._audio_playback(track, client))
            except Back:
                pass
            except api.SoundCloudError as exc:
                ctx.error(str(exc))
            if not getattr(ctx, "interactive", False):
                return

    # --------------------------------------------------------------- playback
    def _audio_playback(self, track: api.Track, client: api.SoundCloudApi) -> Playback:
        quality = str(self.settings.get("audio_quality") or "auto")
        source = client.media_source(track, quality)
        title = self._title(track)
        requested = {
            "auto": "Auto",
            "best": "High · best available",
            "standard": "Standard",
        }.get(quality, quality)
        tags = self._audio_tags(track)
        return Playback(
            title=title,
            save_name=self.save_name(title),
            manifest_url=source.url,
            headers=client.media_headers(source),
            proxy=self.ctx.proxy,
            requested=requested,
            returned=source.quality_label,
            note=f"SoundCloud audio · {source.quality_label}",
            audio_only=True,
            audio_codec_hint=source.codec,
            audio_tags=tags,
            lyrics=None,
            drm=(
                DrmInfo(
                    system="widevine",
                    license_url=api.WIDEVINE_LICENSE_URL,
                    headers={"Content-Type": "application/octet-stream"},
                    context={
                        "soundcloud_transcoding_url": source.transcoding_url,
                        "soundcloud_license_auth_token": source.license_auth_token,
                    },
                )
                if "hls" in source.protocol and source.license_auth_token
                else None
            ),
        )

    def get_license(self, challenge: bytes, drm: DrmInfo) -> bytes:
        """Exchange a Widevine challenge using SoundCloud's APK flow."""
        client = self.client()
        context = drm.context
        transcoding_url = str(context.get("soundcloud_transcoding_url") or "").strip()
        token = str(context.get("soundcloud_license_auth_token") or "").strip()
        if not token and not transcoding_url:
            raise api.SoundCloudError("SoundCloud Widevine license authorization is unavailable")
        if not token:
            token = client.resolve_license_token(transcoding_url)
            context["soundcloud_license_auth_token"] = token
        try:
            return client.post_license(challenge, token)
        except api.SoundCloudAuthError:
            if not transcoding_url or context.get("soundcloud_license_retry"):
                raise
            context["soundcloud_license_retry"] = True
            token = client.resolve_license_token(transcoding_url)
            context["soundcloud_license_auth_token"] = token
            return client.post_license(challenge, token)

    def prepare_drm(self, playback: Playback, tracks, log) -> None:
        """Load SoundCloud's Widevine init data from the HLS init map."""
        del tracks
        drm = playback.drm
        if drm is None or drm.system != "widevine" or drm.pssh:
            return
        manifest_url = str(playback.manifest_url or "").strip()
        if not manifest_url:
            raise api.SoundCloudError("SoundCloud playback did not return an HLS manifest URL")
        client = self.client()
        init_url, init_data = client.fetch_hls_init_segment(
            manifest_url,
            headers=playback.headers,
            proxy=playback.proxy,
        )
        drm.pssh = api.widevine_pssh_from_soundcloud_init(init_data)
        if not drm.pssh:
            raise api.SoundCloudError("SoundCloud HLS init segment contains no Widevine PSSH KID")
        drm.context["soundcloud_hls_init_segment"] = True
        drm.context["soundcloud_hls_init_url"] = init_url
        log("SoundCloud: Widevine init data loaded from HLS EXT-X-MAP")

    def get_playback(self, title: Title) -> Playback:
        raw = title.data.get("soundcloud_raw") if isinstance(title.data, dict) else None
        try:
            track = api.track_from_json(raw)
        except api.SoundCloudError:
            track = self.client().track(title.id)
        if not track.transcodings:
            track = self.client().track(track.urn)
        return self._audio_playback(track, self.client())

    def _title(self, track: api.Track) -> Title:
        return Title(
            id=track.id,
            kind=TitleKind.TRACK,
            name=track.title,
            year=track.release_date[:4] or None,
            duration=track.duration or None,
            artist=track.artist or None,
            album=track.album or None,
            track_number=track.track_number or None,
            genre=track.genre or None,
            publisher=track.publisher or None,
            cover_url=track.cover_url or None,
            service=self.ID,
            data={"soundcloud_raw": track.raw},
        )

    @staticmethod
    def _audio_tags(track: api.Track) -> dict[str, object]:
        tags: dict[str, object] = {
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "album_artist": track.album_artist or track.artist,
            "date": track.release_date,
            "track": (
                f"{track.track_number}/{track.track_total}"
                if track.track_number and track.track_total
                else track.track_number
            ),
            "genre": track.genre,
            "composer": track.composer,
            "copyright": track.copyright,
            "publisher": track.publisher,
            "isrc": track.isrc,
            "comment": track.description,
            "cover": ({"url": track.cover_url, "headers": {"User-Agent": api.USER_AGENT}} if track.cover_url else None),
        }
        return {key: value for key, value in tags.items() if value not in (None, "", 0)}

    @classmethod
    def _preview_tags(
        cls,
        title: str,
        tracks: tuple[api.Track, ...],
        collection: api.Playlist | api.Track | None,
    ) -> dict[str, object]:
        first = tracks[0]
        if isinstance(collection, api.Track):
            tags = cls._audio_tags(collection)
            tags.pop("cover", None)
            return tags
        if isinstance(collection, api.Playlist):
            tags: dict[str, object] = {
                "title": collection.title,
                "artist": collection.creator,
                "album": collection.title,
                "album_artist": collection.creator,
                "date": collection.release_date,
                "genre": collection.genre or first.genre,
                "publisher": first.publisher,
                "copyright": first.copyright,
                "comment": collection.description,
            }
        else:
            tags = {
                "title": title,
                "artist": first.artist,
                "album": first.album,
                "album_artist": first.album_artist or first.artist,
                "date": first.release_date,
                "genre": first.genre,
                "publisher": first.publisher,
                "copyright": first.copyright,
            }
        return {key: value for key, value in tags.items() if value not in (None, "", 0)}

    @staticmethod
    def _track_quality(track: api.Track, requested: str) -> str:
        try:
            return api.select_transcoding(track, requested).quality_label
        except api.SoundCloudError:
            return "Unavailable"


__all__ = ["SoundCloud"]
