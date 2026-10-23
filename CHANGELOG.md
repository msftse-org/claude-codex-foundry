# Internal change history

Implementation history for maintainers; user setup instructions live in README.md.

### 2026-10-23 - Shared Claude route for portal administration

- Added shared-route portal preparation by default, retaining explicit isolated preparation.
- Protected portal-managed policies from baseline gateway redeployment.
- Added mode-aware non-destructive verification and shared-route regression coverage.
- Updated deployment, identity, operations and migration guidance without changing model or network defaults.
- Validated 268 Python tests, 45 frontend tests, package/frontend builds and all five Bicep templates.
- Verified an authorized existing deployment end to end: shared-route reporting, reviewed publication, real Claude Code inference/tool use, and preview-only API/permission removal.
- Confirmed newly governed token usage is attributed to members/groups; retained historical metrics remain unattributed. Portal preview and delayed-ingestion limitations still apply.
