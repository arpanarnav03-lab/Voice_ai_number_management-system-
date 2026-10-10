import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AgentRow, ApiError, NumberRow, PHONE_HINT, PHONE_RE, ReleaseResult, SpamList, SpamListRow, Stack, StackRow,
  api, fmtTime, useApi,
} from "./api";
import { autoSelectHelp, autoSelectNote, pruneSelection, replaceSelection } from "./selection";
import { canStartTest } from "./testRules";
import { ConfirmDialog, Empty, Pill, SpamBadge, TableBox, numberPill, useToast } from "./ui";

const PASTE = "__paste__";

function resultText(r: StackRow["last_test_result"]) {
  return r === "ok" ? "OK" : r === "spam" ? "Spam" : "-";
}

export default function SpamTab() {
  const toast = useToast();
  const nums = useApi<Stack>("/api/spam/stack");
  const agents = useApi<{ items: AgentRow[] }>("/api/agents", 15000);
  const list = useApi<SpamList>("/api/spam/list");
  const [showReleased, setShowReleased] = useState(false);
  const released = useApi<{ items: (NumberRow & { released_at: string | null })[] }>(showReleased ? "/api/spam/released" : null, 15000);

  // ---- test stack
  const [agentSel, setAgentSel] = useState("");
  const [pasted, setPasted] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [reviewerTouched, setReviewerTouched] = useState(false);
  const [q, setQ] = useState("");
  const [neverOnly, setNeverOnly] = useState(false);
  const [picked, setPicked] = useState<Record<number, boolean>>({});
  const [autoNote, setAutoNote] = useState<string | null>(null);
  const [dropNote, setDropNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = nums.data?.items ?? [];
  const shown = useMemo(
    () => rows.filter((n) => n.phone_number.includes(q.trim()) && (!neverOnly || n.never_tested)),
    [rows, q, neverOnly],
  );
  const chosenIds = rows.filter((n) => picked[n.id] && n.eligible).map((n) => n.id);
  const reviewerOk = PHONE_RE.test(reviewer.trim());
  const reviewerBad = reviewerTouched && reviewer.length > 0 && !reviewerOk;
  const agentOk = agentSel === PASTE ? pasted.trim().length > 0 : agentSel !== "";
  const canTest = canStartTest({ selectedCount: chosenIds.length, agentChosen: agentOk, reviewerPhone: reviewer, busy });

  function reloadAll() {
    nums.reload();
    list.reload();
    released.reload();
  }

  async function autoSelect() {
    try {
      const r = await api.post<{ ids: number[]; skipped: unknown[] }>("/api/spam/stack/select");
      setPicked(replaceSelection(r.ids)); // replaces every earlier tick, so pressing it twice is the same
      setDropNote(null);
      setAutoNote(autoSelectNote(r.ids.length));
    } catch (e) {
      toast((e as ApiError).message, "error");
    }
  }

  // a ticked row that stops being testable is unticked, with the reason
  useEffect(() => {
    if (!nums.data) return;
    const { picked: kept, dropped } = pruneSelection(picked, rows);
    if (dropped.length) {
      setPicked(kept);
      setDropNote(dropped.map((d) => `${d.phone} was unticked: ${d.reason}.`).join(" "));
    }
  }, [rows]); // eslint-disable-line react-hooks/exhaustive-deps

  // the selection is cleared when a test run finishes
  const hadTests = useRef(false);
  useEffect(() => {
    const active = rows.some((n) => n.test);
    if (hadTests.current && !active) {
      setPicked({});
      setAutoNote(null);
    }
    hadTests.current = active;
  }, [rows]);

  async function dismiss(n: NumberRow) {
    try {
      await api.post(`/api/numbers/${n.id}/clear-test`);
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      reloadAll();
    }
  }

  async function startTests() {
    setBusy(true);
    try {
      const body: Record<string, unknown> = { number_ids: chosenIds, reviewer_phone: reviewer.trim() };
      if (agentSel === PASTE) body.bolna_agent_id = pasted.trim();
      else body.test_agent_id = Number(agentSel);
      const r = await api.post<{ items: unknown[]; refused: { message: string }[] }>("/api/spam/tests", body);
      toast(`${r.items.length} test ${r.items.length === 1 ? "call" : "calls"} queued. They are placed one at a time.`);
      for (const x of r.refused) toast(x.message, "warn");
      setPicked({});
      setAutoNote(null);
      agents.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setBusy(false);
      reloadAll();
    }
  }

  async function verdict(n: NumberRow, result: "ok" | "spam") {
    try {
      await api.post(`/api/numbers/${n.id}/spam-result`, { result });
      toast(result === "ok" ? `${n.phone_number} marked OK.` : `${n.phone_number} marked as spam.`);
      setPicked({});
      setAutoNote(null);
      agents.reload();
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      reloadAll();
    }
  }

  // ---- spam list + release
  const [sel, setSel] = useState<Record<number, boolean>>({});
  const [confirm, setConfirm] = useState(false);
  const [results, setResults] = useState<ReleaseResult[] | null>(null);
  const flagged = list.data?.items ?? [];
  const deleteOn = list.data?.delete_enabled ?? false;
  const picks = flagged.filter((n) => sel[n.id]);
  const allReleasable = picks.length > 0 && picks.every((n) => !n.release_blocker);

  async function notSpam(n: NumberRow) {
    try {
      await api.post(`/api/numbers/${n.id}/not-spam`);
      toast(`${n.phone_number} is no longer marked as spam.`);
      const next = { ...sel };
      delete next[n.id];
      setSel(next);
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      reloadAll();
    }
  }

  async function doRelease() {
    setBusy(true);
    try {
      const r = await api.post<{ results: ReleaseResult[] }>("/api/spam/release", { ids: picks.map((n) => n.id) });
      setResults(r.results);
      setSel({});
    } catch (e) {
      toast((e as ApiError).message, "error");
    } finally {
      setBusy(false);
      setConfirm(false);
      reloadAll();
    }
  }

  function heldBy(n: SpamListRow) {
    if (!n.used_by) return <span className="muted">Not held</span>;
    return (
      <>
        <Link to={`/agents/${n.used_by.id}`}>{n.used_by.name}</Link>
        {n.used_by.pending_switch_reason === "spam" && <span className="sub">Switching after call</span>}
      </>
    );
  }

  return (
    <>
      <section className="card" aria-labelledby="check-h">
        <h2 id="check-h">Test stack</h2>
        <p className="muted">Numbers are tested from the top. A number that passes goes to the bottom and comes round again; a new number joins at the top. You decide what is spam; nothing is detected or called automatically.</p>
        <p role="status"><strong>{nums.data ? `${nums.data.total} ${nums.data.total === 1 ? "number" : "numbers"} in the stack, ${nums.data.never_tested} never tested.` : nums.error ? "The test stack could not be loaded." : "Loading..."}</strong></p>
        <div className="row">
          <div className="grow">
            <label htmlFor="test-agent">Test agent</label>
            <select id="test-agent" value={agentSel} onChange={(e) => setAgentSel(e.target.value)}>
              <option value="" disabled>Choose an agent</option>
              {(agents.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              <option value={PASTE}>Paste a Bolna agent ID...</option>
            </select>
          </div>
          {agentSel === PASTE && (
            <div className="grow">
              <label htmlFor="test-agent-id">Bolna agent ID</label>
              <input id="test-agent-id" type="text" className="mono-in" value={pasted} autoComplete="off" spellCheck={false}
                onChange={(e) => setPasted(e.target.value)} />
            </div>
          )}
          <div className="grow">
            <label htmlFor="reviewer">Reviewer phone</label>
            <input id="reviewer" type="text" inputMode="tel" className="mono-in" placeholder="+91XXXXXXXXXX" value={reviewer}
              aria-invalid={reviewerBad} onChange={(e) => setReviewer(e.target.value)} onBlur={() => setReviewerTouched(true)} />
            {reviewerBad && <div className="field-err">{PHONE_HINT}</div>}
          </div>
          <button className="btn btn-primary" disabled={!canTest} onClick={startTests}>Test call ({chosenIds.length})</button>
        </div>
        <p className="helper">Calls are placed only when you press the button. They run one at a time and each one costs money.</p>

        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn" onClick={autoSelect}>Auto-select</button>
          <div className="grow">
            <label htmlFor="spam-search">Search numbers</label>
            <input id="spam-search" type="text" inputMode="tel" className="mono-in" placeholder="+91..." value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="chips" role="group" aria-label="Filter numbers" style={{ margin: 0 }}>
            <button className="chip" aria-pressed={neverOnly} onClick={() => setNeverOnly(!neverOnly)}>Never tested</button>
          </div>
        </div>
        <p className="helper">{autoSelectHelp()}</p>
        {autoNote && <p className="helper" role="status">{autoNote}</p>}
        {dropNote && <p className="field-err" role="status">{dropNote}</p>}
        {nums.error && <p className="field-err" role="alert">{nums.error.message}</p>}
        <div style={{ marginTop: 12 }}>
          {!nums.data ? null : !rows.length ? <Empty>The stack is empty. Add numbers on the Number Pool tab first.</Empty> : !shown.length ? <Empty>No numbers match.</Empty> : (
            <TableBox>
              <table style={{ minWidth: 860 }}>
                <thead>
                  <tr><th>Position</th><th className="checkcell"><span className="sr">Select</span></th><th>Number</th><th>Status</th><th>Held by</th><th>Last tested</th><th>Last result</th><th>Actions</th></tr>
                </thead>
                <tbody>
                  {shown.map((n) => (
                    <tr key={n.id}>
                      <td className="mono">{n.position}{n.is_next && <span className="sub">Next</span>}</td>
                      <td className="checkcell">
                        <label className="check"><input type="checkbox" aria-label={`Select ${n.phone_number}`} checked={!!picked[n.id] && n.eligible}
                          disabled={!n.eligible} title={n.reason?.message}
                          onChange={(e) => setPicked({ ...picked, [n.id]: e.target.checked })} /></label>
                      </td>
                      <td className="mono"><Link to={`/numbers/${n.id}`}>{n.phone_number}</Link>
                        {n.reason && !n.test && !n.awaiting_result && <span className="sub">{n.reason.message}</span>}
                      </td>
                      <td>{numberPill(n)}
                        {n.test && <span className="sub">{n.test.status === "calling" ? "Calling..." : `Queued, position ${n.test.position ?? "-"}`}</span>}
                        {n.awaiting_result && <span className="sub">Awaiting result <button className="linkbtn" onClick={() => dismiss(n)}>Dismiss</button></span>}
                      </td>
                      <td>{n.used_by ? <Link to={`/agents/${n.used_by.id}`}>{n.used_by.name}</Link> : <span className="muted">Not held</span>}</td>
                      <td>{n.last_tested_at ? fmtTime(n.last_tested_at) : <span className="muted">Never</span>}</td>
                      <td>{resultText(n.last_test_result)}</td>
                      <td>
                        <div className="row">
                          <button className="btn" disabled={!!n.test} onClick={() => verdict(n, "ok")}>Mark OK</button>
                          <button className="btn" disabled={!!n.test} onClick={() => verdict(n, "spam")}>Mark as spam</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableBox>
          )}
        </div>
      </section>

      <section className="card" aria-labelledby="flagged-h">
        <h2 id="flagged-h">Marked as spam</h2>
        {list.error && <p className="field-err" role="alert">{list.error.message}</p>}
        {!flagged.length ? <Empty>No numbers are marked as spam.</Empty> : (
          <TableBox>
            <table style={{ minWidth: 980 }}>
              <thead>
                <tr><th className="checkcell"><span className="sr">Select</span></th><th>Number</th><th>Provider</th><th>Pool status</th><th>Held by</th><th>Marked on</th><th>Note</th><th>Last test</th><th>Release eligibility</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {flagged.map((n) => (
                  <tr key={n.id}>
                    <td className="checkcell">
                      <label className="check"><input type="checkbox" aria-label={`Select ${n.phone_number}`} checked={!!sel[n.id]}
                        onChange={(e) => setSel({ ...sel, [n.id]: e.target.checked })} /></label>
                    </td>
                    <td className="mono"><Link to={`/numbers/${n.id}`}>{n.phone_number}</Link></td>
                    <td>{n.provider}</td>
                    <td>{numberPill(n)}</td>
                    <td>{heldBy(n)}</td>
                    <td>{fmtTime(n.spam_marked_at)}</td>
                    <td>{n.spam_note || <span className="muted">-</span>}</td>
                    <td>{n.last_tested_at ? `${fmtTime(n.last_tested_at)}, ${resultText(n.last_test_result)}` : <span className="muted">Never</span>}</td>
                    <td>{n.release_blocker ? <><Pill kind="limit">Blocked</Pill><span className="sub">{n.release_blocker.message}</span></> : <Pill kind="available">Ready to release</Pill>}</td>
                    <td><button className="btn" onClick={() => notSpam(n)}>Not spam</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableBox>
        )}
        <div className="actionbar">
          <button className="btn btn-primary" disabled={!deleteOn || !allReleasable || busy} onClick={() => setConfirm(true)}>
            Release selected from pool and Bolna ({picks.length})
          </button>
          {!deleteOn && list.data && <span className="note">Bolna deletion is switched off in settings</span>}
          {deleteOn && picks.length > 0 && !allReleasable && <span className="note">Some selected numbers cannot be released yet. See their eligibility.</span>}
        </div>

        {results && (
          <div style={{ marginTop: 12 }} role="status">
            <h3>Release results</h3>
            <ul className="results">
              {results.map((r) => (
                <li key={r.id}>
                  <Pill kind={r.ok ? "available" : "error"}>{r.ok ? (r.outcome === "already_released" ? "Already released" : "Released") : r.outcome === "failed" ? "Failed" : "Not released"}</Pill>{" "}
                  <span className="mono">{r.phone_number ?? `#${r.id}`}</span> <span className="muted">{r.message}</span>
                </li>
              ))}
            </ul>
            <button className="btn" onClick={() => setResults(null)}>Dismiss results</button>
          </div>
        )}

        <div style={{ marginTop: 12 }}>
          <button className="linkbtn" aria-expanded={showReleased} onClick={() => setShowReleased(!showReleased)}>Released ({list.data?.released_count ?? 0})</button>
          {showReleased && (!released.data?.items.length ? <Empty>No released numbers.</Empty> : (
            <TableBox>
              <table>
                <thead><tr><th>Number</th><th>Provider</th><th>Connected calls</th><th>Total calls</th><th>Released on</th></tr></thead>
                <tbody>
                  {released.data.items.map((n) => (
                    <tr key={n.id}>
                      <td className="mono"><Link to={`/numbers/${n.id}`}>{n.phone_number}</Link></td>
                      <td>{n.provider}</td><td className="mono">{n.connected_count}</td><td className="mono">{n.total_calls}</td>
                      <td>{fmtTime(n.released_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableBox>
          ))}
        </div>
      </section>

      <ConfirmDialog open={confirm} busy={busy} title="Release numbers?" confirmLabel={`Release ${picks.length} ${picks.length === 1 ? "number" : "numbers"}`}
        onConfirm={doRelease} onCancel={() => setConfirm(false)}>
        <ul className="mono">{picks.map((n) => <li key={n.id}>{n.phone_number}</li>)}</ul>
        <p>This permanently deletes these numbers from Bolna and removes them from the pool. This cannot be undone.</p>
      </ConfirmDialog>
    </>
  );
}
