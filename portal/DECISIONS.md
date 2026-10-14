# Implementation decisions

- Deploy one installation per customer subscription and Entra tenant; this is not a shared multitenant SaaS service.
- Use React/TypeScript with a Python API to reuse the tested gateway compiler. A static frontend needs no Next.js server.
- Use Azure Functions Flex Consumption and private Blob Storage with managed identity. No always-on database, registry, or portal subscription is required.
- Use an Entra SPA registration and authorization-code flow with PKCE. Validate API access tokens on the server; never trust browser roles or identity headers.
- Administrators manage gateway membership, not the customer's directory. Directory account lifecycle and Conditional Access remain in Entra.
- Publish model access and token quotas into standard APIM; the portal is not in the inference request path and may sleep without interrupting developers.
- Use APIM managed identity for Foundry when governance is published. Developers need no direct Foundry RBAC. Existing direct access must be reviewed because it bypasses gateway controls.
- Keep policy drafts and publication separate, with impact review, revision checks, and audit events. Failed publication remains visible.
- Monthly token quotas are gateway controls, subject to APIM streaming/concurrency estimation. Dollar budgets are planning thresholds, not guaranteed billing caps.
- Do not simulate invoices, compliance certifications, chat retention, device enrollment, SCIM, or Anthropic-controlled features. Link administrators to the authoritative systems.
- Start with built-in administration roles and one primary group per member, so model grants and usage attribution have a clear source.
- Keep production data, app registrations, deployment profiles, and validation evidence out of public Git.
