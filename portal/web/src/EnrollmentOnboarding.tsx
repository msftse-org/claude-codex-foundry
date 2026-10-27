import { useEffect, useState } from "react";
import { ArrowRight, Copy } from "@phosphor-icons/react";
import { api, download } from "./api";
import { Notice, PageHead } from "./components";
import { TerminalSetup } from "./TerminalSetup";
import type { State } from "./types";

interface Enrollment {
    clients: ("claude" | "codex")[];
    onboarding_url: string;
    descriptor: {
        configuration_url: string;
        routes: Record<string, { gateway_url: string }>;
    };
}

export function EnrollmentOnboarding({
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
    const [result, setResult] = useState<{
        value?: Enrollment;
        error?: string;
    }>();
    const [selection, setSelection] = useState("claude");
    const [retry, setRetry] = useState(0);
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        let current = true;
        setResult(undefined);
        api<Enrollment>("/api/client-enrollment").then(
            (value) => {
                if (!current) return;
                setResult({ value });
                setSelection(
                    value.clients.length > 1 ? "both" : value.clients[0],
                );
            },
            (error: Error) => current && setResult({ error: error.message }),
        );
        return () => {
            current = false;
        };
    }, [state.revision, retry]);
    const enrollment = result?.value;
    const copy = async (value: string) => {
        try {
            await navigator.clipboard.writeText(value);
            notify("Copied.");
        } catch {
            notify("Select and copy the text below.", true);
        }
    };
    return (
        <>
            <PageHead
                title="Developer onboarding"
                description="Enroll once with Microsoft, then receive published model updates."
            />
            <div className="settings">
                <Notice>
                    Experimental enrollment pilot. Endpoint-security and native
                    client qualification are required before user rollout.
                </Notice>
                {manager && (
                    <div className="onboardingstep">
                        <span className="stepnumber">1</span>
                        <div>
                            <h2>Grant and publish access</h2>
                            <p>
                                Add the developer, review model bounds and
                                rates, assign their group, then publish.
                                Deploying a model alone does not grant access.
                            </p>
                            <button className="button" onClick={onMembers}>
                                Manage members <ArrowRight />
                            </button>
                            {admin && state.budget_configured && (
                                <button
                                    className="button"
                                    onClick={async () => {
                                        try {
                                            download(
                                                "model-policy.json",
                                                await api("/api/model-policy"),
                                            );
                                        } catch (error) {
                                            notify(
                                                (error as Error).message,
                                                true,
                                            );
                                        }
                                    }}
                                >
                                    Download model policy
                                </button>
                            )}
                        </div>
                    </div>
                )}
                <div className="onboardingstep">
                    <span className="stepnumber">{manager ? 2 : 1}</span>
                    <div>
                        <h2>One organization link</h2>
                        <p>
                            Share this onboarding link, not a user-specific JSON
                            file. Each developer opens it and signs in with
                            their own authorized Microsoft work account.
                        </p>
                        {result?.error ? (
                            <Notice>
                                {result.error}{" "}
                                <button
                                    className="button"
                                    onClick={() => setRetry(retry + 1)}
                                >
                                    Retry
                                </button>
                            </Notice>
                        ) : !enrollment ? (
                            <p role="status">Loading your published access…</p>
                        ) : (
                            <>
                                <p>
                                    <code>{enrollment.onboarding_url}</code>
                                </p>
                                <button
                                    className="button"
                                    onClick={() =>
                                        copy(enrollment.onboarding_url)
                                    }
                                >
                                    <Copy /> Copy organization onboarding link
                                </button>
                                <label className="setupgroup">
                                    Application
                                    <select
                                        value={selection}
                                        onChange={(e) =>
                                            setSelection(e.target.value)
                                        }
                                        disabled={busy}
                                    >
                                        {enrollment.clients.length > 1 && (
                                            <option value="both">
                                                Claude and Codex
                                            </option>
                                        )}
                                        {enrollment.clients.map((name) => (
                                            <option key={name} value={name}>
                                                {name === "codex"
                                                    ? "Codex"
                                                    : "Claude"}
                                            </option>
                                        ))}
                                    </select>
                                </label>
                                <TerminalSetup
                                    key={`${state.revision}:${selection}`}
                                    selection={selection}
                                    notify={notify}
                                    onBusy={setBusy}
                                />
                                <p className="footnote">
                                    This download is for your signed-in
                                    account’s published access. Administrators
                                    cannot select a different user or group
                                    here.
                                </p>
                            </>
                        )}
                    </div>
                </div>
                {enrollment && (
                    <div className="onboardingstep">
                        <span className="stepnumber">{manager ? 3 : 2}</span>
                        <div>
                            <h2>Sign in and stay up to date</h2>
                            <p>
                                Paste the copied setup command into Terminal.
                                Complete Microsoft sign-in and follow the
                                result. After SETUP COMPLETE, save work and
                                fully reopen the apps. Existing JSON
                                installations require an explicit migration.
                                Background refresh starts automatically as part
                                of enrollment.
                            </p>
                            <p>
                                Published model updates arrive about every 15
                                minutes while logged into your Mac. Reopen an
                                already-running client to reload its picker. No
                                automatic restart or instant hot-reload is
                                promised.
                            </p>
                            <p>
                                Every inference request still checks current
                                gateway access and shared budget policy. An old
                                local model list cannot restore revoked access.
                            </p>
                        </div>
                    </div>
                )}
            </div>
        </>
    );
}
