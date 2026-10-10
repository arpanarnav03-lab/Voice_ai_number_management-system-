import { describe, expect, it } from "vitest";
import { canStartTest } from "./testRules";

const ok = { selectedCount: 3, agentChosen: true, reviewerPhone: "+919999999902" };

describe("canStartTest", () => {
  it("is enabled when a selection, an agent and a valid reviewer phone are present", () => {
    expect(canStartTest(ok)).toBe(true);
  });
  it("stays disabled with nothing selected", () => {
    expect(canStartTest({ ...ok, selectedCount: 0 })).toBe(false);
  });
  it("stays disabled until a test agent is chosen", () => {
    expect(canStartTest({ ...ok, agentChosen: false })).toBe(false);
  });
  it("stays disabled for a missing or invalid reviewer phone", () => {
    for (const bad of ["", "9999999902", "+91999999990", "+9199999999021", "+449999999902", "+91 99999 99902x"]) {
      expect(canStartTest({ ...ok, reviewerPhone: bad })).toBe(false);
    }
  });
  it("stays disabled while a request is in flight", () => {
    expect(canStartTest({ ...ok, busy: true })).toBe(false);
  });
});
