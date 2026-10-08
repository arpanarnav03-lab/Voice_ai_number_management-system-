import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AgentRow, ApiError, NumberRow, PHONE_HINT, PHONE_RE, api, fmtTime, useApi } from "./api";
import { CountBar, Empty, LockIcon, TableBox, agentPill, useToast } from "./ui";

function numberLabel(n: NumberRow) {
  if (n.missing_on_bolna) return `${n.phone_number} - not on Bolna`;
  if (n.status === "limit_reached" && !n.used_by) return `${n.phone_number} - limit reached, cannot be assigned`;
  if (n.used_by) return `${n.phone_number} - in use by ${n.used_by.name}${n.used_by.on_call ? " (on a call)" : ""}`;
  return `${n.phone_number} - available`;
}

export default function AgentsTab() {
  const toast = useToast();
  const agents = useApi<{ threshold: number; items: AgentRow[] }>("/api/agents");
  const numbers = useApi<{ threshold: number; items: NumberRow[] }>("/api/numbers");
  const account = useApi<{ mode: string; concurrency: { max: number; current: number } | null }>("/api/account", 30000);

  const [agentId, setAgentId] = useState("");
  const [numberId, setNumberId] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const [recipients, setRecipients] = useState<Record<number, string>>({});
  const [touched, setTouched] = useState<Record<number, boolean>>({});
  const [triggering, setTriggering] = useState(false);

  const threshold = agents.data?.threshold ?? numbers.data?.threshold ?? 5;
  const pool = numbers.data?.items ?? [];
  const firstFree = pool.find((n) => n.status === "available" && !n.used_by && !n.missing_on_bolna);

  useEffect(() => {
    if (!numberId && firstFree) setNumberId(String(firstFree.id));
  }, [firstFree, numberId]);

  const chosen = pool.find((n) => String(n.id) === numberId);
  let flag: JSX.Element | null = null;
  if (chosen?.missing_on_bolna) {
    flag = <p><strong className="mono">{chosen.phone_number}</strong> is not on your Bolna account any more, so it cannot be assigned. Choose another number.</p>;
  } else if (chosen?.used_by) {
    flag = chosen.used_by.on_call ? (
      <p><strong className="mono">{chosen.phone_number}</strong> is already in use by <strong>{chosen.used_by.name}</strong>, which is on a call right now. A number can serve only one agent, and it cannot be moved until that call ends.</p>
    ) : (
      <p><strong className="mono">{chosen.phone_number}</strong> is already in use by <strong>{chosen.used_by.name}</strong>. A number can serve only one agent, so choose a free number instead.</p>
    );
  } else if (chosen && chosen.status === "limit_reached") {
    flag = (
      <p><strong className="mono">{chosen.phone_number}</strong> has reached its limit of <strong>{threshold} connected calls</strong> and was released. It cannot be assigned until its count is reset, so choose another number.</p>
    );
  }

  async function assign() {
    setBusy(true);
    try {
      const a = await api.post<AgentRow>("/api/agents", { bolna_agent_id: agentId.trim(), number_id: Number(numberId) });
      toast(`${a.name} now uses ${a.number?.phone_number}.`);
      await Promise.all([agents.reload(), numbers.reload()]);
      setAgentId("");
      setNumberId(""); // the effect then picks the first free number from the fresh list
    } catch (e) {
      toast((e as ApiError).message, "error");
      numbers.reload();
    } finally {
      setBusy(false);
    }
  }

  const rows = agents.data?.items ?? [];
  const canSelect = (a: AgentRow) => !!a.number && !a.on_call && a.status !== "pool_empty" && a.status !== "number_missing";
  const chosenAgents = useMemo(() => rows.filter((a) => selected[a.id] && canSelect(a)), [rows, selected]);
  const recipientOk = (id: number) => PHONE_RE.test((recipients[id] || "").trim());
  const canTrigger = chosenAgents.length > 0 && chosenAgents.every((a) => recipientOk(a.id)) && !triggering;
  const conc = account.data?.concurrency;
  const overConcurrency = conc && conc.max != null && conc.current != null && chosenAgents.length > conc.max - conc.current;

  async function trigger() {
    setTriggering(true);
    try {
      const body = { items: chosenAgents.map((a) => ({ agent_id: a.id, recipient: recipients[a.id].trim() })) };
      const res = await api.post<{ results: { agent_id: number; ok: boolean; message?: string }[] }>("/api/calls/trigger", body);
      const next = { ...selected };
      for (const r of res.results) {
        const name = rows.find((a) => a.id === r.agent_id)?.name ?? "Agent";
        if (r.ok) {
          toast(`Call started for ${name}.`);
          delete next[r.agent_id];
        } else {
          toast(`${name}: ${r.message}`, "error");
        }
      }
      setSelected(next);
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setTriggering(false);
      agents.reload();
      numbers.reload();
    }
  }

  return (
    <>
      <section className="card" aria-labelledby="assign-h">
        <h2 id="assign-h">Assign a number to an agent</h2>
        <p className="muted">Paste the agent ID from Bolna and pick a number from the pool.</p>
        <div className="row">
          <div className="grow">
            <label htmlFor="agent-id">Bolna agent ID</label>
            <input id="agent-id" type="text" className="mono-in" value={agentId} onChange={(e) => setAgentId(e.target.value)} autoComplete="off" spellCheck={false} />
          </div>
          <div className="grow">
            <label htmlFor="number-pick">Number</label>
            <select id="number-pick" value={numberId} onChange={(e) => setNumberId(e.target.value)}>
              <option value="" disabled>{pool.length ? "Choose a number" : "No numbers in the pool yet"}</option>
              {pool.map((n) => <option key={n.id} value={n.id} disabled={n.missing_on_bolna}>{numberLabel(n)}</option>)}
            </select>
          </div>
          <button className="btn btn-primary" disabled={busy || !!flag || !agentId.trim() || !numberId} onClick={assign}>Assign number</button>
        </div>
        {flag && (
          <div className="flag" role="alert">
            {flag}
            <button className="btn" onClick={() => setNumberId(firstFree ? String(firstFree.id) : "")}>Choose another</button>
          </div>
        )}
        {!pool.length && numbers.data && <p className="helper">Add numbers on the <Link to="/pool">Number Pool</Link> tab first.</p>}
      </section>

      <section className="card" aria-labelledby="agents-h">
        <div className="card-head"><h2 id="agents-h">Agents</h2></div>
        {agents.error && <p className="field-err" role="alert">{agents.error.message}</p>}
        {!rows.length ? (
          <Empty>No agents yet. Assign a number to a Bolna agent above to add the first one.</Empty>
        ) : (
          <TableBox>
            <table>
              <thead>
                <tr>
                  <th className="checkcell"><span className="sr">Select</span></th>
                  <th>Agent</th><th>Number in use</th><th>Connected calls</th><th>Status</th><th>Last used</th><th>Recipient for this run</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => {
                  const sel = !!selected[a.id] && canSelect(a);
                  const val = recipients[a.id] || "";
                  const bad = sel && (touched[a.id] || val.length > 0) && !recipientOk(a.id);
                  return (
                    <tr key={a.id}>
                      <td className="checkcell">
                        <label className="check">
                          <input type="checkbox" aria-label={`Select ${a.name}`} checked={sel} disabled={!canSelect(a)}
                            onChange={(e) => setSelected({ ...selected, [a.id]: e.target.checked })} />
                        </label>
                      </td>
                      <td>
                        <Link to={`/agents/${a.id}`}><strong>{a.name}</strong></Link>
                        <span className="sub mono">{a.bolna_agent_id}</span>
                      </td>
                      <td>
                        {a.number ? (
                          <>
                            <span className="mono">{a.on_call && <><LockIcon /> </>}<Link to={`/numbers/${a.number.id}`}>{a.number.phone_number}</Link></span>
                            {a.number.missing_on_bolna && <span className="sub">Not on Bolna</span>}
                            {a.on_call && <span className="rowlock">Locked until the call ends</span>}
                            {a.on_call && <span className="sub">Can be changed after this call ends</span>}
                          </>
                        ) : <span className="muted">None</span>}
                      </td>
                      <td>{a.number ? <CountBar count={a.number.connected_count} threshold={threshold} /> : <span className="muted">-</span>}</td>
                      <td>{agentPill(a)}{a.pending_switch && <span className="sub">Switching after this call</span>}</td>
                      <td>{fmtTime(a.last_used)}</td>
                      <td className="recip">
                        <label className="sr" htmlFor={`rec-${a.id}`}>Recipient for {a.name}</label>
                        <input id={`rec-${a.id}`} type="text" inputMode="tel" className="mono-in" placeholder="+91XXXXXXXXXX"
                          value={val} disabled={!sel} aria-invalid={bad} aria-describedby={bad ? `rec-err-${a.id}` : undefined}
                          onChange={(e) => setRecipients({ ...recipients, [a.id]: e.target.value })}
                          onBlur={() => setTouched({ ...touched, [a.id]: true })} />
                        {bad && <div id={`rec-err-${a.id}`} className="field-err">{PHONE_HINT}</div>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableBox>
        )}
        <div className="actionbar">
          <strong>{chosenAgents.length} {chosenAgents.length === 1 ? "agent" : "agents"} selected</strong>
          <button className="btn btn-primary" disabled={!canTrigger} onClick={trigger}>Trigger calls ({chosenAgents.length})</button>
          <span className="note">Each selected agent calls its own recipient from its own number. Calls start together, up to Bolna's concurrency limit.</span>
        </div>
        {overConcurrency && conc && (
          <p className="notice" role="status">Bolna allows {conc.max} calls at once and {conc.current} {conc.current === 1 ? "is" : "are"} running. Some of these calls may be refused or queued.</p>
        )}
      </section>
    </>
  );
}
