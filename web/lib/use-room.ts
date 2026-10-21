"use client";

import { useEffect, useState } from "react";
import type { Dashboard } from "./types";

const THEME_KEY = "ctds-room-theme";

export function useRoom() {
  const [clientId, setClientId] = useState<string | null>(null);
  const [data, setData] = useState<Dashboard | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [clock, setClock] = useState(0);
  const [receivedAt, setReceivedAt] = useState(0);
  const [theme, setTheme] = useState("dark");

  useEffect(() => {
    try {
      const saved = localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark";
      setTheme(saved); document.documentElement.dataset.theme = saved;
    } catch { /* Storage is optional. */ }
    let id;
    try { id = localStorage.getItem("ctds-client-id"); } catch { /* Optional storage. */ }
    if (!id || !/^[0-9a-f-]{36}$/i.test(id)) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
      id = [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join("-");
      try { localStorage.setItem("ctds-client-id", id); } catch { /* Session still works. */ }
    }
    setClientId(id);
    setClock(Date.now());
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!clientId) return;
    let disposed = false;
    let controller: AbortController;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const query = new URLSearchParams({ client: clientId! });
        if (selected) query.set("source", selected);
        const response = await fetch(`/api/dashboard?${query}`, { signal: controller.signal, cache: "no-store" });
        const result: Dashboard & { error?: string } = await response.json();
        if (!response.ok) throw new Error(result.error || "暂时无法读取数据");
        if (!disposed) {
          setData(result); setReceivedAt(Date.now()); setError("");
          setSelected(current => current ?? result.selectedSourceId);
        }
      } catch (e) {
        if (!disposed) setError(e instanceof Error && e.name !== "AbortError" ? e.message : "页面连接超时，正在重试。");
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    }
    void refresh();
    return () => { disposed = true; controller?.abort(); clearTimeout(timer); };
  }, [selected, revision, clientId]);

  const now = data ? Date.parse(data.serverTime) + Math.max(0, clock - receivedAt) : clock;
  const collectorOnline = !!data?.collector && now - Date.parse(data.collector.heartbeat) < 15000 && !error;

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next); document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch { /* Optional persistence. */ }
  }

  return { clientId, data, selected, setSelected, error, clock, now, collectorOnline, theme, toggleTheme,
    refresh: () => setRevision(v => v + 1) };
}
