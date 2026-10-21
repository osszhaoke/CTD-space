CREATE TABLE IF NOT EXISTS sources (
    id text PRIMARY KEY,
    name text NOT NULL,
    kind text NOT NULL,
    metric text NOT NULL,
    unit text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'discovered',
    error text,
    last_seen timestamptz NOT NULL DEFAULT now(),
    last_sample_at timestamptz,
    sample_count bigint NOT NULL DEFAULT 0,
    metadata jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS samples (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id text NOT NULL REFERENCES sources(id),
    value double precision,
    recorded_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS samples_source_time_idx
    ON samples (source_id, recorded_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS collector_status (
    id integer PRIMARY KEY CHECK (id = 1),
    heartbeat timestamptz NOT NULL,
    error text
);

CREATE TABLE IF NOT EXISTS eight_sleep_days (
    source_id text NOT NULL REFERENCES sources(id),
    user_id text NOT NULL,
    day date NOT NULL,
    data jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, user_id, day)
);

CREATE TABLE IF NOT EXISTS eight_sleep_sync (
    source_id text NOT NULL REFERENCES sources(id),
    user_id text NOT NULL,
    next_before date NOT NULL,
    complete boolean NOT NULL DEFAULT false,
    PRIMARY KEY (source_id, user_id)
);
