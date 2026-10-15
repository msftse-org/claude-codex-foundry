import { Command } from "./Command";
import { MemberImport } from "./MemberImport";
import { CostAnalysis } from "./CostAnalysis";
import { Spend } from "./Spend";
import { UsageChart } from "./UsageChart";
import {
    MemberEditor,
    GroupEditor,
    SettingsForm,
    PublishDialog,
} from "./editors";
import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
    ArrowClockwise,
    ArrowRight,
    ArrowSquareOut,
    ChartLine,
    Check,
    CheckCircle,
    Clock,
    CurrencyDollar,
    DownloadSimple,
    GearSix,
    Key,
    List,
    MagnifyingGlass,
    Moon,
    Plus,
    ShieldCheck,
    SignOut,
    SlidersHorizontal,
    Stack,
    Sun,
    Users,
    UsersThree,
    X,
    Desktop,
    Tray,
} from "@phosphor-icons/react";
import { api, download, exportCsv, initialize, signIn, signOut } from "./api";
import type {
    Audit,
    Bootstrap,
    Group,
    Member,
    State,
    Telemetry,
} from "./types";
import {
    compact,
    date,
    Empty,
    Field,
    fmt,
    Drawer,
    DrawerClose,
    Notice,
    PageHead,
    Pager,
    roleName,
    Seal,
    Status,
} from "./components";
import "./style.css";

const pages = [
    { id: "overview", name: "Overview", icon: Stack, section: "" },
    { id: "requests", name: "Requests", icon: Tray, section: "" },
    { id: "members", name: "Members", icon: Users, section: "People" },
    { id: "groups", name: "Groups", icon: UsersThree, section: "" },
    {
        id: "roles",
        name: "Roles & permissions",
        icon: ShieldCheck,
        section: "",
    },
    {
        id: "analytics",
        name: "Usage & analytics",
        icon: ChartLine,
        section: "Usage & spend",
    },
    { id: "costs", name: "Cost analysis", icon: CurrencyDollar, section: "" },
    {
        id: "budgets",
        name: "Budgets & limits",
        icon: CurrencyDollar,
        section: "",
    },
    {
        id: "models",
        name: "Model access",
        icon: SlidersHorizontal,
        section: "Governance",
    },
    { id: "audit", name: "Audit log", icon: Clock, section: "" },
    {
        id: "onboarding",
        name: "Developer onboarding",
        icon: Desktop,
        section: "",
    },
    {
        id: "settings",
        name: "Organization",
        icon: GearSix,
        section: "Settings",
    },
];
type Panel =
    | { kind: "member"; value?: Member }
    | { kind: "member_import" }
    | { kind: "group"; value?: Group }
    | { kind: "publish" }
    | { kind: "audit"; value: Audit }
    | { kind: "command" }
    | { kind: "request" }
    | { kind: "pricing"; model: string };
const emptyTelemetry: Telemetry = {
    status: "loading",
    daily: [],
    models: [],
    people: [],
    requests: [],
};

function App() {
    const [boot, setBoot] = useState<Bootstrap>();
    const [state, setState] = useState<State>();
    const [usage, setUsage] = useState<Telemetry>(emptyTelemetry);
    const [usageError, setUsageError] = useState("");
    const [usageLoading, setUsageLoading] = useState(false);
    const usageSequence = useRef(0);
    const [signInDetails, setSignInDetails] = useState("");
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const [page, setPage] = useState(
        new URLSearchParams(location.search).get("page") || "overview",
    );
    const [query, setQuery] = useState(
        new URLSearchParams(location.search).get("q") || "",
    );
    const [panel, setPanel] = useState<Panel>();
    const [tablePage, setTablePage] = useState(0);
    useEffect(() => setTablePage(0), [page, query]);
    const [toast, setToast] = useState<{ message: string; error?: boolean }>();
    const [navOpen, setNavOpen] = useState(false);
    const [days, setDays] = useState(30);
    const [theme, setTheme] = useState(
        localStorage.getItem("claude-admin-theme") || "system",
    );
    const load = async () => {
        const data = await api<State>("/api/state");
        setState(data);
        setError("");
        return data;
    };
    const loadUsage = async (period = days) => {
        const sequence = ++usageSequence.current;
        setUsageLoading(true);
        setUsageError("");
        try {
            const result = await api<Telemetry>(
                `/api/telemetry?days=${period}`,
            );
            if (sequence === usageSequence.current) setUsage(result);
        } catch (e) {
            if (sequence === usageSequence.current) {
                setUsageError((e as Error).message);
                setUsage((current) => ({ ...current, status: "error" }));
            }
        } finally {
            if (sequence === usageSequence.current) setUsageLoading(false);
        }
    };
    useEffect(() => {
        initialize()
            .then(async (config) => {
                setBoot(config);
                if (config.signInError) {
                    setSignInDetails(config.signInError);
                    setError(
                        "Microsoft could not complete sign-in. Use this portal in your normal work browser and choose an authentication method permitted by your organization.",
                    );
                    return;
                }
                if (config.configured) {
                    const data = await load();
                    if (data.viewer.role !== "developer") await loadUsage();
                }
            })
            .catch((e) => setError(e.message));
    }, []);
    useEffect(() => {
        const media = matchMedia("(prefers-color-scheme: dark)");
        const apply = () => {
            document.documentElement.dataset.theme =
                theme === "system" ? (media.matches ? "dark" : "light") : theme;
        };
        apply();
        media.addEventListener("change", apply);
        localStorage.setItem("claude-admin-theme", theme);
        return () => media.removeEventListener("change", apply);
    }, [theme]);
    useEffect(() => {
        const pop = () => {
            setPage(
                new URLSearchParams(location.search).get("page") || "overview",
            );
            setQuery(new URLSearchParams(location.search).get("q") || "");
        };
        window.addEventListener("popstate", pop);
        const keys = (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "k") {
                e.preventDefault();
                setPanel({ kind: "command" });
            }
        };
        window.addEventListener("keydown", keys);
        return () => {
            window.removeEventListener("popstate", pop);
            window.removeEventListener("keydown", keys);
        };
    }, []);
    useEffect(() => {
        if (!toast || toast.error) return;
        const id = setTimeout(() => setToast(undefined), 6000);
        return () => clearTimeout(id);
    }, [toast]);
    const nav = (id: string, q = "") => {
        setPage(id);
        setQuery(q);
        setPanel(undefined);
        setNavOpen(false);
        const params = new URLSearchParams({ page: id });
        if (q) params.set("q", q);
        history.pushState({}, "", `?${params}`);
        document.getElementById("main")?.focus();
    };
    const search = (q: string) => {
        setQuery(q);
        const params = new URLSearchParams({ page });
        if (q) params.set("q", q);
        history.replaceState({}, "", `?${params}`);
    };
    const notify = (message: string, error = false) =>
        setToast({ message, error });
    const save = async (kind: string, value: unknown) => {
        if (!state) return false;
        setBusy(true);
        try {
            const result = await api<{ gateway_changed: boolean }>(
                "/api/changes",
                {
                    method: "POST",
                    body: JSON.stringify({
                        revision: state.revision,
                        kind,
                        value,
                    }),
                },
            );
            await load();
            notify(
                kind === "pricing"
                    ? "Planning rates saved. Cost estimates will refresh."
                    : kind === "request"
                      ? "Access request sent."
                      : result.gateway_changed
                        ? "Draft saved. Review and publish to apply gateway changes."
                        : "Changes saved.",
            );
            return true;
        } catch (e) {
            notify((e as Error).message, true);
            return false;
        } finally {
            setBusy(false);
        }
    };
    if (!boot)
        return (
            <div className="signinscreen">
                <div className="signincard">
                    <Seal />
                    <h1>Claude & Codex Admin</h1>
                    {error ? (
                        <Notice tone="error">{error}</Notice>
                    ) : (
                        <p className="muted">
                            Connecting to your organization…
                        </p>
                    )}
                    <button
                        className="button"
                        onClick={() => location.reload()}
                    >
                        <ArrowClockwise />
                        Retry connection
                    </button>
                </div>
            </div>
        );
    if (!state)
        return (
            <div className="signinscreen">
                <div className="signincard">
                    <Seal size={48} />
                    <span className="preview">Development preview</span>
                    <h1 style={{ marginTop: 24 }}>
                        Your organization.
                        <br />
                        Your Claude.
                    </h1>
                    <p className="muted">
                        One place to manage developer access, understand usage,
                        and set the boundaries.
                    </p>
                    {error && error !== "Sign in to continue." && (
                        <Notice tone="error">{error}</Notice>
                    )}
                    {signInDetails && (
                        <details className="footnote">
                            <summary>Sign-in diagnostic details</summary>
                            <p>{signInDetails}</p>
                        </details>
                    )}
                    {boot.configured ? (
                        <button
                            className="button primary"
                            onClick={() =>
                                signIn().catch((e) => setError(e.message))
                            }
                        >
                            <Key size={18} />
                            Continue with Microsoft
                        </button>
                    ) : (
                        <Notice>
                            Finish the single-tenant Entra application
                            registration to enable sign-in.
                        </Notice>
                    )}
                    <small>
                        Access is limited to people authorized by your
                        organization. Your existing Claude conversations stay on
                        your devices.
                    </small>
                </div>
            </div>
        );
    const admin = ["owner", "admin"].includes(state.viewer.role);
    const finance = admin || state.viewer.role === "billing";
    const developer = state.viewer.role === "developer";
    const dirty = state.policy_hash !== state.published_hash;
    const filteredPages = pages.filter(
        (p) => !developer || ["requests", "onboarding"].includes(p.id),
    );
    const currentPage =
        developer && !["requests", "onboarding"].includes(page)
            ? "onboarding"
            : page;
    const memberById = (id: string) => state.members.find((m) => m.id === id);
    const groupById = (id: string) => state.groups.find((g) => g.id === id);
    const modelLabel = (id: string) =>
        state.models.find((m) => m.name === id)?.labelOverride || id;
    const openRequests = state.requests.filter((r) => r.status === "open");
    const reviewCount =
        Number(dirty) +
        openRequests.length +
        Number(!state.settings.monthly_tokens);
    const downloadProfile = async (groupId?: string) => {
        try {
            download(
                "client.json",
                await api(
                    `/api/client-profile${groupId ? "?group_id=" + encodeURIComponent(groupId) : ""}`,
                ),
            );
            notify("Client profile downloaded.");
        } catch (e) {
            notify((e as Error).message, true);
        }
    };
    const filteredMembers = state.members.filter((m) =>
        `${m.name} ${m.email} ${roleName(m.role)} ${groupById(m.group_id)?.name}`
            .toLowerCase()
            .includes(query.toLowerCase()),
    );
    const total = usage.daily.reduce((n, r) => n + r.requests, 0);
    const failures = usage.daily.reduce((n, r) => n + r.errors, 0);
    const content = () => {
        if (currentPage === "overview")
            return (
                <>
                    <div className="briefing">
                        <Seal size={440} id={state.tenant_id} />
                        <div className="greeting">
                            {new Date().getHours() < 12
                                ? "Good morning"
                                : "Good afternoon"}
                            , {state.viewer.name.split(" ")[0]}.
                        </div>
                        <h1>
                            Your team has{" "}
                            <button
                                className="figure"
                                onClick={() => nav("models")}
                            >
                                {state.models.length} models
                            </button>{" "}
                            at hand.
                            <br />
                            {dirty
                                ? "A clear policy is your next step."
                                : "Their access is in good hands."}
                        </h1>
                        <p className="sub">
                            <button
                                className="figure"
                                onClick={() => nav("members")}
                            >
                                {fmt(
                                    state.members.filter(
                                        (m) => m.status === "active",
                                    ).length,
                                )}{" "}
                                active{" "}
                                {state.members.filter(
                                    (m) => m.status === "active",
                                ).length === 1
                                    ? "developer"
                                    : "developers"}
                            </button>{" "}
                            across{" "}
                            <button
                                className="figure"
                                onClick={() => nav("groups")}
                            >
                                {state.groups.length}{" "}
                                {state.groups.length === 1 ? "group" : "groups"}
                            </button>
                            .{" "}
                            {usage.status === "connected"
                                ? `${fmt(total)} gateway requests recorded over the past ${days} days.`
                                : "Connect your usage data to follow adoption."}{" "}
                            {openRequests.length
                                ? `${openRequests.length} access requests need a decision.`
                                : "No access requests are waiting."}
                        </p>
                        <button
                            className="button text"
                            onClick={() => nav(dirty ? "models" : "analytics")}
                        >
                            {dirty
                                ? "Review your access policy"
                                : "Explore usage"}
                            <ArrowRight size={16} />
                        </button>
                    </div>
                    <div className="stats">
                        <Stat
                            label="Gateway requests"
                            value={
                                usage.status === "connected" ? fmt(total) : "—"
                            }
                            detail={`Past ${days} days · ingestion delayed`}
                        />
                        <Stat
                            label="Active developers"
                            value={
                                usage.status === "connected"
                                    ? fmt(
                                          usage.people.filter(
                                              (p) => p.key !== "Unattributed",
                                          ).length,
                                      )
                                    : "—"
                            }
                            detail="Seen in gateway request logs"
                        />
                        <Stat
                            label="Successful requests"
                            value={
                                total
                                    ? `${(((total - failures) / total) * 100).toFixed(1)}%`
                                    : "—"
                            }
                            detail={
                                total
                                    ? `${fmt(failures)} requests returned an error`
                                    : "No requests in this period"
                            }
                        />
                        <Stat
                            label="Monthly token quota"
                            value={
                                state.settings.monthly_tokens
                                    ? compact(state.settings.monthly_tokens)
                                    : "Not set"
                            }
                            detail={
                                state.settings.monthly_tokens
                                    ? "Organization gateway limit"
                                    : "Set a limit in Budgets & limits"
                            }
                        />
                    </div>
                    <div className="grid">
                        <div>
                            <section className="section">
                                <div className="sectionhead">
                                    <h2>Developer activity</h2>
                                    <button
                                        className="button text"
                                        onClick={() => nav("analytics")}
                                    >
                                        View analytics
                                        <ArrowRight size={16} />
                                    </button>
                                </div>
                                {usageError ? (
                                    <Notice tone="error">
                                        {usageError}
                                        <button
                                            className="button text"
                                            onClick={() => loadUsage()}
                                        >
                                            Retry
                                        </button>
                                    </Notice>
                                ) : (
                                    <UsageChart usage={usage} />
                                )}
                            </section>
                            <section className="section">
                                <div className="sectionhead">
                                    <h2>Recent changes</h2>
                                    <button
                                        className="button text"
                                        onClick={() => nav("audit")}
                                    >
                                        Open audit log
                                    </button>
                                </div>
                                {state.audit.length ? (
                                    [...state.audit]
                                        .reverse()
                                        .slice(0, 4)
                                        .map((e) => (
                                            <div
                                                className="activity"
                                                key={e.id}
                                            >
                                                <button
                                                    className="linkbutton"
                                                    onClick={() =>
                                                        setPanel({
                                                            kind: "audit",
                                                            value: e,
                                                        })
                                                    }
                                                >
                                                    {e.actor} ·{" "}
                                                    {e.action.replaceAll(
                                                        ".",
                                                        " ",
                                                    )}
                                                </button>
                                                <small>
                                                    {e.target} · {date(e.at)}
                                                </small>
                                            </div>
                                        ))
                                ) : (
                                    <Empty title="A clean starting point">
                                        Changes to people, policies, and budgets
                                        will appear here with the person who
                                        made them.
                                    </Empty>
                                )}
                            </section>
                        </div>
                        <div>
                            <section className="section">
                                <div className="sectionhead">
                                    <h2>Needs your review</h2>
                                    <Status
                                        tone={reviewCount ? "warn" : "good"}
                                    >
                                        {reviewCount}{" "}
                                        {reviewCount === 1 ? "item" : "items"}
                                    </Status>
                                </div>
                                {dirty && (
                                    <div className="listrow">
                                        <div>
                                            <strong>
                                                Access policy is a draft
                                            </strong>
                                            <p>
                                                Review its effect before
                                                applying it.
                                            </p>
                                        </div>
                                        <button
                                            className="iconbutton"
                                            aria-label="Review draft"
                                            onClick={() =>
                                                setPanel({ kind: "publish" })
                                            }
                                        >
                                            <ArrowRight />
                                        </button>
                                    </div>
                                )}
                                {openRequests.slice(0, 3).map((r) => (
                                    <div className="listrow" key={r.id}>
                                        <div>
                                            <strong>{r.member_name}</strong>
                                            <p>
                                                Requested {modelLabel(r.model)}
                                            </p>
                                        </div>
                                        <button
                                            className="iconbutton"
                                            aria-label={`Review request from ${r.member_name}`}
                                            onClick={() => nav("requests")}
                                        >
                                            <ArrowRight />
                                        </button>
                                    </div>
                                ))}
                                {!state.settings.monthly_tokens && (
                                    <div className="listrow">
                                        <div>
                                            <strong>
                                                Choose a token budget
                                            </strong>
                                            <p>
                                                Give usage a predictable
                                                boundary.
                                            </p>
                                        </div>
                                        <button
                                            className="iconbutton"
                                            aria-label="Set budget"
                                            onClick={() => nav("budgets")}
                                        >
                                            <ArrowRight />
                                        </button>
                                    </div>
                                )}
                            </section>
                            <section className="section">
                                <h2>Organization health</h2>
                                <dl>
                                    <div className="health">
                                        <dt>Sign-in</dt>
                                        <dd>
                                            <Status tone="good">
                                                {boot.local
                                                    ? "Local session"
                                                    : "Microsoft Entra"}
                                            </Status>
                                        </dd>
                                    </div>
                                    <div className="health">
                                        <dt>Gateway policy</dt>
                                        <dd>
                                            <Status
                                                tone={dirty ? "warn" : "good"}
                                            >
                                                {dirty
                                                    ? "Draft changes"
                                                    : "Published"}
                                            </Status>
                                        </dd>
                                    </div>
                                    <div className="health">
                                        <dt>Usage reporting</dt>
                                        <dd>
                                            <Status
                                                tone={
                                                    usage.status === "connected"
                                                        ? "good"
                                                        : "warn"
                                                }
                                            >
                                                {usageLoading
                                                    ? "Loading…"
                                                    : usage.status ===
                                                        "connected"
                                                      ? "Connected"
                                                      : "Not connected"}
                                            </Status>
                                        </dd>
                                    </div>
                                    <div className="health">
                                        <dt>Portal conversation storage</dt>
                                        <dd>Not collected</dd>
                                    </div>
                                </dl>
                                <p className="footnote">
                                    This portal does not store conversation
                                    content. Review Azure diagnostic capture
                                    settings separately.
                                </p>
                            </section>
                        </div>
                    </div>
                </>
            );
        if (currentPage === "members")
            return (
                <>
                    <PageHead
                        title="Members"
                        description={`${fmt(state.members.length)} people in ${state.settings.name}. Access is tied to their Microsoft Entra identity.`}
                    >
                        <button
                            className="button"
                            onClick={() =>
                                exportCsv(
                                    "members.csv",
                                    filteredMembers.map((m) => ({
                                        name: m.name,
                                        email: m.email,
                                        role: m.role,
                                        status: m.status,
                                        group: groupById(m.group_id)?.name,
                                    })),
                                )
                            }
                        >
                            <DownloadSimple />
                            Export
                        </button>
                        {admin && (
                            <button
                                className="button"
                                onClick={() =>
                                    setPanel({ kind: "member_import" })
                                }
                            >
                                Import CSV
                            </button>
                        )}
                        {admin && (
                            <button
                                className="button primary"
                                onClick={() => setPanel({ kind: "member" })}
                            >
                                <Plus />
                                Add member
                            </button>
                        )}
                    </PageHead>
                    <div className="toolbar">
                        <input
                            aria-label="Search members"
                            placeholder="Search names, email, roles or groups…"
                            value={query}
                            onChange={(e) => search(e.target.value)}
                        />
                        <span className="muted">
                            {filteredMembers.length} results
                        </span>
                    </div>
                    <div className="sheet tablewrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Name</th>
                                    <th>Role</th>
                                    <th>Primary group</th>
                                    <th>Status</th>
                                    <th className="numeric">Monthly tokens</th>
                                    <th></th>
                                </tr>
                            </thead>
                            <tbody>
                                {filteredMembers
                                    .slice(tablePage * 50, (tablePage + 1) * 50)
                                    .map((m) => (
                                        <tr key={m.id}>
                                            <td>
                                                <div className="person">
                                                    <span className="avatar">
                                                        {initials(m.name)}
                                                    </span>
                                                    <div>
                                                        <button
                                                            className="linkbutton"
                                                            onClick={() =>
                                                                setPanel({
                                                                    kind: "member",
                                                                    value: m,
                                                                })
                                                            }
                                                        >
                                                            {m.name}
                                                        </button>
                                                        <small>{m.email}</small>
                                                    </div>
                                                </div>
                                            </td>
                                            <td>{roleName(m.role)}</td>
                                            <td>
                                                {groupById(m.group_id)?.name}
                                            </td>
                                            <td>
                                                <Status
                                                    tone={
                                                        m.status === "active"
                                                            ? "good"
                                                            : "warn"
                                                    }
                                                >
                                                    {m.status === "active"
                                                        ? "Active"
                                                        : "Suspended"}
                                                </Status>
                                            </td>
                                            <td className="numeric">
                                                {m.monthly_tokens
                                                    ? fmt(m.monthly_tokens)
                                                    : "No personal limit"}
                                            </td>
                                            <td>
                                                <button
                                                    className="iconbutton"
                                                    aria-label={`View ${m.name}`}
                                                    onClick={() =>
                                                        setPanel({
                                                            kind: "member",
                                                            value: m,
                                                        })
                                                    }
                                                >
                                                    <ArrowRight />
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                            </tbody>
                        </table>
                        {!filteredMembers.length && (
                            <Empty title="No matching members">
                                Try another name or clear your search.
                                <button
                                    className="button text"
                                    onClick={() => search("")}
                                >
                                    Clear search
                                </button>
                            </Empty>
                        )}
                    </div>
                    <Pager
                        count={filteredMembers.length}
                        page={tablePage}
                        onChange={setTablePage}
                    />
                </>
            );
        if (currentPage === "groups")
            return (
                <>
                    <PageHead
                        title="Groups"
                        description="Give each team the models and limits that fit their work."
                    >
                        {admin && (
                            <button
                                className="button primary"
                                onClick={() => setPanel({ kind: "group" })}
                            >
                                <Plus />
                                Create group
                            </button>
                        )}
                    </PageHead>
                    <div className="sheet tablewrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Group</th>
                                    <th className="numeric">Members</th>
                                    <th>Default model</th>
                                    <th>Models</th>
                                    <th className="numeric">Monthly tokens</th>
                                    <th></th>
                                </tr>
                            </thead>
                            <tbody>
                                {state.groups.map((g) => (
                                    <tr key={g.id}>
                                        <td>
                                            <button
                                                className="linkbutton"
                                                onClick={() =>
                                                    setPanel({
                                                        kind: "group",
                                                        value: g,
                                                    })
                                                }
                                            >
                                                {g.name}
                                            </button>
                                            <div className="muted">
                                                <small>{g.description}</small>
                                            </div>
                                        </td>
                                        <td className="numeric">
                                            {
                                                state.members.filter(
                                                    (m) => m.group_id === g.id,
                                                ).length
                                            }
                                        </td>
                                        <td>
                                            {modelLabel(g.default_model) ||
                                                "None"}
                                        </td>
                                        <td>{g.model_ids.length} allowed</td>
                                        <td className="numeric">
                                            {g.monthly_tokens
                                                ? fmt(g.monthly_tokens)
                                                : "Not set"}
                                        </td>
                                        <td>
                                            <button
                                                className="iconbutton"
                                                aria-label={`Edit ${g.name}`}
                                                onClick={() =>
                                                    setPanel({
                                                        kind: "group",
                                                        value: g,
                                                    })
                                                }
                                            >
                                                <ArrowRight />
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <Notice>
                        Groups here control access through this gateway.
                        Microsoft Entra remains the source of your directory
                        accounts and sign-in policies.
                    </Notice>
                </>
            );
        if (currentPage === "models")
            return (
                <>
                    <PageHead
                        title="Model access"
                        description="Model entitlements come from a developer’s primary group and any approved personal grants."
                    >
                        {admin && (
                            <button
                                className="button primary"
                                onClick={() => setPanel({ kind: "publish" })}
                            >
                                Review & publish
                                <ArrowRight />
                            </button>
                        )}
                    </PageHead>
                    <div className="sheet tablewrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>Model</th>
                                    <th>Family</th>
                                    <th>Group access</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody>
                                {state.models.map((m) => (
                                    <tr key={m.name}>
                                        <td>
                                            <strong>{m.labelOverride}</strong>
                                            <div className="mono muted">
                                                {m.name}
                                            </div>
                                        </td>
                                        <td
                                            style={{
                                                textTransform: "capitalize",
                                            }}
                                        >
                                            {m.anthropicFamilyTier}
                                        </td>
                                        <td>
                                            {state.groups
                                                .filter((g) =>
                                                    g.model_ids.includes(
                                                        m.name,
                                                    ),
                                                )
                                                .map((g) => (
                                                    <button
                                                        className="button text"
                                                        key={g.id}
                                                        onClick={() =>
                                                            setPanel({
                                                                kind: "group",
                                                                value: g,
                                                            })
                                                        }
                                                    >
                                                        {g.name}
                                                    </button>
                                                ))}
                                        </td>
                                        <td>
                                            <Status tone="good">
                                                Deployed
                                            </Status>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                    <section className="section" style={{ marginTop: 32 }}>
                        <div className="sectionhead">
                            <h2>Access by group</h2>
                            <span className="muted">
                                Open a group to change its entitlements
                            </span>
                        </div>
                        <div className="sheet tablewrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Group</th>
                                        {state.models.map((m) => (
                                            <th key={m.name}>
                                                {m.labelOverride}
                                            </th>
                                        ))}
                                    </tr>
                                </thead>
                                <tbody>
                                    {state.groups.map((g) => (
                                        <tr key={g.id}>
                                            <td>
                                                <button
                                                    className="linkbutton"
                                                    onClick={() =>
                                                        setPanel({
                                                            kind: "group",
                                                            value: g,
                                                        })
                                                    }
                                                >
                                                    {g.name}
                                                </button>
                                            </td>
                                            {state.models.map((m) => (
                                                <td key={m.name}>
                                                    {g.model_ids.includes(
                                                        m.name,
                                                    ) ? (
                                                        <span aria-label="Allowed">
                                                            <Check
                                                                size={18}
                                                                role="img"
                                                                aria-label="Allowed"
                                                                aria-hidden={
                                                                    false
                                                                }
                                                            />
                                                        </span>
                                                    ) : (
                                                        <span
                                                            className="muted"
                                                            aria-label="Not allowed"
                                                        >
                                                            —
                                                        </span>
                                                    )}
                                                </td>
                                            ))}
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </section>
                </>
            );
        if (currentPage === "roles")
            return (
                <>
                    <PageHead
                        title="Roles & permissions"
                        description="Administration roles apply to this console. Model access is governed separately by groups."
                    />
                    <div className="sheet tablewrap">
                        <div className="rolegrid">
                            <div className="heading">Permission</div>
                            {[
                                "Owner",
                                "Administrator",
                                "Billing",
                                "Auditor",
                                "Developer",
                            ].map((r) => (
                                <div className="heading" key={r}>
                                    {r}
                                </div>
                            ))}
                            {[
                                ["View organization", 1, 1, 1, 1, 0],
                                ["Manage members", 1, 1, 0, 0, 0],
                                ["Manage ownership", 1, 0, 0, 0, 0],
                                ["Publish policy", 1, 1, 0, 0, 0],
                                ["Edit token limits", 1, 1, 0, 0, 0],
                                ["Edit dollar budget", 1, 1, 1, 0, 0],
                                ["Read usage & audit", 1, 1, 1, 1, 0],
                                ["Request model access", 1, 1, 1, 1, 1],
                                ["Download own setup", 1, 1, 1, 1, 1],
                            ].map(([label, ...values]) => (
                                <div
                                    key={String(label)}
                                    style={{ display: "contents" }}
                                >
                                    <div>{label}</div>
                                    {values.map((v, i) => (
                                        <div key={i}>
                                            {v ? (
                                                <Check
                                                    role="img"
                                                    aria-label="Allowed"
                                                    aria-hidden={false}
                                                />
                                            ) : (
                                                <span aria-label="Not allowed">
                                                    —
                                                </span>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            ))}
                        </div>
                    </div>
                    <Notice>
                        These permissions are enforced by the API. Only an owner
                        can appoint another owner; no one can suspend themselves
                        or remove the last active owner.
                    </Notice>
                </>
            );
        if (currentPage === "budgets" || currentPage === "settings")
            return (
                <>
                    <PageHead
                        title={
                            currentPage === "budgets"
                                ? "Budgets & limits"
                                : "Organization settings"
                        }
                        description={
                            currentPage === "budgets"
                                ? "Set token boundaries at the gateway and a dollar budget for planning."
                                : "Your customer-owned control plane, connected to Microsoft Entra and Azure."
                        }
                    />
                    {currentPage === "budgets" && (
                        <Notice>
                            Token quotas apply after publishing. APIM estimates
                            streamed tokens and concurrent requests can
                            overshoot. Dollar budgets are planning thresholds,
                            not hard Azure billing caps.
                        </Notice>
                    )}
                    <SettingsForm
                        key={state.revision}
                        settings={state.settings}
                        admin={admin}
                        finance={finance}
                        busy={busy}
                        save={(v) => save("settings", v)}
                        organization={currentPage === "settings"}
                    />
                    {currentPage === "settings" && (
                        <div className="settings" style={{ marginTop: 32 }}>
                            <h2>Connected services</h2>
                            <dl>
                                <div className="health">
                                    <dt>Sign-in provider</dt>
                                    <dd>Microsoft Entra ID · single tenant</dd>
                                </div>
                                <div className="health">
                                    <dt>Directory lifecycle</dt>
                                    <dd>
                                        <a
                                            href="https://entra.microsoft.com"
                                            target="_blank"
                                            rel="noreferrer"
                                        >
                                            Manage in Entra <ArrowSquareOut />
                                        </a>
                                    </dd>
                                </div>
                                <div className="health">
                                    <dt>Gateway</dt>
                                    <dd className="mono">
                                        {state.gateway_url}
                                    </dd>
                                </div>
                                <div className="health">
                                    <dt>Authentication</dt>
                                    <dd>
                                        Entra access tokens · no shared API key
                                    </dd>
                                </div>
                                <div className="health">
                                    <dt>Data captured</dt>
                                    <dd>
                                        Usage metadata and administrator changes
                                    </dd>
                                </div>
                            </dl>
                            <Notice>
                                Conversation retention, tool restrictions,
                                device compliance, and invoices are managed in
                                Claude, your device management platform, and
                                Azure. This portal does not collect chat content
                                or issue compliance certifications.
                            </Notice>
                        </div>
                    )}
                    {currentPage === "budgets" && (
                        <section className="section" style={{ marginTop: 36 }}>
                            <h2>Group limits</h2>
                            <div
                                className="sheet tablewrap"
                                style={{ marginTop: 16 }}
                            >
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Group</th>
                                            <th className="numeric">
                                                Planning budget / month
                                            </th>
                                            <th className="numeric">
                                                Token quota / month
                                            </th>
                                            <th></th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {state.groups.map((g) => (
                                            <tr key={g.id}>
                                                <td>{g.name}</td>
                                                <td className="numeric">
                                                    {g.budget_usd
                                                        ? `$${fmt(g.budget_usd)}`
                                                        : "Not set"}
                                                </td>
                                                <td className="numeric">
                                                    {g.monthly_tokens
                                                        ? fmt(g.monthly_tokens)
                                                        : "Not set"}
                                                </td>
                                                <td>
                                                    {admin && (
                                                        <button
                                                            className="button text"
                                                            onClick={() =>
                                                                setPanel({
                                                                    kind: "group",
                                                                    value: g,
                                                                })
                                                            }
                                                        >
                                                            Edit group
                                                        </button>
                                                    )}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            <p className="footnote">
                                Azure Cost Management remains the authoritative
                                source for billed spend. Set model planning
                                rates below to estimate token usage costs.
                            </p>
                        </section>
                    )}
                    {currentPage === "budgets" && (
                        <Spend
                            state={state}
                            usage={usage}
                            finance={finance}
                            busy={busy}
                            save={(value) => save("pricing", value)}
                        />
                    )}
                </>
            );
        if (currentPage === "costs")
            return <CostAnalysis state={state} busy={busy} save={save} />;
        if (currentPage === "analytics")
            return (
                <>
                    <PageHead
                        title="Usage & analytics"
                        description="Operational activity from Application Insights. Prompt and response content is never shown."
                    >
                        <select
                            aria-label="Usage period"
                            value={days}
                            onChange={(e) => {
                                setDays(Number(e.target.value));
                                loadUsage(Number(e.target.value));
                            }}
                        >
                            {[7, 30, 90].map((d) => (
                                <option key={d} value={d}>
                                    Past {d} days
                                </option>
                            ))}
                        </select>
                        <button className="button" onClick={() => loadUsage()}>
                            <ArrowClockwise />
                            Refresh
                        </button>
                        <button
                            className="button"
                            disabled={
                                usageLoading ||
                                usage.status !== "connected" ||
                                !usage.requests.length
                            }
                            onClick={() =>
                                exportCsv(
                                    "gateway-requests.csv",
                                    usage.requests,
                                )
                            }
                        >
                            <DownloadSimple />
                            Export
                        </button>
                    </PageHead>
                    {usageLoading && (
                        <Notice>
                            Updating usage… Previous results remain visible
                            until this period finishes loading.
                        </Notice>
                    )}
                    {usageError ? (
                        <Notice tone="error">{usageError}</Notice>
                    ) : (
                        <>
                            <div className="stats">
                                <Stat
                                    label="Requests"
                                    value={fmt(total)}
                                    detail={`Past ${days} days`}
                                />
                                <Stat
                                    label="People seen"
                                    value={fmt(
                                        usage.people.filter(
                                            (p) => p.key !== "Unattributed",
                                        ).length,
                                    )}
                                    detail="Distinct Entra identities"
                                />
                                <Stat
                                    label="Errors"
                                    value={fmt(failures)}
                                    detail="Includes authorization and rate limits"
                                />
                                <Stat
                                    label="Average duration"
                                    value={
                                        total
                                            ? `${Math.round(usage.daily.reduce((n, r) => n + r.duration_ms, 0) / total)} ms`
                                            : "—"
                                    }
                                    detail="Gateway request duration"
                                />
                            </div>
                            <UsageChart usage={usage} />
                            <div
                                className="sectionhead"
                                style={{ marginTop: 32 }}
                            >
                                <h2>Usage by model</h2>
                            </div>
                            <div className="sheet tablewrap">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Model</th>
                                            <th className="numeric">
                                                Requests
                                            </th>
                                            <th className="numeric">Errors</th>
                                            <th className="numeric">
                                                Average duration
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {usage.models.map((m) => (
                                            <tr key={m.key}>
                                                <td>{modelLabel(m.key)}</td>
                                                <td className="numeric">
                                                    {fmt(m.requests)}
                                                </td>
                                                <td className="numeric">
                                                    {fmt(m.errors)}
                                                </td>
                                                <td className="numeric">
                                                    {Math.round(
                                                        m.duration_ms /
                                                            m.requests,
                                                    )}{" "}
                                                    ms
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                                {!usage.models.length && (
                                    <Empty title="Your first request starts the story">
                                        Once developers use the gateway, model
                                        activity will appear here after
                                        telemetry ingestion.
                                    </Empty>
                                )}
                            </div>
                            <div
                                className="sectionhead"
                                style={{ marginTop: 32 }}
                            >
                                <h2>Recent gateway requests</h2>
                            </div>
                            <div className="sheet tablewrap">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>When</th>
                                            <th>Model</th>
                                            <th>Developer</th>
                                            <th>Result</th>
                                            <th className="numeric">
                                                Duration
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {usage.requests
                                            .slice(0, 50)
                                            .map((r, i) => (
                                                <tr key={i}>
                                                    <td>{date(r.timestamp)}</td>
                                                    <td>
                                                        {modelLabel(r.model) ||
                                                            "—"}
                                                    </td>
                                                    <td>
                                                        {memberById(
                                                            r.principal_id,
                                                        )?.name ||
                                                            r.principal_id?.slice(
                                                                0,
                                                                8,
                                                            ) ||
                                                            "Unattributed"}
                                                    </td>
                                                    <td>
                                                        <Status
                                                            tone={
                                                                String(
                                                                    r.resultCode,
                                                                ).startsWith(
                                                                    "2",
                                                                )
                                                                    ? "good"
                                                                    : "warn"
                                                            }
                                                        >
                                                            {r.resultCode}
                                                        </Status>
                                                    </td>
                                                    <td className="numeric">
                                                        {Math.round(r.duration)}{" "}
                                                        ms
                                                    </td>
                                                </tr>
                                            ))}
                                    </tbody>
                                </table>
                            </div>
                            <p className="footnote">
                                {usage.note}
                                {usage.truncated
                                    ? " This view reached its 10,000-request limit; narrow the date range for exact counts."
                                    : ""}
                            </p>
                        </>
                    )}
                </>
            );
        if (currentPage === "audit") {
            const events = [...state.audit]
                .reverse()
                .filter((e) =>
                    `${e.actor} ${e.action} ${e.target}`
                        .toLowerCase()
                        .includes(query.toLowerCase()),
                );
            return (
                <>
                    <PageHead
                        title="Audit log"
                        description="A record of every saved administrative change, including drafts and gateway publications."
                    >
                        <button
                            className="button"
                            disabled={!events.length}
                            onClick={() =>
                                exportCsv(
                                    "audit-log.csv",
                                    events.map((e) => ({
                                        at: e.at,
                                        actor: e.actor,
                                        action: e.action,
                                        target: e.target,
                                        before: JSON.stringify(e.before),
                                        after: JSON.stringify(e.after),
                                    })),
                                )
                            }
                        >
                            <DownloadSimple />
                            Export view
                        </button>
                    </PageHead>
                    <div className="toolbar">
                        <input
                            aria-label="Search audit events"
                            value={query}
                            onChange={(e) => search(e.target.value)}
                            placeholder="Search people, actions or targets…"
                        />
                        <span className="muted">{events.length} events</span>
                    </div>
                    <div className="sheet tablewrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>When</th>
                                    <th>Who</th>
                                    <th>Action</th>
                                    <th>Target</th>
                                    <th></th>
                                </tr>
                            </thead>
                            <tbody>
                                {events
                                    .slice(tablePage * 50, (tablePage + 1) * 50)
                                    .map((e) => (
                                        <tr key={e.id}>
                                            <td>{date(e.at)}</td>
                                            <td>{e.actor}</td>
                                            <td>
                                                {e.action.replaceAll(".", " ")}
                                            </td>
                                            <td>{e.target}</td>
                                            <td>
                                                <button
                                                    className="iconbutton"
                                                    aria-label={`Inspect ${e.action}`}
                                                    onClick={() =>
                                                        setPanel({
                                                            kind: "audit",
                                                            value: e,
                                                        })
                                                    }
                                                >
                                                    <ArrowRight />
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                            </tbody>
                        </table>
                        {!events.length && (
                            <Empty
                                title={
                                    query
                                        ? "No matching events"
                                        : "Your audit trail starts here"
                                }
                            >
                                {query
                                    ? "Try a different search."
                                    : "Saved changes will include who made them, when, and the before-and-after values."}
                            </Empty>
                        )}
                    </div>
                    <Pager
                        count={events.length}
                        page={tablePage}
                        onChange={setTablePage}
                    />
                    <p className="footnote">
                        Times use your browser’s local timezone. JSON details
                        retain UTC. Azure Blob versioning protects prior state
                        versions; this is not an immutable compliance archive.
                    </p>
                </>
            );
        }
        if (currentPage === "requests")
            return (
                <>
                    <PageHead
                        title="Requests"
                        description="Review model access requests. Approved changes take effect when the gateway policy is published."
                    >
                        <button
                            className="button primary"
                            onClick={() => setPanel({ kind: "request" })}
                        >
                            <Plus />
                            Request model access
                        </button>
                    </PageHead>
                    <div className="sheet">
                        {state.requests.length ? (
                            [...state.requests].reverse().map((r) => (
                                <div className="settingrow" key={r.id}>
                                    <div>
                                        <strong>
                                            {r.member_name} requested{" "}
                                            {modelLabel(r.model)}
                                        </strong>
                                        <p>{r.reason}</p>
                                        <small className="muted">
                                            {date(r.at)}
                                            {r.note && ` · ${r.note}`}
                                        </small>
                                    </div>
                                    <div className="actions">
                                        {r.status === "open" && admin ? (
                                            <>
                                                <button
                                                    className="button"
                                                    disabled={busy}
                                                    onClick={() =>
                                                        save("decision", {
                                                            id: r.id,
                                                            approve: false,
                                                            note: "",
                                                        })
                                                    }
                                                >
                                                    Decline
                                                </button>
                                                <button
                                                    className="button primary"
                                                    disabled={busy}
                                                    onClick={() =>
                                                        save("decision", {
                                                            id: r.id,
                                                            approve: true,
                                                            note: "",
                                                        })
                                                    }
                                                >
                                                    Approve access
                                                </button>
                                            </>
                                        ) : (
                                            <Status
                                                tone={
                                                    r.status === "approved"
                                                        ? "good"
                                                        : r.status === "open"
                                                          ? "warn"
                                                          : "neutral"
                                                }
                                            >
                                                {r.status}
                                            </Status>
                                        )}
                                    </div>
                                </div>
                            ))
                        ) : (
                            <Empty title="Nothing waiting for a decision">
                                Developers can request a model here. Their
                                administrator can approve access and review the
                                policy before publishing.
                            </Empty>
                        )}
                    </div>
                </>
            );
        if (currentPage === "onboarding")
            return (
                <>
                    <PageHead
                        title="Developer onboarding"
                        description="A repeatable path from an organization account to Claude Desktop and Claude Code."
                    />
                    <div className="settings">
                        <Notice>
                            The client profile contains connection settings, not
                            credentials. Every developer signs in with their own
                            Microsoft Entra account.
                        </Notice>
                        <div className="onboardingstep">
                            <span className="stepnumber">1</span>
                            <div>
                                <h2>Grant access</h2>
                                <p>
                                    Add the developer’s Entra object ID, assign
                                    their primary group, and publish the
                                    reviewed gateway policy.
                                </p>
                                {admin && (
                                    <button
                                        className="button"
                                        onClick={() => nav("members")}
                                    >
                                        Manage members
                                        <ArrowRight />
                                    </button>
                                )}
                            </div>
                        </div>
                        <div className="onboardingstep">
                            <span className="stepnumber">2</span>
                            <div>
                                <h2>Export the client profile</h2>
                                <p>
                                    The profile lists only the models allowed
                                    for the selected group. The gateway still
                                    enforces access, even if a user edits their
                                    local settings.
                                </p>
                                <div className="actions">
                                    {admin ? (
                                        state.groups.map((g) => (
                                            <button
                                                key={g.id}
                                                className="button"
                                                onClick={() =>
                                                    downloadProfile(g.id)
                                                }
                                            >
                                                <DownloadSimple />
                                                {g.name}
                                            </button>
                                        ))
                                    ) : (
                                        <button
                                            className="button"
                                            onClick={() => downloadProfile()}
                                        >
                                            <DownloadSimple />
                                            My client profile
                                        </button>
                                    )}
                                </div>
                            </div>
                        </div>
                        <div className="onboardingstep">
                            <span className="stepnumber">3</span>
                            <div>
                                <h2>Prepare the Mac installer</h2>
                                <p>
                                    Use the repository’s bundle builder with the
                                    exported profile. Send the resulting ZIP
                                    through your organization’s software
                                    distribution channel.
                                </p>
                                <pre className="codewell">
                                    {
                                        "uv run python scripts/build_client_bundle.py \\\n  --profile /path/to/client.json \\\n  --output dist/claude-foundry-macos.zip"
                                    }
                                </pre>
                                <a
                                    href="https://github.com/msftse-org/claude-codex-foundry/blob/main/docs/macos.md"
                                    target="_blank"
                                    rel="noreferrer"
                                >
                                    Open the installation guide{" "}
                                    <ArrowSquareOut />
                                </a>
                            </div>
                        </div>
                        <div className="onboardingstep">
                            <span className="stepnumber">4</span>
                            <div>
                                <h2>Sign in and start working</h2>
                                <p>
                                    The developer opens Install.command, signs
                                    in to Microsoft, and restarts Claude. The
                                    installer preserves the previous
                                    configuration and provides a restore
                                    command.
                                </p>
                                <p className="footnote">
                                    This preview does not push device policies
                                    or track installation status. Use your MDM
                                    platform for managed rollout.
                                </p>
                            </div>
                        </div>
                    </div>
                </>
            );
        return (
            <PageHead
                title="Page not found"
                description="Choose a page from the navigation."
            />
        );
    };
    return (
        <>
            <a className="skip" href="#main">
                Skip to content
            </a>
            <div className="app">
                <aside
                    id="primary-navigation"
                    className={`sidebar ${navOpen ? "open" : ""}`}
                    aria-label="Primary navigation"
                >
                    <div className="brand">
                        <Seal />
                        <div>
                            <strong>{state.settings.name}</strong>
                            <small>Claude & Codex Admin</small>
                        </div>
                    </div>
                    <nav>
                        {filteredPages.map((p) => (
                            <div key={p.id}>
                                {p.section && (
                                    <div className="navlabel">{p.section}</div>
                                )}
                                <button
                                    className={`navitem ${currentPage === p.id ? "active" : ""}`}
                                    aria-current={
                                        currentPage === p.id
                                            ? "page"
                                            : undefined
                                    }
                                    onClick={() => nav(p.id)}
                                >
                                    <p.icon size={20} />
                                    {p.name}
                                    {p.id === "requests" &&
                                        openRequests.length > 0 && (
                                            <span className="count">
                                                {openRequests.length}
                                            </span>
                                        )}
                                </button>
                            </div>
                        ))}
                    </nav>
                    <div className="sidebarfooter">
                        <a
                            href="https://github.com/msftse-org/claude-codex-foundry"
                            target="_blank"
                            rel="noreferrer"
                        >
                            Customer-owned on Azure
                            <ArrowSquareOut size={13} />
                        </a>
                        <div style={{ marginTop: 8 }}>
                            Preview · validation in progress
                        </div>
                    </div>
                </aside>
                <div className="workspace">
                    <header className="topbar">
                        <div className="left">
                            <button
                                className="iconbutton mobiletoggle"
                                aria-label="Toggle navigation"
                                aria-expanded={navOpen}
                                aria-controls="primary-navigation"
                                onClick={() => setNavOpen(!navOpen)}
                            >
                                <List size={22} />
                            </button>
                            <button
                                className="searchjump"
                                aria-label="Search pages and people"
                                onClick={() => setPanel({ kind: "command" })}
                            >
                                <MagnifyingGlass size={18} />
                                <span>Search or jump to…</span>
                                <kbd>⌘ K</kbd>
                            </button>
                        </div>
                        <div className="right">
                            <span className="preview">Development preview</span>
                            <button
                                className="theme"
                                aria-label={`Theme: ${theme}. Change theme`}
                                title={`Theme: ${theme}`}
                                onClick={() =>
                                    setTheme(
                                        theme === "system"
                                            ? "light"
                                            : theme === "light"
                                              ? "dark"
                                              : "system",
                                    )
                                }
                            >
                                {theme === "dark" ? (
                                    <Moon size={19} />
                                ) : theme === "light" ? (
                                    <Sun size={19} />
                                ) : (
                                    <Desktop size={19} />
                                )}
                            </button>
                            <div className="user">
                                <span className="avatar">
                                    {initials(state.viewer.name)}
                                </span>
                                <div>
                                    {state.viewer.name}
                                    <small>{roleName(state.viewer.role)}</small>
                                </div>
                            </div>
                            {!boot.local && (
                                <button
                                    className="iconbutton"
                                    aria-label="Sign out"
                                    onClick={() => signOut()}
                                >
                                    <SignOut size={18} />
                                </button>
                            )}
                        </div>
                    </header>
                    <main id="main" tabIndex={-1} className="content">
                        {boot.local && (
                            <Notice>
                                Local administrator session. This development
                                server can manage the configured preview
                                gateway; keep it bound to localhost.
                            </Notice>
                        )}
                        {state.publish_status === "failed" && (
                            <Notice tone="error">
                                Gateway publication failed. The draft is
                                retained. Review the policy and Azure
                                permissions before retrying.
                            </Notice>
                        )}
                        {!admin && !developer && (
                            <Notice>
                                {state.viewer.role === "billing"
                                    ? "You can edit the dollar planning budget and view operational data. Access and token controls require an owner or administrator."
                                    : "You have read-only access. Ask an owner or administrator to change a policy."}
                            </Notice>
                        )}
                        {content()}
                    </main>
                </div>
            </div>
            {panel?.kind === "member" && (
                <MemberEditor
                    state={state}
                    member={panel.value}
                    editable={admin}
                    busy={busy}
                    close={() => setPanel(undefined)}
                    save={async (v) => {
                        if (await save("member", v)) setPanel(undefined);
                    }}
                />
            )}
            {panel?.kind === "member_import" && (
                <MemberImport
                    state={state}
                    close={() => setPanel(undefined)}
                    busy={busy}
                    save={(value) => save("member_import", value)}
                />
            )}
            {panel?.kind === "group" && (
                <GroupEditor
                    state={state}
                    group={panel.value}
                    editable={admin}
                    busy={busy}
                    close={() => setPanel(undefined)}
                    save={async (v) => {
                        if (await save("group", v)) setPanel(undefined);
                    }}
                />
            )}
            {panel?.kind === "publish" && (
                <PublishDialog
                    state={state}
                    editable={admin}
                    close={() => setPanel(undefined)}
                    completed={async () => {
                        await load();
                        setPanel(undefined);
                        notify(
                            "Gateway policy published. Azure may take a moment to propagate it.",
                        );
                    }}
                />
            )}
            {panel?.kind === "audit" && (
                <Drawer
                    title="Change details"
                    description={`${panel.value.actor} · ${date(panel.value.at)}`}
                    onClose={() => setPanel(undefined)}
                >
                    <div className="drawer-scroll">
                        <pre className="codewell">
                            {JSON.stringify(panel.value, null, 2)}
                        </pre>
                        <button
                            className="button"
                            onClick={() =>
                                download("audit-event.json", panel.value)
                            }
                        >
                            <DownloadSimple />
                            Download JSON
                        </button>
                    </div>
                </Drawer>
            )}
            {panel?.kind === "command" && (
                <Command
                    state={state}
                    pages={filteredPages}
                    close={() => setPanel(undefined)}
                    navigate={nav}
                />
            )}
            {panel?.kind === "request" && (
                <Drawer
                    busy={busy}
                    title="Request model access"
                    description="Your administrator will review this request. Approval becomes active after gateway publication."
                    onClose={() => setPanel(undefined)}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            const f = new FormData(e.currentTarget);
                            if (
                                await save("request", {
                                    model: f.get("model"),
                                    reason: f.get("reason"),
                                })
                            )
                                setPanel(undefined);
                        }}
                    >
                        <div className="drawer-scroll">
                            <Field label="Model">
                                <select name="model">
                                    {state.models.map((m) => (
                                        <option key={m.name} value={m.name}>
                                            {m.labelOverride}
                                        </option>
                                    ))}
                                </select>
                            </Field>
                            <Field label="Why do you need this model?">
                                <textarea
                                    name="reason"
                                    required
                                    minLength={5}
                                    maxLength={1000}
                                />
                            </Field>
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={busy} />
                            <button className="button primary" disabled={busy}>
                                Send access request
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
            {toast && (
                <div
                    className={`toast ${toast.error ? "error" : ""}`}
                    role={toast.error ? "alert" : "status"}
                >
                    {toast.error ? <InfoIcon /> : <CheckCircle size={20} />}
                    <span>{toast.message}</span>
                    <button
                        className="iconbutton"
                        aria-label="Dismiss notification"
                        onClick={() => setToast(undefined)}
                    >
                        <X />
                    </button>
                </div>
            )}
        </>
    );
}
function initials(name: string) {
    return name
        .split(" ")
        .slice(0, 2)
        .map((s) => s[0])
        .join("")
        .toUpperCase();
}
function InfoIcon() {
    return <ShieldCheck size={20} />;
}
function Stat({
    label,
    value,
    detail,
}: {
    label: string;
    value: string;
    detail: string;
}) {
    return (
        <div className="stat">
            <span>{label}</span>
            <strong>{value}</strong>
            <small>{detail}</small>
        </div>
    );
}
createRoot(document.getElementById("root")!).render(<App />);
