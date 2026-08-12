import { chmod, copyFile, mkdir, stat } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = path.resolve(desktopRoot, "..");
const targetTriple = execFileSync("rustc", ["--print", "host-tuple"], { encoding: "utf8" }).trim();
const windows = targetTriple.includes("windows");
const executable = windows ? "brakesmith.exe" : "brakesmith";
const candidates = [
  process.env.BRAKESMITH_SIDECAR_SOURCE,
  path.join(repositoryRoot, "dist", executable),
  windows
    ? path.join(repositoryRoot, ".venv", "Scripts", "brakesmith.exe")
    : path.join(repositoryRoot, ".venv", "bin", "brakesmith"),
].filter(Boolean);

let source;
for (const candidate of candidates) {
  try {
    if ((await stat(candidate)).isFile()) {
      source = candidate;
      break;
    }
  } catch {
    // Try the next explicit candidate.
  }
}

if (!source) {
  throw new Error("No BrakeSmith sidecar found. Build dist/brakesmith or set BRAKESMITH_SIDECAR_SOURCE.");
}

const destinationDirectory = path.join(desktopRoot, "src-tauri", "binaries");
const destination = path.join(destinationDirectory, `brakesmith-${targetTriple}${windows ? ".exe" : ""}`);
await mkdir(destinationDirectory, { recursive: true });
await copyFile(source, destination);
if (!windows) await chmod(destination, 0o755);
process.stdout.write(`Prepared ${destination}\n`);
