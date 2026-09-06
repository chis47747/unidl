"""Turning service ids into brand names.

A service id is a Python package name - ``bbc``, ``10play``, ``mediaset_es`` -
which is fine for a directory and wrong for a menu. The main screen shows
platforms, so it shows brands.

An explicit table beats a clever rule here: no heuristic gets ``bbc`` to ``BBC``
and ``binge`` to ``Binge`` and ``mytvsuper`` to ``myTV SUPER``. The fallback
covers anything added later, and a new service listed as ``Newthing`` instead of
``newthing`` is enough until someone adds a row.
"""

from __future__ import annotations

#: service id -> how the brand is actually written
BRANDS: dict[str, str] = {
    "10play": "10Play",
    "13tv": "13TV",
    "7plus": "7Plus",
    "9now": "9Now",
    # Two unrelated broadcasters share the letters: the plain "ABC" is the US
    # network, Australia's public broadcaster is ABC iView.
    "abc": "ABC",
    "abciview": "ABC iView",
    "acorntv": "Acorn TV",
    "amazon": "Amazon",
    "amc": "AMC+",
    "angel": "Angel",
    "apple": "Apple TV",
    "appletv": "Apple TV",
    "ard": "ARD",
    "atresplayer": "Atresplayer",
    "bbc": "BBC iPlayer",
    "bilibili": "哔哩哔哩",
    "binge": "Binge",
    "britbox": "BritBox",
    "canalplus": "Canal+",
    "catchplay": "CatchPlay+",
    "cbc": "CBC Gem",
    "cbcgem": "CBC Gem",
    "channel4": "Channel 4",
    "channel5": "Channel 5",
    "citytv": "Citytv+",
    "clarotv": "Claro TV",
    "clarovideo": "Claro Video",
    "crave": "Crave",
    "crunchyroll": "Crunchyroll",
    "cw": "The CW",
    "dazn": "DAZN",
    "directv": "DIRECTV",
    "discoveryca": "Discovery+ Canada",
    "discoverygo": "Discovery GO",
    "disney": "Disney+",
    "disneynow": "DisneyNOW",
    "elisa": "Elisa Viihde",
    "espn": "ESPN",
    "exxen": "Exxen",
    "f1tv": "F1 TV",
    "fox": "FOX",
    "francetv": "France TV",
    "friday": "Friday Video",
    "frndlytv": "Frndly TV",
    "fubo": "Fubo",
    "gagaoolala": "GagaOOLala",
    "globaltv": "Global TV",
    "globoplay": "Globoplay",
    "gothamsports": "Gotham Sports",
    "hallmark": "Hallmark+",
    "hamivideo": "Hami Video",
    "hoopla": "Hoopla",
    "hulu": "Hulu",
    "hulujp": "Hulu Japan",
    "icitoutv": "ICI TOU.TV",
    "itv": "ITVX",
    "joyn": "Joyn",
    "justwatch": "JustWatch",
    "kan": "Kan 11",
    "kanopy": "Kanopy",
    "kayo": "Kayo Sports",
    "lifetime": "Lifetime",
    "linetv": "LINE TV",
    "lionsgateplay": "Lionsgate Play",
    "m6": "M6+",
    "mako": "Mako",
    "max": "Max",
    "mediaset": "Mediaset Infinity",
    "mediaset_es": "Mediaset ES",
    "mgm": "MGM+",
    "mgtv": "芒果TV",
    "mlbtv": "MLB.TV",
    "molotov": "Molotov",
    "moviesanywhere": "Movies Anywhere",
    "movistar": "Movistar+",
    "mubi": "MUBI",
    "mytvsuper": "myTV SUPER",
    "myvideo": "MyVideo",
    "nba": "NBA League Pass",
    "nbc": "NBC",
    "nesn": "NESN 360",
    "netflix": "Netflix",
    "netflix.tv": "Netflix (TV app)",
    "nowplayer": "Now Player",
    "nowtv": "NOW TV",
    "npo": "NPO Start",
    "okko": "Okko",
    "oneplay": "OnePlay",
    "optimum": "Optimum",
    "oqee": "OQEE by Free",
    "orf": "ORF ON",
    "osn": "OSN+",
    "paramount": "Paramount+",
    "paramountplus": "Paramount+",
    "pbs": "PBS",
    "peacock": "Peacock",
    "philo": "Philo",
    "playsuisse": "Play Suisse",
    "plex": "Plex",
    "plutotv": "Pluto TV",
    "polsatboxgo": "Polsat Box Go",
    "prima": "Prima+",
    "rakuten": "Rakuten TV",
    "rivertv": "RiverTV",
    "roku": "The Roku Channel",
    "rte": "RTE Player",
    "rtlhu": "RTL+ Hungary",
    "rtlplus": "RTL+",
    "ruutu": "Ruutu",
    "sbs": "SBS On Demand",
    "shahid": "Shahid",
    "shudder": "Shudder",
    "skygo": "Sky Go",
    "skygo_dash": "Sky Go (DASH)",
    "skyshowtime": "SkyShowtime",
    "sling": "Sling TV",
    "sportsnet": "Sportsnet",
    "sporttv": "Sport TV",
    "stan": "Stan",
    "start": "START",
    "starz": "Starz",
    "starzplay": "StarzPlay",
    "stv": "STV Player",
    "sundancenow": "Sundance Now",
    "svtplay": "SVT Play",
    "tabii": "tabii",
    "tennistv": "Tennis TV",
    "tf1": "TF1+",
    "threenow": "ThreeNow",
    # Turner's US network - tntdrama.com - which the tnt script covers along with
    # TBS and truTV. Not Discovery's TNT Sports.
    "tnt": "TNT",
    "tod": "TOD",
    "tsn": "TSN",
    "tubi": "Tubi",
    "tv2play": "TV 2 Play",
    "tv2playno": "TV 2 Play Norway",
    "tv4play": "TV4 Play",
    "tva": "TVA+",
    "tvbanywhere": "TVB Anywhere",
    "tvnz": "TVNZ+",
    "tvplus": "TV+",
    "tvingw": "TVING",
    # UKTV's service, renamed from UKTV Play to just "U". Not Bell's U.
    "u": "U (UKTV)",
    "ufc": "UFC Fight Pass",
    "unext": "U-NEXT",
    "usa": "USA Network",
    "viaplay": "Viaplay",
    "videoland": "Videoland",
    "vidio": "Vidio",
    "viki": "Rakuten Viki",
    "viju": "viju",
    "viu": "Viu",
    "vix": "ViX",
    "vudu": "Vudu",
    "waipu": "waipu.tv",
    "watcha": "Watcha",
    "watchit": "WatchIT",
    "wavve": "Wavve",
    "xfinity": "Xfinity Stream",
    "yes": "YES",
    "youku": "优酷",
    "youtube": "YouTube",
    "ytv": "YTV",
    "zdf": "ZDF",
}

#: file stem -> the short service tag unshackle uses.
#:
#: unshackle names a service by a short uppercase tag rather than by its brand -
#: `DSNP`, `AMZN`, `PMPT` - and those tags turn up in file names, in shared
#: commands and in conversation. Keeping the same ones means a name you already
#: know still works here, and a tag written by one tool is readable by the other.
#:
#: Anything absent gets a derived tag (see :func:`service_tag`), which is
#: predictable but not authoritative; add a row when the real one is known.
TAGS: dict[str, str] = {
    "10play": "10PL",
    "13tv": "13TV",
    "7plus": "7PLUS",
    "9now": "9NOW",
    # `ABC` is the US network; Australia's tag is ``iview``.
    "abc": "ABC",
    "abciview": "iview",
    "acorntv": "ACRN",
    "amazon": "AMZN",
    "amc": "AMCP",
    "apple": "ATV",
    "appletv": "ATV",
    "atresplayer": "ATRP",
    "bbc": "iP",
    "bilibili": "BILI",
    "bbcsounds": "SNDS",
    "binge": "BNGE",
    "britbox": "BRIT",
    "canalplus": "CNLP",
    "catchplay": "CTPL",
    "friday": "FRDY",
    "hamivideo": "HAMI",
    "linetv": "LINE",
    "myvideo": "MYVD",
    "cbc": "GEM",
    "cbcgem": "GEM",
    "channel4": "ALL4",
    "channel5": "MY5",
    "citytv": "CTV+",
    # both would derive to CLAR, and an ambiguous tag is worse than an ugly one
    "clarotv": "CLTV",
    "clarovideo": "CLVD",
    "crave": "CRAV",
    "crunchyroll": "CR",
    "cw": "CWTV",
    "dazn": "DAZN",
    "directv": "DTV",
    "discoveryca": "D+CA",
    "discoverygo": "DISC",
    "disney": "DSNP",
    "disneynow": "DSNW",
    "espn": "ESPN",
    "f1tv": "F1TV",
    "fox": "FOX",
    "francetv": "FRTV",
    "frndlytv": "FRND",
    "fubo": "FUBO",
    "globoplay": "GLBO",
    "hallmark": "HLMK",
    "hoopla": "HOOP",
    "hulu": "HULU",
    "hulujp": "HULJ",
    "icitoutv": "TOUTV",
    "itv": "ITV",
    "joyn": "JOYN",
    "kanopy": "KNPY",
    "kayo": "KAYO",
    "lifetime": "LIFE",
    "lionsgateplay": "LGP",
    "m6": "M6",
    "max": "MAX",
    "rtlhu": "RTLH",
    "mediaset": "MSET",
    "mgm": "MGM",
    "mlbtv": "MLB",
    "movistar": "MVST",
    "mubi": "MUBI",
    "mytvsuper": "MYTV",
    "nba": "NBA",
    "nbc": "NBC",
    "nesn": "NESN",
    "netflix": "NF",
    "nowtv": "NOW",
    "npo": "NPO",
    "okko": "OKKO",
    "orf": "ORF",
    "osn": "OSN",
    "paramount": "PMPT",
    "paramountplus": "PMPT",
    "pbs": "PBS",
    "peacock": "PCOK",
    "philo": "PHLO",
    "playsuisse": "PLSU",
    "plex": "PLEX",
    "plutotv": "PLUT",
    "rakuten": "RKTN",
    "reshet13": "13TV",
    "rivertv": "RIVR",
    "roku": "ROKU",
    "rte": "RTE",
    "rtlplus": "RTLP",
    "sbs": "SBS",
    "shahid": "SHHD",
    "shudder": "SHDR",
    "skygo": "SKGO",
    "skyshowtime": "SKST",
    "sling": "SLNG",
    "sportsnet": "SNET",
    "stan": "STAN",
    "starz": "STRZ",
    "starzplay": "STZP",
    "stv": "STV",
    "sundancenow": "SDNC",
    "svtplay": "SVT",
    "tf1": "TF1",
    "threenow": "3NOW",
    "tnt": "TNT",
    "tubi": "TUBI",
    "tv2play": "TV2",
    "tv4play": "TV4",
    "tvnz": "TVNZ",
    "tvingw": "TVING",
    "u": "UKTV",
    "unext": "UNXT",
    "usa": "USA",
    "viaplay": "VIAP",
    "videoland": "VDLD",
    "vidio": "VIDI",
    "viki": "VIKI",
    "viju": "VIJU",
    "viu": "VIU",
    "vix": "VIX",
    "waipu": "WAIP",
    # both would derive to WATC
    "watcha": "WTCA",
    "watchit": "WTIT",
    "wavve": "WAVE",
    "xfinity": "XFIN",
    "youku": "YK",
    "youtube": "YT",
    "zdf": "ZDF",
}

#: two- to four-letter words that are initialisms wherever they turn up
KNOWN_ACRONYMS = {
    "abc", "amc", "ard", "bbc", "cbc", "cbs", "cnn", "cw", "dstv", "espn",
    "fox", "hbo", "itv", "mgm", "mlb", "nba", "nbc", "nfl", "nhl", "npo",
    "orf", "pbs", "rte", "rtl", "sbs", "stv", "tf1", "tnt", "tod", "tsn",
    "tva", "tvb", "ufc", "usa", "vod", "yes", "zdf",
}


def pretty_platform(raw: str) -> str:
    """Brand name for a script stem.

    >>> pretty_platform("bbc")
    'BBC iPlayer'
    >>> pretty_platform("10play")
    '10Play'
    >>> pretty_platform("somethingnew")
    'Somethingnew'
    """
    stem = (raw or "").strip()
    if not stem:
        return ""
    known = BRANDS.get(stem.lower())
    if known:
        return known
    return _fallback(stem)


def service_tag(raw: str) -> str:
    """The short service tag for a stem, from the table or derived from it.

    >>> service_tag("paramountplus")
    'PMPT'
    >>> service_tag("somethingnew")
    'SOME'

    A derived tag is predictable rather than authoritative, and two services can
    end up sharing one. Lookups therefore try ids and aliases first, so a tag
    collision can never shadow an exact name.
    """
    stem = (raw or "").strip()
    if not stem:
        return ""
    known = TAGS.get(stem.lower())
    if known:
        return known
    letters = "".join(char for char in stem if char.isalnum())
    return letters[:4].upper()


def _fallback(stem: str) -> str:
    """No table entry: make it look like a name rather than a file."""
    words = [part for part in stem.replace("-", " ").replace("_", " ").split(" ") if part]
    out: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in KNOWN_ACRONYMS:
            out.append(word.upper())
        elif word[:1].isdigit():
            # 10play -> 10Play: capitalise after the leading digits
            head = word[: len(word) - len(word.lstrip("0123456789"))]
            tail = word[len(head) :]
            out.append(head + (tail[:1].upper() + tail[1:] if tail else ""))
        elif word.isupper():
            out.append(word)  # already deliberate
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


__all__ = ["BRANDS", "KNOWN_ACRONYMS", "TAGS", "pretty_platform", "service_tag"]
