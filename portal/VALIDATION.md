# Preview validation record

Status: **preview**. The portal source is included in `main` for continued development following maintainer approval on 2026-10-14. This record describes preview checks, not a production readiness certification. Customer configuration and raw Azure evidence remain outside Git.

## Verified on 2026-10-14

- Python suite: 68 passing tests, including token verification, role boundaries, suspension, revision conflicts, import validation, policy compilation, and publication rollback behavior.
- Frontend: strict TypeScript production build and 12 CSV/estimate/authentication/cost-analysis tests pass. Authentication tests cover rejected redirects without automatic retries, cached-account reuse, the absence of a forced account picker, and failed bootstrap handling.
- Infrastructure: Bicep compiles; the isolated Azure Functions preview package deploys with managed identity and private Blob networking.
- Hosted public endpoints: health and sign-in bootstrap return 200; unauthenticated state access returns 401.
- Separate APIM preview API: all nine deployed Anthropic models answer small real inference requests. Streaming, token counting, missing/invalid credentials, model denial, filtered catalog, suspension, and an organization token quota were checked. Original preview policies were restored afterward.
- Application Insights: live preview request aggregates and current-month token metrics load using local administrator credentials. No prompt/response capture is enabled.
- Scale of configuration: 2,000-member CSV import and generated policy size pass automated checks. This is not a concurrency or throughput test.
- UI: overview, live usage/budgets, group editor, command search with Enter navigation, dialog focus restoration, and a 390-pixel layout were inspected in the browser.
- The existing production gateway policy was compared with its pre-test hash and remains unchanged.

Hosted owner sign-in, private Blob state reads/writes, and managed-identity Application Insights queries are verified in the owner’s existing Edge session. Earlier sign-in attempts returned `AADSTS50012`; the existing signed-in session is reused. No authentication-method or tenant policy was changed. All nine portal models received dated, sourced public planning rates through the authenticated interface, including the deployed US Data Zone multiplier for Opus 5.5. All nine rates persisted; the hosted estimator matched independently checked arithmetic for real preview metrics.

Cost-analysis checks cover bounded ranges, escaped KQL literals, rejected partial queries, explicit breakdown truncation, unauthenticated/developer denial, billing access, and cache invalidation when rates change. Live query validation covers current/previous periods, grouping, filters, and standard-rate arithmetic. Browser checks cover combined model/group filtering, daily tables, browser-back filter restoration, drawers with fixed action bars, discarded unsaved edits, and a 390-pixel light/dark layout.

## Still required before release

- Verify APIM publication through the portal's managed identity and complete the remaining hosted write/role journeys.
- Exercise real administrator, billing, auditor, and developer sessions under customer Conditional Access and consent policies.
- Validate each quota/rate scope, publication interruption and recovery, two-administrator conflicts, and Blob version recovery against Azure.
- Run the full Mac onboarding journey from an exported portal profile and verify model defaults and revocations.
- Complete keyboard/screen-reader and error-state coverage, operational monitoring, retention and recovery procedures, security review, and load/cost measurements for the intended deployment size.

The detailed release checklist and cost limitations are in [README.md](README.md). Source merges are permitted for continued development. Complete the outstanding checks and obtain explicit rollout approval before a production release or customer production deployment.
