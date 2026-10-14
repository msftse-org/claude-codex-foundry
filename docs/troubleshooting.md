# Troubleshooting

| Symptom | Check and recovery |
| --- | --- |
| APIM 401 | Run `claude-foundry login`; check tenant, Cognitive Services audience, issuer and the optional object-ID allowlist. A token for Azure Resource Manager is not an inference token. |
| Foundry 401/403 | Verify Cognitive Services User inference RBAC on the Foundry account for the signed-in user/group. Allow time for RBAC propagation. |
| Foundry 429 | Inspect the operation and model in Application Insights. Token-counting bursts can be throttled even when inference succeeds. Check the deployment's `rateLimits` and available model/SKU quota, increase its allocation if appropriate, or request additional quota. Clients must respect retry delays; do not hide throttling with a synthetic successful response. |
| 404 from a model after deployment | Provisioning success does not prove inference availability. Check the exact deployment/model identifier, then test the Foundry endpoint to distinguish a gateway issue from a provider issue. Keep a failing model out of the published client catalog until validated. |
| Marketplace purchase denied | Review the saved report and subscription offer/purchase policy. An internal or sandbox subscription may disallow nonzero-priced offers. Resolve with the billing administrator; retries do not bypass the restriction. |
| No available quota | Request regional/model quota or free unused capacity. Discovery does not claim access based only on the catalog. |
| Azure 412 / IfMatchPreconditionFailed during deployment | The model command retries the same deployment up to four times for resource-provider concurrency conflicts. If it still fails, retain the report, let ongoing provisioning finish, then retry that model with `--model MODEL_NAME`. |
| Deployment still Creating | The report is retained. Check the deployment state and rerun synchronization after it succeeds. A timeout is not a rollback. |
| Expired credentials in Desktop | Run the installed login command, then retry. Helper scripts deliberately do not trigger interactive prompts. |
| Entra app registration denied | The MSAL setup needs a tenant app administrator. Use an Azure CLI profile while awaiting the app registration/consent. |
| CLI works but Desktop fails | Restart Desktop fully; check MDM precedence and `~/Library/Logs/Claude-3p/main.log`. GUI launches have a different PATH, so the installed helper and runtime use absolute paths. |
| Wrong endpoint/provider in Code | Inspect process environment, project settings and managed settings for conflicting `ANTHROPIC_*` / `CLAUDE_CODE_USE_*` variables. Project/MDM settings can override user configuration. |
| Old history missing | Consumer and third-party Desktop use separate profiles/stores. The installer preserves the old profile; it does not migrate conversation history. |
| Catalog mismatch during verify | Redeploy the reviewed gateway catalog and export a fresh client profile. |
| Empty metrics/logs | See the observability guide; distinguish Logs from Live Metrics and check ingestion delay/permissions. |

Use `.local/models-report.json` for administrator diagnostics and the private installation snapshots for user recovery. Before sharing troubleshooting output, remove tokens, account identifiers, prompts and any customer content. Do not enable shell tracing around credential commands.
