export type Role =
    | "owner"
    | "admin"
    | "group_admin"
    | "billing"
    | "auditor"
    | "developer";
export interface Member {
    id: string;
    name: string;
    email: string;
    role: Role;
    status: "active" | "suspended";
    group_id: string;
    monthly_tokens: number;
    extra_models: string[];
    spend_limit_usd?: string | null;
}
export interface Group {
    id: string;
    name: string;
    description: string;
    model_ids: string[];
    default_model: string;
    monthly_tokens: number;
    budget_usd: number;
    spend_ceiling_usd?: string | null;
    spend_ceiling_unlimited?: boolean;
    spend_limit_usd?: string | null;
    delegated_models?: string[];
}
export interface Model {
    name: string;
    labelOverride: string;
    anthropicFamilyTier: string;
    isFamilyDefault?: boolean;
}
export interface Settings {
    name: string;
    monthly_budget_usd: number;
    spend_limit_usd?: string | null;
    budget_enforcement?: boolean;
    spend_limit_unlimited?: boolean;
    monthly_tokens: number;
    member_tokens_per_minute: number;
    member_requests_per_minute: number;
}
export interface Audit {
    id: string;
    at: string;
    actor_id: string;
    actor: string;
    action: string;
    target: string;
    before: unknown;
    after: unknown;
}
export interface AccessRequest {
    id: string;
    member_id: string;
    member_name: string;
    at: string;
    model: string;
    reason: string;
    status: string;
    note?: string;
}
export interface State {
    revision: number;
    budget_configured?: boolean;
    client_types?: ("claude" | "codex")[];
    budget_prices?: BudgetPrice[];
    viewer: Member;
    members: Member[];
    groups: Group[];
    models: Model[];
    settings: Settings;
    audit: Audit[];
    prices: {
        model: string;
        input_per_million: number;
        output_per_million: number;
        source_url?: string;
        verified_on?: string;
        note?: string;
    }[];
    requests: AccessRequest[];
    published_revision: number | null;
    policy_hash: string;
    published_hash?: string;
    publish_status: string;
    published_at: string | null;
    gateway_url: string;
    tenant_id: string;
}
export interface Aggregate {
    key: string;
    requests: number;
    errors: number;
    duration_ms: number;
}
export interface Telemetry {
    status: string;
    daily: Aggregate[];
    models: Aggregate[];
    people: Aggregate[];
    requests: {
        timestamp: string;
        name: string;
        resultCode: string;
        duration: number;
        model: string;
        principal_id: string;
    }[];
    sampled_rows?: number;
    truncated?: boolean;
    note?: string;
    month_tokens?: TokenUsage[];
}
export interface TokenUsage {
    model: string;
    group_id: string;
    principal_id: string;
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
}
export interface Bootstrap {
    tenantId: string;
    clientId: string;
    configured: boolean;
    local: boolean;
    name: string;
}

export interface BudgetPrice {
    model: string;
    provider: "anthropic" | "openai";
    deployment_version: string;
    max_input_tokens: number;
    max_output_tokens: number;
    default_output_tokens: number;
    input_ceiling_per_million: string;
    output_ceiling_per_million: string;
    // Actual list rates (USD per 1M tokens) used for settlement, when known.
    rates?: Record<string, string | null> | null;
    long_context?: Record<string, string | number | null> | null;
    source_url: string;
    verified_at: string;
    // Review-by date. Past it, access continues unless the owner enforces review.
    valid_until: string;
    note: string;
}
