# Working in this repository

For a fresh customer deployment, read [the LLM deployment and onboarding runbook](docs/llm-onboarding.md) first. It includes a customer input checklist, a prompt to hand to an assistant, the supported deployment commands, user import, verification and handover. Use the checked-out scripts and their `--help` as the command reference.

For repository changes, follow [CONTRIBUTING.md](CONTRIBUTING.md). Preserve unrelated work and customer files. Run the relevant offline checks; a request to edit or review code does not authorize deploying Azure resources.

Keep each customer's configuration, identity lists, reports and installer bundles in a separate checkout, under ignored `.local/` and `dist/`. Never commit credentials or customer data. Do not copy another customer's deployment state to start a new installation.

The one-command deployment defaults to unified Claude/Codex administration, including the shared runtime. Read docs/unified-administration.md for model-policy verification, owner publication and the portal-only upgrade command. Inference remains closed until reviewed model bounds and access policy are configured. Use portal.mode=baseline only for an explicitly requested legacy Claude-only portal. Preserve existing ledgers and complete the runtime acceptance checks before claiming customer spending guarantees.
