"use client";

import { useRoom } from "@/lib/use-room";
import { SourceExplorer } from "@/components/source-explorer";
import { Icon, Starfield } from "@/components/room-ui";

export default function ManagePage() {
  const room = useRoom();
  return <>
    <Starfield/>
    <main className="room-shell"><div className="room-panel">
      <header className="room-header">
        <div className="room-title"><p className="eyebrow">CTDS · 运维管理</p><h1>数据源与系统状态</h1></div>
        <a className="button" href="/">返回大屏展示</a>
      </header>
      {(room.error || room.data?.collector?.error) && <p className="notice" role="alert">{room.error || room.data?.collector?.error}</p>}
      <section className="source-explorer" aria-label="数据源与采集记录">
        <div className="manage-heading"><Icon name="sliders"/><h2>数据源与实时流水</h2></div>
        <SourceExplorer data={room.data} selected={room.selected} now={room.now}
          collectorOnline={room.collectorOnline} onSelect={room.setSelected} onChanged={room.refresh}/>
      </section>
    </div></main>
  </>;
}
