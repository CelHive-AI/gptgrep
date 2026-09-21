//! Source-bound document snapshots and vectorless retrieval orchestration.
use anyhow::{Context, Result, bail, ensure};
use fs2::FileExt;
use gptgrep_jev::{Candidate, JevClient};
use gptgrep_pageindex::TreeNode;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const SCHEMA: &str = "gptgrep.v1";
const STATE: &str = ".gptgrep";
const MAX_SOURCE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_MANIFEST_BYTES: u64 = 128 * 1024 * 1024;
const MAX_TEXT_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PageSpan {
    pub number: u32,
    pub line_start: usize,
    pub line_end: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Document {
    pub id: String,
    pub path: String,
    pub source_sha256: String,
    pub source_bytes: u64,
    pub text_sha256: String,
    pub parser: String,
    pub title: String,
    pub pages: Vec<PageSpan>,
    pub nodes: Vec<TreeNode>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub schema_version: String,
    pub generation: String,
    pub root: PathBuf,
    pub created_unix_ms: u128,
    pub documents: Vec<Document>,
    pub excluded_files: usize,
    #[serde(default)]
    pub tree_profile: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct Pointer {
    schema_version: String,
    generation: String,
    manifest_sha256: String,
}

#[derive(Debug, Serialize)]
pub struct IndexReport {
    pub schema_version: String,
    pub root: PathBuf,
    pub generation: String,
    pub indexed_files: usize,
    pub excluded_files: usize,
    pub source_bytes: u64,
    pub elapsed_ms: u128,
    pub warnings: Vec<String>,
    pub tree_profile: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Hit {
    pub path: String,
    pub node_id: String,
    pub title: String,
    pub line_start: usize,
    pub line_end: usize,
    pub page_start: u32,
    pub page_end: u32,
    pub match_line: Option<usize>,
    /// One-based UTF-8 byte column of the match within its line.
    pub match_column: Option<usize>,
    /// Exact half-open UTF-8 byte range in canonical text (raw source for plaintext).
    pub byte_start: usize,
    pub byte_end: usize,
    /// Byte offset from this node's canonical start. None when grep context
    /// crosses the matched node's boundaries.
    pub node_offset: Option<usize>,
    /// First unread byte relative to the node, or None at EOF/outside the node.
    pub next_offset: Option<usize>,
    pub column_start: usize,
    pub coordinate_system: String,
    pub text: String,
    pub text_truncated: bool,
    pub score: f64,
    pub confidence: Option<f64>,
    /// A verified exact token query match retained by the hybrid lexical lane.
    pub literal_anchor: bool,
    pub source_sha256: String,
    pub source_fresh: bool,
    pub citation: String,
}

#[derive(Debug, Default, Serialize)]
pub struct Coverage {
    pub indexed_files: usize,
    pub candidate_files: usize,
    pub verified_files: usize,
    pub routed_documents: usize,
    pub reranked_candidates: usize,
    pub filtered_candidates: usize,
    pub retained_literal_anchors: usize,
    pub stale_files: Vec<String>,
    pub truncated: bool,
    pub semantic_scope_complete: bool,
}

#[derive(Debug, Default, Serialize)]
pub struct Metrics {
    pub elapsed_ms: u128,
    pub jev_requests: usize,
    pub jev_models: Vec<String>,
    pub jev_usage: Vec<Value>,
    pub jev_candidate_bytes: usize,
}

#[derive(Debug, Serialize)]
pub struct SearchReport {
    pub schema_version: String,
    pub query: String,
    pub mode: String,
    pub root: PathBuf,
    pub generation: String,
    pub index_used: bool,
    pub source_fresh: Option<bool>,
    pub minimum_relevance_score: Option<f64>,
    pub hits: Vec<Hit>,
    pub coverage: Coverage,
    pub metrics: Metrics,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct SearchOptions {
    pub mode: String,
    pub limit: usize,
    pub context: usize,
    pub case_insensitive: bool,
    pub literal: bool,
    pub max_candidates: usize,
    pub routing_docs: usize,
    pub model: Option<String>,
    pub min_score: f64,
}

impl Default for SearchOptions {
    fn default() -> Self {
        Self {
            mode: "regex".into(),
            limit: 20,
            context: 0,
            case_insensitive: false,
            literal: false,
            max_candidates: 24,
            routing_docs: 32,
            model: None,
            min_score: 0.5,
        }
    }
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn bounded_read(path: &Path, max: u64) -> Result<Vec<u8>> {
    use std::io::Read;
    let f = File::open(path).with_context(|| format!("cannot read {}", path.display()))?;
    ensure!(
        f.metadata()?.len() <= max,
        "file exceeds {max} byte limit: {}",
        path.display()
    );
    let mut bytes = Vec::new();
    f.take(max + 1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 <= max, "file grew beyond read limit");
    Ok(bytes)
}

fn source_path(root: &Path, relative: &str) -> Result<PathBuf> {
    let rel = Path::new(relative);
    ensure!(
        !rel.as_os_str().is_empty() && rel.components().all(|x| matches!(x, Component::Normal(_))),
        "invalid manifest source path"
    );
    let mut full = root.to_path_buf();
    for component in rel.components() {
        full.push(component);
        ensure!(
            !fs::symlink_metadata(&full)?.file_type().is_symlink(),
            "source became a symlink: {relative}"
        );
    }
    ensure!(full.is_file(), "source is not a regular file: {relative}");
    Ok(full)
}

fn denied(path: &Path) -> bool {
    path.components().any(|c| {
        let name = c.as_os_str().to_string_lossy().to_lowercase();
        matches!(
            name.as_str(),
            ".git" | ".gptgrep" | "node_modules" | "target" | ".local" | "auth.json"
        ) || name == ".env"
            || name.starts_with(".env.")
            || name.ends_with(".pem")
            || name.ends_with(".key")
    })
}

fn no_symlink(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) => ensure!(
            !meta.file_type().is_symlink(),
            "index path cannot be a symlink: {}",
            path.display()
        ),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

fn validate_manifest(manifest: &Manifest) -> Result<()> {
    let mut ids = HashSet::new();
    let mut paths = HashSet::new();
    for doc in &manifest.documents {
        ensure!(
            ids.insert(&doc.id) && paths.insert(&doc.path),
            "duplicate index document identity"
        );
        ensure!(
            doc.id == digest(doc.path.as_bytes())[..24],
            "document identity does not match source path"
        );
        ensure!(
            Path::new(&doc.path)
                .components()
                .all(|c| matches!(c, Component::Normal(_))),
            "invalid source path"
        );
        if doc.pages.is_empty() {
            ensure!(
                doc.nodes.is_empty() && doc.parser == "plaintext" && doc.text_sha256 == digest(b""),
                "document has no page coverage"
            );
            continue;
        }
        let mut prior_end = 0;
        for (i, page) in doc.pages.iter().enumerate() {
            ensure!(
                page.number as usize == i + 1
                    && page.line_start >= 1
                    && page.line_end >= page.line_start
                    && page.line_start > prior_end,
                "invalid or overlapping page coordinates for {}",
                doc.path
            );
            prior_end = page.line_end;
        }
        let mut nodes = HashMap::new();
        for node in &doc.nodes {
            ensure!(
                !node.id.is_empty() && nodes.insert(node.id.as_str(), node).is_none(),
                "duplicate or empty tree node ID"
            );
            ensure!(
                node.line_start >= 1
                    && node.line_start <= node.line_end
                    && node.line_end <= prior_end
                    && node.page_start >= 1
                    && node.page_start <= node.page_end
                    && node.page_end as usize <= doc.pages.len(),
                "invalid tree node coordinates for {}",
                doc.path
            );
        }
        ensure!(!nodes.is_empty(), "document tree is empty");
        for node in &doc.nodes {
            if let Some(id) = &node.parent_id {
                let parent = nodes.get(id.as_str()).context("unknown tree parent")?;
                ensure!(
                    parent.level < node.level
                        && parent.line_start <= node.line_start
                        && parent.line_end >= node.line_end
                        && parent.page_start <= node.page_start
                        && parent.page_end >= node.page_end,
                    "invalid tree hierarchy for {}",
                    doc.path
                );
            }
        }
    }
    Ok(())
}

/// Construct an immutable generation and atomically publish only after all parses and indexing succeed.
pub async fn index(root: &Path, max_files: usize) -> Result<IndexReport> {
    index_with_options(root, max_files, false).await
}

pub async fn index_with_options(
    root: &Path,
    max_files: usize,
    optimize_merge: bool,
) -> Result<IndexReport> {
    let started = Instant::now();
    ensure!(
        max_files > 0 && max_files <= 1_000_000,
        "max-files must be 1..1000000"
    );
    let root = root
        .canonicalize()
        .context("document root does not exist")?;
    ensure!(root.is_dir(), "document root must be a directory");
    let state = root.join(STATE);
    if state.exists() {
        ensure!(
            !fs::symlink_metadata(&state)?.file_type().is_symlink(),
            "index directory cannot be a symlink"
        );
    }
    no_symlink(&state.join("generations"))?;
    no_symlink(&state.join("write.lock"))?;
    no_symlink(&state.join("CURRENT.json"))?;
    fs::create_dir_all(state.join("generations"))?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(state.join("write.lock"))?;
    lock.try_lock_exclusive()
        .context("another index writer owns this corpus")?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?;
    let generation = format!("g{}-{}", now.as_nanos(), std::process::id());
    let dir = state.join("generations").join(&generation);
    fs::create_dir(&dir)?;
    fs::create_dir(dir.join("text"))?;
    let mut sources = Vec::new();
    let mut excluded = 0;
    let walk_root = root.clone();
    let walker = ignore::WalkBuilder::new(&root)
        .hidden(true)
        .follow_links(false)
        .require_git(false)
        .filter_entry(move |entry| {
            !denied(
                entry
                    .path()
                    .strip_prefix(&walk_root)
                    .unwrap_or(entry.path()),
            )
        })
        .build();
    for item in walker {
        let entry = item.context("document traversal failed")?;
        if entry.file_type().is_some_and(|t| t.is_file()) {
            if gptgrep_parse::supports_path(entry.path()) {
                ensure!(
                    sources.len() < max_files,
                    "corpus exceeds --max-files {max_files}; existing index is unchanged"
                );
                sources.push(entry.path().to_path_buf());
            } else {
                excluded += 1;
            }
        }
    }
    sources.sort();
    ensure!(
        !sources.is_empty(),
        "no supported documents found (hidden, ignored and secret files are excluded)"
    );
    let mut documents = Vec::with_capacity(sources.len());
    let mut warnings = Vec::new();
    for path in sources {
        let relative = path
            .strip_prefix(&root)?
            .to_str()
            .context("non-UTF-8 document path is unsupported")?
            .to_owned();
        let safe_path = source_path(&root, &relative)?;
        let bytes = bounded_read(&safe_path, MAX_SOURCE_BYTES)?;
        let source_sha256 = digest(&bytes);
        let mut parsed = gptgrep_parse::parse_path(&safe_path)
            .await
            .with_context(|| format!("parse failed for {relative}; existing index is unchanged"))?;
        if optimize_merge && parsed.parser != "plaintext" {
            parsed = gptgrep_pageindex::optimize_merge(&parsed)?;
        }
        ensure!(
            digest(&bounded_read(
                &source_path(&root, &relative)?,
                MAX_SOURCE_BYTES
            )?) == source_sha256,
            "source changed while parsing: {relative}; retry indexing"
        );
        ensure!(
            parsed.text.len() as u64 <= MAX_TEXT_BYTES,
            "extracted text exceeds document limit: {relative}"
        );
        ensure!(
            parsed.nodes.len() <= 100_000,
            "document tree exceeds 100000 nodes; split the corpus document"
        );
        let id = digest(relative.as_bytes())[..24].to_owned();
        fs::write(dir.join("text").join(format!("{id}.txt")), &parsed.text)?;
        warnings.extend(parsed.warnings.iter().map(|w| format!("{relative}: {w}")));
        documents.push(Document {
            id,
            path: relative,
            source_sha256,
            source_bytes: bytes.len() as u64,
            text_sha256: digest(parsed.text.as_bytes()),
            parser: parsed.parser,
            title: parsed.title,
            pages: parsed
                .pages
                .into_iter()
                .map(|p| PageSpan {
                    number: p.number,
                    line_start: p.line_start,
                    line_end: p.line_end,
                })
                .collect(),
            nodes: parsed.nodes,
            warnings: parsed.warnings,
        });
    }
    let manifest = Manifest {
        schema_version: SCHEMA.into(),
        generation: generation.clone(),
        root: root.clone(),
        created_unix_ms: now.as_millis(),
        documents,
        excluded_files: excluded,
        tree_profile: if optimize_merge {
            "liteparse-layout-v1+flash-merge-v1"
        } else {
            "liteparse-layout-v1"
        }
        .into(),
    };
    let manifest_bytes = serde_json::to_vec_pretty(&manifest)?;
    ensure!(
        manifest_bytes.len() as u64 <= MAX_MANIFEST_BYTES,
        "manifest exceeds supported size; current generation remains unchanged"
    );
    validate_manifest(&manifest)?;
    fs::write(dir.join("manifest.json"), &manifest_bytes)?;
    gptgrep_index::build(&dir.join("text"), &dir.join("trigram"))?;
    let pointer = Pointer {
        schema_version: SCHEMA.into(),
        generation: generation.clone(),
        manifest_sha256: digest(&manifest_bytes),
    };
    let mut temp = tempfile::NamedTempFile::new_in(&state)?;
    serde_json::to_writer_pretty(&mut temp, &pointer)?;
    temp.write_all(b"\n")?;
    temp.as_file().sync_all()?;
    temp.persist(state.join("CURRENT.json"))
        .context("failed to publish index pointer")?;
    File::open(&state)?.sync_all()?;
    Ok(IndexReport {
        schema_version: SCHEMA.into(),
        root,
        generation,
        indexed_files: manifest.documents.len(),
        excluded_files: excluded,
        source_bytes: manifest.documents.iter().map(|d| d.source_bytes).sum(),
        elapsed_ms: started.elapsed().as_millis(),
        warnings,
        tree_profile: manifest.tree_profile,
    })
}

struct Snapshot {
    manifest: Manifest,
    dir: PathBuf,
}

impl Snapshot {
    fn open(root: &Path) -> Result<Self> {
        let root = root.canonicalize()?;
        let state = root.join(STATE);
        ensure!(
            !fs::symlink_metadata(&state)
                .context("index missing; run gptgrep index ROOT")?
                .file_type()
                .is_symlink(),
            "index directory cannot be a symlink"
        );
        no_symlink(&state.join("CURRENT.json"))?;
        let pointer: Pointer =
            serde_json::from_slice(&bounded_read(&state.join("CURRENT.json"), 4096)?)?;
        ensure!(
            pointer.schema_version == SCHEMA,
            "unsupported index schema; reindex required"
        );
        ensure!(
            !pointer.generation.is_empty()
                && pointer
                    .generation
                    .bytes()
                    .all(|x| x.is_ascii_alphanumeric() || x == b'-'),
            "invalid generation ID"
        );
        let dir = state.join("generations").join(&pointer.generation);
        no_symlink(&state.join("generations"))?;
        no_symlink(&dir)?;
        no_symlink(&dir.join("manifest.json"))?;
        no_symlink(&dir.join("trigram"))?;
        let bytes = bounded_read(&dir.join("manifest.json"), MAX_MANIFEST_BYTES)?;
        ensure!(
            digest(&bytes) == pointer.manifest_sha256,
            "index manifest digest mismatch; reindex required"
        );
        let manifest: Manifest = serde_json::from_slice(&bytes)?;
        ensure!(
            manifest.schema_version == SCHEMA
                && manifest.root == root
                && manifest.generation == pointer.generation,
            "index identity mismatch; reindex at current root"
        );
        ensure!(
            manifest
                .documents
                .iter()
                .all(|d| d.id.len() == 24 && d.id.bytes().all(|b| b.is_ascii_hexdigit())),
            "invalid document IDs"
        );
        validate_manifest(&manifest)?;
        Ok(Self { manifest, dir })
    }
    fn text(&self, doc: &Document) -> Result<String> {
        no_symlink(&self.dir.join("text"))?;
        no_symlink(&self.dir.join("text").join(format!("{}.txt", doc.id)))?;
        let bytes = bounded_read(
            &self.dir.join("text").join(format!("{}.txt", doc.id)),
            MAX_TEXT_BYTES,
        )?;
        ensure!(
            digest(&bytes) == doc.text_sha256,
            "indexed text digest mismatch for {}; reindex",
            doc.path
        );
        let text = String::from_utf8(bytes).context("indexed text is not UTF-8")?;
        ensure!(
            (text.is_empty() && doc.pages.is_empty())
                || doc
                    .pages
                    .last()
                    .is_some_and(|p| p.line_end == text.lines().count().max(1)),
            "text and tree line coverage mismatch"
        );
        Ok(text)
    }
    fn fresh(&self, doc: &Document) -> bool {
        source_path(&self.manifest.root, &doc.path)
            .and_then(|p| bounded_read(&p, MAX_SOURCE_BYTES))
            .is_ok_and(|bytes| digest(&bytes) == doc.source_sha256)
    }
}

pub fn status(root: &Path) -> Result<Value> {
    let snapshot = Snapshot::open(root)?;
    let stale: Vec<_> = snapshot
        .manifest
        .documents
        .iter()
        .filter(|d| !snapshot.fresh(d))
        .map(|d| d.path.clone())
        .collect();
    Ok(
        json!({"schema_version":SCHEMA,"root":snapshot.manifest.root,"generation":snapshot.manifest.generation,
        "indexed_files":snapshot.manifest.documents.len(),"excluded_files":snapshot.manifest.excluded_files,
        "stale_files":stale,"source_fresh":stale.is_empty(),
        "scope":"existing indexed documents; rerun index to discover additions", "created_unix_ms":snapshot.manifest.created_unix_ms}),
    )
}

pub fn tree(root: &Path, path: &Path) -> Result<Value> {
    let snapshot = Snapshot::open(root)?;
    let input = if path.is_absolute() {
        path.strip_prefix(&snapshot.manifest.root)?.to_path_buf()
    } else {
        path.to_path_buf()
    };
    let doc = snapshot
        .manifest
        .documents
        .iter()
        .find(|d| Path::new(&d.path) == input)
        .context("document not present in current index")?;
    ensure!(
        snapshot.fresh(doc),
        "source changed or disappeared; reindex before reading tree"
    );
    Ok(
        json!({"schema_version":SCHEMA,"generation":snapshot.manifest.generation,"source_fresh":true,"document":doc}),
    )
}

pub fn read_node(root: &Path, node_id: &str, max_bytes: usize) -> Result<Hit> {
    read_node_window(root, node_id, max_bytes, 0)
}

/// Read a bounded, exact UTF-8 window of a node's canonical text interval.
///
/// Offsets are bytes relative to the node's first source line. The interval
/// includes its line terminators, so successive windows concatenate without
/// losing LF or CRLF bytes. An offset at the interval's end returns an empty
/// EOF window; offsets past it or inside a UTF-8 scalar are errors. A budget
/// too small for the next scalar also errors, preventing stalled continuation.
pub fn read_node_window(
    root: &Path,
    node_id: &str,
    max_bytes: usize,
    offset_bytes: usize,
) -> Result<Hit> {
    ensure!(
        (1..=65536).contains(&max_bytes),
        "max-bytes must be 1..65536"
    );
    let (doc_id, local_id) = node_id
        .split_once(':')
        .context("node ID must be document_id:node_id from search/tree")?;
    let snapshot = Snapshot::open(root)?;
    let doc = snapshot
        .manifest
        .documents
        .iter()
        .find(|d| d.id == doc_id)
        .context("unknown document ID")?;
    let node = doc
        .nodes
        .iter()
        .find(|n| n.id == local_id)
        .context("unknown tree node ID")?;
    ensure!(
        snapshot.fresh(doc),
        "source changed or disappeared; reindex before reading evidence"
    );
    let view = TextView::new(snapshot.text(doc)?);
    let (node_start, node_end) = view
        .node_bytes(node)
        .context("tree node range lies outside canonical text")?;
    let node_length = node_end - node_start;
    ensure!(
        offset_bytes <= node_length,
        "offset-bytes {offset_bytes} exceeds node length {node_length}"
    );
    let byte_start = node_start + offset_bytes;
    ensure!(
        view.text.is_char_boundary(byte_start),
        "offset-bytes {offset_bytes} is not a UTF-8 boundary"
    );
    let remaining = &view.text[byte_start..node_end];
    let (bounded, _) = truncate(remaining, max_bytes);
    if !remaining.is_empty() && bounded.is_empty() {
        bail!(
            "max-bytes {max_bytes} cannot hold the next UTF-8 character (requires {} bytes)",
            remaining.chars().next().expect("nonempty text").len_utf8()
        );
    }
    let byte_end = byte_start + bounded.len();
    let result = hit_from_bytes(
        doc,
        node,
        &view,
        ByteWindow {
            start: byte_start,
            end: byte_end,
            truncated: byte_start > node_start || byte_end < node_end,
        },
        None,
        1.0,
    );
    ensure!(
        snapshot.fresh(doc),
        "source changed or disappeared; reindex before reading evidence"
    );
    Ok(result)
}

fn truncate(text: &str, max: usize) -> (&str, bool) {
    if text.len() <= max {
        return (text, false);
    }
    let mut end = max;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    (&text[..end], true)
}

struct TextView {
    text: String,
    lines: Vec<(usize, usize)>,
}

impl TextView {
    fn new(text: String) -> Self {
        let mut offset = 0;
        let mut lines = Vec::new();
        for line in text.split_inclusive('\n') {
            let content = if let Some(without_lf) = line.strip_suffix('\n') {
                without_lf.strip_suffix('\r').unwrap_or(without_lf)
            } else {
                line
            };
            let length = content.len();
            lines.push((offset, offset + length));
            offset += line.len();
        }
        if lines.is_empty() {
            lines.push((0, 0));
        }
        Self { text, lines }
    }

    /// Full line intervals include terminators, including the last newline.
    fn node_bytes(&self, node: &TreeNode) -> Option<(usize, usize)> {
        if node.line_end < node.line_start {
            return None;
        }
        let start = self.lines.get(node.line_start.checked_sub(1)?)?.0;
        self.lines.get(node.line_end.checked_sub(1)?)?;
        let end = self
            .lines
            .get(node.line_end)
            .map_or(self.text.len(), |line| line.0);
        Some((start, end))
    }
}

struct ByteWindow {
    start: usize,
    end: usize,
    truncated: bool,
}

#[allow(clippy::too_many_arguments)]
fn hit(
    doc: &Document,
    node: &TreeNode,
    view: &TextView,
    start: usize,
    end: usize,
    matched: Option<(usize, usize)>,
    score: f64,
    max_bytes: usize,
) -> Hit {
    let text = &view.text;
    let lines = &view.lines;
    let start = start.max(1).min(lines.len());
    let end = end.max(start).min(lines.len());
    let mut byte_start = lines[start - 1].0;
    let initial_start = byte_start;
    let wanted_end = lines[end - 1].1;
    if let Some((line, column)) = matched {
        let anchor = lines[line - 1].0 + column;
        if anchor >= byte_start.saturating_add(max_bytes.saturating_sub(64)) {
            byte_start = anchor.saturating_sub(max_bytes / 4).max(lines[line - 1].0);
            while !text.is_char_boundary(byte_start) {
                byte_start += 1;
            }
        }
    }
    let (bounded, cut_end) = truncate(&text[byte_start..wanted_end], max_bytes);
    let mut bounded = bounded;
    while let Some(without_lf) = bounded.strip_suffix('\n') {
        bounded = without_lf.strip_suffix('\r').unwrap_or(without_lf);
    }
    let byte_end = byte_start + bounded.len();
    hit_from_bytes(
        doc,
        node,
        view,
        ByteWindow {
            start: byte_start,
            end: byte_end,
            truncated: cut_end || byte_start > initial_start || byte_end < wanted_end,
        },
        matched,
        score,
    )
}

/// Shared citation/coordinate construction for search snippets and exact reads.
/// Search retains its existing -C context and trailing-newline presentation;
/// windows pass their exact unmodified byte interval.
fn hit_from_bytes(
    doc: &Document,
    node: &TreeNode,
    view: &TextView,
    window: ByteWindow,
    matched: Option<(usize, usize)>,
    score: f64,
) -> Hit {
    let text = &view.text;
    let lines = &view.lines;
    let byte_start = window.start;
    let byte_end = window.end;
    let node_bounds = view.node_bytes(node);
    let start = lines
        .partition_point(|(offset, _)| *offset <= byte_start)
        .max(1);
    let end = lines
        .partition_point(|(offset, _)| *offset < byte_end)
        .max(start);
    // A zero-length node EOF may also be the next section's first byte. Its
    // citation remains on this node's final line, not the following section.
    let (start, end) = if byte_start == byte_end
        && node_bounds.is_some_and(|(_, node_end)| byte_end == node_end)
    {
        (node.line_end, node.line_end)
    } else {
        (start, end)
    };
    let contained = node_bounds.filter(|(start, end)| *start <= byte_start && byte_end <= *end);
    let node_offset = contained.map(|(start, _)| byte_start - start);
    let next_offset =
        contained.and_then(|(start, end)| (byte_end < end).then_some(byte_end - start));
    let page_start = doc
        .pages
        .iter()
        .find(|p| p.line_start <= start && p.line_end >= start)
        .map_or(node.page_start, |p| p.number);
    let page_end = doc
        .pages
        .iter()
        .find(|p| p.line_start <= end && p.line_end >= end)
        .map_or(node.page_end, |p| p.number);
    let plaintext = doc.parser == "plaintext";
    let citation = if plaintext {
        format!("{}:L{}-L{}", doc.path, start, end)
    } else {
        format!("{}:p{}-p{}", doc.path, page_start, page_end)
    };
    Hit {
        path: doc.path.clone(),
        node_id: format!("{}:{}", doc.id, node.id),
        title: node.title.clone(),
        line_start: start,
        line_end: end,
        page_start,
        page_end,
        match_line: matched.map(|m| m.0),
        match_column: matched.map(|m| m.1 + 1),
        byte_start,
        byte_end,
        node_offset,
        next_offset,
        column_start: byte_start - lines[start - 1].0 + 1,
        coordinate_system: if plaintext {
            "source_lines"
        } else {
            "extracted_lines_and_source_pages"
        }
        .into(),
        text: text[byte_start..byte_end].into(),
        text_truncated: window.truncated,
        score,
        confidence: None,
        literal_anchor: false,
        source_sha256: doc.source_sha256.clone(),
        source_fresh: true,
        citation,
    }
}

fn node_at(doc: &Document, line: usize) -> Option<&TreeNode> {
    doc.nodes
        .iter()
        .filter(|n| n.line_start <= line && n.line_end >= line)
        .max_by_key(|n| {
            (
                n.level,
                std::cmp::Reverse(n.line_end.saturating_sub(n.line_start)),
            )
        })
}

fn tokens(query: &str) -> Vec<String> {
    let mut seen = HashSet::new();
    query
        .split(|c: char| !c.is_alphanumeric() && c != '_')
        .map(str::to_lowercase)
        .filter(|s| !s.is_empty() && seen.insert(s.clone()))
        .take(16)
        .collect()
}

fn lexical_score(text: &str, terms: &[String]) -> f64 {
    if terms.is_empty() {
        return 0.0;
    }
    let lower = text.to_lowercase();
    terms.iter().filter(|t| lower.contains(t.as_str())).count() as f64 / terms.len() as f64
}

fn literal_anchor(query: &str, line: &str) -> bool {
    let query = query.trim().to_lowercase();
    if query.is_empty() || query.chars().any(|c| !c.is_alphanumeric() && c != '_') {
        return false;
    }
    line.split(|c: char| !c.is_alphanumeric() && c != '_')
        .any(|word| word.to_lowercase() == query)
}

pub async fn search(root: &Path, query: &str, options: &SearchOptions) -> Result<SearchReport> {
    let started = Instant::now();
    ensure!(
        matches!(
            options.mode.as_str(),
            "regex" | "lexical" | "hybrid" | "semantic"
        ),
        "unsupported search mode"
    );
    ensure!(
        !query.is_empty() && query.len() <= 8192,
        "query must contain 1..8192 bytes"
    );
    ensure!((1..=1000).contains(&options.limit), "limit must be 1..1000");
    ensure!(options.context <= 100, "context must be <=100");
    ensure!(
        (1..=24).contains(&options.max_candidates),
        "max-candidates must be 1..24"
    );
    ensure!(
        (1..=32).contains(&options.routing_docs),
        "routing-docs must be 1..32"
    );
    let model_assisted = matches!(options.mode.as_str(), "hybrid" | "semantic");
    ensure!(
        options.min_score.is_finite() && (0.0..=1.0).contains(&options.min_score),
        "min-score must be a finite value in 0..1"
    );
    let client = if model_assisted {
        Some(JevClient::from_env(options.model.as_deref())?)
    } else {
        None
    };
    let snapshot = Snapshot::open(root)?;
    let docs: HashMap<_, _> = snapshot
        .manifest
        .documents
        .iter()
        .map(|d| (d.id.as_str(), d))
        .collect();
    let mut coverage = Coverage {
        indexed_files: docs.len(),
        ..Default::default()
    };
    let mut metrics = Metrics::default();
    let mut warnings = Vec::new();
    let mut freshness = HashMap::new();
    let mut texts = HashMap::new();
    let mut hits = Vec::new();
    let terms = tokens(query);
    if options.mode != "semantic" {
        let pattern = if options.mode == "regex" {
            query.to_owned()
        } else {
            ensure!(
                !terms.is_empty(),
                "lexical/hybrid query must contain a word"
            );
            terms
                .iter()
                .map(|x| regex::escape(x))
                .collect::<Vec<_>>()
                .join("|")
        };
        let verification_pattern = if options.literal && options.mode == "regex" {
            regex::escape(&pattern)
        } else {
            pattern.clone()
        };
        let verifier = regex::RegexBuilder::new(&verification_pattern)
            .case_insensitive(options.case_insensitive || options.mode != "regex")
            .build()?;
        let matches = gptgrep_index::search(
            &snapshot.dir.join("trigram"),
            &pattern,
            options.case_insensitive || options.mode != "regex",
            options.literal && options.mode == "regex",
            if options.mode == "regex" {
                options.limit + 1
            } else {
                10_000
            },
        )?;
        coverage.candidate_files = matches.candidate_files;
        coverage.verified_files = matches.verified_files;
        coverage.truncated = matches.truncated;
        let mut nodes_seen = HashSet::new();
        for m in matches.hits {
            let id = m
                .path
                .file_stem()
                .and_then(|s| s.to_str())
                .context("invalid trigram document path")?;
            let doc = *docs
                .get(id)
                .context("trigram document missing from manifest")?;
            let fresh = *freshness
                .entry(doc.id.clone())
                .or_insert_with(|| snapshot.fresh(doc));
            if !fresh {
                continue;
            }
            if options.mode == "regex" && hits.len() >= options.limit {
                coverage.truncated = true;
                break;
            }
            if !texts.contains_key(&doc.id) {
                texts.insert(doc.id.clone(), TextView::new(snapshot.text(doc)?));
            }
            let text = &texts[&doc.id];
            let Some(node) = node_at(doc, m.line_number) else {
                bail!("tree does not cover indexed match");
            };
            if options.mode != "regex" && !nodes_seen.insert(format!("{}:{}", doc.id, node.id)) {
                continue;
            }
            let (start, end) = if options.mode == "regex" {
                (
                    m.line_number.saturating_sub(options.context).max(1),
                    m.line_number.saturating_add(options.context),
                )
            } else {
                (
                    m.line_number.saturating_sub(4).max(node.line_start),
                    m.line_number.saturating_add(12).min(node.line_end),
                )
            };
            let mut h = hit(
                doc,
                node,
                text,
                start,
                end,
                Some((
                    m.line_number,
                    verifier
                        .find(&m.line)
                        .context("trigram verification mismatch")?
                        .start(),
                )),
                1.0,
                if model_assisted { 1400 } else { 4096 },
            );
            if options.mode != "regex" {
                h.score = lexical_score(&h.text, &terms);
                h.literal_anchor = literal_anchor(query, &m.line);
            }
            hits.push(h);
        }
    }
    if options.mode != "regex" {
        hits.sort_by(|a, b| {
            b.score
                .total_cmp(&a.score)
                .then(a.path.cmp(&b.path))
                .then(a.line_start.cmp(&b.line_start))
        });
    }
    if let Some(client) = client {
        // Document routing is a semantic lane independent of lexical matches. Coverage is explicit.
        let mut routing = Vec::new();
        for doc in snapshot
            .manifest
            .documents
            .iter()
            .take(options.routing_docs)
        {
            let fresh = *freshness
                .entry(doc.id.clone())
                .or_insert_with(|| snapshot.fresh(doc));
            if !fresh {
                continue;
            }
            if !texts.contains_key(&doc.id) {
                texts.insert(doc.id.clone(), TextView::new(snapshot.text(doc)?));
            }
            let headings = doc
                .nodes
                .iter()
                .take(24)
                .flat_map(|n| {
                    std::iter::once(n.title.as_str()).chain(n.key_items.iter().map(String::as_str))
                })
                .collect::<Vec<_>>()
                .join(" / ");
            let description = format!(
                "Path: {}\nTitle: {}\nSections: {}\nOpening: {}",
                doc.path,
                doc.title,
                truncate(&headings, 400).0,
                truncate(&texts[&doc.id].text, 300).0
            );
            routing.push(Candidate {
                id: doc.id.clone(),
                text: truncate(&description, 900).0.to_owned(),
            });
        }
        coverage.routed_documents = routing.len();
        coverage.semantic_scope_complete = routing.len() == docs.len();
        if !coverage.semantic_scope_complete {
            warnings.push("Semantic routing covers a bounded document prefix; increase --routing-docs up to 32 or search narrower roots. Lexical retrieval covers the indexed corpus.".into());
        }
        if !routing.is_empty() {
            metrics.jev_candidate_bytes += routing.iter().map(|c| c.text.len()).sum::<usize>();
            let ranked = client
                .rerank(query, &routing)
                .await
                .context("Jev document routing failed; no silent local fallback")?;
            metrics.jev_requests += 1;
            metrics.jev_models.push(ranked.model);
            metrics.jev_usage.push(ranked.usage);
            let mut ranks = ranked.rankings;
            ranks.sort_by(|a, b| b.score.total_cmp(&a.score).then(a.id.cmp(&b.id)));
            // Reserve half the final budget for tree candidates, protecting no-keyword recall.
            hits.truncate(options.max_candidates / 2);
            let mut seen: HashSet<_> = hits.iter().map(|h| h.node_id.clone()).collect();
            let selected: Vec<_> = ranks
                .iter()
                .take(8)
                .filter_map(|r| docs.get(r.id.as_str()).copied())
                .collect();
            let max_depth = selected.iter().map(|d| d.nodes.len()).max().unwrap_or(0);
            for offset in 0..max_depth {
                for doc in &selected {
                    let leaves: Vec<_> = doc
                        .nodes
                        .iter()
                        .filter(|n| {
                            !doc.nodes
                                .iter()
                                .any(|c| c.parent_id.as_deref() == Some(&n.id))
                        })
                        .collect();
                    if let Some(node) = leaves.get(offset) {
                        let h = hit(
                            doc,
                            node,
                            &texts[&doc.id],
                            node.line_start,
                            node.line_end,
                            None,
                            0.0,
                            1400,
                        );
                        if seen.insert(h.node_id.clone()) {
                            hits.push(h);
                        }
                    }
                    if hits.len() >= options.max_candidates {
                        break;
                    }
                }
                if hits.len() >= options.max_candidates {
                    break;
                }
            }
            coverage.truncated |=
                selected.iter().map(|d| d.nodes.len()).sum::<usize>() > hits.len();
        }
        coverage.truncated |= hits.len() > options.max_candidates;
        hits.truncate(options.max_candidates);
        if !hits.is_empty() {
            let candidates: Vec<_> = hits
                .iter()
                .enumerate()
                .map(|(i, h)| Candidate {
                    id: format!("c{i}"),
                    text: format!(
                        "Document: {}\nSection: {}\nEvidence: {}",
                        h.path, h.title, h.text
                    ),
                })
                .collect();
            metrics.jev_candidate_bytes += candidates.iter().map(|c| c.text.len()).sum::<usize>();
            let ranked = client
                .rerank(query, &candidates)
                .await
                .context("Jev evidence reranking failed; no silent local fallback")?;
            metrics.jev_requests += 1;
            metrics.jev_models.push(ranked.model);
            metrics.jev_usage.push(ranked.usage);
            coverage.reranked_candidates = candidates.len();
            let scores: HashMap<_, _> = ranked
                .rankings
                .into_iter()
                .map(|r| (r.id.clone(), r))
                .collect();
            for (i, h) in hits.iter_mut().enumerate() {
                let rank = scores
                    .get(&format!("c{i}"))
                    .context("Jev omitted a candidate")?;
                h.score = rank.score;
                h.confidence = rank.confidence;
            }
            let before_filter = hits.len();
            coverage.retained_literal_anchors = hits
                .iter()
                .filter(|h| h.literal_anchor && h.score < options.min_score)
                .count();
            hits.retain(|h| h.literal_anchor || h.score >= options.min_score);
            coverage.filtered_candidates = before_filter - hits.len();
            hits.sort_by(|a, b| {
                b.literal_anchor.cmp(&a.literal_anchor).then_with(|| {
                    b.score
                        .total_cmp(&a.score)
                        .then(a.path.cmp(&b.path))
                        .then(a.line_start.cmp(&b.line_start))
                })
            });
        }
    }
    coverage.stale_files = freshness
        .iter()
        .filter(|(_, fresh)| !**fresh)
        .filter_map(|(id, _)| docs.get(id.as_str()).map(|d| d.path.clone()))
        .collect();
    coverage.stale_files.sort();
    if !coverage.stale_files.is_empty() {
        warnings.push("Stale/deleted source documents were excluded; rerun index before relying on a negative result.".into());
    }
    coverage.truncated |= hits.len() > options.limit;
    hits.truncate(options.limit);
    // Recheck selected originals after potentially slow remote inference.
    let mut final_freshness = HashMap::new();
    hits.retain(|h| {
        let doc = docs
            .values()
            .find(|d| d.path == h.path)
            .expect("hit from manifest");
        if *final_freshness
            .entry(doc.id.as_str())
            .or_insert_with(|| snapshot.fresh(doc))
        {
            true
        } else {
            if !coverage.stale_files.contains(&doc.path) {
                coverage.stale_files.push(doc.path.clone());
            }
            false
        }
    });
    metrics.elapsed_ms = started.elapsed().as_millis();
    Ok(SearchReport {
        schema_version: SCHEMA.into(),
        query: query.into(),
        mode: options.mode.clone(),
        root: snapshot.manifest.root,
        generation: snapshot.manifest.generation,
        index_used: true,
        source_fresh: if hits.is_empty() { None } else { Some(true) },
        minimum_relevance_score: model_assisted.then_some(options.min_score),
        hits,
        coverage,
        metrics,
        warnings,
    })
}

/// Inspect all indexed documents without loading their body text.
pub fn catalog(root: &Path) -> Result<Value> {
    let snapshot = Snapshot::open(root)?;
    let docs: Vec<_> = snapshot.manifest.documents.iter().map(|d| json!({"id":d.id,"path":d.path,"title":d.title,
        "pages":d.pages.len(),"nodes":d.nodes.len(),"parser":d.parser,"source_sha256":d.source_sha256})).collect();
    Ok(json!({"schema_version":SCHEMA,"generation":snapshot.manifest.generation,"documents":docs}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unicode_boundaries() {
        assert_eq!(truncate("文档abc", 4), ("文", true));
    }
    #[test]
    fn secret_paths_denied() {
        for p in ["x/.env", "auth.json", "x/private.key", ".gptgrep/a"] {
            assert!(denied(Path::new(p)));
        }
    }
    #[test]
    fn lexical_is_not_semantic() {
        assert_eq!(
            lexical_score("car speed", &tokens("automobile velocity")),
            0.0
        );
    }
    #[test]
    fn literal_anchors_preserve_exact_atomic_queries_not_substrings_or_questions() {
        assert!(literal_anchor("naïve", "Accent forms: naïve and résumé."));
        assert!(literal_anchor("杭州", "城市：杭州。"));
        assert!(literal_anchor("API", "The api accepts JSON."));
        assert!(!literal_anchor("api", "rapid matching"));
        assert!(!literal_anchor("how to repair", "how to repair a disk"));
    }
    #[tokio::test]
    async fn stale_and_failed_rebuild_preserve_generation() -> Result<()> {
        let dir = tempfile::tempdir()?;
        fs::write(dir.path().join("guide.md"), "# Guide\nalpha searchable\n")?;
        let initial = index(dir.path(), 10).await?;
        let found = search(dir.path(), "alpha", &SearchOptions::default()).await?;
        assert_eq!(found.hits.len(), 1);
        assert_eq!(found.hits[0].line_start, 2);
        assert_eq!(found.hits[0].coordinate_system, "source_lines");
        fs::write(dir.path().join("guide.md"), "# Changed\nbeta\n")?;
        let stale = search(dir.path(), "alpha", &SearchOptions::default()).await?;
        assert!(stale.hits.is_empty());
        assert_eq!(stale.coverage.stale_files, vec!["guide.md"]);
        fs::write(dir.path().join("another.txt"), "other")?;
        assert!(index(dir.path(), 1).await.is_err());
        assert_eq!(
            Snapshot::open(dir.path())?.manifest.generation,
            initial.generation
        );
        Ok(())
    }
    #[tokio::test]
    async fn ignored_and_secret_files_are_not_indexed() -> Result<()> {
        let dir = tempfile::tempdir()?;
        fs::write(dir.path().join("a.txt"), "public")?;
        fs::write(dir.path().join("auth.json"), "secret")?;
        fs::write(dir.path().join(".env"), "secret")?;
        fs::write(dir.path().join("ignored.txt"), "secret")?;
        fs::write(dir.path().join(".gitignore"), "ignored.txt\n")?;
        let r = index(dir.path(), 10).await?;
        assert_eq!(r.indexed_files, 1);
        Ok(())
    }
    #[test]
    fn source_path_rejects_escape() {
        assert!(source_path(Path::new("/tmp"), "../secret").is_err());
    }

    #[tokio::test]
    async fn bounded_snippet_keeps_match_and_exact_source_bytes() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let source = format!("{}\r\n{}\r\nneedle\r\n", "p".repeat(5000), "q".repeat(5000));
        fs::write(dir.path().join("long.txt"), &source)?;
        index(dir.path(), 10).await?;
        let result = search(
            dir.path(),
            "needle",
            &SearchOptions {
                context: 2,
                ..Default::default()
            },
        )
        .await?;
        let h = &result.hits[0];
        assert!(h.text.contains("needle"));
        assert_eq!(h.line_start, 3);
        assert_eq!(&source[h.byte_start..h.byte_end], h.text);
        let long_line = format!("{}needle{}\n", "文".repeat(5000), "后".repeat(1000));
        fs::write(dir.path().join("long.txt"), &long_line)?;
        index(dir.path(), 10).await?;
        let result = search(dir.path(), "needle", &SearchOptions::default()).await?;
        let h = &result.hits[0];
        assert!(h.text.contains("needle"));
        assert!(h.column_start > 1 && h.text_truncated);
        assert_eq!(&long_line[h.byte_start..h.byte_end], h.text);
        let carriage_returns = "\r".repeat(5000);
        fs::write(dir.path().join("long.txt"), &carriage_returns)?;
        index(dir.path(), 10).await?;
        let result = search(dir.path(), "\r{3}$", &SearchOptions::default()).await?;
        let h = &result.hits[0];
        assert!(!h.text.is_empty());
        assert_eq!(&carriage_returns[h.byte_start..h.byte_end], h.text);
        Ok(())
    }

    fn indexed_node_id(root: &Path, file: &str, title: Option<&str>) -> Result<String> {
        let snapshot = Snapshot::open(root)?;
        let document = snapshot
            .manifest
            .documents
            .iter()
            .find(|doc| doc.path == file)
            .context("test document missing")?;
        let node = document
            .nodes
            .iter()
            .find(|node| title.is_none_or(|title| node.title == title))
            .context("test node missing")?;
        Ok(format!("{}:{}", document.id, node.id))
    }

    #[tokio::test]
    async fn continuation_reconstructs_headingless_text_larger_than_64kib() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let source = format!("{}final 文🙂\r\n", "alpha 文🙂\r\n".repeat(9000));
        assert!(source.len() > 65536);
        fs::write(directory.path().join("large.txt"), &source)?;
        index(directory.path(), 10).await?;
        let id = indexed_node_id(directory.path(), "large.txt", None)?;
        let mut reconstructed = String::new();
        let mut offset = 0;
        let mut windows = 0;
        loop {
            let budget = if offset == 0 { 65536 } else { 6144 };
            let window = read_node_window(directory.path(), &id, budget, offset)?;
            assert_eq!(window.node_offset, Some(offset));
            assert!(!window.text.is_empty());
            assert!(window.text.len() <= budget);
            assert_eq!(window.byte_start, offset);
            assert_eq!(window.byte_end, offset + window.text.len());
            assert_eq!(window.text, source[window.byte_start..window.byte_end]);
            assert_eq!(
                window.line_start,
                source[..window.byte_start]
                    .bytes()
                    .filter(|byte| *byte == b'\n')
                    .count()
                    + 1
            );
            let prefix = &source[..window.byte_end];
            let end_line = prefix.bytes().filter(|byte| *byte == b'\n').count()
                + usize::from(!prefix.ends_with('\n'));
            assert_eq!(window.line_end, end_line);
            assert_eq!((window.page_start, window.page_end), (1, 1));
            let replay = read_node_window(directory.path(), &id, window.text.len(), offset)?;
            assert_eq!(replay.text, window.text);
            assert_eq!(
                (replay.byte_start, replay.byte_end),
                (window.byte_start, window.byte_end)
            );
            reconstructed.push_str(&window.text);
            windows += 1;
            match window.next_offset {
                Some(next) => {
                    assert_eq!(next, window.byte_end);
                    assert!(next > offset, "continuation stalled");
                    offset = next;
                }
                None => break,
            }
        }
        assert!(windows > 1);
        assert_eq!(reconstructed, source);
        let eof = read_node_window(directory.path(), &id, 4, source.len())?;
        assert_eq!(eof.node_offset, Some(source.len()));
        assert_eq!(eof.next_offset, None);
        assert!(eof.text.is_empty());
        assert_eq!((eof.byte_start, eof.byte_end), (source.len(), source.len()));
        assert_eq!(eof.line_start, source.lines().count());
        assert_eq!(eof.line_end, source.lines().count());
        Ok(())
    }

    #[tokio::test]
    async fn node_windows_preserve_crlf_and_reject_invalid_utf8_offsets() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let source = "# First\r\n甲🙂\r\n\r\n# Second\r\n终\r\n";
        fs::write(directory.path().join("sections.md"), source)?;
        index(directory.path(), 10).await?;
        let id = indexed_node_id(directory.path(), "sections.md", Some("First"))?;
        let end = source.find("# Second").unwrap();
        let full = read_node(directory.path(), &id, 65536)?;
        assert_eq!(full.text, source[..end]);
        assert_eq!(full.node_offset, Some(0));
        assert_eq!(full.next_offset, None);
        assert!(!full.text_truncated);
        let mut combined = String::new();
        let mut offset = 0;
        loop {
            let window = read_node_window(directory.path(), &id, 4, offset)?;
            combined.push_str(&window.text);
            match window.next_offset {
                Some(next) => {
                    assert!(next > offset);
                    offset = next;
                }
                None => break,
            }
        }
        assert_eq!(combined, source[..end]);
        let cr = source.find("\r\n").unwrap();
        let carriage = read_node_window(directory.path(), &id, 1, cr)?;
        let newline = read_node_window(directory.path(), &id, 1, cr + 1)?;
        assert_eq!(carriage.text, "\r");
        assert_eq!(newline.text, "\n");
        assert_eq!((carriage.line_start, carriage.line_end), (1, 1));
        assert_eq!((newline.line_start, newline.line_end), (1, 1));
        assert_eq!(carriage.column_start, cr + 1);
        assert_eq!(newline.column_start, cr + 2);
        let chinese = source.find('甲').unwrap();
        assert!(
            read_node_window(directory.path(), &id, 4, chinese + 1)
                .unwrap_err()
                .to_string()
                .contains("UTF-8 boundary")
        );
        assert!(
            read_node_window(directory.path(), &id, 2, chinese)
                .unwrap_err()
                .to_string()
                .contains("requires 3 bytes")
        );
        assert!(read_node_window(directory.path(), &id, 4, end + 1).is_err());
        assert!(read_node_window(directory.path(), &id, 4, usize::MAX).is_err());
        assert!(read_node_window(directory.path(), &id, 0, 0).is_err());
        assert!(read_node_window(directory.path(), &id, 65537, 0).is_err());
        let eof = read_node_window(directory.path(), &id, 4, end)?;
        assert!(eof.text.is_empty());
        assert_eq!((eof.line_start, eof.line_end), (3, 3));
        assert_eq!(eof.next_offset, None);
        Ok(())
    }

    #[tokio::test]
    async fn blank_node_reads_return_exact_delimiters_and_explicit_eof() -> Result<()> {
        let directory = tempfile::tempdir()?;
        fs::write(directory.path().join("blank.txt"), "\r\n\r\n")?;
        fs::write(directory.path().join("empty.txt"), "")?;
        index(directory.path(), 10).await?;
        let id = indexed_node_id(directory.path(), "blank.txt", None)?;
        let mut output = String::new();
        for offset in 0..4 {
            let window = read_node_window(directory.path(), &id, 1, offset)?;
            assert_eq!(window.text.len(), 1);
            assert_eq!(window.node_offset, Some(offset));
            assert_eq!(window.next_offset, (offset < 3).then_some(offset + 1));
            output.push_str(&window.text);
        }
        assert_eq!(output, "\r\n\r\n");
        let eof = read_node_window(directory.path(), &id, 1, 4)?;
        assert!(eof.text.is_empty());
        assert_eq!(eof.next_offset, None);
        assert_eq!(eof.line_start, 2);
        let snapshot = Snapshot::open(directory.path())?;
        let empty = snapshot
            .manifest
            .documents
            .iter()
            .find(|doc| doc.path == "empty.txt")
            .unwrap();
        assert!(empty.nodes.is_empty());
        assert!(read_node_window(directory.path(), &format!("{}:0000", empty.id), 4, 0).is_err());
        Ok(())
    }

    #[tokio::test]
    async fn search_cursors_replay_contained_hits_without_clipping_grep_context() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let source = "# One\nfirst\n## Nested\nneedle\n# Two\ntail\n";
        fs::write(directory.path().join("sections.md"), source)?;
        index(directory.path(), 10).await?;
        let contextual = search(
            directory.path(),
            "needle",
            &SearchOptions {
                context: 3,
                ..Default::default()
            },
        )
        .await?;
        let context = &contextual.hits[0];
        assert_eq!((context.line_start, context.line_end), (1, 6));
        assert_eq!(context.text, source.trim_end_matches('\n'));
        assert_eq!(context.node_offset, None);
        assert_eq!(context.next_offset, None);
        let direct = search(directory.path(), "needle", &SearchOptions::default()).await?;
        let hit = &direct.hits[0];
        let node_start = source.find("## Nested").unwrap();
        assert_eq!(hit.node_offset, Some(hit.byte_start - node_start));
        assert_eq!(hit.next_offset, Some(hit.byte_end - node_start));
        let replay = read_node_window(
            directory.path(),
            &hit.node_id,
            hit.byte_end - hit.byte_start,
            hit.node_offset.unwrap(),
        )?;
        assert_eq!(replay.text, hit.text);
        assert_eq!(
            (replay.byte_start, replay.byte_end),
            (hit.byte_start, hit.byte_end)
        );
        assert_eq!(
            (replay.line_start, replay.line_end),
            (hit.line_start, hit.line_end)
        );
        assert_eq!(replay.citation, hit.citation);
        assert_eq!(replay.source_sha256, hit.source_sha256);
        Ok(())
    }

    #[tokio::test]
    async fn continuation_rechecks_changed_and_deleted_sources() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let source = directory.path().join("large.txt");
        fs::write(&source, "content".repeat(10000))?;
        index(directory.path(), 10).await?;
        let id = indexed_node_id(directory.path(), "large.txt", None)?;
        let first = read_node_window(directory.path(), &id, 6144, 0)?;
        let offset = first
            .next_offset
            .context("fixture should need continuation")?;
        fs::write(&source, "changed")?;
        assert!(
            read_node_window(directory.path(), &id, 6144, offset)
                .unwrap_err()
                .to_string()
                .contains("source changed or disappeared")
        );
        fs::remove_file(&source)?;
        assert!(
            read_node_window(directory.path(), &id, 6144, offset)
                .unwrap_err()
                .to_string()
                .contains("source changed or disappeared")
        );
        Ok(())
    }

    #[test]
    fn byte_windows_report_native_physical_pages_and_extracted_lines() -> Result<()> {
        let parsed = gptgrep_pageindex::from_pages(
            "report",
            &[
                gptgrep_pageindex::PageInput {
                    number: 1,
                    text: "第一页".into(),
                },
                gptgrep_pageindex::PageInput {
                    number: 2,
                    text: "Second".into(),
                },
                gptgrep_pageindex::PageInput {
                    number: 3,
                    text: "末页".into(),
                },
            ],
            &[gptgrep_pageindex::Heading {
                title: "第一页".into(),
                level: 1,
                page: 1,
                line: 1,
            }],
            &[],
            "liteparse-test",
        )?;
        let doc = Document {
            id: "test-document".into(),
            path: "report.pdf".into(),
            source_sha256: "test-hash".into(),
            source_bytes: 0,
            text_sha256: digest(parsed.text.as_bytes()),
            parser: parsed.parser,
            title: parsed.title,
            warnings: parsed.warnings,
            nodes: parsed.nodes,
            pages: parsed
                .pages
                .iter()
                .map(|page| PageSpan {
                    number: page.number,
                    line_start: page.line_start,
                    line_end: page.line_end,
                })
                .collect(),
        };
        let view = TextView::new(parsed.text);
        let start = view.lines[1].0;
        let hit = hit_from_bytes(
            &doc,
            &doc.nodes[0],
            &view,
            ByteWindow {
                start,
                end: view.text.len(),
                truncated: true,
            },
            None,
            1.0,
        );
        assert_eq!((hit.line_start, hit.line_end), (2, 3));
        assert_eq!((hit.page_start, hit.page_end), (2, 3));
        assert_eq!(hit.node_offset, Some(start));
        assert_eq!(hit.next_offset, None);
        assert_eq!(hit.coordinate_system, "extracted_lines_and_source_pages");
        assert_eq!(hit.citation, "report.pdf:p2-p3");
        assert_eq!(hit.text, "Second\n末页\n");
        Ok(())
    }

    #[tokio::test]
    async fn empty_plaintext_remains_a_valid_indexed_document() -> Result<()> {
        let dir = tempfile::tempdir()?;
        fs::write(dir.path().join("empty.txt"), "")?;
        assert_eq!(index(dir.path(), 10).await?.indexed_files, 1);
        assert!(
            search(dir.path(), "needle", &SearchOptions::default())
                .await?
                .hits
                .is_empty()
        );
        Ok(())
    }

    #[tokio::test]
    async fn explicitly_selected_hidden_root_can_be_indexed() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let root = dir.path().join(".private-documents");
        fs::create_dir(&root)?;
        fs::write(root.join("public.txt"), "needle")?;
        fs::write(root.join(".env"), "not admitted")?;
        assert_eq!(index(&root, 10).await?.indexed_files, 1);
        assert_eq!(
            search(&root, "needle", &SearchOptions::default())
                .await?
                .hits
                .len(),
            1
        );
        Ok(())
    }

    #[tokio::test]
    async fn damaged_manifest_is_rejected_even_with_updated_digest() -> Result<()> {
        let dir = tempfile::tempdir()?;
        fs::write(dir.path().join("a.txt"), "hello\nworld\n")?;
        index(dir.path(), 10).await?;
        let snapshot = Snapshot::open(dir.path())?;
        let manifest_path = snapshot.dir.join("manifest.json");
        let mut value: Value = serde_json::from_slice(&fs::read(&manifest_path)?)?;
        value["documents"][0]["nodes"][0]["line_end"] = json!(999);
        let bytes = serde_json::to_vec(&value)?;
        fs::write(&manifest_path, &bytes)?;
        let pointer_path = dir.path().join(STATE).join("CURRENT.json");
        let mut pointer: Pointer = serde_json::from_slice(&fs::read(&pointer_path)?)?;
        pointer.manifest_sha256 = digest(&bytes);
        fs::write(pointer_path, serde_json::to_vec(&pointer)?)?;
        assert!(Snapshot::open(dir.path()).is_err());
        Ok(())
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn index_generation_symlink_cannot_redirect_writes() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let outside = tempfile::tempdir()?;
        fs::write(dir.path().join("a.txt"), "hello")?;
        fs::create_dir(dir.path().join(STATE))?;
        std::os::unix::fs::symlink(outside.path(), dir.path().join(STATE).join("generations"))?;
        assert!(index(dir.path(), 10).await.is_err());
        assert!(fs::read_dir(outside.path())?.next().is_none());
        Ok(())
    }
}
