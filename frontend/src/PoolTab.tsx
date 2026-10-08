import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, NumberRow, PHONE_HINT, PHONE_RE, api, useApi } from "./api";
import { CountBar, Empty, LockIcon, TableBox, numberPill, useToast } from "./ui";

type Filter = "all" | "in_use" | "available" | "limit_reached";

export default function PoolTab() {
  const toast = useToast();
  const numbers = useApi<{ threshold: number; items: NumberRow[] }>("/api/numbers");
  const [filter, setFilter] = useState<Filter>("all");
  const [thr, setThr] = useState("");
  const [thrDirty, setThrDirty] = useState(false);
  const [phone, setPhone] = useState("");
  const [phoneTouched, setPhoneTouched] = useState(false);
  const [busy, setBusy] = useState(false);

  const threshold = numbers.data?.threshold ?? 5;
  const items = numbers.data?.items ?? [];
  useEffect(() => {
    if (numbers.data && !thrDirty) setThr(String(numbers.data.threshold));
  }, [numbers.data, thrDirty]);

  const count = (f: Filter) => (f === "all" ? items.length : items.filter((n) => n.status === f).length);
  const shown = filter === "all" ? items : items.filter((n) => n.status === filter);
  const thrNum = Number(thr);
  const thrValid = Number.isInteger(thrNum) && thrNum >= 1;
  const phoneBad = phoneTouched && phone.length > 0 && !PHONE_RE.test(phone.trim());

  async function saveThreshold() {
    try {
      await api.put("/api/settings/threshold", { threshold: thrNum });
      toast(`Threshold saved: ${thrNum} connected calls.`);
      setThrDirty(false);
      numbers.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    }
  }

  async function addNumber() {
    setBusy(true);
    try {
      const r = await api.post<{ number: NumberRow; warning: string | null }>("/api/numbers", { phone_number: phone.trim() });
      toast(`${r.number.phone_number} added to the pool.`);
      if (r.warning) toast(r.warning, "warn");
      setPhone("");
      setPhoneTouched(false);
      numbers.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function reset(n: NumberRow) {
    try {
      await api.post(`/api/numbers/${n.id}/reset`);
      toast(`Count reset for ${n.phone_number}.`);
      numbers.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    }
  }

  const chips: [Filter, string][] = [["all", "All"], ["in_use", "In use"], ["available", "Available"], ["limit_reached", "Limit reached"]];

  return (
    <>
      <div className="grid2">
        <section className="card" aria-labelledby="thr-h">
          <h2 id="thr-h">Switching threshold</h2>
          <p className="muted">When a number reaches this many connected calls, it is released as Limit reached and its agent moves to the next available number. A number is never changed while its agent is on a call: if the limit is reached mid-call, the switch happens once the call ends.</p>
          <div className="row">
            <div className="grow">
              <label htmlFor="thr">Connected calls per number</label>
              <input id="thr" type="number" min={1} step={1} value={thr} aria-invalid={thr !== "" && !thrValid}
                onChange={(e) => { setThr(e.target.value); setThrDirty(true); }} />
            </div>
            <button className="btn btn-primary" disabled={!thrValid || !thrDirty} onClick={saveThreshold}>Save threshold</button>
          </div>
          {thr !== "" && !thrValid && <div className="field-err">Enter a whole number of 1 or more.</div>}
        </section>

        <section className="card" aria-labelledby="add-h">
          <h2 id="add-h">Add numbers to the pool</h2>
          <div className="row">
            <div className="grow">
              <label htmlFor="new-phone">Phone number</label>
              <input id="new-phone" type="text" inputMode="tel" className="mono-in" placeholder="+91XXXXXXXXXX" value={phone}
                aria-invalid={phoneBad} onChange={(e) => setPhone(e.target.value)} onBlur={() => setPhoneTouched(true)} />
            </div>
            <button className="btn btn-primary" disabled={busy || !PHONE_RE.test(phone.trim())} onClick={addNumber}>Add to pool</button>
          </div>
          {phoneBad && <div className="field-err">{PHONE_HINT}</div>}
          <p className="helper">Must be +91 followed by 10 digits. The number is checked against your Bolna account before it is added.</p>
        </section>
      </div>

      <section className="card" aria-labelledby="pool-h">
        <h2 id="pool-h">Pool</h2>
        <div className="chips" role="group" aria-label="Filter numbers">
          {chips.map(([f, label]) => (
            <button key={f} className="chip" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {label}<span className="n">{count(f)}</span>
            </button>
          ))}
        </div>
        {numbers.error && <p className="field-err" role="alert">{numbers.error.message}</p>}
        {!items.length ? (
          <Empty>The pool is empty. Add a number above to get started.</Empty>
        ) : !shown.length ? (
          <Empty>No numbers match this filter.</Empty>
        ) : (
          <TableBox>
            <table>
              <thead>
                <tr><th>Number</th><th>Status</th><th>Used by</th><th>Connected / {threshold}</th><th>Agents served</th><th>Total calls</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {shown.map((n) => (
                  <tr key={n.id}>
                    <td className="mono"><Link to={`/numbers/${n.id}`}>{n.phone_number}</Link>{n.linked && <span className="sub">Linked to an agent in Bolna</span>}</td>
                    <td>{numberPill(n)}</td>
                    <td>
                      {n.used_by ? <>{n.used_by.on_call && <LockIcon />} <Link to={`/agents/${n.used_by.id}`}>{n.used_by.name}</Link>
                        {n.used_by.pending_switch && <span className="sub">Switching after this call</span>}</> : <span className="muted">-</span>}
                    </td>
                    <td><CountBar count={n.connected_count} threshold={threshold} /></td>
                    <td className="mono">{n.agents_served}</td>
                    <td className="mono">{n.total_calls}</td>
                    <td>{n.status === "limit_reached" && <button className="btn" onClick={() => reset(n)}>Reset count</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableBox>
        )}
      </section>
    </>
  );
}
