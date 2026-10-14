import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
    initialize: vi.fn(),
    handleRedirectPromise: vi.fn(),
    setActiveAccount: vi.fn(),
    getAllAccounts: vi.fn(),
    loginRedirect: vi.fn(),
    acquireTokenSilent: vi.fn(),
}));

vi.mock("@azure/msal-browser", () => ({
    PublicClientApplication: class {
        initialize = identity.initialize;
        handleRedirectPromise = identity.handleRedirectPromise;
        setActiveAccount = identity.setActiveAccount;
        getAllAccounts = identity.getAllAccounts;
        loginRedirect = identity.loginRedirect;
        acquireTokenSilent = identity.acquireTokenSilent;
    },
    InteractionRequiredAuthError: class extends Error {},
}));

const configuration = {
    tenantId: "tenant",
    clientId: "portal",
    configured: true,
    local: false,
    name: "Test portal",
};

beforeEach(() => {
    vi.resetModules();
    vi.resetAllMocks();
    vi.stubGlobal("location", { origin: "https://portal.example.com" });
    vi.stubGlobal(
        "fetch",
        vi
            .fn()
            .mockResolvedValue({ ok: true, json: async () => configuration }),
    );
    identity.initialize.mockResolvedValue(undefined);
    identity.handleRedirectPromise.mockResolvedValue(null);
    identity.getAllAccounts.mockReturnValue([]);
});
afterEach(() => vi.unstubAllGlobals());

describe("portal sign-in recovery", () => {
    it("keeps a denied sign-in on the portal without restarting authentication", async () => {
        identity.handleRedirectPromise.mockRejectedValue(
            new Error("AADSTS50012: Authentication failed."),
        );
        const { initialize } = await import("./api");
        const result = await initialize();
        expect(result).toEqual({
            ...configuration,
            signInError: "AADSTS50012: Authentication failed.",
        });
        expect(identity.loginRedirect).not.toHaveBeenCalled();
        expect(identity.acquireTokenSilent).not.toHaveBeenCalled();
    });

    it("reuses the only cached account without an interactive sign-in", async () => {
        const account = { homeAccountId: "existing-account" };
        identity.getAllAccounts.mockReturnValue([account]);
        const { initialize } = await import("./api");
        await initialize();
        expect(identity.setActiveAccount).toHaveBeenCalledWith(account);
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });

    it("lets Entra reuse its session instead of forcing the account picker", async () => {
        const { initialize, signIn } = await import("./api");
        await initialize();
        await signIn();
        expect(identity.loginRedirect).toHaveBeenCalledExactlyOnceWith({
            scopes: ["api://portal/access_as_user"],
        });
    });

    it("does not initialize sign-in when bootstrap fails", async () => {
        vi.mocked(fetch).mockResolvedValue({ ok: false } as Response);
        const { initialize } = await import("./api");
        await expect(initialize()).rejects.toThrow(
            "configuration could not be loaded",
        );
        expect(identity.initialize).not.toHaveBeenCalled();
    });
});
