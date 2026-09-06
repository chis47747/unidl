# Partner authorization

`PartnerAuthorization` is the core contract for handing a short-lived sign-in
URL from one installed service to another without displaying, serializing or
persisting it. It is for a provider relationship such as a library BingePass
authorizing a separate streaming service. It is not a general browser-link
model and it does not let services call into one another.

The first implementation is Hoopla to Hallmark+:

```text
Hoopla service                 core                     Hallmark+ service
     |                           |                              |
     | call Hoopla /authorize    |                              |
     | create PartnerAuthorization                             |
     | yield PartnerHandoff ---->| validate and build receiver |
     |                           |----------------------------->|
     |                           |       claim URL once         |
     |                           |       complete Hallmark SSO  |
     |                           |       save Hallmark session  |
     |<--------------------------| PartnerAuthorizationResult   |
```

The URL belongs to the transfer, not to configuration or runtime state. The
producer obtains it from its own API, core carries it in memory, and the
consumer immediately exchanges it for state owned by the consumer.

## Contract objects

The contract lives in `unidl.core.partner`.

### `PartnerAuthorization`

```python
PartnerAuthorization(
    url,
    source_service_id="producer",
    service_id="consumer",
    provider="Consumer display name",
    max_age=300.0,
)
```

| Field | Meaning |
|-------|---------|
| `url` | HTTPS handoff URL. Reading this property claims and erases it. |
| `source_service_id` | Registered service that obtained the authorization. |
| `service_id` | Registered service allowed to consume it. |
| `provider` | Non-sensitive display label used in status messages. |
| `max_age` | In-memory lifetime in seconds; five minutes by default. |
| `host` | Read-only, non-secret hostname metadata. |
| `claimed` | Whether the URL was consumed, expired or discarded. |

Construction rejects non-HTTPS URLs, invalid service IDs and non-positive
lifetimes. Metadata is read-only. `repr()` redacts the URL, pickling is refused,
and a second read of `.url` raises `PartnerAuthorizationError`. Expiration uses
a monotonic clock and erases the URL before raising.

### `PartnerHandoff`

`FlowContext.partner_handoff()` wraps the authorization in a `PartnerHandoff`
ask. This ask is a routing instruction, not visible content. The Textual session
driver resolves and builds the target service, then calls core delivery. It
never renders the URL in a widget or log.

### `PartnerAuthorizationResult`

The consumer returns:

```python
PartnerAuthorizationResult(
    service_id="consumer",
    authenticated=True,
    label="Consumer session saved",
    detail="Open Consumer to continue.",
)
```

`label` and `detail` are user-visible and must contain no URL, token, cookie,
credential or response body. The result is truthy when `authenticated` is true.

## Lifecycle and ownership

There are three owners with deliberately narrow responsibilities.

### Producer

The producer:

1. calls only its own API to create the partner authorization;
2. validates and classifies the returned destination;
3. creates `PartnerAuthorization` with explicit source and target IDs;
4. yields `ctx.partner_handoff(authorization)`;
5. handles only the non-sensitive result.

It does not open the target URL, read `.url`, instantiate the consumer, import
the consumer package or cache the URL for retry.

```python
from ...core.partner import PartnerAuthorization, PartnerAuthorizationResult


def _open_partner(self, ctx, client):
    # The producer API, not the consumer, mints this URL.
    target = client.create_partner_authorization()
    authorization = PartnerAuthorization(
        target,
        source_service_id=self.ID,
        service_id="consumer",
        provider="Consumer",
    )

    result = yield ctx.partner_handoff(authorization)
    if isinstance(result, PartnerAuthorizationResult) and result.authenticated:
        ctx.log(result.label or "Consumer session saved", "ok")
    elif isinstance(result, PartnerAuthorizationResult):
        ctx.error(result.detail or result.label or "partner authorization failed")
    else:
        ctx.error("partner authorization returned no result")
```

When a returned link is only an ordinary external browser login, keep it in a
separate model. Do not wrap every external URL in `PartnerAuthorization`: doing
so would claim that an installed consumer and an in-process exchange exist.

### Core

The session driver checks that `source_service_id` is the active producer,
resolves `service_id` through the service registry and builds a fresh consumer.
`deliver_partner_authorization()` then verifies:

- the receiver's `ID` exactly matches `service_id`;
- the receiver explicitly accepts `source_service_id`;
- the receiver returns `PartnerAuthorizationResult`.

Delivery is synchronous. Its `finally` block discards the URL on success,
refusal or exception, including failures that happen before the consumer reads
it. Core invalidates the consumer's account hint after delivery, but it does not
interpret, store or refresh the resulting service session.

Producer and consumer services should use the Flow handoff. Direct calls to
`deliver_partner_authorization()` are for core drivers and focused tests, not a
shortcut for service-to-service imports.

### Consumer

The consumer opts in with an allowlist and implements the receiver hook:

```python
from ...core.partner import (
    PartnerAuthorization,
    PartnerAuthorizationError,
    PartnerAuthorizationResult,
)
from ...core.service import Service


class ConsumerError(RuntimeError):
    pass


class Consumer(Service):
    ID = "consumer"
    PARTNER_AUTHORIZATION_SOURCES = ("producer",)

    def consume_partner_authorization(
        self, authorization: PartnerAuthorization
    ) -> PartnerAuthorizationResult:
        if self.login_method() != "partner":
            return PartnerAuthorizationResult(
                self.ID,
                False,
                label="Consumer is set to account login",
            )
        if (
            authorization.service_id != self.ID
            or not self.accepts_partner_authorization(
                authorization.source_service_id
            )
        ):
            return PartnerAuthorizationResult(
                self.ID,
                False,
                label="Consumer refused the partner authorization",
            )

        try:
            # This is the only read. It claims and erases the contract URL.
            self.api.exchange_partner_url(authorization.url)
        except (ConsumerError, PartnerAuthorizationError) as exc:
            return PartnerAuthorizationResult(
                self.ID,
                False,
                label="Consumer partner sign-in failed",
                detail=str(exc),
            )

        return PartnerAuthorizationResult(
            self.ID,
            True,
            label="Consumer session saved",
        )
```

The consumer must validate the URL's scheme, authority, redirects and expected
payload before sending credentials or tokens. The generic contract proves only
that the URL is HTTPS; provider-specific host policy belongs in the consumer.

After claiming `.url`, complete the exchange immediately. Save only the final
consumer session through that service's own `TokenStore` and cookie profile.
The consumer must not:

- call the producer's authorization endpoint;
- import the producer service or API;
- put the one-time URL in tokens, cookies, settings, cache, logs or exceptions;
- treat a producer refresh token as a consumer refresh token;
- leave the URL in a queue, callback, task record or retry file;
- fall back to account, anonymous or browser login after a failed handoff unless
  the user explicitly selected that separate login method.

## Multiple login methods

A consumer may also support its own account credentials. Expose that as an
explicit service setting rather than an automatic fallback:

```python
Setting(
    key="login_method",
    label="Login method",
    kind="choice",
    options=[
        Option("partner", "Partner SSO"),
        Option("account", "Account - email and password"),
    ],
    default="partner",
    resets_session=True,
)
```

`resets_session=True` signs out when the setting changes. The service should
also tag cached state with its login method and refuse to load a token from the
other authentication domain. Partner cookies and account tokens may have
similar shapes while remaining invalid in one another's tenant.

When account mode is selected, `consume_partner_authorization()` returns a
non-sensitive refusal without reading `.url`; core then discards it. When
partner mode is selected, missing or expired partner state should direct the
user back to the producer workflow, not silently ask for account credentials.

## Hallmark+ reference implementation

Hoopla and Hallmark+ demonstrate the complete pattern:

- Hoopla calls its own BingePass authorization API.
- `hoopla.api.partner_authorization()` classifies Hallmark destinations and
  creates a contract targeting `hallmark`.
- Hoopla yields `ctx.partner_handoff()` and never renders Hallmark's URL.
- core builds Hallmark and verifies that it accepts authorization from `hoopla`.
- Hallmark reads `.url` once, follows only approved Hallmark authorities,
  completes the Hallmark login exchange and stores the resulting Hallmark token
  and `partner_sso` cookie profile.
- Hallmark never calls Hoopla's authorization API and never stores the handoff
  URL.
- Hallmark account mode remains a separate setting and uses only its TV token
  and refresh path.

Hallmark also shows why authorization and API transport may need separate
adapters. In partner mode, its TV client reads navigation, recommendations,
details, search and live-channel lists, while its Partner client owns profile,
cookie refresh, VOD/Live playback, subtitles and licence transport. The two
refresh domains are never mixed.

Reference files:

- `src/unidl/core/partner.py`
- `src/unidl/core/flow.py` (`PartnerHandoff` and `partner_handoff`)
- `src/unidl/tui/session.py` (`route_partner_authorization`)
- `src/unidl/services/hoopla/` (producer)
- `src/unidl/services/hallmark/` (consumer)

## Headless and test use

`AutoPresenter` deliberately refuses `PartnerHandoff`: it has no registry,
configuration or target-service builder, so pretending to deliver would bypass
the contract. A headless integration presenter must provide the same resolver
and call `deliver_partner_authorization()` with the built receiver.

Focused consumer tests may call core delivery directly:

```python
result = deliver_partner_authorization(authorization, receiver)
assert result.authenticated
assert authorization.claimed
```

Tests should cover all of these properties:

- non-HTTPS and invalid service IDs are refused;
- `repr()` and errors do not expose the URL;
- the URL is single-use, expires and cannot be pickled;
- wrong targets and untrusted producers are refused before consumption;
- the URL is erased on every success and failure path;
- the producer emits the correct source/target contract and makes its own
  authorization request exactly once;
- the consumer rejects the handoff in another login mode without reading it;
- the consumer persists only its final state and uses owner-only files;
- account and partner refresh tokens cannot cross authentication domains;
- live, VOD and licence traffic uses the session selected by the login method.

Run the project smoke suite alongside focused partner-service tests:

```bash
python -m pytest -q
```

Use fixture URLs and token placeholders. Live checks may report counts, types and
success states, but must not print authorization URLs, tokens, cookies, signed
manifests, licence responses or content keys.

## Adoption checklist

- [ ] The producer alone calls its authorization endpoint.
- [ ] Destination classification is explicit and fail-closed.
- [ ] The contract names registered source and target service IDs.
- [ ] The producer yields `ctx.partner_handoff()` and never reads `.url`.
- [ ] The consumer declares `PARTNER_AUTHORIZATION_SOURCES`.
- [ ] The consumer validates its login mode and the target before reading `.url`.
- [ ] The consumer claims `.url` once and completes the exchange synchronously.
- [ ] Redirect and host validation is provider-specific and fail-closed.
- [ ] Only final consumer state is saved in consumer-owned token/cookie storage.
- [ ] Results and exceptions contain no sensitive values.
- [ ] Account login is explicit and isolated, not a failure fallback.
- [ ] Core contract, producer and consumer fixture tests all pass.
