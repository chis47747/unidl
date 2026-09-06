# Live channels

A live stream is the only job here that does not end on its own. Everything below
follows from that.

## Recording is confirmed after track selection

`live_record` on the main screen is the saved default for whether a live channel
is *recorded* or only *resolved*. In the interactive TUI the question is asked
after the final tracks have been selected and before a recorder starts:

| `live_record` | what happens to a live channel |
|---|---|
| off (default) | preselect **Save the command only** |
| on | preselect **Record it now** |

The user may change that answer for this run without rewriting the saved default.
In a headless run the stored value remains authoritative. Off is the default
because the failure modes are not symmetric. A live job left
running fills a disk and holds a session for as long as its limit allows; a live
job that was not started costs one keypress to start again. The delivery screen
says which of the two happened rather than quietly doing nothing:

```
live     not recording, saving the command instead
!  live recording was not selected; saving the command
```

## Length is per service

`live_record_limit` lives in each service's own settings as the default, because
it is a property of what is being recorded: a football match, a rolling news
channel and a radio station want three different numbers. After tracks are final
and **Record it now** was chosen, the TUI asks for this run's length. Changing it
there does not rewrite the saved service default. It accepts `HH:MM:SS`, plain
seconds, or `1h20m` — whatever native delivery core accepts. The default shown in
the prompt is `00:00:00`, which means **no length limit**: recording continues
until the user presses Stop, Back or Esc. A non-zero value is the explicit safety
limit for unattended/headless recording.

Back is local to this sequence. From the duration field it returns to the
record-vs-command choice; from replay inspection it returns to the duration; from
the replay-mode choice it returns to replay inspection; and from an offset field
it returns to the replay-mode choice. Back on the first record-vs-command choice
reveals the track picker that preceded it when interactive track selection is
enabled.

Live settings only exist on services that declare `SUPPORTS_LIVE`. A recording
length on a service with no live channels would be a setting that can be changed
and never read.

## The replay window

Most live manifests publish more than the live edge: a stretch of the recent past
that can still be rewound into — a replay, or DVR, window. `live_replay` (per
service, off by default) preselects whether the post-track question starts at the
live edge or offers to inspect that window.

With it **off**, **At the live edge** is preselected, which is what a recorder
normally wants. The user can still choose to inspect the current window once.

With it **on**, **Inspect the replay / DVR window** is preselected. Only after
that explicit choice does UniDL fetch the selected media playlists to measure the
window; if a usable window exists, the delivery screen asks what to take:

| choice | native delivery core | ends when |
|---|---|---|
| from now, the live edge | *(nothing extra)* | the length limit |
| from the start of the window | `--live-dvr-from-start` | the length limit |
| the window as it stands, once | `--live-perform-as-vod` | the window has been fetched |
| a stretch of the window | `--live-dvr-start-at`, `--live-dvr-end-at` | the end offset |

The last two are finite, so no length limit is imposed on them — it could only
truncate a window that was going to finish anyway. A stretch is typed as one
field: `00:30:00`, or `00:30:00-01:15:00`, measured from the window's start. An
unparseable answer records from the live edge and says so rather than passing
nonsense on.

### Why it is a question and not a setting

How much replay exists is a property of the stream at the moment it is opened,
not of the service. A 32-second buffer and a six-hour catch-up window are both
"live", and the useful answer differs. So the setting is a remembered default and
the actual choice is asked after the manifest and final selected tracks are known.

Measuring it costs a second parse. The normal one stops at the master playlist —
everything the track picker needs is described there — and without the child
playlists nothing knows the window length, or even that the stream is live:
`is_live` comes back False and `duration` empty. So the measurement is taken only
when the answer is about to be used, and only when replay inspection is selected.
Anything under two minutes is treated as buffer rather than a window: BBC live radio
publishes 32 seconds, and a broadcaster's catch-up window is measured in hours.

## Checking it

Use a local playlist with no `ENDLIST` or a deterministic fake refresh loop, then
run the project test suite:

```bash
python -m pytest -q
```

Live fixtures should be local and redacted. Never commit credentials, signed
manifests or content keys.
