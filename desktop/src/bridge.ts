import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open, save } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { BridgeEvent, JobEnvelope } from "./types";

interface CallResponse {
  events: BridgeEvent[];
  exit_code: number;
}

export interface BridgeCall<T> {
  data: T;
  events: BridgeEvent[];
  exitCode: number;
}

function request(method: string, params: Record<string, unknown>) {
  return { protocol: 1, method, params };
}

export function isDesktopRuntime(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

export async function callBridgeDetailed<T>(
  method: string,
  params: Record<string, unknown> = {},
  cliPath = "",
): Promise<BridgeCall<T>> {
  if (!isDesktopRuntime()) {
    throw new Error("Start this interface with Tauri to connect to BrakeSmith CLI.");
  }
  const response = await invoke<CallResponse>("call_bridge", {
    request: request(method, params),
    cliPath: cliPath || null,
  });
  const error = [...response.events].reverse().find((event) => event.event === "error");
  const result = [...response.events].reverse().find((event) => event.event === "result");
  if (!result || (response.exit_code !== 0 && !["doctor", "health"].includes(method))) {
    throw new Error(error?.message || `BrakeSmith exited with code ${response.exit_code}`);
  }
  return { data: result.data as T, events: response.events, exitCode: response.exit_code };
}

export async function callBridge<T>(
  method: string,
  params: Record<string, unknown> = {},
  cliPath = "",
): Promise<T> {
  return (await callBridgeDetailed<T>(method, params, cliPath)).data;
}

export async function startBridgeJob(
  method: string,
  params: Record<string, unknown>,
  cliPath = "",
): Promise<string> {
  if (!isDesktopRuntime()) {
    throw new Error("Start this interface with Tauri to run BrakeSmith jobs.");
  }
  return invoke<string>("start_bridge_job", {
    request: request(method, params),
    cliPath: cliPath || null,
  });
}

export async function cancelBridgeJob(jobId: string): Promise<boolean> {
  return invoke<boolean>("cancel_bridge_job", { jobId });
}

export function onJobEvent(handler: (event: JobEnvelope) => void): Promise<UnlistenFn> {
  return listen<JobEnvelope>("brakesmith://job-event", (event) => handler(event.payload));
}

export async function chooseDirectory(defaultPath?: string): Promise<string | null> {
  const chosen = await open({ directory: true, multiple: false, defaultPath });
  return typeof chosen === "string" ? chosen : null;
}

export async function chooseFile(
  extensions: string[],
  defaultPath?: string,
): Promise<string | null> {
  const chosen = await open({
    directory: false,
    multiple: false,
    defaultPath,
    ...(extensions.length ? { filters: [{ name: "Supported file", extensions }] } : {}),
  });
  return typeof chosen === "string" ? chosen : null;
}

export function chooseSavePath(name: string, extensions: string[]): Promise<string | null> {
  return save({ defaultPath: name, filters: [{ name: "BrakeSmith file", extensions }] });
}

export function openExternal(url: string): Promise<void> {
  return openUrl(url);
}
