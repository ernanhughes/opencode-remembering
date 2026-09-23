import { describe, expect, test } from "bun:test";

import { establishFrame } from "./service";
import { validateProjectFrame } from "./model";

const FRAME = validateProjectFrame(
  { version: "project-frame-v0.1", work_types: ["backend", "frontend"], objective: "Ship the product.", constraints: ["no downtime"] },
  "test",
);

describe("work framing", () => {
  test("no frame configured disables framing without failure", () => {
    const result = establishFrame(null, null, { mode: "auto", signals: [] });
    expect(result.applied).toBe(false);
  });

  test("invalid frame fails closed", () => {
    expect(() => establishFrame(null, "project frame is broken.", { mode: "auto" })).toThrow(/FRAME_INVALID/);
    expect(() => validateProjectFrame({ work_types: [] }, "test")).toThrow(/FRAME_INVALID/);
  });

  test("explicit known work type declares hard control", () => {
    const result = establishFrame(FRAME, null, { mode: "explicit", work_type: "backend" });
    expect(result.establishment?.establishment).toBe("declared");
    expect(result.control).toBe("hard");
  });

  test("explicit unknown work type is query-only", () => {
    const result = establishFrame(FRAME, null, { mode: "explicit", work_type: "quantum" });
    expect(result.control).toBe("query_only");
  });

  test("none mode disables framing", () => {
    const result = establishFrame(FRAME, null, { mode: "none" });
    expect(result.applied).toBe(false);
  });

  test("auto signal match corroborates; conflict is query-only", () => {
    const matched = establishFrame(FRAME, null, {
      mode: "auto",
      prior_work_type: "backend",
      signals: [{ signal_id: "s1", kind: "user", text: "fix the backend endpoint" }],
    });
    expect(matched.establishment?.workType).toBe("backend");
    expect(matched.establishment?.establishment).toBe("corroborated");
    expect(matched.control).toBe("soft");
    const conflict = establishFrame(FRAME, null, {
      mode: "auto",
      signals: [{ signal_id: "s1", kind: "user", text: "backend and frontend work" }],
    });
    expect(conflict.establishment?.establishment).toBe("conflicting");
    expect(conflict.control).toBe("query_only");
  });

  test("insufficient evidence yields unknown without control", () => {
    const result = establishFrame(FRAME, null, { mode: "auto", signals: [] });
    expect(result.establishment?.establishment).toBe("unknown");
    expect(result.control).toBe("query_only");
  });

  test("digest is deterministic", () => {
    const again = validateProjectFrame(
      { version: "project-frame-v0.1", work_types: ["frontend", "backend"], objective: "Ship the product.", constraints: ["no downtime"] },
      "test",
    );
    expect(again.digest).toBe(FRAME.digest);
  });
});
