import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, NumberRow, PHONE_HINT, PHONE_RE, SyncStatus, api, timeAgo, useApi } from "./api";
import DiscoveredList from "./DiscoveredList";
import { CountBar, Empty, LockIcon, MissingNote, SpamBadge, TableBox, numberPill, useToast } from "./ui";

type Filter = "all" | "in_use" | "available" | "limit_reached" | "discovered" | "spam";

export default function PoolTab() {
  const toast = useToast();
  const numbers = useApi<{ threshold: number; items: NumberRow[] }>("/api/numbers");
  const syncSt = useApi<SyncStatus>("/api/sync/status", 5000);
  const [syncing, setSyncing] = useState(false);
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

  const sync = syncSt.data;
  const discoveredCount = sync?.discovered_count ?? 0;
  // a number marked as spam is never handed out, so it does not count as available
  const matches = (n: NumberRow, f: Filter) =>
    f === "all" ? true : f === "spam" ? n.spam_flag : f === "available" ? n.status === f && !n.spam_flag : n.status === f;
  const count = (f: Filter) => (f === "discovered" ? discoveredCount : items.filter((n) => matches(n, f)).length);
  const shown = items.filter((n) => matches(n, filter));
  const ok = sync?.last_success ?? null;
  const failed = sync?.last_run?.status === "failed" ? sync.last_run : null;
  const isSyncing = syncing || !!sync?.running;
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

  async function runSync() {
    setSyncing(true);
    try {
      await api.post("/api/sync/run");
    } catch (e) {
      const err = e as ApiError;
      if (err.code === "SYNC_ALREADY_RUNNING") toast(err.message, "warn");
      else if (err.code !== "SYNC_FAILED") toast(err.message, "error"); // a failed run shows in the alert below
    } finally {
      setSyncing(false);
      syncSt.reload();
      numbers.reload();
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

  const chips: [Filter, string][] = [["all", "All"], ["in_use", "In use"], ["available", "Available"], ["limit_reached", "Limit reached"], ["discovered", "Discovered"], ["spam", "Spam"]];

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
        <div className="head-row">
          <h2 id="pool-h">Pool</h2>
          <button className="btn" disabled={isSyncing} onClick={runSync}>{isSyncing ? "Syncing..." : "Sync from Bolna"}</button>
          <span className="syncline" role="status">
            {ok
              ? `Last synced ${timeAgo(ok.finished_at)}. ${ok.total_on_bolna} on Bolna, ${ok.new_count} new, ${ok.missing_count} missing, ${ok.skipped_invalid_count} skipped.`
              : sync ? "Not synced yet." : ""}
          </span>
        </div>
        {failed && (
          <div className="flag" role="alert">
            <p>Sync failed: {failed.error_message}. Nothing was changed.{" "}
              {ok ? `Last successful sync: ${timeAgo(ok.finished_at)}.` : "There has been no successful sync yet."}</p>
          </div>
        )}
        {ok && ok.skipped_invalid_count > 0 && (
          <details className="skipnote">
            <summary>{ok.skipped_invalid_count} {ok.skipped_invalid_count === 1 ? "number" : "numbers"} skipped because {ok.skipped_invalid_count === 1 ? "it is" : "they are"} not +91 followed by 10 digits</summary>
            <ul>{ok.skipped_numbers.map((v, i) => <li key={i}>{v}</li>)}</ul>
          </details>
        )}
        <div className="chips" role="group" aria-label="Filter numbers">
          {chips.map(([f, label]) => (
            <button key={f} className="chip" aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {label}<span className="n">{count(f)}</span>
            </button>
          ))}
        </div>
        {numbers.error && <p className="field-err" role="alert">{numbers.error.message}</p>}
        {filter === "discovered" ? (
          <DiscoveredList ignoredCount={sync?.ignored_count ?? 0} onChanged={() => { numbers.reload(); syncSt.reload(); }} />
        ) : !items.length ? (
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
                    <td className="mono"><Link to={`/numbers/${n.id}`}>{n.phone_number}</Link>{n.linked_in_bolna && <span className="sub">Linked in Bolna</span>}{n.never_tested && !n.spam_flag && <span className="sub">Never tested</span>}</td>
                    <td>{numberPill(n)} <SpamBadge n={n} /><MissingNote n={n} /></td>
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
