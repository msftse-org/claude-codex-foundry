# Security model

The client obtains a delegated Microsoft Entra access token for `https://cognitiveservices.azure.com`. Standard APIM validates the signed token, issuer, tenant, audience and expiry, optionally restricts caller object IDs, and forwards the original bearer token to Foundry. Foundry enforces each caller's Azure inference RBAC. APIM does not impersonate all users using a shared backend key or managed identity.

The provided foundation disables Foundry local/API-key authentication. APIM's `subscriptionRequired: false` removes the subscription-key gate **after** JWT validation is installed. It does not make the API anonymous. Incoming API-key headers are stripped before forwarding.

The gateway enforces one organization-wide model allowlist. This release does not implement per-user model permissions, spending limits or a management portal. Foundry RBAC revocation remains the inference authorization control. A tenant user without Foundry RBAC may see the authenticated static model catalog unless an APIM object-ID allowlist is configured.

The public Foundry endpoint remains reachable directly by users with valid Foundry RBAC. Therefore APIM logging/model restrictions are not a guaranteed enforcement boundary against deliberate direct access. Mandatory gateway-only access needs a separately designed private network path or backend credential architecture; the public foundation does not claim to enforce organization budgets.

MSAL uses a tenant-owned public client, browser PKCE, and encrypted persistence backed by macOS Keychain. No client secret is shipped. There is no plaintext token-cache fallback. Azure CLI mode uses the CLI's own credential storage and account selection. Tenant targeting avoids changing the default subscription during background refresh. Interactive Azure CLI login may change CLI account state as part of normal CLI behavior.

The local profile controls the HTTPS destination receiving the user's token. Distribute it and the installer through a trusted organization channel. Files use private permissions. Backups can contain credentials that predated installation and should receive the same protection as the original settings. This release is a source/script distribution, not a signed or notarized installer package.

New diagnostics log request metadata, model, request ID and the Entra principal object ID, with zero request/response body bytes. Treat user object IDs as organizational personal data, restrict log-reader RBAC, and apply your retention policy. Existing diagnostics are preserved; review them separately for payload/header logging. Token metrics are operational telemetry, not a billing ledger.

Provider hosting, geographic processing and retention depend on the model version/SKU. Review [Microsoft's Claude model documentation](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models) for the deployments selected by your organization.
