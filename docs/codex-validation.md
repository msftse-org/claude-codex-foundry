# Codex validation — October 15, 2026

This record describes the feature-branch pilot, not a production certification. Resource names, tenant IDs, credentials, session IDs and raw conversation exports are intentionally excluded. Private evidence is kept locally and is not distributed in installation bundles.

## Environment and results

- macOS; installed Codex CLI **0.145.0**; desktop bundled engine **0.162.0-alpha.17.2**; app build **26.1007.71326**.
- Existing standard APIM and Foundry foundation; new **Codex via Azure Foundry** API. The Claude APIs were not repurposed.
- Foundry region: **East US 2**. **34** successful OpenAI text model deployments. Metered capacity started at 10 units, or the SKU minimum where higher; these are pilot allocations.
- **34/34** models passed streamed completion, function-call and replayed tool-output protocol checks.
- **34/34** models used the desktop bundled engine to execute a real read-only local shell/file operation and return a test marker through APIM.
- The installed CLI passed a current-model file test and a legacy-model file test, including the latter with the station's actual user configuration and plugins present. Client version catalogs were parsed with strict configuration validation.
- The customer installer ran on the station using Azure CLI credentials and an absolute-path helper. Default model: `gpt-6.1-sol`; 34 models published. The profile contains no permanent inference key.
- Source and packaging checks: **130 Python tests passed**, Ruff checks/formatting, shell syntax, wheel build and client bundle inspection (see PR checks for final results).
- Dependency audit of the resolved authentication and portal runtime packages: **no known vulnerabilities reported** by `pip-audit` on the validation date. This does not rule out undisclosed vulnerabilities.

**Desktop UI boundary:** automation of Codex's own UI was blocked by the application's safety controls. Bundled-engine results are real engine tests, not a claim of completed visual UI acceptance. The user reported three successful README file reads. The routing audit below shows that these turns used OpenAI, so Azure desktop UI acceptance remains outstanding.

## Models

“Protocol” means the three Responses probes. “Native tool” means the local desktop-engine file test. These checks do not assess broad coding quality, every plugin, multimodal features or maximum context length.

| Model deployment | Version | Deployment type | Protocol | Native tool |
| --- | --- | --- | --- | --- |
| `gpt-4.1` | 2025-04-14 | GlobalStandard | Passed | Passed |
| `gpt-4.1-mini` | 2025-04-14 | GlobalStandard | Passed | Passed |
| `gpt-4.1-nano` | 2025-04-14 | GlobalStandard | Passed | Passed |
| `gpt-4o` | 2024-11-20 | GlobalStandard | Passed | Passed |
| `gpt-4o-mini` | 2024-07-18 | GlobalStandard | Passed | Passed |
| `gpt-5` | 2025-08-07 | GlobalStandard | Passed | Passed |
| `gpt-5-codex` | 2025-09-15 | GlobalStandard | Passed | Passed |
| `gpt-5-mini` | 2025-08-07 | GlobalStandard | Passed | Passed |
| `gpt-5-nano` | 2025-08-07 | GlobalStandard | Passed | Passed |
| `gpt-5-pro` | 2025-10-06 | GlobalStandard | Passed | Passed |
| `gpt-5.1` | 2025-11-13 | GlobalStandard | Passed | Passed |
| `gpt-5.1-codex` | 2025-11-13 | GlobalStandard | Passed | Passed |
| `gpt-5.1-codex-max` | 2025-12-04 | GlobalStandard | Passed | Passed |
| `gpt-5.1-codex-mini` | 2025-11-13 | GlobalStandard | Passed | Passed |
| `gpt-5.2` | 2025-12-11 | GlobalStandard | Passed | Passed |
| `gpt-5.2-codex` | 2026-01-14 | GlobalStandard | Passed | Passed |
| `gpt-5.3-codex` | 2026-02-24 | GlobalStandard | Passed | Passed |
| `gpt-5.4` | 2026-03-05 | GlobalStandard | Passed | Passed |
| `gpt-5.4-mini` | 2026-03-17 | GlobalStandard | Passed | Passed |
| `gpt-5.4-nano` | 2026-03-17 | GlobalStandard | Passed | Passed |
| `gpt-5.4-pro` | 2026-03-05 | GlobalStandard | Passed | Passed |
| `gpt-5.5` | 2026-04-24 | GlobalStandard | Passed | Passed |
| `gpt-5.6-luna` | 2026-07-09 | GlobalStandard | Passed | Passed |
| `gpt-5.6-sol` | 2026-07-09 | GlobalStandard | Passed | Passed |
| `gpt-5.6-terra` | 2026-07-09 | GlobalStandard | Passed | Passed |
| `gpt-6-astra` | 2026-09-03 | DataZoneStandard | Passed | Passed |
| `gpt-6-luna` | 2026-09-22 | DataZoneStandard | Passed | Passed |
| `gpt-6-sol` | 2026-09-22 | DataZoneStandard | Passed | Passed |
| `gpt-6.1-sol` | 2026-09-29 | GlobalStandard | Passed | Passed |
| `gpt-chat-latest` | 2026-10-07 | GlobalStandard | Passed | Passed |
| `o3` | 2025-04-16 | GlobalStandard | Passed | Passed |
| `o3-mini` | 2025-01-31 | GlobalStandard | Passed | Passed |
| `o3-pro` | 2025-06-10 | GlobalStandard | Passed | Passed |
| `o4-mini` | 2025-04-16 | GlobalStandard | Passed | Passed |

`codex-mini` and `o1` were discoverable but Azure rejected new deployment with `ServiceModelDeprecating`. They are not in the published catalog. Audio, image, video, embedding and realtime models were excluded from this coding-client workflow.

Global quota was unavailable for `gpt-6-astra`, `gpt-6-sol` and `gpt-6-luna`; available Data Zone allocations were used. New deployments did not change other resources' allocations. Some network requests needed retry/reconciliation, and one native test encountered 429s before its retry succeeded. These results do not establish capacity for 2,000 concurrent developers.

## Existing conversations

A pre-existing local conversation originally backed by OpenAI was copied into an isolated test home, then forked through Codex's native API with the Azure provider. Azure rejected the opaque reasoning state with `invalid_encrypted_content`. The original source's SHA-256 was unchanged.

The read-only exporter then produced a private handoff containing **24 visible user/assistant messages**, omitting **89 non-text/tool/reasoning items**. Both the CLI and desktop bundled engine correctly identified the original request from that transcript through Azure. Both resumed their new Azure conversations and correctly answered a follow-up using the earlier context. This demonstrates a visible-text handoff and subsequent Azure continuity; it does not demonstrate a full-fidelity cross-provider resume.

The exporter does not modify original sessions, upload a transcript or make a model request. It excludes tool payloads and attachments; visible messages can still contain sensitive data and must be reviewed before sharing.

## Security and observability

Live checks passed for:

- Missing or malformed bearer tokens: **401**.
- A valid Microsoft-signed token with the wrong audience: **401**.
- Authenticated model listing: **200**.
- Unpublished model: **400**.
- Stored `previous_response_id`, `conversation`, and background jobs: **400**.
- Stored-response retrieval route: **404**.
- A request containing `store=true`: response confirmed **store=false**.
- Conventional API frontend/backend request/response diagnostics: **zero body bytes and no captured headers**. A later opt-in [native content-log verification](../portal/VALIDATION.md#verification-on-2026-10-15-restricted-content-logs) enabled message capture in a separate workspace.

Application Insights queries confirmed Codex API request records and token metrics, including prompt, cached-prompt, completion and reasoning categories. Failed compatibility probes and intentional authentication/security tests appear as expected failures in the logs. Metrics are not billing reconciliation.

Offline tests cover tenant/object-ID/audience policy generation, stateless guards, model allowlisting, streaming, separate API ownership, quota planning, explicit SKU restrictions, installer/restore behavior, custom catalog/provider conflicts and visible-history filtering. This is not a penetration test. Customer Conditional Access, MDM, private networking, token revocation timing, plugin data flows, recovery drills and concurrency remain rollout-specific checks.

## Repeat before rollout

Follow [Codex onboarding](codex.md), retain the deployment and qualification reports privately, then verify native client tool use and subsequent turns. Check the actual APIM API/request logs, not just the selected model label. Re-run after client, model-version, catalog or policy changes. Continue portal governance work separately; its current policies and costs remain Claude-only.

## User README tests and routing audit — October 15, 2026

The user ran the read-only README summary prompt three times in the existing chat, at **08:31:22, 09:52:38, and 10:11:27 UTC** (11:31, 12:52, and 13:11 Israel time). All three local turn records identify `gpt-6-astra` with the built-in OpenAI provider. The current station configuration had also been restored to OpenAI. Application Insights contained **no Codex APIM request records at those test times**. These successful file reads are not evidence of Azure routing or cross-provider conversation migration.

A separate synthetic Azure control at **10:35:14 UTC** returned HTTP **200** and the expected marker. Application Insights recorded `POST /codex/v1/responses` with an approximately **2.39-second** duration. This confirms that the gateway and its logging path were working; it does not retroactively validate the earlier README tests. Request identifiers and raw evidence remain private. Prompt/response capture was disabled during those earlier tests.

For a desktop acceptance test, load the Azure configuration, restart the app, and create a new local chat. Correlate its time and request ID with APIM before marking acceptance complete. An existing chat or a model label alone does not establish the provider.
