# Changelog

What changes for administrators and for developers using Claude and Codex, newest first. Each entry says whether you need to act.

Changes are listed under **Unreleased** when they merge into `release/next`, after they pass checks and a live test deployment. They move under a dated release heading when `main` is updated and tagged. `main` is the version customers deploy.

## Unreleased

### Upgrade notes

- **Short outage during upgrades.** `scripts/deploy.py` now restarts the budget runtime and then the portal after uploading new code. Expect about two minutes without governed Claude and Codex requests, then about two minutes without the portal. Plan upgrades outside working hours.
- **Price review dates warn instead of blocking.** A model whose review date has passed keeps working, and the portal marks the price as "review due". To keep the previous behavior, where overdue prices close the model, set `settings.enforce_price_review` to `true` and publish the policy.
- **Spend is charged at list rates.** Requests are now charged at the actual rate of each token type, including the lower cache-read and cache-write rates. Cache-heavy usage counts less against budgets than before. Limits are unchanged.
- **Existing prices are kept.** Upgrading doesn't change reviewed prices. To add catalog prices for pinned models that have none, run `scripts/publish_prices.py` as a portal owner. Add `--replace` to refresh existing prices too, and `--dry-run` to preview the changes.

### Administrators

- A fresh unified deployment opens governed inference for models deployed with a pinned version, priced from a built-in public price catalog, dated 2026-10-29. That's 13 Claude and 34 Azure OpenAI models, including long-context tiers and the 1.1× Data Zone rate. Models without a catalog price, or not pinned to a version, are listed in the deployment output and stay closed until you price them.
- Price review dates can be set up to 366 days ahead (previously 31).
- Portal owners can script audited changes with their Azure CLI sign-in. The portal's own app registration now pre-authorizes Azure CLI. No new application is created, and portal roles still decide every request.
- Deployment confirms that each Function app serves the build that was just uploaded before it reports success. The runtime's `/health` and the portal's `/api/health` report a `build` ID that matches `build.json` in the package.
- Unified deployments create only the governed APIs (`claude-governed`, `codex-governed`).
- Deployment checks that the portal's public pages and scripts can be read before handover, and packages stay readable even when built under a private file-permission setting.

### Developers (Claude and Codex users)

- The setup download and portal onboarding page explain macOS's **Open Anyway** step for `Install.command`. The installer doesn't remove quarantine or change Gatekeeper.
- The installer's authentication tools use an updated trusted certificate bundle.

## customer-baseline-2026-10-29

The version on `main` that customers deploy today (tag `customer-baseline-2026-10-29`, commit `110a578`).
