import { useState } from "react";
import type { Telemetry } from "./types";
import { Empty, fmt } from "./components";

export function UsageChart({ usage }: { usage: Telemetry }) {
    const [table, setTable] = useState(false);
    const max = Math.max(...usage.daily.map((d) => d.requests), 1);
    return (
        <div className="sheet">
            <div className="chartbox">
                {usage.daily.length ? (
                    <>
                        {table ? (
                            <table>
                                <thead>
                                    <tr>
                                        <th>Date</th>
                                        <th className="numeric">Requests</th>
                                        <th className="numeric">Errors</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {usage.daily.map((d) => (
                                        <tr key={d.key}>
                                            <td>{d.key}</td>
                                            <td className="numeric">
                                                {fmt(d.requests)}
                                            </td>
                                            <td className="numeric">
                                                {fmt(d.errors)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        ) : (
                            <>
                                <div
                                    className="chart"
                                    role="img"
                                    aria-label="Daily gateway requests. Use View as table for exact values."
                                >
                                    {usage.daily.map((d) => (
                                        <div className="barcol" key={d.key}>
                                            <div
                                                className="bar"
                                                title={`${d.key}: ${d.requests} requests`}
                                                style={{
                                                    height: `${(d.requests / max) * 100}%`,
                                                }}
                                            />
                                        </div>
                                    ))}
                                </div>
                                <div className="chartlabels">
                                    <span>{usage.daily[0]?.key}</span>
                                    <span>{usage.daily.at(-1)?.key}</span>
                                </div>
                            </>
                        )}
                        <button
                            className="button text"
                            style={{ marginTop: 14 }}
                            onClick={() => setTable(!table)}
                        >
                            {table ? "View as chart" : "View as table"}
                        </button>
                    </>
                ) : (
                    <Empty
                        title={
                            usage.status === "loading"
                                ? "Loading activity…"
                                : "Ready for the first request"
                        }
                    >
                        Gateway activity will appear here once it reaches
                        Application Insights.
                    </Empty>
                )}
            </div>
        </div>
    );
}
