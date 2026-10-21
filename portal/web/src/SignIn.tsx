import { ArrowClockwise, Key } from "@phosphor-icons/react";
import { Notice, Seal } from "./components";
import type { PortalSession } from "./api";

export function SignInScreen({
    boot,
    loading,
    signingIn,
    requiresSignIn,
    error,
    details,
    onSignIn,
    onRetry,
    onSignOut,
}: {
    boot?: PortalSession;
    loading: boolean;
    signingIn: boolean;
    requiresSignIn: boolean;
    error: string;
    details: string;
    onSignIn: () => void;
    onRetry: () => void;
    onSignOut: () => void;
}) {
    const connected = boot?.authenticated && !requiresSignIn;
    return (
        <div className="signinscreen">
            <div className="signincard" aria-busy={loading || signingIn}>
                <Seal size={48} />
                <span className="preview">Development preview</span>
                <h1 style={{ marginTop: 24 }}>
                    {loading || !boot
                        ? "Claude & Codex Admin"
                        : connected
                          ? "Let’s reconnect to your organization."
                          : "Your organization. Your AI."}
                </h1>
                {loading ? (
                    <p className="muted" role="status">
                        Finishing sign-in and loading your organization…
                    </p>
                ) : (
                    <>
                        <p className="muted">
                            {connected
                                ? "Your Microsoft session is signed in. Retry the connection to load the portal."
                                : "Manage developer access, understand usage, and set the boundaries."}
                        </p>
                        {error && <Notice tone="error">{error}</Notice>}
                        {details && (
                            <details className="footnote">
                                <summary>Sign-in diagnostic details</summary>
                                <p>{details}</p>
                            </details>
                        )}
                        {!boot || connected ? (
                            <button className="button" onClick={onRetry}>
                                <ArrowClockwise /> Retry connection
                            </button>
                        ) : boot.configured ? (
                            <button
                                className="button primary"
                                disabled={signingIn}
                                onClick={onSignIn}
                            >
                                <Key size={18} />
                                {signingIn
                                    ? "Connecting to Microsoft…"
                                    : "Continue with Microsoft"}
                            </button>
                        ) : (
                            <Notice>
                                Finish the single-tenant Entra application
                                registration to enable sign-in.
                            </Notice>
                        )}
                        {connected && (
                            <button className="button" onClick={onSignOut}>
                                Sign out
                            </button>
                        )}
                        <small>
                            Access is limited to people authorized by your
                            organization. Your existing conversations stay on
                            your devices.
                        </small>
                    </>
                )}
            </div>
        </div>
    );
}
