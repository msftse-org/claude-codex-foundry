// @vitest-environment jsdom
import {
    cleanup,
    fireEvent,
    render,
    screen,
    waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, downloadFile, downloadSetup } from "./api";
import { Onboarding } from "./Onboarding";
import { MAC_SECURITY_SETTINGS } from "./MacInstallHelp";
import type { State } from "./types";

vi.mock("./api", () => ({
    api: vi.fn(),
    download: vi.fn(),
    downloadFile: vi.fn(),
    downloadSetup: vi.fn(),
}));
const profile = {
    gateway_url: "https://organization.azure-api.net/claude-budget-preview",
    tenant_id: "tenant",
    auth_mode: "msal",
};
const state = {
    revision: 1,
    viewer: { role: "owner", group_id: "platform" },
    groups: [
        { id: "platform", name: "Platform" },
        { id: "other", name: "Other team" },
    ],
    // Deliberately different: onboarding must use the exported profile's gateway.
    gateway_url: "https://organization.azure-api.net/legacy",
} as State;
const notify = vi.fn();
beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api).mockResolvedValue(profile);
    vi.mocked(downloadFile).mockResolvedValue(undefined);
    vi.mocked(downloadSetup).mockResolvedValue("a".repeat(64));
    Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
});
afterEach(cleanup);

it("downloads a complete scoped ZIP and copies the correct gateway and target for Codex", async () => {
    render(<Onboarding state={state} onMembers={vi.fn()} notify={notify} />);
    await screen.findByText(profile.gateway_url);
    fireEvent.click(
        screen.getByRole("button", { name: "Download Mac setup ZIP" }),
    );
    await waitFor(() =>
        expect(downloadFile).toHaveBeenCalledWith(
            "/api/client-bundle?client=claude&group_id=platform",
            "claude-foundry-macos.zip",
        ),
    );
    fireEvent.click(
        screen.getByRole("button", { name: "Copy setup prompt for Codex" }),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining(profile.gateway_url),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining("Set up Claude Desktop / Claude Code"),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining("bash Install.command"),
    );
    expect(screen.queryByText(state.gateway_url)).toBeNull();
});

it("discards an older group's connection response after selection changes", async () => {
    let resolveOld!: (value: unknown) => void;
    vi.mocked(api).mockReturnValueOnce(
        new Promise((resolve) => {
            resolveOld = resolve;
        }),
    );
    const other = {
        ...profile,
        gateway_url: "https://other.azure-api.net/codex/v1",
        client_type: "codex",
    };
    vi.mocked(api).mockResolvedValueOnce(other);
    render(<Onboarding state={state} onMembers={vi.fn()} notify={notify} />);
    fireEvent.change(screen.getByLabelText("Group"), {
        target: { value: "other" },
    });
    await screen.findByText(other.gateway_url);
    resolveOld(profile);
    await waitFor(() =>
        expect(screen.queryByText(profile.gateway_url)).toBeNull(),
    );
    fireEvent.click(
        screen.getByRole("button", { name: "Copy setup prompt for Codex" }),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining("Set up Codex Desktop / CLI"),
    );
    fireEvent.click(
        screen.getByRole("button", { name: "Download Mac setup ZIP" }),
    );
    expect(downloadFile).toHaveBeenCalledWith(
        "/api/client-bundle?client=claude&group_id=other",
        "claude-foundry-macos.zip",
    );
});

it("shows access failures with a retry and does not offer an incomplete download", async () => {
    vi.mocked(api).mockRejectedValueOnce(
        new Error(
            "Publish this member's budget access before exporting a profile",
        ),
    );
    render(
        <Onboarding
            state={{ ...state, viewer: { ...state.viewer, role: "developer" } }}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    await screen.findByText(
        "Publish this member's budget access before exporting a profile",
    );
    expect(
        screen.queryByRole("button", { name: "Download Mac setup ZIP" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Manage members" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(profile.gateway_url);
    expect(api).toHaveBeenLastCalledWith("/api/client-profile?client=claude");
});

it("preserves the unified portal's provider choice for profiles, ZIPs and setup prompts", async () => {
    render(
        <Onboarding
            state={{ ...state, client_types: ["claude", "codex"] }}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    await screen.findByText(profile.gateway_url);
    const codex = {
        ...profile,
        client_type: "codex",
        gateway_url: "https://organization.azure-api.net/codex/v1",
    };
    vi.mocked(api).mockResolvedValueOnce(codex);
    fireEvent.change(screen.getByLabelText("Application"), {
        target: { value: "codex" },
    });
    await screen.findByText(codex.gateway_url);
    expect(api).toHaveBeenLastCalledWith(
        "/api/client-profile?client=codex&group_id=platform",
    );
    fireEvent.click(
        screen.getByRole("button", { name: "Copy setup prompt for Codex" }),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining("Set up Codex Desktop / CLI"),
    );
    fireEvent.click(
        screen.getByRole("button", { name: "Download Mac setup ZIP" }),
    );
    expect(downloadFile).toHaveBeenCalledWith(
        "/api/client-bundle?client=codex&group_id=platform",
        "claude-foundry-macos.zip",
    );
});

it("starts with Codex setup when the group's default is an OpenAI model", async () => {
    const codex = {
        ...profile,
        client_type: "codex",
        gateway_url: "https://organization.azure-api.net/codex-governed/v1",
    };
    vi.mocked(api).mockResolvedValue(codex);
    const firstUser = {
        ...state,
        client_types: ["claude", "codex"],
        models: [{ name: "gpt-test", anthropicFamilyTier: "openai" }],
        groups: [
            {
                ...state.groups[0],
                default_model: "gpt-test",
                model_ids: ["gpt-test"],
            },
        ],
    } as State;
    render(
        <Onboarding state={firstUser} onMembers={vi.fn()} notify={notify} />,
    );
    await screen.findByText(codex.gateway_url);
    expect(api).toHaveBeenCalledWith(
        "/api/client-profile?client=codex&group_id=platform",
    );
    fireEvent.click(
        screen.getByRole("button", { name: "Download Mac setup ZIP" }),
    );
    expect(downloadFile).toHaveBeenCalledWith(
        "/api/client-bundle?client=codex&group_id=platform",
        "claude-foundry-macos.zip",
    );
});

const enrollment = {
    clients: ["claude", "codex"],
    onboarding_url: "https://portal.example.com/?page=onboarding",
    descriptor: {
        configuration_url:
            "https://portal.example.com/api/v1/me/client-configuration",
        routes: {},
    },
};
const enrolledState = { ...state, client_enrollment_enabled: true } as State;

it("offers one stable enrollment link with automatic refresh and only an application choice", async () => {
    vi.mocked(api).mockResolvedValue(enrollment);
    render(
        <Onboarding
            state={enrolledState}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    await screen.findByText(enrollment.onboarding_url);
    expect(api).toHaveBeenCalledWith("/api/client-enrollment");
    expect(screen.queryByLabelText("Group")).toBeNull();
    expect(screen.getByLabelText("Application")).toHaveProperty(
        "value",
        "both",
    );
    fireEvent.click(
        screen.getByRole("button", {
            name: "Copy organization onboarding link",
        }),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        enrollment.onboarding_url,
    );
    fireEvent.change(screen.getByLabelText("Application"), {
        target: { value: "codex" },
    });
    fireEvent.click(
        screen.getByRole("button", { name: "Download setup & copy command" }),
    );
    await waitFor(() =>
        expect(downloadSetup).toHaveBeenCalledWith(
            "/api/client-enrollment-bundle?client=codex",
        ),
    );
    await screen.findByLabelText("Setup command");
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        expect.stringContaining("/bin/bash -c"),
    );
    expect(screen.getByText(/Background refresh starts/)).toBeTruthy();
    expect(screen.queryByText(/enable-refresh/)).toBeNull();
});

it("enrollment failures offer retry without a partial ZIP or a group override", async () => {
    vi.mocked(api).mockRejectedValueOnce(
        new Error("Publish model access first"),
    );
    vi.mocked(api).mockResolvedValueOnce({ ...enrollment, clients: ["codex"] });
    render(
        <Onboarding
            state={{
                ...enrolledState,
                viewer: { ...state.viewer, role: "developer" },
            }}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    await screen.findByText("Publish model access first");
    expect(
        screen.queryByRole("button", { name: "Download setup & copy command" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Manage members" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(enrollment.onboarding_url);
    expect(screen.getByLabelText("Application")).toHaveProperty(
        "value",
        "codex",
    );
    expect(screen.queryByRole("option", { name: "Claude" })).toBeNull();
    expect(api).toHaveBeenLastCalledWith("/api/client-enrollment");
});

it.each([false])(
    "provides a fixed, explicit Mac security handoff for legacy ZIPs (enrollment=%s)",
    async (enroll) => {
        vi.mocked(api).mockResolvedValue(enroll ? enrollment : profile);
        render(
            <Onboarding
                state={enroll ? enrolledState : state}
                onMembers={vi.fn()}
                notify={notify}
            />,
        );
        const help = await screen.findByRole("region", {
            name: "Mac installation and security",
        });
        expect(help.textContent).toContain("Open Anyway");
        expect(help.textContent).toContain("not Apple-signed or notarized");
        expect(help.textContent).toContain("do not remove quarantine");
        expect(help.textContent).toContain("contact IT");
        const link = screen.getByRole("link", {
            name: "Open macOS Privacy & Security",
        });
        expect(link.getAttribute("href")).toBe(MAC_SECURITY_SETTINGS);
        expect(link.getAttribute("href")).not.toContain("portal.example.com");
        expect(notify).not.toHaveBeenCalled();
        fireEvent.click(
            screen.getByRole("button", {
                name: enroll
                    ? "Download setup & copy command"
                    : "Download Mac setup ZIP",
            }),
        );
        await waitFor(() =>
            expect(notify).toHaveBeenCalledWith(
                expect.stringContaining("Apple approval if prompted"),
            ),
        );
        expect(notify).not.toHaveBeenCalledWith(
            expect.stringContaining("SETUP COMPLETE"),
        );
    },
);

it("discards a pending enrollment response when published state changes", async () => {
    let resolveOld!: (value: unknown) => void;
    vi.mocked(api).mockReturnValueOnce(
        new Promise((resolve) => {
            resolveOld = resolve;
        }),
    );
    vi.mocked(api).mockResolvedValueOnce({ ...enrollment, clients: ["codex"] });
    const view = render(
        <Onboarding
            state={enrolledState}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    view.rerender(
        <Onboarding
            state={{ ...enrolledState, revision: 2 }}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    await screen.findByText(enrollment.onboarding_url);
    resolveOld(enrollment);
    await waitFor(() =>
        expect(screen.getByLabelText("Application")).toHaveProperty(
            "value",
            "codex",
        ),
    );
    expect(
        screen.queryByRole("option", { name: "Claude and Codex" }),
    ).toBeNull();
});

it("keeps a visible command and retry copy button when clipboard access fails", async () => {
    vi.mocked(api).mockResolvedValue(enrollment);
    vi.mocked(navigator.clipboard.writeText).mockRejectedValue(
        new Error("Clipboard denied"),
    );
    render(
        <Onboarding
            state={enrolledState}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    fireEvent.click(
        await screen.findByRole("button", {
            name: "Download setup & copy command",
        }),
    );
    await screen.findByLabelText("Setup command");
    await waitFor(() =>
        expect(notify).toHaveBeenCalledWith(
            expect.stringContaining("clipboard access was unavailable"),
            true,
        ),
    );
    expect(screen.getByRole("button", { name: "Copy command" })).toBeTruthy();
    expect(
        screen.queryByRole("region", { name: "Mac installation and security" }),
    ).toBeNull();
});

it("offers retry after a rejected download without presenting an install command", async () => {
    vi.mocked(api).mockResolvedValue(enrollment);
    vi.mocked(downloadSetup).mockRejectedValue(
        new Error("Access no longer published"),
    );
    render(
        <Onboarding
            state={enrolledState}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    fireEvent.click(
        await screen.findByRole("button", {
            name: "Download setup & copy command",
        }),
    );
    await waitFor(() =>
        expect(notify).toHaveBeenCalledWith("Access no longer published", true),
    );
    expect(screen.queryByLabelText("Setup command")).toBeNull();
    expect(
        screen
            .getByRole("button", { name: "Download setup & copy command" })
            .hasAttribute("disabled"),
    ).toBe(false);
});

it("discards a prepared command when application selection changes", async () => {
    vi.mocked(api).mockResolvedValue(enrollment);
    render(
        <Onboarding
            state={enrolledState}
            onMembers={vi.fn()}
            notify={notify}
        />,
    );
    fireEvent.click(
        await screen.findByRole("button", {
            name: "Download setup & copy command",
        }),
    );
    await screen.findByLabelText("Setup command");
    fireEvent.change(screen.getByLabelText("Application"), {
        target: { value: "codex" },
    });
    expect(screen.queryByLabelText("Setup command")).toBeNull();
});
