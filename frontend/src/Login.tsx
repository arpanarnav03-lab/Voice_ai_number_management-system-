import { FormEvent, useState } from "react";
import { ApiError, api } from "./api";

export default function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await api.post("/api/auth/login", { password: pw });
      onDone();
    } catch (x) {
      setErr((x as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card login" onSubmit={submit}>
      <h1>Bolna Number Manager</h1>
      <p className="muted">Enter the app password to continue.</p>
      <label htmlFor="pw">Password</label>
      <input id="pw" type="password" value={pw} autoFocus autoComplete="current-password" onChange={(e) => setPw(e.target.value)} aria-invalid={!!err} />
      {err && <div className="field-err" role="alert">{err}</div>}
      <p />
      <button className="btn btn-primary" type="submit" disabled={busy || !pw}>Sign in</button>
    </form>
  );
}
