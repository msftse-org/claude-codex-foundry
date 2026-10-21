import type {
    PublicClientApplication,
    InteractionRequiredAuthError,
} from "@azure/msal-browser";
import type { Bootstrap } from "./types";
import { csvText } from "./csv";
let msal: PublicClientApplication | undefined;
let interactionRequired: typeof InteractionRequiredAuthError | undefined;
let config: Bootstrap;
export type PortalSession = Bootstrap & {
    authenticated: boolean;
    signInError?: string;
};
let initialization: Promise<PortalSession> | undefined;
let signingIn: Promise<void> | undefined;

export class SignInRequiredError extends Error {}

// One instance and one redirect handler per page, including concurrent startup calls.
export function initialize(): Promise<PortalSession> {
    if (!initialization)
        initialization = initializeOnce().catch((error) => {
            initialization = undefined;
            throw error;
        });
    return initialization;
}

async function initializeOnce(): Promise<PortalSession> {
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
            // Redeem the callback here instead of navigating a second time before
            // tokens are available. MSAL validates the custom return-page state.
            redirect = await msal.handleRedirectPromise({
                navigateToLoginRequestUrl: false,
            });
        } catch (error) {
            return {
                ...config,
                authenticated: false,
                signInError:
                    error instanceof Error
                        ? error.message
                        : "Microsoft did not complete sign-in.",
            };
        }
        if (redirect?.account) {
            msal.setActiveAccount(redirect.account);
            restorePage(redirect.state);
        } else if (
            !msal.getActiveAccount() &&
            msal.getAllAccounts().length === 1
        )
            msal.setActiveAccount(msal.getAllAccounts()[0]);
    }
    return {
        ...config,
        authenticated: config.local || Boolean(msal?.getActiveAccount()),
    };
}

function restorePage(state?: string) {
    if (!state?.startsWith("/") || state.startsWith("//")) return;
    try {
        const target = new URL(state, location.origin);
        // The portal has query-based pages at /. Never navigate to an external
        // site, an API endpoint or an OAuth fragment supplied as return state.
        if (target.origin === location.origin && target.pathname === "/")
            history.replaceState(
                history.state,
                "",
                target.pathname + target.search,
            );
    } catch {
        // An invalid return page must not discard a successful authentication.
    }
}
export async function signIn() {
    await initialize();
    if (msal && !signingIn)
        signingIn = msal
            .loginRedirect({
                scopes: [`api://${config.clientId}/access_as_user`],
                state: location.pathname + location.search,
            })
            .catch((error) => {
                signingIn = undefined;
                throw error;
            });
    return signingIn;
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
    await initialize();
    const headers = new Headers(options.headers);
    if (options.body) headers.set("Content-Type", "application/json");
    if (!config?.local) {
        headers.set("Authorization", `Bearer ${await accessToken()}`);
    }
    let result = await fetch(path, { ...options, headers });
    // Refresh once for a rejected read token. Never replay writes or retry a
    // permission denial; neither condition should start an interactive sign-in.
    if (
        !config.local &&
        result.status === 401 &&
        (options.method || "GET").toUpperCase() === "GET"
    ) {
        headers.set("Authorization", `Bearer ${await accessToken(true)}`);
        result = await fetch(path, { ...options, headers });
    }
    if (!result.ok) {
        const body = await result.json().catch(() => ({
            detail: "The service could not complete this request.",
        }));
        const message =
            typeof body.detail === "string"
                ? body.detail
                : "Check the form values and try again.";
        if (result.status === 401) throw new SignInRequiredError(message);
        throw new Error(message);
    }
    return result.json();
}

async function accessToken(forceRefresh = false): Promise<string> {
    const account = msal?.getActiveAccount();
    if (!msal || !account)
        throw new SignInRequiredError("Sign in to continue.");
    try {
        const token = await msal.acquireTokenSilent({
            account,
            scopes: [`api://${config.clientId}/access_as_user`],
            ...(forceRefresh ? { forceRefresh: true } : {}),
        });
        return token.accessToken;
    } catch (error) {
        if (interactionRequired && error instanceof interactionRequired)
            throw new SignInRequiredError(
                "Your Microsoft session needs to be renewed. Continue with Microsoft to sign in again.",
            );
        throw error;
    }
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
