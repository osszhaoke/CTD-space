import type { Sample, Source } from "./types";

export function overviewSource(sources: Source[], kind: string, selectedId?: string | null) {
  return sources.find((source) => source.kind === kind && source.id === selectedId)
    ?? sources.find((source) => source.kind === kind && source.enabled)
    ?? sources.find((source) => source.kind === kind);
}

export function freshFor(source?: Source) {
  return source?.kind === "eight_sleep" ? 45000 : 10000;
}

export function isSourceFresh(source: Source | undefined, collectorOnline: boolean, now: number) {
  return !!(source?.enabled && collectorOnline && source.last_sample_at
    && (["streaming", "partial"].includes(source.status) || source.kind === "eight_sleep" && source.status === "waiting")
    && now - new Date(source.last_sample_at).getTime() < freshFor(source));
}

export function sourceStatus(source: Source | undefined, collectorOnline: boolean, now: number) {
  if (!source) return "待接入";
  if (!source.enabled) return source.status === "discovered" ? "待启用" : "已暂停";
  if (!collectorOnline) return "采集服务离线";
  if (source.status === "streaming") {
    return isSourceFresh(source, collectorOnline, now)
      ? source.kind === "eight_sleep" ? "已同步" : "采集中"
      : "等待新数据";
  }
  return ({ discovered: "待启用", pending: "准备连接", connecting: "正在连接", waiting: "等待数据",
    offline: "已断开", error: "连接异常", paused: "已暂停", partial: "部分数据已同步" } as Record<string, string>)[source.status] ?? source.status;
}

export function sampleSeries(series: Record<string, Sample[]> | undefined, source: Source | undefined,
  selectedId: string | null | undefined, selectedSamples: Sample[] = []) {
  if (!source) return [];
  return series?.[source.id] ?? (source.id === selectedId ? selectedSamples : []);
}

export function timelineSamples(samples: Sample[], limit = 12) {
  if (samples.length <= limit) return samples;
  return Array.from({ length: limit }, (_, index) => samples[Math.round(index * (samples.length - 1) / (limit - 1))]);
}
