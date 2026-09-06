# Security policy

unidl handles unusually sensitive local material: account credentials, browser
cookies, session tokens, CDM device files and secrets, content keys, remote-vault
tokens, exported commands and debug logs. Treat a disclosure involving any of
these as a security issue even when no source-code vulnerability is involved.

## Reporting a vulnerability

Use the repository host's private security-reporting channel. If no private
channel is enabled, contact the maintainers privately before opening a public
issue. A public issue must not contain working credentials, cookies, bearer
tokens, device files, CDM or vault secrets, full `KID:key` pairs, private URLs or
unredacted logs.

Include the affected version or revision, operating system, impact, minimal
reproduction and whether the issue requires a malicious config, service response
or local file. Replace secrets with stable placeholders and trim response bodies
to the smallest structure that demonstrates the problem.

The project is under active development; security fixes target the current
codebase. There is not yet a separately maintained long-term-support release.

## Sensitive files and defaults

- Runtime settings, tokens, cookies, CDM material, vaults, logs, command files
  and portable exports are enforced as owner-only (`0600`, inside `0700` state
  directories) where POSIX permissions are available. Keep any YAML containing
  a remote endpoint, token or other secret private and remove the secret before
  committing.
- Never commit `tokens/`, `cdm/`, `db/`, `cookies/`, `helpers/`, command exports,
  logs, `.wvd`, `.prd`, `.mld`, wasm device modules or browser cookie exports.
- Debug mode can record full URLs, headers and response previews. Review and
  redact a log before sharing it.
- A `PartnerAuthorization` URL is a single-use bearer secret. Transfer it only
  through core's in-memory handoff, consume it immediately, and never render,
  log, serialize, queue or persist it. See
  [partner-authorization.md](partner-authorization.md).
- Use HTTPS for remote CDMs and API vaults. Tokens are bearer credentials;
  `no_push: true` prevents writes but does not make an untrusted vault safe.
- A remote CDM sees init data and licence responses; a writable remote vault sees
  acquired content keys. Configure only endpoints you control or trust.
- Service code runs through the typed native service contract. Do not add an
  alternate execution path that bypasses authentication, DRM, vault or audit
  boundaries.

If a secret or device was exposed, remove the shared artifact, rotate or revoke
the credential at its issuer, replace remote-vault/CDM tokens, invalidate cached
sessions where possible and only then publish a redacted incident description.

## Scope

Security reports include credential or secret disclosure, unsafe path handling,
command injection, untrusted helper execution, cross-service cookie/header
leakage, remote CDM or vault authentication bypass, and key-vault confidentiality
or integrity failures.

Service outages, catalogue changes, region restrictions and account entitlement
errors are normally compatibility issues unless they expose or alter sensitive
data. Questions about whether a user is authorised to download particular media
are legal or account matters, not software vulnerabilities.
