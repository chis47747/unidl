# Content-key verification

**Verify content keys before download** is an optional, best-effort VOD preflight.
It is off by default. Enable it under **Settings → Download behavior**, or override
it for one service under **Settings → License & vaults**. A service follows the
global value until an override is saved; resetting the service value restores
inheritance.

## When it runs

The order is: finalize output tracks, acquire their required keys according to
the existing License action, verify selected encrypted media, then download.
The checker never samples unselected profiles or languages. Licensing Before,
After and Custom retain their existing responsibilities.

Live playbacks are always skipped, including live-to-VOD replay modes. Clear
tracks, track listing, saving an export and generating a command alone do not
trigger media verification. Generated download commands include `--verify-keys`
when enabled; running that command performs the preflight at execution time.
The standalone downloader checks supplied keys but cannot obtain another service
license or use account credentials.

## What a sample proves

The checker uses bounded HTTP byte ranges and complete fragmented MP4 samples,
not arbitrary downloader chunks. An MP4 SIDX index can locate distributed media
fragments without downloading the intervening content. A bounded box-header walk
is used when no suitable index is present.

A clear lead is not a successful key check. The internal CENC/CBCS decryptor must
change actual media payload for the target KID, and the first media sample in the
decode window must be affected. Clear-only starts and mixed windows that cannot
be judged safely are skipped in favor of another sample. FFmpeg must then produce
decoded video frames or audio samples. Header recognition and a successful
decrypt exit code alone do not count.

The initial implementation supports the internal fragmented-MP4 CENC/CBCS
surface. Other containers and transport ciphers need their own future adapters.
Missing decoders, unsupported layouts, incomplete samples, unavailable media and
budget exhaustion are **inconclusive**, not bad keys. An inconclusive result
warns and continues the normal download without claiming successful validation.
A candidate is rejected only after two encrypted samples fail decoding. A pass
is an early warning aid, not proof that every later sample or rotated KID works.

Sampling is limited to 32 MiB of fetched data, 48 requests, an 8 MiB per-request
cap, and a 45-second sampling/decode budget per title. Each decode also has an
8-second timeout and uses one decoder thread. Vault and service license requests
retain their own timeouts. Ciphertext samples are reused across key candidates;
known response sizes are reused by the later video bitrate probe. Only size
metadata survives preflight, not media payloads retained on every batch row.
Temporary sample files are isolated from normal download and resume files.

## Candidate order and failure behavior

Vault reads remain local-first, then remote vaults in configuration order, using
only enabled read targets. For each selected KID:

1. Check the key already returned for the download.
2. If it came from a vault and fails, try other candidates from the selected
   local vaults, then the selected remote vaults. Bounded older candidates from a
   local database are considered too.
3. Skip repeated identical candidates. Do not query an already-used remote source
   again just to receive its same rejected answer.
4. If all vault candidates fail, ask the configured service/CDM once for the
   failing selected inventory, retaining its authorization context and device.
5. If that result fails or repeats a rejected candidate, stop this title. A key
   obtained directly from the CDM, or supplied by an import, does not start a
   license-retry loop.

No media probe runs during key acquisition itself. The checker does not switch
DRM systems, devices or playback profiles, and does not turn a portable import
into an authenticated service session. Batch failure follows the existing
current-title failure policy.

## Vault safety

With verification enabled for a download, vault hits are provisional. Remote
results are backfilled locally only after successful verification, respecting
local write targets and read-only vaults. Freshly licensed keys are written only
if they passed verification and the existing write policies permit it. An
unselected key is not certified simply because another key passed.

This does not introduce automatic vault-to-vault synchronization. There is no
persistent bad-key blacklist, automatic deletion of local records, remote
deletion or bad-key reporting. Failed candidates are excluded only for the
current preflight. Inconclusive candidates are not marked bad or propagated as
verified keys.
