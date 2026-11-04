# Changelog

What changes for administrators and for developers using Claude and Codex, newest first. Each entry says whether you need to act.

Changes are listed under **Unreleased** when they merge into `release/next`, after they pass checks and a live test deployment. They move under a dated release heading when `main` is updated and tagged. `main` is the version customers deploy.

## Unreleased

### Administrators

- **Rejected requests no longer hold budget.** When Azure rejects a request before the model runs (throttling, sign-in or routing errors, or an invalid request such as an unsupported tool schema), its whole reservation is released at once. Before, each one held its full worst-case cost for the rest of the month, so a burst of throttled retries or failed tool calls could block a capped user. Content-filter rejections, 5xx errors and interrupted streams still stay held, because they can follow billable work.
- Deploy summaries no longer ask owners to publish access that is already on: they list developer onboarding and the setup link, or the specific access failure and its next step.
- Baseline Codex qualification waits up to three minutes for API Management's own not-found response on a route updated by that deploy, and up to 4.5 minutes for initial 401/403 access propagation. Final Codex verification restores that access wait without replaying any successful probe or whole billable sequence. Backend/model errors and failures after the first successful request are not retried. Default unified qualification still uses direct Foundry.

### Developers

- Codex `admin qualify` and `verify` expose `--route-propagation-wait SECONDS` and `--access-propagation-wait SECONDS` (both default to `0`, off). These first-request-only waits include HTTP time and use bounded backoff; later probes retain their normal timeouts. The deployer's hidden retry subprocess has been removed.

### Upgrade notes

- Configuration is unchanged; existing successful deployments need no action.

## release-2026-11-01

Upgrades from `customer-baseline-2026-10-29`. Tested by upgrading a deployment that was created from that baseline.

### How to upgrade

In the checkout you deployed from, update to this release and rerun the portal upgrade. It keeps your models, users, published policy, spend and audit history:

```sh
git switch main && git pull
uv sync --frozen --extra auth --extra portal
uv run python scripts/deploy.py --settings .local/deploy.json --upgrade-portal
```

The governed routes and then the portal are each unavailable for about two minutes while their Function apps restart onto the new code.

### "Publish this member's budget access before exporting a profile"

Developers saw this on deployments from the baseline because no budget policy had been published, and publishing one took several manual steps. After you upgrade, the deployer turns access on by itself, and adding a developer is one step:

1. Run the upgrade above. If no budget policy was ever published, it pins and prices your models from the catalog and publishes a working policy. A policy you already published is kept unchanged.
2. Add developers:

   ```sh
   uv run python scripts/onboard_users.py alice@contoso.com bob@contoso.com
   ```

   Or add them under **Members** in the portal. Either way, their access is live as soon as it's saved.
3. Developers download their setup from **Onboarding**.

If the deploy output says access was not turned on (for example, because you aren't the portal owner), the owner chooses **Publish budget policy** in **Budgets & limits** once.

### Upgrade notes

- **Access starts unlimited and metered.** On a deployment that never published a budget policy, the deployer turns on hard enforcement with unlimited organization and group limits, so every request is charged to a user but nothing is capped. To start with a cap, set `portal.monthly_limit_usd` in `.local/deploy.json` before upgrading. Tighten limits later in **Budgets & limits**.
- **Member changes publish immediately.** When an owner or administrator adds, moves or suspends a member, the change takes effect at once. Prices, limits, groups and grants still wait for **Publish budget policy**. Group administrators keep their **Publish team budget & access** step.
- **Model versions are pinned.** Deployments the deployer creates, and existing ones it prices from the catalog during an upgrade, are set to `NoAutoUpgrade`, so each price matches the version it serves. Capacity and other settings are unchanged. Azure still retires old versions on its published schedule.
- **Short outage during upgrades.** `scripts/deploy.py` now restarts the budget runtime and then the portal after uploading new code, and waits until each one reports the new build. Plan upgrades outside working hours.
- **Unpriced runtimes are priced on upgrade.** A runtime deployed without reviewed models gets catalog prices for its deployments when you upgrade. A runtime that already has reviewed models keeps them.
- **Price review dates warn instead of blocking.** A model whose review date has passed keeps working, and the portal marks the price as "review due". To keep the previous behavior, where overdue prices close the model, set `settings.enforce_price_review` to `true` and publish the policy.
- **Spend is charged at list rates.** Requests are now charged at the actual rate of each token type, including the lower cache-read and cache-write rates. Cache-heavy usage counts less against budgets than before. Limits are unchanged.
- **Existing prices are kept.** Upgrading doesn't change reviewed prices. `scripts/publish_prices.py` adds catalog prices only for pinned models that have none. Add `--replace` to refresh existing prices too.

### Administrators

- New deployments start with prices from a built-in public price catalog, dated 2026-10-29: 13 Claude and 34 Azure OpenAI models, including long-context tiers and the 1.1× Data Zone rate. Models outside the catalog are listed in the deployment output and stay closed until you price them.
- `scripts/onboard_users.py` makes people active portal members in shared deployments, and `--remove` suspends them. Use `--portal-group` when there's more than one group.
- Profile downloads explain what's missing: access not turned on yet, enforcement paused, or a member added after the last publish.
- Price review dates can be set up to 366 days ahead (previously 31).
- Portal owners can script audited changes with their Azure CLI sign-in. The portal's own app registration now pre-authorizes Azure CLI. No new application is created, and portal roles still decide every request.
- Deployment confirms that each Function app serves the build that was just uploaded before it reports success. The runtime's `/health` and the portal's `/api/health` report a `build` ID that matches `build.json` in the package.
- Unified deployments create only the governed APIs (`claude-governed`, `codex-governed`).
- Deployment checks that the portal's public pages and scripts can be read before handover, and packages stay readable even when built under a private file-permission setting.

### Developers (Claude and Codex users)

- The setup download and portal onboarding page explain macOS's **Open Anyway** step for `Install.command`. The installer doesn't remove quarantine or change Gatekeeper.
- The installer's authentication tools use an updated trusted certificate bundle.

## customer-baseline-2026-10-29

The version customers deployed before this release (tag `customer-baseline-2026-10-29`, commit `110a578`).
