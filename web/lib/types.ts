export type BedDisplay = {
  measurements: Record<string, unknown>;
  updated_at: Record<string, string>;
};

export type Sample = {
  id: string;
  value: number | null;
  recorded_at: string;
  metadata: {
    contact?: boolean | null;
    reported_bpm?: number;
    measurement_valid?: boolean;
    raw?: string;
    measurements?: Record<string, unknown>;
    address?: string;
    responses?: Record<string, unknown>;
    errors?: Record<string, string>;
    poll_seconds?: number;
    display?: BedDisplay;
  };
};

export type Source = {
  id: string;
  name: string;
  kind: string;
  metric: string;
  unit: string;
  enabled: boolean;
  status: string;
  error: string | null;
  last_seen: string;
  last_sample_at: string | null;
  sample_count: string;
  latest_value: number | null;
  latest_metadata: Sample["metadata"] | null;
  occupied?: boolean;
};

export type Experience = {
  heart_source_id: string | null;
  client_id: string | null;
  heart_source_name?: string | null;
  id: string;
  name: string;
  started_at: string;
  ended_at: string | null;
  sample_count: string;
};
export type ExperienceRecords = {
  experience: Experience;
  sources: Pick<Source, "id" | "name" | "kind" | "unit" | "sample_count">[];
  samples: (Sample & { source_id: string; source_name: string; kind: string; unit: string })[];
  next: string | null;
};

export type Dashboard = {
  experience?: Experience | null;
  sources: Source[];
  selectedSourceId: string | null;
  samples: Sample[];
  series?: Record<string, Sample[]>;
  collector: { heartbeat: string; error: string | null } | null;
  serverTime: string;
  history?: {
    days: string;
    first_day: string | null;
    last_day: string | null;
    users_complete: string;
    users_total: string;
  } | null;
};
