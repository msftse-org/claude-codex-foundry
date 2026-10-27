import { useEffect, useRef, useState } from "react";
import { Copy, DownloadSimple } from "@phosphor-icons/react";
import { downloadSetup } from "./api";
import { setupFilename, terminalSetupCommand } from "./setupCommand";

export function TerminalSetup({
    selection,
    notify,
    onBusy,
}: {
    selection: string;
    notify: (message: string, error?: boolean) => void;
    onBusy: (busy: boolean) => void;
}) {
    const [digest, setDigest] = useState<string>();
    const [busy, setBusy] = useState(false);
    const active = useRef(true);
    useEffect(() => {
        active.current = true;
        return () => {
            active.current = false;
            onBusy(false);
        };
    }, [onBusy]);
    const command = digest ? terminalSetupCommand(digest) : "";
    const copy = async (value: string) => {
        try {
            await navigator.clipboard.writeText(value);
            if (active.current)
                notify(
                    "Setup command copied. Paste it into Terminal and press Enter.",
                );
        } catch {
            if (active.current)
                notify(
                    "The ZIP download started, but clipboard access was unavailable. Use Copy command or select the command below.",
                    true,
                );
        }
    };
    return (
        <section aria-label="Terminal setup">
            <h3>Install with one Terminal command</h3>
            <p>
                Install your organization’s approved apps first. Azure CLI is
                required for organizations using Azure CLI sign-in. Your device
                policy must permit Terminal scripts.
            </p>
            <p>
                This button downloads a ZIP and copies its setup command. Open
                Terminal, paste once, and press Enter. Setup verifies the ZIP,
                extracts it privately, downloads its runtime dependencies, and
                opens Microsoft sign-in when needed. Keep the ZIP downloaded by
                this button; there is no need to extract or double-click it.
            </p>
            <button
                className="button primary"
                disabled={busy}
                onClick={async () => {
                    setBusy(true);
                    onBusy(true);
                    setDigest(undefined);
                    try {
                        const sha256 = await downloadSetup(
                            `/api/client-enrollment-bundle?client=${selection}`,
                        );
                        if (!active.current) return;
                        const value = terminalSetupCommand(sha256);
                        setDigest(sha256);
                        await copy(value);
                    } catch (error) {
                        if (active.current)
                            notify((error as Error).message, true);
                    } finally {
                        if (active.current) {
                            setBusy(false);
                            onBusy(false);
                        }
                    }
                }}
            >
                <DownloadSimple />
                {busy ? "Preparing setup…" : "Download setup & copy command"}
            </button>
            {digest && (
                <>
                    <p>
                        Download: <code>{setupFilename(digest)}</code>. Wait for
                        the browser download to finish. The command looks in
                        Downloads; if you saved elsewhere, enter the full ZIP
                        path when asked, without quotes.
                    </p>
                    <button className="button" onClick={() => copy(command)}>
                        <Copy /> Copy command
                    </button>
                    <pre
                        className="codewell setupprompt"
                        aria-label="Setup command"
                    >
                        {command}
                    </pre>
                    <p className="footnote">
                        Checksum verification detects a different or damaged
                        ZIP; trust the organization portal supplying both. It
                        does not provide an Apple signature. Setup does not
                        change device security settings. If IT blocks scripts,
                        request an approved managed installation.
                    </p>
                </>
            )}
        </section>
    );
}
