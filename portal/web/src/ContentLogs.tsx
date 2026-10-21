import { useEffect, useState } from "react";
import {
    ArrowClockwise,
    ArrowRight,
    LockKey,
    MagnifyingGlass,
} from "@phosphor-icons/react";
import { api } from "./api";
import { formatCaptured } from "./content-logs-format";
import type { Member } from "./types";
import { date, Drawer, Empty, Field, Notice, PageHead } from "./components";
import "./content-logs.css";

type Config = {
    configured: boolean;
    capture_enabled: boolean;
    api_ids: string[];
    retention_days: number;
    max_message_bytes: number;
};
type Entry = {
    correlation_id: string;
    timestamp: string;
    api_id: string;
    model: string;
    model_name: string;
    status: number;
    has_request: boolean;
    has_response: boolean;
    chunks: number;
    principal_id: string;
};
type Result = { items: Entry[]; has_more: boolean };
type Detail = {
    correlation_id: string;
    chunks: { sequence: number; request: unknown; response: unknown }[];
    note: string;
    max_message_bytes: number;
};
const localDate = (d: Date) =>
    new Date(d.getTime() - d.getTimezoneOffset() * 60_000)
        .toISOString()
        .slice(0, 16);

function ContentDrawer({
    entry,
    onClose,
    onAudit,
}: {
    entry: Entry;
    onClose: () => void;
    onAudit: () => void;
}) {
    const [reason, setReason] = useState("");
    const [detail, setDetail] = useState<Detail>();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [tab, setTab] = useState<"request" | "response" | "raw">("request");
    async function reveal() {
        setBusy(true);
        setError("");
        try {
            setDetail(
                await api<Detail>("/api/content-logs/read", {
                    method: "POST",
                    body: JSON.stringify({
                        correlation_id: entry.correlation_id,
                        reason: reason.trim(),
                    }),
                }),
            );
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
            onAudit();
        }
    }
    const fragments =
        detail?.chunks.filter(
            (c) =>
                tab === "raw" ||
                (c[tab] !== null &&
                    c[tab] !== "" &&
                    JSON.stringify(c[tab]) !== "[]"),
        ) || [];
    return (
        <Drawer
            title="Captured prompt & response"
            description={`${entry.model || entry.model_name} · ${date(entry.timestamp)}`}
            onClose={onClose}
            busy={busy}
            protect={false}
        >
            <div className="content-log-detail">
                <dl className="content-log-meta">
                    <div>
                        <dt>API</dt>
                        <dd>{entry.api_id}</dd>
                    </div>
                    <div>
                        <dt>Request ID</dt>
                        <dd>{entry.correlation_id}</dd>
                    </div>
                    <div>
                        <dt>Developer ID</dt>
                        <dd>{entry.principal_id || "Unattributed"}</dd>
                    </div>
                    <div>
                        <dt>HTTP status</dt>
                        <dd>{entry.status}</dd>
                    </div>
                </dl>
                {error && <Notice tone="error">{error}</Notice>}
                {!detail ? (
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            void reveal();
                        }}
                    >
                        <div className="content-log-lock">
                            <LockKey size={28} />
                            <h3>Open sensitive content</h3>
                            <p>
                                This can include developer prompts, source code,
                                conversation context, and model output. Your
                                name, request ID, time, and access reason will
                                be recorded in the audit log.
                            </p>
                        </div>
                        <Field label="Reason for access">
                            <textarea
                                value={reason}
                                onChange={(e) => setReason(e.target.value)}
                                minLength={5}
                                maxLength={300}
                                required
                                placeholder="For example: investigating a reported response error. Do not include secrets."
                            />
                        </Field>
                        <button
                            className="button primary"
                            disabled={busy || reason.trim().length < 5}
                        >
                            {busy ? "Opening…" : "Record access & open content"}
                        </button>
                    </form>
                ) : (
                    <>
                        <div
                            className="content-log-tabs"
                            aria-label="Captured content"
                        >
                            {(["request", "response", "raw"] as const).map(
                                (t) => (
                                    <button
                                        key={t}
                                        className={tab === t ? "active" : ""}
                                        aria-pressed={tab === t}
                                        onClick={() => setTab(t)}
                                    >
                                        {t === "request"
                                            ? "Prompt"
                                            : t === "response"
                                              ? "Response"
                                              : "Raw record"}
                                    </button>
                                ),
                            )}
                        </div>
                        <p className="muted">
                            Native captured messages, up to{" "}
                            {Math.round(detail.max_message_bytes / 1024)} KiB
                            per direction. Fragments remain separate so missing
                            or truncated content is not hidden.
                        </p>
                        {!fragments.length && (
                            <Empty title="No captured content in this direction">
                                Capture may have been disabled, the stream
                                interrupted, or ingestion may still be in
                                progress.
                            </Empty>
                        )}
                        {fragments.map((chunk, i) => (
                            <section
                                className="content-log-fragment"
                                key={`${chunk.sequence}-${i}`}
                            >
                                <span className="eyebrow">
                                    Fragment {chunk.sequence}
                                </span>
                                <pre tabIndex={0}>
                                    {tab === "raw"
                                        ? JSON.stringify(chunk, null, 2)
                                        : formatCaptured(chunk[tab])}
                                </pre>
                            </section>
                        ))}
                        <p className="muted">{detail.note}</p>
                    </>
                )}
            </div>
        </Drawer>
    );
}

export function ContentLogs({
    onAudit,
    members,
}: {
    onAudit: () => void;
    members: Member[];
}) {
    const [config, setConfig] = useState<Config>();
    const [result, setResult] = useState<Result>();
    const [start, setStart] = useState(() =>
        localDate(new Date(Date.now() - 86_400_000)),
    );
    const [end, setEnd] = useState(() =>
        localDate(new Date(Date.now() + 60_000)),
    );
    const [apiId, setApiId] = useState("");
    const [model, setModel] = useState("");
    const [requestId, setRequestId] = useState("");
    const [principalId, setPrincipalId] = useState("");
    const [selected, setSelected] = useState<Entry>();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [searched, setSearched] = useState(false);
    useEffect(() => {
        api<Config>("/api/content-logs/config")
            .then(setConfig)
            .catch((e) => setError(e.message));
    }, []);
    async function search(more = false) {
        setBusy(true);
        setError("");
        try {
            const last = more ? result?.items.at(-1) : undefined;
            const query = {
                start: new Date(start).toISOString(),
                end: new Date(end).toISOString(),
                api_id: apiId,
                model: model.trim(),
                correlation_id: requestId.trim() || null,
                principal_id: principalId.trim() || null,
                before: last?.timestamp || null,
                before_id: last?.correlation_id || null,
            };
            const next = await api<Result>("/api/content-logs/search", {
                method: "POST",
                body: JSON.stringify(query),
            });
            setResult({
                ...next,
                items: more
                    ? [...(result?.items || []), ...next.items]
                    : next.items,
            });
            setSearched(true);
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    }
    function changeFilter(change: () => void) {
        change();
        setResult(undefined);
        setSearched(false);
    }
    return (
        <div className="content-logs">
            <PageHead
                title="Prompt & response logs"
                description="Inspect captured inference messages across Claude and Codex. Content access is restricted and audited."
            />
            {error && <Notice tone="error">{error}</Notice>}
            {!config ? (
                <p className="muted">Loading logging configuration…</p>
            ) : !config.configured ? (
                <Empty title="Content capture is not configured">
                    A platform administrator can enable the optional
                    content-logging deployment and connect its dedicated
                    workspace. Existing usage metrics do not contain prompts or
                    responses.
                </Empty>
            ) : (
                <>
                    <div className="content-log-status">
                        <span
                            className={`status ${config.capture_enabled ? "active" : ""}`}
                        >
                            {config.capture_enabled
                                ? "Capture enabled"
                                : "Capture paused"}
                        </span>
                        <span>
                            {config.retention_days}-day configured retention
                        </span>
                        <span>
                            <LockKey size={14} /> Owners & administrators
                        </span>
                    </div>
                    <form
                        className="content-log-filters"
                        onSubmit={(e) => {
                            e.preventDefault();
                            void search();
                        }}
                    >
                        <Field label="From (local time)">
                            <input
                                disabled={busy}
                                type="datetime-local"
                                required
                                value={start}
                                onChange={(e) =>
                                    changeFilter(() => setStart(e.target.value))
                                }
                            />
                        </Field>
                        <Field label="To (local time)">
                            <input
                                disabled={busy}
                                type="datetime-local"
                                required
                                value={end}
                                onChange={(e) =>
                                    changeFilter(() => setEnd(e.target.value))
                                }
                            />
                        </Field>
                        <Field label="Client API">
                            <select
                                disabled={busy}
                                value={apiId}
                                onChange={(e) =>
                                    changeFilter(() => setApiId(e.target.value))
                                }
                            >
                                <option value="">All configured APIs</option>
                                {config.api_ids.map((id) => (
                                    <option key={id} value={id}>
                                        {id}
                                    </option>
                                ))}
                            </select>
                        </Field>
                        <Field label="Developer object ID">
                            <input
                                disabled={busy}
                                placeholder="All developers"
                                value={principalId}
                                onChange={(e) =>
                                    changeFilter(() =>
                                        setPrincipalId(e.target.value),
                                    )
                                }
                            />
                        </Field>
                        <Field label="Model deployment">
                            <input
                                disabled={busy}
                                placeholder="All models"
                                value={model}
                                maxLength={128}
                                onChange={(e) =>
                                    changeFilter(() => setModel(e.target.value))
                                }
                            />
                        </Field>
                        <Field label="Request ID">
                            <input
                                disabled={busy}
                                placeholder="Optional correlation UUID"
                                value={requestId}
                                onChange={(e) =>
                                    changeFilter(() =>
                                        setRequestId(e.target.value),
                                    )
                                }
                            />
                        </Field>
                        <button className="button primary" disabled={busy}>
                            <MagnifyingGlass size={17} />
                            {busy ? "Searching…" : "Search logs"}
                        </button>
                    </form>
                    <p className="muted">
                        Only new requests made while capture is enabled have
                        content. Log ingestion is delayed; this view is not a
                        live stream. List results contain metadata only.
                    </p>
                    {result?.items.length ? (
                        <>
                            <div className="content-log-resultbar">
                                <span>
                                    {result.items.length} requests loaded
                                </span>
                                <button
                                    className="button"
                                    disabled={busy}
                                    onClick={() => void search()}
                                >
                                    <ArrowClockwise size={16} />
                                    Refresh results
                                </button>
                            </div>
                            <div className="content-log-table">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>Time</th>
                                            <th>API / model</th>
                                            <th>Developer</th>
                                            <th>Status</th>
                                            <th>Captured</th>
                                            <th>
                                                <span className="sr-only">
                                                    Inspect
                                                </span>
                                            </th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {result.items.map((item) => (
                                            <tr key={item.correlation_id}>
                                                <td>
                                                    {date(item.timestamp)}
                                                    <small>
                                                        {item.correlation_id.slice(
                                                            0,
                                                            8,
                                                        )}
                                                    </small>
                                                </td>
                                                <td>
                                                    {item.model ||
                                                        item.model_name ||
                                                        "Unknown model"}
                                                    <small>{item.api_id}</small>
                                                </td>
                                                <td title={item.principal_id}>
                                                    {members.find(
                                                        (m) =>
                                                            m.id ===
                                                            item.principal_id,
                                                    )?.name ||
                                                        item.principal_id?.slice(
                                                            0,
                                                            8,
                                                        ) ||
                                                        "Unattributed"}
                                                </td>
                                                <td>{item.status}</td>
                                                <td>
                                                    {item.has_request
                                                        ? "Prompt"
                                                        : "—"}{" "}
                                                    /{" "}
                                                    {item.has_response
                                                        ? "Response"
                                                        : "—"}
                                                </td>
                                                <td>
                                                    <button
                                                        className="button"
                                                        onClick={() =>
                                                            setSelected(item)
                                                        }
                                                        aria-label={`Inspect request ${item.correlation_id}`}
                                                    >
                                                        Inspect{" "}
                                                        <ArrowRight size={16} />
                                                    </button>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            {result.has_more && (
                                <button
                                    className="button"
                                    disabled={busy}
                                    onClick={() => void search(true)}
                                >
                                    Load more requests
                                </button>
                            )}
                        </>
                    ) : (
                        <Empty
                            title={
                                searched
                                    ? "No captured requests in this range"
                                    : "Find a request to inspect"
                            }
                        >
                            {searched
                                ? "Widen the time range, clear filters, or allow time for Azure Monitor ingestion. Older uncaptured conversations cannot be recovered here."
                                : "Choose a time range and optional API, model, or request ID. Opening content requires an access reason."}
                        </Empty>
                    )}
                </>
            )}
            {selected && (
                <ContentDrawer
                    key={selected.correlation_id}
                    entry={selected}
                    onClose={() => setSelected(undefined)}
                    onAudit={onAudit}
                />
            )}
        </div>
    );
}
