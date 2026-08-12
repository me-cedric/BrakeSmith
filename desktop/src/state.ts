import type { Settings } from "./types";

export const DEFAULT_SETTINGS: Settings = {
  root: "",
  cliPath: "",
  handbrakePath: "",
  ffprobePath: "",
  ffmpegPath: "",
  theme: "system",
  formatPreset: "recommended",
  quality: 18,
  encoderPreset: "slow",
  bitDepth: 10,
  tune: "",
  encoderProfile: "",
  encoderLevel: "",
  crop: "auto",
  deinterlace: "auto",
  lossless: false,
  audio: "eng,fra",
  subtitles: "eng,fra",
  unknownAudio: "keep",
  unknownSubtitles: "keep",
  keepCommentary: false,
  forcedSubtitlesOnly: false,
  keepOriginal: false,
  originalLanguage: "",
  allowNoAudio: false,
  includeHevc: false,
  excludeTitles: "commentary,description",
  extensions: "",
  workers: 2,
  probeTimeout: 60,
  useCache: true,
  overrides: "",
  replaceSource: false,
  stopWhenLarger: false,
  outputDirectory: "",
};

export function loadSettings(storage: Pick<Storage, "getItem"> = localStorage): Settings {
  const raw = storage.getItem("brakesmith.settings.v1");
  if (!raw) return DEFAULT_SETTINGS;
  try {
    return { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<Settings>) };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

export function saveSettings(
  settings: Settings,
  storage: Pick<Storage, "setItem"> = localStorage,
): void {
  storage.setItem("brakesmith.settings.v1", JSON.stringify(settings));
}

export function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) || path;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}
