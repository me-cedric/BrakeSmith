export type Page =
  | "overview"
  | "library"
  | "queue"
  | "activity"
  | "outcomes"
  | "health"
  | "settings";

export type Theme = "system" | "light" | "dark";
export type LibraryGroup = "ready" | "success" | "blocked" | "stale";

export interface Track {
  index: number;
  type_index: number;
  kind: string;
  language: string;
  codec: string;
  title?: string | null;
  default?: boolean;
  forced?: boolean;
  commentary?: boolean;
  hearing_impaired?: boolean;
}

export interface MediaItem {
  path: string;
  codec: string;
  should_convert: boolean;
  duration: number;
  size: number;
  original_language?: string | null;
  audio: Track[];
  subtitles: Track[];
  video: {
    width?: number;
    height?: number;
    hdr?: boolean;
    dolby_vision?: boolean;
  };
  attachments: number;
  chapters: number;
  sidecars: string[];
  warnings: string[];
  transcode_status: "ready" | "success" | "blocked" | "not-required";
  blocked_reason?: string;
}

export interface StatusEntry {
  group: LibraryGroup;
  source: string;
  reason: string;
  size: number;
  duration: number;
}

export interface StatusResponse {
  root: string;
  totals: Record<LibraryGroup, { files: number; bytes: number; duration_seconds: number }>;
  items: StatusEntry[];
}

export interface DoctorResponse {
  version: string;
  healthy: boolean;
  tools: {
    handbrake: string | null;
    ffprobe: string | null;
    ffmpeg: string | null;
  };
}

export interface FailureRecord {
  source: string;
  type: string;
  error?: string;
  failed_at?: string;
  recorded_at?: string;
  log?: string | null;
  active: boolean;
}

export interface HistoryRecord {
  source: string;
  recorded_at?: string;
  outcome: string;
  type: string;
  error?: string;
  result?: string;
  log?: string | null;
}

export interface HealthResult {
  path: string;
  mode: "quick" | "full";
  status: "healthy" | "error";
  error?: string | null;
}

export interface PlanItem {
  source: string;
  destination: string;
  duration: number;
  replace_source: boolean;
  warnings: string[];
  format: {
    preset: string;
    resolution: string;
    quality: number;
    encoder_preset: string;
    bit_depth: number;
  };
}

export interface PlanResponse {
  created_at: string;
  root: string;
  digest: string;
  totals: {
    files: number;
    source_bytes: number;
    minimum_free_bytes: number;
    duration_seconds: number;
  };
  settings: Record<string, unknown>;
  items: PlanItem[];
}

export interface Settings {
  root: string;
  cliPath: string;
  handbrakePath: string;
  ffprobePath: string;
  ffmpegPath: string;
  theme: Theme;
  formatPreset: "recommended" | "highest" | "high" | "compact" | "custom";
  quality: number;
  encoderPreset: string;
  bitDepth: number;
  tune: string;
  encoderProfile: string;
  encoderLevel: string;
  crop: "auto" | "none";
  deinterlace: "auto" | "off" | "decomb" | "yadif";
  lossless: boolean;
  audio: string;
  subtitles: string;
  unknownAudio: "keep" | "drop";
  unknownSubtitles: "keep" | "drop";
  keepCommentary: boolean;
  forcedSubtitlesOnly: boolean;
  keepOriginal: boolean;
  originalLanguage: string;
  allowNoAudio: boolean;
  includeHevc: boolean;
  excludeTitles: string;
  extensions: string;
  workers: number;
  probeTimeout: number;
  useCache: boolean;
  overrides: string;
  replaceSource: boolean;
  stopWhenLarger: boolean;
  outputDirectory: string;
}

export interface BridgeEvent {
  protocol?: number;
  event: string;
  message?: string;
  stream?: string;
  data?: unknown;
  method?: string;
  phase?: string;
  source?: string;
  index?: number;
  total?: number;
  percent?: number;
  completed?: number;
  failed?: number;
  ok?: boolean;
  exit_code?: number;
  result?: Record<string, unknown>;
}

export interface JobEnvelope {
  job_id: string;
  payload: BridgeEvent;
}
