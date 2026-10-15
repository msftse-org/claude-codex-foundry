import { useEffect, useState } from "react";
import type { Group, Member, Settings, State } from "./types";
import { api } from "./api";
import {
    Drawer,
    DrawerClose,
    Field,
    Modal,
    Notice,
    roleName,
} from "./components";

export function MemberEditor({
    state,
    member,
    editable,
    busy,
    close,
    save,
}: {
    state: State;
    member?: Member;
    editable: boolean;
    busy: boolean;
    close: () => void;
    save: (m: Member) => Promise<void>;
}) {
    const m = member;
    const [grants, setGrants] = useState(m?.extra_models || []);
    return (
        <Drawer
            busy={busy}
            title={m ? m.name : "Add a member"}
            description="Use the object ID from this organization’s Entra directory. Saving creates a draft gateway entitlement; it does not send an invitation or create a directory account."
            onClose={close}
        >
            <form
                className="drawer-form"
                onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    save({
                        id: String(f.get("id")),
                        name: String(f.get("name")),
                        email: String(f.get("email")),
                        role: f.get("role") as Member["role"],
                        status: f.get("status") as Member["status"],
                        group_id: String(f.get("group")),
                        monthly_tokens: Number(f.get("tokens")),
                        extra_models: grants,
                        spend_limit_usd: String(f.get("spend") || "") || null,
                    });
                }}
            >
                <fieldset
                    className="drawer-scroll drawer-fields"
                    disabled={!editable || busy}
                >
                    <Field
                        label="Entra object ID"
                        help="For guests, use their object ID in this tenant."
                    >
                        <input
                            name="id"
                            defaultValue={m?.id}
                            required
                            readOnly={!!m}
                            pattern="[a-fA-F0-9-]{36}"
                        />
                    </Field>
                    <div className="twocol">
                        <Field label="Full name">
                            <input
                                name="name"
                                defaultValue={m?.name}
                                required
                                maxLength={120}
                            />
                        </Field>
                        <Field label="Email">
                            <input
                                name="email"
                                type="email"
                                defaultValue={m?.email}
                                required
                            />
                        </Field>
                    </div>
                    <div className="twocol">
                        <Field label="Role">
                            <select
                                name="role"
                                defaultValue={m?.role || "developer"}
                            >
                                {[
                                    "developer",
                                    ...(state.viewer.role === "group_admin"
                                        ? []
                                        : [
                                              "group_admin",
                                              "auditor",
                                              "billing",
                                              "admin",
                                          ]),
                                    ...(state.viewer.role === "owner"
                                        ? ["owner"]
                                        : []),
                                ].map((r) => (
                                    <option key={r} value={r}>
                                        {roleName(r)}
                                    </option>
                                ))}
                            </select>
                        </Field>
                        <Field label="Primary group">
                            <select
                                name="group"
                                defaultValue={
                                    m?.group_id || state.groups[0]?.id
                                }
                            >
                                {state.groups.map((g) => (
                                    <option key={g.id} value={g.id}>
                                        {g.name}
                                    </option>
                                ))}
                            </select>
                        </Field>
                    </div>
                    <div className="twocol">
                        <Field label="Status">
                            <select
                                name="status"
                                defaultValue={m?.status || "active"}
                            >
                                <option value="active">Active</option>
                                <option value="suspended">Suspended</option>
                            </select>
                        </Field>
                        <Field
                            label="Monthly token limit"
                            help="0 means no personal limit; group and organization limits still apply."
                        >
                            <input
                                name="tokens"
                                type="number"
                                min="0"
                                step="1"
                                defaultValue={m?.monthly_tokens || 0}
                            />
                        </Field>
                    </div>
                    <Field
                        label="Monthly spending limit (USD)"
                        help="Calendar month in UTC. Blank inherits the group allowance; zero blocks new requests. Applies after budget publication."
                    >
                        <input
                            name="spend"
                            type="number"
                            min="0"
                            step="0.000001"
                            defaultValue={m?.spend_limit_usd ?? ""}
                        />
                    </Field>
                    <fieldset className="field modelchoices">
                        <legend>Personal model grants</legend>
                        <p className="footnote">
                            Additional access beyond the primary group. Clear a
                            grant to revoke the exception at the next
                            publication.
                        </p>
                        {state.models.map((model) => (
                            <label className="checkrow" key={model.name}>
                                <input
                                    type="checkbox"
                                    disabled={
                                        !editable ||
                                        (state.viewer.role === "group_admin" &&
                                            !state.groups[0]?.delegated_models?.includes(
                                                model.name,
                                            ) &&
                                            !grants.includes(model.name))
                                    }
                                    checked={grants.includes(model.name)}
                                    onChange={(e) =>
                                        setGrants(
                                            e.target.checked
                                                ? [...grants, model.name]
                                                : grants.filter(
                                                      (id) => id !== model.name,
                                                  ),
                                        )
                                    }
                                />
                                {model.labelOverride}
                            </label>
                        ))}
                    </fieldset>
                    <div className="impact">
                        Changes affect this member’s next gateway requests after
                        publication. Suspending access does not delete their
                        local conversations.
                    </div>
                </fieldset>
                <div className="dialogfooter">
                    <DrawerClose disabled={busy} />
                    <button
                        className="button primary"
                        disabled={!editable || busy}
                        title={
                            !editable
                                ? "Only owners and administrators can edit members."
                                : undefined
                        }
                    >
                        {busy ? "Saving…" : "Save member draft"}
                    </button>
                </div>
            </form>
        </Drawer>
    );
}
export function GroupEditor({
    state,
    group,
    editable,
    busy,
    close,
    save,
}: {
    state: State;
    group?: Group;
    editable: boolean;
    busy: boolean;
    close: () => void;
    save: (g: Group) => Promise<void>;
}) {
    const [models, setModels] = useState(group?.model_ids || []);
    const [delegated, setDelegated] = useState(group?.delegated_models || []);
    const owner = state.viewer.role === "owner";
    const count = state.members.filter((m) => m.group_id === group?.id).length;
    return (
        <Drawer
            busy={busy}
            title={group ? group.name : "Create a group"}
            description="Groups give developers a clear set of models and one shared monthly token limit."
            onClose={close}
        >
            <form
                className="drawer-form"
                onSubmit={(e) => {
                    e.preventDefault();
                    const f = new FormData(e.currentTarget);
                    save({
                        id: group?.id || crypto.randomUUID(),
                        name: String(f.get("name")),
                        description: String(f.get("description")),
                        model_ids: models,
                        default_model: String(f.get("default")),
                        monthly_tokens: Number(f.get("tokens")),
                        budget_usd: Number(f.get("budget")),
                        spend_ceiling_usd: owner
                            ? String(f.get("ceiling") || "") || null
                            : (group?.spend_ceiling_usd ?? null),
                        spend_limit_usd: String(f.get("spend") || "") || null,
                        delegated_models: delegated,
                    });
                }}
            >
                <fieldset
                    className="drawer-scroll drawer-fields"
                    disabled={!editable || busy}
                >
                    <Field label="Group name">
                        <input
                            name="name"
                            defaultValue={group?.name}
                            required
                            maxLength={80}
                        />
                    </Field>
                    <Field label="Description">
                        <input
                            name="description"
                            defaultValue={group?.description}
                            maxLength={500}
                        />
                    </Field>
                    <div className="field">
                        <label>Allowed models</label>
                        <div className="modelchoices">
                            {state.models.map((m) => (
                                <label className="checkrow" key={m.name}>
                                    <input
                                        type="checkbox"
                                        checked={models.includes(m.name)}
                                        disabled={
                                            state.viewer.role ===
                                                "group_admin" &&
                                            !delegated.includes(m.name) &&
                                            !models.includes(m.name)
                                        }
                                        onChange={(e) =>
                                            setModels(
                                                e.target.checked
                                                    ? [...models, m.name]
                                                    : models.filter(
                                                          (x) => x !== m.name,
                                                      ),
                                            )
                                        }
                                    />
                                    {m.labelOverride}
                                    <small>{m.anthropicFamilyTier}</small>
                                </label>
                            ))}
                        </div>
                    </div>
                    <Field label="Default model">
                        <select
                            name="default"
                            key={models.join(",")}
                            defaultValue={
                                models.includes(group?.default_model || "")
                                    ? group?.default_model
                                    : models[0] || ""
                            }
                        >
                            {!models.length && (
                                <option value="">No models selected</option>
                            )}
                            {state.models
                                .filter((m) => models.includes(m.name))
                                .map((m) => (
                                    <option value={m.name} key={m.name}>
                                        {m.labelOverride}
                                    </option>
                                ))}
                        </select>
                    </Field>
                    <div className="twocol">
                        <Field
                            label="Monthly token quota"
                            help="0 leaves this group unlimited; organization limits still apply."
                        >
                            <input
                                name="tokens"
                                type="number"
                                min="0"
                                step="1"
                                defaultValue={group?.monthly_tokens || 0}
                            />
                        </Field>
                        <Field
                            label="Planning budget (USD)"
                            help="A planning threshold, not a hard billing cap."
                        >
                            <input
                                name="budget"
                                type="number"
                                min="0"
                                step="0.01"
                                defaultValue={group?.budget_usd || 0}
                            />
                        </Field>
                    </div>
                    <div className="twocol">
                        <Field
                            label="Owner-assigned ceiling (USD)"
                            help="Only an organization owner can change this maximum."
                        >
                            <input
                                name="ceiling"
                                type="number"
                                min="0"
                                step="0.000001"
                                disabled={!owner}
                                defaultValue={group?.spend_ceiling_usd ?? ""}
                            />
                        </Field>
                        <Field
                            label="Group spending limit (USD)"
                            help="Blank uses the ceiling. Zero blocks new requests. Cannot exceed the owner-assigned ceiling."
                        >
                            <input
                                name="spend"
                                type="number"
                                min="0"
                                step="0.000001"
                                defaultValue={group?.spend_limit_usd ?? ""}
                            />
                        </Field>
                    </div>
                    {owner && (
                        <fieldset className="field modelchoices">
                            <legend>Models this group may grant</legend>
                            <p className="footnote">
                                The group administrator can enable these models
                                for the team or individual developers.
                            </p>
                            {state.models.map((model) => (
                                <label className="checkrow" key={model.name}>
                                    <input
                                        type="checkbox"
                                        checked={delegated.includes(model.name)}
                                        onChange={(e) =>
                                            setDelegated(
                                                e.target.checked
                                                    ? [...delegated, model.name]
                                                    : delegated.filter(
                                                          (id) =>
                                                              id !== model.name,
                                                      ),
                                            )
                                        }
                                    />
                                    {model.labelOverride}
                                </label>
                            ))}
                        </fieldset>
                    )}
                    <div className="impact">
                        <strong>
                            {count} {count === 1 ? "person" : "people"}
                        </strong>{" "}
                        in this group will receive the reviewed model access
                        after publication. {models.length} models are selected.
                    </div>
                </fieldset>
                <div className="dialogfooter">
                    <DrawerClose disabled={busy} />
                    <button
                        className="button primary"
                        disabled={!editable || busy}
                        title={
                            !editable
                                ? "Only owners and administrators can edit groups."
                                : undefined
                        }
                    >
                        {busy ? "Saving…" : "Save group draft"}
                    </button>
                </div>
            </form>
        </Drawer>
    );
}
export function SettingsForm({
    settings,
    admin,
    finance,
    busy,
    save,
    organization,
}: {
    settings: Settings;
    admin: boolean;
    finance: boolean;
    busy: boolean;
    save: (s: Settings) => Promise<boolean>;
    organization: boolean;
}) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(settings);
    const fields: {
        key: keyof Settings;
        label: string;
        help: string;
        enabled: boolean;
    }[] = organization
        ? [
              {
                  key: "name",
                  label: "Organization name",
                  help: "Shown in this console and publication confirmations.",
                  enabled: admin,
              },
          ]
        : [
              {
                  key: "monthly_budget_usd",
                  label: "Monthly planning budget (USD)",
                  help: "A planning threshold. 0 means no planning budget is set.",
                  enabled: finance,
              },
              {
                  key: "monthly_tokens",
                  label: "Organization token quota",
                  help: "Monthly allowance across the gateway. 0 means unlimited.",
                  enabled: admin,
              },
              {
                  key: "member_tokens_per_minute",
                  label: "Tokens per minute, per developer",
                  help: "Controls token bursts. 0 adds no per-person rate limit.",
                  enabled: admin,
              },
              {
                  key: "member_requests_per_minute",
                  label: "Requests per minute, per developer",
                  help: "Includes inference and token-counting requests. 0 adds no rate limit.",
                  enabled: admin,
              },
          ];
    return (
        <div className="settings">
            <div className="sheet">
                {fields.map((field) => (
                    <div className="settingrow" key={field.key}>
                        <div>
                            <strong>{field.label}</strong>
                            <p>{field.help}</p>
                        </div>
                        <span className="num">
                            {field.key === "name"
                                ? settings.name
                                : Number(settings[field.key]) === 0
                                  ? "Not set"
                                  : Number(
                                        settings[field.key],
                                    ).toLocaleString()}
                        </span>
                    </div>
                ))}
            </div>
            <div className="actions">
                <button
                    className="button primary"
                    disabled={!fields.some((f) => f.enabled)}
                    onClick={() => {
                        setDraft(settings);
                        setEditing(true);
                    }}
                >
                    {organization
                        ? "Edit organization"
                        : "Edit budget & limits"}
                </button>
            </div>
            {editing && (
                <Drawer
                    title={
                        organization
                            ? "Organization settings"
                            : "Budget & limits"
                    }
                    description="Save a draft, then review and publish gateway limits when ready. Planning budgets do not stop inference."
                    onClose={() => setEditing(false)}
                    busy={busy}
                >
                    <form
                        className="drawer-form"
                        onSubmit={async (e) => {
                            e.preventDefault();
                            if (await save(draft)) setEditing(false);
                        }}
                    >
                        <div className="drawer-scroll">
                            {fields.map((field) => (
                                <Field
                                    key={field.key}
                                    label={field.label}
                                    help={field.help}
                                >
                                    <input
                                        type={
                                            field.key === "name"
                                                ? "text"
                                                : "number"
                                        }
                                        min="0"
                                        maxLength={
                                            field.key === "name"
                                                ? 120
                                                : undefined
                                        }
                                        required
                                        step={
                                            field.key === "monthly_budget_usd"
                                                ? "0.01"
                                                : "1"
                                        }
                                        value={String(draft[field.key] ?? "")}
                                        disabled={!field.enabled || busy}
                                        onChange={(e) =>
                                            setDraft({
                                                ...draft,
                                                [field.key]:
                                                    field.key === "name"
                                                        ? e.target.value
                                                        : Number(
                                                              e.target.value,
                                                          ),
                                            })
                                        }
                                    />
                                </Field>
                            ))}
                        </div>
                        <div className="dialogfooter">
                            <DrawerClose disabled={busy} />
                            <button
                                className="button primary"
                                disabled={
                                    busy ||
                                    JSON.stringify(draft) ===
                                        JSON.stringify(settings)
                                }
                            >
                                {busy ? "Saving…" : "Save changes"}
                            </button>
                        </div>
                    </form>
                </Drawer>
            )}
        </div>
    );
}
export function PublishDialog({
    state,
    editable,
    close,
    completed,
}: {
    state: State;
    editable: boolean;
    close: () => void;
    completed: () => Promise<void>;
}) {
    const [impact, setImpact] = useState<{
        active_members: number;
        suspended_members: number;
        groups: number;
        policy_bytes: number;
        note: string;
    }>();
    const [error, setError] = useState("");
    const [name, setName] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        api<typeof impact>("/api/impact")
            .then(setImpact)
            .catch((e) => setError(e.message));
    }, []);
    return (
        <Modal
            title="Review gateway publication"
            description="This applies the current access and token policies to the configured gateway. Review the affected people before proceeding."
            onClose={() => {
                if (!busy) close();
            }}
        >
            {error && <Notice tone="error">{error}</Notice>}
            {impact ? (
                <>
                    <div className="impact">
                        <h3>
                            {impact.active_members} active{" "}
                            {impact.active_members === 1 ? "member" : "members"}{" "}
                            · {impact.groups} groups
                        </h3>
                        <p>
                            {impact.suspended_members} suspended members will be
                            denied. Model access and limits come from this
                            reviewed draft.
                        </p>
                    </div>
                    <p>{impact.note}</p>
                    <p className="mono muted">{state.gateway_url}</p>
                    <Notice>
                        Foundry will authenticate the gateway’s managed
                        identity. Ensure developers cannot bypass these controls
                        through direct Foundry RBAC.
                    </Notice>
                    <Field label={`Type “${state.settings.name}” to publish`}>
                        <input
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            autoComplete="off"
                        />
                    </Field>
                    <div className="dialogfooter">
                        <button
                            className="button"
                            onClick={close}
                            disabled={busy}
                        >
                            Keep as draft
                        </button>
                        <button
                            className="button primary"
                            disabled={
                                !editable ||
                                busy ||
                                name !== state.settings.name
                            }
                            onClick={async () => {
                                setBusy(true);
                                setError("");
                                try {
                                    await api("/api/publish", {
                                        method: "POST",
                                        body: JSON.stringify({
                                            revision: state.revision,
                                            organization: name,
                                        }),
                                    });
                                    await completed();
                                } catch (e) {
                                    setError((e as Error).message);
                                } finally {
                                    setBusy(false);
                                }
                            }}
                        >
                            {busy ? "Publishing…" : "Publish gateway policy"}
                        </button>
                    </div>
                </>
            ) : (
                !error && (
                    <div className="loading">
                        <span className="spinner" />
                        Calculating impact…
                    </div>
                )
            )}
        </Modal>
    );
}
