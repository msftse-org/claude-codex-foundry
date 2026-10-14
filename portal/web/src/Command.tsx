import { useState } from "react";
import { ArrowRight } from "@phosphor-icons/react";
import type { State } from "./types";
import { Empty, Modal } from "./components";

type NavigationPage = { id: string; name: string };
export function Command({
    state,
    pages,
    close,
    navigate,
}: {
    state: State;
    pages: NavigationPage[];
    close: () => void;
    navigate: (id: string, q?: string) => void;
}) {
    const [query, setQuery] = useState("");
    const [selected, setSelected] = useState(0);
    const q = query.trim().toLowerCase();
    const results = [
        ...pages
            .filter((p) => p.name.toLowerCase().includes(q))
            .map((p) => ({
                id: `page-${p.id}`,
                label: p.name,
                detail: "Page",
                go: () => navigate(p.id),
            })),
        ...(q && state.viewer.role !== "developer"
            ? state.members
                  .filter((m) =>
                      `${m.name} ${m.email}`.toLowerCase().includes(q),
                  )
                  .slice(0, 5)
                  .map((m) => ({
                      id: `member-${m.id}`,
                      label: m.name,
                      detail: m.email,
                      go: () => navigate("members", m.email),
                  }))
            : []),
    ];
    return (
        <Modal
            title="Find your way"
            description="Search pages and people. Use the arrow keys to move and Enter to open."
            onClose={close}
        >
            <input
                autoFocus
                className="commandinput"
                aria-label="Search pages and people"
                placeholder="Where would you like to go?"
                value={query}
                onChange={(e) => {
                    setQuery(e.target.value);
                    setSelected(0);
                }}
                onKeyDown={(e) => {
                    if (e.key === "ArrowDown") {
                        e.preventDefault();
                        setSelected((i) => Math.min(i + 1, results.length - 1));
                    }
                    if (e.key === "ArrowUp") {
                        e.preventDefault();
                        setSelected((i) => Math.max(i - 1, 0));
                    }
                    if (e.key === "Enter") {
                        e.preventDefault();
                        results[selected]?.go();
                    }
                }}
            />
            <div className="commandresults">
                {results.map((r, i) => (
                    <button
                        key={r.id}
                        className={`commanditem ${i === selected ? "selected" : ""}`}
                        onClick={r.go}
                        onMouseEnter={() => setSelected(i)}
                    >
                        <span>
                            {r.label}
                            <small>{r.detail}</small>
                        </span>
                        <ArrowRight />
                    </button>
                ))}
                {!results.length && (
                    <Empty title="No results">
                        Try a page name, a person’s name, or their email.
                    </Empty>
                )}
            </div>
        </Modal>
    );
}
