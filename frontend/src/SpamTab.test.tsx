// @vitest-environment jsdom
import { act } from "react-dom/test-utils";
import { createRoot, Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SpamTab from "./SpamTab";
import { ToastProvider } from "./ui";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

type Row = Record<string, any>;
const row = (i: number, over: Row = {}): Row => ({
  id: i, phone_number: `+91800000${String(i).padStart(4, "0")}`, position: i, status: "available", provider: "vobiz",
  used_by: null, last_tested_at: null, last_test_result: null, never_tested: true, spam_flag: false,
  eligible: true, reason: null, test: null, awaiting_result: false, missing_on_bolna: false, ...over,
});

let rows: Row[];
let selectBodies: unknown[];
let root: Root | null = null;
let container: HTMLElement;

// what the server does: the first row from the top that can be tested is the one that is "Next"
const firstEligible = () => rows.find((r) => r.eligible);
const withNext = () => rows.map((r) => ({ ...r, is_next: r === firstEligible() }));

function mockFetch() {
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const json = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data });
    if (url === "/api/spam/stack" && (init?.method ?? "GET") === "GET") {
      return json({ threshold: 5, items: withNext(), total: rows.length, never_tested: rows.length });
    }
    if (url === "/api/spam/stack/select" && init?.method === "POST") {
      selectBodies.push(init.body ? JSON.parse(init.body as string) : null);
      const first = firstEligible();
      return json({ ids: first ? [first.id] : [], skipped: [] });
    }
    if (url === "/api/agents") return json({ threshold: 5, items: [{ id: 1, name: "Tester", bolna_agent_id: "t" }] });
    if (url === "/api/spam/list") return json({ items: [], released_count: 0, delete_enabled: false });
    return json({ code: "NOT_FOUND", message: "Not found" }, 404);
  }) as any;
}

async function flush() {
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
}
async function poll() {
  await act(async () => { await vi.advanceTimersByTimeAsync(5100); });
}
async function mount() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<ToastProvider><MemoryRouter><SpamTab /></MemoryRouter></ToastProvider>);
  });
  await flush();
  await flush();
}
const boxes = () =>
  Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')).filter((b) => b.getAttribute("aria-label")?.startsWith("Select +91"));
const ticked = () => boxes().filter((b) => b.checked).map((b) => b.getAttribute("aria-label")!.replace("Select ", ""));
const phones = (ids: number[]) => ids.map((i) => row(i).phone_number);
const button = (text: string) => Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === text)!;
async function click(el: Element) {
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  await flush();
}

beforeEach(() => {
  vi.useFakeTimers();
  rows = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => row(i));
  selectBodies = [];
  localStorage.clear();
  sessionStorage.clear();
  mockFetch();
});
afterEach(async () => {
  await act(async () => { root?.unmount(); });
  root = null;
  container?.remove();
  vi.useRealTimers();
});

const busy = { eligible: false, reason: { code: "NUMBER_BUSY", message: "On a call" } };
const nextTags = () => Array.from(container.querySelectorAll("tbody tr")).filter((r) => r.textContent?.includes("Next")).map((r) => r.querySelector("td")!.textContent!.replace("Next", ""));

describe("Spam Check, Auto-select", () => {
  it("has no 'Select the next' box, keeps Auto-select, ticks nothing on load and tags one row Next", async () => {
    await mount();
    expect(container.textContent).not.toContain("Select the next");
    expect(container.querySelector("#select-count")).toBeNull();
    expect(container.querySelector('input[type="number"]')).toBeNull();
    expect(button("Auto-select")).toBeTruthy();
    expect(boxes().length).toBe(8);
    expect(ticked()).toEqual([]);
    expect(container.textContent).toContain("Ticks the next number that can be tested.");
    expect(button("Test call (0)")).toBeTruthy();
    expect(nextTags()).toEqual(["1"]);
  });

  it("ticks exactly one row, the first that can be tested, and sends no count", async () => {
    await mount();
    await click(button("Auto-select"));
    expect(ticked()).toEqual(phones([1]));
    expect(selectBodies).toEqual([{}]);
    expect(button("Test call (1)")).toBeTruthy();
  });

  it("skips numbers that cannot be tested: they stay put, keep a disabled box and a reason, and are not tagged Next", async () => {
    rows[0] = row(1, busy);
    rows[1] = row(2, { eligible: false, awaiting_result: true, reason: { code: "AWAITING_RESULT", message: "Awaiting result" } });
    rows[2] = row(3, { eligible: false, missing_on_bolna: true, reason: { code: "NUMBER_MISSING_ON_BOLNA", message: "Not on Bolna" } });
    await mount();
    expect(nextTags()).toEqual(["4"]); // exactly one, and not the ineligible rows above it
    await click(button("Auto-select"));
    expect(ticked()).toEqual(phones([4]));
    const b = boxes();
    expect(b.slice(0, 3).every((x) => x.disabled && !x.checked)).toBe(true);
    expect(b.slice(0, 3).map((x) => x.title)).toEqual(["On a call", "Awaiting result", "Not on Bolna"]);
    expect(Array.from(container.querySelectorAll("tbody tr")).map((r) => r.querySelector("td")!.textContent?.replace("Next", ""))).toEqual(
      ["1", "2", "3", "4", "5", "6", "7", "8"]);
  });

  it("ticks nothing and says so when no number can be tested, and tags no row", async () => {
    rows = rows.map((r) => row(r.id, busy));
    await mount();
    expect(nextTags()).toEqual([]);
    await click(button("Auto-select"));
    expect(ticked()).toEqual([]);
    expect(container.textContent).toContain("No numbers can be tested right now.");
  });

  it("gives the same single selection when pressed twice and replaces ticks made by hand", async () => {
    await mount();
    await click(boxes()[6]);
    await click(boxes()[3]); // the person ticks rows 7 and 4 by hand
    expect(ticked()).toEqual(phones([4, 7]));
    await click(button("Auto-select"));
    expect(ticked()).toEqual(phones([1]));
    await click(button("Auto-select"));
    expect(ticked()).toEqual(phones([1]));
  });

  it("keeps the Next tag and Auto-select on the same row, including after a call starts on the top number", async () => {
    await mount();
    expect(nextTags()).toEqual(["1"]);
    rows = rows.map((r) => (r.id === 1 ? row(1, busy) : r)); // a call starts on the top number
    await poll();
    expect(nextTags()).toEqual(["2"]);
    await click(button("Auto-select"));
    expect(ticked()).toEqual(phones([2]));
  });

  it("never persists the selection: a reload starts with nothing ticked", async () => {
    await mount();
    await click(button("Auto-select"));
    expect(ticked().length).toBe(1);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    await act(async () => { root!.unmount(); });
    container.remove();
    await mount();
    expect(ticked()).toEqual([]);
  });

  it("unticks a row that stops being testable and shows why, and clears the selection when a run ends", async () => {
    await mount();
    await click(boxes()[4]);
    await click(boxes()[5]); // two rows ticked by hand
    expect(ticked()).toEqual(phones([5, 6]));
    rows = rows.map((r) => (r.id === 5 ? row(5, busy) : r)); // a call starts on row 5
    await poll();
    expect(ticked()).toEqual(phones([6]));
    expect(container.textContent).toContain(`${row(5).phone_number} was unticked: On a call.`);
    expect(boxes()[4].disabled).toBe(true);

    rows = rows.map((r) => (r.id === 1 ? row(1, { test: { status: "calling", position: null, run_id: 1 } }) : r)); // a test run is in progress...
    await poll();
    expect(ticked()).toEqual(phones([6]));
    rows = rows.map((r) => (r.id === 1 ? row(1, { awaiting_result: true, eligible: false, reason: { code: "AWAITING_RESULT", message: "Awaiting result" } }) : r));
    await poll(); // ...and has now finished
    expect(ticked()).toEqual([]);
  });

  it("keeps Test call disabled until a row is ticked, an agent is chosen and the reviewer phone is valid", async () => {
    await mount();
    const test = () => Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.startsWith("Test call"))! as HTMLButtonElement;
    expect(test().disabled).toBe(true);
    await click(button("Auto-select"));
    expect(test().disabled).toBe(true); // no agent, no reviewer phone yet
    expect(test().textContent).toBe("Test call (1)");
  });
});
