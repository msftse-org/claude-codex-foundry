import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
    construct: vi.fn(),
    initialize: vi.fn(),
    handleRedirectPromise: vi.fn(),
    setActiveAccount: vi.fn(),
    getAllAccounts: vi.fn(),
    getActiveAccount: vi.fn(),
    loginRedirect: vi.fn(),
    acquireTokenSilent: vi.fn(),
    logoutRedirect: vi.fn(),
}));

vi.mock("@azure/msal-browser", () => ({
    PublicClientApplication: class {
        constructor(config: unknown) {
            identity.construct(config);
        }
        initialize = identity.initialize;
        handleRedirectPromise = identity.handleRedirectPromise;
        setActiveAccount = identity.setActiveAccount;
        getAllAccounts = identity.getAllAccounts;
        getActiveAccount = identity.getActiveAccount;
        loginRedirect = identity.loginRedirect;
        acquireTokenSilent = identity.acquireTokenSilent;
        logoutRedirect = identity.logoutRedirect;
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
    vi.stubGlobal("location", {
        origin: "https://portal.example.com",
        pathname: "/",
        search: "?page=budgets",
    });
    vi.stubGlobal("history", { state: {}, replaceState: vi.fn() });
    vi.stubGlobal(
        "fetch",
        vi
            .fn()
            .mockResolvedValue({ ok: true, json: async () => configuration }),
    );
    identity.initialize.mockResolvedValue(undefined);
    identity.handleRedirectPromise.mockResolvedValue(null);
    identity.getAllAccounts.mockReturnValue([]);
    identity.getActiveAccount.mockReturnValue(null);
    identity.setActiveAccount.mockImplementation((account) =>
        identity.getActiveAccount.mockReturnValue(account),
    );
    identity.loginRedirect.mockResolvedValue(undefined);
    identity.acquireTokenSilent.mockResolvedValue({
        accessToken: "synthetic-token",
    });
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
            authenticated: false,
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
            state: "/?page=budgets",
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

    it("initializes exactly once for concurrent consumers", async () => {
        const { initialize } = await import("./api");
        const results = await Promise.all([
            initialize(),
            initialize(),
            initialize(),
        ]);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(identity.construct).toHaveBeenCalledTimes(1);
        expect(identity.initialize).toHaveBeenCalledTimes(1);
        expect(identity.handleRedirectPromise).toHaveBeenCalledExactlyOnceWith({
            navigateToLoginRequestUrl: false,
        });
        expect(results.every((result) => result === results[0])).toBe(true);
    });

    it("finishes the callback and restores the requested portal page without navigation", async () => {
        const account = { homeAccountId: "signed-in-account" };
        identity.handleRedirectPromise.mockResolvedValue({
            account,
            state: "/?page=budgets&q=test",
        });
        const { initialize } = await import("./api");
        expect((await initialize()).authenticated).toBe(true);
        expect(identity.setActiveAccount).toHaveBeenCalledWith(account);
        expect(history.replaceState).toHaveBeenCalledWith(
            {},
            "",
            "/?page=budgets&q=test",
        );
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });

    it.each([
        "https://other.example/",
        "//other.example/",
        "/\\other.example/",
        "/api/state",
        "/\\",
        "javascript:alert(1)",
    ])("ignores an unsafe return page: %s", async (state) => {
        identity.handleRedirectPromise.mockResolvedValue({
            account: { homeAccountId: "account" },
            state,
        });
        const { initialize } = await import("./api");
        expect((await initialize()).authenticated).toBe(true);
        expect(history.replaceState).not.toHaveBeenCalled();
    });

    it("never restores an OAuth fragment", async () => {
        identity.handleRedirectPromise.mockResolvedValue({
            account: { homeAccountId: "account" },
            state: "/?page=budgets#code=synthetic",
        });
        const { initialize } = await import("./api");
        await initialize();
        expect(history.replaceState).toHaveBeenCalledWith(
            {},
            "",
            "/?page=budgets",
        );
    });

    it("preserves an explicitly active account when multiple accounts are cached", async () => {
        const active = { homeAccountId: "active" };
        identity.getActiveAccount.mockReturnValue(active);
        identity.getAllAccounts.mockReturnValue([
            { homeAccountId: "other" },
            active,
        ]);
        const { initialize } = await import("./api");
        expect((await initialize()).authenticated).toBe(true);
        expect(identity.setActiveAccount).not.toHaveBeenCalled();
    });

    it("does not select an arbitrary account from multiple cached accounts", async () => {
        identity.getAllAccounts.mockReturnValue([
            { homeAccountId: "first" },
            { homeAccountId: "second" },
        ]);
        const { initialize } = await import("./api");
        expect((await initialize()).authenticated).toBe(false);
        expect(identity.setActiveAccount).not.toHaveBeenCalled();
    });

    it("does not start duplicate interactive redirects", async () => {
        let finish!: () => void;
        identity.loginRedirect.mockReturnValue(
            new Promise<void>((resolve) => {
                finish = resolve;
            }),
        );
        const { initialize, signIn } = await import("./api");
        await initialize();
        const first = signIn(),
            second = signIn();
        await vi.waitFor(() =>
            expect(identity.loginRedirect).toHaveBeenCalledTimes(1),
        );
        finish();
        await Promise.all([first, second]);
    });

    it("permits an explicit retry after a rejected interactive request", async () => {
        identity.loginRedirect.mockRejectedValueOnce(
            new Error("User cancelled"),
        );
        const { signIn } = await import("./api");
        await expect(signIn()).rejects.toThrow("User cancelled");
        await signIn();
        expect(identity.loginRedirect).toHaveBeenCalledTimes(2);
    });

    it("recovers from bootstrap failure without creating duplicate clients", async () => {
        vi.mocked(fetch).mockRejectedValueOnce(
            new Error("Network unavailable"),
        );
        const { initialize } = await import("./api");
        await expect(initialize()).rejects.toThrow("Network unavailable");
        expect((await initialize()).authenticated).toBe(false);
        expect(identity.construct).toHaveBeenCalledTimes(1);
    });
});

describe("authenticated portal requests", () => {
    async function ready() {
        identity.getActiveAccount.mockReturnValue({ homeAccountId: "account" });
        const module = await import("./api");
        await module.initialize();
        vi.mocked(fetch).mockClear();
        return module;
    }
    const response = (status: number, body: unknown) =>
        ({ ok: status < 400, status, json: async () => body }) as Response;

    it("refreshes a rejected read token once without interactive sign-in", async () => {
        const { api } = await ready();
        vi.mocked(fetch)
            .mockResolvedValueOnce(response(401, {}))
            .mockResolvedValueOnce(response(200, { viewer: "owner" }));
        expect(await api("/api/state")).toEqual({ viewer: "owner" });
        expect(fetch).toHaveBeenCalledTimes(2);
        expect(identity.acquireTokenSilent.mock.calls[1][0]).toMatchObject({
            forceRefresh: true,
        });
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });

    it("stops after one failed refresh and asks for one explicit sign-in", async () => {
        const { api, SignInRequiredError } = await ready();
        vi.mocked(fetch).mockResolvedValue(
            response(401, { detail: "Expired session" }),
        );
        await expect(api("/api/state")).rejects.toBeInstanceOf(
            SignInRequiredError,
        );
        expect(fetch).toHaveBeenCalledTimes(2);
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });

    it("never retries permission denials", async () => {
        const { api, SignInRequiredError } = await ready();
        vi.mocked(fetch).mockResolvedValue(
            response(403, { detail: "Access suspended" }),
        );
        const failure = await api("/api/state").catch((e) => e);
        expect(failure).toHaveProperty("message", "Access suspended");
        expect(failure).not.toBeInstanceOf(SignInRequiredError);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(identity.acquireTokenSilent).toHaveBeenCalledTimes(1);
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });

    it("does not replay administrative writes after a 401", async () => {
        const { api, SignInRequiredError } = await ready();
        vi.mocked(fetch).mockResolvedValue(
            response(401, { detail: "Expired session" }),
        );
        await expect(
            api("/api/budgets/publish", { method: "POST", body: "{}" }),
        ).rejects.toBeInstanceOf(SignInRequiredError);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(identity.acquireTokenSilent).toHaveBeenCalledTimes(1);
    });

    it("does not start an interactive redirect when silent refresh needs user interaction", async () => {
        const { api, SignInRequiredError } = await ready();
        const { InteractionRequiredAuthError } = await import(
            "@azure/msal-browser"
        );
        identity.acquireTokenSilent.mockRejectedValue(
            new InteractionRequiredAuthError(
                "interaction_required",
                "Please sign in",
            ),
        );
        await expect(api("/api/state")).rejects.toBeInstanceOf(
            SignInRequiredError,
        );
        expect(fetch).not.toHaveBeenCalled();
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });

    it("keeps a service outage distinct from an authentication failure", async () => {
        const { api, SignInRequiredError } = await ready();
        vi.mocked(fetch).mockResolvedValue(
            response(503, { detail: "Storage unavailable" }),
        );
        const failure = await api("/api/state").catch((e) => e);
        expect(failure).toHaveProperty("message", "Storage unavailable");
        expect(failure).not.toBeInstanceOf(SignInRequiredError);
        expect(fetch).toHaveBeenCalledTimes(1);
        expect(identity.loginRedirect).not.toHaveBeenCalled();
    });
});
