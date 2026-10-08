import { createContext, ReactNode, useCallback, useContext, useState } from "react";
import { Link } from "react-router-dom";
import type { AgentRow, CallRow, NumberRow } from "./api";
import { fmtDuration, fmtTime } from "./api";

export function LockIcon({ label = "Locked until the call ends" }: { label?: string }) {
  return (
    <svg className="lock" viewBox="0 0 16 16" width="14" height="14" role="img" aria-label={label}>
      <title>{label}</title>
      <rect x="3" y="7" width="10" height="7" rx="1.5" fill="currentColor" />
      <path d="M5 7V5a3 3 0 0 1 6 0v2" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export type PillKind = "available" | "in_use" | "on_call" | "limit" | "error" | "neutral";

/** Status pill. Colour is never the only signal: every kind has its own shape. */
export function Pill({ kind, children }: { kind: PillKind; children: ReactNode }) {
  return (
    <span className={`pill pill-${kind}`}>
      {kind === "on_call" ? <LockIcon label="On a call" /> : <span className={`shape shape-${kind}`} aria-hidden="true" />}
      {children}
    </span>
  );
}

export function agentPill(a: AgentRow) {
  switch (a.status) {
    case "ready": return <Pill kind="available">Ready</Pill>;
    case "near_limit": return <Pill kind="limit">Near limit</Pill>;
    case "on_call": return <Pill kind="on_call">On a call</Pill>;
    case "pool_empty": return <Pill kind="error">Pool empty</Pill>;
    default: return <Pill kind="neutral">No number</Pill>;
  }
}

export function numberPill(n: NumberRow) {
  if (n.status === "limit_reached") return <Pill kind="limit">Limit reached</Pill>;
  if (n.status === "in_use") {
    return n.used_by?.on_call ? <Pill kind="on_call">In use, on a call</Pill> : <Pill kind="in_use">In use</Pill>;
  }
  return <Pill kind="available">Available</Pill>;
}

export function resultPill(r: CallRow["result"]) {
  switch (r) {
    case "connected": return <Pill kind="available">Connected</Pill>;
    case "in_progress": return <Pill kind="in_use">In progress</Pill>;
    case "unclear": return <Pill kind="limit">Unclear</Pill>;
    case "voicemail": return <Pill kind="neutral">Voicemail</Pill>;
    case "no_answer": return <Pill kind="neutral">No answer</Pill>;
    case "busy": return <Pill kind="neutral">Busy</Pill>;
    default: return <Pill kind="neutral">Failed</Pill>;
  }
}

export function CountBar({ count, threshold }: { count: number; threshold: number }) {
  const pct = Math.min(100, Math.round((count / Math.max(threshold, 1)) * 100));
  const hot = count >= threshold - 1;
  return (
    <div className="countbar">
      <span className="mono">{count} / {threshold}</span>
      <span className={`bar ${hot ? "bar-hot" : ""}`} role="img" aria-label={`${count} of ${threshold} connected calls`}>
        <span style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
}

export function TableBox({ children }: { children: ReactNode }) {
  return <div className="table-box" tabIndex={0}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function CallTable({ calls, showAgent, showNumber }: { calls: CallRow[]; showAgent?: boolean; showNumber?: boolean }) {
  if (!calls.length) return <Empty>No calls yet.</Empty>;
  return (
    <TableBox>
      <table>
        <thead>
          <tr>
            <th>Time</th>
            {showAgent && <th>Agent</th>}
            <th>Recipient</th>
            {showNumber && <th>Number used</th>}
            <th>Result</th>
            <th>Duration</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((c) => (
            <tr key={c.id}>
              <td>{fmtTime(c.placed_at)}</td>
              {showAgent && <td><Link to={`/agents/${c.agent_id}`}>{c.agent_name}</Link></td>}
              <td className="mono">{c.recipient}</td>
              {showNumber && <td className="mono"><Link to={`/numbers/${c.number_id}`}>{c.phone_number}</Link></td>}
              <td title={c.reason}>{resultPill(c.result)}</td>
              <td className="mono">{fmtDuration(c.duration)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableBox>
  );
}

export const REASON_TEXT: Record<string, string> = {
  initial: "Assigned by hand",
  manual: "Assigned by hand",
  auto_threshold: "Auto-switched, previous number hit the threshold",
  auto_refill: "Filled automatically when a number became available",
  released: "Released",
};

// ---------------------------------------------------------------- toasts
type Toast = { id: number; text: string; kind: "ok" | "error" | "warn" };
const ToastCtx = createContext<(text: string, kind?: Toast["kind"]) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: Toast["kind"] = "ok") => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, text, kind }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === "ok" ? 4000 : 8000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} role={t.kind === "error" ? "alert" : "status"}>
            <span>{t.text}</span>
            <button className="icon-btn" aria-label="Dismiss message" onClick={() => setItems((x) => x.filter((i) => i.id !== t.id))}>
              <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.8" /></svg>
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
