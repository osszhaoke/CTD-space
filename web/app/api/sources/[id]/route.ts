import { pool } from "@/lib/db";

export async function PATCH(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const origin = request.headers.get("origin");
  if (origin) {
    try {
      if (new URL(origin).host !== request.headers.get("host")) {
        return Response.json({ error: "请在本应用页面内操作。" }, { status: 403 });
      }
    } catch {
      return Response.json({ error: "请求来源无效。" }, { status: 403 });
    }
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "请求格式无效。" }, { status: 400 });
  }
  if (!body || typeof body.enabled !== "boolean") {
    return Response.json({ error: "enabled 必须为布尔值。" }, { status: 400 });
  }
  try {
    const { id } = await context.params;
    const result = await pool.query(
      `UPDATE sources SET enabled = $2, error = NULL,
         status = CASE WHEN $2 THEN 'pending' ELSE 'paused' END
       WHERE id = $1 RETURNING id, enabled`,
      [id, body.enabled],
    );
    if (!result.rowCount) {
      return Response.json({ error: "没有找到该数据源。" }, { status: 404 });
    }
    return Response.json(result.rows[0]);
  } catch (error) {
    console.error("Source update failed", error);
    return Response.json({ error: "保存失败，请稍后重试。" }, { status: 503 });
  }
}
