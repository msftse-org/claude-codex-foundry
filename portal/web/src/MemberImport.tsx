import { useState } from "react";
import { api, exportCsv } from "./api";
import { Drawer, DrawerClose, Field, Notice, fmt } from "./components";
import type { Member, State } from "./types";

export function MemberImport({
    state,
    close,
    save,
    busy,
}: {
    state: State;
    close: () => void;
    save: (value: unknown) => Promise<boolean>;
    busy: boolean;
}) {
    const [csv, setCsv] = useState("");
    const [group, setGroup] = useState(state.groups[0]?.id || "");
    const [preview, setPreview] = useState<{
        count: number;
        members: Member[];
    }>();
    const [error, setError] = useState("");
    const [checking, setChecking] = useState(false);
    return (
        <Drawer
            busy={busy || checking}
            title="Import developers"
            description="Add up to 2,500 new developers from a CSV export. Existing members are never overwritten; everyone is assigned the Developer role."
            onClose={close}
        >
            <div className="drawer-scroll">
                {error && <Notice tone="error">{error}</Notice>}
                <Field
                    label="Default group"
                    help="An optional group_id column can override this for individual rows."
                >
                    <select
                        value={group}
                        onChange={(e) => {
                            setGroup(e.target.value);
                            setPreview(undefined);
                        }}
                    >
                        {state.groups.map((g) => (
                            <option key={g.id} value={g.id}>
                                {g.name}
                            </option>
                        ))}
                    </select>
                </Field>
                <Field
                    label="CSV file"
                    help="Required columns: object_id,name,email. Use object IDs from this Entra tenant."
                >
                    <input
                        type="file"
                        accept=".csv,text/csv"
                        onChange={async (e) => {
                            setPreview(undefined);
                            setError("");
                            setCsv("");
                            const file = e.target.files?.[0];
                            if (!file) return;
                            if (file.size > 700000) {
                                setError("Choose a CSV smaller than 700 KB.");
                                return;
                            }
                            setCsv(await file.text());
                        }}
                    />
                </Field>
                <button
                    className="button text"
                    onClick={() =>
                        exportCsv("members-template.csv", [
                            {
                                object_id:
                                    "11111111-1111-4111-8111-111111111111",
                                name: "Example Developer",
                                email: "developer@example.com",
                                group_id: group,
                            },
                        ])
                    }
                >
                    Download CSV template
                </button>
                {preview && (
                    <>
                        <div className="impact">
                            <strong>
                                {fmt(preview.count)} developers will be added to
                                the draft.
                            </strong>
                            <p>
                                They receive gateway access only after you
                                review and publish the policy.
                            </p>
                        </div>
                        <div className="tablewrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Name</th>
                                        <th>Email</th>
                                        <th>Group</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {preview.members.map((m) => (
                                        <tr key={m.id}>
                                            <td>{m.name}</td>
                                            <td>{m.email}</td>
                                            <td>
                                                {
                                                    state.groups.find(
                                                        (g) =>
                                                            g.id === m.group_id,
                                                    )?.name
                                                }
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        {preview.count > 20 && (
                            <p className="footnote">
                                Showing the first 20 validated rows. All{" "}
                                {fmt(preview.count)} rows passed validation.
                            </p>
                        )}
                    </>
                )}
            </div>
            <div className="dialogfooter">
                <DrawerClose disabled={busy || checking} />
                {preview ? (
                    <button
                        className="button primary"
                        disabled={busy}
                        onClick={async () => {
                            if (await save({ csv, group_id: group })) close();
                        }}
                    >
                        {busy
                            ? "Importing…"
                            : `Import ${fmt(preview.count)} developers`}
                    </button>
                ) : (
                    <button
                        className="button primary"
                        disabled={!csv || checking}
                        onClick={async () => {
                            setChecking(true);
                            setError("");
                            try {
                                setPreview(
                                    await api("/api/import-preview", {
                                        method: "POST",
                                        body: JSON.stringify({
                                            csv,
                                            group_id: group,
                                        }),
                                    }),
                                );
                            } catch (e) {
                                setError((e as Error).message);
                            } finally {
                                setChecking(false);
                            }
                        }}
                    >
                        {checking ? "Checking every row…" : "Validate & review"}
                    </button>
                )}
            </div>
        </Drawer>
    );
}
