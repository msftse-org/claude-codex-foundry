/** CSV exports are quoted and neutralize spreadsheet formula injection. */
export function csvText(rows: Record<string, unknown>[]): string {
    if (!rows.length) return "";
    const keys = Object.keys(rows[0]);
    const quote = (value: unknown) => {
        let text = String(value ?? "");
        if (/^\s*[=+@\-]/.test(text) || /^[\t\r\n]/.test(text))
            text = "'" + text;
        return '"' + text.replaceAll('"', '""') + '"';
    };
    return (
        "\ufeff" +
        [
            keys.map(quote).join(","),
            ...rows.map((row) => keys.map((key) => quote(row[key])).join(",")),
        ].join("\r\n")
    );
}
