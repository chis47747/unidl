# Scheduled downloads & Upcoming Radar

[English](scheduled-downloads.md) | [简体中文](zh-CN/scheduled-downloads.md)

UniDL can schedule any registered service to run at a future time — either from
a direct URL / content ID or through that service's own search flow — with
automatic episode selection, release-window retries, an ongoing watch ledger,
an aggregated multi-source **Upcoming Radar**, optional 2:3 poster previews,
and unattended OS background hosting (`launchd`, `systemd --user`, and Windows
Task Scheduler).

## Opening the manager

You can open **Scheduled downloads** from anywhere in the TUI:

- **Home status row**: click the **`schedule`** chip (`cdm · drm · after picking · ready · import · schedule`). When jobs are pending, queued, running, or need attention, the chip summarizes the state directly (for example `schedule 2 · in 3h` or `schedule 1 !`).
- **Command palette**: press `ctrl+p` and search `Scheduled downloads` (aliases: `schedule`, `timer`, `upcoming`, `radar`, `cron`, `launchd`).
- **Experimental (Beta) Home**: enter `/schedule` to open the Scheduled tab or `/radar` to open Upcoming Radar directly.
- **JustWatch search & availability screens**: highlight any title and press `t` to open the schedule editor prefilled with that title, year, media type, poster, and synopsis.

## Manager layout

The manager uses UniDL's four-tab layout (`1`–`4` or mouse click), with a
read-only summary pill in the status row and context-sensitive action buttons
at the bottom:

| Tab | Shortcut | Purpose |
|-----|----------|---------|
| **Scheduled** | `1` | Manage timed jobs (`+ New job`, `Edit`, `Run now`, `Pause / Enable`, `Delete`) with a right-hand detail and poster inspector. |
| **Upcoming radar** | `2` | Browse upcoming streaming movies, series, and anime grouped by date (`+ Schedule selected`, `Refresh`, `Installed only`, live filter box). |
| **History** | `3` | Inspect recent scheduled executions, view redacted logs (`Enter` / `View log`, `ctrl+y` copy), rerun, or clear history. |
| **Settings** | `4` | Configure Background host scheduler, Upcoming release sources, TMDB API key, Upcoming radar region, Installed services only, and Poster preview in UniDL's standard two-column settings list. |

Keyboard shortcuts remain available alongside the clickable action buttons:
`n` creates a new job, `enter` edits / schedules / opens logs or settings, `r`
runs now or refreshes radar, `space` enables/pauses a job, `d` deletes a job,
`o` toggles installed services only, `p` toggles the poster inspector, and `c`
clears history.

## Creating and testing a scheduled job

Click **`+ New job`** (or press `n`, or select a title from Upcoming Radar /
JustWatch) to open the editor modal.

### 1. Service & target

- **Service**: any installed service. Changing the service immediately runs a
  live preflight check showing whether the service is installed, signed in, and
  has a usable CDM (`✓ Ready` / `⚠ Sign in required` / `✗`).
- **Lookup mode**:
  - **Direct URL / ID**: passes the URL or content identifier straight to the
    service's `open_url` flow.
  - **In-service search**: runs the service's own `search` flow and uses
    **conservative disambiguation** (normalized title, optional release year,
    and media type). If multiple candidates remain ambiguous, UniDL refuses to
    guess and marks the job `needs_review` instead of downloading the wrong
    title.
- **Display name**, **Media type** (`Auto`, `Movie`, `Series`), and **Year**
  help keep the queue legible and narrow search matching.

### 2. Content & episodes

- **Movie / single**: resolves and downloads a single title.
- **Specific episodes**: accepts UniDL's range syntax in **Episode expression**
  (for example `S01E06`, `S01E01-E05`, `S01E01,S01E03`, or `all`).
- **Watch new episodes**: pairs with `Repeat = Watch ongoing`. UniDL records
  every successfully downloaded episode in the job's `completed_episodes`
  ledger and skips already-downloaded episodes on subsequent runs.

Only episodes actually delivered count toward a bounded selection. If the
service lists two episodes but returns one, the other remains pending. On
retry, a season that is already complete is skipped. If a one-time job was
interrupted after its final episode was recorded, recovery marks it done.
Saving a name change in an open editor also preserves the latest run state.

### 3. Timing, retry window & action

- **Start time**: accepts local wall-clock time (`YYYY-MM-DD HH:MM`), full ISO
  UTC timestamps (`2026-12-04T05:00:00Z`), date-only strings (`2026-12-04`,
  automatically shifted using provider drop-time offsets where known), or
  relative offsets (`+30m`, `+2h`). Quick preset buttons (`+1h`, `Tonight 20:00`,
  `Tomorrow 09:00`) fill the field in one click.
- **Repeat**: `Once`, `Daily`, `Weekly`, or `Watch ongoing`.
- **Release retry window**: `No retries`, `6`, `12`, `24`, or `48 hours`. If a
  scheduled title or target episode has not appeared on the platform yet at the
  trigger time, the job enters `waiting_release` and retries automatically
  within the window instead of failing immediately.
- **After resolving**: `Download`, `Export portable JSON`, or `Save command only`.
  JSON export uses the service's master/media manifest setting, as in the TUI.
- **Test lookup (Dry-run)**: runs the service lookup and episode matching
  without downloading media or mutating the saved job status, confirming that
  your query and episode expression match what you expect before the release
  time arrives.

## Job states

| State | Meaning |
|-------|---------|
| `scheduled` | Enabled and waiting for its next trigger time. |
| `waiting_release` | Target title or episode not published yet; retrying inside the configured release retry window. |
| `queued` | Due while a foreground download/session is active; starts automatically once the active session finishes. |
| `running` | Currently executing. |
| `done` | Completed successfully (one-shot jobs finish; repeating/watch jobs advance to the next run). |
| `paused` | Temporarily disabled by the user. |
| `needs_auth` | Service session is missing or expired; sign in via the TUI and rerun. |
| `needs_review` | Conservative search matching found ambiguous candidates; refine target/year or switch to Direct URL. |
| `failed` | Unrecoverable error or release retry window exhausted; inspect the History log for details. |

## Upcoming Radar & data sources

The **Upcoming radar** tab aggregates upcoming streaming movies, series, and
anime from up to four configurable sources:

| Source | Default | Strengths | API key required |
|--------|---------|-----------|------------------|
| **JustWatch** | Enabled | Regional streaming catalogue releases and platform-to-service mapping. | No |
| **TVMaze** | Enabled | Exact UTC episode air times (`SxxExx`) for TV series. | No |
| **AniList** | Enabled | Exact airing timestamps for seasonal anime episodes. | No |
| **TMDB** | Optional | Broad upcoming movie and TV release calendars with high-res posters. | Yes (v3 key or v4 token) |

When multiple sources are enabled, UniDL fetches them concurrently and merges
matching titles (for example combining JustWatch's platform/service mapping and
poster with TVMaze's exact UTC air time and episode code).

Configure sources in **Scheduled downloads → Settings → Upcoming release
sources** (or **Settings → JustWatch → Upcoming release sources**):

- Press `space` to toggle sources (**multi-select**).
- Press `o` to pick only the highlighted source (**single-select**).
- Selecting **TMDB** when no key is saved immediately opens the masked **TMDB
  API Key** prompt. Both TMDB v3 API keys and v4 Read Access Tokens are
  accepted. The key is stored privately in `settings.json` (`0600`) and
  automatically masked/redacted in logs, errors, and readiness diagnostics.

## Poster preview & terminal compatibility

The right-hand inspector renders a 2:3 poster above structured metadata and
synopsis across **Scheduled downloads**, **Upcoming radar**, **JustWatch
search**, and **JustWatch availability**.

The global `poster_preview` setting (editable in **Settings → Interface &
diagnostics → Poster preview**, **Scheduled downloads → Settings → Poster
preview**, or toggled per screen with `p`) has three modes:

| Mode | Behavior |
|------|----------|
| `auto` *(default)* | Shows native high-resolution posters **only** in terminals supporting Kitty Graphics Protocol (Ghostty, Kitty) or Sixel (iTerm2, WezTerm) when the viewport is at least `104×20`. Automatically hides the poster panel with **zero HTTP requests** on macOS native `Terminal.app` (`Apple_Terminal`), basic terminals, `dumb` terminals, and narrow windows. |
| `always` | Uses native graphics when supported; on macOS `Terminal.app` falls back to solid colour cells (avoiding half-block hairline gaps), and on other truecolor terminals falls back to half-cell blocks. Still collapses automatically below `104×20` or in `dumb` terminals. |
| `off` | Always hides posters and never fetches poster images. |

Poster requests use a 150 ms highlight debounce, a bounded in-memory LRU plus a
private `0600` disk cache under `<paths.cache>/posters/`, send no service
`Cookie` or `Authorization` headers, and clean up native Kitty/Sixel layers on
screen suspend, tab switch, hide, and unmount so graphics never ghost across
modals.

## Foreground conflicts & OS background hosting

### While UniDL is open (in-app scheduler)

The TUI polls due jobs every 30 seconds. If a foreground download or service
session is already running, due jobs are **not** dropped or run concurrently on
top of your active session — they automatically transition to `queued` and run
as soon as the active foreground session ends.

### When UniDL is closed (OS background host scheduler)

Install the native OS scheduler from **Scheduled downloads → Settings →
Background host scheduler** (or via CLI) so due jobs execute automatically even
after you quit the TUI:

- **macOS**: installs a `launchd` LaunchAgent plist at
  `~/Library/LaunchAgents/com.unidl.scheduler.plist` (default interval: every 5
  minutes, configurable to 5 / 10 / 15 / 30 minutes).
- **Linux**: installs `unidl-scheduler.service` and `unidl-scheduler.timer`
  under `~/.config/systemd/user/`.
- **Windows**: registers a Task Scheduler job (`UniDL-Scheduler`) via
  `schtasks`.

A cross-process file lock (`<paths.home>/schedules.lock`) ensures the TUI, CLI,
and OS background host never execute the same due job twice simultaneously.

### Unattended authentication boundary

Scheduled runs execute headless and **never** block waiting for interactive
login prompts, device codes, or passwords. If a service's cached session or
cookies have expired, the job stops immediately with status `needs_auth` so you
can sign back in through the TUI and rerun it.

## CLI commands

Every scheduler action is also available from the command line using the same
store and lock:

```console
# List all scheduled jobs and their next trigger time
unidl schedule list

# Run all currently due jobs once (respects the cross-process lock)
unidl schedule run

# Test lookup and episode matching for one job without downloading
unidl schedule run --id <job-id> --dry-run

# Force-run a specific job immediately even if its run_at is in the future
unidl schedule run --id <job-id> --force

# Run a foreground polling loop (useful in containers or tmux/screen)
unidl schedule daemon --interval 60

# Inspect, install/update, or remove the OS background host scheduler
unidl schedule host status
unidl schedule host install --interval 5
unidl schedule host uninstall
```

Pass `--config /path/to/unidl.yaml` before `schedule` whenever you run against
a non-default configuration file; `host install` embeds that exact config path
and Python executable into the generated `launchd` / `systemd` / `schtasks`
entry.

## Storage & privacy

| Path | Purpose | Permissions |
|------|---------|-------------|
| `<paths.home>/schedules.json` | Saved scheduled jobs, watch ledgers, and run history (atomic writes). | `0600` |
| `<paths.home>/schedules.lock` | Cross-process runner lock preventing overlapping runs. | Private |
| `<paths.home>/settings.json` | Persists `upcoming_sources`, `tmdb_api_key`, `poster_preview`, and regions. | `0600` |
| `<paths.cache>/posters/` | Bounded 7-day / 25 MB thumbnail cache for poster previews. | `0600` |

Execution logs stored in history and copied via `ctrl+y` are automatically
redacted so bearer tokens, cookies, and API keys are never written to disk or
clipboard.
