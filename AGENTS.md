# Working in this repository

For a fresh customer deployment, read [the LLM deployment and onboarding runbook](docs/llm-onboarding.md) first. It includes a customer input checklist, a prompt to hand to an assistant, the supported deployment commands, user import, verification and handover. Use the checked-out scripts and their `--help` as the command reference.

For repository changes, follow [CONTRIBUTING.md](CONTRIBUTING.md). Preserve unrelated work and customer files. Run the relevant offline checks; a request to edit or review code does not authorize deploying Azure resources.

Keep each customer's configuration, identity lists, reports and installer bundles in a separate checkout, under ignored `.local/` and `dist/`. Never commit credentials or customer data. Do not copy another customer's deployment state to start a new installation.

The one-command deployment defaults to unified Claude/Codex administration, including the shared runtime. Read docs/unified-administration.md for model-policy verification, owner publication and the portal-only upgrade command. Inference remains closed until reviewed model bounds and access policy are configured. Use portal.mode=baseline only for an explicitly requested legacy Claude-only portal. Preserve existing ledgers and complete the runtime acceptance checks before claiming customer spending guarantees.

## Rules for coding assistants

Several people and their assistants work in this repository. On every task:

- Work from an issue. Branch from `release/next` as `fix/<issue>-<slug>` or `feat/<issue>-<slug>`, and open a pull request against `release/next` with `Closes #<issue>`. Never push to `main` or `release/next`, and never merge a pull request: a person reviews and merges.
- Don't run `scripts/deploy.py`, `scripts/onboard_users.py`, `scripts/publish_prices.py`, gateway verification, or any other command that changes Azure, Entra or portal state or sends billable requests, unless the person you're working for asks for it in this session. Then run it only against their own test deployment, named in the request. Never touch a customer environment or another contributor's deployment.
- Never delete or reset a budget ledger, published policy, portal state or audit history, and never close held reservations as cleanup.
- Treat the repository as public. Never write customer names, tenant or subscription IDs, resource endpoints, emails, tokens, conversation content or raw Azure output into code, tests, docs, commits, issues or pull requests.
- Run the checks in CONTRIBUTING.md before saying a change works, and report what you didn't verify. Passing unit tests don't prove a live deployment works.
- Add a CHANGELOG.md entry under **Unreleased** for anything administrators or developers will notice.
