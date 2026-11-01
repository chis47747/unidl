# Mixed import download queue

Open **Import → Mixed download queue**, the bold first entry in the import list.
This queue combines individual titles from different exports, services and media
types without merging their manifests, keys or service settings into a new file.

## Build and run

1. Put supported export JSON files under the exports folder. **Ctrl+O** opens
   that folder; **Ctrl+R** refreshes the file list.
2. Choose an export on the left. Use **Space** to select titles, **a** to select
   all and **n** to clear. **Enter** adds the selection; with no boxes selected
   it adds the highlighted title. **Back** returns without adding anything.
3. Repeat for other files: for example, episodes 1 and 3, a film from another
   service, then songs 1, 4 and 5 from an album.
4. Review the right-hand queue. **Up / Down** buttons (or **Ctrl+Up / Ctrl+Down**)
   move its highlighted title. **Remove / Delete** removes it; **Clear** empties
   the queue.
5. Choose **Start queue** or **Ctrl+Enter**. Titles download sequentially in the
   displayed order.

The action bar is a single compact row: editing controls are on the left and
**Start queue** on the right. In narrow windows the move controls show only
arrows, with full labels in their tooltips. Unavailable actions are dimmed
(including moving the first title up or the last one down). The empty queue
shows an add-titles hint; file and queue rows keep titles and source metadata on
separate, width-fitted lines in both light and dark themes.

Files are appended in confirmation order. Within each confirmation, titles
follow their export order, not the order boxes were ticked. Re-adding an entry
from the same file does not duplicate it. Different files stay separate even
when their titles, IDs or release names match.

## Independent delivery contexts

Each item keeps its source format, manifests, headers, proxy, exported keys,
selection/output intent, chapters, attachments and lyrics. Preparation does not
modify the source export objects or files.

- Native UniDL exports use the installed source service's settings and delivery
  hooks when available, just like ordinary import.
- Without that service, native exports use portable generic delivery.
- Third-party exports always use generic delivery. Their service labels never
  grant access to installed service accounts, cookies, helpers or licensing.

Each title uses its own source's track selection, output format, naming and
destination settings. Import always downloads: an after-picking preference to
save another command/export does not replace it. Interactive track selection
still asks per title; building a queue does not silently switch to auto mode.

Signed URLs can expire, keys can be incomplete, or a native custom delivery
hook can need an installed helper. Adding a title does not contact a service
to refresh or re-authorize it.

## Errors, navigation and retry

One failed title is recorded and the next one runs. Retry restores the failed
title's original service context, never the context left by the last download.
Completed and pending titles retain the existing queue-details inspection.

Pause, resume, skip, Back and Quit use existing delivery controls. Back leaves
the import session and returns to the builder with selections and order intact;
another Back returns to the import list. Quit keeps normal application exit
behavior. Live streams without a stop limit keep recording until stopped, so
later items wait for them.

This is an in-memory queue for this screen, not a persistent background
scheduler. Refreshing the file list preserves queued source snapshots.
Start runs the entire displayed queue, including if started again. Use the
delivery retry action to retry only failed/cancelled items.

The shared vault collection and delivery engine are reused. Service settings
and lifecycle ownership remain per source. Per-title download diagnostics
follow the active service's policy; a mixed queue does not impose one source's
session debug policy on unrelated services.
