import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, CallRow, NumberEvent, NumberRow, api, fmtTime, useApi } from "./api";
import { CallTable, ConfirmDialog, CountBar, Empty, MissingNote, REASON_TEXT, SpamBadge, TableBox, numberPill, useToast } from "./ui";

type Detail = {
  number: NumberRow;
  agents: { agent_id: number; agent_name: string; started_at: string; ended_at: string | null; calls: number; connected_calls: number; reason: string }[];
  calls: CallRow[];
  events: NumberEvent[];
  moves: { id: number; detail: string; at: string }[];
};

const EVENT_TEXT: Record<string, string> = {
  marked_spam: "Marked as spam",
  marked_not_spam: "Marked as not spam",
  test_call_placed: "Test call placed",
  test_result: "Test result recorded",
  release_started: "Release started",
  release_succeeded: "Released from the pool and Bolna",
  release_failed: "Release failed",
  moved_idle: "Moved to another agent (the holder was idle)",
};

export default function NumberDetail() {
  const { id } = useParams();
  const toast = useToast();
  const d = useApi<Detail>(`/api/numbers/${id}`);
  const [confirmSwitch, setConfirmSwitch] = useState(false);
  const [busy, setBusy] = useState(false);

  if (d.error && !d.data) {
    return <section className="card"><p role="alert" className="field-err">{d.error.message}</p><Link to="/pool">Back to the pool</Link></section>;
  }
  if (!d.data) return <section className="card"><p className="muted">Loading...</p></section>;
  const { number: n, agents, calls, events, moves } = d.data;
  const testCalls = calls.filter((c) => c.purpose === "test");

  async function reset() {
    try {
      await api.post(`/api/numbers/${n.id}/reset`);
      toast(`Count reset for ${n.phone_number}.`);
      d.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    }
  }

  async function setAutoSwitch(allow: boolean) {
    setBusy(true);
    try {
      await api.put(`/api/numbers/${n.id}/auto-switch`, { allow, confirm: true });
      toast(`Auto-switching ${allow ? "allowed" : "turned off"} for ${n.phone_number}.`);
      d.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setBusy(false);
      setConfirmSwitch(false);
    }
  }

  return (
    <>
      <div className="crumbs"><Link to="/pool">Number Pool</Link> / {n.phone_number}</div>
      <section className="card">
        <div className="card-head">
          <div>
            <h1 className="mono" style={{ fontFamily: "var(--mono)" }}>{n.phone_number}</h1>
            <div style={{ marginTop: 6 }}>{numberPill(n)} <SpamBadge n={n} /><MissingNote n={n} /></div>
            {n.linked_in_bolna && <p className="helper">Linked in Bolna to agent <span className="mono">{n.bolna_linked_agent_id}</span>, probably for inbound calls.</p>}
            {n.missing_on_bolna && <p className="helper">Bolna no longer lists this number. It is skipped by auto-switching and cannot be assigned.</p>}
            <label className="toggle">
              <input type="checkbox" role="switch" checked={n.allow_auto_switch} disabled={busy}
                onChange={() => setConfirmSwitch(true)} />
              Allow auto-switching
            </label>
            <p className="helper">{n.allow_auto_switch
              ? "Agents can be moved to this number automatically when their number reaches the limit."
              : "This number is only used when you assign it by hand."}</p>
          </div>
          {n.status === "limit_reached" && <button className="btn btn-primary" onClick={reset}>Reset count</button>}
        </div>
      </section>

      <ConfirmDialog open={confirmSwitch} busy={busy} confirmLabel={n.allow_auto_switch ? "Turn off" : "Allow auto-switching"}
        title={n.allow_auto_switch ? "Turn off auto-switching?" : "Allow auto-switching?"}
        onConfirm={() => setAutoSwitch(!n.allow_auto_switch)} onCancel={() => setConfirmSwitch(false)}>
        <p>{n.allow_auto_switch
          ? `${n.phone_number} will no longer be given to agents automatically. You can still assign it by hand.`
          : `${n.phone_number} can be given to an agent automatically when another number reaches its limit.${n.linked_in_bolna ? " It is linked to an agent in Bolna, probably for inbound calls, so check that is what you want." : ""}`}</p>
      </ConfirmDialog>

      <div className="cards4">
        <div className="stat"><div className="label">Connected calls</div><CountBar count={n.connected_count} threshold={n.threshold} /></div>
        <div className="stat"><div className="label">Total calls</div><div className="value">{n.total_calls}</div></div>
        <div className="stat"><div className="label">Agents that used it</div><div className="value">{n.agents_served}</div></div>
        <div className="stat"><div className="label">Provider</div><div className="value" style={{ fontSize: 17 }}>{n.provider}</div></div>
      </div>

      <section className="card" aria-labelledby="moves-h">
        <h2 id="moves-h">Taken from the pool</h2>
        <p>{n.taken_from_pool_at ? `Taken from the pool on ${fmtTime(n.taken_from_pool_at)}.` : <span className="muted">Not held by an agent right now.</span>}</p>
        <h3 style={{ marginTop: 12 }}>Moves between agents</h3>
        {!moves.length ? <Empty>This number has not been moved between agents.</Empty> : (
          <TableBox>
            <table>
              <thead><tr><th>When</th><th>Move</th></tr></thead>
              <tbody>{moves.map((m) => <tr key={m.id}><td>{fmtTime(m.at)}</td><td>{m.detail}</td></tr>)}</tbody>
            </table>
          </TableBox>
        )}
      </section>

      <section className="card" aria-labelledby="spam-h">
        <h2 id="spam-h">Spam check</h2>
        {n.spam_flag ? (
          <p>Marked as spam on {fmtTime(n.spam_marked_at)}. {n.spam_note ? <>Note: {n.spam_note}</> : <span className="muted">No note.</span>} It stays in the pool but is never given to an agent.</p>
        ) : <p className="muted">Not marked as spam.</p>}
        <p className="helper">Last test call: {n.last_tested_at ? fmtTime(n.last_tested_at) : "never"}{n.last_test_result ? `, result: ${n.last_test_result === "ok" ? "OK" : "spam"}` : ""}.</p>
        <h3 style={{ marginTop: 12 }}>Test calls</h3>
        <CallTable calls={testCalls} showAgent />
        <h3 style={{ marginTop: 12 }}>Event log</h3>
        {!events.length ? <Empty>No events yet.</Empty> : (
          <TableBox>
            <table>
              <thead><tr><th>When</th><th>Event</th><th>Detail</th></tr></thead>
              <tbody>
                {events.map((e) => <tr key={e.id}><td>{fmtTime(e.at)}</td><td>{EVENT_TEXT[e.event] ?? e.event}</td><td>{e.detail}</td></tr>)}
              </tbody>
            </table>
          </TableBox>
        )}
      </section>

      <section className="card">
        <h2>Agents that used this number</h2>
        {!agents.length ? <Empty>No agent has used this number yet.</Empty> : (
          <TableBox>
            <table>
              <thead><tr><th>Agent</th><th>From</th><th>To</th><th>Calls</th><th>Connected calls</th><th>How it started</th></tr></thead>
              <tbody>
                {agents.map((a, i) => (
                  <tr key={i}>
                    <td><Link to={`/agents/${a.agent_id}`}>{a.agent_name}</Link></td>
                    <td>{fmtTime(a.started_at)}</td>
                    <td>{a.ended_at ? fmtTime(a.ended_at) : "Now"}</td>
                    <td className="mono">{a.calls}</td>
                    <td className="mono">{a.connected_calls}</td>
                    <td>{REASON_TEXT[a.reason] ?? a.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableBox>
        )}
      </section>

      <section className="card">
        <h2>Call history from this number</h2>
        <CallTable calls={calls} showAgent />
      </section>
    </>
  );
}
