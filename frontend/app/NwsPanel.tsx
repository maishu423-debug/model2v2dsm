"use client";

import { useEffect, useState } from "react";

// NWS data for Miami International (KMIA). Display only: it never feeds the forecast or the bot.
//   • DSM: the ASOS Daily Summary Message (latest + last final summary) with the raw text

type DsmEntry = {
  kind: "partial" | "final";
  date: string; // YYYY-MM-DD, the DSM day (midnight to midnight local STANDARD time)
  issued_at: string;
  report_time_lst: string | null;
  product_id: string;
  high_f: number | null;
  high_time_lst: string | null;
  low_f: number | null;
  low_time_lst: string | null;
  coop_high_f: number | null;
  coop_low_f: number | null;
  min_slp_inhg: number | null;
  min_slp_time_lst: string | null;
  precip_in: number | null;
  precip_trace: boolean;
  raw: string;
};

type DsmPayload = {
  fetched_at?: string; // when the backend last got this from upstream
  latest: DsmEntry | null;
  last_final: DsmEntry | null;
  stale?: boolean;
  error?: string;
  note?: string;
};

const ET = "America/New_York";
const DSM_POLL_MS = 60_000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// ── Helpers ──────────────────────────────────────────────────────────────────

function usePolled<T>(url: string, everyMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    async function load() {
      try {
        const res = await fetch(url, { cache: "no-store" });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(typeof body.error === "string" ? body.error : `HTTP ${res.status}`);
        if (!ignore) {
          setData(body as T);
          setError(null);
        }
      } catch (err) {
        if (!ignore) setError(err instanceof Error ? err.message : "Unavailable");
      }
    }
    load();
    const timer = window.setInterval(load, everyMs);
    return () => {
      ignore = true;
      window.clearInterval(timer);
    };
  }, [url, everyMs]);

  return { data, error };
}

/** Re-renders on a timer so "x min ago" labels stay correct between fetches. */
function useNow(everyMs = 15_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(timer);
  }, [everyMs]);
  return now;
}

function ageMinutes(iso: string, now: number) {
  return Math.max(0, Math.floor((now - new Date(iso).getTime()) / 60_000));
}

function agoText(minutes: number) {
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h} h ago` : `${h} h ${m} min ago`;
}

/** "14:25" -> "2:25 PM" (a clock reading in local standard time, not converted). */
function clock12(hhmm: string | null) {
  if (!hhmm) return "—";
  const [h, m] = hhmm.split(":").map(Number);
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

function shortDate(iso: string) {
  const [, mo, d] = iso.split("-").map(Number);
  return `${MONTHS[mo - 1]} ${d}`;
}

function etClock(iso: string, withDate = false) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: ET,
    hour: "numeric",
    minute: "2-digit",
    ...(withDate ? { month: "short", day: "numeric" } : {}),
  });
}

function deg(value: number | null) {
  return value == null ? "—" : `${value}°F`;
}

function precipText(e: DsmEntry) {
  if (e.precip_trace) return "Trace";
  return e.precip_in == null ? "—" : `${e.precip_in.toFixed(2)} in`;
}

// ── DSM card ─────────────────────────────────────────────────────────────────

function DsmBlock({ entry }: { entry: DsmEntry }) {
  const heading =
    entry.kind === "partial"
      ? `Today so far · ${shortDate(entry.date)}`
      : `Final summary · ${shortDate(entry.date)}`;
  return (
    <div className="nws-dsm-block">
      <div className="nws-dsm-head">
        <strong>{heading}</strong>
        <span className="nws-muted">
          issued {etClock(entry.issued_at, true)} ET
          {entry.report_time_lst ? ` · covers through ${clock12(entry.report_time_lst)} LST` : ""}
        </span>
      </div>
      <div className="nws-stats">
        <div className="nws-stat">
          <span className="nws-stat-label">High</span>
          <strong className="nws-stat-hi">{deg(entry.high_f)}</strong>
          <span className="nws-muted">{entry.high_time_lst ? `${clock12(entry.high_time_lst)} LST` : ""}</span>
        </div>
        <div className="nws-stat">
          <span className="nws-stat-label">Low</span>
          <strong className="nws-stat-lo">{deg(entry.low_f)}</strong>
          <span className="nws-muted">{entry.low_time_lst ? `${clock12(entry.low_time_lst)} LST` : ""}</span>
        </div>
        <div className="nws-stat">
          <span className="nws-stat-label">Precip</span>
          <strong>{precipText(entry)}</strong>
          <span className="nws-muted">{entry.kind === "partial" ? "so far" : "24 h"}</span>
        </div>
        <div className="nws-stat">
          <span className="nws-stat-label">Min pressure</span>
          <strong>{entry.min_slp_inhg != null ? `${entry.min_slp_inhg.toFixed(2)}″` : "—"}</strong>
          <span className="nws-muted">{entry.min_slp_time_lst ? `${clock12(entry.min_slp_time_lst)} LST` : ""}</span>
        </div>
      </div>
      <details className="nws-raw">
        <summary>Raw DSM text</summary>
        <pre>{entry.raw}</pre>
      </details>
    </div>
  );
}

function DsmCard() {
  const { data, error } = usePolled<DsmPayload>("/api/kalshi/nws/dsm", DSM_POLL_MS);
  const now = useNow();
  const blocks: DsmEntry[] = [];
  if (data?.latest) blocks.push(data.latest);
  if (data?.last_final && data.last_final.product_id !== data.latest?.product_id) blocks.push(data.last_final);

  return (
    <section className="nws-card">
      <h3 className="algo-section-title">
        NWS Daily Summary (DSM)
        <span className="algo-hint-inline"> — Miami Intl · KMIA</span>
      </h3>
      {!data && !error && <p className="algo-hint">Loading…</p>}
      {error && !data && <p className="algo-error">{error}</p>}
      {data?.stale && <p className="nws-warn">Showing the last good copy — NWS data could not be refreshed.</p>}
      {data && blocks.length === 0 && <p className="algo-hint">No DSM has been published for KMIA yet.</p>}
      {blocks.map((b) => (
        <DsmBlock key={b.product_id} entry={b} />
      ))}
      {data && (
        <p className="nws-fresh">
          {data.latest ? `Newest DSM issued ${agoText(ageMinutes(data.latest.issued_at, now))}` : "No DSM yet"}
          {data.fetched_at ? ` · checked ${etClock(data.fetched_at)} ET` : ""}
          {" · DSMs are issued irregularly"}
        </p>
      )}
      {data && <p className="nws-foot">{data.note ?? "DSM times are local standard time (LST), with no daylight saving."}</p>}
    </section>
  );
}

// ── Panel ────────────────────────────────────────────────────────────────────

export default function NwsPanel() {
  return (
    <div className="nws-panel">
      <DsmCard />
    </div>
  );
}
