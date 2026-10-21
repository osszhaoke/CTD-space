"use client";

import { useState } from "react";
import { airMetrics, airReferences, lastAirDisplay, measurement } from "@/lib/airgradient";
import { availableBedMetrics, bedDisplayTime, bedMetricValue, eightMetrics, lastBedDisplay } from "@/lib/eight-sleep";
import { overviewSource, sampleSeries, timelineSamples } from "@/lib/dashboard";
import { useRoom } from "@/lib/use-room";
import type { Sample, Source } from "@/lib/types";
import { displayNumber, displayTime, Icon, metricIcon, Sparkline, Starfield, type Point } from "@/components/room-ui";
import "./display.css";

const AIR = ["atmp", "rhum", "rco2", "pm02", "pm01", "pm10", "tvocIndex", "noxIndex"];
type Inspection = { sourceId: string; sample: Sample };

function DisplayMetric({ label, unit, value, icon, points, time, approximate = false, reference }: {
  label: string; unit: string; value: number | null; icon: string; points: Point[]; time?: string | null; approximate?: boolean; reference?: { text: string; description: string };
}) {
  return <article className={`display-metric${reference ? " has-reference" : ""}`} aria-label={label}>
    {reference && <small className="display-reference" title={reference.description} aria-label={reference.description}>{reference.text}</small>}
    <div className="display-metric-heading"><Icon name={icon}/><h3>{label}</h3></div>
    <div className="display-reading"><strong>{approximate && value !== null ? "≈" : ""}{displayNumber(value)}</strong><span>{unit}</span></div>
    <div className="display-trend"><Sparkline points={points} selectedTime={time} quiet/></div>
  </article>;
}

export default function Page() {
  const room = useRoom();
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [hover, setHover] = useState<Inspection | null>(null);
  const sources = room.data?.sources ?? [];
  const air = overviewSource(sources, "airgradient", room.data?.selectedSourceId);
  const bed = overviewSource(sources, "eight_sleep", room.data?.selectedSourceId);
  const seriesFor = (source?: Source) => sampleSeries(room.data?.series, source, room.data?.selectedSourceId, room.data?.samples);
  const airSamples = seriesFor(air);
  const bedSamples = seriesFor(bed);
  const active = hover?.sourceId === air?.id ? hover : inspection?.sourceId === air?.id ? inspection : null;
  const airDisplay = active
    ? lastAirDisplay(undefined, airSamples.filter(sample => Date.parse(sample.recorded_at) <= Date.parse(active.sample.recorded_at)))
    : lastAirDisplay(air, airSamples);
  const airPayload = airDisplay.measurements;
  const airKeys = AIR.filter(key => measurement(airPayload, key) !== null);
  const airUpdatedAt = bedDisplayTime(airDisplay, airKeys);
  const bedDisplay = lastBedDisplay(bed, bedSamples);
  const bedPayload = bedDisplay.measurements;
  const points = timelineSamples(airSamples);
  const bedKeys = availableBedMetrics(bedPayload);
  const bedUpdatedAt = bedDisplayTime(bedDisplay, bedKeys);
  const sleepScore = measurement(bedPayload, "sleep_score");
  const showsSleepData = bedKeys.some(key => ["heartRate", "hrv", "respiratoryRate", "tempBedC", "tempRoomC"].includes(key));
  const roomName = process.env.NEXT_PUBLIC_ROOM_NAME || "睡眠房";

  return <>
    <Starfield/>
    <main className="experience-display">
      <div className="display-frame">
        <header className="display-header">
          <div className="display-brand"><span>CTDS<i>✦</i></span><h1>{roomName}</h1></div>
          <div className="display-tools">
            <time className="display-clock">{room.clock ? displayTime(new Date(room.clock).toISOString()) : "—"}</time>
            <button className="display-tool" onClick={room.toggleTheme} aria-label={room.theme === "dark" ? "切换到白昼模式" : "切换到黑夜模式"}>
              <Icon name={room.theme === "dark" ? "sun" : "moon"}/>
            </button>
            <a className="display-tool display-manage" href="/manage" aria-label="管理"><Icon name="sliders"/></a>
          </div>
        </header>

        <div className="display-panels">
          <section className="display-section display-air" aria-labelledby="air-title">
            <header className="display-section-heading"><div><Icon name="home"/><h2 id="air-title">室内环境 · 空间空气与温湿</h2></div>
              <time>{airUpdatedAt ? displayTime(airUpdatedAt) : ""}</time>
            </header>
            <div className="display-air-grid">{airKeys.map(key => <DisplayMetric key={key} label={airMetrics[key].label} unit={airMetrics[key].unit}
              value={measurement(airPayload, key)} icon={metricIcon(key)} reference={airReferences[key]}
              points={airSamples.map(s => ({ time: s.recorded_at, value: measurement(s.metadata.measurements, key) }))}
              time={active?.sample.recorded_at}/>)}</div>
            <div className="display-timeline" aria-label="环境时间轴">
              <div className="display-timeline-track">{points.map((sample, i) => <button key={sample.id} type="button"
                className={active ? active.sample.id === sample.id ? "is-selected" : "" : i === points.length - 1 ? "is-selected" : ""}
                aria-label={`查看 ${displayTime(sample.recorded_at, true)} 的环境快照`}
                aria-pressed={active ? active.sample.id === sample.id : i === points.length - 1}
                onMouseEnter={() => air && setHover({ sourceId: air.id, sample })} onMouseLeave={() => setHover(null)}
                onFocus={() => air && setHover({ sourceId: air.id, sample })} onBlur={() => setHover(null)}
                onClick={() => { setHover(null); setInspection(inspection?.sample.id === sample.id ? null : air ? { sourceId: air.id, sample } : null); }}>
                <span/>
              </button>)}</div>
              <button className={`display-now ${active ? "" : "is-selected"}`} onClick={() => { setHover(null); setInspection(null); }}>现在</button>
            </div>
          </section>

          <section className="display-section display-bed" aria-labelledby="bed-title">
            <header className="display-section-heading"><div><Icon name="bed"/><h2 id="bed-title">床微环境 · 睡眠体征与温控</h2></div>
              {sleepScore !== null && <span className="display-score">睡眠评分 <strong>{displayNumber(sleepScore)}</strong></span>}
            </header>
            <div className="display-bed-grid" data-count={bedKeys.length}>{bedKeys.map(key => <DisplayMetric key={key} label={eightMetrics[key].label}
              unit={eightMetrics[key].unit} approximate={eightMetrics[key].approximate} value={bedMetricValue(bedPayload, key)} icon={metricIcon(key)}
              points={bedSamples.map(s => ({ time: s.recorded_at, value: bedMetricValue(s.metadata.measurements, key) }))}/>)}</div>
            <div className="display-bed-date"><Icon name={showsSleepData ? "moon" : "clock"}/><time>{showsSleepData && typeof bedPayload?.sleep_day === "string" ? bedPayload.sleep_day : bedUpdatedAt ? displayTime(bedUpdatedAt, true) : ""}</time></div>
          </section>
        </div>
        <footer className="display-footer"><span>CTDS</span><i>✦</i><span>空间 · 身体</span></footer>
      </div>
    </main>
  </>;
}
