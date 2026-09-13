> Palette and layout conventions are defined by UniDL's own terminal UI.

# Interface

Four core screens carry a session: Home, the service menu, the service flow,
and delivery. Settings, search, availability and the CDM picker are overlays on
top of that sequence.

## Main screen

```
   esc Quit                                             ^f Search  ^s Settings
   UNIDL
   cdm samsung_l3 · drm Widevine · after picking download · config unidl.yaml

   SERVICES  available services
     1  BBC iPlayer

   platform name, or its number, or a video URL
   enter open  ↑↓←→ move  d after picking  ^f search  ^s settings  ^r dev reload
```

It answers one question - which platform - so it shows platform names and a
number for each. Nothing else: no account state, no feature flags, no helper
status. Those belong on the service's own screen, and leaving them out means the
screen does not probe every registered service just to draw itself.

Three equal ways to choose:

* click a name
* type part of a name, then `enter`
* type its number, then `enter`

Everything on this screen is a unidl service; there is one section because there
is one kind.

With global Debug mode on, `ctrl+r` explicitly reloads the highlighted service,
an exact id/number in the input, or the only visible search result. Its loaded
multi-file package and native shared-service dependencies are reloaded; no file
watcher runs. The cells are then rebuilt from the registry, so the next service
session uses the new class. The action only exists at the bottom of the screen
stack and never replaces a running service, download, licence or playback
session.

### Responsive grid

Column count is recomputed from the real terminal width, so a wide window shows
more platforms at once. Measured: 5 columns at 118 characters, 2 at 64. The
shortcut bar trims hints the same way rather than being cut off mid-word.

## Service screen

Choosing a platform clears the main screen and hands over to that service, which
presents its own options:

```
   ^b Back  esc Quit                                     ^f Search  ^s Settings
   BBC iPlayer  ·  anonymous  ·  widevine-test
   ^s after picking download the file  ·  quality best available  ·  choose tracks myself

     1  VOD - open a URL or content ID
     2  Live TV
     3  Search
     4  Settings
  enter confirm  space toggle  ^b back  ^l log  ^s settings  ^p commands
```

The header states who is signed in and which CDM is in use. Account names are
always shortened before display, by the UI rather than by each service, so no
service can leak one.

The option list is built from whatever entry points the service declares, and
is numbered like the main screen so typing a number works the same way.

Below it sits a log pane, hidden until something is written to it, expandable
with `ctrl+l`.

## Flow and delivery screens

The flow screen holds the work between choosing a service action and producing
a playback: login prompts, seasons, episodes, live channels and progress. The
delivery screen begins when a `Playback` exists. It reports the title, output
name, manifest, keys, selected tracks and optional service/API chapters, then
downloads or saves the command. One to three chapters fit in the details card;
the `Chapters` control opens the complete scrollable timeline without taking
space from progress or the log.
The service-level **Tracks and output → Embed chapters in the final file** toggle
controls only whether those chapters are written into the final MKV/MP4 container;
it does not hide or discard the metadata shown here or stored in exports.
A multi-title selection adds the queue described in [batch.md](batch.md).

A portable export import enters the same delivery screen and queue. Import first
matches the service ID (including declared legacy IDs). A match uses the installed
Service context so custom download preparation, key formatting, sidecars and
playback lifecycle actions continue to work. No match uses generic delivery, so
the source-service package is not mandatory for standard manifests. The export
still supplies its manifest, request headers and content keys; the generic path
never falls back to a new licence request when a key is missing.

An empty export `keys` list is not itself an error. Clear DASH/HLS and direct
media have nothing to decrypt. Standard HLS `AES-128` is handled by the native
downloader from the playlist's key URI (using the exported request headers), and
a service-resolved raw AES-128 key is retained in the export's DRM fields. A
wholly absent exported `KID:key` is fatal only when the playback declares
licence-bound DRM such as Widevine or PlayReady and the parsed ladder contains
encrypted tracks. If a file carries only some KIDs, Core reports the uncovered
inventory and only selected encrypted tracks whose KIDs are covered can complete.
With an installed service, its existing custom path remains authoritative;
generic fallback never attempts to reacquire a missing licence key.

### Native delivery progress

While a download or a recording is running, the structured delivery progress is
drawn in a card in the middle of this screen, repainting in place - the same rows,
the same colours and bars as the standalone `unidl download` command. One row per track,
and for a batch the queue above it says where the batch has got to.

It used to be in the log, and not by design. The embedded backend now emits
typed progress events instead of ANSI frames or one printed line per update, so
the log is left holding what it is for: messages, selected tracks, output paths
and errors.

## Chrome

The same bar is in the same place on every screen. Home omits Back because it is
the base of the stack:

| Position | Action | Key | Also |
|----------|--------|-----|------|
| top left | Back one level | `ctrl+b` | `b` |
| top left | Quit | `esc` | `ctrl+q` |
| top right | Search | `ctrl+f` | `/` |
| top right | Settings | `ctrl+s` | `s` |

Everything shown is a click target as well as a shortcut.

### Why control chords

A focused text field consumes plain letters, so `s` types an `s` instead of
opening settings. The control chords always reach the application; the plain
letters are a convenience that gives way while you type. `ctrl+k` is not used
because Textual's `Input` binds it to delete-to-end-of-line.

### Quitting takes two presses

`esc` is bound globally, so a single stray press must not end a session with a
download in flight. The first press arms it and says so; a second press within
1.5 seconds quits.

## Scoping

Search and Settings mean different things depending on where you are. This is
deliberate: it keeps each screen answering one question.

| | Main screen | Inside a service |
|---|---|---|
| **Search** | services, titles, availability, and the whole key vault | that service's keys only |
| **Settings** | what happens on pick, debug, batch confirmation | that service's own options, plus its track preferences |

Service settings apply to that service alone. BBC's source-resolution and live
region options do not affect global track settings. See [settings.md](settings.md).

## Navigation levels

| Where | `ctrl+b` does |
|-------|---------------|
| Main screen | clears the filter, otherwise nothing (it is the base screen) |
| Inside a flow | steps up one ask: episodes to seasons to the service menu |
| Inside a flow, between prompts | says the service is still working, and stays |
| Inside a flow that stopped with an error | leaves the service, back to the main screen |
| A service's top menu | leaves the service, back to the main screen |
| Settings, search | closes and returns to the caller |

The last two rows are the same outcome for the same reason. A flow that raised
took its service menu with it - the menu is that generator - so there is nothing
underneath to return to, and Back has to leave rather than step up. Refusing it
because no ask was pending is how a failed sign-in used to become a screen with
no way out.

Screens opt in by implementing `go_back() -> bool`: `True` means handled
internally, `False` means pop. Service flows do not need to know any of this -
`Service.home()` catches `Back` from a sub-flow and redisplays its menu.

## Selection

| Key | Action |
|-----|--------|
| `↑ ↓ ← →` | move, in two dimensions on the grid |
| `enter` | confirm |
| `space` | toggle, in a multi-select |
| `a` / `n` | select all / clear |
| digits | jump to that number |
| `home` / `end` | first / last |
| `ctrl+r` | on Home with Debug enabled, reload the explicitly selected service code |

Multi-selects arrive with the automatic choice already ticked, so `enter`
accepts it. That is how the track picker starts on whatever the track settings
resolved to.

## Command palette

`ctrl+p` opens a searchable list of every global action and every registered
service. This avoids memorising either service names or shortcuts.

## Colour

The default palette is dark: a near-black `#141414` base with a violet
`#bb9af7` accent. Light is the counterpart. Switch with `ctrl+t`, the command
palette, or the `theme` setting.

| Meaning | Colour |
|---------|--------|
| accent, keys, selected values | `#bb9af7` |
| primary text | `#e1e1e1` |
| secondary | `#c8c8c8` |
| muted, hints | `#6c6c6c` |
| dimmest, structure | `#414141` |
| ok | `#9ece6a` |
| warning | `#e0af68` |
| error | `#f7768e` |

Layout follows the same project's defaults: one row of vertical padding, two
columns of horizontal padding, no nested boxes.

## Rendering a screen without a terminal

UniDL uses Textual's headless pilot in focused smoke tests;
there is no production snapshot script or service-wide fixture in this
checkout.
