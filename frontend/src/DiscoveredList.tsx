import { useEffect, useState } from "react";
import { ApiError, NumberPage, NumberRow, api, fmtTime, useApi } from "./api";
import { ConfirmDialog, Empty, Pill, TableBox, useToast } from "./ui";

const PAGE_SIZE = 50;

/** Numbers found on the Bolna account that are not in the pool yet. A person approves them. */
export default function DiscoveredList({ ignoredCount, onChanged }: { ignoredCount: number; onChanged: () => void }) {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(1);
  const [sel, setSel] = useState<Record<number, boolean>>({}); // id -> linked
  const [confirmLinked, setConfirmLinked] = useState(false);
  const [showIgnored, setShowIgnored] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => { setApplied(q.trim()); setPage(1); }, 250);
    return () => clearTimeout(t);
  }, [q]);

  const list = useApi<NumberPage>(`/api/numbers?status=discovered&page=${page}&page_size=${PAGE_SIZE}&q=${encodeURIComponent(applied)}`, 10000);
  const ignored = useApi<NumberPage>(showIgnored ? "/api/numbers?status=ignored&page=1&page_size=200" : null, 10000);

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const unlinked = list.data?.unlinked_total ?? 0;
  const ids = Object.keys(sel).map(Number);
  const anyLinked = Object.values(sel).some(Boolean);
  const allOnPage = items.length > 0 && items.every((n) => sel[n.id] !== undefined);

  function toggle(n: NumberRow, on: boolean) {
    const next = { ...sel };
    if (on) next[n.id] = n.linked_in_bolna; else delete next[n.id];
    setSel(next);
  }
  function togglePage(on: boolean) {
    const next = { ...sel };
    for (const n of items) { if (on) next[n.id] = n.linked_in_bolna; else delete next[n.id]; }
    setSel(next);
  }

  async function run(fn: () => Promise<string>) {
    setBusy(true);
    try {
      toast(await fn());
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setBusy(false);
      setConfirmLinked(false);
      setSel({});
      list.reload();
      ignored.reload();
      onChanged();
    }
  }

  const addSelected = (confirm: boolean) => run(async () => {
    const r = await api.post<{ approved: number; skipped_linked: number[] }>("/api/numbers/approve", { ids, confirm_linked: confirm });
    return `${r.approved} ${r.approved === 1 ? "number" : "numbers"} added to the pool.`;
  });
  const addAllUnlinked = () => run(async () => {
    const r = await api.post<{ approved: number }>("/api/numbers/approve", { all_unlinked: true });
    return `${r.approved} ${r.approved === 1 ? "number" : "numbers"} added to the pool.`;
  });
  const ignoreSelected = () => run(async () => {
    const r = await api.post<{ ignored: number }>("/api/numbers/ignore", { ids });
    return `${r.ignored} ${r.ignored === 1 ? "number" : "numbers"} ignored.`;
  });
  const restore = (n: NumberRow) => run(async () => {
    await api.post(`/api/numbers/${n.id}/restore`);
    return `${n.phone_number} restored to Discovered.`;
  });

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="grow">
          <label htmlFor="disc-search">Search discovered numbers</label>
          <input id="disc-search" type="text" inputMode="tel" className="mono-in" placeholder="+91..." value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button className="btn btn-primary" disabled={busy || !ids.length} onClick={() => (anyLinked ? setConfirmLinked(true) : addSelected(false))}>Add selected to pool</button>
        <button className="btn" disabled={busy || unlinked === 0} onClick={addAllUnlinked}>Add all unlinked ({unlinked})</button>
        <button className="btn" disabled={busy || !ids.length} onClick={ignoreSelected}>Ignore selected</button>
      </div>
      {list.error && <p className="field-err" role="alert">{list.error.message}</p>}
      {list.data && !items.length ? (
        <Empty>{applied ? "No discovered numbers match this search." : "No new numbers. Everything on your Bolna account is already in the pool, ignored, or skipped."}</Empty>
      ) : (
        <TableBox>
          <table>
            <thead>
              <tr>
                <th className="checkcell">
                  <label className="check"><input type="checkbox" aria-label="Select all numbers on this page" checked={allOnPage} onChange={(e) => togglePage(e.target.checked)} /></label>
                </th>
                <th>Number</th><th>Provider</th><th>Linked in Bolna</th><th>First seen</th>
              </tr>
            </thead>
            <tbody>
              {items.map((n) => (
                <tr key={n.id}>
                  <td className="checkcell">
                    <label className="check"><input type="checkbox" aria-label={`Select ${n.phone_number}`} checked={sel[n.id] !== undefined} onChange={(e) => toggle(n, e.target.checked)} /></label>
                  </td>
                  <td className="mono">{n.phone_number}</td>
                  <td>{n.provider}</td>
                  <td>
                    {n.linked_in_bolna
                      ? <><Pill kind="limit">Linked to an agent in Bolna</Pill><span className="sub mono">{n.bolna_linked_agent_id}</span></>
                      : <span className="muted">-</span>}
                  </td>
                  <td>{fmtTime(n.discovered_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableBox>
      )}
      {total > PAGE_SIZE && (
        <div className="pager">
          <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="muted">Page {page} of {pages}, {total} numbers</span>
          <button className="btn" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <button className="linkbtn" aria-expanded={showIgnored} onClick={() => setShowIgnored(!showIgnored)}>Ignored ({ignoredCount})</button>
        {showIgnored && (
          !ignored.data?.items.length ? <Empty>No ignored numbers.</Empty> : (
            <TableBox>
              <table>
                <thead><tr><th>Number</th><th>Provider</th><th>First seen</th><th>Actions</th></tr></thead>
                <tbody>
                  {ignored.data.items.map((n) => (
                    <tr key={n.id}>
                      <td className="mono">{n.phone_number}</td>
                      <td>{n.provider}</td>
                      <td>{fmtTime(n.discovered_at)}</td>
                      <td><button className="btn" disabled={busy} onClick={() => restore(n)}>Restore</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableBox>
          )
        )}
      </div>

      <ConfirmDialog open={confirmLinked} title="Add linked numbers?" confirmLabel="Add to pool" busy={busy}
        onConfirm={() => addSelected(true)} onCancel={() => setConfirmLinked(false)}>
        <p>This number is linked to an agent in Bolna, probably for inbound calls. Adding it to the pool means this app can assign it to an agent. It will not be used for auto-switching unless you turn that on.</p>
      </ConfirmDialog>
    </>
  );
}
