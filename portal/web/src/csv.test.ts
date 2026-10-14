import { describe, expect, it } from "vitest";
import { csvText } from "./csv";

describe("administrative CSV exports", () => {
    it("neutralizes formulas in both headings and untrusted directory fields", () => {
        const result = csvText([
            {
                "=heading": '  =HYPERLINK("https://example.com")',
                email: "@SUM(1,2)",
                name: "+cmd",
                note: "\tformula",
            },
        ]);
        expect(result).toContain('"\'=heading"');
        expect(result).toContain('"\'  =HYPERLINK(""https://example.com"")"');
        expect(result).toContain('"\'@SUM(1,2)"');
        expect(result).toContain('"\'+cmd"');
        expect(result).toContain('"\'\tformula"');
    });
    it("preserves Unicode, delimiters, newlines and absent values", () => {
        expect(
            csvText([{ name: "Zoë, 李", note: 'One\n"Two"', missing: null }]),
        ).toBe('\ufeff"name","note","missing"\r\n"Zoë, 李","One\n""Two""",""');
        expect(csvText([])).toBe("");
    });
});
