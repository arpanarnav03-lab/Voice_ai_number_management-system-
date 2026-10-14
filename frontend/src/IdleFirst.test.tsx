// @vitest-environment jsdom
import { act } from "react-dom/test-utils";
import { createRoot, Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentDetail from "./AgentDetail";
import AgentsTab from "./AgentsTab";
import { ToastProvider } from "./ui";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const num = (id: number, over: Record<string, any> = {}) => ({
  id, phone_number: `+91800000${String(id).padStart(4, "0")}`, provider: "vobiz", status: "available", connected_count: 0,
  total_calls: 0, agents_served: 0, threshold: 5, linked: false, linked_in_bolna: false, bolna_linked_agent_id: null,
  allow_auto_switch: true, missing_on_bolna: false, missing_since: null, discovered_at: null, spam_flag: false,
  never_tested: true, taken_from_pool_at: null, spam_marked_at: null, spam_note: null, last_tested_at: null,
  last_test_result: null, bolna_rented: true, has_bolna_id: true, added_at: "2026-10-01T00:00:00Z", used_by: null, ...over,
});
const agent = (id: number, over: Record<string, any> = {}) => ({
  id, bolna_agent_id: `fake-agent-${id}`, name: `Agent ${id}`, number: null, status: "ready", on_call: false,
  active_calls: 0, pending_switch: false, pending_switch_reason: null, last_used: null, last_call_at: null,
  never_lend: false, number_taken: null, ...over,
});

const receiver = agent(1, { number: { id: 2, phone_number: num(2).phone_number, status: "in_use", connected_count: 0, provider: "vobiz", missing_on_bolna: false, spam_flag: false } });
const taken = agent(7, { status: "number_taken", number_taken: { at: "2026-10-14T09:30:00Z", reason: "moved to Agent 1 because this agent was idle", by: { id: 1, name: "Agent 1" } } });
const pool = [
  num(1),
  num(2, { status: "in_use", used_by: { id: 1, name: "Agent 1", on_call: false, pending_switch: false, pending_switch_reason: null } }),
  num(3, { status: "limit_reached", connected_count: 5 }),
  num(4, { spam_flag: true }),
];

let posts: { url: string; body: any }[];
let root: Root | null = null;
let container: HTMLElement;

beforeEach(() => {
  vi.useFakeTimers();
  posts = [];
  globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
    const json = (data: unknown, status = 200) => ({ ok: status < 400, status, json: async () => data });
    if (init?.method === "POST") {
      posts.push({ url, body: JSON.parse((init.body as string) || "{}") });
      return json({ ...agent(9), number: { id: 1, phone_number: num(1).phone_number } }, 201);
    }
    if (url === "/api/agents") return json({ threshold: 5, items: [receiver, taken] });
    if (url === "/api/numbers") return json({ threshold: 5, items: pool });
    if (url === "/api/account") return json({ mode: "fake", concurrency: null });
    if (url === "/api/agents/7") return json({
      agent: taken, threshold: 5, summary: { calls_placed: 0, connected_calls: 0, number_switches: 0 },
      number_history: [{ id: 3, number_id: 2, phone_number: num(2).phone_number, started_at: "2026-10-01T00:00:00Z",
        ended_at: "2026-10-14T09:30:00Z", connected_calls: 0, reason: "initial", end_reason: "taken_idle" }],
      calls: [],
    });
    return json({ code: "NOT_FOUND", message: "Not found" }, 404);
  }) as any;
});
afterEach(async () => {
  await act(async () => { root?.unmount(); });
  root = null;
  container?.remove();
  vi.useRealTimers();
});

async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(10); }); }
async function render(path: string) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<ToastProvider><MemoryRouter initialEntries={[path]}><Routes>
      <Route path="/" element={<AgentsTab />} />
      <Route path="/agents/:id" element={<AgentDetail />} />
    </Routes></MemoryRouter></ToastProvider>);
  });
  await flush();
  await flush();
}
async function click(el: Element) {
  await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
  await flush();
}
async function type(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => { setter.call(input, value); input.dispatchEvent(new Event("input", { bubbles: true })); });
}
const button = (text: string) => Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) as HTMLButtonElement;

describe("Agents tab", () => {
  it("shows Number taken with the reason, a link to the receiving agent and disabled controls", async () => {
    await render("/");
    const row = Array.from(container.querySelectorAll("tbody tr")).find((r) => r.textContent?.includes("Agent 7"))!;
    expect(row.textContent).toContain("Number taken");
    expect(row.textContent).toMatch(/Number moved to Agent 1 on .+: idle/);
    expect(row.querySelector('a[href="/agents/1"]')).toBeTruthy();
    expect((row.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true);
    expect((row.querySelector("#rec-7") as HTMLInputElement).disabled).toBe(true);
    const pill = Array.from(row.querySelectorAll(".pill")).find((p) => p.textContent?.includes("Number taken"))!;
    expect(pill.className).toContain("pill-limit"); // amber, with the square shape
    expect(pill.querySelector(".shape-limit")).toBeTruthy();
  });

  it("offers 'Assign automatically from the pool' first, selected by default, and sends no number for it", async () => {
    await render("/");
    const select = container.querySelector("#number-pick") as HTMLSelectElement;
    expect(select.options[0].textContent).toBe("Assign automatically from the pool");
    expect(select.value).toBe("auto");
    await type(container.querySelector("#agent-id") as HTMLInputElement, "fake-agent-9");
    await click(button("Assign number"));
    expect(posts).toEqual([{ url: "/api/agents", body: { bolna_agent_id: "fake-agent-9" } }]);
  });

  it("still lets a person choose a specific number, with the existing flags", async () => {
    await render("/");
    const select = container.querySelector("#number-pick") as HTMLSelectElement;
    const options = Array.from(select.options).map((o) => [o.textContent, o.disabled]);
    expect(options[1][0]).toContain("available");
    expect(options.find(([t]) => String(t).includes("marked as spam"))![1]).toBe(true);
    await act(async () => { select.value = "2"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(container.textContent).toContain("is already in use by");
    expect(button("Assign number").disabled).toBe(true);
  });
});

describe("Agent detail for an agent whose number was taken", () => {
  it("explains what happened and offers an Assign a number panel with only free numbers under the limit", async () => {
    await render("/agents/7");
    expect(container.textContent).toContain("No number. Its number was moved to Agent 1");
    expect(container.textContent).toContain("because this agent was idle");
    expect(container.textContent).toContain("Never take this agent's number");
    expect(container.textContent).toContain("Last call");
    expect(container.textContent).toContain("Moved to another agent because this agent was idle");
    await click(button("Assign a number"));
    expect(container.querySelector("legend")!.textContent).toBe("Assign a number");
    const radios = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="radio"]'));
    expect(radios.map((r) => !r.disabled)).toEqual([true, false, false, false]); // free / in use / limit / spam
    expect(button("Trigger a call").disabled).toBe(true);
  });
});
