import { Pool } from "pg";

const globalForDatabase = globalThis as unknown as { pool?: Pool };

export const pool =
  globalForDatabase.pool ??
  new Pool({
    host: "db",
    database: "postgres",
    user: "postgres",
    password: process.env.POSTGRES_PASSWORD,
    max: 5,
    connectionTimeoutMillis: 5000,
    statement_timeout: 10000,
  });

if (!globalForDatabase.pool) {
  pool.on("error", (error) => console.error("PostgreSQL connection error", error.message));
  globalForDatabase.pool = pool;
}
