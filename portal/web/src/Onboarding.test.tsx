// @vitest-environment jsdom
import {
    cleanup,
    fireEvent,
    render,
    screen,
    waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, downloadFile } from "./api";
import { Onboarding } from "./Onboarding";
import type { State } from "./types";

vi.mock("./api", () => ({
    api: vi.fn(),
    download: vi.fn(),
    downloadFile: vi.fn(),
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
            "/api/client-bundle?group_id=platform",
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
        "/api/client-bundle?group_id=other",
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
    expect(api).toHaveBeenLastCalledWith("/api/client-profile");
});
