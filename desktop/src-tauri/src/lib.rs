use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    env,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, Command},
    sync::{oneshot, Mutex},
    time::timeout,
};

#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE},
    System::{
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        Threading::CREATE_NO_WINDOW,
    },
};

static NEXT_JOB_ID: AtomicU64 = AtomicU64::new(1);

struct JobRegistry {
    jobs: Arc<Mutex<HashMap<String, Option<oneshot::Sender<()>>>>>,
    shutdown_started: AtomicBool,
}

impl Default for JobRegistry {
    fn default() -> Self {
        Self {
            jobs: Arc::new(Mutex::new(HashMap::new())),
            shutdown_started: AtomicBool::new(false),
        }
    }
}

enum JobCompletion {
    Exited(std::process::ExitStatus),
    Cancelled,
    WaitFailed,
}

struct BridgeProcess {
    child: Child,
    #[cfg(windows)]
    job: WindowsJob,
}

#[cfg(windows)]
struct WindowsJob(HANDLE);

#[cfg(windows)]
unsafe impl Send for WindowsJob {}

#[cfg(windows)]
impl Drop for WindowsJob {
    fn drop(&mut self) {
        unsafe {
            TerminateJobObject(self.0, 1);
            CloseHandle(self.0);
        }
    }
}

#[cfg(windows)]
fn assign_windows_job(child: &Child) -> Result<WindowsJob, String> {
    let process = child
        .raw_handle()
        .ok_or_else(|| "BrakeSmith process exited before isolation".to_string())?
        as HANDLE;
    let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
    if job.is_null() {
        return Err("Cannot create a BrakeSmith process job".to_string());
    }
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    let configured = unsafe {
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            std::ptr::addr_of!(limits).cast(),
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    };
    let assigned = configured != 0 && unsafe { AssignProcessToJobObject(job, process) } != 0;
    if !assigned {
        unsafe { CloseHandle(job) };
        return Err("Cannot isolate the BrakeSmith process tree".to_string());
    }
    Ok(WindowsJob(job))
}

#[derive(Serialize)]
struct CallResponse {
    events: Vec<Value>,
    exit_code: i32,
}

#[derive(Serialize, Clone)]
struct JobEnvelope {
    job_id: String,
    payload: Value,
}

fn executable_name() -> &'static str {
    if cfg!(windows) {
        "brakesmith.exe"
    } else {
        "brakesmith"
    }
}

fn valid_custom_path(raw: &str) -> Result<PathBuf, String> {
    let path = PathBuf::from(raw);
    if !path.is_file() {
        return Err(format!("BrakeSmith CLI does not exist: {}", path.display()));
    }
    path.canonicalize()
        .map_err(|error| format!("Cannot resolve BrakeSmith CLI: {error}"))
}

fn bundled_candidates(app: &AppHandle) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(current) = env::current_exe() {
        if let Some(parent) = current.parent() {
            candidates.push(parent.join(executable_name()));
            candidates.push(parent.join("Resources").join(executable_name()));
            if let Some(contents) = parent.parent() {
                candidates.push(contents.join("Resources").join(executable_name()));
            }
        }
    }
    if let Ok(resources) = app.path().resource_dir() {
        candidates.push(resources.join(executable_name()));
        candidates.push(resources.join("binaries").join(executable_name()));
    }
    candidates
}

fn development_candidates() -> Vec<PathBuf> {
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .unwrap_or_else(|| Path::new("."));
    if cfg!(windows) {
        vec![repository
            .join(".venv")
            .join("Scripts")
            .join("brakesmith.exe")]
    } else {
        vec![repository.join(".venv").join("bin").join("brakesmith")]
    }
}

fn resolve_cli(app: &AppHandle, custom: Option<&str>) -> Result<PathBuf, String> {
    if let Some(path) = custom.filter(|value| !value.trim().is_empty()) {
        return valid_custom_path(path);
    }
    if let Ok(path) = env::var("BRAKESMITH_CLI") {
        if !path.trim().is_empty() {
            return valid_custom_path(&path);
        }
    }
    for path in bundled_candidates(app)
        .into_iter()
        .chain(development_candidates())
    {
        if path.is_file() {
            return Ok(path);
        }
    }
    Ok(PathBuf::from(executable_name()))
}

async fn spawn_bridge(
    app: &AppHandle,
    request: &Value,
    custom: Option<&str>,
) -> Result<BridgeProcess, String> {
    if !request.is_object() {
        return Err("Bridge request must be a JSON object".to_string());
    }
    let executable = resolve_cli(app, custom)?;
    let mut command = Command::new(executable);
    command
        .arg("bridge")
        .arg("--protocol")
        .arg("1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(cfg!(windows));
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Cannot start BrakeSmith CLI: {error}"))?;
    #[cfg(windows)]
    let job = assign_windows_job(&child)?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| "Cannot open BrakeSmith input".to_string())?;
    let payload = serde_json::to_vec(request)
        .map_err(|error| format!("Cannot encode bridge request: {error}"))?;
    stdin
        .write_all(&payload)
        .await
        .map_err(|error| format!("Cannot send bridge request: {error}"))?;
    drop(stdin);
    Ok(BridgeProcess {
        child,
        #[cfg(windows)]
        job,
    })
}

fn parse_events(stdout: &[u8], stderr: &[u8]) -> Vec<Value> {
    let mut events = String::from_utf8_lossy(stdout)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .collect::<Vec<_>>();
    for line in String::from_utf8_lossy(stderr).lines() {
        events.push(json!({ "event": "log", "stream": "transport", "message": line }));
    }
    events
}

#[tauri::command]
async fn call_bridge(
    app: AppHandle,
    request: Value,
    cli_path: Option<String>,
) -> Result<CallResponse, String> {
    let process = spawn_bridge(&app, &request, cli_path.as_deref()).await?;
    let BridgeProcess {
        child,
        #[cfg(windows)]
            job: _job,
    } = process;
    let output = child
        .wait_with_output()
        .await
        .map_err(|error| format!("BrakeSmith bridge failed: {error}"))?;
    Ok(CallResponse {
        events: parse_events(&output.stdout, &output.stderr),
        exit_code: output.status.code().unwrap_or(1),
    })
}

fn emit_job_event(app: &AppHandle, job_id: &str, payload: Value) {
    let _ = app.emit(
        "brakesmith://job-event",
        JobEnvelope {
            job_id: job_id.to_string(),
            payload,
        },
    );
}

async fn stop_child(process: &mut BridgeProcess, cancel_file: &Path) {
    let _ = std::fs::write(cancel_file, b"stop");
    #[cfg(unix)]
    if let Some(process_id) = process.child.id() {
        unsafe {
            libc::kill(process_id as i32, libc::SIGTERM);
        }
        if timeout(Duration::from_secs(12), process.child.wait())
            .await
            .is_ok()
        {
            return;
        }
    }
    #[cfg(windows)]
    if timeout(Duration::from_secs(12), process.child.wait())
        .await
        .is_ok()
    {
        return;
    }
    #[cfg(windows)]
    unsafe {
        TerminateJobObject(process.job.0, 130);
    }
    let _ = process.child.start_kill();
    let _ = process.child.wait().await;
}

async fn stream_job(
    app: AppHandle,
    registry: Arc<Mutex<HashMap<String, Option<oneshot::Sender<()>>>>>,
    job_id: String,
    mut request: Value,
    cli_path: Option<String>,
    mut cancelled: oneshot::Receiver<()>,
) {
    let cancel_directory = match tempfile::Builder::new().prefix("brakesmith-job-").tempdir() {
        Ok(directory) => directory,
        Err(error) => {
            emit_job_event(
                &app,
                &job_id,
                json!({ "event": "error", "message": format!("Cannot create secure cancellation state: {error}") }),
            );
            emit_job_event(
                &app,
                &job_id,
                json!({ "event": "finished", "ok": false, "exit_code": 2 }),
            );
            registry.lock().await.remove(&job_id);
            return;
        }
    };
    let cancel_file = cancel_directory.path().join("cancel");
    request["cancel_file"] = json!(cancel_file);
    let mut process = match spawn_bridge(&app, &request, cli_path.as_deref()).await {
        Ok(process) => process,
        Err(error) => {
            emit_job_event(&app, &job_id, json!({ "event": "error", "message": error }));
            emit_job_event(
                &app,
                &job_id,
                json!({ "event": "finished", "ok": false, "exit_code": 2 }),
            );
            registry.lock().await.remove(&job_id);
            return;
        }
    };
    let stdout = process.child.stdout.take();
    let stderr = process.child.stderr.take();
    let output_app = app.clone();
    let output_job = job_id.clone();
    let terminal_seen = Arc::new(AtomicBool::new(false));
    let output_terminal = terminal_seen.clone();
    let output_task = tokio::spawn(async move {
        if let Some(stream) = stdout {
            let mut lines = BufReader::new(stream).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if let Ok(payload) = serde_json::from_str::<Value>(&line) {
                    if matches!(payload["event"].as_str(), Some("finished" | "cancelled")) {
                        output_terminal.store(true, Ordering::Relaxed);
                    }
                    emit_job_event(&output_app, &output_job, payload);
                } else {
                    emit_job_event(
                        &output_app,
                        &output_job,
                        json!({ "event": "log", "stream": "transport", "message": line }),
                    );
                }
            }
        }
    });
    let error_app = app.clone();
    let error_job = job_id.clone();
    let error_task = tokio::spawn(async move {
        if let Some(stream) = stderr {
            let mut lines = BufReader::new(stream).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                emit_job_event(
                    &error_app,
                    &error_job,
                    json!({ "event": "log", "stream": "transport", "message": line }),
                );
            }
        }
    });

    let completion = tokio::select! {
        status = process.child.wait() => {
            match status {
                Ok(status) => JobCompletion::Exited(status),
                Err(error) => {
                    emit_job_event(&app, &job_id, json!({ "event": "error", "message": error.to_string() }));
                    JobCompletion::WaitFailed
                }
            }
        }
        _ = &mut cancelled => {
            stop_child(&mut process, &cancel_file).await;
            JobCompletion::Cancelled
        }
    };
    let _ = output_task.await;
    let _ = error_task.await;
    let _ = std::fs::remove_file(&cancel_file);
    match completion {
        JobCompletion::Cancelled => {
            if !terminal_seen.load(Ordering::Relaxed) {
                emit_job_event(
                    &app,
                    &job_id,
                    json!({ "event": "cancelled", "message": "Stop completed" }),
                );
            }
        }
        JobCompletion::Exited(status) if !terminal_seen.load(Ordering::Relaxed) => {
            let exit_code = status.code().unwrap_or(1);
            emit_job_event(
                &app,
                &job_id,
                json!({ "event": "error", "message": format!("BrakeSmith exited without a final result (code {exit_code})") }),
            );
            emit_job_event(
                &app,
                &job_id,
                json!({ "event": "finished", "ok": false, "exit_code": exit_code }),
            );
        }
        JobCompletion::WaitFailed if !terminal_seen.load(Ordering::Relaxed) => emit_job_event(
            &app,
            &job_id,
            json!({ "event": "finished", "ok": false, "exit_code": 1 }),
        ),
        JobCompletion::Exited(_) | JobCompletion::WaitFailed => {}
    }
    registry.lock().await.remove(&job_id);
}

#[tauri::command]
async fn start_bridge_job(
    app: AppHandle,
    state: State<'_, JobRegistry>,
    request: Value,
    cli_path: Option<String>,
) -> Result<String, String> {
    if !request.is_object() {
        return Err("Bridge request must be a JSON object".to_string());
    }
    let job_id = format!("job-{}", NEXT_JOB_ID.fetch_add(1, Ordering::Relaxed));
    let (sender, receiver) = oneshot::channel();
    let registry = state.jobs.clone();
    registry.lock().await.insert(job_id.clone(), Some(sender));
    tauri::async_runtime::spawn(stream_job(
        app,
        registry,
        job_id.clone(),
        request,
        cli_path,
        receiver,
    ));
    Ok(job_id)
}

#[tauri::command]
async fn cancel_bridge_job(state: State<'_, JobRegistry>, job_id: String) -> Result<bool, String> {
    let sender = state
        .jobs
        .lock()
        .await
        .get_mut(&job_id)
        .and_then(Option::take);
    Ok(sender.map(|value| value.send(()).is_ok()).unwrap_or(false))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let application = tauri::Builder::default()
        .manage(JobRegistry::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            call_bridge,
            start_bridge_job,
            cancel_bridge_job
        ])
        .build(tauri::generate_context!())
        .expect("error while building BrakeSmith Desktop");
    application.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let state = app.state::<JobRegistry>();
            let has_jobs = state
                .jobs
                .try_lock()
                .map(|jobs| !jobs.is_empty())
                .unwrap_or(true);
            if !has_jobs {
                return;
            }
            if !state.shutdown_started.swap(true, Ordering::SeqCst) {
                api.prevent_exit();
                let handle = app.clone();
                let jobs = state.jobs.clone();
                tauri::async_runtime::spawn(async move {
                    {
                        let mut active = jobs.lock().await;
                        for sender in active.values_mut().filter_map(Option::take) {
                            let _ = sender.send(());
                        }
                    }
                    for _ in 0..65 {
                        if jobs.lock().await.is_empty() {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(200)).await;
                    }
                    handle.exit(0);
                });
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_only_structured_stdout_events() {
        let events = parse_events(
            b"{\"event\":\"accepted\"}\nnot-json\n{\"event\":\"finished\",\"ok\":true}\n",
            b"transport warning\n",
        );
        assert_eq!(events.len(), 3);
        assert_eq!(events[0]["event"], "accepted");
        assert_eq!(events[1]["event"], "finished");
        assert_eq!(events[2]["stream"], "transport");
    }

    #[test]
    fn accepts_an_existing_custom_executable_path() {
        let executable = env::current_exe().expect("test executable path");
        let resolved = valid_custom_path(executable.to_str().expect("UTF-8 executable path"))
            .expect("existing executable");
        assert!(resolved.is_file());
    }
}
