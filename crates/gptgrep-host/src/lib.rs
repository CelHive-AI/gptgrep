//! Local, ephemeral Codex workflows over source-bound GPTgrep evidence.
mod completion;
mod process_group;
mod protocol;
pub use completion::{
    CompletionError, CompletionReport, MAX_COMPLETION_INPUT_BYTES, MAX_COMPLETION_OUTPUT_BYTES,
    complete_json,
};
mod retrieval;
mod trace;

use anyhow::{Result, anyhow, ensure};
pub use retrieval::{Citation, ToolReceipt};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, BufReader},
    process::Command,
};

pub const DEFAULT_MODEL: &str = "gpt-5.6-luna";
pub const DEFAULT_REASONING_EFFORT: &str = "max";

#[derive(Debug)]
pub struct HostCapabilityError;
impl std::fmt::Display for HostCapabilityError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("host_no_evidence_tools")
    }
}
impl std::error::Error for HostCapabilityError {}

#[derive(Debug, Clone)]
pub struct HostConfig {
    pub codex_bin: String,
    pub codex_home: PathBuf,
    pub model: String,
    pub reasoning_effort: String,
    pub timeout_secs: u64,
    pub max_tool_calls: usize,
    pub max_input_bytes: usize,
    pub trace_path: Option<PathBuf>,
}

impl Default for HostConfig {
    fn default() -> Self {
        Self {
            codex_bin: "codex".into(),
            codex_home: std::env::var_os("CODEX_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".codex")
                }),
            model: DEFAULT_MODEL.into(),
            reasoning_effort: DEFAULT_REASONING_EFFORT.into(),
            timeout_secs: 180,
            max_tool_calls: 12,
            max_input_bytes: 256 * 1024,
            trace_path: None,
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
pub struct HostReport {
    pub schema_version: String,
    pub status: String,
    pub operation: String,
    pub generation: String,
    pub thread_id: String,
    pub turn_id: String,
    pub requested_model: String,
    pub model: String,
    pub model_provider: String,
    pub auth_mode: String,
    pub codex_home: PathBuf,
    pub requested_reasoning_effort: String,
    pub effective_reasoning_effort: Option<String>,
    pub answer: String,
    pub citations: Vec<Citation>,
    pub tool_calls: Vec<ToolReceipt>,
    pub usage: Option<Value>,
    pub elapsed_ms: u128,
    pub stderr_bytes: Option<u64>,
    pub stderr_truncated: Option<bool>,
    pub warnings: Vec<String>,
}

pub async fn ask(root: &Path, question: &str, config: &HostConfig) -> Result<HostReport> {
    execute(root, question, None, config).await
}

pub async fn summarize(root: &Path, node_id: &str, config: &HostConfig) -> Result<HostReport> {
    ensure!(
        !node_id.is_empty() && node_id.len() <= 256,
        "Invalid summary node ID"
    );
    execute(root, "Summarize the selected node, its scope, key claims and limitations. Expand its document tree and read relevant child or neighboring nodes when needed. Cite the evidence used.", Some(node_id), config).await
}

async fn execute(
    root: &Path,
    question: &str,
    node_id: Option<&str>,
    config: &HostConfig,
) -> Result<HostReport> {
    validate_config(config, question)?;
    let root = root
        .canonicalize()
        .map_err(|_| anyhow!("Document root is unavailable"))?;
    let mut evidence = retrieval::Evidence::open(&root, node_id)?;
    let completed = run_process(
        config,
        protocol::Workflow::Retrieval {
            question,
            node_id,
            evidence: &mut evidence,
        },
    )
    .await?;
    let ProcessOutcome {
        result,
        home,
        elapsed_ms,
        stderr_bytes,
        stderr_truncated,
    } = completed;
    let (answer, citations, insufficient) = evidence.finish(&result.answer)?;
    let mut warnings = result.warnings;
    warnings.push("Citation validation checks issued snapshot identity and current source freshness; it does not prove semantic entailment.".into());
    Ok(HostReport {
        schema_version: "gptgrep.host.v1".into(),
        status: if insufficient {
            "insufficient_evidence"
        } else {
            "completed"
        }
        .into(),
        operation: if node_id.is_some() {
            "summarize"
        } else {
            "ask"
        }
        .into(),
        generation: evidence.generation.clone(),
        thread_id: result.thread_id,
        turn_id: result.turn_id,
        requested_model: config.model.clone(),
        model: result.model,
        model_provider: result.provider,
        auth_mode: "chatgpt".into(),
        codex_home: home,
        requested_reasoning_effort: config.reasoning_effort.clone(),
        effective_reasoning_effort: result.effort,
        answer,
        citations,
        tool_calls: evidence.receipts,
        usage: result.usage,
        elapsed_ms,
        stderr_bytes,
        stderr_truncated,
        warnings,
    })
}

struct ProcessOutcome {
    result: protocol::Outcome,
    home: PathBuf,
    elapsed_ms: u128,
    stderr_bytes: Option<u64>,
    stderr_truncated: Option<bool>,
}

async fn run_process(
    config: &HostConfig,
    workflow: protocol::Workflow<'_>,
) -> Result<ProcessOutcome> {
    let trace = trace::Trace::open(config.trace_path.as_deref())?;
    let home = config
        .codex_home
        .canonicalize()
        .map_err(|_| anyhow!("Selected CODEX_HOME is unavailable"))?;
    let cwd = tempfile::Builder::new().prefix("gptgrep-host-").tempdir()?;
    let binary = if Path::new(&config.codex_bin).components().count() > 1 {
        PathBuf::from(&config.codex_bin)
            .canonicalize()
            .map_err(|_| anyhow!("Configured Codex binary is unavailable"))?
    } else {
        PathBuf::from(&config.codex_bin)
    };
    ensure!(home.is_dir(), "Selected CODEX_HOME must be a directory");
    let mut command = process_command(&binary, cwd.path(), &home)?;
    let mut child = command
        .spawn()
        .map_err(|_| anyhow!("Could not start the configured Codex app-server"))?;
    let mut process_group = process_group::OwnedGroup::new(&child)?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("Codex stdin unavailable"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow!("Codex stdout unavailable"))?;
    let mut stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow!("Codex stderr unavailable"))?;
    let mut stderr_task = tokio::spawn(async move {
        const CAP: usize = 64 * 1024;
        let mut retained = Vec::new();
        let mut total = 0u64;
        let mut buffer = [0; 4096];
        while let Ok(count) = stderr.read(&mut buffer).await {
            if count == 0 {
                break;
            }
            total = total.saturating_add(count as u64);
            let keep = count.min(CAP.saturating_sub(retained.len()));
            retained.extend_from_slice(&buffer[..keep]);
        }
        // Raw stderr is private to this task and is never placed in a report or error.
        (total, total > retained.len() as u64)
    });
    let started = Instant::now();
    let session = protocol::run(
        BufReader::new(stdout),
        stdin,
        cwd.path(),
        config,
        workflow,
        trace,
    );
    let outcome = tokio::time::timeout(Duration::from_secs(config.timeout_secs), session).await;
    // All exits kill and reap the owned app-server. kill_on_drop also covers caller cancellation.
    if let Err(error) = process_group.stop(&mut child).await {
        stderr_task.abort();
        return Err(error);
    }
    let stderr_result = tokio::time::timeout(Duration::from_secs(2), &mut stderr_task).await;
    let (stderr_bytes, stderr_truncated) = match stderr_result {
        Ok(Ok((bytes, truncated))) => (Some(bytes), Some(truncated)),
        _ => {
            stderr_task.abort();
            (None, None)
        }
    };
    let result = outcome.map_err(|_| {
        anyhow!("Codex host exceeded its time limit; owned app-server was terminated")
    })??;
    Ok(ProcessOutcome {
        result,
        home,
        elapsed_ms: started.elapsed().as_millis(),
        stderr_bytes,
        stderr_truncated,
    })
}

fn process_command(binary: &Path, cwd: &Path, home: &Path) -> Result<Command> {
    let mut command = Command::new(binary);
    process_group::configure(&mut command);
    command
        .arg("app-server")
        .args(["--listen", "stdio://"])
        .current_dir(cwd)
        .env("CODEX_HOME", home)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for key in [
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENROUTER_API_KEY",
        "CODEX_SQLITE_HOME",
        "CODEX_THREAD_ID",
        "CODEX_TURN_ID",
        "CODEX_PARENT_THREAD_ID",
        "CODEX_SESSION_ID",
    ] {
        command.env_remove(key);
    }
    // Explicit CLI state root wins over an inherited CODEX_SQLITE_HOME or config override.
    command
        .arg("-c")
        .arg(format!("sqlite_home={}", serde_json::to_string(home)?));
    for (key, value) in protocol::base_config().as_object().expect("static object") {
        command
            .arg("-c")
            .arg(format!("{key}={}", protocol::toml_override(value)?));
    }
    Ok(command)
}

fn validate_config(config: &HostConfig, question: &str) -> Result<()> {
    ensure!(
        !question.trim().is_empty() && question.len() <= 8192,
        "Question must contain 1..8192 bytes"
    );
    ensure!(
        (1..=900).contains(&config.timeout_secs),
        "Host timeout must be 1..900 seconds"
    );
    ensure!(
        (1..=64).contains(&config.max_tool_calls),
        "Host tool-call limit must be 1..64"
    );
    ensure!(
        !config.model.is_empty()
            && config.model.len() <= 128
            && !config.model.chars().any(char::is_control),
        "Invalid host model"
    );
    ensure!(
        matches!(
            config.reasoning_effort.as_str(),
            "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra"
        ),
        "Invalid reasoning effort"
    );
    ensure!(!config.codex_bin.is_empty(), "Codex binary is empty");
    Ok(())
}

pub(crate) fn final_schema() -> Value {
    json!({"type":"object","additionalProperties":false,
        "properties":{"answer":{"type":"string","maxLength":8192},
        "citations":{"type":"array","maxItems":24,"items":{"type":"string"}},
        "insufficient_evidence":{"type":"boolean"}},
        "required":["answer","citations","insufficient_evidence"]})
}

#[cfg(test)]
mod tests;
