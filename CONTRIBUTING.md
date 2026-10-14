# Contributing

Thanks for helping improve Claude Foundry. Useful contributions include clearer installation instructions, reproducible bug reports, accessibility improvements, authentication fixes, and tested gateway or portal changes.

Start with the [README](README.md) for the product and the [security model](docs/security.md) and [identity/permission reference](docs/identity-and-permissions.md) before changing authentication or enforcement. The portal is a preview in `main`; remaining rollout work is tracked in [portal/VALIDATION.md](portal/VALIDATION.md).

## Before you start

For a substantial feature, open an issue describing the user problem, proposed behavior, and scope. For a small fix, a focused pull request is sufficient. Search existing issues first. Never include customer configuration, access tokens, conversation content, or identifiable telemetry in a public issue.

There is currently no repository license. Ask the maintainer to clarify contribution and redistribution terms before submitting work that requires an explicit license grant. Do not add a license, sign-off requirement, or legal commitment on behalf of someone else.

## Set up a checkout

You need access to the private `msftse-org/claude-foundry` repository. Team contributions use branches and pull requests here. The public repository is maintained separately; do not copy private team or customer information into it.

Prerequisites: Git, [uv](https://docs.astral.sh/uv/), and Python 3.11 or newer (uv can supply Python). Portal work also requires Node.js 24 and npm, matching CI. Azure CLI with Bicep is needed for infrastructure compilation and live Azure work; offline Python and frontend tests need no Azure sign-in.

```bash
git clone https://github.com/msftse-org/claude-foundry.git
cd claude-foundry
git switch -c feat/describe-your-change
uv sync --frozen --extra auth --extra portal
npm --prefix portal/web ci --ignore-scripts
```

For external contributions, fork the repository and push your branch to your fork. Keep organization files under ignored `.local/`; use placeholders in committed examples.

## Find the right code

| Area | Location |
| --- | --- |
| CLI, model discovery, baseline gateway policy | `src/claude_foundry/cli.py`, `models.py`, `gateway.py`, `azure.py` |
| User authentication and Claude settings | `src/claude_foundry/auth.py`, `client.py`, `config.py` |
| Portal API, roles, drafts, policy compilation, storage | `src/claude_foundry/portal/` |
| React interface and frontend tests | `portal/web/src/` |
| Foundation and portal infrastructure | `infra/` |
| Mac bootstrap, packaging, Entra setup, live verification | `scripts/` |
| Offline Python tests and fixtures | `tests/` |
| Customer guides and screenshots | `docs/` |

See [portal/DESIGN.md](portal/DESIGN.md) and [portal/DECISIONS.md](portal/DECISIONS.md) for design context.

## Run the relevant checks

These checks use local fixtures and do not deploy Azure resources or make billable model requests:

```bash
uv run --frozen --extra auth --extra portal ruff check src scripts tests
uv run --frozen --extra auth --extra portal ruff format --check src scripts tests
uv run --frozen --extra auth --extra portal pytest -q
bash -n scripts/macos/install.sh Install.command
uv build
npm --prefix portal/web run build
npm --prefix portal/web test
```

Compile infrastructure changes with the same checks used by CI:

```bash
az bicep build --file infra/main.bicep --outfile /tmp/claude-foundry.arm.json
az bicep build --file infra/portal.bicep --outfile /tmp/claude-foundry-portal.arm.json
```

CI runs Python checks on Linux/macOS with Python 3.11 and 3.13, plus frontend and Bicep validation. For documentation-only changes, check links, commands against CLI help, image paths, and screenshot privacy; do not invoke live inference just to validate prose.

## Work on the portal locally

Follow [the local development instructions](portal/README.md#local-development) using a separate preview organization configuration and state file. The backend serves a built frontend on `127.0.0.1:8787`; Vite can serve the frontend on port 5173 with an API proxy.

Local mode intentionally impersonates the configured owner for development. Bind it to loopback only. It does not test Entra sign-in, tenant policy, or hosted role authorization. Use the authenticated Azure preview for those checks.

Inspect loading, empty, error, narrow-screen, and light/dark states. Use the shared field and drawer components; preserve accessible labels, keyboard navigation, focus restoration, and unsaved-change protection. Check the production frontend build as well as the development server.

## Live validation is a separate step

Live commands can provision billable resources, accept Marketplace terms, modify policies, or send billable inference requests. Use an explicitly authorized test subscription and a dedicated preview API. Review the command and target before running it; never use another contributor's customer environment.

The [portal verification workflow](portal/README.md#validation-and-release-checks) temporarily changes preview policies and restores them. Record the commit, environment type, checks performed, outcomes, and remaining gaps in sanitized validation notes. Keep raw Azure responses, downloaded profiles, and identity details outside Git.

A passing local test suite is not evidence of real Conditional Access, recovery, or production throughput.

## Code and dependency expectations

- Keep changes focused on an observable user problem. Preserve unrelated settings and provide a recovery path for configuration changes.
- Enforce authorization in the server/gateway, not only by hiding UI controls. Do not introduce shared inference secrets, plaintext token-cache fallbacks, or anonymous administrative routes.
- Follow existing Python types/style and Ruff formatting. Keep frontend TypeScript strict and reuse the established design tokens/components.
- Add meaningful regression coverage for behavior changes, especially auth, policy generation, concurrency, and recovery. Avoid tests that only restate implementation details.
- Review dependency updates together with their lockfile changes. Do not edit lockfiles by hand.

Python dependencies use `uv.lock`; the Mac authentication bundle additionally verifies hashes from `requirements-auth.lock`. After a reviewed Python dependency update, refresh the installer lock:

```bash
uv export --frozen --extra auth --no-dev --no-emit-project \
  --output-file requirements-auth.lock
```

Frontend dependencies use `portal/web/package-lock.json`. CI actions are pinned to commit hashes; preserve that convention.

## Documentation and screenshot standards

Write for the person performing the task: prerequisites, steps, expected result, verification, and recovery. Use placeholder subscriptions, domains, emails, and object IDs. Make clear whether a command reads state, deploys resources, publishes policy, or makes billable requests.

For UI changes, update the [portal walkthrough](docs/portal-guide.md) when behavior changes. Screenshots must show the real application and state the environment/date. Move the pointer outside the webpage before capture. Crop during capture to exclude names, emails, tenant/resource identifiers, URLs, and credentials. Do not edit values to imply results that did not happen. Keep full private evidence in `.local/`, and commit only the reviewed public images. See the [image capture notes](docs/images/README.md).

## Submit a pull request

1. Rebase or merge the current `main` into your working branch and resolve conflicts deliberately.
2. Run checks relevant to your changes; record both passing checks and anything not exercised.
3. Inspect `git diff --check`, the complete diff, and all new files for private data.
4. Use a concise commit title such as `fix(auth): preserve tenant selection` or `docs: clarify Mac recovery`.
5. Open a PR against `main` using the template. Explain the problem, resulting behavior, validation, and any rollout or compatibility concerns.

Keep follow-up changes on feature branches and merge through reviewed PRs. Source merges do not trigger cloud deployment or constitute a production release. Update the validation record as real evidence closes preview gaps; tagging and customer rollout are separate maintainer decisions.

## Verify security-sensitive portal changes

Run the role journey and failure-boundary tests with `uv run --extra portal pytest tests/test_portal.py tests/test_portal_costs.py`. Preserve tenant/audience/client/scope checks, use the same state snapshot for authorization and conditional writes, and test denied writes for absence of mutation. Never use local-owner mode as proof of authentication.

Audit the pinned runtime dependencies when changing packages:

```bash
uv export --frozen --extra portal --no-dev --no-hashes --no-emit-project \
  --format requirements-txt --output /tmp/claude-foundry-runtime.txt
uvx pip-audit --no-deps --disable-pip --requirement /tmp/claude-foundry-runtime.txt
npm --prefix portal/web audit --omit=dev
```

The export contains all resolved transitive runtime dependencies; `--no-deps --disable-pip` avoids resolving a second, potentially different environment. Inspect audit errors and skipped packages; an unavailable advisory service is not a passing result. Keep a dated, sanitized result in the validation record. Never publish a live token or raw customer policy to reproduce a security issue.
