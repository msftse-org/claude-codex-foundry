import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { SignInScreen } from "./SignIn";

const props = {
    boot: {
        tenantId: "tenant",
        clientId: "portal",
        configured: true,
        local: false,
        name: "Test",
        authenticated: true,
    },
    loading: false,
    signingIn: false,
    requiresSignIn: false,
    error: "",
    details: "",
    onSignIn: vi.fn(),
    onRetry: vi.fn(),
    onSignOut: vi.fn(),
};

describe("portal startup screen", () => {
    it("never offers another sign-in while loading authenticated organization data", () => {
        const html = renderToStaticMarkup(<SignInScreen {...props} loading />);
        expect(html).toContain('role="status"');
        expect(html).toContain("Finishing sign-in");
        expect(html).not.toContain("<button");
        expect(html).not.toContain("Continue with Microsoft");
    });

    it("retries the connection instead of sign-in after an authenticated service failure", () => {
        const html = renderToStaticMarkup(
            <SignInScreen {...props} error="Storage unavailable" />,
        );
        expect(html).toContain("Storage unavailable");
        expect(html).toContain("Retry connection");
        expect(html).not.toContain("Continue with Microsoft");
    });

    it("offers sign-in only when the session needs renewal", () => {
        const html = renderToStaticMarkup(
            <SignInScreen {...props} requiresSignIn />,
        );
        expect(html).toContain("Continue with Microsoft");
        expect(html).not.toContain("Retry connection");
    });

    it("disables the login button after the user starts Microsoft sign-in", () => {
        const html = renderToStaticMarkup(
            <SignInScreen {...props} requiresSignIn signingIn />,
        );
        expect(html).toContain('disabled=""');
        expect(html).toContain("Connecting to Microsoft");
    });

    it("escapes provider diagnostics instead of rendering HTML", () => {
        const html = renderToStaticMarkup(
            <SignInScreen
                {...props}
                requiresSignIn
                details="<script>test</script>"
            />,
        );
        expect(html).toContain("&lt;script&gt;test&lt;/script&gt;");
        expect(html).not.toContain("<script>");
    });
});
