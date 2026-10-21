import type { BedDisplay, Sample, Source } from "./types";
import { measurement } from "./airgradient";

export const eightMetrics: Record<string, { label: string; unit: string; approximate?: boolean }> = {
  leftBedTempC: { label: "左侧床温", unit: "°C", approximate: true },
  rightBedTempC: { label: "右侧床温", unit: "°C", approximate: true },
  currentBedTempC: { label: "当前床温", unit: "°C", approximate: true },
  leftTorsoAngle: { label: "左侧背部角度", unit: "°" },
  rightTorsoAngle: { label: "右侧背部角度", unit: "°" },
  leftLegAngle: { label: "左侧腿部角度", unit: "°" },
  rightLegAngle: { label: "右侧腿部角度", unit: "°" },
  audioVolume: { label: "播放器音量", unit: "%" },
  leftBedLevel: { label: "左侧温控档位", unit: "档" },
  rightBedLevel: { label: "右侧温控档位", unit: "档" },
  sleep_score: { label: "睡眠评分", unit: "分" },
  heartRate: { label: "睡眠心率", unit: "bpm" },
  hrv: { label: "心率变异性", unit: "ms" },
  respiratoryRate: { label: "睡眠呼吸率", unit: "次/分" },
  tempBedC: { label: "睡眠床温", unit: "°C" },
  tempRoomC: { label: "睡眠室温", unit: "°C" },
  sleepDuration: { label: "睡眠时长", unit: "秒" },
  presenceDuration: { label: "在床时长", unit: "秒" },
  lightDuration: { label: "浅睡时长", unit: "秒" },
  deepDuration: { label: "深睡时长", unit: "秒" },
  remDuration: { label: "REM 时长", unit: "秒" },
  tnt: { label: "翻身次数", unit: "次" },
  currentLevel: { label: "目标温控档位", unit: "档" },
  currentDeviceLevel: { label: "设备温控档位", unit: "档" },
};

export const eightOverview = ["sleep_score", "heartRate", "hrv", "respiratoryRate", "sleepDuration", "tempRoomC", "currentLevel", "currentDeviceLevel"];

const bedPriority = [
  "leftBedTempC", "rightBedTempC", "currentBedTempC",
  "leftTorsoAngle", "rightTorsoAngle", "leftLegAngle", "rightLegAngle",
  "currentLevel", "currentDeviceLevel",
  "heartRate", "hrv", "respiratoryRate", "tempBedC", "tempRoomC",
];

export function bedMetricValue(payload: Record<string, unknown> | undefined, key: string) {
  const value = measurement(payload, key);
  return key === "heartRate" && value !== null && value <= 0 ? null : value;
}

export function availableBedMetrics(payload: Record<string, unknown> | undefined) {
  const hasSideTemperature = ["leftBedTempC", "rightBedTempC"].some(key => measurement(payload, key) !== null);
  return bedPriority.filter(key => !(key === "currentBedTempC" && hasSideTemperature)
    && !(payload?.hub_online === false && ["currentLevel", "currentDeviceLevel"].includes(key))
    && bedMetricValue(payload, key) !== null).slice(0, 6);
}

// Display values are retained independently from raw samples and source status.
export function lastBedDisplay(source: Source | undefined, samples: Sample[] = []): BedDisplay {
  if (source?.latest_metadata?.display) return source.latest_metadata.display;
  const display: BedDisplay = { measurements: {}, updated_at: {} };
  const append = (payload: Record<string, unknown> | undefined, time: string | null | undefined) => {
    if (!time) return;
    for (const key of Object.keys(eightMetrics)) {
      const value = bedMetricValue(payload, key);
      if (value === null || payload?.hub_online === false && ["currentLevel", "currentDeviceLevel"].includes(key)) continue;
      display.measurements[key] = value;
      display.updated_at[key] = time;
    }
    if (typeof payload?.sleep_day === "string") display.measurements.sleep_day = payload.sleep_day;
  };
  for (const sample of samples) append(sample.metadata.measurements, sample.recorded_at);
  append(source?.latest_metadata?.measurements, source?.last_sample_at);
  return display;
}

export function bedDisplayTime(display: BedDisplay, keys: string[]) {
  return keys.map(key => display.updated_at[key]).filter(time => Number.isFinite(Date.parse(time)))
    .sort((a, b) => Date.parse(b) - Date.parse(a))[0];
}
