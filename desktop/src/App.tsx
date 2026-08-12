import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ArrowClockwise,
  ArrowCounterClockwise,
  Broom,
  Bug,
  Check,
  CheckCircle,
  Coffee,
  Desktop,
  Export,
  FilmSlate,
  FolderOpen,
  GearSix,
  GithubLogo,
  HardDrives,
  Heartbeat,
  House,
  Info,
  ListChecks,
  MagnifyingGlass,
  Moon,
  Play,
  Pulse as Activity,
  Queue,
  ShieldCheck,
  SlidersHorizontal,
  Stop,
  Sun,
  Terminal,
  Trash,
  WarningCircle,
  XCircle,
  type Icon,
} from "@phosphor-icons/react";
import {
  callBridge,
  callBridgeDetailed,
  cancelBridgeJob,
  chooseDirectory,
  chooseFile,
  chooseSavePath,
  isDesktopRuntime,
  onJobEvent,
  openExternal,
  startBridgeJob,
} from "./bridge";
import {
  basename,
  formatBytes,
  formatDuration,
  loadSettings,
  saveSettings,
} from "./state";
import type {
  BridgeEvent,
  DoctorResponse,
  FailureRecord,
  HealthResult,
  HistoryRecord,
  JobEnvelope,
  LibraryGroup,
  MediaItem,
  Page,
  PlanResponse,
  Settings,
  StatusResponse,
  Theme,
} from "./types";

const NAVIGATION: { page: Page; label: string; icon: Icon }[] = [
  { page: "overview", label: "Overview", icon: House },
  { page: "library", label: "Library", icon: FilmSlate },
  { page: "queue", label: "Queue", icon: Queue },
  { page: "activity", label: "Activity", icon: Activity },
  { page: "outcomes", label: "Outcomes", icon: ShieldCheck },
  { page: "health", label: "Health", icon: Heartbeat },
];

const GROUP_LABELS: Record<LibraryGroup, string> = {
  ready: "Ready",
  success: "Complete",
  blocked: "Blocked",
  stale: "Stale",
};

type SortKey =
  | "state"
  | "file"
  | "codec"
  | "video"
  | "duration"
  | "size"
  | "reason";

interface ActiveJob {
  id: string;
  method: string;
  phase: string;
  source: string;
  index: number;
  total: number;
  percent: number;
  startedAt: number;
  logs: string[];
  finished: boolean;
  ok?: boolean;
  error?: string;
}

function IconButton({
  label,
  children,
  onClick,
  disabled,
}: {
  label: string;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      className="icon-button"
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}

function StatusMark({ state }: { state: string }) {
  const normalized = state === "not-required" ? "success" : state;
  const Mark =
    normalized === "success" || normalized === "healthy"
      ? CheckCircle
      : normalized === "blocked"
        ? WarningCircle
        : normalized === "error" || normalized === "failed"
          ? XCircle
          : normalized === "ready"
            ? Play
            : Info;
  return (
    <span className={`status status-${normalized}`}>
      <Mark aria-hidden="true" weight="fill" />
      {normalized === "success" ? "Complete" : normalized.replaceAll("-", " ")}
    </span>
  );
}

function EmptyState({
  icon: EmptyIcon,
  title,
  body,
  action,
}: {
  icon: Icon;
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <EmptyIcon aria-hidden="true" />
      </span>
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

function Panel({
  title,
  eyebrow,
  action,
  children,
  className = "",
}: {
  title: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-header">
        <div>
          {eyebrow && <span className="eyebrow">{eyebrow}</span>}
          <h2>{title}</h2>
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

function SkeletonRows() {
  return (
    <div className="skeleton-list" aria-label="Loading">
      {[1, 2, 3, 4, 5].map((line) => (
        <span className="skeleton" key={line} />
      ))}
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [settings, setSettings] = useState<Settings>(() => loadSettings());
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [media, setMedia] = useState<MediaItem[]>([]);
  const [failures, setFailures] = useState<FailureRecord[]>([]);
  const [history, setHistory] = useState<HistoryRecord[]>([]);
  const [doctor, setDoctor] = useState<DoctorResponse | null>(null);
  const [health, setHealth] = useState<HealthResult[]>([]);
  const [scanWarnings, setScanWarnings] = useState<string[]>([]);
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planPath, setPlanPath] = useState("");
  const [focusedMedia, setFocusedMedia] = useState<MediaItem | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const [sort, setSort] = useState<{
    key: SortKey;
    direction: "ascending" | "descending";
  }>({ key: "file", direction: "ascending" });
  const [outcomeTab, setOutcomeTab] = useState<"blocked" | "history">(
    "blocked",
  );
  const [healthMode, setHealthMode] = useState<"quick" | "full">("quick");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);
  const refreshRef = useRef<() => Promise<void>>(async () => undefined);
  const doctorStartedRef = useRef(false);
  const autoRefreshRootRef = useRef("");
  const refreshIdRef = useRef(0);
  const detailDrawerRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const toolParams = useMemo(
    () => ({
      handbrake: settings.handbrakePath,
      ffprobe: settings.ffprobePath,
      ffmpeg: settings.ffmpegPath,
    }),
    [settings.ffmpegPath, settings.ffprobePath, settings.handbrakePath],
  );

  const updateSettings = <K extends keyof Settings>(
    key: K,
    value: Settings[K],
  ) => {
    setSettings((current) => ({ ...current, [key]: value }));
  };

  useEffect(() => {
    saveSettings(settings);
    document.documentElement.dataset.theme = settings.theme;
  }, [settings]);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(""), 2600);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  const closeMediaDetails = useCallback(() => {
    setFocusedMedia(null);
    window.setTimeout(() => returnFocusRef.current?.focus(), 0);
  }, []);

  const openMediaDetails = (item: MediaItem) => {
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    setFocusedMedia(item);
  };

  useEffect(() => {
    if (!focusedMedia) return;
    detailDrawerRef.current?.querySelector<HTMLElement>("button")?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMediaDetails();
        return;
      }
      if (event.key !== "Tab" || !detailDrawerRef.current) return;
      const focusable = [
        ...detailDrawerRef.current.querySelectorAll<HTMLElement>(
          "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
        ),
      ].filter((element) => !element.hasAttribute("disabled"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1) as HTMLElement;
      if (
        event.shiftKey &&
        (document.activeElement === first ||
          document.activeElement === detailDrawerRef.current)
      ) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [closeMediaDetails, focusedMedia]);

  const checkDoctor = useCallback(async () => {
    setError("");
    try {
      const nextDoctor = await callBridge<DoctorResponse>(
        "doctor",
        toolParams,
        settings.cliPath,
      );
      setDoctor(nextDoctor);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [settings.cliPath, toolParams]);

  useEffect(() => {
    if (!doctorStartedRef.current && isDesktopRuntime()) {
      doctorStartedRef.current = true;
      void checkDoctor();
    }
  }, [checkDoctor]);

  const refresh = useCallback(async () => {
    if (!settings.root) return;
    const root = settings.root;
    const refreshId = ++refreshIdRef.current;
    setLoading(true);
    setError("");
    setScanWarnings([]);
    try {
      const common = {
        root,
        extensions: settings.extensions,
        workers: settings.workers,
        probe_timeout: settings.probeTimeout,
        use_cache: settings.useCache,
        ffprobe: settings.ffprobePath,
      };
      const scanResult = await callBridgeDetailed<MediaItem[]>(
        "scan",
        common,
        settings.cliPath,
      );
      const [nextStatus, nextFailures, nextHistory, nextDoctor] =
        await Promise.all([
          callBridge<StatusResponse>("status", common, settings.cliPath),
          callBridge<FailureRecord[]>("failures.list", {}, settings.cliPath),
          callBridge<HistoryRecord[]>("history", common, settings.cliPath),
          callBridge<DoctorResponse>("doctor", toolParams, settings.cliPath),
        ]);
      if (refreshId !== refreshIdRef.current || root !== settings.root) return;
      const nextMedia = scanResult.data;
      setStatus(nextStatus);
      setMedia(nextMedia);
      setScanWarnings(
        scanResult.events
          .filter((event) => event.event === "warning" && event.message)
          .map((event) => event.message as string),
      );
      setFailures(nextFailures);
      setHistory(nextHistory);
      setDoctor(nextDoctor);
      setSelected(
        (current) =>
          new Set(
            [...current].filter((path) =>
              nextMedia.some((item) => item.path === path),
            ),
          ),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (refreshId === refreshIdRef.current) setLoading(false);
    }
  }, [
    settings.cliPath,
    settings.extensions,
    settings.ffprobePath,
    settings.probeTimeout,
    settings.root,
    settings.useCache,
    settings.workers,
    toolParams,
  ]);

  refreshRef.current = refresh;

  useEffect(() => {
    if (
      settings.root &&
      settings.root !== autoRefreshRootRef.current &&
      isDesktopRuntime()
    ) {
      autoRefreshRootRef.current = settings.root;
      void refreshRef.current();
    }
  }, [settings.root]);

  useEffect(() => {
    if (!isDesktopRuntime()) return;
    let disposed = false;
    let release: (() => void) | undefined;
    void onJobEvent((envelope: JobEnvelope) => {
      if (disposed) return;
      const event = envelope.payload;
      setActiveJob((current) => {
        const active =
          current?.id === envelope.job_id
            ? current
            : {
                id: envelope.job_id,
                method: event.method || "unknown",
                phase: event.phase || "starting",
                source: "",
                index: 0,
                total: 0,
                percent: 0,
                startedAt: Date.now(),
                logs: [],
                finished: false,
              };
        const logs =
          event.event === "log" && event.message
            ? [...active.logs, event.message].slice(-500)
            : active.logs;
        const calculatedPercent =
          typeof event.percent === "number"
            ? event.index && event.total
              ? ((event.index - 1 + event.percent / 100) / event.total) * 100
              : event.percent
            : event.total && typeof event.completed === "number"
              ? (event.completed / event.total) * 100
              : active.percent;
        return {
          ...active,
          method: event.method || active.method,
          phase: event.phase || active.phase,
          source: event.source || active.source,
          index: event.index || active.index,
          total: event.total || active.total,
          percent: calculatedPercent,
          logs,
          error: event.event === "error" ? event.message : active.error,
          finished:
            event.event === "finished" || event.event === "cancelled"
              ? true
              : active.finished,
          ok: event.event === "finished" ? event.ok : active.ok,
        };
      });
      if (event.event === "result" && Array.isArray(event.data)) {
        const items = event.data as HealthResult[];
        if (
          items.every(
            (item) =>
              item && (item.status === "healthy" || item.status === "error"),
          )
        ) {
          setHealth(items);
        }
      }
      if (event.event === "finished" || event.event === "cancelled") {
        setLoading(false);
        if (event.event === "finished") void refreshRef.current();
      }
    }).then((unlisten) => {
      if (disposed) unlisten();
      else release = unlisten;
    });
    return () => {
      disposed = true;
      release?.();
    };
  }, []);

  const chooseLibrary = async () => {
    try {
      const path = await chooseDirectory(settings.root || undefined);
      if (path) {
        if (path === settings.root) {
          await refresh();
          return;
        }
        refreshIdRef.current += 1;
        setStatus(null);
        setMedia([]);
        setFailures([]);
        setHistory([]);
        setHealth([]);
        setScanWarnings([]);
        setPlan(null);
        setPlanPath("");
        setFocusedMedia(null);
        setSelected(new Set());
        updateSettings("root", path);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const toggleSelected = (path: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const createPlan = async () => {
    if (!selected.size) {
      setError("Select at least one ready file.");
      setPage("library");
      return;
    }
    const output = await chooseSavePath("brakesmith-plan.json", ["json"]);
    if (!output) return;
    setLoading(true);
    setError("");
    try {
      const nextPlan = await callBridge<PlanResponse>(
        "plan.create",
        {
          root: settings.root,
          output,
          sources: [...selected],
          audio: settings.audio,
          subtitles: settings.subtitles,
          unknown_audio: settings.unknownAudio,
          unknown_subtitles: settings.unknownSubtitles,
          keep_commentary: settings.keepCommentary,
          forced_subtitles_only: settings.forcedSubtitlesOnly,
          keep_original: settings.keepOriginal,
          allow_no_audio: settings.allowNoAudio,
          format_preset: settings.formatPreset,
          quality: settings.quality,
          preset: settings.encoderPreset,
          bit_depth: settings.bitDepth,
          tune: settings.tune,
          encoder_profile: settings.encoderProfile,
          encoder_level: settings.encoderLevel,
          crop: settings.crop,
          deinterlace: settings.deinterlace,
          lossless: settings.lossless,
          replace_source: settings.replaceSource,
          stop_when_larger: settings.stopWhenLarger,
          include_hevc: settings.includeHevc,
          original_language: settings.originalLanguage,
          exclude_titles: settings.excludeTitles,
          extensions: settings.extensions,
          workers: settings.workers,
          probe_timeout: settings.probeTimeout,
          use_cache: settings.useCache,
          handbrake: settings.handbrakePath,
          ffprobe: settings.ffprobePath,
          overrides: settings.overrides,
          output_directory: settings.outputDirectory,
        },
        settings.cliPath,
      );
      setPlan(nextPlan);
      setPlanPath(output);
      setPage("queue");
      setToast("Plan is ready for review");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  const openExistingPlan = async () => {
    const path = await chooseFile(["json"], planPath || undefined);
    if (!path) return;
    setLoading(true);
    setError("");
    try {
      const savedPlan = await callBridge<PlanResponse>(
        "plan.read",
        { plan_file: path },
        settings.cliPath,
      );
      setPlan(savedPlan);
      setPlanPath(path);
      setToast("Saved plan loaded");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  const executePlan = async (retryBlocked = false) => {
    if (!planPath) return;
    setError("");
    setLoading(true);
    try {
      const jobId = await startBridgeJob(
        "plan.execute",
        { plan_file: planPath, max_failures: 1, retry_blocked: retryBlocked },
        settings.cliPath,
      );
      const initialJob: ActiveJob = {
        id: jobId,
        method: "plan.execute",
        phase: "execute",
        source: "",
        index: 0,
        total: plan?.totals.files || 0,
        percent: 0,
        startedAt: Date.now(),
        logs: [],
        finished: false,
      };
      setActiveJob((current) =>
        current?.id === jobId ? { ...initialJob, ...current } : initialJob,
      );
      setPage("activity");
    } catch (reason) {
      setLoading(false);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const runHealth = async () => {
    if (!settings.root) return;
    setLoading(true);
    setError("");
    setHealth([]);
    try {
      const params: Record<string, unknown> = {
        root: settings.root,
        full: healthMode === "full",
        ffprobe: settings.ffprobePath,
        ffmpeg: settings.ffmpegPath,
      };
      if (selected.size) params.sources = [...selected];
      const jobId = await startBridgeJob("health", params, settings.cliPath);
      const initialJob: ActiveJob = {
        id: jobId,
        method: "health",
        phase: "health",
        source: "",
        index: 0,
        total: selected.size || media.length,
        percent: 0,
        startedAt: Date.now(),
        logs: [],
        finished: false,
      };
      setActiveJob((current) =>
        current?.id === jobId ? { ...initialJob, ...current } : initialJob,
      );
    } catch (reason) {
      setLoading(false);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const cancelJob = async () => {
    if (!activeJob || activeJob.finished) return;
    try {
      await cancelBridgeJob(activeJob.id);
      setToast("Stop requested");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const outcomeAction = async (
    method: string,
    params: Record<string, unknown>,
    message: string,
  ) => {
    setLoading(true);
    setError("");
    try {
      await callBridge(method, params, settings.cliPath);
      setToast(message);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  const exportCandidates = async () => {
    const output = await chooseSavePath("brakesmith-candidates.json", [
      "json",
      "csv",
      "txt",
    ]);
    if (!output) return;
    await outcomeAction(
      "candidates.export",
      {
        root: settings.root,
        output,
        include_blocked: stateFilter === "blocked",
        extensions: settings.extensions,
        workers: settings.workers,
        probe_timeout: settings.probeTimeout,
        use_cache: settings.useCache,
        ffprobe: settings.ffprobePath,
      },
      "Candidate report saved",
    );
  };

  const readyMedia = useMemo(
    () => media.filter((item) => item.transcode_status === "ready"),
    [media],
  );
  const visibleMedia = useMemo(() => {
    const term = search.trim().toLocaleLowerCase();
    const filtered = media.filter((item) => {
      const state =
        item.transcode_status === "not-required"
          ? "success"
          : item.transcode_status;
      return (
        (!term ||
          item.path.toLocaleLowerCase().includes(term) ||
          item.codec.toLocaleLowerCase().includes(term)) &&
        (stateFilter === "all" || state === stateFilter)
      );
    });
    const direction = sort.direction === "ascending" ? 1 : -1;
    return [...filtered].sort((left, right) => {
      const videoSize = (item: MediaItem) =>
        (item.video.width || 0) * (item.video.height || 0);
      const reason = (item: MediaItem) =>
        item.blocked_reason || item.warnings[0] || "";
      const values: Record<SortKey, [string | number, string | number]> = {
        state: [left.transcode_status, right.transcode_status],
        file: [left.path, right.path],
        codec: [left.codec, right.codec],
        video: [videoSize(left), videoSize(right)],
        duration: [left.duration, right.duration],
        size: [left.size, right.size],
        reason: [reason(left), reason(right)],
      };
      const [leftValue, rightValue] = values[sort.key];
      return (
        direction *
        (typeof leftValue === "number" && typeof rightValue === "number"
          ? leftValue - rightValue
          : String(leftValue).localeCompare(String(rightValue)))
      );
    });
  }, [media, search, sort, stateFilter]);

  const changeSort = (key: SortKey) => {
    setSort((current) =>
      current.key === key
        ? {
            key,
            direction:
              current.direction === "ascending" ? "descending" : "ascending",
          }
        : { key, direction: "ascending" },
    );
  };

  const pageTitle =
    NAVIGATION.find((item) => item.page === page)?.label || "Settings";
  const totals = status?.totals;

  const renderOverview = () => {
    if (!settings.root) {
      return (
        <EmptyState
          icon={HardDrives}
          title="Choose a media library"
          body="BrakeSmith scans locally. It does not upload or index your files."
          action={
            <button className="primary" type="button" onClick={chooseLibrary}>
              <FolderOpen />
              Choose library
            </button>
          }
        />
      );
    }
    if (loading && !status) return <SkeletonRows />;
    return (
      <div className="page-grid overview-grid">
        <Panel
          title="Library readiness"
          eyebrow="Live state"
          className="readiness-panel"
          action={
            <IconButton
              label="Refresh library"
              onClick={() => void refresh()}
              disabled={loading}
            >
              <ArrowClockwise />
            </IconButton>
          }
        >
          <div className="readiness-total">
            <strong>{status?.items.length || 0}</strong>
            <span>known files</span>
          </div>
          <div
            className="readiness-rail"
            aria-label="Library readiness distribution"
          >
            {(["ready", "success", "blocked", "stale"] as LibraryGroup[]).map(
              (group) => {
                const count = totals?.[group].files || 0;
                const all = status?.items.length || 1;
                return (
                  <span
                    key={group}
                    className={`rail-${group}`}
                    style={{ flexGrow: count / all }}
                    title={`${GROUP_LABELS[group]}: ${count}`}
                  />
                );
              },
            )}
          </div>
          <div className="readiness-legend">
            {(["ready", "success", "blocked", "stale"] as LibraryGroup[]).map(
              (group) => (
                <button
                  key={group}
                  type="button"
                  onClick={() => {
                    setStateFilter(group);
                    setPage("library");
                  }}
                >
                  <span className={`dot dot-${group}`} />
                  <span>{GROUP_LABELS[group]}</span>
                  <strong>{totals?.[group].files || 0}</strong>
                  <small>{formatBytes(totals?.[group].bytes || 0)}</small>
                </button>
              ),
            )}
          </div>
        </Panel>
        <Panel
          title={
            activeJob && !activeJob.finished
              ? "Work in progress"
              : "Next action"
          }
          eyebrow="Workshop"
        >
          {activeJob && !activeJob.finished ? (
            <div className="next-action">
              <span className="pulse-mark">
                <Activity weight="fill" />
              </span>
              <div>
                <strong>
                  {activeJob.phase === "health"
                    ? "Checking media"
                    : "Transcoding queue"}
                </strong>
                <p>
                  {activeJob.source
                    ? basename(activeJob.source)
                    : "Preparing the next item"}
                </p>
              </div>
              <button
                className="secondary"
                type="button"
                onClick={() => setPage("activity")}
              >
                View progress
              </button>
            </div>
          ) : readyMedia.length ? (
            <div className="next-action">
              <span className="accent-mark">
                <ListChecks />
              </span>
              <div>
                <strong>
                  {readyMedia.length} file{readyMedia.length === 1 ? "" : "s"}{" "}
                  ready
                </strong>
                <p>Select exact files and create a sealed plan.</p>
              </div>
              <button
                className="primary"
                type="button"
                onClick={() => setPage("library")}
              >
                Review files
              </button>
            </div>
          ) : (
            <div className="next-action">
              <span className="success-mark">
                <Check />
              </span>
              <div>
                <strong>Library is settled</strong>
                <p>No ready candidates need attention.</p>
              </div>
              <button
                className="secondary"
                type="button"
                onClick={() => setPage("health")}
              >
                Check health
              </button>
            </div>
          )}
        </Panel>
        <Panel title="Toolchain" eyebrow="Local system">
          <div className="toolchain-list">
            {doctor ? (
              <>
                <div>
                  <Terminal />
                  <span>BrakeSmith</span>
                  <strong>v{doctor.version}</strong>
                </div>
                <div>
                  <span
                    className={`tool-dot ${doctor.tools.handbrake ? "online" : "offline"}`}
                  />
                  <span>HandBrakeCLI</span>
                  <strong>
                    {doctor.tools.handbrake ? "Ready" : "Missing"}
                  </strong>
                </div>
                <div>
                  <span
                    className={`tool-dot ${doctor.tools.ffprobe ? "online" : "offline"}`}
                  />
                  <span>ffprobe</span>
                  <strong>{doctor.tools.ffprobe ? "Ready" : "Missing"}</strong>
                </div>
                <div>
                  <span
                    className={`tool-dot ${doctor.tools.ffmpeg ? "online" : "optional"}`}
                  />
                  <span>ffmpeg</span>
                  <strong>{doctor.tools.ffmpeg ? "Ready" : "Optional"}</strong>
                </div>
              </>
            ) : (
              <p className="muted">Tool state is not available.</p>
            )}
          </div>
        </Panel>
        <Panel title="Safety model" eyebrow="Always on">
          <ul className="safety-list">
            <li>
              <CheckCircle />
              Sources change only after output validation.
            </li>
            <li>
              <CheckCircle />
              Unhelpful outputs become blocked outcomes.
            </li>
            <li>
              <CheckCircle />
              Plans keep a durable resume journal.
            </li>
          </ul>
        </Panel>
      </div>
    );
  };

  const renderLibrary = () => {
    if (!settings.root) return renderOverview();
    const header = (key: SortKey, label: string) => (
      <th aria-sort={sort.key === key ? sort.direction : "none"}>
        <button type="button" onClick={() => changeSort(key)}>
          {label}
          {sort.key === key
            ? sort.direction === "ascending"
              ? " ↑"
              : " ↓"
            : ""}
        </button>
      </th>
    );
    return (
      <Panel
        title="Media inventory"
        eyebrow={`${media.length} inspected`}
        action={
          <div className="header-actions">
            <button
              className="secondary"
              type="button"
              onClick={() => void exportCandidates()}
            >
              <Export />
              Export
            </button>
            <IconButton
              label="Refresh library"
              onClick={() => void refresh()}
              disabled={loading}
            >
              <ArrowClockwise />
            </IconButton>
          </div>
        }
      >
        <div className="table-toolbar">
          <label className="search-field">
            <MagnifyingGlass />
            <span className="sr-only">Search media</span>
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search files or codec"
            />
          </label>
          <label className="filter-field">
            <SlidersHorizontal />
            <span className="sr-only">Filter by state</span>
            <select
              value={stateFilter}
              onChange={(event) => setStateFilter(event.target.value)}
            >
              <option value="all">All states</option>
              <option value="ready">Ready</option>
              <option value="success">Complete</option>
              <option value="blocked">Blocked</option>
            </select>
          </label>
          <span className="selection-count">{selected.size} selected</span>
          <button
            className="primary"
            type="button"
            disabled={!selected.size}
            onClick={() => setPage("queue")}
          >
            Build queue
          </button>
        </div>
        {loading && !media.length ? (
          <SkeletonRows />
        ) : visibleMedia.length ? (
          <div className="table-scroll">
            <table className="media-table">
              <thead>
                <tr>
                  <th className="check-cell">
                    <span className="sr-only">Select</span>
                  </th>
                  {header("state", "State")}
                  {header("file", "File")}
                  {header("codec", "Codec")}
                  {header("video", "Video")}
                  {header("duration", "Duration")}
                  {header("size", "Size")}
                  {header("reason", "Reason")}
                </tr>
              </thead>
              <tbody>
                {visibleMedia.map((item) => {
                  const canSelect =
                    item.transcode_status === "ready" ||
                    (settings.includeHevc &&
                      item.transcode_status === "not-required");
                  return (
                    <tr
                      key={item.path}
                      className={selected.has(item.path) ? "selected-row" : ""}
                    >
                      <td className="check-cell">
                        <input
                          type="checkbox"
                          aria-label={`Select ${basename(item.path)}`}
                          checked={selected.has(item.path)}
                          disabled={!canSelect}
                          onChange={() => toggleSelected(item.path)}
                        />
                      </td>
                      <td>
                        <StatusMark state={item.transcode_status} />
                      </td>
                      <td className="file-cell">
                        <button
                          className="detail-trigger"
                          type="button"
                          onClick={() => openMediaDetails(item)}
                        >
                          <strong>{basename(item.path)}</strong>
                          <span>
                            {item.path.slice(0, -basename(item.path).length)}
                          </span>
                          {item.warnings.length > 0 && (
                            <small>
                              <WarningCircle />
                              {item.warnings.length} fidelity warning
                              {item.warnings.length === 1 ? "" : "s"}
                            </small>
                          )}
                        </button>
                      </td>
                      <td className="mono">{item.codec.toUpperCase()}</td>
                      <td className="mono">
                        {item.video.width && item.video.height
                          ? `${item.video.width}×${item.video.height}`
                          : "Unknown"}
                        {item.video.dolby_vision
                          ? " DV"
                          : item.video.hdr
                            ? " HDR"
                            : ""}
                      </td>
                      <td className="mono">{formatDuration(item.duration)}</td>
                      <td className="mono">{formatBytes(item.size)}</td>
                      <td className="reason-cell">
                        {item.blocked_reason || item.warnings[0] || "None"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon={FilmSlate}
            title="No matching media"
            body="Change the filter or refresh this library."
          />
        )}
      </Panel>
    );
  };

  const renderQueue = () => (
    <div className="queue-layout">
      <Panel
        title="Exact source queue"
        eyebrow={`${selected.size} selected`}
        action={
          <button
            className="secondary"
            type="button"
            onClick={() => void openExistingPlan()}
          >
            <FolderOpen />
            Open plan
          </button>
        }
      >
        {selected.size ? (
          <ol className="queue-list">
            {[...selected].map((path, index) => (
              <li key={path}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{basename(path)}</strong>
                  <small>{path}</small>
                </div>
                <IconButton
                  label={`Remove ${basename(path)}`}
                  onClick={() => toggleSelected(path)}
                >
                  <XCircle />
                </IconButton>
              </li>
            ))}
          </ol>
        ) : (
          <EmptyState
            icon={Queue}
            title="Queue is empty"
            body="Select ready files from the library."
            action={
              <button
                className="secondary"
                type="button"
                onClick={() => setPage("library")}
              >
                Open library
              </button>
            }
          />
        )}
      </Panel>
      <Panel title="Transcode profile" eyebrow="Batch settings">
        <div className="settings-form compact-form">
          <label>
            <span>Format profile</span>
            <select
              value={settings.formatPreset}
              onChange={(event) =>
                updateSettings(
                  "formatPreset",
                  event.target.value as Settings["formatPreset"],
                )
              }
            >
              <option value="recommended">Recommended automatic</option>
              <option value="highest">Highest practical quality</option>
              <option value="high">High quality</option>
              <option value="compact">Compact</option>
              <option value="custom">Custom</option>
            </select>
          </label>
          <div className="field-row">
            <label>
              <span>Quality RF</span>
              <input
                type="number"
                min="0"
                max="51"
                value={settings.quality}
                onChange={(event) =>
                  updateSettings("quality", Number(event.target.value))
                }
              />
            </label>
            <label>
              <span>Encoder preset</span>
              <select
                value={settings.encoderPreset}
                onChange={(event) =>
                  updateSettings("encoderPreset", event.target.value)
                }
              >
                <option>ultrafast</option>
                <option>fast</option>
                <option>medium</option>
                <option>slow</option>
                <option>slower</option>
                <option>veryslow</option>
              </select>
            </label>
            <label>
              <span>Bit depth</span>
              <select
                value={settings.bitDepth}
                onChange={(event) =>
                  updateSettings("bitDepth", Number(event.target.value))
                }
              >
                <option value="8">8-bit</option>
                <option value="10">10-bit</option>
                <option value="12">12-bit</option>
              </select>
            </label>
          </div>
          <div className="field-row two">
            <label>
              <span>Audio languages</span>
              <input
                value={settings.audio}
                onChange={(event) =>
                  updateSettings("audio", event.target.value)
                }
              />
            </label>
            <label>
              <span>Subtitle languages</span>
              <input
                value={settings.subtitles}
                onChange={(event) =>
                  updateSettings("subtitles", event.target.value)
                }
              />
            </label>
          </div>
          <div className="field-row two">
            <label>
              <span>Unknown audio</span>
              <select
                value={settings.unknownAudio}
                onChange={(event) =>
                  updateSettings(
                    "unknownAudio",
                    event.target.value as "keep" | "drop",
                  )
                }
              >
                <option value="keep">Keep</option>
                <option value="drop">Drop</option>
              </select>
            </label>
            <label>
              <span>Unknown subtitles</span>
              <select
                value={settings.unknownSubtitles}
                onChange={(event) =>
                  updateSettings(
                    "unknownSubtitles",
                    event.target.value as "keep" | "drop",
                  )
                }
              >
                <option value="keep">Keep</option>
                <option value="drop">Drop</option>
              </select>
            </label>
          </div>
          <label>
            <span>
              Output directory <small>Optional</small>
            </span>
            <div className="path-input">
              <input
                value={settings.outputDirectory}
                onChange={(event) =>
                  updateSettings("outputDirectory", event.target.value)
                }
              />
              <IconButton
                label="Choose output directory"
                onClick={() =>
                  void chooseDirectory(
                    settings.outputDirectory || undefined,
                  ).then(
                    (value) =>
                      value && updateSettings("outputDirectory", value),
                  )
                }
              >
                <FolderOpen />
              </IconButton>
            </div>
          </label>
          <div className="toggle-grid">
            <label>
              <input
                type="checkbox"
                checked={settings.keepCommentary}
                onChange={(event) =>
                  updateSettings("keepCommentary", event.target.checked)
                }
              />
              <span>Keep commentary</span>
            </label>
            <label>
              <input
                type="checkbox"
                checked={settings.forcedSubtitlesOnly}
                onChange={(event) =>
                  updateSettings("forcedSubtitlesOnly", event.target.checked)
                }
              />
              <span>Forced subtitles only</span>
            </label>
            <label>
              <input
                type="checkbox"
                checked={settings.keepOriginal}
                onChange={(event) =>
                  updateSettings("keepOriginal", event.target.checked)
                }
              />
              <span>Keep original language</span>
            </label>
            <label>
              <input
                type="checkbox"
                checked={settings.allowNoAudio}
                onChange={(event) =>
                  updateSettings("allowNoAudio", event.target.checked)
                }
              />
              <span>Allow no audio</span>
            </label>
          </div>
          <details className="advanced-settings">
            <summary>Advanced controls</summary>
            <div className="advanced-body">
              <div className="field-row">
                <label>
                  <span>x265 tune</span>
                  <input
                    value={settings.tune}
                    onChange={(event) =>
                      updateSettings("tune", event.target.value)
                    }
                    placeholder="Optional"
                  />
                </label>
                <label>
                  <span>Encoder profile</span>
                  <input
                    value={settings.encoderProfile}
                    onChange={(event) =>
                      updateSettings("encoderProfile", event.target.value)
                    }
                    placeholder="Automatic"
                  />
                </label>
                <label>
                  <span>Encoder level</span>
                  <input
                    value={settings.encoderLevel}
                    onChange={(event) =>
                      updateSettings("encoderLevel", event.target.value)
                    }
                    placeholder="Automatic"
                  />
                </label>
              </div>
              <div className="field-row">
                <label>
                  <span>Crop</span>
                  <select
                    value={settings.crop}
                    onChange={(event) =>
                      updateSettings(
                        "crop",
                        event.target.value as Settings["crop"],
                      )
                    }
                  >
                    <option value="auto">Automatic</option>
                    <option value="none">None</option>
                  </select>
                </label>
                <label>
                  <span>Deinterlace</span>
                  <select
                    value={settings.deinterlace}
                    onChange={(event) =>
                      updateSettings(
                        "deinterlace",
                        event.target.value as Settings["deinterlace"],
                      )
                    }
                  >
                    <option value="auto">Automatic</option>
                    <option value="off">Off</option>
                    <option value="decomb">Decomb</option>
                    <option value="yadif">Yadif</option>
                  </select>
                </label>
                <label>
                  <span>Probe workers</span>
                  <input
                    type="number"
                    min="1"
                    max="32"
                    value={settings.workers}
                    onChange={(event) =>
                      updateSettings("workers", Number(event.target.value))
                    }
                  />
                </label>
              </div>
              <div className="field-row two">
                <label>
                  <span>
                    Original language <small>Optional</small>
                  </span>
                  <input
                    value={settings.originalLanguage}
                    onChange={(event) =>
                      updateSettings("originalLanguage", event.target.value)
                    }
                  />
                </label>
                <label>
                  <span>Excluded title text</span>
                  <input
                    value={settings.excludeTitles}
                    onChange={(event) =>
                      updateSettings("excludeTitles", event.target.value)
                    }
                  />
                </label>
              </div>
              <div className="field-row two">
                <label>
                  <span>Extra extensions</span>
                  <input
                    value={settings.extensions}
                    onChange={(event) =>
                      updateSettings("extensions", event.target.value)
                    }
                    placeholder="divx,video"
                  />
                </label>
                <label>
                  <span>Probe timeout</span>
                  <input
                    type="number"
                    min="1"
                    value={settings.probeTimeout}
                    onChange={(event) =>
                      updateSettings("probeTimeout", Number(event.target.value))
                    }
                  />
                </label>
              </div>
              <label>
                <span>
                  Per-file track overrides <small>Optional JSON</small>
                </span>
                <div className="path-input">
                  <input
                    value={settings.overrides}
                    onChange={(event) =>
                      updateSettings("overrides", event.target.value)
                    }
                  />
                  <IconButton
                    label="Choose track overrides"
                    onClick={() =>
                      void chooseFile(
                        ["json"],
                        settings.overrides || undefined,
                      ).then(
                        (value) => value && updateSettings("overrides", value),
                      )
                    }
                  >
                    <FolderOpen />
                  </IconButton>
                </div>
              </label>
              <div className="toggle-grid">
                <label>
                  <input
                    type="checkbox"
                    checked={settings.lossless}
                    onChange={(event) =>
                      updateSettings("lossless", event.target.checked)
                    }
                  />
                  <span>Lossless x265</span>
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={settings.includeHevc}
                    onChange={(event) =>
                      updateSettings("includeHevc", event.target.checked)
                    }
                  />
                  <span>Allow HEVC reprocess</span>
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={settings.useCache}
                    onChange={(event) =>
                      updateSettings("useCache", event.target.checked)
                    }
                  />
                  <span>Use probe cache</span>
                </label>
              </div>
            </div>
          </details>
          <div
            className={`replace-control ${settings.replaceSource ? "armed" : ""}`}
          >
            <div>
              <strong>Replace source</strong>
              <p>Only a smaller, validated output can replace its source.</p>
            </div>
            <label className="switch">
              <input
                type="checkbox"
                aria-label="Replace source"
                checked={settings.replaceSource}
                onChange={(event) => {
                  updateSettings("replaceSource", event.target.checked);
                  if (!event.target.checked)
                    updateSettings("stopWhenLarger", false);
                }}
              />
              <span />
            </label>
          </div>
          {settings.replaceSource && (
            <label className="inline-check">
              <input
                type="checkbox"
                checked={settings.stopWhenLarger}
                onChange={(event) =>
                  updateSettings("stopWhenLarger", event.target.checked)
                }
              />
              Stop early when output reaches source size
            </label>
          )}
          <button
            className="primary wide"
            type="button"
            disabled={!selected.size || loading}
            onClick={() => void createPlan()}
          >
            <ListChecks />
            Create sealed plan
          </button>
        </div>
      </Panel>
      {plan && (
        <Panel
          title="Plan review"
          eyebrow="Ready to execute"
          className="plan-review"
          action={<span className="digest">{plan.digest.slice(0, 10)}</span>}
        >
          <div className="plan-totals">
            <div>
              <strong>{plan.totals.files}</strong>
              <span>files</span>
            </div>
            <div>
              <strong>{formatBytes(plan.totals.source_bytes)}</strong>
              <span>source size</span>
            </div>
            <div>
              <strong>{formatDuration(plan.totals.duration_seconds)}</strong>
              <span>duration</span>
            </div>
          </div>
          <div className="plan-paths">
            {plan.items.map((item) => (
              <div key={item.source}>
                <span>{basename(item.source)}</span>
                <small>{item.destination}</small>
                {item.warnings.map((warning) => (
                  <em key={warning}>
                    <WarningCircle />
                    {warning}
                  </em>
                ))}
              </div>
            ))}
          </div>
          <div className="execution-bar">
            <div>
              <ShieldCheck />
              <span>
                <strong>Safety checks active</strong>
                <small>
                  The plan verifies source identity before each item.
                </small>
              </span>
            </div>
            <button
              className="primary"
              type="button"
              onClick={() => void executePlan(false)}
              disabled={loading}
            >
              <Play weight="fill" />
              Start
            </button>
          </div>
        </Panel>
      )}
    </div>
  );

  const renderActivity = () => {
    if (!activeJob)
      return (
        <EmptyState
          icon={Activity}
          title="No active work"
          body="Create and execute a plan, or start a health check."
          action={
            <button
              className="secondary"
              type="button"
              onClick={() => setPage("queue")}
            >
              Open queue
            </button>
          }
        />
      );
    const elapsed = Math.max(
      0,
      Math.floor((Date.now() - activeJob.startedAt) / 1000),
    );
    return (
      <div className="activity-layout">
        <Panel
          title={
            activeJob.method === "health"
              ? "Media health check"
              : "Transcode activity"
          }
          eyebrow={
            activeJob.finished
              ? activeJob.ok
                ? "Finished"
                : "Stopped"
              : "Running"
          }
          action={
            !activeJob.finished ? (
              <button
                className="danger"
                type="button"
                onClick={() => void cancelJob()}
              >
                <Stop weight="fill" />
                Stop safely
              </button>
            ) : !activeJob.ok &&
              activeJob.method === "plan.execute" &&
              planPath ? (
              <button
                className="secondary"
                type="button"
                onClick={() => void executePlan(true)}
              >
                <ArrowCounterClockwise />
                Retry failed
              </button>
            ) : undefined
          }
        >
          <div className="activity-hero">
            <div
              className={`activity-orb ${activeJob.finished ? "still" : ""}`}
            >
              <Activity weight="fill" />
            </div>
            <div>
              <span>{activeJob.phase}</span>
              <h2>
                {activeJob.source
                  ? basename(activeJob.source)
                  : "Preparing job"}
              </h2>
              <p>
                {activeJob.index && activeJob.total
                  ? `Item ${activeJob.index} of ${activeJob.total}`
                  : "Waiting for the first item"}
              </p>
            </div>
            <strong className="activity-percent">
              {Math.round(activeJob.percent)}%
            </strong>
          </div>
          <div
            className="progress-track"
            role="progressbar"
            aria-label="Job progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(activeJob.percent)}
          >
            <span style={{ width: `${Math.max(1, activeJob.percent)}%` }} />
          </div>
          <div className="activity-meta">
            <span>
              Elapsed <strong>{formatDuration(elapsed)}</strong>
            </span>
            <span>
              Process <strong>{activeJob.id}</strong>
            </span>
          </div>
          {activeJob.error && (
            <div className="inline-error">
              <XCircle />
              {activeJob.error}
            </div>
          )}
          {!activeJob.finished && (
            <div className="cancel-note">
              <Info />A safe stop keeps completed outputs and cleans the current
              partial file.
            </div>
          )}
        </Panel>
        <Panel title="Process log" eyebrow={`${activeJob.logs.length} lines`}>
          <pre className="process-log" tabIndex={0}>
            {activeJob.logs.length
              ? activeJob.logs.join("\n")
              : "Structured progress is active. Diagnostic lines will appear here."}
          </pre>
        </Panel>
      </div>
    );
  };

  const renderOutcomes = () => (
    <Panel
      title="Outcome registry"
      eyebrow="Global memory"
      action={
        <button
          className="secondary"
          type="button"
          onClick={() =>
            window.confirm(
              "Remove stale records and orphan logs? Source media is not deleted.",
            ) &&
            void outcomeAction("outcomes.prune", {}, "Stale outcomes pruned")
          }
        >
          <Broom />
          Prune stale
        </button>
      }
    >
      <div className="tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={outcomeTab === "blocked"}
          onClick={() => setOutcomeTab("blocked")}
        >
          Blocked <span>{failures.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={outcomeTab === "history"}
          onClick={() => setOutcomeTab("history")}
        >
          History <span>{history.length}</span>
        </button>
      </div>
      {outcomeTab === "blocked" ? (
        failures.length ? (
          <div className="outcome-list">
            {failures.map((record) => (
              <article key={`${record.source}-${record.type}`}>
                <StatusMark state={record.active ? "blocked" : "stale"} />
                <div>
                  <h3>{basename(record.source)}</h3>
                  <p>{record.error || record.type}</p>
                  <small>
                    {record.failed_at || record.recorded_at || "Unknown time"}
                    {record.log ? ` · Log: ${record.log}` : ""}
                  </small>
                </div>
                <div className="row-actions">
                  <button
                    type="button"
                    className="secondary"
                    onClick={() =>
                      void outcomeAction(
                        "outcomes.retry",
                        { sources: [record.source] },
                        "Source is ready to retry",
                      )
                    }
                  >
                    <ArrowCounterClockwise />
                    Retry
                  </button>
                  <IconButton
                    label={`Forget ${basename(record.source)}`}
                    onClick={() =>
                      window.confirm(
                        "Forget this outcome? The source can be proposed again.",
                      ) &&
                      void outcomeAction(
                        "outcomes.forget",
                        { sources: [record.source] },
                        "Outcome forgotten",
                      )
                    }
                  >
                    <Trash />
                  </IconButton>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={ShieldCheck}
            title="No blocked files"
            body="Normal scans can propose every eligible source."
          />
        )
      ) : history.length ? (
        <div className="history-list">
          {history.map((record, index) => (
            <article key={`${record.source}-${record.recorded_at}-${index}`}>
              <StatusMark state={record.outcome} />
              <div>
                <strong>{basename(record.source)}</strong>
                <p>{record.result || record.error || record.type}</p>
              </div>
              <time>{record.recorded_at || "Unknown time"}</time>
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={ArrowCounterClockwise}
          title="No attempt history"
          body="Completed and blocked attempts will appear here."
        />
      )}
      {failures.length > 0 && (
        <div className="registry-danger">
          <div>
            <strong>Registry maintenance</strong>
            <p>These actions do not delete source media.</p>
          </div>
          <button
            className="secondary"
            type="button"
            onClick={() =>
              window.confirm(
                "Delete diagnostic logs but keep blocked records?",
              ) &&
              void outcomeAction(
                "outcomes.clear",
                { logs_only: true },
                "Diagnostic logs cleared",
              )
            }
          >
            Clear logs
          </button>
          <button
            className="danger"
            type="button"
            onClick={() =>
              window.confirm(
                "Clear all blocked records? These files can be proposed again.",
              ) &&
              void outcomeAction(
                "outcomes.clear",
                { keep_logs: true },
                "Blocked records cleared",
              )
            }
          >
            Clear records
          </button>
        </div>
      )}
    </Panel>
  );

  const renderHealth = () => (
    <div className="health-layout">
      <Panel title="Integrity pass" eyebrow="Read-only check">
        <div className="mode-switch">
          <button
            type="button"
            aria-pressed={healthMode === "quick"}
            className={healthMode === "quick" ? "active" : ""}
            onClick={() => setHealthMode("quick")}
          >
            <Heartbeat />
            <span>
              <strong>Quick</strong>
              <small>Inspect headers and streams</small>
            </span>
          </button>
          <button
            type="button"
            aria-pressed={healthMode === "full"}
            className={healthMode === "full" ? "active" : ""}
            onClick={() => setHealthMode("full")}
          >
            <HardDrives />
            <span>
              <strong>Full decode</strong>
              <small>Read every frame. This can take hours.</small>
            </span>
          </button>
        </div>
        <div className="health-scope">
          <span>Scope</span>
          <strong>
            {selected.size
              ? `${selected.size} selected file${selected.size === 1 ? "" : "s"}`
              : "Whole library"}
          </strong>
          {selected.size > 0 && (
            <button type="button" onClick={() => setSelected(new Set())}>
              Use whole library
            </button>
          )}
        </div>
        <button
          className="primary wide"
          type="button"
          onClick={() => void runHealth()}
          disabled={loading || !settings.root}
        >
          <Play />
          Start {healthMode} check
        </button>
      </Panel>
      <Panel title="Latest results" eyebrow={`${health.length} checked`}>
        {activeJob?.method === "health" && !activeJob.finished && (
          <div
            className="mini-progress"
            role="progressbar"
            aria-label="Health check progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(activeJob.percent)}
          >
            <span style={{ width: `${activeJob.percent}%` }} />
            <strong>{Math.round(activeJob.percent)}%</strong>
          </div>
        )}
        {health.length ? (
          <div className="health-results">
            {health.map((result) => (
              <article key={result.path}>
                <StatusMark state={result.status} />
                <div>
                  <strong>{basename(result.path)}</strong>
                  <small>{result.path}</small>
                </div>
                <p>{result.error || `${result.mode} check passed`}</p>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            icon={Heartbeat}
            title="No health results"
            body="Run a quick header check or a complete frame decode."
          />
        )}
      </Panel>
    </div>
  );

  const renderSettings = () => (
    <div className="settings-layout">
      <Panel title="Connection" eyebrow="CLI engine">
        <div className="settings-form">
          <label>
            <span>
              Custom BrakeSmith path <small>Optional</small>
            </span>
            <div className="path-input">
              <input
                value={settings.cliPath}
                onChange={(event) =>
                  updateSettings("cliPath", event.target.value)
                }
                placeholder="Use bundled CLI"
              />
              <IconButton
                label="Choose BrakeSmith CLI"
                onClick={() =>
                  void chooseFile([], settings.cliPath || undefined).then(
                    (value) => value && updateSettings("cliPath", value),
                  )
                }
              >
                <FolderOpen />
              </IconButton>
            </div>
          </label>
          <details className="advanced-settings">
            <summary>
              <span>Media tool paths</span>
              <small>Optional GUI launch overrides</small>
            </summary>
            <div className="advanced-body">
              <label>
                <span>HandBrakeCLI</span>
                <div className="path-input">
                  <input
                    value={settings.handbrakePath}
                    onChange={(event) =>
                      updateSettings("handbrakePath", event.target.value)
                    }
                    placeholder="Auto-detect"
                  />
                  <IconButton
                    label="Choose HandBrakeCLI"
                    onClick={() =>
                      void chooseFile(
                        [],
                        settings.handbrakePath || undefined,
                      ).then(
                        (value) =>
                          value && updateSettings("handbrakePath", value),
                      )
                    }
                  >
                    <FolderOpen />
                  </IconButton>
                </div>
              </label>
              <label>
                <span>ffprobe</span>
                <div className="path-input">
                  <input
                    value={settings.ffprobePath}
                    onChange={(event) =>
                      updateSettings("ffprobePath", event.target.value)
                    }
                    placeholder="Auto-detect"
                  />
                  <IconButton
                    label="Choose ffprobe"
                    onClick={() =>
                      void chooseFile(
                        [],
                        settings.ffprobePath || undefined,
                      ).then(
                        (value) =>
                          value && updateSettings("ffprobePath", value),
                      )
                    }
                  >
                    <FolderOpen />
                  </IconButton>
                </div>
              </label>
              <label>
                <span>ffmpeg</span>
                <div className="path-input">
                  <input
                    value={settings.ffmpegPath}
                    onChange={(event) =>
                      updateSettings("ffmpegPath", event.target.value)
                    }
                    placeholder="Auto-detect"
                  />
                  <IconButton
                    label="Choose ffmpeg"
                    onClick={() =>
                      void chooseFile(
                        [],
                        settings.ffmpegPath || undefined,
                      ).then(
                        (value) => value && updateSettings("ffmpegPath", value),
                      )
                    }
                  >
                    <FolderOpen />
                  </IconButton>
                </div>
              </label>
            </div>
          </details>
          <div className="connection-card">
            <Terminal />
            <div>
              <strong>
                {doctor ? `BrakeSmith ${doctor.version}` : "Not checked"}
              </strong>
              <p>{settings.cliPath || "Bundled or system CLI"}</p>
            </div>
            <StatusMark
              state={doctor ? (doctor.healthy ? "healthy" : "error") : "stale"}
            />
          </div>
          <button
            className="secondary"
            type="button"
            onClick={() => void checkDoctor()}
            disabled={loading}
          >
            <ArrowClockwise />
            Check connection
          </button>
        </div>
      </Panel>
      <Panel title="Appearance" eyebrow="Local preference">
        <div className="theme-picker">
          {(
            [
              ["system", Desktop, "System"],
              ["light", Sun, "Light"],
              ["dark", Moon, "Dark"],
            ] as [Theme, Icon, string][]
          ).map(([theme, ThemeIcon, label]) => (
            <button
              type="button"
              key={theme}
              aria-pressed={settings.theme === theme}
              className={settings.theme === theme ? "active" : ""}
              onClick={() => updateSettings("theme", theme)}
            >
              <ThemeIcon />
              <span>{label}</span>
              {settings.theme === theme && <Check weight="bold" />}
            </button>
          ))}
        </div>
      </Panel>
      <Panel title="Project" eyebrow="Open source">
        <div className="link-list">
          <button
            type="button"
            onClick={() =>
              void openExternal("https://github.com/me-cedric/BrakeSmith")
            }
          >
            <GithubLogo />
            <span>
              <strong>BrakeSmith on GitHub</strong>
              <small>Source, documentation, and releases</small>
            </span>
            <Export />
          </button>
          <button
            type="button"
            onClick={() =>
              void openExternal(
                "https://github.com/me-cedric/BrakeSmith/issues",
              )
            }
          >
            <Bug />
            <span>
              <strong>Report a bug</strong>
              <small>Open an issue with diagnostic details</small>
            </span>
            <Export />
          </button>
          <button
            type="button"
            onClick={() => void openExternal("https://github.com/me-cedric")}
          >
            <GithubLogo />
            <span>
              <strong>Cedric Meyer</strong>
              <small>More open-source work</small>
            </span>
            <Export />
          </button>
          <button
            type="button"
            onClick={() => void openExternal("https://ko-fi.com/mecedric")}
          >
            <Coffee />
            <span>
              <strong>Support on Ko-fi</strong>
              <small>Optional support for the project</small>
            </span>
            <Export />
          </button>
        </div>
        <p className="privacy-note">
          <ShieldCheck />
          No account, telemetry, subscription, or payment is built into
          BrakeSmith.
        </p>
      </Panel>
    </div>
  );

  const renderPage = () => {
    switch (page) {
      case "overview":
        return renderOverview();
      case "library":
        return renderLibrary();
      case "queue":
        return renderQueue();
      case "activity":
        return renderActivity();
      case "outcomes":
        return renderOutcomes();
      case "health":
        return renderHealth();
      case "settings":
        return renderSettings();
    }
  };

  return (
    <div className="app-shell">
      <aside className="sidebar" inert={focusedMedia ? true : undefined}>
        <div className="brand">
          <span className="brand-mark">
            <FilmSlate weight="fill" />
          </span>
          <span>
            <strong>BrakeSmith</strong>
            <small>Desktop workshop</small>
          </span>
        </div>
        <nav aria-label="Primary navigation">
          {NAVIGATION.map(({ page: target, label, icon: NavIcon }) => (
            <button
              type="button"
              key={target}
              aria-label={label}
              className={page === target ? "active" : ""}
              onClick={() => setPage(target)}
              aria-current={page === target ? "page" : undefined}
            >
              <NavIcon weight={page === target ? "fill" : "regular"} />
              <span>{label}</span>
              {target === "queue" && selected.size > 0 && (
                <em>{selected.size}</em>
              )}
              {target === "outcomes" && failures.length > 0 && (
                <em className="warning-count">{failures.length}</em>
              )}
            </button>
          ))}
        </nav>
        <button
          aria-label="Settings"
          className={`settings-nav ${page === "settings" ? "active" : ""}`}
          type="button"
          onClick={() => setPage("settings")}
        >
          <GearSix weight={page === "settings" ? "fill" : "regular"} />
          <span>Settings</span>
        </button>
        <div className="local-badge">
          <span />
          <div>
            <strong>Local only</strong>
            <small>No daemon</small>
          </div>
        </div>
      </aside>
      <main inert={focusedMedia ? true : undefined}>
        <header className="command-strip">
          <div>
            <span className="eyebrow">Workspace</span>
            <h1>{pageTitle}</h1>
          </div>
          <button
            aria-label="Choose media library"
            className="library-picker"
            type="button"
            disabled={loading || Boolean(activeJob && !activeJob.finished)}
            onClick={() => void chooseLibrary()}
          >
            <FolderOpen />
            <span>
              <small>Media library</small>
              <strong>
                {settings.root ? basename(settings.root) : "Choose a folder"}
              </strong>
            </span>
          </button>
          <IconButton
            label="Refresh current library"
            onClick={() => void refresh()}
            disabled={!settings.root || loading}
          >
            <ArrowClockwise className={loading ? "spin" : ""} />
          </IconButton>
        </header>
        {!isDesktopRuntime() && (
          <div className="runtime-banner">
            <Info />
            Browser preview is read-only. Start Tauri to connect to the CLI.
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            <XCircle weight="fill" />
            <span>{error}</span>
            <button
              type="button"
              onClick={() => setError("")}
              aria-label="Dismiss error"
            >
              <XCircle />
            </button>
          </div>
        )}
        {scanWarnings.length > 0 && (
          <div className="warning-banner" role="status">
            <WarningCircle weight="fill" />
            <span>
              Partial scan: {scanWarnings[0]}
              {scanWarnings.length > 1
                ? ` (+${scanWarnings.length - 1} more)`
                : ""}
            </span>
            <button
              type="button"
              onClick={() => setScanWarnings([])}
              aria-label="Dismiss scan warning"
            >
              <XCircle />
            </button>
          </div>
        )}
        <div className="page-content">{renderPage()}</div>
      </main>
      {focusedMedia && (
        <>
          <button
            type="button"
            tabIndex={-1}
            className="drawer-backdrop"
            aria-label="Close media details"
            onClick={closeMediaDetails}
          />
          <aside
            ref={detailDrawerRef}
            className="detail-drawer"
            role="dialog"
            aria-modal="true"
            aria-labelledby="media-detail-title"
            tabIndex={-1}
          >
            <header>
              <div>
                <span className="eyebrow">Media details</span>
                <h2 id="media-detail-title">{basename(focusedMedia.path)}</h2>
              </div>
              <IconButton
                label="Close media details"
                onClick={closeMediaDetails}
              >
                <XCircle />
              </IconButton>
            </header>
            <div className="detail-body">
              <div className="detail-summary">
                <StatusMark state={focusedMedia.transcode_status} />
                <p>{focusedMedia.path}</p>
                {focusedMedia.blocked_reason && (
                  <strong className="blocked-reason">
                    Reason: {focusedMedia.blocked_reason}
                  </strong>
                )}
              </div>
              <dl>
                <div>
                  <dt>Codec</dt>
                  <dd>{focusedMedia.codec.toUpperCase()}</dd>
                </div>
                <div>
                  <dt>Video</dt>
                  <dd>
                    {focusedMedia.video.width && focusedMedia.video.height
                      ? `${focusedMedia.video.width}×${focusedMedia.video.height}`
                      : "Unknown"}
                    {focusedMedia.video.dolby_vision
                      ? " Dolby Vision"
                      : focusedMedia.video.hdr
                        ? " HDR"
                        : ""}
                  </dd>
                </div>
                <div>
                  <dt>Duration</dt>
                  <dd>{formatDuration(focusedMedia.duration)}</dd>
                </div>
                <div>
                  <dt>Size</dt>
                  <dd>{formatBytes(focusedMedia.size)}</dd>
                </div>
                <div>
                  <dt>Chapters</dt>
                  <dd>{focusedMedia.chapters}</dd>
                </div>
                <div>
                  <dt>Attachments</dt>
                  <dd>{focusedMedia.attachments}</dd>
                </div>
              </dl>
              <section>
                <h3>Audio tracks</h3>
                {focusedMedia.audio.length ? (
                  <ul>
                    {focusedMedia.audio.map((track) => (
                      <li key={`audio-${track.index}`}>
                        <strong>
                          {track.type_index}. {track.language.toUpperCase()}
                        </strong>
                        <span>
                          {track.codec}
                          {track.title ? ` · ${track.title}` : ""}
                          {track.default ? " · Default" : ""}
                          {track.commentary ? " · Commentary" : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">No audio tracks.</p>
                )}
              </section>
              <section>
                <h3>Subtitle tracks</h3>
                {focusedMedia.subtitles.length ? (
                  <ul>
                    {focusedMedia.subtitles.map((track) => (
                      <li key={`subtitle-${track.index}`}>
                        <strong>
                          {track.type_index}. {track.language.toUpperCase()}
                        </strong>
                        <span>
                          {track.codec}
                          {track.title ? ` · ${track.title}` : ""}
                          {track.default ? " · Default" : ""}
                          {track.forced ? " · Forced" : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">No subtitle tracks.</p>
                )}
              </section>
              <section>
                <h3>Warnings</h3>
                {focusedMedia.warnings.length ? (
                  <ul className="warning-list">
                    {focusedMedia.warnings.map((warning) => (
                      <li key={warning}>
                        <WarningCircle />
                        {warning}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">No detected fidelity warnings.</p>
                )}
              </section>
              <section>
                <h3>Recent attempts</h3>
                {history
                  .filter((entry) => entry.source === focusedMedia.path)
                  .slice(0, 5).length ? (
                  <ul>
                    {history
                      .filter((entry) => entry.source === focusedMedia.path)
                      .slice(0, 5)
                      .map((entry, index) => (
                        <li key={`${entry.recorded_at}-${index}`}>
                          <strong>{entry.outcome}</strong>
                          <span>
                            {entry.result || entry.error || entry.type}
                          </span>
                        </li>
                      ))}
                  </ul>
                ) : (
                  <p className="muted">No saved attempts.</p>
                )}
              </section>
              {focusedMedia.sidecars.length > 0 && (
                <section>
                  <h3>Sidecar files</h3>
                  <ul>
                    {focusedMedia.sidecars.map((path) => (
                      <li key={path}>
                        <span>{path}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          </aside>
        </>
      )}
      {toast && (
        <div className="toast" role="status">
          <CheckCircle weight="fill" />
          {toast}
        </div>
      )}
    </div>
  );
}
