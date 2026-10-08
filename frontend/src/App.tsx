import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import AgentDetail from "./AgentDetail";
import AgentsTab from "./AgentsTab";
import Login from "./Login";
import NumberDetail from "./NumberDetail";
import PoolTab from "./PoolTab";
import { api } from "./api";
import { ToastProvider } from "./ui";

type AuthState = { required: boolean; authenticated: boolean; mode: string } | null;

export default function App() {
  const [auth, setAuth] = useState<AuthState>(null);
  const [down, setDown] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setAuth(await api.get<AuthState>("/api/auth/status"));
      setDown(false);
    } catch {
      setDown(true);
    }
  }, []);

  useEffect(() => {
    refresh();
    const h = () => setAuth((a) => (a ? { ...a, authenticated: false } : a));
    window.addEventListener("auth-required", h);
    return () => window.removeEventListener("auth-required", h);
  }, [refresh]);

  async function logout() {
    await api.post("/api/auth/logout");
    refresh();
  }

  if (down) return <main><section className="card"><h1>Cannot reach the server</h1><p className="muted">Start the backend (python run.py) and reload this page.</p><button className="btn" onClick={refresh}>Try again</button></section></main>;
  if (!auth) return null;
  if (auth.required && !auth.authenticated) return <main><Login onDone={refresh} /></main>;

  return (
    <ToastProvider>
      <BrowserRouter>
        <header className="topbar">
          <div className="topbar-in">
            <h1>Bolna Number Manager</h1>
            <nav aria-label="Main">
              <NavLink to="/" end>Agents</NavLink>
              <NavLink to="/pool">Number Pool</NavLink>
            </nav>
            <span className="spacer" />
            {auth.mode === "fake"
              ? <span className="modebadge" title="BOLNA_MODE=fake: no real call is ever placed">Simulated Bolna, no real calls</span>
              : <span className="modebadge real" title="BOLNA_MODE=real">Live Bolna, calls are real</span>}
            {auth.required && <button className="btn" onClick={logout}>Sign out</button>}
          </div>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<AgentsTab />} />
            <Route path="/pool" element={<PoolTab />} />
            <Route path="/agents/:id" element={<AgentDetail />} />
            <Route path="/numbers/:id" element={<NumberDetail />} />
            <Route path="*" element={<section className="card"><p>Page not found.</p></section>} />
          </Routes>
        </main>
      </BrowserRouter>
    </ToastProvider>
  );
}
