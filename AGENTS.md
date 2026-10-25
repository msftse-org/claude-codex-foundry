# Working in this repository

For a fresh customer deployment, read [the LLM deployment and onboarding runbook](docs/llm-onboarding.md) first. It includes a customer input checklist, a prompt to hand to an assistant, the supported deployment commands, user import, verification and handover. Use the checked-out scripts and their `--help` as the command reference.

For repository changes, follow [CONTRIBUTING.md](CONTRIBUTING.md). Preserve unrelated work and customer files. Run the relevant offline checks; a request to edit or review code does not authorize deploying Azure resources.

Keep each customer's configuration, identity lists, reports and installer bundles in a separate checkout, under ignored `.local/` and `dist/`. Never commit credentials or customer data. Do not copy another customer's deployment state to start a new installation.

The one-command deployment creates the baseline gateways and optionally the Claude administration portal. Portal governance requires a separate reviewed publication. Enforced USD budgets and shared Claude/Codex delegated administration require the additional budget runtime and its documented acceptance checks; do not claim these are enabled by the baseline script.
