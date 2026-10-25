# Client configuration refresh — experimental components

These are opt-in server/client increments of automatic configuration refresh,
not yet a working end-to-end installer. The API is disabled by default. Existing
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
  "schema_version": 1,
  "organization_id": "00000000-0000-0000-0000-000000000001",
  "tenant_id": "00000000-0000-0000-0000-000000000002",
  "configuration_url": "https://portal.example.com/api/v1/me/client-configuration",
  "configuration_resource_id": "00000000-0000-0000-0000-000000000004",
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

## Enrolled Azure CLI credentials

`enrollment_auth.py` adds the credential provider for the Azure CLI pilot.
It uses an installer-owned absolute directory with mode `0700`, rejects
symlinked cache paths, and passes `AZURE_CONFIG_DIR` only to child processes.
It never changes the user's normal Azure CLI account or cache. Initial login
is an explicit foreground operation; background token reads cannot sign in.

Configuration reads request only `api://<resource-id>/configuration.read`;
inference reads request only Cognitive Services `user_impersonation`.
Before returning a token, the provider checks its tenant, audience, public
client, delegated scope, expiry and pinned object ID. JWT payload inspection
is **only a consistency guard**: it does not verify a signature or authorize
access. The API and gateway still validate and authorize the actual request.
The authoritative enrollment subject must come from the authenticated API
response, not an unverified local token.

An account switch or interaction-required response stops the helper with a
safe sign-in-required error. The wrapper never prints CLI diagnostic output
or persists tokens itself. Azure CLI still owns its cache and may store
credentials there in plaintext on macOS/Linux. This is the existing pilot
authentication posture, not an encrypted-cache fallback or a new security
guarantee. Approval of that posture is a release gate; use the encrypted MSAL
path instead where endpoint policy prohibits it.

## Explicit macOS pilot enrollment

The CLI now connects the reader, isolated credentials and recoverable settings
updates. This is a manual pilot entrypoint for a reviewed descriptor, not a
stable-link bootstrap installer:

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

Enrollment opens one foreground Entra sign-in, establishes the user from the
authenticated API response and checks inference credentials before replacing
client files. Subsequent `refresh` calls never sign in. An interaction-required
failure needs an explicit foreground `refresh --login`; an account switch is
still refused. One organization and one user are supported per installation.

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
instead of being overwritten. A private original-settings snapshot is retained
for future three-way restoration; **enrolled uninstall/restore is not yet
implemented**. The legacy `restore` command is not an enrollment uninstaller.

Failed authentication, network/schema errors and route drift preserve the last
successful settings. Status currently reports the last success, not a fresh
health guarantee. Stale data cannot restore server-revoked access or bypass
current budget admission. Routine status output contains no account identifiers
or tokens. Never capture `enrolled-token` output in support logs.

Qualification still required: stable-link packaging, background scheduling,
Entra consent/Conditional Access, real model-publish/revocation/budget tests,
all four native client surfaces and recovery/uninstall. Offline temporary-home
tests do not establish any of those live results.
