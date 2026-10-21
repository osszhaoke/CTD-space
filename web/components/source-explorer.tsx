"use client";

import { useState } from "react";
import { airMetrics, measurement } from "@/lib/airgradient";
import { eightMetrics, lastBedDisplay } from "@/lib/eight-sleep";
import { freshFor, isSourceFresh, sourceStatus } from "@/lib/dashboard";
import { frequency } from "@/lib/stats";
import type { Dashboard } from "@/lib/types";
import { displayNumber, displayTime, Icon } from "./room-ui";

export function SourceExplorer({ data, selected, now, collectorOnline, onSelect, onChanged }: {
  data: Dashboard | null; selected: string | null; now: number; collectorOnline: boolean;
  onSelect: (id: string) => void; onChanged: () => void;
}) {
  const [airMetric, setAirMetric] = useState("rco2");
  const [eightMetric, setEightMetric] = useState("currentDeviceLevel");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const source = data?.sources.find(s => s.id === data.selectedSourceId);
  const switching = !!selected && selected !== data?.selectedSourceId;
  const isAir = source?.kind === "airgradient", isEight = source?.kind === "eight_sleep";
  const multi = isAir || isEight;
  const definitions = isEight ? eightMetrics : airMetrics;
  const key = isEight ? eightMetric : airMetric;
  const payload = source?.latest_metadata?.measurements;
  const snapshots = data?.samples ?? [];
  const samples = snapshots.map(s => ({ ...s, value: multi ? measurement(s.metadata.measurements, key) : s.value }));
  const retainedBed = isEight ? lastBedDisplay(source, snapshots) : undefined;
  const valueTime = retainedBed?.updated_at[key] ?? source?.last_sample_at;
  const value = multi ? measurement(retainedBed?.measurements ?? payload, key) : source?.latest_value;
  const label = multi ? definitions[key]?.label ?? key : source?.metric ?? "读数";
  const unit = multi ? definitions[key]?.unit ?? "" : source?.unit ?? "";
  const live = isSourceFresh(source, collectorOnline, now);
  const hz = live ? frequency(snapshots, now, freshFor(source)) : null;

  async function toggle() {
    if (!source || switching || busy) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/sources/${encodeURIComponent(source.id)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !source.enabled }), signal: AbortSignal.timeout(10000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "保存失败");
      onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }

  return <div className="explorer-content">
    <div className="source-tabs" aria-label="选择数据源">{data?.sources.map(item => <button type="button" key={item.id}
      className={`source-tab ${item.id === source?.id ? "selected" : ""}`} aria-pressed={item.id === source?.id}
      onClick={() => { onSelect(item.id); setError(""); }}>
      <Icon name={item.kind === "eight_sleep" ? "bed" : "air"}/>
      <span><strong>{item.name}</strong><small>{sourceStatus(item, collectorOnline, now)}</small></span>
    </button>)}</div>
    {!source ? <p className="empty-note">{data ? "暂无数据源。连接环境监测设备或配置 Eight Sleep 后，采集服务会自动发现。" : "正在读取采集服务状态…"}</p> : <>
      <div className="source-card">
        <div className="source-row">
          <div><span className="source-kind">{source.kind === "eight_sleep" ? "床微环境" : "室内环境"}</span>
            <h2>{source.name}</h2><p className="source-id">{source.id}</p></div>
          <button className={`button ${source.enabled ? "subtle" : "primary"}`} onClick={toggle} disabled={busy || switching}>
            {busy ? "…" : source.enabled ? "暂停采集" : "开始采集"}
          </button>
        </div>
        {source.error && <p className="notice" role="alert">{source.error}</p>}
        {error && <p className="notice" role="alert">{error}</p>}
      </div>

      <div className="stats-grid">
        <div><span>当前读数</span><strong className="text-stat">{displayNumber(value)} <small>{unit}</small></strong><p>{valueTime ? displayTime(valueTime, true) : "暂无采样"}</p></div>
        <div><span>采集状态</span><strong className="text-stat">{sourceStatus(source, collectorOnline, now)}</strong><p>{hz ? `${hz} 次/秒` : "暂无频率"}</p></div>
        <div><span>累计样本</span><strong className="text-stat">{Number(source.sample_count).toLocaleString()}</strong><p>记录保存在本地 PostgreSQL</p></div>
      </div>

      <div className="table-wrap"><table><thead><tr><th>接收时间</th><th>{label}</th><th>记录状态</th></tr></thead><tbody>{samples.length ? samples.slice(-8).reverse().map(s => <tr key={s.id}><td>{displayTime(s.recorded_at, true)}</td><td>{isEight && eightMetrics[key]?.approximate && s.value !== null ? "≈" : ""}{displayNumber(s.value)} <span className="muted">{unit}</span></td><td>已保存</td></tr>) : <tr><td colSpan={3} className="empty-note">暂无采集记录</td></tr>}</tbody></table></div>
    </>}
  </div>;
}
