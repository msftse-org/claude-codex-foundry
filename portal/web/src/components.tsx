import {
    createContext,
    useContext,
    useEffect,
    useRef,
    useState,
    type ReactNode,
} from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { CheckCircle, WarningCircle, X, Info } from "@phosphor-icons/react";
export const fmt = (n: number) => new Intl.NumberFormat("en-US").format(n);
export const compact = (n: number) =>
    new Intl.NumberFormat("en-US", {
        notation: "compact",
        maximumFractionDigits: 1,
    }).format(n);
export const date = (s: string) =>
    new Date(s).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
export const roleName = (r: string) =>
    ({
        owner: "Owner",
        group_admin: "Group administrator",
        admin: "Administrator",
        billing: "Billing administrator",
        auditor: "Auditor",
        developer: "Developer",
    })[r] || r;
export function Seal({
    size = 34,
    id = "claude-foundry",
}: {
    size?: number;
    id?: string;
}) {
    const hash = [...id].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);
    const r = 5,
        R = 13 + (hash % 4),
        d = 8;
    const points = Array.from({ length: 900 }, (_, i) => {
        const t = (i / 899) * Math.PI * 2 * r;
        return [
            (R - r) * Math.cos(t) + d * Math.cos(((R - r) * t) / r),
            (R - r) * Math.sin(t) - d * Math.sin(((R - r) * t) / r),
        ];
    });
    const path =
        points
            .map(
                ([x, y], i) =>
                    `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`,
            )
            .join(" ") + "Z";
    return (
        <svg
            className="seal"
            width={size}
            height={size}
            viewBox="-24 -24 48 48"
            aria-hidden="true"
        >
            {Array.from({ length: size < 32 ? 5 : 11 }, (_, i) => (
                <path
                    key={i}
                    d={path}
                    transform={`rotate(${i * 32.72})`}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={size < 40 ? 0.4 : 0.09}
                />
            ))}
        </svg>
    );
}
export function Status({
    children,
    tone = "neutral",
}: {
    children: ReactNode;
    tone?: "good" | "warn" | "bad" | "neutral";
}) {
    return (
        <span className={`status ${tone}`}>
            {tone === "good" ? (
                <CheckCircle size={13} />
            ) : tone === "bad" || tone === "warn" ? (
                <WarningCircle size={13} />
            ) : null}
            {children}
        </span>
    );
}
export function Notice({
    children,
    tone = "",
}: {
    children: ReactNode;
    tone?: string;
}) {
    return (
        <div
            className={`notice ${tone}`}
            role={tone === "error" ? "alert" : undefined}
        >
            <Info size={18} />
            <div>{children}</div>
        </div>
    );
}
export function PageHead({
    title,
    description,
    children,
}: {
    title: string;
    description: string;
    children?: ReactNode;
}) {
    return (
        <div className="pagehead">
            <div>
                <h1>{title}</h1>
                <p>{description}</p>
            </div>
            <div className="actions">{children}</div>
        </div>
    );
}
export function Empty({
    title,
    children,
    action,
}: {
    title: string;
    children: ReactNode;
    action?: ReactNode;
}) {
    return (
        <div className="empty">
            <Info size={22} className="muted" />
            <h2>{title}</h2>
            <p>{children}</p>
            {action}
        </div>
    );
}
export function Modal({
    title,
    description,
    children,
    onClose,
}: {
    title: string;
    description: string;
    children: ReactNode;
    onClose: () => void;
}) {
    const returnFocus = useRef(document.activeElement as HTMLElement | null);
    return (
        <Dialog.Root
            open
            onOpenChange={(open) => {
                if (!open) onClose();
            }}
        >
            <Dialog.Portal>
                <Dialog.Overlay className="overlay" />
                <Dialog.Content
                    className="dialog"
                    onCloseAutoFocus={(event) => {
                        event.preventDefault();
                        if (returnFocus.current?.isConnected)
                            returnFocus.current.focus();
                        else document.getElementById("main")?.focus();
                    }}
                >
                    <Dialog.Title asChild>
                        <h2>{title}</h2>
                    </Dialog.Title>
                    <Dialog.Description className="description">
                        {description}
                    </Dialog.Description>
                    <Dialog.Close asChild>
                        <button
                            className="iconbutton close"
                            aria-label="Close dialog"
                        >
                            <X size={20} />
                        </button>
                    </Dialog.Close>
                    {children}
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
const DrawerCloseContext = createContext<() => void>(() => {});
export function DrawerClose({
    children = "Cancel",
    disabled = false,
}: {
    children?: ReactNode;
    disabled?: boolean;
}) {
    const close = useContext(DrawerCloseContext);
    return (
        <button
            type="button"
            className="button"
            onClick={close}
            disabled={disabled}
        >
            {children}
        </button>
    );
}
export function Drawer({
    title,
    description,
    children,
    onClose,
    busy = false,
    protect = true,
}: {
    title: string;
    description: string;
    children: ReactNode;
    onClose: () => void;
    busy?: boolean;
    protect?: boolean;
}) {
    const returnFocus = useRef(document.activeElement as HTMLElement | null);
    const [open, setOpen] = useState(true);
    const [dirty, setDirty] = useState(false);
    const [confirm, setConfirm] = useState(false);
    const closeRef = useRef(onClose);
    closeRef.current = onClose;
    useEffect(() => {
        if (open) return;
        const timer = window.setTimeout(() => closeRef.current(), 180);
        return () => window.clearTimeout(timer);
    }, [open]);
    const close = () => {
        if (busy) return;
        if (protect && dirty) setConfirm(true);
        else setOpen(false);
    };
    return (
        <DrawerCloseContext.Provider value={close}>
            <Dialog.Root
                open={open}
                onOpenChange={(next) => {
                    if (!next) close();
                }}
            >
                <Dialog.Portal>
                    <Dialog.Overlay className="overlay drawer-overlay" />
                    <Dialog.Content
                        className="drawer"
                        onChangeCapture={() => setDirty(true)}
                        onCloseAutoFocus={(event) => {
                            event.preventDefault();
                            if (returnFocus.current?.isConnected)
                                returnFocus.current.focus();
                            else document.getElementById("main")?.focus();
                        }}
                    >
                        <header className="drawer-header">
                            <span className="eyebrow">
                                Claude & Codex / Workspace
                            </span>
                            <Dialog.Title asChild>
                                <h2>{title}</h2>
                            </Dialog.Title>
                            <Dialog.Description>
                                {description}
                            </Dialog.Description>
                            <button
                                className="iconbutton close"
                                aria-label="Close drawer"
                                onClick={close}
                                disabled={busy}
                            >
                                <X size={20} />
                            </button>
                        </header>
                        {confirm && (
                            <div className="drawer-confirm" role="alert">
                                <strong>Discard unsaved changes?</strong>
                                <p>
                                    Your saved configuration will stay as it is.
                                </p>
                                <div className="actions">
                                    <button
                                        className="button"
                                        onClick={() => setConfirm(false)}
                                    >
                                        Keep editing
                                    </button>
                                    <button
                                        className="button danger"
                                        onClick={() => setOpen(false)}
                                    >
                                        Discard changes
                                    </button>
                                </div>
                            </div>
                        )}
                        <div className="drawer-content" aria-busy={busy}>
                            {children}
                        </div>
                    </Dialog.Content>
                </Dialog.Portal>
            </Dialog.Root>
        </DrawerCloseContext.Provider>
    );
}
export function Field({
    label,
    help,
    children,
}: {
    label: string;
    help?: string;
    children: ReactNode;
}) {
    return (
        <div className="field">
            <label>
                {label}
                {children}
            </label>
            {help && <small>{help}</small>}
        </div>
    );
}
export function Pager({
    count,
    page,
    onChange,
}: {
    count: number;
    page: number;
    onChange: (page: number) => void;
}) {
    if (count <= 50) return null;
    const pages = Math.ceil(count / 50);
    return (
        <div className="toolbar" style={{ marginTop: 16 }}>
            <span className="muted">
                {fmt(page * 50 + 1)}–{fmt(Math.min((page + 1) * 50, count))} of{" "}
                {fmt(count)}
            </span>
            <div className="actions">
                <button
                    className="button"
                    disabled={page === 0}
                    onClick={() => onChange(page - 1)}
                >
                    Previous
                </button>
                <span className="muted">
                    Page {page + 1} of {pages}
                </span>
                <button
                    className="button"
                    disabled={page + 1 >= pages}
                    onClick={() => onChange(page + 1)}
                >
                    Next
                </button>
            </div>
        </div>
    );
}
