import type { Sample } from "./types";

export function frequency(samples: Sample[], now: number, maxGap = 10000): number | null {
  if (samples.length < 2) return null;
  const latest = new Date(samples.at(-1)!.recorded_at).getTime();
  if (now - latest > maxGap) return null;
  let start = samples.length - 1;
  while (start > 0) {
    const gap =
      new Date(samples[start].recorded_at).getTime() -
      new Date(samples[start - 1].recorded_at).getTime();
    if (gap > maxGap) break;
    start--;
  }
  const span = latest - new Date(samples[start].recorded_at).getTime();
  return span > 0 ? ((samples.length - start - 1) * 1000) / span : null;
}
