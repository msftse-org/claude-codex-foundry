# Client configuration refresh — experimental components

These are the first server/client increments of automatic configuration refresh,
not a working end-to-end installer. It is disabled by default. Existing JSON
installations and inference authentication are unchanged.

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

Registration, consent, deployment wiring, account binding and background
refresh are subsequent increments. Do not reuse the portal application to
enable this endpoint.

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
      "model_ids": ["example-codex"]
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
