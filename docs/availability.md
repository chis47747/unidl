# Availability lookup

Global Search can answer the question that comes before choosing a service:
who carries this title in the regions that matter to me? The lookup uses
JustWatch, then maps each provider back to a native service registered in unidl.

## Using it

1. From Home, click Search or press `ctrl+f`.
2. Type a film or series title.
3. Open **Where can I watch this** under Availability.
4. Choose the matching title.
5. Open an offer that names an installed service.

If that service supports search, unidl opens it with the title already filled
in. Otherwise it opens the service menu and asks you to supply the service's own
URL or identifier. Disc-only offers and providers with no matching service are
shown but cannot be opened.

Channel add-ons follow the platform that actually hosts playback. The mapper
checks the reseller/platform prefix before the content brand and opens only a
registered host service. If the host platform is not registered, the row shows
no service instead of falling back to the content brand. This prevents a
third-party channel add-on from being mistaken for the provider's native app.

Availability rows are grouped by region and then by subscription, free,
ad-supported, rental and purchase offers. Series offers include the seasons
reported by the provider. `ctrl+y` copies the highlighted offer URL, falling
back to the title's JustWatch page.

## Regions

Press `ctrl+r` on a title or offer screen to choose regions. The picker supports
filtering, `space` to tick entries and `enter` to confirm. Inside the picker,
`ctrl+r` restores the built-in defaults.

The same value is editable under Global Settings:

```text
justwatch_regions = US,GB,CA,AU,JP
```

It is stored in `<paths.home>/settings.json`, not `unidl.yaml`, because it is a
browsing preference. Codes are ISO country codes, de-duplicated while keeping
their order. An empty or invalid list falls back to the built-in defaults.

Each region is a separate network request. A shorter list returns faster and a
failure in one region is reported on that region instead of discarding the
others.

## What the result means

JustWatch availability is discovery data, not an entitlement check. An offer
does not prove that your account tier, location or device can play it, and the
provider-to-service mapping is necessarily best effort. The selected service
still performs its normal login, region and playback checks.

No lookup runs on every search keystroke. Global Search only offers the action;
the network request starts after you choose it.

## Checking it

Run the project test suite for offline mapping and UI checks:

```bash
python -m pytest -q
```

For a live lookup, use a configured region list and verify that each provider
offer resolves to the intended registered service. Availability is discovery
data; the selected service still performs its own entitlement and region checks.
