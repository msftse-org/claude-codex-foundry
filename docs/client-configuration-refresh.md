# Client configuration refresh — experimental components

These are opt-in server/client increments of automatic configuration refresh,
not yet qualified for rollout. The API and enrollment page are disabled by default. Existing
JSON installations and inference authentication are unchanged unless a user
explicitly enrolls or migrates.

## Trust boundary

`GET /api/v1/me/client-configuration` returns the authenticated user's effective
model list from the **active published shared budget policy**, not unpublished
portal edits. Models need matching operator-reviewed deployment versions, token
bounds and current price records. A deployment does not grant access by itself.

Use a separate single-tenant Entra resource with a delegated
`configuration.read` scope, v2 access tokens and an explicit public-client
allowlist. Neither the portal resource/client nor the configuration resource
itself may be in that allowlist. The portal's `access_as_user` and Cognitive
Services inference tokens are not configuration credentials. Configuration
credentials cannot administer the portal or perform inference. This endpoint
rejects application-only tokens and user/group selectors.

Registration, consent and deployment wiring are subsequent
increments. Do not reuse the portal application to enable this endpoint.

## Operator settings

Set these through the reviewed deployment workflow, never from an HTTP request
or downloaded refresh response:

| Setting | Meaning |
| --- | --- |
| `PORTAL_CLIENT_CONFIGURATION_ENABLED` | Exactly `1` to opt in; `0` by default |
| `PORTAL_CONFIGURATION_RESOURCE_ID` | Distinct configuration resource application UUID |
| `PORTAL_CONFIGURATION_CLIENT_IDS_JSON` | JSON array of 1–10 distinct authorized public-client UUIDs |
| `BUDGET_ORGANIZATION_ID` | Stable organization UUID, already used by the shared runtime |
| `PORTAL_CONFIGURATION_DEPLOYMENTS_JSON` | Reviewed deployment map matching the budget runtime's bounds |
| `PORTAL_CLIENT_ENROLLMENT_ENABLED` | Exactly `1` enables the experimental link/ZIP onboarding page; `0` by default |
| `PORTAL_ENROLLMENT_ORIGIN` | Reviewed canonical HTTPS portal origin, without path, query or trailing slash |
| `PORTAL_ENROLLMENT_CLIENT_ID` | Dedicated organization public-client UUID, present in the configuration client allowlist; not Azure CLI |

The deployment map has this shape, with no additional fields:

```json
{
  "example-deployment": {
    "provider": "openai",
    "version": "reviewed-version",
    "max_input_tokens": 32768,
    "max_output_tokens": 4096,
    "supports_reasoning": true
  }
}
```

The service refuses unsafe/invalid enablement settings and local
owner-impersonation mode. Normal portal development works with the feature off.

## Response contract

```json
{
  "schema_version": 1,
  "organization_id": "00000000-0000-0000-0000-000000000001",
  "tenant_id": "00000000-0000-0000-0000-000000000002",
  "subject_id": "00000000-0000-0000-0000-000000000003",
  "state": "active",
  "clients": {
    "claude": {
      "route_id": "claude-gateway",
      "default_model": "example-sonnet",
      "models": [
        {"name": "example-sonnet", "family_tier": "sonnet", "family_default": true}
      ]
    },
    "codex": {
      "route_id": "codex-gateway",
      "default_model": "example-codex",
      "models": [{"name": "example-codex", "model": "gpt-example"}]
    }
  }
}
```

Only enabled providers appear. Empty lists/defaults are valid; when every
provider has no eligible models, `state` is `no_models`. These are declarative
model selections, not application settings. No URLs, commands, credentials,
budgets, roles or other users' data are returned. A later client must validate
the exact schema and pinned organization, tenant, subject and route IDs before
rendering locally reviewed templates.

The quoted `ETag` hashes canonical response bytes with SHA-256.
`If-None-Match` returns `304` only **after** fresh token, membership, policy and
deployment/price checks. An unrelated policy change need not change this user's
response. Polling makes no reservations, budget-counter updates, draft writes
or per-poll audit rows. Responses, including errors, use
`Cache-Control: private, no-store` and `Vary: Authorization`.

| Status | Meaning |
| --- | --- |
| `200` | Current own-user configuration |
| `304` | Current authorization checked; personal response unchanged |
| `400` | Query parameters are not supported |
| `401` | Invalid/expired or wrong-audience/client/scope credential |
| `403` | No active published membership |
| `404` | Feature disabled |
| `503` | Paused/missing/unavailable policy, invalid projection or concurrent publication |

Local freshness is a convenience, never an authorization grant. Inference still
passes current shared-runtime access and budget admission. A stale file, old
ETag or local rollback cannot restore revoked access. The final policy recheck
narrows publication races; it does not promise the response cannot become stale
immediately after being sent.

## Validation and rollout

Run the checks in [CONTRIBUTING](../CONTRIBUTING.md). Tests cover audience/client/
scope separation, malformed claims, own-user selection, revocation before
conditional reads, deployment/price mismatch, policy failure/concurrency and
absence of polling mutations.

Do not advertise automatic onboarding until enrollment, isolated credentials,
atomic reconciliation and native client/version qualification pass. Claude
Desktop's `modelCatalogUrl` mirrors Anthropic's signed metadata; it is not this
entitlement feed. Codex remote catalogs require a complete version-compatible
`ModelsResponse`, not this wrapper or a generic OpenAI models response.

## Client reader (not yet an installer)

`configuration_client.py` implements bounded data-only reads. It does not log
in, persist credentials/enrollment, schedule work or change application files.
The trusted setup channel supplies this descriptor; a refresh response may
never replace it:

```json
{
  "schema_version": 2,
  "organization_id": "00000000-0000-0000-0000-000000000001",
  "tenant_id": "00000000-0000-0000-0000-000000000002",
  "configuration_url": "https://portal.example.com/api/v1/me/client-configuration",
  "configuration_resource_id": "00000000-0000-0000-0000-000000000004",
  "public_client_id": "00000000-0000-0000-0000-000000000005",
  "routes": {
    "claude": {
      "route_id": "claude-gateway",
      "gateway_url": "https://gateway.example.com/claude"
    },
    "codex": {
      "route_id": "codex-gateway",
      "gateway_url": "https://gateway.example.com/codex/v1"
    }
  }
}
```

The reader derives the exact delegated scope from the pinned resource ID.
Explicit initial enrollment obtains the server-authenticated subject; every
subsequent read requires that subject. A cached response is revalidated before
it can authorize an `If-None-Match` request. Account switches, provider/route
changes, unknown fields, extra helper commands/URLs, invalid defaults, duplicate
models and Claude family-default conflicts are rejected.

Requests use HTTPS, no redirects, a ten-second timeout, identity encoding and
a 256 KiB response cap. The ETag must match the bytes received. This is a
corruption check, not an independent signature or proof of freshness: trust
still comes from authenticated HTTPS to the reviewed endpoint. Errors expose
neither response bodies nor tokens. The HTTP client does not retry or prompt.

Descriptor validation rejects IP literals, local/metadata host names and
noncanonical URLs; it is not a general SSRF defense against arbitrary DNS
aliases. Distribution through the reviewed organization setup channel remains
essential. This component alone does not make unattended refresh available.

## Enrolled encrypted credentials

`enrollment_auth.py` uses an organization-owned single-tenant public client
and MSAL's browser/loopback PKCE flow. The descriptor pins that public client
separately from the configuration API resource. Descriptor schema 2 is required;
the unreleased schema 1 Azure CLI pilot is not silently reinterpreted.
Azure CLI is not required for enrollment and its normal account/cache is untouched.

MSAL extensions owns token persistence and cross-process cache locking. On
macOS credentials are stored in Keychain; the private `0700` state directory
contains only cache-change markers and lock files. There is **no plaintext
fallback**. An unavailable or locked store fails closed. Noninteractive reads
also disable macOS Keychain interaction for the short-lived helper process,
so background refresh cannot display an unlock or sign-in dialog. Initial
login and explicit `refresh --login` may interact with the user.

Configuration reads request only `api://<resource-id>/configuration.read`;
inference reads request only Cognitive Services `user_impersonation`.
Before returning a token, the provider checks its tenant, audience, public
client, delegated scope, expiry and pinned object ID. JWT payload inspection
is **only a consistency guard**: it does not verify a signature or authorize
access. The API and gateway still validate and authorize the actual request.
The authoritative enrollment subject must come from the authenticated API
response, not an unverified local token.

Silent requests select exactly one account with the enrolled tenant and object
ID, never the first account in a shared cache. An account switch, duplicate
account, storage failure or interaction-required response stops the helper
with a sanitized sign-in-required error. MSAL response bodies and tokens are
never printed in diagnostics. Foreground reauthentication cannot silently
change the enrolled user. Entra registration/consent, actual Keychain behavior,
and real Conditional Access remain release qualification gates.

## Explicit macOS pilot enrollment

The CLI connects the reader, isolated credentials and recoverable settings
updates. A reviewed descriptor can be used directly:

```bash
claude-foundry enroll --descriptor /path/to/reviewed-organization.json
claude-foundry refresh
claude-foundry configuration-status
claude-foundry enable-refresh
claude-foundry disable-refresh
```

Use repeated `--client claude` / `--client codex` options to select clients.
Codex uses its approved locally installed binary to render version-matched
catalog metadata; `--codex-binary /absolute/path/to/codex` selects that binary
explicitly. No executable path can come from the API.

Enrollment starts foreground Entra sign-in, establishes the user from the
authenticated API response and checks inference credentials before replacing
client files. Subsequent `refresh` calls never sign in. An interaction-required
failure needs an explicit foreground `refresh --login`; an account switch is
still refused. Configuration and inference consent/Conditional Access may require
separate prompts; exactly one prompt is not promised. One organization and one
user are supported per installation.

An existing matching JSON installation requires `--migrate-existing` once.
Migration checks the recorded tenant and gateway, recognizes the existing
installer-owned helper/catalog and preserves unrelated preferences and history.
Unknown routes, remote Codex catalogs, custom `CODEX_HOME`, MDM ownership and
unrecognized installations stop for review. Legacy profiles are retained.

After successful enrollment, `enable-refresh` explicitly installs a per-user
macOS LaunchAgent. It performs one noninteractive attempt at login and roughly
every 15 minutes, with up to 60 seconds of jitter. The computer is not woken;
macOS may defer attempts while asleep or logged out. There is no retry loop,
background login prompt, client restart or server-provided job definition.
`disable-refresh` unloads the job and retains its disabled definition and
ownership record; it does not remove client settings or credentials. Enabling
again resumes it. Unknown or locally modified jobs stop for review.

If the job is saved but macOS cannot load it, the command reports that failure;
retry `enable-refresh` explicitly from the user's GUI session. Routine job
output is discarded. `configuration-status` retains the last successful time
and, after a failed background attempt, a safe error code and attempt time.
`sign_in_required` needs foreground `refresh --login`; `access_unavailable`
needs organization access review. Neither failure clears working settings nor
lets those settings authorize inference. This component has offline coverage;
real macOS job execution remains a release qualification check.

Refresh updates only model selections, installer-owned Claude family aliases,
the enrolled Claude Desktop model list and the local Codex catalog. It preserves
an allowed selected model and moves a removed selection to the current published
default. Empty grants clear model catalogs; they do not grant inference or erase
credentials. Routing, helpers and trust anchors cannot be changed by the feed;
local drift in those fields stops refresh. Existing open sessions may need to
be reopened to show new picker data; hot-reload is not claimed.

Files under `~/.config/claude-foundry/enrollment` are private (`0700` directory,
`0600` records; `0700` fixed helper). Each application file is atomically replaced.
A private journal recovers interrupted multi-file updates on the next run.
Concurrent runs are locked out, and unexpected later user edits stop recovery
instead of being overwritten. A private original-settings snapshot and the last
applied settings support three-way restoration, including preferences edited
before a later refresh.

Run `claude-foundry unenroll` to pause automatic refresh and restore only settings
still owned by this enrollment. Unknown routing/helper/catalog changes or an
edited enrolled Desktop library entry stop for review before the job is paused.
Later user preferences, other Desktop profiles, conversations, installed
applications, the runtime and encrypted credentials are retained. Untouched
original settings are restored byte-for-byte. A disabled owned LaunchAgent is
retained; `unenroll` does not uninstall the runtime or sign the account out.
The legacy `restore` command refuses an enrolled installation.

The last recovery record is retained as private
`~/.config/claude-foundry/enrollment/last-restored-settings.json`. It includes
original local preferences and connection/account metadata, not tokens; treat it
as private support material and never upload it automatically. Explicit
reenrollment is required to resume. Failure during restoration rolls client files
back and leaves the job paused. If a crash removed the binding before the journal
was cleared, rerun `enroll` with the same reviewed descriptor to recover the
transaction before authentication; do not delete the journal or hand-edit its
paths. Post-crash conflicting edits still require review.

Failed authentication, network/schema errors and route drift preserve the last
successful settings. Status currently reports the last success, not a fresh
health guarantee. Stale data cannot restore server-revoked access or bypass
current budget admission. Routine status output contains no account identifiers
or tokens. Never capture `enrolled-token` output in support logs.

## Opt-in organization-link onboarding

With both flags enabled and the credential client explicitly allowed, the portal's
`/?page=onboarding` link becomes the stable entrypoint. It uses the existing browser
Entra sign-in. A developer sees only applications with current published access,
not a group selector. The downloaded ZIP contains reviewed installer code,
`enrollment.json` trust anchors, and `installation.json` application choices; it
contains no personal grants or credentials. Group changes and new models come
from the authenticated own-user API after installation.

`GET /api/client-enrollment` and `GET /api/client-enrollment-bundle?client=claude`
(or `codex` / `both`) require portal authentication and current published access.
They cannot target another user or override group membership. Their responses
are private/no-store. The deployment supplies the pinned origin; incoming Host
headers and forwarded headers cannot select token destinations.

Users review the connection details in the ZIP, install their approved applications,
and run `bash Install.command`. The installer explicitly signs in, verifies
access, and prints `SETUP COMPLETE` only after the selected apps are configured.
A missing selected Codex app, failed sign-in, or settings conflict is not silently
skipped. Matching existing installations need explicit `--migrate-existing`.
Automatic refresh is enabled separately with `claude-foundry enable-refresh`,
or explicitly with `bash Install.command --enable-refresh`. No background job is
installed merely by downloading a ZIP. Save active work and fully quit/reopen the
apps after setup; live picker refresh is not promised.

This packaging does not close the authentication release gate above. Do not
enable it for users until encrypted credentials, Entra consent/Conditional Access,
actual macOS scheduling, model publication/revocation/budget admission, all four
native client surfaces and recovery/uninstall have been qualified. Offline
temporary-home and browser-component tests do not establish those live results.

### Offline recovery qualification — 2026-10-25

678 Python tests passed, including crash recovery, deletion rollback, preservation
of later preferences across refreshes, modified-route refusal, and pausing the job
before helper removal. Ruff, shell syntax, package build and whitespace checks
passed. These tests use isolated homes and mocked scheduling/authentication;
they do not establish native scheduling, interactive Entra or application UI
behavior.
