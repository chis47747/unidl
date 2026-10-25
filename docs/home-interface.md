# Experimental home

The classic service-list home remains the default. To try the search-first
layout, open **Settings → Interface & diagnostics → Beta interface** and enable
it. The change takes effect when you return home; it never replaces an active
service, download or modal screen. Turn it off in the same place to return to
the classic home. The preference is remembered across restarts.

In **Settings → Interface & diagnostics → Beta home Auto result types**, choose
which categories Auto searches and displays: services, export files, local vault
keys, settings, JustWatch, search within a service and link matching. All are
checked by default. Uncheck a type to hide both its results and its automatic
action/shortcut rows; disabled local Vault, export and settings sources are not
queried while typing in Auto. You may uncheck every type; an empty selection
stays empty rather than restoring defaults.

This preference only affects the experimental home's **Auto** mode. It does
not remove modes from the menu or disable their slash commands: for example,
with Vault unchecked, choose **Search vault** or `/vault` to see live local
results and the existing explicit remote-search action. If link matching is
unchecked, Auto does not open a pasted URL; choose Open link or Open with service.
Export-path imports in Auto follow the export-files checkbox; explicit Open
link and Search export files keep their existing import behavior. Classic Home,
global search, manual Add keys and remote-vault policy are unchanged.

The UniDL banner and the CDM, DRM, after-picking, configuration, readiness and
import controls are unchanged. The experimental layout replaces the persistent
service grid with a single outlined search bar and a local result list.
In Auto, each result starts with a short category label, such as **[Service]**,
**[Export]**, **[Vault]**, **[Setting]**, **[JustWatch]**, **[Service search]** or
**[Link]**. Each category has its own muted, theme-aware ink; titles keep their
normal styling. Labels follow the interface language, so color is not the only
way to tell results apart. Specific search modes do not repeat these labels,
and the shared **Load more** control has no category.
The mode, input and submit action share one aligned row; the heading and
keyboard hint sit outside the bar. An empty query leaves the page background
clear instead of displaying an empty results panel.
The visible search bar uses three terminal rows, with the controls and input
text on its exact middle row. Normal windows add one row of spacing outside
the outline, rather than uneven padding inside it. Short windows remove that
extra spacing to leave room for search results.
The search bar, heading, hints and results share the visible CDM-to-import
status row's left and right edges. Their width follows status updates and window
resizing rather than a fixed width cap. Narrow windows use the available width
to keep the input and actions usable.

When the input is non-empty, a small **×** appears on its right. Click it or
press `ctrl+u` to clear the whole input and keep typing in the same mode and
service scope. The button has a reserved slot so showing it does not shift the
input or Go button. Long text scrolls horizontally inside the field.

With the input focused and already empty, press `backspace` or `delete` to
remove the current mode and its selected service and return to Auto. Deleting
the last character only clears that character; press again to reset the mode.
Clicking **×** or pressing `ctrl+u` still clears text only. Other modes' remembered
service scopes are not reset.

The input accepts up to 65,536 characters. An insertion that would exceed this
limit is rejected with a message, keeping the previous input and never silently
truncating signed URLs. In Auto or Open link, HTTP(S) URLs longer than 4,096
characters offer **Open with service** rather than running automatic service
matchers on the UI thread. Choose a service to pass the complete, unchanged URL
to its handler. The explicit Open with service mode also accepts these URLs.

## Choose what to search

Click the mode label on the left of the input, or type `/` in an empty input.
Choose a mode with the mouse or arrow keys and Enter. Escape closes this
selector without quitting UniDL.
Mode and service selectors open at their fitted size, stay within the terminal,
and scroll when there is not enough room to show every choice.
The service selector uses a compact 52-column card, a single-line name/ID filter
and up to eight visible services. All services remain available by filtering or
scrolling; filtering keeps the card size stable. Smaller windows constrain the
card to the available space.

| Mode | What it does |
|------|--------------|
| Auto | Recognizes a supported URL, an existing export JSON path or a KID/key query. Other text offers matching home-visible services, local export files, settings and explicit title-search actions. |
| Find services | Searches home-visible service names, IDs and aliases. An exact, unambiguous name can open directly on Enter. |
| JustWatch | Submitting a title opens the existing JustWatch search using its configured country and availability regions. |
| Search within a service | First choose a registered service that supports search, then submit a title. This uses that service's own search flow, not JustWatch. |
| Open link | Routes a URL only to registered services that declare support for it; also accepts an export JSON path. |
| Open with service | First choose a registered service that supports opening URLs/content IDs, then submit a link or ID directly to that service. It does not use automatic URL matching or search. |
| Search export files | Searches the configured exports folder and its service subfolders by filename, title, episode or service. Choose a file to use the existing export import flow. |
| Search vault | Shows matching local keys as you type a KID, key, pair, title or service. The Search vault action opens the existing full search page for more results and explicit remote lookup. |
| Add keys | Opens the existing manual-key editor. Enter KID:KEY pairs to prefill it, or leave the input blank. Choose the service and review local/remote destinations there before submitting a write. |
| Find settings | Searches setting names, descriptions and stable keys, then opens the selected setting. Shared track preferences first ask which service to edit. |

The mode stays visible beside the input, so an explicit choice is not silently
reinterpreted. For example, a URL entered in **Find settings** is searched as
setting text rather than opening a service. **Search within a service** remembers
the chosen service; choose that mode again to select a different one.
**Open with service** remembers its own chosen service independently. Once
selected, the left control shows **open · Service name**; the full mode and
service name remain available in its tooltip.

In **Open with service**, the service's existing URL/content-ID handler owns
validation. A numeric ID, a case-sensitive ID, a link without an automatic match,
or an ID resembling a KID is still passed to the explicitly selected service.
UniDL does not reroute it to Vault, JustWatch, another service or export import.
Selecting this mode and its service does not submit the input; press Enter or
Go to continue. If you submit before a service is chosen, choosing one completes
that already-confirmed action. Cancelling the picker keeps the input unchanged.

You can also paste a command with its query, then press Enter:

- `/service BBC`
- `/justwatch The Tourist`
- `/catalog The Tourist`
- `/url https://example.com/watch/123`
- `/open 12345678`
- `/exports The Tourist`
- `/vault 0123456789abcdef0123456789abcdef`
- `/addkeys` (optionally followed by a `KID:KEY` pair)
- `/settings default_subtitle`
- `/auto BBC`

These aliases are stable English commands; the visible menus and help follow
the interface language. A command changes the mode and fills the input.
Press Enter again to submit, choosing a service first when required.

## Explicit actions and safety

Typing only builds local suggestions from registered service declarations,
setting metadata and indexed export-file metadata. Export-folder scanning runs
in the background for ordinary Auto queries or when export mode is chosen or
refreshed. Typing does not log
into services, fetch manifests, contact
JustWatch or query remote vaults. Settings search indexes labels, descriptions
and keys, never saved values, passwords or tokens.

For a fuzzy or ambiguous service query, Enter first moves to the result list.
Choose the intended result with the mouse or arrows and Enter. UniDL does not
open the first service merely because it exists. A supported URL opens directly
only when exactly one service accepts it; multiple matches require a choice.
The original URL, including case-sensitive signatures, is passed unchanged.

Auto and **Search vault** show local key results directly while typing, without
opening another page. Auto previews up to three matching local records; the
vault mode lists up to forty with the ordinary Load more control. Empty input
does not reveal the whole vault. The same Search vaults target selection and
local-only backend rules as the full vault search apply; choosing no vaults or
remote-only targets does not query local databases.

Rapid edits are coalesced for 100 ms and local SQLite lookups run off the UI
thread. Old results are cleared immediately when the query or target selection
changes; late results cannot replace the current query. Click a key result or
highlight it and press Enter/`ctrl+y` to copy its complete KID:KEY pair using
the existing clipboard behavior. Press `ctrl+r` in vault mode to refresh the
same query after database changes.

The **Search vault** action still opens the existing full vault search screen.
Remote lookup retains its existing **Search remote vault** action and backend/
service selection. Typing, local results and clipboard actions never perform a
remote search, license request or vault write.

**Add keys** is a separate, explicit write action, not an automatic response to
pasting a key into Auto or Search vault. Selecting the mode or typing a pair
does not write anything. Enter or Go opens the existing editor; its destination
selector, input validation, conflict confirmation and write reporting remain
unchanged. Local vaults and enabled writable remote vaults follow the existing
write policy. Remote manual writes require both `remote_vault` and
`remote_vault_manual_add`; read-only/no-push backends cannot receive keys.
Save only after reviewing the editor's service and selected destinations.

## Export imports and navigation

Auto also searches local export filenames, titles, episodes and service names
for ordinary text queries. Matching files appear without switching modes.
Up to three file matches are previewed; **Search export files** opens all
matches with the same query. The first ordinary query starts a background scan;
later typing filters that index, and returning home refreshes it. URLs, KID/key
queries and explicit service-search modes retain their existing routing.
The empty Auto home still shows no file list.

Choose **Search export files** (or `/exports`) to browse the configured exports
folder, newest files first. An empty query shows recent files; typing filters
file names, service names/IDs and the titles/episodes inside batch files.
Scanning runs in the background; typing filters cached metadata rather than
re-reading every file. Keys, authorization headers and manifest URLs are not
part of the search index. Press `ctrl+r` to refresh files that were added or
changed, or choose this mode again.

Click a result, or use arrows and Enter to choose it. Enter from the input first
focuses the results; scanning or filtering alone never starts a download.
Single-title files enter the existing delivery flow directly. Multi-title files
open the existing title selector, where you can choose one, several or all
videos before continuing. Back returns to the same export search and query.
The source file is re-read when selected; unreadable or removed files report an
error without starting an import. The existing export-folder import screen and
native/third-party import rules are unchanged.

In **Auto** or **Open link**, paste an existing UniDL export JSON path and press
Enter. Absolute paths beginning with `/`, paths with spaces and surrounding
quotes are supported. A paste never opens the slash menu. If typing an absolute
path by hand, close the menu with Escape and continue after the retained slash.
Single-title exports use normal delivery; multi-title exports open the existing
entry selector rather than starting an arbitrary episode.

Back from a service, search, setting or import screen returns to this same home
with its mode, input and result selection retained. On the home itself,
`ctrl+b` moves focus from results to the input, then clears the query, then
returns a non-Auto mode to Auto. The top-right **Search** button and `ctrl+f`
open the existing global search page, just as on the classic home; they do not
silently focus an input that may already have focus. Back returns to the same
home mode and query. Click the large input to focus it. The existing settings,
command palette and quit controls remain available.

The interface and Auto result-type preferences are saved. Search text, the chosen service scope
and the result cursor are session state, not new persistent search history.
