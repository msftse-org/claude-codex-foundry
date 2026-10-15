import { describe, expect, it } from "vitest";
import { formatCaptured } from "./content-logs-format";

describe("native captured fragments", () => {
    it("pretty prints JSON strings without discarding original message fields", () => {
        const original = {
            input: [{ role: "user", content: "<script>not HTML</script>" }],
        };
        expect(JSON.parse(formatCaptured(JSON.stringify(original)))).toEqual(
            original,
        );
    });
    it("preserves partial JSON and literal text verbatim", () => {
        expect(formatCaptured('{"input":"unfinished')).toBe(
            '{"input":"unfinished',
        );
        expect(formatCaptured("hello\nworld")).toBe("hello\nworld");
    });
});
