import { Link, useParams } from "react-router-dom";
import { ApiError, CallRow, NumberRow, api, fmtTime, useApi } from "./api";
import { CallTable, CountBar, Empty, REASON_TEXT, TableBox, numberPill, useToast } from "./ui";

type Detail = {
  number: NumberRow;
  agents: { agent_id: number; agent_name: string; started_at: string; ended_at: string | null; calls: number; connected_calls: number; reason: string }[];
  calls: CallRow[];
};

export default function NumberDetail() {
  const { id } = useParams();
  const toast = useToast();
  const d = useApi<Detail>(`/api/numbers/${id}`);

  if (d.error && !d.data) {
    return <section className="card"><p role="alert" className="field-err">{d.error.message}</p><Link to="/pool">Back to the pool</Link></section>;
  }
  if (!d.data) return <section className="card"><p className="muted">Loading...</p></section>;
  const { number: n, agents, calls } = d.data;

  async function reset() {
    try {
      await api.post(`/api/numbers/${n.id}/reset`);
      toast(`Count reset for ${n.phone_number}.`);
      d.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    }
  }

  return (
    <>
      <div className="crumbs"><Link to="/pool">Number Pool</Link> / {n.phone_number}</div>
      <section className="card">
        <div className="card-head">
          <div>
            <h1 className="mono" style={{ fontFamily: "var(--mono)" }}>{n.phone_number}</h1>
            <div style={{ marginTop: 6 }}>{numberPill(n)}</div>
            {n.linked && <p className="helper">This number is linked to an agent inside Bolna, so it is never used for auto-switching.</p>}
          </div>
          {n.status === "limit_reached" && <button className="btn btn-primary" onClick={reset}>Reset count</button>}
        </div>
      </section>

      <div className="cards4">
        <div className="stat"><div className="label">Connected calls</div><CountBar count={n.connected_count} threshold={n.threshold} /></div>
        <div className="stat"><div className="label">Total calls</div><div className="value">{n.total_calls}</div></div>
        <div className="stat"><div className="label">Agents that used it</div><div className="value">{n.agents_served}</div></div>
        <div className="stat"><div className="label">Provider</div><div className="value" style={{ fontSize: 17 }}>{n.provider}</div></div>
      </div>

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
