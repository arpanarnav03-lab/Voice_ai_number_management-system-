import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AgentRow, ApiError, CallRow, HistoryRow, NumberRow, PHONE_HINT, PHONE_RE, api, fmtTime, useApi } from "./api";
import { CallTable, CountBar, Empty, LockIcon, REASON_TEXT, TableBox, agentPill, useToast } from "./ui";

type Detail = {
  agent: AgentRow;
  threshold: number;
  summary: { calls_placed: number; connected_calls: number; number_switches: number };
  number_history: HistoryRow[];
  calls: CallRow[];
};

export default function AgentDetail() {
  const { id } = useParams();
  const toast = useToast();
  const detail = useApi<Detail>(`/api/agents/${id}`);
  const numbers = useApi<{ threshold: number; items: NumberRow[] }>("/api/numbers");
  const [panel, setPanel] = useState<"none" | "change" | "call">("none");
  const [pick, setPick] = useState<number | null>(null);
  const [recipient, setRecipient] = useState("");
  const [busy, setBusy] = useState(false);

  if (detail.error && !detail.data) {
    return <section className="card"><p role="alert" className="field-err">{detail.error.message}</p><Link to="/">Back to agents</Link></section>;
  }
  if (!detail.data) return <section className="card"><p className="muted">Loading...</p></section>;

  const { agent, threshold, summary } = detail.data;
  const cur = agent.number;
  const locked = agent.on_call;

  function reason(n: NumberRow): string | null {
    if (cur && n.id === cur.id) return "Current number.";
    if (n.missing_on_bolna) return "Not on Bolna.";
    if (n.spam_flag) return "Marked as spam.";
    if (n.used_by) return `Used by ${n.used_by.name}${n.used_by.on_call ? ", on a call" : ""}. Cannot be taken.`;
    if (n.status === "limit_reached" || n.connected_count >= threshold) return `${n.connected_count} / ${threshold} connected calls. Cannot be assigned.`;
    return null;
  }

  async function confirm() {
    if (pick === null) return;
    setBusy(true);
    try {
      const a = await api.post<AgentRow>(`/api/agents/${agent.id}/change-number`, { number_id: pick });
      toast(`${a.name} now uses ${a.number?.phone_number}.`);
      setPanel("none");
      setPick(null);
      detail.reload();
      numbers.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
      detail.reload();
      numbers.reload();
    } finally {
      setBusy(false);
    }
  }

  async function callNow() {
    setBusy(true);
    try {
      const r = await api.post<{ results: { ok: boolean; message?: string }[] }>("/api/calls/trigger", { items: [{ agent_id: agent.id, recipient: recipient.trim() }] });
      if (r.results[0].ok) {
        toast("Call started.");
        setRecipient("");
        setPanel("none");
      } else toast(r.results[0].message || "The call could not be started.", "error");
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setBusy(false);
      detail.reload();
    }
  }

  const recipBad = recipient.length > 0 && !PHONE_RE.test(recipient.trim());
  const canCall = !!cur && agent.status !== "pool_empty" && agent.status !== "number_missing" && !agent.pending_switch;

  return (
    <>
      <div className="crumbs"><Link to="/">Agents</Link> / {agent.name}</div>
      <section className="card">
        <div className="card-head">
          <div>
            <h1>{agent.name}</h1>
            <span className="mono muted">{agent.bolna_agent_id}</span>
            <div style={{ marginTop: 6 }}>{agentPill(agent)}</div>
          </div>
          <div className="row">
            <button className="btn" disabled={locked} aria-expanded={panel === "change"} onClick={() => { setPanel(panel === "change" ? "none" : "change"); setPick(null); }}>Change number</button>
            <button className="btn btn-primary" disabled={!canCall} aria-expanded={panel === "call"} onClick={() => setPanel(panel === "call" ? "none" : "call")}>Trigger a call</button>
          </div>
        </div>
        {locked && <p className="notice"><LockIcon /> This agent is on a call. Its number is locked until the call ends, then it can be changed.</p>}
        {agent.pending_switch && agent.pending_switch_reason === "spam" && <p className="flag" role="alert">This agent's number was marked as spam. It will be swapped for a clean number as soon as its current calls end, and new calls are blocked until then.</p>}
        {agent.pending_switch && agent.pending_switch_reason !== "spam" && <p className="notice">The number reached its limit. The agent switches to the next available number when the current call ends.</p>}
        {agent.status === "number_missing" && <p className="flag" role="alert">This agent's number is not on your Bolna account any more, so calls are blocked. Nothing was switched automatically: change its number by hand.</p>}
        {agent.status === "pool_empty" && <p className="flag" role="alert">No number is available for this agent. Add a number to the pool or reset one and it is filled automatically.</p>}

        {panel === "change" && (
          <fieldset disabled={locked} style={{ marginTop: 12 }}>
            <legend><h3>Choose a new number</h3></legend>
            <div className="radiolist" role="radiogroup" aria-label="Pool numbers">
              {(numbers.data?.items ?? []).map((n) => {
                const why = reason(n);
                return (
                  <label key={n.id} className={`radio ${why ? "disabled" : ""}`}>
                    <input type="radio" name="num" disabled={!!why} checked={pick === n.id} onChange={() => setPick(n.id)} />
                    <span><span className="mono">{n.phone_number}</span>{why && <span className="why">{why}</span>}</span>
                  </label>
                );
              })}
              {!numbers.data?.items.length && <Empty>The pool is empty.</Empty>}
            </div>
            {cur && <p className="helper">The current number, <span className="mono">{cur.phone_number}</span>, goes back to the pool.</p>}
            <div className="row">
              <button className="btn" onClick={() => setPanel("none")}>Cancel</button>
              <button className="btn btn-primary" disabled={pick === null || busy} onClick={confirm}>Confirm change</button>
            </div>
          </fieldset>
        )}

        {panel === "call" && (
          <div style={{ marginTop: 12 }}>
            <h3>Call a recipient from {cur?.phone_number}</h3>
            <div className="row">
              <div className="grow">
                <label htmlFor="one-recipient">Recipient</label>
                <input id="one-recipient" type="text" inputMode="tel" className="mono-in" placeholder="+91XXXXXXXXXX" value={recipient}
                  aria-invalid={recipBad} onChange={(e) => setRecipient(e.target.value)} />
              </div>
              <button className="btn btn-primary" disabled={busy || !PHONE_RE.test(recipient.trim())} onClick={callNow}>Trigger call</button>
            </div>
            {recipBad && <div className="field-err">{PHONE_HINT}</div>}
          </div>
        )}
      </section>

      <div className="cards4">
        <div className="stat">
          <div className="label">Number in use</div>
          {cur ? (
            <>
              <div className="value" style={{ fontSize: 17 }}>{locked && <LockIcon />} <Link to={`/numbers/${cur.id}`}>{cur.phone_number}</Link></div>
              <CountBar count={cur.connected_count} threshold={threshold} />
              <div className="note">{locked ? "Can be changed after this call ends" : "Can be changed now"}</div>
            </>
          ) : <div className="value">None</div>}
        </div>
        <div className="stat"><div className="label">Calls placed</div><div className="value">{summary.calls_placed}</div></div>
        <div className="stat"><div className="label">Connected calls</div><div className="value">{summary.connected_calls}</div></div>
        <div className="stat"><div className="label">Number switches</div><div className="value">{summary.number_switches}</div></div>
      </div>

      <section className="card">
        <h2>Number history</h2>
        {!detail.data.number_history.length ? <Empty>No numbers assigned yet.</Empty> : (
          <TableBox>
            <table>
              <thead><tr><th>Number</th><th>From</th><th>To</th><th>Connected calls</th><th>Why it changed</th></tr></thead>
              <tbody>
                {detail.data.number_history.map((h) => (
                  <tr key={h.id}>
                    <td className="mono"><Link to={`/numbers/${h.number_id}`}>{h.phone_number}</Link></td>
                    <td>{fmtTime(h.started_at)}</td>
                    <td>{h.ended_at ? fmtTime(h.ended_at) : "Now"}</td>
                    <td className="mono">{h.connected_calls}</td>
                    <td>{REASON_TEXT[h.reason] ?? h.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableBox>
        )}
      </section>

      <section className="card">
        <h2>Call history</h2>
        <CallTable calls={detail.data.calls} showNumber />
      </section>
    </>
  );
}
