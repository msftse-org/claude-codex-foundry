# Claude & Codex on Azure

Python CLI, APIM policies and an Azure Functions administration portal for customer-owned AI access.

## Project structure

```text
src/claude_foundry/   CLI, gateway policies, client setup and portal backend
scripts/             Deployment preparation, packaging and live verification
infra/               Foundation, portal, content-log and budget Bicep templates
portal/web/          React frontend and tests
portal/functions/    Azure Functions entry point
budget/              Optional budget runtime
tests/               Offline Python tests
docs/                User and operator guides
examples/            Placeholder organization configuration
```

## Key decisions

- Portal preparation defaults to the existing Claude API; reporting and reviewed publication must target the same route used by clients.
- Attaching a portal must preserve existing policies and state. It must not implicitly enable governance.
- Isolated APIs remain explicit options for disruptive live tests, not prerequisites for ordinary deployment.
- Portal governance changes backend authorization to APIM managed identity. Baseline deployment must not overwrite published governance.
- Codex and budget protocol/runtime changes are separate scopes.

## Validation

Use the commands in CONTRIBUTING.md. Keep customer state, deployment outputs and raw validation evidence in ignored local/session storage. A source merge does not deploy Azure.
