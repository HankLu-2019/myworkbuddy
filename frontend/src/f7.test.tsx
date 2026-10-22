import { describe, expect, it } from "vitest";
import { SOURCE_LABELS } from "./components/ui";

describe("Merged changes as knowledge (08 §F7)", () => {
  it("labels them as changes wherever sources are shown", () => {
    expect(SOURCE_LABELS.change).toBe("Change");
  });
});
