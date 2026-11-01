# One portal for Claude and Codex

The standard deployment uses `portal.mode=unified` (the default). It connects every enabled provider to the same member/group policy and budget ledger. The portal shows both model catalogs, exports separate Claude and Codex profiles, and queries the configured APIs for usage and estimated costs. It supports Claude-only and Codex-only installations too.

A fresh standard installation creates **one governed APIM API per enabled provider**:
`claude-governed` and/or `codex-governed`. It does not create the extra `claude-foundry`
or `codex-foundry` user-token APIs. Codex qualification is a billable deployment-admin
operation against the resource's direct Foundry `/openai/v1` endpoint using the
operator's Entra token and existing Foundry RBAC. The temporary qualification profile
is never distributed. It checks model protocol support, not gateway/budget enforcement;
the governed-route acceptance checks below remain required.

Reruns do not recreate or overwrite retired baseline routes. Existing upgrades still
preserve legacy APIs for an explicit, reviewed client cutover; an API left from an
older deployment is not needed for the standard two-provider architecture. Back up
its API definition, operations, policies and diagnostics before retiring it, verify
clients and portal exports use the governed replacement, then remove the legacy API.
Never retire an API solely by matching its name in a shared/customer APIM service.

Cost estimates prefer explicit planning rates and otherwise use reviewed enforcement ceilings. They are not Azure invoices.

The shared runtime, Table/Blob storage and private endpoints are deployed automatically. This adds Azure resources and charges compared with the legacy Claude portal. The runtime's [release acceptance checks](budget-enforcement.md#release-acceptance) still apply; selecting the default does not certify a customer rollout.

## Fresh installation

1. Follow the [quick start](quickstart.md). Leave `portal.mode` as `unified`. The deployer provisions the foundation, qualifies Codex models, creates the shared runtime, connects the portal and packages profiles for `/claude-governed` and `/codex-governed/v1`.
2. The deployer prices every succeeded, version-pinned deployment from the built-in public price catalog and turns on governed access: it publishes a policy with hard enforcement and unlimited organization and group limits, so spend is metered per user. Set `portal.monthly_limit_usd` in the deployment settings to start with an organization cap instead. Models without a catalog price, or not pinned to a version, are reported and stay closed. A deployment that already has a published policy keeps it unchanged.
3. Add developers by email, UPN or object ID:

   ```sh
   uv run python scripts/onboard_users.py alice@contoso.com bob@contoso.com
   ```

   They become active portal members and can use the governed routes right away. With more than one portal group, name one with `--portal-group`. Owners and administrators can also add members under **Members**; those changes are published as soon as they are saved. Entra group membership alone does not grant shared-route inference.
4. Developers download their Mac setup ZIP from **Onboarding**. It includes the scoped profile, installer, gateway README and instructions for the customer's coding assistant. Use **Copy setup prompt for Codex** for guided local setup. Advanced profile-only downloads and combined installers remain available; when building a shared-runtime bundle manually, use both `--profile` arguments and `--budgeted`.
5. Tighten the policy when you're ready: review prices, organization, group and user limits, groups and model grants under **Budgets & limits** and the group pages, then choose **Publish budget policy**. These changes stay in draft until published. Only membership changes are published automatically.
6. To use your own reviewed prices instead of the catalog, download **model-policy.json** under **Onboarding**, review it and pin those exact deployment versions with `versionUpgradeOption=NoAutoUpgrade`. Then set `portal.model_policy_file` to that file under this customer's ignored `.local/` directory and rerun the deploy, or the portal-only command below. The deployer validates the manifest against live qualified, pinned deployments and configures the runtime bounds. An unknown, expired, unqualified, duplicate or mismatched model stops the update.

Without priced model bounds and a published policy, governed inference stays closed. If the deployer reports that access was not turned on (for example, the operator isn't the portal owner), the owner publishes under **Budgets & limits**. Deployment health and installer generation do not prove that developers can make requests. Existing portal state, published policy, spend, reservations and audit history survive reruns; a missing new price file never clears an established runtime manifest.

The initial install grants Foundry inference to the deploying operator for model qualification, not to the developer group. Subscription operators remain inside the trust boundary. Review other direct/group/inherited Foundry roles and alternate credentials before claiming gateway-only access.

## Upgrade an existing standard installation

Use a separate checkout for this customer. Preserve its existing `.local/deploy-state.json`, `.local/deploy.json`, provider organization files, `.local/portal/parameters.json`, `.local/portal/entra.json` and any runtime model manifest. Match their tenant, subscription and resource names. A manual installation must first map its existing values into these files; do not run a fresh foundation deployment to discover or replace them.

Change the settings to:

```json
"portal": {
  "enabled": true,
  "mode": "unified"
}
```

Then run:

```sh
uv sync --frozen --extra auth --extra portal
uv run python scripts/deploy.py --settings .local/deploy.json --upgrade-portal
```

This skips foundation/model deployment, model qualification, developer-group changes and desktop sign-in registration. It reuses the existing portal registration, owner and portal-state storage, creates the separate shared runtime, and builds new governed profiles. If no budget policy has ever been published, it turns on access with the defaults described above; a published policy is kept unchanged. It does not automatically copy existing users into the portal, grant new model permissions, reset balances or repoint existing client installations.

If the live portal already uses a different budget ledger, the command stops before ARM deployment. Preserve that ledger and use the existing [budget deployment procedure](../budget/README.md) to adopt its configuration; never replace it with an empty store. Use the same recorded deployment state on all subsequent runs. A unified installation cannot be switched back to baseline by changing `portal.mode`.

Complete steps 2–7 above. Once the intended pilot clients pass the checks below, migrate the remaining clients. Then retire the old baseline APIs or replace their inference operations with a deny policy, and remove obsolete developer inference roles at their actual assignment scopes. Do this as an explicit customer cutover: the upgrade preserves old routes and assignments so current clients can continue working during migration. Retained paths can bypass shared policy. Removing a group assignment does not revoke an inherited or directly assigned role; inventory both.

## Acceptance and handover

The developer's connection flow is **Download Mac setup ZIP → open Install.command → sign in with Microsoft**. Gateway addresses are supplied by the deployment; developers do not enter or repair them.

The deployer now checks that client setup is connected for every enabled provider before reporting completion. A code-only portal update does not add missing gateway configuration. If an older installation shows “Your organization's setup is not ready yet”, the deployment administrator must finish the portal upgrade using the same private settings. For a manually assembled budget installation, retain its existing ledger and gateway configuration as described above; do not run a fresh deployment over it. No developer-side configuration change is needed.

- Verify both providers in the portal catalog and in group/member model selection. Existing groups retain their previous grants; adding a provider does not automatically grant it to every group.
- Verify each exported profile's tenant, provider, published model subset, default and governed endpoint. Codex profiles must end in `/v1`, contain only qualified OpenAI models, and contain no Claude models. A group administrator cannot export another group's grants.
- Using the customer's exported profiles, run `claude-foundry verify --config CLAUDE_PROFILE --budgeted` for Claude and `claude-foundry verify --config CODEX_PROFILE` for Codex. These are small billable checks. Verify model listing, JSON and streaming inference for each provider. Run a real local file tool and a history follow-up with the approved native Codex and Claude clients. Test Codex compaction with its required output allowance. Keep synthetic evidence private.
- Check `/v1/budget` on each governed route and confirm usage settlement. Test a separate constrained identity, cross-group denials and suspension/publication. Follow the full [acceptance procedure](budget-enforcement.md#release-acceptance); do not exhaust real customer allowances as a test.
- Verify usage/cost queries include exactly the configured API IDs on the intended APIM service. Azure metric ingestion is delayed and streaming metrics can be estimates. Unattributed metrics are not evidence of team spend and are excluded from group-scoped reports. The ledger controls admission independently of monitoring.
- Verify old client routes and direct Foundry permissions can no longer bypass policy after cutover. Record any retained paths and unfinished checks in the handover.

The explicit legacy option is `portal.mode=baseline`. It retains the Claude-only portal and baseline Entra/RBAC Codex route, with no enforced shared USD budgets. Use `portal.enabled=false` for an intentionally portal-free installation.
