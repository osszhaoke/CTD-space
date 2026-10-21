import { pool } from "@/lib/db";
import type { Sample, Source } from "@/lib/types";
import { overviewSource } from "@/lib/dashboard";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  try {
    const [sourcesRes, collectorRes] = await Promise.all([
      pool.query<Source>(`
        SELECT s.*, false AS occupied, latest.value AS latest_value, latest.metadata AS latest_metadata
        FROM sources s
        LEFT JOIN LATERAL (
          SELECT value, metadata - 'responses' AS metadata FROM samples WHERE source_id = s.id
          ORDER BY recorded_at DESC, id DESC LIMIT 1
        ) latest ON true
        ORDER BY s.name, s.id
      `),
      pool.query("SELECT heartbeat, error FROM collector_status WHERE id = 1").catch(() => ({ rows: [] })),
    ]);

    const sources = sourcesRes.rows;
    const requested = new URL(request.url).searchParams.get("source");
    const selected = sources.find((source) => source.id === requested)
      ?? sources.find((source) => source.enabled)
      ?? sources[0];

    // One bounded history per visible source (airgradient and eight_sleep)
    const visible = [selected, ...["airgradient", "eight_sleep"].map((kind) =>
      overviewSource(sources, kind, selected?.id))].filter((source): source is Source => !!source);
    const ids = [...new Set(visible.map((source) => source.id))];

    const entries = await Promise.all(ids.map(async (id) => {
      const result = await pool.query<Sample>(
        `SELECT id, value, recorded_at, metadata - 'responses' AS metadata FROM samples
         WHERE source_id = $1 ORDER BY recorded_at DESC, id DESC LIMIT 120`, [id]);
      return [id, result.rows.reverse()] as const;
    }));
    const series = Object.fromEntries(entries);

    let history = null;
    if (selected?.kind === "eight_sleep") {
      const [raw, stats] = await Promise.all([
        pool.query(
          `SELECT metadata FROM samples WHERE source_id=$1 ORDER BY recorded_at DESC, id DESC LIMIT 1`,
          [selected.id],
        ).catch(() => ({ rows: [] })),
        pool.query(
          `SELECT count(*)::text AS days, min(day)::text AS first_day, max(day)::text AS last_day,
           (SELECT count(*)::text FROM eight_sleep_sync WHERE source_id=$1 AND complete) AS users_complete,
           (SELECT count(*)::text FROM eight_sleep_sync WHERE source_id=$1) AS users_total
           FROM eight_sleep_days WHERE source_id=$1`, [selected.id],
        ).catch(() => ({ rows: [{ days: "0", first_day: null, last_day: null, users_complete: "0", users_total: "0" }] })),
      ]);
      selected.latest_metadata = raw.rows[0]?.metadata ?? selected.latest_metadata;
      history = stats.rows[0];
    }

    return Response.json(
      {
        experience: null,
        sources,
        selectedSourceId: selected?.id ?? null,
        samples: selected ? series[selected.id] ?? [] : [],
        series,
        collector: collectorRes.rows[0] ?? null,
        serverTime: new Date().toISOString(),
        history,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    console.error("Dashboard query failed", error);
    return Response.json({ error: "暂时无法读取数据，请稍后重试。" }, { status: 503 });
  }
}
