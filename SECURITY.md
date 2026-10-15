# Security reporting

Report suspected vulnerabilities through [the public project's private vulnerability reporting](https://github.com/orsharon7/claude-codex-foundry/security/advisories/new). Private reporting is enabled on that public repository; reports are confidential. This team repository is private, but its issues are visible to everyone with repository access and are not a confidential security reporting channel. Do not disclose exploit details, tokens, customer identities, or affected infrastructure in a public issue.

Include the affected commit/version, component, expected security boundary, reproduction steps using a test environment, and likely impact. Redact credentials and customer data; a minimal proof of concept is more useful than a complete production log dump. Do not test against an organization without authorization.

For an active exposure, notify your organization's security team and revoke affected credentials or access through its incident process. A repository report does not replace that response. Microsoft/Azure or Anthropic service vulnerabilities should also be reported through the relevant vendor's security channel.

## Project status

The project is under active development and the portal remains a preview. There is no published long-term support window or response-time SLA. Describe the exact affected commit in your report; do not assume an older snapshot receives security updates automatically.

The technical [security architecture](docs/security.md) documents trust boundaries and limitations. The [operations guide](docs/operations.md) covers ownership and recovery, and the [portal validation record](portal/VALIDATION.md) identifies work still required before production rollout.
