# Product documentation plan

Status: proposed. Repository and open-source research reviewed on October 27, 2026.

## Recommendation

Keep the authoritative documentation in this repository as ordinary Markdown. Render the same articles inside the administration portal with navigation, search, section links, and help links from the relevant screens. Ship documentation with each portal build so it describes that deployed version.

For the current React 19 and Vite portal, the recommended first implementation is **react-markdown + remark-gfm + MiniSearch**, using the portal's existing components and styles. This supplies open-source Markdown rendering and local search while keeping the documentation experience native to the app.

**Fumadocs is the leading full documentation framework alternative.** Its Markdown/MDX layer supports Vite. Before selecting it, run a small integration experiment against the portal's routing, themes, authentication, and build. Vite content support alone does not establish that the complete UI is a drop-in integration.

Start with the content structure and core user journeys. The first portal implementation should prove three articles, deep links, and search before expanding the library. A separate documentation website can later reuse the Markdown if public discovery, multiple product versions, or translations become necessary.

## Product positioning

Use the structure and clarity of Microsoft Learn's Azure product documentation: explain the product, help someone achieve their first result, then organize detailed tasks, concepts, and reference material. Describe this independently maintained, customer-deployed solution and its actual release status.

Suggested opening copy:

> Claude & Codex on Azure connects familiar Claude and Codex applications to organization-approved models through Azure Foundry, Azure OpenAI, and standard Azure API Management. Developers sign in with Microsoft Entra. Platform teams deploy the shared infrastructure in their own Azure subscription, and administrators manage access and usage through a customer-owned portal.
>
> An organization installation bundle configures supported Mac clients. Optional gateway capabilities add published access policies, usage limits, content capture, and budget controls. This documentation explains how to deploy a pilot, register members, publish access, onboard developers, and operate the deployment.

Follow the introduction with a capability map, architecture diagram, current limitations, and a “Choose your starting point” section. Link detailed prerequisites from the appropriate journey.

### Explain deployment modes early

| Mode | How inference access works | Documentation requirement |
| --- | --- | --- |
| Baseline gateway | APIM validates the developer's Entra token; Foundry uses the developer's inference permissions. | Explain inference RBAC, approved models, and client configuration. |
| Portal governance preview | APIM uses managed identity to reach Foundry; published portal policies control membership, grants, token quotas, and rate limits. | Mark the legacy policy path as Claude-only and explain draft versus published state. |
| Budget hierarchy preview | Dedicated APIM routes call a budget Function, which reserves and settles organization, group, and user USD allowances around inference. | Explain shared Claude/Codex policy, delegated group administration, explicit deployment, and outstanding release checks. |

Existing endpoints do not acquire USD enforcement automatically. The administration portal manages policy; the budget service participates in inference when that mode is enabled. Direct Foundry inference permissions can provide a path outside gateway controls.

Use [identity and permissions](identity-and-permissions.md), [budget architecture](budget-architecture.md), and [budget validation](budget-validation.md) as evidence when writing these distinctions.

## Readers and starting points

| Reader | First journey | Next tasks |
| --- | --- | --- |
| Azure/platform administrator | Plan and deploy a pilot. | Add models, deploy optional capabilities, monitor and upgrade. |
| Portal owner or administrator | Register a member and publish access. | Manage groups, roles, imports, suspensions, and access requests. |
| Group administrator | Manage their group's access and budgets in budget mode. | Publish scoped changes and onboard group members. |
| Developer | Install the organization bundle and make a first request. | Request access, use Codex, migrate history, and restore settings. |
| Billing or finance reader | Understand usage and estimated costs. | Maintain planning rates where permitted, filter usage, and export data. |
| Auditor or security reviewer | Understand identity and enforcement boundaries. | Inspect administrative audit records and permitted content-log access. |
| Contributor | Add a portal feature or backend module. | Understand API permissions, state changes, publication, packaging, and validation. |

Role labels must match the API and enabled mode. Some tasks require Azure or Entra permissions in addition to a portal role. State those prerequisites in each guide.

## Information architecture

Organize by learning intent, with task shortcuts for each reader:

```text
Documentation
├── Overview
│   ├── What is Claude & Codex on Azure?
│   ├── Capabilities and supported clients
│   ├── Architecture and deployment modes
│   └── Preview status and limitations
├── Quickstarts
│   ├── Deploy a pilot
│   ├── Register and onboard your first developer
│   └── Connect Claude or Codex on a Mac
├── Tutorials
│   ├── Set up a team from deployment to first request
│   └── Introduce group budgets in a pilot
├── How-to guides
│   ├── Users, groups, and roles
│   ├── Client onboarding and conversation migration
│   ├── Models and access requests
│   ├── Policy publication and usage limits
│   ├── Budgets and cost analysis
│   └── Monitoring, audit, recovery, and upgrades
├── Concepts
│   ├── Identity and inference authorization
│   ├── Draft and published policy
│   ├── Model grants and primary groups
│   ├── Token limits, USD budgets, and estimated cost
│   └── Content capture and retention
├── Reference
│   ├── Role permissions and CLI commands
│   ├── Configuration and client profile schema
│   ├── CSV import format
│   └── Supported clients, models, and limitations
├── Troubleshooting and FAQ
└── Contributor guides
    ├── Add a portal feature or module
    └── Extend gateway or model support
```

A quickstart produces one initial result. A tutorial teaches an end-to-end scenario. A how-to solves one specific task. Concepts explain behavior; reference provides precise lookup information.

### Resolve “create a user” and “add a module”

**Create a user:** write separate directory and portal instructions. Entra creates or invites the account. The portal registers an existing Entra object ID and assigns permitted membership and access settings. The portal currently has no directory account creation or invitation workflow.

The first-developer quickstart should connect these steps: obtain the Entra account/object ID, register the portal member, assign a primary group and model access, review and publish, distribute the installation ZIP, sign in, and verify a request.

**Add a module:** provisionally cover three distinct tasks:

1. Add an Azure model deployment and expose it through the appropriate catalog and gateway.
2. Enable an optional capability, such as the budget gateway or content capture.
3. Add a portal feature or Python module as a contributor.

The repository does not currently expose a general installable module registry. Describe its actual extension points; reserve “Install a module” for a future supported product workflow.

## First writing batch

Paths below are proposed destinations under `docs/`. Existing source pages remain in place during migration.

| Priority | Article | Proposed path | Starting evidence |
| --- | --- | --- | --- |
| P0 | What is Claude & Codex on Azure? | `overview/index.md` | Root README, architecture and security guides. |
| P0 | Understand deployment modes and preview status | `overview/deployment-modes.md` | Identity guide, budget architecture, portal/budget validation. |
| P0 | Plan an Azure deployment | `how-to/deployment/plan.md` | Administrator guide and infrastructure parameters. |
| P0 | Deploy a pilot | `quickstarts/deploy-pilot.md` | Administrator guide, portal deployment instructions, supported CLI. |
| P0 | Register and onboard your first developer | `quickstarts/onboard-developer.md` | Portal guide, member API, onboarding component, Mac guide. |
| P0 | Register an existing Entra user | `how-to/users/register-member.md` | Member drawer, roles, member validation, identity guide. |
| P0 | Create a group and grant model access | `how-to/groups/create-group.md` | Groups UI/API and publication behavior. |
| P0 | Review and publish access changes | `how-to/policies/publish.md` | Publication review, legacy and budget publication APIs. |
| P0 | Install and verify Claude on a Mac | `quickstarts/claude-on-mac.md` | Mac guide, installer, full organization bundle. |
| P0 | Configure Codex with Azure models | `quickstarts/codex-on-azure.md` | Codex guide, qualification results, onboarding setup prompt. |
| P1 | Request and approve model access | `how-to/models/request-access.md` | Requests UI and API role checks. |
| P1 | Add a model deployment | `how-to/models/add-model.md` | Discovery/provisioning CLI, qualification, catalogs, planning rates. |
| P1 | Set organization, group, and user budgets | `how-to/budgets/configure.md` | Budget rules, ledger, UI, scoped publication, validation. |
| P1 | Add a portal feature or module | `contributing/add-portal-module.md` | Contributor guide, frontend navigation, API/state, packaging. |

Write P0 as the first usable library; add P1 once the core paths are coherent. Each guide needs an expected result and a verification step.

The next batch should cover CSV import; changing roles; suspending access; personal model grants; token quotas and rate limits; group administrator onboarding; enabling the budget gateway; planning rates and cost exports; monitoring; audit inspection; content logs; recovery; upgrades; client restore; and conversation migration.

### Accuracy points for the first batch

- Use actual UI labels and supported CLI options. Confirm commands from the parser/help before including runnable examples.
- Explain which changes take effect on save and which require publication. Portal role/status changes affect subsequent portal calls; gateway policy changes require the appropriate publish workflow.
- Distinguish profile export from the full installation ZIP. The working tree includes bundle download and a Codex setup prompt; verify the target release before describing that experience as shipped.
- A draft profile export does not prove gateway access is active. Publish and verify the route before distributing onboarding instructions.
- Explain zero and inheritance by feature: baseline token limits can use zero as unlimited; enforced USD zero blocks new requests; a blank personal budget inherits the group rule.
- Separate planning estimates from Azure billed spend. Explain rate coverage and how current planning rates affect displayed historical estimates.
- Keep content logging opt-in and explain audited access. It captures new gateway traffic when enabled and does not supply a complete historical conversation archive.

## Article contract

Use plain `.md` with headings, numbered steps, fenced code, tables, links, and images. Avoid React imports or executable MDX in routine product guides. This keeps articles readable on GitHub and reusable in another renderer.

Each article needs:

1. A task-based title and one-sentence outcome.
2. Prerequisites: portal role, Azure/Entra permissions, client, enabled features, and deployment mode.
3. Numbered instructions using exact screen labels or verified commands.
4. A description of when changes become effective.
5. Verification with the expected observable result.
6. Recovery, cleanup, or a relevant troubleshooting link.
7. Related tasks and the next useful step.

Proposed front matter:

```yaml
---
id: register-member
title: Register an existing Entra user
summary: Give an existing directory account membership in the portal.
type: how-to
audiences: [owner, admin]
applies_to: [portal-governance-preview, budget-hierarchy-preview]
aliases: [create user, add user, register developer]
---
```

Record `last_verified` and the release or commit used for verification only after checking the workflow. A research date or file edit date is not a successful workflow verification.

Maintain article identity in front matter and navigation order in a small manifest. The manifest references article IDs; it should not duplicate titles, summaries, or role metadata. Validate IDs, navigation entries, links, and anchors at build time.

Use repository-relative links in source. The portal resolves article links to its route and image links to packaged assets. Use consistent GitHub-compatible heading anchors. Standardize callouts in a form readable on GitHub. Audit existing HTML disclosure blocks before choosing sanitized rendering or rewriting them as Markdown. Render Mermaid fences through a lazy-loaded component when diagrams are present.

Screenshots must show actual, sanitized runtime behavior and state their capture date and environment. Follow the [image guidance](images/README.md). Keep real customer identities, credentials, and conversation content out of source and search fixtures.

## Portal experience

Add **Documentation** to the main navigation for every authenticated role, including developers and group administrators. Update both navigation filtering and current-page redirect rules.

Provide a documentation landing page with reader/task shortcuts, article navigation, breadcrumbs, a desktop table of contents, heading links, copyable code, related articles, and the documented release. Use existing typography, colors, spacing, dialogs, and keyboard behavior. On narrow screens, collapse navigation and the table of contents into accessible controls.

Contextual help should open the relevant article:

| Portal screen | Help destination |
| --- | --- |
| Members | Register a member; import CSV; change roles; suspend access. |
| Groups | Create a group; assign models; understand primary membership. |
| Models and requests | Add a model; request access; approve access. |
| Onboarding | Download the bundle; install Claude; configure Codex; verify. |
| Publication review | Draft versus published policy; publish; investigate failures. |
| Budgets and costs | Allowances, inheritance, planning estimates, and rates. |
| Audit and content logs | Administrative auditing and permitted content inspection. |

The portal currently uses query-based navigation. Start with a route such as `?page=docs&article=register-member#verify-access`. Preserve article selection and anchors through sign-in, refresh, back/forward navigation, and copying links. Missing articles should show a useful documentation error with navigation and search. Clean `/docs/...` URLs can follow if the app adopts path-based routing.

Sign-in controls access to the portal experience. Static documentation and search assets must contain only distributable product material; role-aware navigation does not make those files confidential.

### Search

Generate a section-level index from the same Markdown during the build. Index titles, summaries, headings, aliases, and body text. Boost exact title and heading matches; support prefix and typo-tolerant matching. Return a short excerpt and a link to the matching section.

Add documentation results to the existing command palette as a distinct result group alongside pages and people. Also provide search within Documentation. Load the index and article content lazily so the library does not inflate initial portal startup.

Use aliases to support user language: “create user,” “onboard,” “add module,” “zero budget,” and “publish access.” Distinguish directory creation from portal registration and contributor modules from model deployment.

The generated documentation index must never include live members, tenant policy, audit records, or captured prompts. No external search service is needed for the first implementation.

## Open-source options

Research reflects upstream documentation available on October 27, 2026. Check package versions and compatibility when implementing.

| Option | License | Fit for this project | Decision |
| --- | --- | --- | --- |
| [react-markdown](https://github.com/remarkjs/react-markdown) + [remark-gfm](https://github.com/remarkjs/remark-gfm) + [MiniSearch](https://github.com/lucaong/minisearch) | MIT | React Markdown rendering, GFM tables, and browser search. The portal supplies the layout and article pipeline. | Recommended first implementation; smallest change to the current app architecture. |
| [Fumadocs](https://www.fumadocs.dev/docs) | MIT | Modular content, search, and documentation UI. [Vite content integration](https://www.fumadocs.dev/docs/mdx/vite) is documented; full integration needs routing/provider and styling work. | Leading framework candidate. Prove compatibility before adopting. |
| [Docusaurus](https://docusaurus.io/docs) | MIT framework | React static documentation site with versioning, navigation, and localization. [Search options](https://docusaurus.io/docs/search) include Algolia, community local search, and custom integration. | Strong separate-site option; a full embed introduces another build and navigation shell. |
| [Nextra](https://nextra.site) | MIT | Next.js/MDX documentation with [Pagefind search](https://nextra.site/docs/guide/search) and static export support. | Better suited to a separate Next.js documentation build than this Vite portal. |
| [Pagefind](https://pagefind.app/docs/) | MIT | Search engine with static index bundles. Its [Node API](https://pagefind.app/docs/node-api/) supports custom article records without crawling rendered pages. | Credible search alternative if MiniSearch index size or ranking becomes a problem. |
| [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) | MIT | Python static documentation with browser search; upstream announces end of life on May 5, 2027. | Exclude from a new implementation shortlist given the announced maintenance window. |

### Fumadocs compatibility experiment

Use the overview, member registration, and onboarding articles. Test the content plugin with this exact React/Vite build; route links through the existing navigation; connect providers without replacing portal authentication or themes; and measure required styling changes, added dependencies, and bundle size.

Choose Fumadocs if its reusable navigation, table of contents, and search reduce maintenance while meeting these constraints. Otherwise keep the smaller React Markdown implementation. Preserve the same Markdown and article IDs whichever option wins.

## Content and build design

```text
docs/**/*.md + docs/navigation.json + allowlisted images
                    │
              content build
                    │
        manifest + article assets + section search index
                    │
             portal/web/dist/assets
                    │
           existing portal deployment ZIP
                    │
             Documentation in the portal
```

Use an explicit manifest of product articles and referenced images. Do not ship every repository Markdown file automatically. Keep generated output out of the authored source tree.

The current backend mounts `/assets` and returns the SPA HTML for other non-API paths. Generate content under the mounted asset path or add an explicit static mount. Simply placing JSON at `/docs/...` would currently return HTML. Use content-hashed assets and a manifest tied to the portal build.

Repository `main` can contain newer docs than a deployed portal. Display the documented release/build in the portal and make repository links point to that revision where appropriate. Begin with one library per deployed build; introduce a version selector only when multiple supported releases justify it.

Proposed implementation work:

| Area | Files or proposed addition | Purpose |
| --- | --- | --- |
| Authored content | `docs/README.md`, article folders, `docs/navigation.json` | GitHub hub, shared articles, explicit navigation. |
| Content generation | Proposed `portal/web/scripts/build-docs.mjs` or a Vite plugin | Validate metadata/links, generate assets and section index, refresh during development. |
| Article experience | Proposed `portal/web/src/Documentation.tsx` and docs components | Render the hub/articles, table of contents, and search. |
| Navigation and links | [main.tsx](../portal/web/src/main.tsx) | Docs access for all roles, route state, sign-in return links, contextual help. |
| Command palette | [Command.tsx](../portal/web/src/Command.tsx) | Ranked documentation results and section links. |
| Visual styling | [style.css](../portal/web/src/style.css) | Reuse portal tokens and add article layouts. |
| Build and packaging | [package.json](../portal/web/package.json), [Vite config](../portal/web/vite.config.ts), [build_portal.py](../scripts/build_portal.py) | Generate docs in dev/build and include all assets in the ZIP. |
| Static serving, if needed | [app.py](../src/claude_foundry/portal/app.py) | Serve generated content correctly without SPA fallback collisions. |

No additional Azure runtime service or documentation CMS is needed for this design.

## Migration of existing material

Preserve existing paths and anchors while extracting smaller articles. Existing pages can become entry pages that link to the new guides. Move or remove old content only after replacement articles are verified and inbound links are handled.

| Existing material | Planned use |
| --- | --- |
| [Administrator guide](administrator.md) | Deployment prerequisites, pilot quickstart, model provisioning, gateway and bundle tasks. |
| [Portal guide](portal-guide.md) | Feature overview, member/group/request/publication tasks, contextual screenshots. |
| [Mac guide](macos.md) and [Codex guide](codex.md) | Separate client quickstarts, detailed setup, verification, restore, limitations. |
| [Conversation migration](conversation-migration.md) | Optional history-import and handoff guides. |
| [Identity](identity-and-permissions.md) and [security](security.md) | Concepts and role/permission reference. |
| [Budget architecture](budget-architecture.md) and [budget rules](budget-enforcement.md) | Mode concepts, budget setup, inheritance and enforcement reference. |
| [Operations](operations.md), [observability](observability.md), [content logs](content-logs.md) | Focused operational how-tos and concepts. |
| [Troubleshooting](troubleshooting.md) and [FAQ](faq.md) | Task-linked problem resolution and searchable answers. |
| [Portal README](../portal/README.md), [pricing](../portal/PRICING.md), [design](../portal/DESIGN.md) | Deployment/contributor reference and cost-estimate semantics. |
| Validation records | Evidence for preview status and release gates; separate from the default product task navigation/search. |

## Delivery sequence and acceptance

1. **Content foundation:** add the documentation hub, article contract, navigation metadata, and P0 guides. Reconcile the newer onboarding flow with the target release. Done when a reader can follow deployment → membership → publication → installation → first request.
2. **Portal proof:** integrate three articles and local search; run the Fumadocs experiment and record the choice. Done when contextual help, search, deep links, themes, and role access work in the packaged portal.
3. **Complete how-to library:** add P1 and the next batch, extract long existing guides, and add links from all relevant screens. Done when each supported administration task has a dedicated guide with prerequisites and verification.
4. **Reference and upkeep:** finish CLI/schema/permission reference and documentation checks in CI. Require relevant docs updates when workflows or UI labels change. Add release navigation or a separate site when there is a concrete need.

For documentation-only changes, check links, image references, headings, terminology, and commands against current help/source. Do not run billable inference or deploy Azure resources solely to validate prose. Mark workflows needing live validation accordingly.

For the portal implementation, verify:

- Search for “create user,” “onboard,” “add module,” and a misspelling finds the intended guide and section.
- Direct article/anchor links survive Microsoft sign-in, refresh, and browser back/forward.
- Every authenticated role can read Documentation; recommendations do not change operation permissions.
- Keyboard navigation, readable narrow-screen layouts, both themes, code copying, and missing-article handling work.
- Relative links, images, Mermaid diagrams, and heading anchors render consistently.
- The deployment ZIP contains the generated articles/index and serves them as the correct file types.
- Article and search loading stay lazy; measure size and startup impact before choosing performance budgets.
- Static build output contains only allowlisted product documentation and sanitized examples.

## Research references

The comparison above links directly to upstream tools and integration documentation. Additional design references:

- [Azure Foundry documentation landing page](https://learn.microsoft.com/en-us/azure/foundry/) — product introduction and navigation by learning intent.
- [Fumadocs manual integration](https://www.fumadocs.dev/docs/manual-installation/react-router) — example framework/provider and styling requirements.
- [Nextra static export](https://nextra.site/docs/guide/static-exports) — separate static-build option.
- [Pagefind license](https://github.com/Pagefind/pagefind/blob/main/LICENSE) — license for the search alternative.
