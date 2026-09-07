"""Output naming.

One implementation of the convention used by legacy integrations:
``Title.S01E02.Episode.Title`` / ``Title.YEAR``. Previously this was copy-pasted
per script with slight drift.

The name is built in two halves, because the two halves are known at different
times:

* the **title** half, from the :class:`~unidl.core.titles.Title` alone, as soon as
  a service resolves one. That is what pickers, queue rows and logs show.
* the **release** half, from the tracks that were actually chosen, which is not
  known until the manifest has been read and the selection has settled:
  ``.1080p.SERVICE.WEB-DL.DDP5.1.Atmos.DV-GROUP``

Ordering follows the scene convention that unshackle's default template spells
out—quality, platform, source, audio, Atmos and picture range—so a file named here
reads the same way as one named there.
"""

from __future__ import annotations

import re

from . import template
from .titles import Title, TitleKind

_MULTI_DOT = re.compile(r"\.{2,}")
_TITLE_DASH = re.compile(r"[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d]")

#: What the release name says about where the file came from. Always this: every
#: manifest here is a streaming service's own.
SOURCE = "WEB-DL"

#: The kinds that get a release half. A live capture is named for when it was
#: taken and an audio track for its artist and album; neither is a scene release,
#: and bolting the vocabulary onto them would only make the names longer.
RELEASE_KINDS = (TitleKind.MOVIE, TitleKind.EPISODE)

#: How UniDL's normalised range names are written into a file name. SDR is
#: deliberately absent: the convention is to say nothing when there is nothing
#: unusual about the picture, and "SDR" on the overwhelming majority of files is
#: noise. Matches unshackle's DYNAMIC_RANGE_MAP.
RANGE_TAGS = {
    "DV": "DV",
    "HDR10+": "HDR10P",
    "HDR10": "HDR",
    "HDR": "HDR",
    "HLG": "HLG",
}

#: Audio names use release vocabulary rather than the labels drawn by UniDL's
#: track picker. In particular, E-AC-3 is DDP and AC-3 is DD in a file name.
_AUDIO_CODEC_TAGS = {
    "AAC": "AAC",
    "HE-AAC": "AAC",
    "AC-3": "DD",
    "E-AC-3": "DDP",
    "E-AC-3 ATMOS": "DDP",
    "DTS": "DTS",
    "DTS-HD": "DTS-HD",
    "DTS:X": "DTSX",
    "OPUS": "OPUS",
    "FLAC": "FLAC",
    "MP3": "MP3",
    "TRUEHD": "TrueHD",
}

#: Used only to break ties between selected tracks with the same channel layout.
#: The chosen track must come from the user's final selection; this never reaches
#: into the unselected manifest inventory looking for a nicer name.
_AUDIO_CODEC_ORDER = {
    "MP3": 10,
    "AAC": 20,
    "OPUS": 25,
    "DD": 30,
    "DTS": 35,
    "DDP": 40,
    "AC4": 45,
    "DTS-HD": 50,
    "FLAC": 55,
    "TrueHD": 60,
    "MPEGH": 60,
    "DTSX": 65,
}

#: Strongest first. With several video tracks chosen, the file is named for the
#: best thing in it, the same way it is named for the highest resolution.
_RANGE_ORDER = ("DV", "HDR10+", "HDR10", "HDR", "HLG", "SDR")

#: Frames that are named for their short edge: the everyday shapes, and the
#: portrait flips of them. Anything else is scope, and gets the treatment below.
_PLAIN_RATIOS = (16 / 9, 4 / 3, 9 / 16, 3 / 4)
_RATIO_TOLERANCE = 0.02

#: What a scope frame's width is rounded to before its release height is worked
#: out. 1620 and 1080 are here because a few services deliver a horizontally
#: scaled master. Same widths and the same tolerance unshackle uses, plus the two
#: DCI widths (4096, 2048) that turn up here and that unshackle's list misses -
#: without them a 2048x858 master is named 1152p, which is not a release anyone
#: names.
_STANDARD_WIDTHS = {
    4096: 3840,
    3840: 3840,
    2560: 2560,
    2048: 1920,
    1920: 1920,
    1620: 1920,
    1280: 1280,
    1080: 1280,
}
_WIDTH_TOLERANCE = 50

#: Heights that are a release in their own right, whatever the frame around them
#: is, and how far off one a height may be and still count as it - encoders pad
#: 1080 to 1088 for macroblock alignment, and that is not a 1088p release.
_STANDARD_HEIGHTS = (2160, 1440, 1080, 720, 576, 480, 360)
_HEIGHT_TOLERANCE = 10


def clean_part(value: object) -> str:
    text = str(value or "")
    text = text.replace("&", "and")
    text = text.replace("'", "").replace('"', "").replace("’", "")
    text = _TITLE_DASH.sub(".", text)
    for char in (":", " ", "/", "\\", ",", "?", "!", "|", "*", "<", ">"):
        text = text.replace(char, ".")
    text = _MULTI_DOT.sub(".", text)
    return text.strip(".")


def format_title(
    name: str,
    *,
    year: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    episode_name: str | None = None,
) -> str:
    base = clean_part(name)
    has_season = season is not None
    season_number = int(season or 0)
    episode_number = int(episode or 0)

    if episode_number > 0:
        tag = (
            f"S{season_number:02d}E{episode_number:02d}"
            if has_season
            else f"E{episode_number:02d}"
        )
        if episode_name:
            return f"{base}.{tag}.{clean_part(episode_name)}"
        return f"{base}.{tag}"
    if episode_name:
        combined = f"{base}.{clean_part(episode_name)}"
        return f"{combined}.{year}" if year else combined
    if year:
        return f"{base}.{year}"
    return base


#: The shape of each kind of name, as a template the user can change. These are
#: exactly what the code used to do, written down: an episode is
#: ``Show.S01E02.Episode.Title``, a film is ``Film.2024``. The release half lives in
#: its own template below, because it is worked out later - after the tracks are
#: known - and the two halves are appended, not rendered together.
TITLE_TEMPLATES = {
    "episode": "{title}.{season_episode}.{episode_name?}",
    "movie": "{title}.{year?}",
}
#: ``.1080p.SERVICE.WEB-DL.DDP5.1.Atmos.H.265.DV-GROUP``. The order is unshackle's,
#: and the whole reason this is a setting is that the order is a preference:
#: put ``{range?}`` before ``{platform}`` here and every later name follows.
RELEASE_TEMPLATE = (
    "{quality?}.{platform?}.{source}.{audio_full?}.{atmos?}.{video?}.{range?}-{tag?}"
)

#: What a title template may say. Kept as a set so the settings screen can tell the
#: user what is available instead of letting a typo through to a file name.
TITLE_FIELDS = frozenset(
    {"title", "year", "season", "episode", "season_episode", "episode_name"}
)
#: and what the release half may say
RELEASE_FIELDS = frozenset(
    {
        "quality",
        "platform",
        "source",
        "audio",
        "audio_channels",
        "audio_full",
        "atmos",
        "video",
        "range",
        "tag",
    }
)


def title_fields(title: Title) -> dict[str, str]:
    """What a title template can be filled in with."""
    season = int(title.season or 0)
    episode = int(title.episode or 0)
    if episode > 0:
        tag = f"S{season:02d}E{episode:02d}" if title.season is not None else f"E{episode:02d}"
    else:
        tag = ""
    return {
        "title": clean_part(title.name),
        "year": str(title.year or ""),
        "season": f"{season:02d}" if title.season is not None else "",
        "episode": f"{episode:02d}" if episode else "",
        "season_episode": tag,
        "episode_name": clean_part(title.episode_name) if title.episode_name else "",
    }


def save_name_for(title: Title, templates: dict[str, str] | None = None) -> str:
    """Build the ``--save-name`` UniDL will use."""
    if title.kind in (TitleKind.CHANNEL, TitleKind.PROGRAM, TitleKind.STATION):
        parts = [clean_part(title.channel or title.name)]
        if title.kind in (TitleKind.PROGRAM, TitleKind.STATION) and title.episode_name:
            parts.append(clean_part(title.episode_name))
        if title.starts_at:
            parts.append(title.starts_at.strftime("%Y%m%d_%H%M%S"))
        return ".".join(p for p in parts if p)

    if title.kind is TitleKind.TRACK:
        # The release date belongs in audio metadata, not in its file name.  In
        # particular, it is not a film year and audio services often group
        # several distinct episodes under the same date.
        return format_title(title.name, episode_name=title.episode_name)

    fields = title_fields(title)
    # An episode can legitimately have no supplied number: sport, news and TV
    # specials still need the episode template so their supplied subtitle is not
    # discarded in favour of the film template.
    template_kind = "episode" if title.kind is TitleKind.EPISODE else "movie"
    wanted = (templates or {}).get(template_kind, "")
    if wanted:
        rendered = template.render(wanted, fields)
        if rendered:
            return rendered
        # a template that renders to nothing is a template with a mistake in it, and
        # a nameless file is worse than an unfashionable name: fall through
    return format_title(
        title.name,
        year=title.year,
        season=title.season,
        episode=title.episode,
        episode_name=title.episode_name,
    )


# --------------------------------------------------------------------- release


def _dimensions(resolution: object) -> tuple[int, int]:
    """``"1920x1080"`` as ``(width, height)``, or ``(0, 0)``."""
    match = re.search(r"(\d{2,5})\s*[x×]\s*(\d{2,5})", str(resolution or ""))
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _nearest_standard(height: int) -> int:
    """``height`` rounded to a standard one if it is within a hair of it."""
    return next(
        (
            edge
            for edge in _STANDARD_HEIGHTS
            if abs(height - edge) <= _HEIGHT_TOLERANCE
        ),
        height,
    )


def _release_height(width: int, height: int) -> int:
    """Which release a ``width x height`` frame is, in scan lines.

    The short edge for the everyday shapes - 640x480 is a 480p release, and so is
    a vertical 480x640. For scope it is the *width* that says which release this
    is, because a 2.39:1 film is letterboxed into a standard frame and naming it
    800p would describe the letterbox rather than the master.
    """
    if not width or not height:
        return height or width
    ratio = width / height
    if any(abs(ratio - plain) <= _RATIO_TOLERANCE for plain in _PLAIN_RATIOS):
        return _nearest_standard(min(width, height))
    snapped = next(
        (
            value
            for edge, value in _STANDARD_WIDTHS.items()
            if abs(width - edge) <= _WIDTH_TOLERANCE
        ),
        width,
    )
    guess = int(max(snapped, height) * 9 / 16)
    if height in _STANDARD_HEIGHTS or abs(guess - height) <= _HEIGHT_TOLERANCE:
        return height
    return guess


def quality_of(streams: object) -> str:
    """``"1080p"`` for the best video track among ``streams``, or ``""``.

    Always progressive: a manifest does not report scan type, and interlaced
    content does not reach a browser player.
    """
    best = 0
    for stream in streams or ():
        if getattr(stream, "media_type", "") != "video":
            continue
        width, height = _dimensions(getattr(stream, "resolution", ""))
        if not height:
            continue
        best = max(best, _release_height(width, height))
    return f"{best}p" if best else ""


def range_of(streams: object) -> str:
    """``"DV"`` / ``"HDR"`` / ``"HDR10P"`` / ``"HLG"`` for the best video, or ``""``.

    Empty for SDR, and empty when the manifest did not say - an absent tag reads
    as "nothing unusual", which is the right answer in both cases.
    """
    found = {
        str(getattr(stream, "video_range", "") or "").strip().upper()
        for stream in streams or ()
        if getattr(stream, "media_type", "") == "video"
    }
    for name in _RANGE_ORDER:
        if name in found:
            return RANGE_TAGS.get(name, "")
    return ""


def _audio_is_atmos(stream: object) -> bool:
    extra = getattr(stream, "extra", {})
    extra = extra if isinstance(extra, dict) else {}
    explicit = extra.get("audio_atmos")
    if isinstance(explicit, str):
        if explicit.strip().lower() in {"1", "true", "yes", "on"}:
            return True
    elif explicit:
        return True
    text = " ".join(
        str(value or "")
        for value in (
            getattr(stream, "codecs", ""),
            getattr(stream, "name", ""),
            getattr(stream, "group_id", ""),
            getattr(stream, "role", ""),
            extra.get("channels_raw"),
            extra.get("audio_type"),
            extra.get("profile"),
        )
    ).lower()
    return any(marker in text for marker in ("atmos", "joc", "atm3"))


def _audio_codec(stream: object) -> str:
    from unidl.downloader.utils import pretty_codec

    raw = str(getattr(stream, "codecs", "") or "")
    label = str(pretty_codec(raw, "audio") or raw.split(",", 1)[0] or "").strip()
    upper = label.upper()
    if "TRUEHD" in upper or upper.startswith(("MLP", "MLPA")):
        return "TrueHD"
    if upper.startswith(("MPEG-H", "MPEGH", "MHM1", "MHA1")):
        return "MPEGH"
    if upper.startswith("AC-4"):
        return "AC4"
    return _AUDIO_CODEC_TAGS.get(upper, clean_tag(label).upper())


def _audio_channels(stream: object, *, atmos: bool) -> tuple[str, float]:
    raw = str(getattr(stream, "channels", "") or "").strip()
    extra = getattr(stream, "extra", {})
    extra = extra if isinstance(extra, dict) else {}
    channel_hint = str(extra.get("channels_raw") or "").strip()
    # Some HLS providers use ``16/JOC`` as an Atmos signalling value, not a conventional
    # sixteen-channel bed. The downloadable E-AC-3 core is named as 5.1.
    if atmos and (raw == "16" or channel_hint.upper().startswith("16/JOC")):
        return "5.1", 6.0
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return "", 0.0
    value = match.group(0)
    if "." in value:
        try:
            parts = value.split(".", 1)
            score = float(parts[0]) + (1.0 if int(parts[1] or "0") else 0.0)
        except ValueError:
            score = 0.0
        return value, score
    count = int(value)
    layouts = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}
    return layouts.get(count, f"{count}.0"), float(count)


def audio_fields_of(streams: object) -> dict[str, str]:
    """Release audio fields for the strongest track in the final selection.

    ``streams`` is deliberately the caller's selected list, not the complete
    manifest. An automatically pre-checked DDP track that the user unticks cannot
    name a file whose selected audio is AAC.
    """
    values = list(streams or ())
    candidates = [stream for stream in values if getattr(stream, "media_type", "") == "audio"]
    if not candidates:
        # A muxed HLS variant can carry audio without a separate audio row.
        candidates = [
            stream
            for stream in values
            if getattr(stream, "media_type", "") == "video"
            and isinstance(getattr(stream, "extra", {}), dict)
            and getattr(stream, "extra", {}).get("muxed_audio")
        ]

    described: list[tuple[tuple[int, float, int, int], str, str, bool]] = []
    for stream in candidates:
        codec = _audio_codec(stream)
        if not codec:
            continue
        atmos = _audio_is_atmos(stream)
        channels, channel_score = _audio_channels(stream, atmos=atmos)
        score = (
            1 if atmos else 0,
            channel_score,
            _AUDIO_CODEC_ORDER.get(codec, 1),
            int(getattr(stream, "bandwidth", 0) or 0),
        )
        described.append((score, codec, channels, atmos))
    if not described:
        return {"audio": "", "audio_channels": "", "audio_full": "", "atmos": ""}

    _score, codec, channels, atmos = max(described, key=lambda item: item[0])
    return {
        "audio": codec,
        "audio_channels": channels,
        "audio_full": f"{codec}{channels}" if channels else codec,
        "atmos": "Atmos" if atmos else "",
    }


def audio_of(streams: object) -> str:
    """``AAC2.0`` or ``DDP5.1.Atmos`` for the final selected tracks."""
    fields = audio_fields_of(streams)
    return ".".join(value for value in (fields["audio_full"], fields["atmos"]) if value)


def video_of(streams: object) -> str:
    """``H.264`` / ``H.265`` / ``VP9`` / ``AV1`` for the selected video."""
    from unidl.downloader.utils import pretty_codec

    best_score = (-1, -1, -1, -1)
    best = ""
    for stream in streams or ():
        if getattr(stream, "media_type", "") != "video":
            continue
        width, height = _dimensions(getattr(stream, "resolution", ""))
        codec = str(pretty_codec(getattr(stream, "codecs", ""), "video") or "").strip()
        if not codec:
            continue
        range_name = str(getattr(stream, "video_range", "") or "").strip().upper()
        try:
            range_score = len(_RANGE_ORDER) - _RANGE_ORDER.index(range_name)
        except ValueError:
            range_score = 0
        score = (
            _release_height(width, height),
            range_score,
            int(getattr(stream, "bandwidth", 0) or 0),
            len(codec),
        )
        if score > best_score:
            best_score = score
            best = codec
    return best


def clean_tag(value: object) -> str:
    """A release group as the user typed it, minus anything a path cannot hold.

    Not :func:`clean_part`: that turns a dash into a dot, which is right for a
    title and wrong here - half the group names in circulation contain one, and
    ``WEB-DL`` is the convention's own word.
    """
    text = str(value or "").strip()
    for char in ('/', "\\", ":", "*", "?", '"', "<", ">", "|", " ", "\t"):
        text = text.replace(char, ".")
    text = _MULTI_DOT.sub(".", text)
    return text.strip(".")


def release_suffix(
    *,
    quality: str = "",
    platform: str = "",
    audio: str = "",
    audio_channels: str = "",
    audio_full: str = "",
    atmos: str = "",
    video: str = "",
    dynamic_range: str = "",
    tag: str = "",
    layout: str = "",
) -> str:
    """The release half, e.g. ``.1080p.SERVICE.WEB-DL.DDP5.1.Atmos.H.265.DV-GROUP``.

    Anything unknown is left out rather than written as a placeholder, so a service
    that reports no resolution simply produces a shorter name. ``layout`` is the
    template to use; the default is :data:`RELEASE_TEMPLATE`, and changing it is how
    the order of these parts is changed.
    """
    fields = {
        "quality": clean_part(quality),
        "platform": clean_part(platform),
        "source": SOURCE,
        "audio": clean_part(audio),
        "audio_channels": clean_part(audio_channels),
        "audio_full": clean_part(audio_full),
        "atmos": clean_part(atmos),
        "video": clean_part(video),
        "range": clean_part(dynamic_range),
        "tag": clean_tag(tag),
    }
    body = template.render(layout or RELEASE_TEMPLATE, fields)
    return f".{body}" if body else ""


def with_release(
    save_name: str,
    title: Title,
    *,
    streams: object = (),
    platform: str = "",
    tag: str = "",
    layout: str = "",
) -> str:
    """``save_name`` with the release half appended, for the kinds that take one.

    Idempotent: a name that already carries the source marker is returned as it
    is, so a job that is renamed twice - retried, resumed - does not accumulate.
    """
    if title.kind not in RELEASE_KINDS or not save_name:
        return save_name
    if f".{SOURCE}" in save_name:
        # already a release name. A title that genuinely contains ".WEB-DL" is one
        # too, so skipping is the right answer either way.
        return save_name
    suffix = release_suffix(
        quality=quality_of(streams),
        platform=platform,
        **audio_fields_of(streams),
        video=video_of(streams),
        dynamic_range=range_of(streams),
        tag=tag,
        layout=layout,
    )
    return f"{save_name}{suffix}" if suffix else save_name
