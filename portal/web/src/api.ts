import type {
    PublicClientApplication,
    InteractionRequiredAuthError,
} from "@azure/msal-browser";
import type { Bootstrap } from "./types";
import { csvText } from "./csv";
let msal: PublicClientApplication | undefined;
let interactionRequired: typeof InteractionRequiredAuthError | undefined;
let config: Bootstrap;
export async function initialize(): Promise<
    Bootstrap & { signInError?: string }
> {
    const response = await fetch("/api/bootstrap");
    if (!response.ok)
        throw new Error(
            "The portal configuration could not be loaded. Try again shortly.",
        );
    config = await response.json();
    if (config.configured && !config.local) {
        const { PublicClientApplication, InteractionRequiredAuthError } =
            await import("@azure/msal-browser");
        interactionRequired = InteractionRequiredAuthError;
        msal = new PublicClientApplication({
            auth: {
                clientId: config.clientId,
                authority: `https://login.microsoftonline.com/${config.tenantId}`,
                redirectUri: location.origin + "/",
            },
            cache: { cacheLocation: "sessionStorage" },
        });
        await msal.initialize();
        // A rejected sign-in must leave the portal usable, without automatically
        // starting another redirect or confusing it with a connection failure.
        let redirect;
        try {
            redirect = await msal.handleRedirectPromise();
        } catch (error) {
            return {
                ...config,
                signInError:
                    error instanceof Error
                        ? error.message
                        : "Microsoft did not complete sign-in.",
            };
        }
        if (redirect) msal.setActiveAccount(redirect.account);
        else if (msal.getAllAccounts().length === 1)
            msal.setActiveAccount(msal.getAllAccounts()[0]);
    }
    return config;
}
export async function signIn() {
    if (msal)
        await msal.loginRedirect({
            scopes: [`api://${config.clientId}/access_as_user`],
        });
}
export async function signOut() {
    if (msal)
        await msal.logoutRedirect({
            postLogoutRedirectUri: location.origin + "/",
        });
}
export async function api<T>(
    path: string,
    options: RequestInit = {},
): Promise<T> {
    const headers = new Headers(options.headers);
    if (options.body) headers.set("Content-Type", "application/json");
    if (!config?.local) {
        const account = msal?.getActiveAccount();
        if (!msal || !account) throw new Error("Sign in to continue.");
        try {
            const token = await msal.acquireTokenSilent({
                account,
                scopes: [`api://${config.clientId}/access_as_user`],
            });
            headers.set("Authorization", `Bearer ${token.accessToken}`);
        } catch (error) {
            if (interactionRequired && error instanceof interactionRequired)
                throw new Error(
                    "Your session needs attention. Sign out and sign in again.",
                );
            throw error;
        }
    }
    const result = await fetch(path, { ...options, headers });
    if (!result.ok) {
        const body = await result.json().catch(() => ({
            detail: "The service could not complete this request.",
        }));
        throw new Error(
            typeof body.detail === "string"
                ? body.detail
                : "Check the form values and try again.",
        );
    }
    return result.json();
}
export function download(name: string, value: unknown) {
    const blob = new Blob([JSON.stringify(value, null, 2)], {
        type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
}
export function exportCsv(name: string, rows: Record<string, unknown>[]) {
    if (!rows.length) return;
    const content = csvText(rows);
    const url = URL.createObjectURL(
        new Blob([content], { type: "text/csv;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
}
