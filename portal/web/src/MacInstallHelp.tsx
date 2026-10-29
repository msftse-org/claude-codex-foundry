// A fixed, user-initiated OS destination. Never take a settings URL from the feed.
export const MAC_SECURITY_SETTINGS =
    "x-apple.systempreferences:com.apple.preference.security?General";

export function MacInstallHelp() {
    return (
        <section aria-label="Mac installation and security">
            <h3>Install on your Mac</h3>
            <ol>
                <li>
                    Extract the downloaded ZIP, open its{" "}
                    <code>claude-foundry</code> folder, then double-click{" "}
                    <code>Install.command</code>.
                </li>
                <li>
                    If Apple says it cannot verify the installer, click{" "}
                    <strong>Done</strong>, then use the button below. In Privacy
                    &amp; Security, find <code>Install.command</code>, choose{" "}
                    <strong>Open Anyway</strong>, and confirm. Approve only the
                    installer you downloaded from your trusted organization
                    portal.
                </li>
                <li>
                    Complete Microsoft sign-in in your browser. Wait for{" "}
                    <strong>SETUP COMPLETE</strong>, then save work and fully
                    reopen your apps. Downloading or approving the file is not
                    installation success.
                </li>
            </ol>
            <a className="button" href={MAC_SECURITY_SETTINGS}>
                Open macOS Privacy &amp; Security
            </a>
            <p className="footnote">
                Your browser may ask to open System Settings. If the button does
                not work, open Apple menu → System Settings → Privacy &amp;
                Security yourself. If your organization blocks exceptions,
                contact IT; do not work around its device policy.
            </p>
            <p className="footnote">
                This source/script installer is not Apple-signed or notarized.
                The portal cannot approve it for you. Keep Gatekeeper enabled;
                do not remove quarantine or change system-wide security
                settings.{" "}
                <a
                    href="https://support.apple.com/102445"
                    target="_blank"
                    rel="noopener noreferrer"
                >
                    Apple’s guidance
                </a>
            </p>
        </section>
    );
}
