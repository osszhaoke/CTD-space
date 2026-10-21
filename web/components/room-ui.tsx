import type { ReactNode } from "react";

const paths: Record<string, ReactNode> = {
  home: <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/><path d="M9 21v-8h6v8"/></>,
  bed: <><path d="M3 4v17M21 11v10M3 17h18M3 8h15a3 3 0 0 1 3 3v6M8 8v9"/></>,
  heart: <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>,
  temperature: <><path d="M10 14.5V5a2 2 0 0 1 4 0v9.5a4 4 0 1 1-4 0Z"/><path d="M12 8v9"/></>,
  humidity: <path d="M12 3c-2 5-7 7-7 12a7 7 0 0 0 14 0c0-5-5-7-7-12ZM8 15a4 4 0 0 0 4 4"/>,
  air: <><path d="M3 8h12a3 3 0 1 0-3-3M3 12h16a2 2 0 1 1-2 2M3 16h7a2 2 0 1 1-2 2"/></>,
  particle: <><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/></>,
  wave: <path d="M2 12h4l3-8 5 16 3-8h5"/>,
  moon: <path d="M20.9 13a9 9 0 1 1-9.9-9.9A7 7 0 0 0 20.9 13Z"/>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  sliders: <><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="17" r="2"/></>,
  arrow: <path d="m9 5 7 7-7 7"/>,
};

export function Icon({ name, className = "" }: { name: string; className?: string }) {
  return <svg className={`icon ${className}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.wave}</svg>;
}

export function metricIcon(key: string) {
  if (/atmp|temp|level/i.test(key)) return "temperature";
  if (/Angle$/.test(key)) return "bed";
  if (key === "rhum") return "humidity";
  if (/heart/i.test(key)) return "heart";
  if (/pm/.test(key)) return "particle";
  if (/co2|Index/.test(key)) return "air";
  return "wave";
}

export function displayTime(value?: string | null, seconds = false) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return "—";
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: false });
}

export function displayNumber(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) : "—";
}

export type Point = { time: string; value: number | null };

export function Sparkline({ points, selectedTime, large = false, quiet = false }: { points: Point[]; selectedTime?: string | null; large?: boolean; quiet?: boolean }) {
  const valid = points.filter((p): p is Point & { value: number } => p.value !== null && Number.isFinite(p.value) && Number.isFinite(Date.parse(p.time)));
  const width = 300, height = large ? 150 : 46, pad = 6;
  if (!valid.length && quiet) return <svg className="sparkline spark-silent" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true"><path className="spark-grid" d={`M${pad} ${height / 2}H${width - pad}`}/></svg>;
  if (!valid.length) return <div className={`spark-empty ${large ? "large" : ""}`}>{large ? "收到有效数据后，这里会显示趋势" : "暂无趋势"}</div>;
  const values = valid.map(p => p.value);
  const low = Math.min(...values), high = Math.max(...values);
  const start = Date.parse(valid[0].time), span = Math.max(1, Date.parse(valid.at(-1)!.time) - start);
  const xy = valid.map(p => [pad + (Date.parse(p.time) - start) / span * (width - pad * 2), high === low ? height / 2 : height - pad - (p.value - low) / (high - low) * (height - pad * 2)]);
  const chosen = selectedTime ? valid.findLastIndex(p => Date.parse(p.time) <= Date.parse(selectedTime)) : valid.length - 1;
  const dot = xy[chosen];
  // Missing readings break the curve rather than inventing intermediate values.
  let path = "", drawing = false, index = 0;
  for (const point of points) {
    if (point.value === null || !Number.isFinite(point.value) || !Number.isFinite(Date.parse(point.time))) { drawing = false; continue; }
    const [x, y] = xy[index++];
    path += `${drawing ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)} `;
    drawing = true;
  }
  return <svg className={`sparkline ${large ? "large" : ""}`} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`记录趋势，最低 ${displayNumber(low)}，最高 ${displayNumber(high)}`}>
    <path className="spark-grid" d={`M${pad} ${height - pad}H${width - pad}`} />
    {dot && <path className="spark-guide" d={`M${dot[0]} 1V${height}`} />}
    <path className="spark-trace" d={path} />
    {dot && <circle className="spark-dot" cx={dot[0]} cy={dot[1]} r={large ? 2.5 : 2} />}
  </svg>;
}

export function MetricCard({ label, unit, value, icon, points, timestamp, status, compact = false, muted = false }: {
  label: string; unit: string; value: number | null; icon: string; points: Point[];
  timestamp?: string | null; status: string; compact?: boolean; muted?: boolean;
}) {
  return <article className={`metric-card ${compact ? "compact" : ""} ${muted ? "is-muted" : ""}`} aria-label={label}>
    <div className="metric-head"><span className="metric-icon"><Icon name={icon}/></span><h3>{label}</h3></div>
    <div className="metric-reading"><strong>{displayNumber(value)}</strong><span>{unit}</span></div>
    <Sparkline points={points} selectedTime={timestamp} />
    <div className="metric-foot"><span>{value === null ? "未提供" : status}</span><time dateTime={timestamp ?? undefined}>{displayTime(timestamp)}</time></div>
  </article>;
}

export function Starfield() {
  const stars = [[260,190],[625,238],[410,414],[752,408],[893,172],[1233,233],[1452,344],[1540,577],[1218,662],[738,618],[1650,265],[1380,820],[180,760],[560,880],[980,790]];
  const lines = [[0,1],[2,1],[1,4],[4,5],[1,3],[5,6],[6,7],[7,8],[8,9],[9,14],[12,13]];
  return <svg className="starfield" viewBox="0 0 1600 1000" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
    {[260,420,600,800,1020].map(r => <ellipse key={r} className="sky-ring" cx="990" cy="420" rx={r} ry={r * .6} />)}
    {lines.map(([a,b],i) => <path key={i} className="sky-line" d={`M${stars[a][0]} ${stars[a][1]}L${stars[b][0]} ${stars[b][1]}`} />)}
    {stars.map(([x,y],i) => <g key={i}>{i % 3 === 1 && <circle className="sky-orbit" cx={x} cy={y} r="7"/>}<circle className="sky-star" cx={x} cy={y} r={i % 2 ? 1.7 : 2.4}/></g>)}
  </svg>;
}
