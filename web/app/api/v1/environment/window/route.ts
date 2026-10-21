import { pool } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const sourceId = url.searchParams.get("source_id") || "airgradient";
  const startAt = url.searchParams.get("start_at");
  const endAt = url.searchParams.get("end_at");

  if (!startAt || !endAt) {
    return Response.json(
      { error: "INVALID_PARAMS", detail: "start_at and end_at query parameters are required." },
      { status: 400 }
    );
  }

  const startParsed = Date.parse(startAt);
  const endParsed = Date.parse(endAt);
  if (!Number.isFinite(startParsed) || !Number.isFinite(endParsed) || startParsed >= endParsed) {
    return Response.json(
      { error: "INVALID_PARAMS", detail: "start_at must be strictly before end_at and formatted in ISO-8601 UTC." },
      { status: 400 }
    );
  }

  // Max 2 hours query window
  if (endParsed - startParsed > 2 * 3600 * 1000) {
    return Response.json(
      { error: "WINDOW_TOO_LARGE", detail: "Query window cannot exceed 2 hours." },
      { status: 422 }
    );
  }

  const now = Date.now();
  if (endParsed > now + 5000) {
    return Response.json(
      { error: "WINDOW_IN_FUTURE", detail: "end_at cannot be in the future." },
      { status: 422 }
    );
  }

  const startIso = new Date(startParsed).toISOString();
  const endIso = new Date(endParsed).toISOString();

  try {
    const client = await pool.connect();
    try {
      // Query samples matching source_id in half-open interval [start_at, end_at)
      const query = `
        SELECT
          id, value, recorded_at, metadata->'measurements' AS measurements
        FROM samples
        WHERE (source_id = $1 OR source_id LIKE 'airgradient:%')
          AND recorded_at >= $2
          AND recorded_at < $3
        ORDER BY recorded_at ASC
      `;
      const res = await client.query(query, [sourceId, startIso, endIso]);
      const samples = res.rows;

      const metricDefs = [
        { key: "temperature", field: "atmp", unit: "℃" },
        { key: "humidity", field: "rhum", unit: "%RH" },
        { key: "co2", field: "rco2", unit: "ppm" },
        { key: "pm2_5", field: "pm02", unit: "µg/m³" },
      ];

      const metrics: Record<string, any> = {};

      for (const m of metricDefs) {
        const values: number[] = [];
        let firstSample: string | null = null;
        let lastSample: string | null = null;
        let excludedCount = 0;

        for (const s of samples) {
          const raw = s.measurements?.[m.field];
          if (typeof raw === "number" && Number.isFinite(raw)) {
            values.push(raw);
            const t = new Date(s.recorded_at).toISOString();
            if (!firstSample) firstSample = t;
            lastSample = t;
          } else {
            excludedCount++;
          }
        }

        const count = values.length;
        let mean: number | null = null;
        let status = "NO_DATA";

        if (count >= 2) {
          const sum = values.reduce((a, b) => a + b, 0);
          mean = Math.round((sum / count) * 100) / 100;
          const firstAge = (Date.parse(firstSample!) - startParsed) / 1000;
          const lastAge = (endParsed - Date.parse(lastSample!)) / 1000;
          if (firstAge > 180 || lastAge > 180) {
            status = "STALE";
          } else {
            status = "CURRENT";
          }
        } else if (count === 1) {
          status = "INSUFFICIENT_DATA";
        } else {
          status = "NO_DATA";
        }

        metrics[m.key] = {
          unit: m.unit,
          provider_field: m.field,
          mean,
          sample_count: count,
          excluded_count: excludedCount,
          first_sample_at: firstSample,
          last_sample_at: lastSample,
          status,
        };
      }

      const collectionState = samples.length > 0 ? "CONNECTED" : "NO_DATA";

      return Response.json(
        {
          contract_version: "ctds.env-window/0.1-draft",
          server_time: new Date().toISOString(),
          source_id: sourceId,
          source_type: "airgradient",
          binding_revision: 1,
          data_mode: "LIVE",
          time_basis: "RECEIPT_ONLY",
          mapping_version: "ctds.c01.airgradient-local.mapping/0.1.0",
          start_at: startIso,
          end_at: endIso,
          interval: "[start_at,end_at)",
          poll_interval_s: 1,
          stale_after_s: 180,
          min_samples: 2,
          collection: {
            state: collectionState,
            samples_total: samples.length,
          },
          metrics,
        },
        {
          headers: {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
          },
        }
      );
    } finally {
      client.release();
    }
  } catch (err: any) {
    console.error("Environment window API error:", err);
    return Response.json(
      { error: "INTERNAL_ERROR", detail: String(err?.message || err) },
      { status: 500 }
    );
  }
}
