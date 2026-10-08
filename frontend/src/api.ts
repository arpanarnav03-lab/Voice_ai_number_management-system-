import { useCallback, useEffect, useRef, useState } from "react";

export class ApiError extends Error {
  code: string;
  status: number;
  extra: Record<string, any>;
  constructor(status: number, body: any) {
    super(body?.message || `Request failed (${status})`);
    this.status = status;
    this.code = body?.code || "ERROR";
    this.extra = body || {};
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, { code: "NETWORK", message: "Cannot reach the server. Is it running?" });
  }
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  if (!res.ok) {
    if (res.status === 401 && data?.code === "UNAUTHORIZED") window.dispatchEvent(new Event("auth-required"));
    throw new ApiError(res.status, data);
  }
  return data as T;
}

export const api = {
  get: <T,>(url: string) => request<T>("GET", url),
  post: <T,>(url: string, body?: unknown) => request<T>("POST", url, body ?? {}),
  put: <T,>(url: string, body?: unknown) => request<T>("PUT", url, body ?? {}),
};

/** Fetch now and then every `ms` (5s by default) so lock and call states stay live. */
export function useApi<T>(path: string | null, ms = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api.get<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(e as ApiError);
    }
  }, [path]);
  useEffect(() => {
    alive.current = true;
    setData(null);
    load();
    const t = setInterval(() => {
      if (!document.hidden) load();
    }, ms);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [load, ms]);
  return { data, error, reload: load };
}

// ---------------------------------------------------------------- types
export type NumberRef = {
  id: number;
  phone_number: string;
  status: string;
  connected_count: number;
  provider: string;
  missing_on_bolna: boolean;
};
export type AgentRow = {
  id: number;
  bolna_agent_id: string;
  name: string;
  number: NumberRef | null;
  status: "ready" | "near_limit" | "on_call" | "pool_empty" | "no_number" | "number_missing";
  on_call: boolean;
  active_calls: number;
  pending_switch: boolean;
  last_used: string | null;
};
export type NumberRow = {
  id: number;
  phone_number: string;
  provider: string;
  status: "available" | "in_use" | "limit_reached" | "discovered" | "ignored";
  connected_count: number;
  total_calls: number;
  agents_served: number;
  threshold: number;
  linked: boolean;
  linked_in_bolna: boolean;
  bolna_linked_agent_id: string | null;
  allow_auto_switch: boolean;
  missing_on_bolna: boolean;
  missing_since: string | null;
  discovered_at: string | null;
  added_at: string;
  used_by: { id: number; name: string; on_call: boolean; pending_switch: boolean } | null;
};
export type CallRow = {
  id: number;
  agent_id: number;
  agent_name: string;
  number_id: number;
  phone_number: string;
  recipient: string;
  placed_at: string;
  status: string;
  duration: number | null;
  result: "connected" | "voicemail" | "no_answer" | "busy" | "in_progress" | "unclear" | "failed";
  reason: string;
  needs_review: boolean;
};
export type HistoryRow = {
  id: number;
  number_id: number;
  phone_number: string;
  started_at: string;
  ended_at: string | null;
  connected_calls: number;
  reason: string;
  end_reason: string | null;
};

export type SyncRun = {
  id: number;
  status: "success" | "failed";
  started_at: string;
  finished_at: string | null;
  total_on_bolna: number;
  new_count: number;
  missing_count: number;
  skipped_invalid_count: number;
  error_message: string | null;
  skipped_numbers: string[];
};
export type SyncStatus = {
  running: boolean;
  last_run: SyncRun | null;
  last_success: SyncRun | null;
  discovered_count: number;
  ignored_count: number;
};
export type NumberPage = {
  threshold: number;
  items: NumberRow[];
  total: number;
  page: number;
  page_size: number;
  unlinked_total?: number;
};

export const PHONE_RE = /^\+91[0-9]{10}$/;
export const PHONE_HINT = "Use +91 followed by 10 digits";

export function fmtTime(iso: string | null | undefined) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
export function fmtDuration(d: number | null) {
  return d === null || d === undefined ? "-" : `${Math.round(d)}s`;
}

export function timeAgo(iso: string | null | undefined) {
  if (!iso) return "never";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}
