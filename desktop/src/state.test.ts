import { describe, expect, it } from "vitest";
import { DEFAULT_SETTINGS, basename, formatBytes, formatDuration, loadSettings } from "./state";

describe("desktop state helpers", () => {
  it("loads defaults when saved state is invalid", () => {
    expect(loadSettings({ getItem: () => "not-json" })).toEqual(DEFAULT_SETTINGS);
  });

  it("keeps cross-platform file names readable", () => {
    expect(basename("C:\\Media\\Film.mkv")).toBe("Film.mkv");
    expect(basename("/media/Film.mkv")).toBe("Film.mkv");
  });

  it("formats media measurements", () => {
    expect(formatBytes(2 * 1024 ** 3)).toBe("2.0 GB");
    expect(formatDuration(7500)).toBe("2h 5m");
  });
});
