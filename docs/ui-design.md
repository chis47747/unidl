> Palette and layout conventions are defined by UniDL's own terminal UI.

# UI design rules

Binding rules for every screen in unidl. If a screen breaks one of these, the
screen is wrong, not the rule.

## The four core screens

There are four screens in the service-to-delivery sequence, and each answers
one question.

| # | Screen | Question | Class |
|---|--------|----------|-------|
| 1 | Home | which platform? | `tui/home.py:HomeScreen` |
| 2 | Service home | what do I want to do on this platform? | `tui/service_screen.py:ServiceScreen` |
| 3 | Flow | the service's actual work: which title, season, track | `tui/flow_screen.py:FlowScreen` |
| 4 | Delivery | download it, or show the command | `tui/download_screen.py:DownloadScreen` |

The Home platform list can be cycled with `v` or the `view` control at the far
right of the `SERVICES … available` heading. It has three deterministic views:
A–Z, country, and media type. Country is metadata, not a live location check: a
service's first `GEOFENCE` code is its primary market and an empty declaration
is International. Media type is declared by the service as audio, video, or
both; the combined value gets its own “Audio + video” section so a platform such
as YouTube is not hidden from either audience. Filtering, keyboard navigation
and numeric addresses continue to work in every view. The status line is
reserved for run state and entry points: CDM, DRM, after-picking action, config,
readiness, and import.

The two caption values below the Home wordmark are controls as well. Clicking
the copyright opens the in-app open-source acknowledgements; clicking the
version opens local release notes and the update status. Until a signed remote
release feed is configured, that dialog states that remote checking is reserved
and performs no network request—it must never claim that an unchecked build is
current.

Screens 2 to 4 share `tui/askhost.py:AskHost`, which is the whole machine minus
the header: one mounted ask, a log, and the plumbing that lets a worker thread
block on the answer. They differ only in what sits above the content.

Which screen an ask lands on is carried by the ask itself, as
`Ask.scope` - `root`, `flow` or `delivery`. Services never set it except for
their own top menu; the default puts them on screen 3.
`tui/session.py:SessionController` reads the scope and pushes or pops to suit.

Settings and Search are overlays on top of whichever screen is current, not
screens in this sequence.

### Screens replace, never accumulate

Moving from one screen to the next **clears the previous screen's state**. Each
is a separate Textual `Screen`, pushed and popped, so nothing from screen 2
bleeds into screen 3. Returning from 3 to 2 discards what 3 was showing.

Consequence: a screen must not be the only holder of state the next screen
needs. Anything that has to survive lives on the session controller
(`tui/session.py:SessionController`), not in a widget.

## Common frame

Every screen has the same three bands, in the same order:

```
┌──────────────────────────────────────────────────────────────┐
│ CHROME      left: navigation      right: search, settings    │  1 row
├──────────────────────────────────────────────────────────────┤
│ IDENTITY    who and where you are, plus state and parameters │  varies
├──────────────────────────────────────────────────────────────┤
│ CONTENT     the one thing this screen is for                 │  1fr
├──────────────────────────────────────────────────────────────┤
│ LOG         what just happened            (screens 2, 3, 4)  │  8 rows
├──────────────────────────────────────────────────────────────┤
│ KEYBAR      shortcuts for this context                       │  1 row
└──────────────────────────────────────────────────────────────┘
```

### Chrome

Fixed positions so muscle memory works. Search and Settings never move. Back is
present on every screen except the first, which has nothing above it - an inert
greyed control there would be worse than no control.

| Screen | Left | Right |
|--------|------|-------|
| 1 Home | Quit | Search · Settings |
| 2 Service home | Back · Quit | Search · Settings |
| 3 Flow | Back · Quit | Search · Settings |
| 4 Delivery | Back (stops the queue) · Pause/Resume delivery · Quit | Search · Settings |

Every item is a click target and a shortcut. Chrome items are rendered **bold on
a panel background with two columns of padding**, so they read as controls rather
than as text.

> A terminal has one font at one size, set by the terminal, not by the
> application. "Larger" is expressed through weight, contrast, spacing and
> background, and for titles through block-letter art of differing heights.

Keys:

| Action | Chord | Also |
|--------|-------|------|
| Back | `ctrl+b` | `b` |
| Quit | `esc` (twice) | `ctrl+q` |
| Search | `ctrl+f` | `/` |
| Settings | `ctrl+s` | `s` |

The chords are advertised because they work while a text field has focus. The
plain letters give way to typing. `ctrl+k` is unavailable: Textual's `Input`
binds it to delete-to-end-of-line.

### Overlays carry the frame too

Settings, search, the device picker and the value editors are screens like any
other, so they draw the same chrome and the same key bar. A screen that drops
them leaves Back and Quit missing from the corner they are always in; the
shortcuts still work, but a control you cannot see is not a control.

A modal that is see-through cannot do this, because the screen underneath shows
its own chrome and key bar behind the modal's and neither is obviously the live
one. Value editors are therefore opaque, with the card centred and the frame
where it always is.

### Identity band

The visual hierarchy is deliberate and monotonic: the further in you are, the
smaller the title.

| Screen | Title treatment | Height |
|--------|-----------------|--------|
| 1 Home | `unidl` in large block letters, centred | 6 rows |
| 2 Service home | service name in small block letters, centred; other scripts use the same ▀▄█ mosaic from a system UI font | 3 rows Latin, 6 rows CJK/others (or 1 when too narrow) |
| 3 Flow | service name as bold text | 1 row |
| 4 Delivery | service name as bold text | 1 row |

Below the title, one row of state and parameters. On screen 1 that is the CDM,
the DRM system, what happens after a pick, and the config file. On screens 2 to 4
it is the signed-in account, the CDM, and the active track preferences.

Service names are data, not interface messages. They are kept in their original
Unicode form and rendered through the bidi-safe boundary; CJK/wide characters
use terminal-cell widths, and scripts without the hand-drawn Latin glyph table
are drawn as a six-row half-block mosaic of the real glyphs. A framed tile is
only the last resort when no font can rasterise the name. A future interface-language setting must
translate only UniDL-owned labels and prompts, never service names, title data,
URLs, filenames, KIDs or API locale settings. See [i18n.md](i18n.md).

### Log band

Screens 2, 3 and 4 carry a log. It stays collapsed until something is written,
expands with `ctrl+l`, and is where the mandatory report appears.

Screen 1 has no log; it does no work.

## The mandatory report

Whenever a playback is resolved - any service, any entry point, VOD or live, any
mode - these are reported, always, in this order and these colours:

| Field | Colour | Note |
|-------|--------|------|
| `title` | accent violet | human title |
| `save as` | primary text | output filename |
| `manifest` | blue | the URL handed to native delivery core |
| `kind` | yellow | only for live |
| `note` | muted | only when the service has something to say |
| `key` | green | one row per `kid:key`, or "none needed" |

Written as Rich `Text`, never as markup, so a title or URL containing square
brackets cannot be reinterpreted as styling. Selectable with the mouse, which
copies it.

### Delivery details and controls

The delivery screen keeps the mandatory report in the log and adds a compact,
scrollable facts block above progress. It states the output path, parsed source,
selection summary, every selected stream, each encrypted stream's KID and every
`KID:KEY` being handed to native delivery. This is inspection data, not another
picker, so it never changes the final selection.

When a service supplies chapters, the same block shows a count and time span;
short lists are expanded there, while longer lists use the `Chapters` button (or
`c`) to open the dedicated scrollable view. Chapter labels remain service data,
so Unicode and RTL titles go through the same safe render boundary as all other
metadata.

The visible **Stop** control sends the same native cancellation token as leaving
the delivery screen. While cancellation drains it becomes disabled **Stopping**;
after a cancelled row retains resumable native parts, the same control becomes
**Resume**. Back stops the whole queue, while the existing skip action stops only
the current row. Quit first requests cancellation and makes the second Quit the
explicit app exit.

The progress card consumes structured Core events. It does not capture terminal
stdout or ask the standalone CLI to own the screen.

Audio-only delivery is the one intentional delivery-layout variant. Its track
picker keeps album art and ID3 metadata in a left preview rail while the right
rail is used for track selection. Once downloading starts, progress and mux
status occupy the left side and the same artwork/metadata preview stays on the
right. Video VOD and live recording do not create this rail and retain the
full-width layout above.

### Track row colour

Track text receives a subtle semantic palette role while the checkbox remains the
only selection state. Dolby Vision and CBCS use `accent`; HDR/HLG and AES/SAMPLE
encryption use `warn`; ordinary CENC/CTR uses `manifest`; unknown encryption is
`muted`; clear SDR remains normal foreground. These are role names rather than
fixed hues, so both dark and light palettes retain useful contrast.

## Interaction

### Three equal ways to choose

Click, type a name, or type a number. Every list is numbered, and numbering is
continuous within a screen so a number is unambiguous.

Home also has one development-only action: with global Debug mode enabled,
`ctrl+r` reloads the focused service, an exact id/number, or the sole filtered
result. It is a manual safe point, never a watcher. The screen rebuilds its cells
from the new registry classes; no screen inside a service exposes the action, so
an active instance and its playback lifecycle remain unchanged.

### Anything that states a value can change it

Every item on the main screen's status row is a control, not a label:

| Item | Click it to |
|------|-------------|
| `cdm <name>` | open the device picker |
| `drm <system>` | cycle through registered DRM systems |
| `after picking <action>` | cycle download / command only / ask |
| `config <file>` | open the config file in the system default editor |

Each is its own widget rather than a span of markup, because a widget can have a
`:hover` style and a span cannot. The hover highlight *is* the affordance.

Switching DRM system takes the device with it: a device for another system would
be silently wrong, so the first matching device is picked, or the interface says
which folder and extension are needed. Choosing a device works the other way
round for the same reason: `.wvd`, `.prd` and `.mld` identify Widevine,
PlayReady and MonaLisa respectively.

### Input goes at the bottom

Where the text you type appears. A one-line grey hint sits above it explaining
what the field accepts, in words rather than abbreviations.

### Selection keys

| Key | Action |
|-----|--------|
| `↑ ↓ ← →` | move, two-dimensionally on a grid |
| `enter` | take the highlight; with ticks, confirm the selected batch |
| `space` | toggle, in a multi-select |
| `a` / `n` | select all / clear, of what is currently shown |
| digits | jump to that number |
| `home` / `end` | first / last |
| `ctrl+r` | Home only: explicitly reload selected service code when Debug is on |

When a multi-select arrives with an automatic choice already ticked, `enter`
accepts it. With nothing ticked, `enter` takes the highlighted row like a
single-select; this keeps the normal search → season → episode path consistent.
Use `space` first to build a batch, then `enter` confirms every tick.

### Long lists narrow as you type

Any list of ten or more entries gets a filter field beneath it
(`tui/filterbox.py`). One contract everywhere, the same one the platform list
uses:

- letters narrow the list, every whitespace-separated term having to match
  somewhere in the label, the detail or the tags
- a number addresses an entry, so typing one clears the filter and points at it
- `↑ ↓` and `page up` / `page down` move the list while the field has focus

Numbers keep their meaning while a filter is on. An address that moves is not an
address, which is why a digit clears the filter rather than being searched for.

Where the focus starts differs by list type, on purpose:

| List | Focus starts on | Because |
|------|-----------------|---------|
| single-select | the filter | the job is find-and-take, and `enter` takes the highlighted row |
| multi-select | the list | the job is ticking, and `space` has to toggle rather than type a space |

In a multi-select the filter is a view, not a deselection. What is ticked is
remembered independently of what is shown, so narrowing to `S01`, ticking two
episodes, then clearing the filter keeps them ticked. The hint line states the
count and says when it includes rows the filter is hiding.

### Mouse

Clicking anything actionable activates it. Dragging to select text copies it on
release, through OSC 52 and through the OS clipboard tool where one exists.

### Destructive actions confirm

Quit needs a second `esc` within 1.5 seconds. A stray keypress must not end a
session with a download running.

## Scoping

Search and Settings mean different things depending on the screen. This is how
each screen keeps to one question.

| | Screen 1 | Screens 2 to 4 |
|---|----------|----------------|
| Search | services, titles, whole key vault | that service's keys |
| Settings | six concise global managers: Download behavior, DRM & vaults, Files & naming, Proxy & VPN, JustWatch search, and Interface & diagnostics | that service's own options and its track preferences |

Service settings apply to that service alone.

## Naming

Platform names are shown as brands, not as file names: `BBC iPlayer`, not `bbc`.
The interface uses one native service registry and one delivery contract so
navigation and error handling remain consistent across providers.

Say "config", not "yaml". Spell out what a setting does: "after picking
download the file", not "on pick download".

## Responsiveness

The platform grid recomputes its column count from the real terminal width.
Measured: 5 columns at 118 characters, 2 at 64. The key bar drops hints from the
right rather than being truncated mid-word. Row detail is dropped whole rather
than clipped.

## Bidirectional titles

Service and core strings stay in Unicode logical order. Search, filtering,
cache values, explicit copy actions and output names must never receive a
visually reversed title. At the TUI boundary, `tui/bidi.py` creates a terminal-only copy:
the Unicode BiDi algorithm orders Hebrew/Arabic runs, Arabic presentation forms
are shaped without dropping vowel marks, and Textual markup plus explicit bidi
override/isolate controls from remote titles are neutralised. Chinese and
European left-to-right text pass through unchanged.

Apply this at the shared picker/table/header renderer, not inside an individual
service. Exercise mixed Hebrew or Arabic, Latin, numbers, combining marks and
CJK with the interface test suite.

## Colour

Two palettes, dark and light. Dark is the default,
chosen there because it quantizes cleanly on 256-colour and 16-colour terminals
rather than requiring truecolor. Light exists because a dark interface is
unreadable in a light terminal, and that is not a preference we get to have on
the user's behalf.

Colours are named by **role**, not by hue, so the light palette can pick a
different hue for the same job without any call site lying about it: `#9ece6a` on
white is barely legible, so `ok` is a deeper green there while staying green.

| Role | Used for | Dark | Light |
|------|----------|------|-------|
| `accent` | selection, keys, titles | `#bb9af7` | `#6d3fc4` |
| `fg` | primary text | `#e1e1e1` | `#1c1c1c` |
| `fg2` | secondary | `#c8c8c8` | `#3a3a3a` |
| `muted` | captions, labels | `#6c6c6c` | `#6a6a6a` |
| `dim` | hints | `#585858` | `#8a8a8a` |
| `gutter` | numbers, rules, structure | `#414141` | `#b4b4b4` |
| `manifest` | URLs | `#7aa2f7` | `#2a54b8` |
| `ok` | success, keys | `#9ece6a` | `#3c7a1e` |
| `warn` | warnings, live | `#e0af68` | `#8a5a00` |
| `error` | failures | `#f7768e` | `#b3243b` |
| `bg` | base | `#141414` | `#fbfbfb` |
| `panel` | recessed blocks | `#1c1c1c` | `#f1f1f1` |
| `highlight` | selected row | `#242424` | `#e6e2f2` |
| `hover` | mouse hover | `#2c2c2c` | `#ebebeb` |

### How to reference a colour

Never as a hex literal. Colours are Textual design tokens - `$accent`, `$muted`,
`$gutter`, `$manifest`, `$ok`, `$warn`, `$bad` - in the stylesheet **and** in
widget markup, so switching theme repaints everything with no restart.

The one exception is Rich `Text` written into the log: Rich resolves styles
itself and knows nothing about Textual's tokens. Those call sites name a palette
role and read the live value from `app.palette`, so they follow the theme too.

Switching: `ctrl+t`, the command palette, or Settings. It is remembered.

One row of vertical padding, two columns of horizontal padding, no nested boxes,
no borders except a single rule above the log.

An unticked checkbox is the same glyph as a ticked one, drawn in the panel
colour. Colour carries that whole distinction, which is why both palettes keep a
real contrast between `accent` and `panel`.

## Checking a screen

Run the interface checks with:

```bash
python -m pytest -q
```

Two layout mistakes this caught, worth knowing about:

- A Textual widget's `height` includes its padding. `height: 1` with
  `padding-bottom: 1` leaves no content row, and the text renders as nothing at
  all rather than as clipped.
- An `Input` with `border: none` and an empty value is pixel-identical to empty
  space. Ask fields get an accent bar down the left edge so the cursor's location
  is visible.
