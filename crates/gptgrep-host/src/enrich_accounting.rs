//! A separate append-only ledger for the explicit raw-document builder.
//! No ask-role counters or protocol observers are shared with this workflow.
use crate::enrich::{BuildBinding, CompletedWindow, EnrichCursor};
use crate::retrieval::hash;
use anyhow::{Result, anyhow, ensure};
use gptgrep_core::NavigationOverlayPublication;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub(crate) const MAX_RECORD_BYTES: usize = 32 * 1024;
pub(crate) const MAX_LEDGER_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum CallKind {
    Builder,
    Jev,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Reservation {
    pub call_id: usize,
    pub kind: CallKind,
    pub anchor_id: String,
    pub requested_model: String,
    pub request_sha256: String,
    pub request_bytes: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Observation {
    pub model: String,
    pub provider: Option<String>,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub response_id: Option<String>,
    pub effective_reasoning_effort: Option<String>,
    pub effective_service_tier: Option<String>,
    pub server_retry_notifications: Option<usize>,
    /// Numeric allowlist only. Missing usage is unknown, including on failed calls.
    pub usage: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Receipt {
    pub call_id: usize,
    pub elapsed_ms: u64,
    pub error_code: Option<String>,
    pub observed: Option<Observation>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Event {
    Bound {
        binding: BuildBinding,
        plan_sha256: String,
    },
    CallReserved {
        reservation: Reservation,
    },
    CallFinished {
        receipt: Receipt,
    },
    WindowCompleted {
        window: CompletedWindow,
    },
    Checkpoint {
        cursor: Option<EnrichCursor>,
        reason: String,
    },
    Failed {
        code: String,
    },
    PublicationPrepared {
        artifact_sha256: String,
    },
    Published {
        publication: NavigationOverlayPublication,
    },
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Line {
    sequence: usize,
    previous_sha256: Option<String>,
    payload: Event,
    sha256: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct EnrichCallSummary {
    /// Logical calls admitted before launch; not inferred physical or billed requests.
    pub attempted_calls: usize,
    pub completed_calls: usize,
    pub failed_calls: usize,
    pub unobserved_calls: usize,
    pub missing_usage_calls: usize,
    pub models: Vec<String>,
    pub known_total_tokens: Option<u64>,
    pub missing_total_tokens: usize,
    pub total_tokens_overflowed: bool,
}

pub(crate) struct Ledger {
    file: File,
    pub path: PathBuf,
    pub binding: BuildBinding,
    plan_sha256: String,
    pub windows: Vec<CompletedWindow>,
    pub reservations: Vec<Reservation>,
    pub receipts: Vec<Receipt>,
    pub failed: bool,
    pub publication_prepared: Option<String>,
    pub publication: Option<NavigationOverlayPublication>,
    sequence: usize,
    previous_sha256: Option<String>,
    bytes: u64,
    failed_write: bool,
    builder_calls: usize,
    jev_calls: usize,
    committed_calls: usize,
}

impl Ledger {
    pub fn open(
        path: &Path,
        binding: BuildBinding,
        plan_sha256: &str,
        resume: bool,
    ) -> Result<Self> {
        ensure!(path.is_absolute(), "enrich_ledger_path_must_be_absolute");
        no_symlinks(path)?;
        let parent = path
            .parent()
            .ok_or_else(|| anyhow!("enrich_ledger_path_invalid"))?;
        fs::create_dir_all(parent).map_err(|_| anyhow!("enrich_ledger_storage_failed"))?;
        no_symlinks(path)?;
        let mut options = OpenOptions::new();
        options.read(true).append(true);
        if !resume {
            options.create_new(true);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
        }
        let mut file = options
            .open(path)
            .map_err(|_| anyhow!("enrich_ledger_open_failed"))?;
        file.try_lock()
            .map_err(|_| anyhow!("enrich_ledger_locked"))?;
        let size = file.metadata()?.len();
        ensure!(
            size <= MAX_LEDGER_BYTES && size <= binding.max_ledger_bytes,
            "enrich_ledger_limit"
        );
        let mut bytes = vec![];
        file.read_to_end(&mut bytes)?;
        let mut ledger = Self {
            file,
            path: path.to_owned(),
            binding: binding.clone(),
            plan_sha256: plan_sha256.into(),
            windows: vec![],
            reservations: vec![],
            receipts: vec![],
            failed: false,
            publication_prepared: None,
            publication: None,
            sequence: 0,
            previous_sha256: None,
            bytes: 0,
            failed_write: false,
            builder_calls: 0,
            jev_calls: 0,
            committed_calls: 0,
        };
        if resume {
            ensure!(
                !bytes.is_empty() && bytes.ends_with(b"\n"),
                "enrich_ledger_partial_or_empty"
            );
            for raw in bytes.split_inclusive(|byte| *byte == b'\n') {
                ensure!(raw.len() <= MAX_RECORD_BYTES, "enrich_ledger_record_limit");
                let line: Line =
                    serde_json::from_slice(raw).map_err(|_| anyhow!("enrich_ledger_invalid"))?;
                ensure!(
                    line.sequence == ledger.sequence
                        && line.previous_sha256 == ledger.previous_sha256,
                    "enrich_ledger_chain_invalid"
                );
                ensure!(
                    line.sha256 == event_hash(line.sequence, &line.previous_sha256, &line.payload)?,
                    "enrich_ledger_digest_invalid"
                );
                ledger.apply(&line.payload)?;
                ledger.sequence += 1;
                ledger.previous_sha256 = Some(line.sha256);
                ledger.bytes += raw.len() as u64;
            }
            ensure!(!ledger.failed, "enrich_failed_build_cannot_resume");
            ensure!(
                ledger.reservations.len() == ledger.receipts.len(),
                "enrich_pending_call_blocks_resume"
            );
            ensure!(
                ledger.completed_call_count() == ledger.reservations.len(),
                "enrich_uncommitted_window_blocks_resume"
            );
        } else {
            ledger.append(Event::Bound {
                binding,
                plan_sha256: plan_sha256.into(),
            })?;
            File::open(parent)?.sync_all()?;
        }
        Ok(ledger)
    }

    fn completed_call_count(&self) -> usize {
        self.committed_calls
    }

    pub fn attempt_count(&self, kind: CallKind) -> usize {
        match kind {
            CallKind::Builder => self.builder_calls,
            CallKind::Jev => self.jev_calls,
        }
    }

    fn apply(&mut self, event: &Event) -> Result<()> {
        ensure!(
            self.publication.is_none() && !self.failed,
            "enrich_ledger_terminal"
        );
        match event {
            Event::Bound {
                binding,
                plan_sha256,
            } => ensure!(
                self.sequence == 0 && *binding == self.binding && *plan_sha256 == self.plan_sha256,
                "enrich_binding_changed"
            ),
            _ if self.sequence == 0 => return Err(anyhow!("enrich_ledger_unbound")),
            Event::CallReserved { reservation } => {
                ensure!(
                    self.publication_prepared.is_none()
                        && reservation.call_id == self.reservations.len()
                        && self.reservations.len() == self.receipts.len(),
                    "enrich_call_reservation_invalid"
                );
                ensure!(
                    reservation.request_sha256.len() == 64 && reservation.request_bytes > 0,
                    "enrich_request_binding_invalid"
                );
                let count = self.attempt_count(reservation.kind);
                let limit = match reservation.kind {
                    CallKind::Builder => self.binding.max_builder_calls,
                    CallKind::Jev => self.binding.max_jev_calls,
                };
                ensure!(count < limit, "enrich_call_budget_exhausted");
                let expected = match reservation.kind {
                    CallKind::Builder => &self.binding.builder_model,
                    CallKind::Jev => &self.binding.jev_model,
                };
                ensure!(
                    reservation.requested_model == *expected,
                    "enrich_call_profile_changed"
                );
                self.reservations.push(reservation.clone());
                match reservation.kind {
                    CallKind::Builder => self.builder_calls += 1,
                    CallKind::Jev => self.jev_calls += 1,
                }
            }
            Event::CallFinished { receipt } => {
                ensure!(
                    receipt.call_id == self.receipts.len()
                        && self.reservations.len() == self.receipts.len() + 1,
                    "enrich_call_receipt_invalid"
                );
                self.receipts.push(receipt.clone());
            }
            Event::WindowCompleted { window } => {
                let start = self.completed_call_count();
                let end = start + 1 + usize::from(window.jev_call_id.is_some());
                ensure!(
                    self.reservations.len() == end
                        && self.receipts.len() == end
                        && window.builder_call_id == start
                        && window.jev_call_id.is_none_or(|id| id == start + 1),
                    "enrich_window_call_binding_invalid"
                );
                for call in start..end {
                    ensure!(
                        self.reservations[call].anchor_id == window.anchor.anchor_id
                            && self.receipts[call].error_code.is_none()
                            && self.receipts[call].observed.is_some(),
                        "enrich_window_receipt_invalid"
                    );
                }
                ensure!(
                    self.reservations[start].kind == CallKind::Builder
                        && window
                            .jev_call_id
                            .is_none_or(|id| self.reservations[id].kind == CallKind::Jev),
                    "enrich_window_call_kind_invalid"
                );
                self.windows.push(window.clone());
                self.committed_calls = end;
            }
            Event::Checkpoint { .. } => ensure!(
                self.reservations.len() == self.completed_call_count(),
                "enrich_checkpoint_incomplete_window"
            ),
            Event::Failed { .. } => self.failed = true,
            Event::PublicationPrepared { artifact_sha256 } => {
                ensure!(
                    self.reservations.len() == self.completed_call_count()
                        && self.publication_prepared.is_none()
                        && artifact_sha256.len() == 64,
                    "enrich_publication_binding_invalid"
                );
                self.publication_prepared = Some(artifact_sha256.clone());
            }
            Event::Published { publication } => {
                ensure!(
                    self.publication_prepared.as_ref() == Some(&publication.artifact_sha256)
                        && publication.generation == self.binding.source.generation
                        && publication.manifest_sha256 == self.binding.source.manifest_sha256,
                    "enrich_publication_receipt_invalid"
                );
                self.publication = Some(publication.clone());
            }
        }
        Ok(())
    }

    pub fn capacity_for_window(&self) -> bool {
        // Reserve room for both calls, their receipts, the window and a terminal checkpoint.
        self.bytes + (MAX_RECORD_BYTES as u64 * 7) <= self.binding.max_ledger_bytes
    }

    pub fn append(&mut self, payload: Event) -> Result<()> {
        ensure!(!self.failed_write, "enrich_ledger_storage_failed");
        let sha256 = event_hash(self.sequence, &self.previous_sha256, &payload)?;
        let line = Line {
            sequence: self.sequence,
            previous_sha256: self.previous_sha256.clone(),
            payload,
            sha256: sha256.clone(),
        };
        let mut bytes = serde_json::to_vec(&line)?;
        bytes.push(b'\n');
        ensure!(
            bytes.len() <= MAX_RECORD_BYTES
                && self.bytes + bytes.len() as u64 <= self.binding.max_ledger_bytes,
            "enrich_ledger_limit"
        );
        // In-memory validation comes first; a failed write poisons this handle and resume
        // validates the whole durable chain. No provider call follows a failed reservation.
        self.apply(&line.payload)?;
        if self
            .file
            .write_all(&bytes)
            .and_then(|()| self.file.sync_all())
            .is_err()
        {
            self.failed_write = true;
            return Err(anyhow!("enrich_ledger_storage_failed"));
        }
        self.sequence += 1;
        self.previous_sha256 = Some(sha256);
        self.bytes += bytes.len() as u64;
        Ok(())
    }

    pub fn summary(&self, kind: CallKind) -> EnrichCallSummary {
        let calls: Vec<_> = self
            .reservations
            .iter()
            .filter(|call| call.kind == kind)
            .collect();
        let mut result = EnrichCallSummary {
            attempted_calls: calls.len(),
            ..Default::default()
        };
        let mut models = BTreeSet::new();
        let mut seen = BTreeSet::new();
        for call in calls {
            let receipt = self.receipts.get(call.call_id);
            let observed = receipt.and_then(|receipt| receipt.observed.as_ref());
            if let Some(receipt) = receipt {
                if receipt.error_code.is_none() {
                    result.completed_calls += 1;
                } else {
                    result.failed_calls += 1;
                }
            }
            if observed.is_none() {
                result.unobserved_calls += 1;
            }
            if observed.and_then(|value| value.usage.as_ref()).is_none() {
                result.missing_usage_calls += 1;
            }
            if let Some(observed) = observed {
                models.insert(observed.model.clone());
                // Never add the same actual native turn twice. Jev receipts without a
                // provider ID are still independent admitted logical calls.
                let identity = match kind {
                    CallKind::Builder => format!("{:?}:{:?}", observed.thread_id, observed.turn_id),
                    CallKind::Jev => observed
                        .response_id
                        .clone()
                        .unwrap_or_else(|| format!("call:{}", call.call_id)),
                };
                if !seen.insert(identity) {
                    continue;
                }
            }
            let total =
                observed
                    .and_then(|value| value.usage.as_ref())
                    .and_then(|usage| match kind {
                        CallKind::Builder => usage["total"]["totalTokens"].as_u64(),
                        CallKind::Jev => usage["total_tokens"].as_u64(),
                    });
            match total {
                Some(total) if !result.total_tokens_overflowed => {
                    result.known_total_tokens =
                        result.known_total_tokens.unwrap_or(0).checked_add(total);
                    result.total_tokens_overflowed = result.known_total_tokens.is_none();
                }
                Some(_) => (),
                None => result.missing_total_tokens += 1,
            }
        }
        result.models = models.into_iter().collect();
        result
    }
}

fn event_hash(sequence: usize, previous: &Option<String>, payload: &Event) -> Result<String> {
    Ok(hash(&serde_json::to_vec(
        &json!({"sequence":sequence,"previous_sha256":previous,"payload":payload}),
    )?))
}

fn no_symlinks(path: &Path) -> Result<()> {
    let mut current = PathBuf::new();
    for part in path.components() {
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(metadata) => ensure!(!metadata.file_type().is_symlink(), "enrich_ledger_symlink"),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err(anyhow!("enrich_ledger_storage_failed")),
        }
    }
    Ok(())
}

pub(crate) fn builder_usage(value: Option<&Value>) -> Option<Value> {
    let value = value?;
    let mut total = serde_json::Map::new();
    for field in [
        "totalTokens",
        "inputTokens",
        "cachedInputTokens",
        "cacheWriteInputTokens",
        "outputTokens",
        "reasoningOutputTokens",
    ] {
        if let Some(number) = value["total"][field].as_u64() {
            total.insert(field.into(), json!(number));
        }
    }
    (!total.is_empty()).then(|| json!({"total":total}))
}

pub(crate) fn jev_usage(value: &Value) -> Option<Value> {
    let mut usage = serde_json::Map::new();
    for field in ["prompt_tokens", "completion_tokens", "total_tokens"] {
        if let Some(number) = value[field].as_u64() {
            usage.insert(field.into(), json!(number));
        }
    }
    if let Some(cost) = value["cost"]
        .as_f64()
        .filter(|cost| cost.is_finite() && *cost >= 0.0)
    {
        usage.insert("cost".into(), json!(cost));
    }
    (!usage.is_empty()).then_some(Value::Object(usage))
}
