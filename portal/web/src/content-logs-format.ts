/** Pretty-print complete native JSON; preserve incomplete/plain fragments verbatim. */
export function formatCaptured(value: unknown): string {
    if (typeof value === "string") {
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch {
            return value;
        }
    }
    return JSON.stringify(value, null, 2);
}
