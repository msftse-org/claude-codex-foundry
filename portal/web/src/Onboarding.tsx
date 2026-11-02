import { useEffect, useState } from "react";
import { ArrowRight, Copy, DownloadSimple } from "@phosphor-icons/react";
import { api, download, downloadFile } from "./api";
import { Notice, PageHead } from "./components";
import { MacInstallHelp } from "./MacInstallHelp";
import type { State } from "./types";

interface ClientProfile {
    gateway_url: string;
    tenant_id: string;
    auth_mode: "msal" | "azure-cli";
    client_type?: "claude" | "codex";
}

export function Onboarding({
    state,
    onMembers,
    notify,
}: {
    state: State;
    onMembers: () => void;
    notify: (message: string, error?: boolean) => void;
}) {
    const admin = ["owner", "admin"].includes(state.viewer.role);
    const manager = admin || state.viewer.role === "group_admin";
    const clientTypes = state.client_types || ["claude"];
    const preferredClient = (id: string) => {
        const group = state.groups.find((g) => g.id === id);
        const modelId = group?.default_model || group?.model_ids?.[0];
        const model = state.models?.find((m) => m.name === modelId);
        const preferred =
            model?.anthropicFamilyTier === "openai" ? "codex" : "claude";
        return clientTypes.includes(preferred) ? preferred : clientTypes[0];
    };
    const [clientType, setClientType] = useState(
        preferredClient(state.viewer.group_id),
    );
    const [groupId, setGroupId] = useState(state.viewer.group_id);
    const scope = admin ? groupId : "";
    const query = `?client=${clientType}${scope ? `&group_id=${encodeURIComponent(scope)}` : ""}`;
    const [result, setResult] = useState<{
        query: string;
        profile?: ClientProfile;
        error?: string;
    }>();
    const [busy, setBusy] = useState(false);
    const [retry, setRetry] = useState(0);
    useEffect(() => {
        let current = true;
        setResult(undefined);
        api<ClientProfile>(`/api/client-profile${query}`).then(
            (profile) => current && setResult({ query, profile }),
            (error: Error) =>
                current && setResult({ query, error: error.message }),
        );
        return () => {
            current = false;
        };
    }, [query, state.revision, retry]);
    const profile = result?.query === query ? result.profile : undefined;
    const error = result?.query === query ? result.error : undefined;
    const target =
        profile?.client_type === "codex"
            ? "Codex Desktop / CLI"
            : "Claude Desktop / Claude Code";
    const prompt = profile
        ? `Set up ${target} on my Mac using the claude-foundry-macos.zip I downloaded from our organization portal. Locate and extract that ZIP, then read README.md, AGENTS.md, and client.json in the extracted claude-foundry folder. The gateway address is ${profile.gateway_url}; use gateway_url from client.json exactly. Run bash Install.command from that folder and help me complete Microsoft sign-in. Preserve my existing settings and conversations. Explain how to reopen ${target} and check the connection.`
        : "";
    const getBundle = async () => {
        setBusy(true);
        try {
            await downloadFile(
                `/api/client-bundle${query}`,
                "claude-foundry-macos.zip",
            );
            notify(
                "Setup ZIP downloaded. Follow Install on your Mac below, including Apple approval if prompted.",
            );
        } catch (error) {
            notify((error as Error).message, true);
        } finally {
            setBusy(false);
        }
    };
    const copyPrompt = async () => {
        try {
            await navigator.clipboard.writeText(prompt);
            notify(
                "Setup prompt copied. Paste it into Codex on the customer's Mac.",
            );
        } catch {
            notify("Select and copy the setup prompt below.", true);
        }
    };
    return (
        <>
            <PageHead
                title="Developer onboarding"
                description="Download the setup ZIP, sign in with Microsoft, and connect your local app."
            />
            <div className="settings">
                {admin && state.budget_configured && (
                    <div className="onboardingstep">
                        <span className="stepnumber">✓</span>
                        <div>
                            <h2>Complete model verification</h2>
                            <p>
                                After reviewing enforcement prices in Budgets
                                &amp; limits, give this model policy to your
                                deployment administrator to verify the pinned
                                deployments before publishing access.
                            </p>
                            <button
                                className="button"
                                onClick={async () => {
                                    try {
                                        download(
                                            "model-policy.json",
                                            await api("/api/model-policy"),
                                        );
                                    } catch (e) {
                                        notify((e as Error).message, true);
                                    }
                                }}
                            >
                                <DownloadSimple />
                                Download model policy
                            </button>
                        </div>
                    </div>
                )}
                {manager && (
                    <div className="onboardingstep">
                        <span className="stepnumber">1</span>
                        <div>
                            <h2>Grant and publish access</h2>
                            <p>
                                Add the developer’s Entra object ID, assign
                                their primary group, and publish the reviewed
                                gateway policy.
                            </p>
                            <button className="button" onClick={onMembers}>
                                Manage members <ArrowRight />
                            </button>
                        </div>
                    </div>
                )}
                <div className="onboardingstep">
                    <span className="stepnumber">{manager ? 2 : 1}</span>
                    <div>
                        <h2>Download the complete Mac setup</h2>
                        <p>
                            The ZIP includes the installer, your gateway
                            address, approved models, and instructions for you
                            or Codex. Install your organization’s approved app
                            first.
                        </p>
                        {admin && (
                            <label className="setupgroup">
                                Group
                                <select
                                    value={groupId}
                                    onChange={(event) => {
                                        setGroupId(event.target.value);
                                        setClientType(
                                            preferredClient(event.target.value),
                                        );
                                    }}
                                    disabled={busy}
                                >
                                    {state.groups.map((group) => (
                                        <option key={group.id} value={group.id}>
                                            {group.name}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        )}
                        {clientTypes.length > 1 && (
                            <label className="setupgroup">
                                Application
                                <select
                                    value={clientType}
                                    onChange={(event) =>
                                        setClientType(
                                            event.target.value as
                                                | "claude"
                                                | "codex",
                                        )
                                    }
                                    disabled={busy}
                                >
                                    {clientTypes.map((client) => (
                                        <option key={client} value={client}>
                                            {client === "codex"
                                                ? "Codex"
                                                : "Claude"}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        )}
                        {error ? (
                            <Notice>
                                {error}{" "}
                                <button
                                    className="button"
                                    onClick={() => setRetry(retry + 1)}
                                >
                                    Retry
                                </button>
                            </Notice>
                        ) : !profile ? (
                            <p role="status">
                                Loading your connection settings…
                            </p>
                        ) : (
                            <>
                                <dl className="setupdetails">
                                    <dt>Application</dt>
                                    <dd>{target}</dd>
                                    <dt>Gateway address</dt>
                                    <dd>
                                        <code>{profile.gateway_url}</code>
                                    </dd>
                                    <dt>Microsoft sign-in</dt>
                                    <dd>
                                        {profile.auth_mode === "msal"
                                            ? "Browser sign-in; Azure CLI is not needed."
                                            : "Azure CLI; setup instructions include installation help."}
                                    </dd>
                                </dl>
                                <button
                                    className="button primary"
                                    onClick={getBundle}
                                    disabled={busy}
                                >
                                    <DownloadSimple />
                                    {busy
                                        ? "Preparing setup ZIP…"
                                        : "Download Mac setup ZIP"}
                                </button>
                                <p className="footnote">
                                    Connection settings contain no credentials.
                                    Each developer signs in with their own
                                    Microsoft work account. Local setup
                                    currently supports macOS.
                                </p>
                                <MacInstallHelp />
                            </>
                        )}
                    </div>
                </div>
                <div className="onboardingstep">
                    <span className="stepnumber">{manager ? 3 : 2}</span>
                    <div>
                        <h2>Open the installer or ask Codex</h2>
                        <p>
                            Extract the ZIP and double-click{" "}
                            <code>Install.command</code> inside the{" "}
                            <code>claude-foundry</code> folder. Complete
                            Microsoft sign-in, then save active work and fully
                            reopen the app.
                        </p>
                        {profile && (
                            <>
                                <p>
                                    To have Codex handle setup on the customer’s
                                    Mac, paste this prompt into Codex after
                                    downloading the ZIP:
                                </p>
                                <button className="button" onClick={copyPrompt}>
                                    <Copy />
                                    Copy setup prompt for Codex
                                </button>
                                <pre className="codewell setupprompt">
                                    {prompt}
                                </pre>
                            </>
                        )}
                        <p>
                            Choose an approved model and send a short test
                            message. The installer preserves previous settings
                            and prints a restore command.
                        </p>
                        <details>
                            <summary>Advanced: connection profile only</summary>
                            <p>
                                For an existing installer or managed rollout.
                                The JSON alone does not include the setup tools.
                            </p>
                            <button
                                className="button"
                                disabled={!profile || busy}
                                onClick={async () => {
                                    try {
                                        download(
                                            "client.json",
                                            await api(
                                                `/api/client-profile${query}`,
                                            ),
                                        );
                                        notify(
                                            "Connection profile downloaded.",
                                        );
                                    } catch (error) {
                                        notify((error as Error).message, true);
                                    }
                                }}
                            >
                                Download client.json
                            </button>
                        </details>
                    </div>
                </div>
            </div>
        </>
    );
}
