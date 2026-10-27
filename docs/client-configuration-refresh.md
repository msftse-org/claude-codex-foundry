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

The dedicated registration helper is available below. Deployed wiring
and native client qualification are still release gates. Do not reuse the
portal application to enable this endpoint.

## Dedicated Entra registrations

An operator with permission to manage the dedicated API registration runs this
from the customer's checkout. It previews by default (read-only); `--apply`
creates the API and preauthorizes Azure CLI for its single read-only scope.
It does not change Microsoft's Azure CLI registration, grant Graph/ARM
permissions to clients, deploy the portal or enable the feature.

```bash
uv run --frozen --extra auth --extra portal python scripts/register_enrollment.py \
  --tenant TENANT_UUID --subscription SUBSCRIPTION_UUID \
  --name "Example Refresh Pilot" --azure-cli \
  --output .local/enrollment/entra-receipt.json
# Review the target, then repeat with --apply.
```

Use a unique environment-specific name. The API registration exposes only
admin-consent delegated `configuration.read`, uses v2 tokens and has no secret.
The API preauthorizes Microsoft's Azure CLI public client
(`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) for exactly this scope. No new public
client, secret or tenant-wide delegated grant is needed for this mode.
Existing Azure CLI Cognitive Services credentials remain separate inference
credentials. API preauthorization and the server's narrow client allowlist are
separate checks; a public-client ID is not proof of a trusted device or executable.
Tenant Conditional Access, assignment and application-management policy still apply.

For existing schema-2 MSAL pilots, omit `--azure-cli` to retain the separate
public client and encrypted Keychain cache. Only that legacy mode accepts
`--grant-admin-consent` with `--apply`, requiring an appropriately authorized
administrator. Modes are explicit and never silently substituted. Moving an owned
API to CLI mode verifies its complete manifest first and changes only its API
preauthorization; the old public client is retained for explicit cleanup.

Keep the private receipt (`0600`) and reuse the **same path, tenant and name**
after an interrupted run. A durable registration tag lets the helper find a
created app even when its creation response was lost. It never blindly retries
creation or consent, adopts another app by display name, repairs unrelated drift, adds
credentials, or re-enables a disabled enterprise application. Do not run
registration concurrently. If the receipt is lost or an administrator changes
the dedicated manifest, stop for review; do not generate a fresh receipt to
retry the same deployment.

The operator needs permission to manage the owned applications/service principals;
the separate MSAL mode additionally requires the appropriate tenant consent role.
These operator privileges
are not granted to the workstation client. See Microsoft's
[application creation API](https://learn.microsoft.com/en-us/graph/api/application-post-applications?view=graph-rest-1.0),
[consent guidance](https://learn.microsoft.com/en-us/entra/identity-platform/v2-admin-consent),
[preauthorized clients](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-configure-app-expose-web-apis#add-a-scope),
and [MSAL interactive PKCE prerequisites](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens#interactive-vs-silent).

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
| `PORTAL_ENROLLMENT_CLIENT_ID` | Azure CLI public-client UUID for schema 3; dedicated organization public-client UUID for legacy schema 2; must be in the configuration client allowlist |

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

## Data-only client reader

`configuration_client.py` implements bounded data-only reads. It does not log
in, persist credentials/enrollment, schedule work or change application files.
The trusted setup channel supplies this descriptor; a refresh response may
never replace it:

```json
{
  "schema_version": 3,
  "organization_id": "00000000-0000-0000-0000-000000000001",
  "tenant_id": "00000000-0000-0000-0000-000000000002",
  "configuration_url": "https://portal.example.com/api/v1/me/client-configuration",
  "configuration_resource_id": "00000000-0000-0000-0000-000000000004",
  "public_client_id": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
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

## Account-bound credentials

Descriptor schema 3 explicitly selects Azure CLI and pins its public-client ID
separately from the API resource. The helper first reuses `az account get-access-token`
with the exact tenant and scope/resource. Only initial enrollment or explicit
`refresh --login` can invoke foreground `az login`; background refresh never can.
It does not call `az account set`, log out other accounts, distribute secrets,
copy token files or maintain another credential cache.

Azure CLI owns storage and renewal. Its macOS/Linux cache is not necessarily
encrypted; OS account protection, disk encryption and endpoint policy are required.
This is an explicit use of the existing CLI credential mechanism, **not an
encrypted-cache guarantee**. Organizations requiring hardware-backed or Keychain-only
credentials must review this tradeoff or qualify the separate schema-2 MSAL path.

Schema 2 retains organization-owned MSAL browser/loopback PKCE and encrypted
MSAL-extensions persistence in macOS Keychain, with cross-process locking and no
plaintext fallback. Locked/unavailable storage fails closed; silent helpers
disable Keychain interaction. Schema 1 is rejected. Neither mode falls back to
the other when authentication fails.

Configuration reads request only `api://<resource-id>/configuration.read`;
inference reads request only Cognitive Services `user_impersonation`.
Before returning a token, the provider checks its tenant, audience, public
client, delegated scope, expiry and pinned object ID. JWT payload inspection
is **only a consistency guard**: it does not verify a signature or authorize
access. The API and gateway still validate and authorize the actual request.
The authoritative enrollment subject must come from the authenticated API
response, not an unverified local token.

Every returned credential must match the enrolled tenant and object ID; MSAL
also selects exactly one matching cache account. Azure CLI account switching
cannot rebind enrollment. Storage failure or interaction-required responses stop
the helper with a sanitized sign-in-required error. Authentication bodies and tokens are
never printed in diagnostics. Foreground reauthentication cannot silently
change the enrolled user. Tenant policy, actual credential-store behavior,
and real Conditional Access remain release qualification gates.

## Explicit macOS pilot enrollment

The CLI connects the reader, account-bound credentials and recoverable settings
updates. A reviewed descriptor can be used directly:

```bash
claude-foundry enroll --descriptor /path/to/reviewed-organization.json
claude-foundry refresh
claude-foundry configuration-status
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

Enrollment automatically installs and loads a per-user macOS LaunchAgent; there
is no separate opt-in or installer flag. It performs one noninteractive attempt
at login and roughly every 15 minutes, with up to 60 seconds of jitter. The computer is not woken;
macOS may defer attempts while asleep or logged out. There is no retry loop,
background login prompt, client restart or server-provided job definition.
For troubleshooting, `disable-refresh` unloads the job and retains its disabled
definition and ownership record; it does not remove client settings or credentials.
Enabling again with `enable-refresh` resumes it. Explicitly rerunning enrollment
also resumes automatic refresh. Unknown or locally modified jobs stop for review.

If the job is saved but macOS cannot load it, enrollment exits unsuccessfully and
the installer does not print `SETUP COMPLETE`, even if client settings were
already configured; retry `enable-refresh` explicitly from the user's GUI session. Routine job
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
applications, the runtime and credentials are retained. Untouched
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
access, and prints `SETUP COMPLETE` only after the selected apps are configured
and automatic background refresh is loaded.
A missing selected Codex app, failed sign-in, or settings conflict is not silently
skipped. Matching existing installations need explicit `--migrate-existing`.
No background job is installed merely by downloading a ZIP. Save active work and
fully quit/reopen the apps after setup; live picker refresh is not promised.

This packaging alone does not close the live release gates. Do not
enable it for general rollout until tenant authentication/Conditional Access,
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

### Registration qualification — 2026-10-26

The initial separate-public-client pilot encountered a tenant-wide consent
denial. The revised CLI mode uses resource-owner preauthorization of the
dedicated configuration API's read-only scope. In the authorized demo this
preauthorization was read back and Azure CLI successfully obtained the distinct
configuration token without a tenant-admin consent command. No Microsoft-owned
registration, delegated tenant grant, installed client settings or budget policy
was changed by that authentication check. The prior API manifest and registration
receipt are retained privately for recovery.

This establishes the demo's CLI token-acquisition path, not universal tenant
eligibility, Conditional Access acceptance or native UI qualification. An operator
still needs permission to manage the API registration. Preserve the same receipt;
the unused dedicated public client is not automatically deleted.

### Azure CLI adapter offline qualification — 2026-10-26

724 Python tests and 57 frontend tests passed. Ruff lint/format checks, shell
syntax, frontend production build, wheel/source build and whitespace checks
passed. The new cases cover explicit authentication-mode selection, separate
token audiences, tenant/user/client/scope/expiry binding, silent background
failure, registration preauthorization and drift refusal, missing-CLI setup,
and preservation of existing installations. No real installed client settings
were changed. These results do not establish live scheduling, native model-picker
reload, policy publication/revocation or budget enforcement for this increment.

### Dedicated demo qualification — 2026-10-26

The opt-in portal build was deployed successfully to the expressly authorized
existing demo. Its original source bundle, relevant settings and registration
receipt are retained privately for recovery. The integration build includes the
reviewed CA-bundle dependency update; no feature PR is merged to main, and the
flags remain off by default for other deployments. Existing spending ledgers,
published budget policy and installed client settings were not changed.

Real Entra requests verified a successful own-user response (9 Claude and 33
Codex models), an authorized conditional 304, and rejection of anonymous requests,
inference-audience tokens and user-selector parameters. The configuration token
was also rejected by portal administration. Actual enrollment, unchanged refresh
and unenrollment passed in disposable client homes using Azure CLI credentials.
Credentials remained with Azure CLI; they were not copied into enrollment state.

Codex CLI `0.145.0` and the Desktop-bundled engine
`0.164.0-alpha.11.1` each consumed the enrolled 33-model catalog with strict
configuration validation, read a synthetic local file through a real tool call,
and resumed that conversation with the expected answer through the enrolled
provider. Each isolated enrollment was restored after the test. These are engine
checks, not visual Desktop UI acceptance or evidence of hot model-picker reload.

Release gates still open: real macOS scheduled execution, interactive
Conditional Access/reauthentication, Claude Code and Claude Desktop native
journeys, new-model publication-to-picker timing, published revocation with stale
files/local rollback, and shared-budget denial/failure-boundary checks for this
increment. Do not merge or broaden rollout until these pass. Raw native logs,
identities, tokens and customer configuration remain outside Git.
