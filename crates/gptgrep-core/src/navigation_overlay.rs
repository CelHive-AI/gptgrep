//! Optional model-derived navigation metadata, never citation authority.
//!
//! Overlay publication is separate from immutable index generations. Loading
//! binds metadata cheaply; a selected document is source-checked before its
//! hints can be obtained. This module performs no model or provider calls.
use super::{
    Document, Pointer, SCHEMA, STATE, Snapshot, TextView, bounded_read, digest, no_symlink,
};
use anyhow::{Context, Result, ensure};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

const OVERLAY_SCHEMA: &str = "gptgrep.navigation-overlay.v1";
const POINTER_NAME: &str = "NAVIGATION.json";
const ARTIFACT_DIRECTORY: &str = "navigation-overlays";
pub const MAX_NAVIGATION_OVERLAY_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_NAVIGATION_HINT_BYTES: usize = 4096;
const MAX_DOCUMENTS: usize = 1024;
const MAX_HINTS: usize = 16_384;
const MAX_WINDOWS_PER_HINT: usize = 8;
const MAX_WINDOW_BYTES: usize = 65_536;
const MAX_WINDOWS: usize = 65_536;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationOverlayBinding {
    pub generation: String,
    pub manifest_sha256: String,
    pub documents_total: usize,
    pub nodes_total: usize,
}

/// Producer-reported identity, shape-checked here; not proof of a provider call.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationOverlayProducer {
    pub model: String,
    pub reasoning_effort: String,
    pub service_tier: String,
    pub prompt_sha256: String,
    pub schema_sha256: String,
    pub jev: NavigationJevIdentity,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationJevIdentity {
    pub requested_model: Option<String>,
    pub actual_models: Vec<String>,
    /// Logical operation attempts, not physical requests or inferred billing.
    pub logical_calls_attempted: usize,
    pub validated_responses: usize,
    pub prompt_sha256: String,
    pub schema_sha256: String,
}

/// Hint presence and exact canonical byte-union coverage are separate. Neither
/// proves that a model understood the text or that its hints are semantically true.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationOverlayCoverage {
    pub documents_total: usize,
    pub documents_with_hints: usize,
    pub nodes_total: usize,
    pub nodes_with_hints: usize,
    pub hints: usize,
    pub raw_windows: usize,
    pub hint_window_references: usize,
    pub canonical_text_bytes: usize,
    /// Exact interval union; overlapping source windows count once.
    pub covered_text_bytes: usize,
    pub documents_with_complete_windows: usize,
    pub partial_source_coverage: bool,
    pub partial_hint_coverage: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NavigationHintOrigin {
    ModelDerivedNavigationOnly,
}

/// A provenance binding, not a Hit, citation, excerpt or evidence token.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationWindowBinding {
    /// Host-issued navigation anchor, never a citation/evidence token.
    pub anchor_id: String,
    /// Exact half-open UTF-8 byte interval in canonical text.
    pub byte_start: usize,
    pub byte_end: usize,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum NavigationHintTarget {
    Document,
    Chunk {
        anchor_id: String,
    },
    /// Qualified existing ID: document_id:node_id.
    Node {
        node_id: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationHint {
    pub origin: NavigationHintOrigin,
    pub target: NavigationHintTarget,
    pub hint: String,
    pub anchor_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationDocumentHints {
    pub document_id: String,
    pub path: String,
    pub source_sha256: String,
    pub text_sha256: String,
    pub text_bytes: usize,
    pub windows: Vec<NavigationWindowBinding>,
    pub hints: Vec<NavigationHint>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationDocumentIdentity {
    pub document_id: String,
    pub path: String,
    pub source_sha256: String,
    pub text_sha256: String,
    pub text_bytes: usize,
}

/// Exact source data for a builder; contains no citation authority.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationDocumentWindow {
    pub generation: String,
    pub manifest_sha256: String,
    pub document: NavigationDocumentIdentity,
    pub byte_start: usize,
    pub byte_end: usize,
    pub sha256: String,
    pub text: String,
    pub next_offset: Option<usize>,
}

impl NavigationDocumentWindow {
    pub fn anchor(&self, anchor_id: impl Into<String>) -> NavigationWindowBinding {
        NavigationWindowBinding {
            anchor_id: anchor_id.into(),
            byte_start: self.byte_start,
            byte_end: self.byte_end,
            sha256: self.sha256.clone(),
        }
    }
}

/// Source/text checked once at open, then UTF-8-safe in-memory traversal.
/// Publication rechecks the complete source and every supplied anchor.
pub struct NavigationDocumentCursor {
    root: PathBuf,
    binding: NavigationOverlayBinding,
    identity: NavigationDocumentIdentity,
    text: String,
    offset: usize,
}

impl NavigationDocumentCursor {
    pub fn binding(&self) -> &NavigationOverlayBinding {
        &self.binding
    }

    pub fn identity(&self) -> &NavigationDocumentIdentity {
        &self.identity
    }

    pub fn offset(&self) -> usize {
        self.offset
    }

    pub fn next_window(&mut self, max_bytes: usize) -> Result<Option<NavigationDocumentWindow>> {
        ensure!(
            (1..=MAX_WINDOW_BYTES).contains(&max_bytes),
            "navigation window budget must be 1..65536"
        );
        check_current(&self.root, &self.binding)?;
        if self.offset == self.text.len() {
            return Ok(None);
        }
        let window = self.read_window(max_bytes, self.offset)?;
        self.offset = window.byte_end;
        Ok(Some(window))
    }

    pub fn read_window(
        &self,
        max_bytes: usize,
        offset_bytes: usize,
    ) -> Result<NavigationDocumentWindow> {
        ensure!(
            (1..=MAX_WINDOW_BYTES).contains(&max_bytes),
            "navigation window budget must be 1..65536"
        );
        check_current(&self.root, &self.binding)?;
        ensure!(
            offset_bytes <= self.text.len() && self.text.is_char_boundary(offset_bytes),
            "invalid navigation document offset"
        );
        let mut end = offset_bytes.saturating_add(max_bytes).min(self.text.len());
        while !self.text.is_char_boundary(end) {
            end -= 1;
        }
        ensure!(
            end > offset_bytes || end == self.text.len(),
            "navigation window budget cannot fit next UTF-8 scalar"
        );
        let text = self.text[offset_bytes..end].to_owned();
        Ok(NavigationDocumentWindow {
            generation: self.binding.generation.clone(),
            manifest_sha256: self.binding.manifest_sha256.clone(),
            document: self.identity.clone(),
            byte_start: offset_bytes,
            byte_end: end,
            sha256: digest(text.as_bytes()),
            text,
            next_offset: (end < self.text.len()).then_some(end),
        })
    }
}

pub fn open_navigation_document(
    root: &Path,
    document_path: &str,
) -> Result<NavigationDocumentCursor> {
    let (snapshot, binding) = open_base(root)?;
    let document = snapshot
        .manifest
        .documents
        .iter()
        .find(|document| document.path == document_path)
        .context("navigation document is not in the current index")?;
    ensure!(
        snapshot.fresh(document),
        "navigation source changed or disappeared; rebuild required"
    );
    let text = snapshot.text(document)?;
    check_current(&snapshot.manifest.root, &binding)?;
    Ok(NavigationDocumentCursor {
        root: snapshot.manifest.root.clone(),
        binding,
        identity: NavigationDocumentIdentity {
            document_id: document.id.clone(),
            path: document.path.clone(),
            source_sha256: document.source_sha256.clone(),
            text_sha256: document.text_sha256.clone(),
            text_bytes: text.len(),
        },
        text,
        offset: 0,
    })
}

/// Convenience one-shot read. Builders should reuse open_navigation_document
/// instead of rehashing a full source for each chunk.
pub fn read_document_window(
    root: &Path,
    document_path: &str,
    max_bytes: usize,
    offset_bytes: usize,
) -> Result<NavigationDocumentWindow> {
    open_navigation_document(root, document_path)?.read_window(max_bytes, offset_bytes)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationOverlay {
    pub schema_version: String,
    pub generation: String,
    pub manifest_sha256: String,
    pub producer: NavigationOverlayProducer,
    pub coverage: NavigationOverlayCoverage,
    pub documents: Vec<NavigationDocumentHints>,
}

impl NavigationOverlay {
    /// Computes hint counts and byte-union coverage. Publication verifies source data.
    pub fn new(
        binding: &NavigationOverlayBinding,
        producer: NavigationOverlayProducer,
        documents: Vec<NavigationDocumentHints>,
    ) -> Result<Self> {
        let coverage = coverage(binding, &documents)?;
        Ok(Self {
            schema_version: OVERLAY_SCHEMA.into(),
            generation: binding.generation.clone(),
            manifest_sha256: binding.manifest_sha256.clone(),
            producer,
            coverage,
            documents,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NavigationOverlayPublication {
    pub schema_version: String,
    pub generation: String,
    pub manifest_sha256: String,
    pub artifact_sha256: String,
}

/// Base/schema-verified metadata. Raw hints remain private until selected reads
/// validate that document's current source, canonical text and exact windows.
pub struct BoundNavigationOverlay {
    snapshot: Snapshot,
    binding: NavigationOverlayBinding,
    publication: NavigationOverlayPublication,
    overlay: NavigationOverlay,
}

impl BoundNavigationOverlay {
    pub fn binding(&self) -> &NavigationOverlayBinding {
        &self.binding
    }

    pub fn publication(&self) -> &NavigationOverlayPublication {
        &self.publication
    }

    pub fn producer(&self) -> &NavigationOverlayProducer {
        &self.overlay.producer
    }

    pub fn coverage(&self) -> &NavigationOverlayCoverage {
        &self.overlay.coverage
    }

    /// Exact source-relative path, as in the ordinary deterministic catalog.
    /// A fully traversed document may return an empty hints list. Active v1
    /// overlays must include every indexed document's raw-window coverage.
    pub fn selected_document_hints(
        &self,
        root: &Path,
        document_path: &str,
    ) -> Result<Option<NavigationDocumentHints>> {
        ensure!(
            root.canonicalize()? == self.snapshot.manifest.root,
            "navigation overlay root changed"
        );
        self.check_pointers()?;
        let document = self
            .snapshot
            .manifest
            .documents
            .iter()
            .find(|document| document.path == document_path)
            .context("navigation document is not in the bound index")?;
        let Some(hints) = self
            .overlay
            .documents
            .iter()
            .find(|hints| hints.document_id == document.id)
        else {
            return Ok(None);
        };
        validate_document_windows(&self.snapshot, document, hints)?;
        self.check_pointers()?;
        Ok(Some(hints.clone()))
    }

    fn check_pointers(&self) -> Result<()> {
        let root = &self.snapshot.manifest.root;
        check_current(root, &self.binding)?;
        ensure!(
            read_publication(root)?.as_ref() == Some(&self.publication),
            "navigation overlay pointer changed"
        );
        Ok(())
    }
}

pub fn navigation_overlay_binding(root: &Path) -> Result<NavigationOverlayBinding> {
    Ok(open_base(root)?.1)
}

/// Does not read source bodies or canonical text. Stale selected documents fail
/// in selected_document_hints; absent overlays do not affect ordinary readers.
pub fn read_navigation_overlay(root: &Path) -> Result<Option<BoundNavigationOverlay>> {
    let (snapshot, binding) = open_base(root)?;
    let root = &snapshot.manifest.root;
    let Some(publication) = read_publication(root)? else {
        return Ok(None);
    };
    ensure!(
        publication.generation == binding.generation
            && publication.manifest_sha256 == binding.manifest_sha256,
        "navigation overlay is bound to a different index generation"
    );
    let artifact = artifact_path(root, &publication.artifact_sha256)?;
    let bytes = bounded_read(&artifact, MAX_NAVIGATION_OVERLAY_BYTES as u64)?;
    ensure!(
        digest(&bytes) == publication.artifact_sha256,
        "navigation overlay artifact digest mismatch"
    );
    let overlay: NavigationOverlay = serde_json::from_slice(&bytes)?;
    validate_structure(&snapshot, &binding, &overlay)?;
    ensure!(
        !overlay.coverage.partial_source_coverage,
        "active navigation overlay has incomplete raw-document coverage"
    );
    let bound = BoundNavigationOverlay {
        snapshot,
        binding,
        publication,
        overlay,
    };
    bound.check_pointers()?;
    Ok(Some(bound))
}

/// Validate every supplied window before publishing an immutable artifact and
/// then a separate atomic pointer. CURRENT and its generation are never edited.
pub fn publish_navigation_overlay(
    root: &Path,
    overlay: &NavigationOverlay,
) -> Result<NavigationOverlayPublication> {
    publish(root, overlay, || Ok(()))
}

fn publish(
    root: &Path,
    overlay: &NavigationOverlay,
    before_pointer: impl FnOnce() -> Result<()>,
) -> Result<NavigationOverlayPublication> {
    let root = root.canonicalize()?;
    let state = root.join(STATE);
    no_symlink(&state)?;
    let lock_path = state.join("write.lock");
    no_symlink(&lock_path)?;
    let lock = OpenOptions::new().read(true).write(true).open(lock_path)?;
    lock.try_lock_exclusive()
        .context("another index or navigation writer owns this corpus")?;
    let (snapshot, binding) = open_base(&root)?;
    validate_structure(&snapshot, &binding, overlay)?;
    ensure!(
        !overlay.coverage.partial_source_coverage,
        "cannot publish incomplete raw-document navigation coverage"
    );
    for hints in &overlay.documents {
        let document = snapshot
            .manifest
            .documents
            .iter()
            .find(|document| document.id == hints.document_id)
            .expect("structure checked document identity");
        validate_document_windows(&snapshot, document, hints)?;
    }
    let bytes = serde_json::to_vec(overlay)?;
    ensure!(
        bytes.len() <= MAX_NAVIGATION_OVERLAY_BYTES,
        "navigation overlay exceeds artifact byte limit"
    );
    let publication = NavigationOverlayPublication {
        schema_version: OVERLAY_SCHEMA.into(),
        generation: binding.generation.clone(),
        manifest_sha256: binding.manifest_sha256.clone(),
        artifact_sha256: digest(&bytes),
    };
    let directory = state.join(ARTIFACT_DIRECTORY);
    no_symlink(&directory)?;
    fs::create_dir_all(&directory)?;
    let artifact = artifact_path(&root, &publication.artifact_sha256)?;
    if artifact.exists() {
        ensure!(
            bounded_read(&artifact, MAX_NAVIGATION_OVERLAY_BYTES as u64)? == bytes,
            "existing immutable navigation artifact changed"
        );
    } else {
        let mut temporary = tempfile::NamedTempFile::new_in(&directory)?;
        temporary.write_all(&bytes)?;
        temporary.as_file().sync_all()?;
        temporary
            .persist_noclobber(&artifact)
            .context("failed to retain immutable navigation artifact")?;
        File::open(&directory)?.sync_all()?;
    }
    before_pointer()?;
    check_current(&root, &binding)?;
    let pointer = state.join(POINTER_NAME);
    no_symlink(&pointer)?;
    let mut temporary = tempfile::NamedTempFile::new_in(&state)?;
    serde_json::to_writer(&mut temporary, &publication)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary
        .persist(pointer)
        .context("failed to publish navigation overlay pointer")?;
    File::open(&state)?.sync_all()?;
    Ok(publication)
}

fn open_base(root: &Path) -> Result<(Snapshot, NavigationOverlayBinding)> {
    let root = root.canonicalize()?;
    let pointer = current_pointer(&root)?;
    let snapshot = Snapshot::open(&root)?;
    ensure!(
        snapshot.manifest.generation == pointer.generation,
        "index generation changed while binding navigation overlay"
    );
    let nodes_total = snapshot
        .manifest
        .documents
        .iter()
        .try_fold(0usize, |sum, document| {
            sum.checked_add(document.nodes.len())
        })
        .context("navigation node count overflow")?;
    let binding = NavigationOverlayBinding {
        generation: pointer.generation,
        manifest_sha256: pointer.manifest_sha256,
        documents_total: snapshot.manifest.documents.len(),
        nodes_total,
    };
    check_current(&root, &binding)?;
    Ok((snapshot, binding))
}

fn current_pointer(root: &Path) -> Result<Pointer> {
    no_symlink(&root.join(STATE))?;
    let path = root.join(STATE).join("CURRENT.json");
    no_symlink(&path)?;
    let pointer: Pointer = serde_json::from_slice(&bounded_read(&path, 4096)?)?;
    ensure!(
        pointer.schema_version == SCHEMA,
        "unsupported index pointer"
    );
    Ok(pointer)
}

fn check_current(root: &Path, binding: &NavigationOverlayBinding) -> Result<()> {
    let current = current_pointer(root)?;
    ensure!(
        current.generation == binding.generation
            && current.manifest_sha256 == binding.manifest_sha256,
        "navigation overlay base pointer changed"
    );
    Ok(())
}

fn read_publication(root: &Path) -> Result<Option<NavigationOverlayPublication>> {
    no_symlink(&root.join(STATE))?;
    let path = root.join(STATE).join(POINTER_NAME);
    match fs::symlink_metadata(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
        Ok(metadata) => ensure!(
            metadata.is_file(),
            "navigation pointer is not a regular file"
        ),
    }
    let publication: NavigationOverlayPublication =
        serde_json::from_slice(&bounded_read(&path, 4096)?)?;
    ensure!(
        publication.schema_version == OVERLAY_SCHEMA,
        "unsupported navigation overlay pointer schema"
    );
    check_sha(&publication.artifact_sha256)?;
    check_sha(&publication.manifest_sha256)?;
    Ok(Some(publication))
}

fn artifact_path(root: &Path, sha256: &str) -> Result<PathBuf> {
    check_sha(sha256)?;
    let directory = root.join(STATE).join(ARTIFACT_DIRECTORY);
    no_symlink(&directory)?;
    let artifact = directory.join(format!("{sha256}.json"));
    no_symlink(&artifact)?;
    Ok(artifact)
}

fn check_sha(value: &str) -> Result<()> {
    ensure!(
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)),
        "navigation identity must be a lowercase SHA-256 digest"
    );
    Ok(())
}

fn check_label(value: &str) -> Result<()> {
    ensure!(
        !value.trim().is_empty() && value.len() <= 256 && !value.chars().any(char::is_control),
        "navigation producer label is empty, oversized or contains control characters"
    );
    Ok(())
}

fn coverage(
    binding: &NavigationOverlayBinding,
    documents: &[NavigationDocumentHints],
) -> Result<NavigationOverlayCoverage> {
    let mut nodes = HashSet::new();
    let mut hints = 0usize;
    let mut windows = 0usize;
    let mut references = 0usize;
    let mut bytes = 0usize;
    let mut covered = 0usize;
    let mut complete_documents = 0usize;
    for document in documents {
        windows = windows
            .checked_add(document.windows.len())
            .context("navigation window count overflow")?;
        bytes = bytes
            .checked_add(document.text_bytes)
            .context("navigation text bytes overflow")?;
        let document_covered = covered_bytes(document)?;
        covered = covered
            .checked_add(document_covered)
            .context("navigation coverage bytes overflow")?;
        complete_documents += usize::from(document_covered == document.text_bytes);
        for hint in &document.hints {
            if let NavigationHintTarget::Node { node_id } = &hint.target {
                nodes.insert(node_id);
            }
            hints = hints
                .checked_add(1)
                .context("navigation hint count overflow")?;
            references = references
                .checked_add(hint.anchor_ids.len())
                .context("navigation reference count overflow")?;
        }
    }
    let documents_with_hints = documents
        .iter()
        .filter(|document| !document.hints.is_empty())
        .count();
    Ok(NavigationOverlayCoverage {
        documents_total: binding.documents_total,
        documents_with_hints,
        nodes_total: binding.nodes_total,
        nodes_with_hints: nodes.len(),
        hints,
        raw_windows: windows,
        hint_window_references: references,
        canonical_text_bytes: bytes,
        covered_text_bytes: covered,
        documents_with_complete_windows: complete_documents,
        partial_source_coverage: documents.len() < binding.documents_total
            || complete_documents < documents.len(),
        partial_hint_coverage: documents_with_hints < binding.documents_total
            || nodes.len() < binding.nodes_total,
    })
}

fn covered_bytes(document: &NavigationDocumentHints) -> Result<usize> {
    let mut intervals: Vec<_> = document
        .windows
        .iter()
        .map(|window| (window.byte_start, window.byte_end))
        .collect();
    intervals.sort_unstable();
    let mut covered = 0usize;
    let mut prior_end = 0usize;
    for (start, end) in intervals {
        ensure!(
            start < end && end <= document.text_bytes,
            "navigation window outside declared canonical text"
        );
        covered = covered
            .checked_add(end.saturating_sub(start.max(prior_end)))
            .context("navigation interval union overflow")?;
        prior_end = prior_end.max(end);
    }
    Ok(covered)
}

fn validate_structure(
    snapshot: &Snapshot,
    binding: &NavigationOverlayBinding,
    overlay: &NavigationOverlay,
) -> Result<()> {
    ensure!(
        overlay.schema_version == OVERLAY_SCHEMA
            && overlay.generation == binding.generation
            && overlay.manifest_sha256 == binding.manifest_sha256,
        "navigation overlay schema or base identity differs"
    );
    ensure!(
        overlay.documents.len() <= MAX_DOCUMENTS,
        "too many navigation documents"
    );
    let producer = &overlay.producer;
    for value in [
        &producer.model,
        &producer.reasoning_effort,
        &producer.service_tier,
    ] {
        check_label(value)?;
    }
    let jev = &producer.jev;
    for value in [
        &producer.prompt_sha256,
        &producer.schema_sha256,
        &jev.prompt_sha256,
        &jev.schema_sha256,
    ] {
        check_sha(value)?;
    }
    if let Some(requested) = &jev.requested_model {
        check_label(requested)?;
    }
    ensure!(
        jev.actual_models.len() <= 16
            && jev.validated_responses <= jev.logical_calls_attempted
            && jev.logical_calls_attempted <= 1_000_000
            && (jev.validated_responses == 0 || !jev.actual_models.is_empty()),
        "invalid navigation Jev identity or operation counts"
    );
    let mut models = HashSet::new();
    for model in &jev.actual_models {
        check_label(model)?;
        ensure!(models.insert(model), "duplicate navigation Jev model");
    }
    let expected = coverage(binding, &overlay.documents)?;
    ensure!(
        overlay.coverage == expected,
        "navigation hint coverage metadata differs"
    );
    ensure!(
        expected.hints <= MAX_HINTS && expected.raw_windows <= MAX_WINDOWS,
        "too many navigation hints or windows"
    );
    let mut document_ids = HashSet::new();
    for hints in &overlay.documents {
        ensure!(
            document_ids.insert(&hints.document_id),
            "duplicate navigation document"
        );
        let document = snapshot
            .manifest
            .documents
            .iter()
            .find(|document| document.id == hints.document_id)
            .context("unknown navigation document")?;
        ensure!(
            document.path == hints.path
                && document.source_sha256 == hints.source_sha256
                && document.text_sha256 == hints.text_sha256,
            "navigation document source or text binding differs"
        );
        ensure!(
            hints.text_bytes as u64 <= super::MAX_TEXT_BYTES,
            "navigation text byte limit"
        );
        let nodes: HashSet<_> = document
            .nodes
            .iter()
            .map(|node| format!("{}:{}", document.id, node.id))
            .collect();
        let mut anchors = HashSet::new();
        let mut windows = HashSet::new();
        for window in &hints.windows {
            check_label(&window.anchor_id)?;
            ensure!(
                anchors.insert(&window.anchor_id),
                "duplicate navigation anchor ID"
            );
            ensure!(
                window.byte_end > window.byte_start
                    && window.byte_end - window.byte_start <= MAX_WINDOW_BYTES,
                "invalid or oversized navigation window"
            );
            ensure!(
                windows.insert((window.byte_start, window.byte_end)),
                "duplicate navigation window"
            );
            check_sha(&window.sha256)?;
        }
        let mut node_targets = HashSet::new();
        for hint in &hints.hints {
            ensure!(
                !hint.hint.trim().is_empty()
                    && hint.hint.len() <= MAX_NAVIGATION_HINT_BYTES
                    && !hint.hint.chars().any(char::is_control),
                "navigation hint is empty, oversized or contains control characters"
            );
            match &hint.target {
                NavigationHintTarget::Document => {}
                NavigationHintTarget::Chunk { anchor_id } => {
                    ensure!(
                        hint.anchor_ids.len() == 1 && hint.anchor_ids.first() == Some(anchor_id),
                        "chunk hint must reference its exact anchor"
                    );
                }
                NavigationHintTarget::Node { node_id } => {
                    ensure!(nodes.contains(node_id), "unknown navigation hint node");
                    ensure!(
                        node_targets.insert(node_id),
                        "duplicate navigation node hint target"
                    );
                }
            }
            ensure!(
                (1..=MAX_WINDOWS_PER_HINT).contains(&hint.anchor_ids.len()),
                "navigation hint window limit"
            );
            let mut references = HashSet::new();
            for anchor in &hint.anchor_ids {
                ensure!(anchors.contains(anchor), "unknown navigation hint anchor");
                ensure!(
                    references.insert(anchor),
                    "duplicate navigation hint anchor reference"
                );
            }
        }
    }
    Ok(())
}

fn validate_document_windows(
    snapshot: &Snapshot,
    document: &Document,
    hints: &NavigationDocumentHints,
) -> Result<()> {
    ensure!(
        snapshot.fresh(document),
        "navigation source changed or disappeared; rebuild required"
    );
    let view = TextView::new(snapshot.text(document)?);
    ensure!(
        view.text.len() == hints.text_bytes,
        "navigation canonical text length differs"
    );
    for window in &hints.windows {
        let text = view
            .text
            .get(window.byte_start..window.byte_end)
            .context("navigation window is not an exact UTF-8 interval")?;
        ensure!(
            digest(text.as_bytes()) == window.sha256,
            "navigation raw window digest differs"
        );
    }
    for hint in &hints.hints {
        if let NavigationHintTarget::Node { node_id } = &hint.target {
            let local = node_id
                .strip_prefix(&format!("{}:", document.id))
                .context("navigation node document differs")?;
            let node = document
                .nodes
                .iter()
                .find(|node| node.id == local)
                .context("unknown navigation hint node")?;
            let (start, end) = view
                .node_bytes(node)
                .context("navigation node interval outside canonical text")?;
            for anchor in &hint.anchor_ids {
                let window = hints
                    .windows
                    .iter()
                    .find(|window| window.anchor_id == *anchor)
                    .context("unknown navigation hint anchor")?;
                ensure!(
                    start <= window.byte_start && window.byte_end <= end,
                    "navigation window is outside its node"
                );
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use anyhow::bail;
    use serde_json::json;

    async fn fixture(files: &[(&str, &str)]) -> Result<tempfile::TempDir> {
        let directory = tempfile::tempdir()?;
        for (name, text) in files {
            fs::write(directory.path().join(name), text)?;
        }
        super::super::index(directory.path(), 64).await?;
        Ok(directory)
    }

    fn producer() -> NavigationOverlayProducer {
        NavigationOverlayProducer {
            model: "synthetic-builder".into(),
            reasoning_effort: "max".into(),
            service_tier: "fast".into(),
            prompt_sha256: digest(b"synthetic-builder-prompt"),
            schema_sha256: digest(b"synthetic-builder-schema"),
            jev: NavigationJevIdentity {
                requested_model: Some("synthetic-jev".into()),
                actual_models: vec!["synthetic-jev".into()],
                logical_calls_attempted: 1,
                validated_responses: 1,
                prompt_sha256: digest(b"synthetic-jev-prompt"),
                schema_sha256: digest(b"synthetic-jev-schema"),
            },
        }
    }

    fn complete_overlay(root: &Path) -> Result<NavigationOverlay> {
        let (snapshot, binding) = open_base(root)?;
        let mut documents = Vec::new();
        for document in &snapshot.manifest.documents {
            let mut cursor = open_navigation_document(root, &document.path)?;
            let identity = cursor.identity().clone();
            let mut windows = Vec::new();
            while let Some(window) = cursor.next_window(17)? {
                windows.push(window.anchor(format!("anchor-{}", windows.len())));
            }
            let hints = windows.first().map_or_else(Vec::new, |window| {
                vec![NavigationHint {
                    origin: NavigationHintOrigin::ModelDerivedNavigationOnly,
                    target: NavigationHintTarget::Chunk {
                        anchor_id: window.anchor_id.clone(),
                    },
                    hint: "Synthetic navigation description".into(),
                    anchor_ids: vec![window.anchor_id.clone()],
                }]
            });
            documents.push(NavigationDocumentHints {
                document_id: identity.document_id,
                path: identity.path,
                source_sha256: identity.source_sha256,
                text_sha256: identity.text_sha256,
                text_bytes: identity.text_bytes,
                windows,
                hints,
            });
        }
        NavigationOverlay::new(&binding, producer(), documents)
    }

    fn recount(root: &Path, overlay: &mut NavigationOverlay) -> Result<()> {
        overlay.coverage = coverage(&navigation_overlay_binding(root)?, &overlay.documents)?;
        Ok(())
    }

    /// Simulate a stored artifact whose digest pointer was also changed. Lazy
    /// selected validation must still reject invalid source-window provenance.
    fn replace_artifact(root: &Path, overlay: &NavigationOverlay) -> Result<()> {
        let bytes = serde_json::to_vec(overlay)?;
        let sha256 = digest(&bytes);
        fs::write(artifact_path(root, &sha256)?, bytes)?;
        fs::write(
            root.join(STATE).join(POINTER_NAME),
            serde_json::to_vec(&NavigationOverlayPublication {
                schema_version: OVERLAY_SCHEMA.into(),
                generation: overlay.generation.clone(),
                manifest_sha256: overlay.manifest_sha256.clone(),
                artifact_sha256: sha256,
            })?,
        )?;
        Ok(())
    }

    #[tokio::test]
    async fn navigation_absence_and_publication_leave_deterministic_outputs_unchanged() -> Result<()>
    {
        let directory = fixture(&[(
            "notes.md",
            "# First\nCopper beads.\n# Second\nBlue shelves.\n",
        )])
        .await?;
        let root = directory.path();
        let current = fs::read(root.join(STATE).join("CURRENT.json"))?;
        let catalog = super::super::catalog(root)?;
        let tree = super::super::tree(root, Path::new("notes.md"))?;
        assert!(read_navigation_overlay(root)?.is_none());
        let overlay = complete_overlay(root)?;
        let publication = publish_navigation_overlay(root, &overlay)?;
        let bound = read_navigation_overlay(root)?.unwrap();
        assert_eq!(bound.publication(), &publication);
        assert_eq!(
            bound.selected_document_hints(root, "notes.md")?,
            Some(overlay.documents[0].clone())
        );
        assert_eq!(fs::read(root.join(STATE).join("CURRENT.json"))?, current);
        assert_eq!(super::super::catalog(root)?, catalog);
        assert_eq!(super::super::tree(root, Path::new("notes.md"))?, tree);
        assert_eq!(publish_navigation_overlay(root, &overlay)?, publication);
        Ok(())
    }

    #[tokio::test]
    async fn navigation_cursor_reaches_eof_with_exact_utf8_crlf_and_no_first_node_cutoff()
    -> Result<()> {
        let text = format!(
            "{}\n# Tail\nFinal violet marker.\r\n",
            "α🙂\r\nplain text\n".repeat(10_000)
        );
        let directory = fixture(&[("long.md", &text)]).await?;
        let root = directory.path();
        let mut cursor = open_navigation_document(root, "long.md")?;
        assert!(cursor.identity().text_bytes > 65_536);
        let mut collected = String::new();
        while let Some(window) = cursor.next_window(65_531)? {
            assert_eq!(window.byte_start, collected.len());
            assert_eq!(window.sha256, digest(window.text.as_bytes()));
            assert_eq!(
                window.next_offset,
                (window.byte_end < text.len()).then_some(window.byte_end)
            );
            collected.push_str(&window.text);
        }
        assert_eq!(collected, text);
        assert_eq!(cursor.offset(), text.len());
        assert!(cursor.read_window(1, 0).is_err());
        assert!(cursor.read_window(4, 1).is_err());
        let eof = read_document_window(root, "long.md", 4, text.len())?;
        assert_eq!(eof.text, "");
        assert_eq!(eof.next_offset, None);
        assert!(cursor.read_window(4, text.len() + 1).is_err());
        Ok(())
    }

    #[tokio::test]
    async fn navigation_cursor_is_pinned_in_memory_but_publication_rechecks_source() -> Result<()> {
        let directory = fixture(&[(
            "notes.txt",
            "Original complete source, including the end.\n",
        )])
        .await?;
        let root = directory.path();
        let mut cursor = open_navigation_document(root, "notes.txt")?;
        let overlay = complete_overlay(root)?;
        fs::write(
            root.join("notes.txt"),
            "Changed after the cursor was opened.\n",
        )?;
        let window = cursor.next_window(65_536)?.unwrap();
        assert!(window.text.starts_with("Original"));
        assert_eq!(
            window.document.source_sha256,
            overlay.documents[0].source_sha256
        );
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        assert!(read_navigation_overlay(root)?.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn navigation_read_checks_only_selected_document_bodies() -> Result<()> {
        let directory = fixture(&[
            ("a.txt", "Intact first document.\n"),
            ("b.txt", "Second source.\n"),
        ])
        .await?;
        let root = directory.path();
        publish_navigation_overlay(root, &complete_overlay(root)?)?;
        let (snapshot, _) = open_base(root)?;
        let second = snapshot
            .manifest
            .documents
            .iter()
            .find(|document| document.path == "b.txt")
            .unwrap();
        fs::write(root.join("b.txt"), "Source changed.\n")?;
        fs::write(
            snapshot.dir.join("text").join(format!("{}.txt", second.id)),
            "Canonical text damaged.\n",
        )?;
        let bound = read_navigation_overlay(root)?.unwrap();
        assert!(bound.selected_document_hints(root, "a.txt")?.is_some());
        assert!(bound.selected_document_hints(root, "b.txt").is_err());
        assert!(
            bound
                .selected_document_hints(root, "not-present.txt")
                .is_err()
        );
        Ok(())
    }

    #[tokio::test]
    async fn navigation_stale_base_rejects_reader_cursor_and_old_renamed_reordered_overlay()
    -> Result<()> {
        let directory = fixture(&[("original.md", "# First\nOne.\n# Second\nTwo.\n")]).await?;
        let root = directory.path();
        let old = complete_overlay(root)?;
        publish_navigation_overlay(root, &old)?;
        let bound = read_navigation_overlay(root)?.unwrap();
        let cursor = open_navigation_document(root, "original.md")?;
        fs::rename(root.join("original.md"), root.join("relocated.md"))?;
        fs::write(root.join("relocated.md"), "# Second\nTwo.\n# First\nOne.\n")?;
        super::super::index(root, 64).await?;
        assert!(read_navigation_overlay(root).is_err());
        assert!(bound.selected_document_hints(root, "original.md").is_err());
        assert!(cursor.read_window(8, 0).is_err());
        assert!(publish_navigation_overlay(root, &old).is_err());
        let new = complete_overlay(root)?;
        assert_ne!(new.documents[0].document_id, old.documents[0].document_id);
        assert_ne!(new.documents[0].text_sha256, old.documents[0].text_sha256);
        publish_navigation_overlay(root, &new)?;
        assert!(
            read_navigation_overlay(root)?
                .unwrap()
                .selected_document_hints(root, "relocated.md")?
                .is_some()
        );
        Ok(())
    }

    #[tokio::test]
    async fn navigation_corrupt_artifact_and_raw_window_digests_fail_at_the_right_boundary()
    -> Result<()> {
        let directory =
            fixture(&[("notes.txt", "A sufficiently long synthetic source.\n")]).await?;
        let root = directory.path();
        let mut overlay = complete_overlay(root)?;
        let published = publish_navigation_overlay(root, &overlay)?;
        let path = artifact_path(root, &published.artifact_sha256)?;
        OpenOptions::new()
            .append(true)
            .open(&path)?
            .write_all(b" ")?;
        assert!(read_navigation_overlay(root).is_err());
        overlay.documents[0].windows[0].sha256 = digest(b"wrong-window");
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        replace_artifact(root, &overlay)?;
        let bound = read_navigation_overlay(root)?.unwrap();
        assert!(bound.selected_document_hints(root, "notes.txt").is_err());
        Ok(())
    }

    #[tokio::test]
    async fn navigation_partial_source_coverage_cannot_be_published_or_mislabelled() -> Result<()> {
        let directory = fixture(&[
            (
                "a.txt",
                "First complete source is longer than one window.\n",
            ),
            ("b.txt", "Second complete source.\n"),
        ])
        .await?;
        let root = directory.path();
        let mut overlay = complete_overlay(root)?;
        overlay.documents.pop();
        recount(root, &mut overlay)?;
        assert!(overlay.coverage.partial_source_coverage);
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        overlay.coverage.partial_source_coverage = false;
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        let mut overlay = complete_overlay(root)?;
        overlay.documents[0].windows.truncate(1);
        recount(root, &mut overlay)?;
        assert!(overlay.coverage.partial_source_coverage);
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        assert!(read_navigation_overlay(root)?.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn navigation_full_traversal_with_zero_hints_and_overlap_has_honest_counts() -> Result<()>
    {
        let directory = fixture(&[
            (
                "notes.txt",
                "Complete synthetic bytes with zero model-accepted hints.\n",
            ),
            ("empty.txt", ""),
        ])
        .await?;
        let root = directory.path();
        let mut overlay = complete_overlay(root)?;
        for document in &mut overlay.documents {
            document.hints.clear();
        }
        let document = overlay
            .documents
            .iter_mut()
            .find(|document| document.path == "notes.txt")
            .unwrap();
        document
            .windows
            .push(read_document_window(root, "notes.txt", 5, 2)?.anchor("overlap"));
        recount(root, &mut overlay)?;
        assert_eq!(
            overlay.coverage.covered_text_bytes,
            overlay.coverage.canonical_text_bytes
        );
        assert!(!overlay.coverage.partial_source_coverage);
        assert!(overlay.coverage.partial_hint_coverage);
        assert_eq!(overlay.coverage.hints, 0);
        publish_navigation_overlay(root, &overlay)?;
        assert!(
            read_navigation_overlay(root)?
                .unwrap()
                .selected_document_hints(root, "notes.txt")?
                .unwrap()
                .hints
                .is_empty()
        );
        Ok(())
    }

    #[tokio::test]
    async fn navigation_node_targets_require_existing_ids_and_contained_windows() -> Result<()> {
        let directory =
            fixture(&[("notes.md", "# First\nContent A.\n# Second\nContent B.\n")]).await?;
        let root = directory.path();
        let mut overlay = complete_overlay(root)?;
        let document_id = overlay.documents[0].document_id.clone();
        overlay.documents[0].hints[0].target = NavigationHintTarget::Node {
            node_id: format!("{document_id}:unknown"),
        };
        recount(root, &mut overlay)?;
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        let (snapshot, _) = open_base(root)?;
        let document = &snapshot.manifest.documents[0];
        let view = TextView::new(snapshot.text(document)?);
        let node = document
            .nodes
            .iter()
            .find(|node| view.node_bytes(node).is_some_and(|(start, _)| start > 0))
            .unwrap();
        overlay.documents[0].hints[0].target = NavigationHintTarget::Node {
            node_id: format!("{}:{}", document.id, node.id),
        };
        recount(root, &mut overlay)?;
        assert!(publish_navigation_overlay(root, &overlay).is_err());
        let (start, end) = view.node_bytes(node).unwrap();
        let anchor =
            read_document_window(root, "notes.md", end - start, start)?.anchor("contained-node");
        overlay.documents[0].windows.push(anchor);
        overlay.documents[0].hints[0].anchor_ids = vec!["contained-node".into()];
        recount(root, &mut overlay)?;
        publish_navigation_overlay(root, &overlay)?;
        Ok(())
    }

    #[tokio::test]
    async fn navigation_size_caps_unknown_anchors_and_producer_identity_are_enforced() -> Result<()>
    {
        let directory = fixture(&[("notes.txt", "Small full source.\n")]).await?;
        let root = directory.path();
        let original = complete_overlay(root)?;
        let mut changed = original.clone();
        changed.documents[0].hints[0].hint = "x".repeat(MAX_NAVIGATION_HINT_BYTES + 1);
        assert!(publish_navigation_overlay(root, &changed).is_err());
        let mut changed = original.clone();
        changed.documents[0].hints[0].anchor_ids = vec!["unissued".into()];
        assert!(publish_navigation_overlay(root, &changed).is_err());
        let mut changed = original.clone();
        changed.producer.schema_sha256 = "missing".into();
        assert!(publish_navigation_overlay(root, &changed).is_err());
        let mut changed = original;
        let mut hint = changed.documents[0].hints[0].clone();
        hint.target = NavigationHintTarget::Document;
        hint.hint = "x".repeat(MAX_NAVIGATION_HINT_BYTES);
        changed.documents[0].hints = vec![hint; 1100];
        recount(root, &mut changed)?;
        assert!(
            format!(
                "{:#}",
                publish_navigation_overlay(root, &changed).unwrap_err()
            )
            .contains("artifact byte limit")
        );
        assert!(read_navigation_overlay(root)?.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn navigation_failure_before_pointer_retains_old_or_absent_active_overlay() -> Result<()>
    {
        for existing in [false, true] {
            let directory = fixture(&[("notes.txt", "Whole synthetic source.\n")]).await?;
            let root = directory.path();
            let mut overlay = complete_overlay(root)?;
            let prior = if existing {
                Some(publish_navigation_overlay(root, &overlay)?)
            } else {
                None
            };
            let current = fs::read(root.join(STATE).join("CURRENT.json"))?;
            overlay.producer.prompt_sha256 = digest(b"different-prompt-identity");
            let error = publish(root, &overlay, || bail!("injected failure before pointer"));
            assert!(error.is_err());
            assert_eq!(
                read_navigation_overlay(root)?.map(|bound| bound.publication().clone()),
                prior
            );
            assert_eq!(fs::read(root.join(STATE).join("CURRENT.json"))?, current);
            assert_eq!(
                fs::read_dir(root.join(STATE).join(ARTIFACT_DIRECTORY))?.count(),
                if existing { 2 } else { 1 }
            );
        }
        Ok(())
    }

    #[tokio::test]
    async fn navigation_hint_controls_are_rejected_without_normalizing_source_windows() -> Result<()>
    {
        let source = "First raw line\r\nSecond\tcell\u{1b}\u{7f}\n";
        let directory = fixture(&[("controls.txt", source)]).await?;
        let root = directory.path();
        let original = complete_overlay(root)?;
        let publication = publish_navigation_overlay(root, &original)?;
        let window = read_document_window(root, "controls.txt", 65_536, 0)?;
        assert_eq!(window.text.as_bytes(), source.as_bytes());
        assert_eq!(window.sha256, digest(source.as_bytes()));
        for control in ['\n', '\r', '\t', '\u{1b}', '\u{7f}', '\0'] {
            let mut changed = original.clone();
            changed.documents[0].hints[0].hint = format!("First{control}second");
            let error = publish_navigation_overlay(root, &changed).unwrap_err();
            assert!(error.to_string().contains("control characters"));
            assert_eq!(
                read_navigation_overlay(root)?.unwrap().publication(),
                &publication
            );
        }
        assert_eq!(
            read_document_window(root, "controls.txt", 65_536, 0)?
                .text
                .as_bytes(),
            source.as_bytes()
        );
        Ok(())
    }

    #[tokio::test]
    async fn navigation_fields_cannot_grant_citations_and_symlinks_cannot_redirect_artifacts()
    -> Result<()> {
        let directory = fixture(&[("notes.txt", "Only source evidence may be cited.\n")]).await?;
        let root = directory.path();
        let overlay = complete_overlay(root)?;
        for field in [
            "citation",
            "citation_id",
            "evidence_token",
            "source_fresh",
            "score",
        ] {
            let mut value = serde_json::to_value(&overlay)?;
            value["documents"][0]["hints"][0][field] = json!("forged");
            assert!(serde_json::from_value::<NavigationOverlay>(value).is_err());
        }
        let window = serde_json::to_value(read_document_window(root, "notes.txt", 16, 0)?)?;
        for field in [
            "citation",
            "citation_id",
            "evidence_token",
            "source_fresh",
            "score",
        ] {
            assert!(window.get(field).is_none());
        }
        #[cfg(unix)]
        {
            let outside = tempfile::tempdir()?;
            std::os::unix::fs::symlink(outside.path(), root.join(STATE).join(ARTIFACT_DIRECTORY))?;
            assert!(publish_navigation_overlay(root, &overlay).is_err());
            assert_eq!(fs::read_dir(outside.path())?.count(), 0);
        }
        Ok(())
    }
}
