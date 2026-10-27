import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import {
    mkdtempSync,
    mkdirSync,
    readFileSync,
    rmSync,
    writeFileSync,
    existsSync,
    realpathSync,
    symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, it } from "vitest";
import { setupFilename, terminalSetupCommand } from "./setupCommand";

const roots = [];
afterEach(() =>
    roots
        .splice(0)
        .forEach((root) => rmSync(root, { recursive: true, force: true })),
);

function fixture() {
    const root = mkdtempSync(join(tmpdir(), "setup-command-test-"));
    roots.push(root);
    const payload = join(root, "payload");
    mkdirSync(join(payload, "claude-foundry/scripts/macos"), {
        recursive: true,
    });
    writeFileSync(
        join(payload, "claude-foundry/scripts/macos/install.sh"),
        '#!/bin/bash\nprintf "%s" "$0" > "$SETUP_TEST_MARKER"\nexit "${SETUP_TEST_EXIT:-0}"\n',
    );
    // Spaces and shell syntax are data, including when a user supplies another path.
    const archive = join(root, "setup $(touch unexpected) ' copy.zip");
    execFileSync("zip", ["-qr", archive, "claude-foundry"], { cwd: payload });
    const digest = createHash("sha256")
        .update(readFileSync(archive))
        .digest("hex");
    const temporary = join(root, "private ' temporary");
    mkdirSync(temporary);
    const marker = join(root, "installed");
    const run = (path = archive, extraEnv = {}) =>
        spawnSync("/bin/bash", ["-c", terminalSetupCommand(digest)], {
            input: path + "\n",
            encoding: "utf8",
            cwd: root,
            env: {
                ...process.env,
                TMPDIR: temporary,
                SETUP_TEST_MARKER: marker,
                ...extraEnv,
            },
        });
    return { root, archive, temporary, marker, run };
}

it.each([
    "",
    "A".repeat(64),
    "x".repeat(64),
    "a".repeat(63),
    "$(touch unexpected)",
])("refuses untrusted checksum syntax: %s", (value) => {
    expect(() => terminalSetupCommand(value)).toThrow("Invalid setup checksum");
    expect(() => setupFilename(value)).toThrow();
});

it("rejects a changed ZIP before extracting or running any installer", () => {
    const item = fixture();
    writeFileSync(item.archive, "damaged download");
    const result = item.run();
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("checksum does not match");
    expect(existsSync(item.marker)).toBe(false);
    expect(
        execFileSync("ls", ["-A", item.temporary], { encoding: "utf8" }),
    ).toBe("");
});

it("reports a missing ZIP without invoking setup", () => {
    const item = fixture();
    const result = item.run(join(item.root, "absent.zip"));
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("Download setup again");
    expect(existsSync(item.marker)).toBe(false);
});

it.runIf(process.platform === "darwin")(
    "runs a verified ZIP from another folder and cleans its temporary copy",
    () => {
        const item = fixture();
        const result = item.run();
        expect(result.status, result.stderr).toBe(0);
        expect(result.stdout).toContain("Setup ZIP verified");
        expect(existsSync(item.marker)).toBe(true);
        expect(existsSync(join(item.root, "unexpected"))).toBe(false);
        expect(existsSync(item.archive)).toBe(true);
        expect(
            execFileSync("ls", ["-A", item.temporary], { encoding: "utf8" }),
        ).toBe("");
    },
);

it.runIf(process.platform === "darwin")(
    "passes physical paths to the installer even when macOS supplies a temporary-directory symlink",
    () => {
        const item = fixture();
        const alias = join(item.root, "linked-temp");
        symlinkSync(item.temporary, alias);
        const result = item.run(item.archive, { TMPDIR: alias });
        expect(result.status, result.stderr).toBe(0);
        expect(
            readFileSync(item.marker, "utf8").startsWith(
                realpathSync(item.temporary) + "/",
            ),
        ).toBe(true);
        expect(readFileSync(item.marker, "utf8")).not.toContain("linked-temp");
    },
);

it.runIf(process.platform === "darwin")(
    "propagates an installer failure and still removes temporary files",
    () => {
        const item = fixture();
        const result = item.run(item.archive, { SETUP_TEST_EXIT: "7" });
        expect(result.status, result.stderr).toBe(7);
        expect(
            execFileSync("ls", ["-A", item.temporary], { encoding: "utf8" }),
        ).toBe("");
    },
);
