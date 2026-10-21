import type { BedDisplay, Sample, Source } from "./types";

export const airMetrics: Record<string, { label: string; unit: string }> = {
  rco2: { label: "CO₂", unit: "ppm" },
  atmp: { label: "温度", unit: "°C" },
  rhum: { label: "相对湿度", unit: "%" },
  pm01: { label: "PM1.0", unit: "µg/m³" },
  pm02: { label: "PM2.5", unit: "µg/m³" },
  pm10: { label: "PM10", unit: "µg/m³" },
  tvocIndex: { label: "VOC 指数", unit: "" },
  noxIndex: { label: "NOx 指数", unit: "" },
  pm02Compensated: { label: "PM2.5（修正）", unit: "µg/m³" },
  atmpCompensated: { label: "温度（修正）", unit: "°C" },
  rhumCompensated: { label: "相对湿度（修正）", unit: "%" },
  pm01Standard: { label: "PM1.0（标准颗粒物）", unit: "µg/m³" },
  pm02Standard: { label: "PM2.5（标准颗粒物）", unit: "µg/m³" },
  pm10Standard: { label: "PM10（标准颗粒物）", unit: "µg/m³" },
  pm003Count: { label: "0.3 µm 颗粒数", unit: "个/0.1L" },
  pm005Count: { label: "0.5 µm 颗粒数", unit: "个/0.1L" },
  pm01Count: { label: "1.0 µm 颗粒数", unit: "个/0.1L" },
  pm02Count: { label: "2.5 µm 颗粒数", unit: "个/0.1L" },
  pm50Count: { label: "5.0 µm 颗粒数", unit: "个/0.1L" },
  pm10Count: { label: "10 µm 颗粒数", unit: "个/0.1L" },
  tvocRaw: { label: "VOC 原始信号", unit: "" },
  noxRaw: { label: "NOx 原始信号", unit: "" },
  wifi: { label: "Wi-Fi 信号", unit: "dBm" },
  boot: { label: "设备测量周期", unit: "" },
  bootCount: { label: "设备测量周期（兼容字段）", unit: "" },
};

export const overviewMetrics = ["rco2", "atmp", "rhum", "pm01", "pm02", "pm10", "tvocIndex", "noxIndex"];

export function measurement(payload: Record<string, unknown> | undefined, key: string): number | null {
  const value = payload?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}


// Compact display references: comfort/ventilation ranges, WHO daily-average
// particle guidelines, and sensor index baselines have different meanings.
// Sources and interpretation are documented in README.md.
export const airReferences: Record<string, { text: string; description: string }> = {
  atmp: { text: "舒适 20–26°C", description: "一般室内舒适参考 20–26°C；随季节、衣着与个人感受调整，不是统一的睡眠温度标准。" },
  rhum: { text: "参考 30–50%", description: "EPA 建议室内相对湿度保持在 30–50%。" },
  rco2: { text: "通风 <800 ppm", description: "AirGradient 将低于 800 ppm 作为良好通风参考，不是毒性或健康安全界限。" },
  pm02: { text: "日均 ≤15", description: "WHO 2021：PM2.5 的 24 小时平均浓度指导值为 15 µg/m³；不能用单次实时读数判断日均是否达标。" },
  pm01: { text: "暂无独立标准", description: "PM1.0 暂无广泛采用的独立空气质量指导值，可结合 PM2.5 及变化趋势观察。" },
  pm10: { text: "日均 ≤45", description: "WHO 2021：PM10 的 24 小时平均浓度指导值为 45 µg/m³；不能用单次实时读数判断日均是否达标。" },
  tvocIndex: { text: "基线 100", description: "Sensirion VOC 指数以历史环境基线 100 为参考，是相对变化指标，不是 VOC 浓度或健康限值。" },
  noxIndex: { text: "基线 1", description: "Sensirion NOx 指数基线为 1，是相对变化指标，不是 NO₂ 浓度或健康限值。" },
};

// Use persisted display data; older installations can recover from real history.
export function lastAirDisplay(source: Source | undefined, samples: Sample[] = []): BedDisplay {
  if (source?.latest_metadata?.display) return source.latest_metadata.display;
  const display: BedDisplay = { measurements: {}, updated_at: {} };
  const append = (payload: Record<string, unknown> | undefined, time: string | null | undefined) => {
    if (!time) return;
    for (const key of Object.keys(airMetrics)) {
      const value = measurement(payload, key);
      if (value === null) continue;
      display.measurements[key] = value;
      display.updated_at[key] = time;
    }
  };
  for (const sample of samples) {
    if (sample.metadata.display) {
      Object.assign(display.measurements, sample.metadata.display.measurements);
      Object.assign(display.updated_at, sample.metadata.display.updated_at);
    }
    append(sample.metadata.measurements, sample.recorded_at);
  }
  append(source?.latest_metadata?.measurements, source?.last_sample_at);
  return display;
}
