//! Deterministic merge operators ported from PageIndex `tree_optimize.py` at
//! 9c4c3ff2ddd2cbd70501997635578064f2b7c7c3 (MIT; see THIRD_PARTY.md).
//!
//! Source mapping: S/residual/C at lines 231-259; same-span sibling merge and
//! union titles at 469-529; bottom-up, tie-merging collapse at 533-579; preorder
//! relabeling at 207-224; deterministic operator order at 798-810. This module
//! adds validation for GPTgrep's line coordinates and preserves a parent's
//! pre-existing key_items. No model expansion or summary generation runs here.

use crate::{BuildError, ParsedDocument, TreeNode};
use std::collections::{BTreeMap, HashMap};

const ROUTING_COST: u64 = 1;
const TITLE_MAX_CHARS: usize = 200;

struct Entry {
    node: TreeNode,
    children: Vec<usize>,
    alive: bool,
    cost: u64,
}

struct Forest {
    entries: Vec<Entry>,
    roots: Vec<usize>,
}

/// Return a document with redundant tree nodes deterministically merged.
///
/// A leaf's scan cost S is its inclusive page span. An expanded node costs
/// `1 + max(uncovered_parent_pages, child_costs)`. Subtrees collapse when S is
/// less than or equal to that cost, so ties merge. Frontier siblings covering
/// identical page spans coalesce before each bottom-up cost pass. Operators run
/// to a fixed point; every changing pass removes nodes, so termination is finite.
///
/// Text, pages and citation coverage are preserved. Removed titles survive in
/// `key_items`; same-span sibling line ranges are unioned. IDs and parent IDs are
/// rebuilt in preorder, so use the returned document as a new index generation.
/// Invalid source ranges, missing parents or incomplete coverage return errors.
pub fn optimize_merge(document: &ParsedDocument) -> Result<ParsedDocument, BuildError> {
    let mut forest = Forest::read(document)?;
    loop {
        let siblings_changed = forest.merge_same_span();
        let subtrees_changed = forest.merge_subtrees();
        if !siblings_changed && !subtrees_changed {
            break;
        }
    }
    let mut result = document.clone();
    result.nodes = forest.flatten();
    // Validate the public result as well as input; this also checks that unioned
    // line ranges stay within their physical pages and their parent ranges.
    Forest::read(&result)?;
    Ok(result)
}

impl Forest {
    fn read(document: &ParsedDocument) -> Result<Self, BuildError> {
        let line_count = document.text.lines().count();
        let mut next_line = 1;
        for (index, page) in document.pages.iter().enumerate() {
            if page.number as usize != index + 1
                || page.line_start != next_line
                || page.line_end < page.line_start
                || page.line_end > line_count
            {
                return Err(BuildError(
                    "optimizer requires complete ordered page/line ranges".into(),
                ));
            }
            next_line = page.line_end + 1;
        }
        if next_line - 1 != line_count {
            return Err(BuildError(
                "optimizer pages do not cover the document text".into(),
            ));
        }
        let mut entries: Vec<Entry> = Vec::with_capacity(document.nodes.len());
        let mut roots: Vec<usize> = Vec::new();
        let mut ids: HashMap<String, usize> = HashMap::new();
        for node in &document.nodes {
            if ids.contains_key(&node.id) {
                return Err(BuildError(format!("duplicate tree node ID: {}", node.id)));
            }
            let start_page = node
                .page_start
                .checked_sub(1)
                .and_then(|page| document.pages.get(page as usize));
            let end_page = node
                .page_end
                .checked_sub(1)
                .and_then(|page| document.pages.get(page as usize));
            let ranges_valid = start_page.zip(end_page).is_some_and(|(start, end)| {
                node.level > 0
                    && node.page_start <= node.page_end
                    && node.line_start <= node.line_end
                    && (start.line_start..=start.line_end).contains(&node.line_start)
                    && (end.line_start..=end.line_end).contains(&node.line_end)
            });
            if !ranges_valid {
                return Err(BuildError(format!(
                    "invalid page/line range for node {}",
                    node.id
                )));
            }
            let index = entries.len();
            if let Some(parent_id) = &node.parent_id {
                let parent: usize = *ids.get(parent_id).ok_or_else(|| {
                    BuildError(format!(
                        "node {} has an absent, cyclic, or later parent {parent_id}",
                        node.id
                    ))
                })?;
                let ancestor = &entries[parent].node;
                if ancestor.level >= node.level
                    || ancestor.line_start > node.line_start
                    || ancestor.line_end < node.line_end
                    || ancestor.page_start > node.page_start
                    || ancestor.page_end < node.page_end
                {
                    return Err(BuildError(format!(
                        "node {} is outside its parent's range or level",
                        node.id
                    )));
                }
                if let Some(previous) = entries[parent].children.last()
                    && entries[*previous].node.line_start > node.line_start
                {
                    return Err(BuildError("tree siblings are not in source order".into()));
                }
                entries[parent].children.push(index);
            } else {
                if let Some(previous) = roots.last()
                    && entries[*previous].node.line_start > node.line_start
                {
                    return Err(BuildError("tree roots are not in source order".into()));
                }
                roots.push(index);
            }
            ids.insert(node.id.clone(), index);
            entries.push(Entry {
                node: node.clone(),
                children: Vec::new(),
                alive: true,
                cost: 0,
            });
        }
        // Parents contain children; complete root coverage therefore proves
        // every input line (and every physical page) remains reachable.
        let mut covered_end = 0;
        for index in &roots {
            let node = &entries[*index].node;
            if node.line_start > covered_end + 1 {
                return Err(BuildError(
                    "tree leaves a gap in source line coverage".into(),
                ));
            }
            covered_end = covered_end.max(node.line_end);
        }
        if covered_end != line_count {
            return Err(BuildError("tree does not cover the entire document".into()));
        }
        Ok(Self { entries, roots })
    }

    fn span(&self, index: usize) -> u64 {
        let node = &self.entries[index].node;
        u64::from(node.page_end) - u64::from(node.page_start) + 1
    }

    fn residual(&self, index: usize) -> u64 {
        let mut spans: Vec<_> = self.entries[index]
            .children
            .iter()
            .map(|child| {
                let node = &self.entries[*child].node;
                (u64::from(node.page_start), u64::from(node.page_end))
            })
            .collect();
        spans.sort_unstable();
        let mut covered = 0;
        let mut union: Option<(u64, u64)> = None;
        for (start, end) in spans {
            match union {
                Some((left, right)) if start <= right + 1 => union = Some((left, right.max(end))),
                Some((left, right)) => {
                    covered += right - left + 1;
                    union = Some((start, end));
                }
                None => union = Some((start, end)),
            }
        }
        if let Some((start, end)) = union {
            covered += end - start + 1;
        }
        self.span(index) - covered
    }

    fn cost(&self, index: usize) -> u64 {
        let entry = &self.entries[index];
        if entry.children.is_empty() {
            self.span(index)
        } else {
            ROUTING_COST
                + entry
                    .children
                    .iter()
                    .map(|child| self.entries[*child].cost)
                    .chain(std::iter::once(self.residual(index)))
                    .max()
                    .unwrap_or(0)
        }
    }

    fn merge_same_span(&mut self) -> bool {
        let mut changed = false;
        // Parent-before-child input means reverse order is a valid postorder.
        // No recursive Rust calls or recursively owned trees are necessary.
        for index in (0..self.entries.len()).rev() {
            if !self.entries[index].alive {
                continue;
            }
            let mut children = std::mem::take(&mut self.entries[index].children);
            changed |= self.merge_siblings(&mut children);
            self.entries[index].children = children;
        }
        let mut roots = std::mem::take(&mut self.roots);
        changed |= self.merge_siblings(&mut roots);
        self.roots = roots;
        changed
    }

    fn merge_siblings(&mut self, siblings: &mut Vec<usize>) -> bool {
        let mut groups: BTreeMap<(u32, u32), Vec<usize>> = BTreeMap::new();
        for index in siblings.iter().copied() {
            let entry = &self.entries[index];
            if entry.children.is_empty() {
                groups
                    .entry((entry.node.page_start, entry.node.page_end))
                    .or_default()
                    .push(index);
            }
        }
        let mut changed = false;
        for ((start, end), group) in groups {
            if group.len() < 2 {
                continue;
            }
            let keeper = group[0];
            let mut titles = Vec::new();
            let mut line_start = usize::MAX;
            let mut line_end = 0;
            for index in &group {
                let node = &mut self.entries[*index].node;
                titles.push(std::mem::take(&mut node.title));
                titles.append(&mut node.key_items);
                line_start = line_start.min(node.line_start);
                line_end = line_end.max(node.line_end);
            }
            let joined = titles
                .iter()
                .filter(|title| !title.is_empty())
                .cloned()
                .collect::<Vec<_>>()
                .join("; ");
            self.entries[keeper].node.title =
                if joined.is_empty() || joined.chars().count() > TITLE_MAX_CHARS {
                    if start == end {
                        format!("p.{start}")
                    } else {
                        format!("p.{start}-{end}")
                    }
                } else {
                    joined
                };
            self.entries[keeper].node.key_items = titles;
            self.entries[keeper].node.line_start = line_start;
            self.entries[keeper].node.line_end = line_end;
            for index in group.into_iter().skip(1) {
                self.entries[index].alive = false;
            }
            changed = true;
        }
        if changed {
            siblings.retain(|index| self.entries[*index].alive);
        }
        changed
    }

    fn merge_subtrees(&mut self) -> bool {
        let mut changed = false;
        for index in (0..self.entries.len()).rev() {
            if !self.entries[index].alive {
                continue;
            }
            let cost = self.cost(index);
            let span = self.span(index);
            if !self.entries[index].children.is_empty() && span <= cost {
                let children = std::mem::take(&mut self.entries[index].children);
                let mut pending: Vec<_> = children.into_iter().rev().collect();
                // Preserve metadata supplied before optimization as well as the
                // removed subtree; upstream only replaces with removed titles.
                let mut titles = std::mem::take(&mut self.entries[index].node.key_items);
                while let Some(child) = pending.pop() {
                    let entry = &mut self.entries[child];
                    titles.push(std::mem::take(&mut entry.node.title));
                    titles.append(&mut entry.node.key_items);
                    pending.extend(std::mem::take(&mut entry.children).into_iter().rev());
                    entry.alive = false;
                }
                self.entries[index].node.key_items = titles;
                self.entries[index].cost = span;
                changed = true;
            } else {
                self.entries[index].cost = cost;
            }
        }
        changed
    }

    fn flatten(&self) -> Vec<TreeNode> {
        let mut nodes: Vec<TreeNode> = Vec::new();
        let mut pending: Vec<_> = self
            .roots
            .iter()
            .rev()
            .map(|index| (*index, None))
            .collect();
        while let Some((index, parent_id)) = pending.pop() {
            let entry = &self.entries[index];
            let mut node = entry.node.clone();
            node.id = format!("{:04}", nodes.len());
            node.parent_id = parent_id;
            pending.extend(
                entry
                    .children
                    .iter()
                    .rev()
                    .map(|child| (*child, Some(node.id.clone()))),
            );
            nodes.push(node);
        }
        nodes
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Page, from_text};
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct SourceNode {
        title: String,
        node_id: String,
        start_index: u32,
        end_index: u32,
        #[serde(default)]
        nodes: Vec<SourceNode>,
        #[serde(default)]
        key_items: Vec<String>,
    }

    #[derive(Deserialize)]
    struct ExpectedCost {
        node_id: String,
        span: u64,
        residual: u64,
        cost: u64,
        frontier_cost: u64,
    }

    #[derive(Deserialize)]
    struct Fixture {
        name: String,
        page_count: u32,
        input_tree: Vec<SourceNode>,
        costs_before: Vec<ExpectedCost>,
        expected_tree: Vec<SourceNode>,
    }

    #[derive(Deserialize)]
    struct Fixtures {
        source_revision: String,
        source_sha256: String,
        cases: Vec<Fixture>,
    }

    fn fixture_document(tree: &[SourceNode], page_count: u32) -> ParsedDocument {
        fn append(
            nodes: &[SourceNode],
            parent: Option<&str>,
            level: u32,
            output: &mut Vec<TreeNode>,
        ) {
            for node in nodes {
                output.push(TreeNode {
                    id: node.node_id.clone(),
                    parent_id: parent.map(str::to_owned),
                    title: node.title.clone(),
                    level,
                    page_start: node.start_index,
                    page_end: node.end_index,
                    line_start: node.start_index as usize,
                    line_end: node.end_index as usize,
                    key_items: node.key_items.clone(),
                });
                append(&node.nodes, Some(&node.node_id), level + 1, output);
            }
        }
        let mut nodes = Vec::new();
        append(tree, None, 1, &mut nodes);
        let pages: Vec<_> = (1..=page_count)
            .map(|number| Page {
                number,
                text: format!("page {number}"),
                line_start: number as usize,
                line_end: number as usize,
            })
            .collect();
        ParsedDocument {
            title: "fixture".into(),
            text: pages
                .iter()
                .map(|page| format!("{}\n", page.text))
                .collect(),
            pages,
            nodes,
            parser: "optimizer-fixture".into(),
            warnings: Vec::new(),
        }
    }

    // Independent frontier formulation: max(distance from root + scan pages),
    // including a virtual frontier for residual pages under expanded nodes.
    fn frontier_cost(forest: &Forest, root: usize) -> u64 {
        let mut pending = vec![(root, 0)];
        let mut cost = 0;
        while let Some((index, distance)) = pending.pop() {
            let entry = &forest.entries[index];
            if entry.children.is_empty() {
                cost = cost.max(distance + forest.span(index));
            } else {
                let residual = forest.residual(index);
                if residual > 0 {
                    cost = cost.max(distance + ROUTING_COST + residual);
                }
                pending.extend(
                    entry
                        .children
                        .iter()
                        .map(|child| (*child, distance + ROUTING_COST)),
                );
            }
        }
        cost
    }

    #[test]
    fn differential_equations_and_outputs_match_pinned_upstream_fixtures() {
        let fixtures: Fixtures =
            serde_json::from_str(include_str!("../tests/fixtures/pageindex_merge.json")).unwrap();
        assert_eq!(
            fixtures.source_revision,
            "9c4c3ff2ddd2cbd70501997635578064f2b7c7c3"
        );
        assert_eq!(
            fixtures.source_sha256,
            "3112940bf4c4e4add0203316575e1426cf06b375a97e67d415129af7c119061a"
        );
        assert_eq!(fixtures.cases.len(), 11);
        for fixture in fixtures.cases {
            let document = fixture_document(&fixture.input_tree, fixture.page_count);
            let mut forest = Forest::read(&document).unwrap();
            for index in (0..forest.entries.len()).rev() {
                forest.entries[index].cost = forest.cost(index);
            }
            for (index, expected) in fixture.costs_before.iter().enumerate() {
                assert_eq!(
                    forest.entries[index].node.id, expected.node_id,
                    "{}",
                    fixture.name
                );
                assert_eq!(forest.span(index), expected.span, "{} S", fixture.name);
                assert_eq!(
                    forest.residual(index),
                    expected.residual,
                    "{} residual",
                    fixture.name
                );
                assert_eq!(
                    forest.entries[index].cost, expected.cost,
                    "{} C",
                    fixture.name
                );
                assert_eq!(
                    frontier_cost(&forest, index),
                    expected.frontier_cost,
                    "{} frontier",
                    fixture.name
                );
            }
            let expected = fixture_document(&fixture.expected_tree, fixture.page_count);
            let optimized = optimize_merge(&document).unwrap();
            assert_eq!(
                serde_json::to_value(&optimized.nodes).unwrap(),
                serde_json::to_value(&expected.nodes).unwrap(),
                "{} output",
                fixture.name
            );
            assert_eq!(optimized.text, document.text);
            assert_eq!(
                serde_json::to_value(&optimized.pages).unwrap(),
                serde_json::to_value(&document.pages).unwrap()
            );
            assert_eq!(
                serde_json::to_value(optimize_merge(&optimized).unwrap()).unwrap(),
                serde_json::to_value(&optimized).unwrap(),
                "{} idempotence",
                fixture.name
            );
        }
    }

    #[test]
    fn same_page_merge_unions_source_lines_and_retains_original_titles() {
        let source = from_text("notes", "# Alpha\na\n# Beta\nb\n");
        let optimized = optimize_merge(&source).unwrap();
        assert_eq!(optimized.nodes.len(), 1);
        let node = &optimized.nodes[0];
        assert_eq!(node.title, "Alpha; Beta");
        assert_eq!(node.key_items, ["Alpha", "Beta"]);
        assert_eq!((node.line_start, node.line_end), (1, 4));
        assert_eq!((node.page_start, node.page_end), (1, 1));
        assert_eq!(source.nodes.len(), 2, "input was mutated");
    }

    #[test]
    fn collapse_preserves_preexisting_parent_and_child_metadata() {
        let mut source = from_text("notes", "# Parent\n## Child\nbody\n");
        source.nodes[0].key_items.push("Earlier parent item".into());
        source.nodes[1].key_items.push("Earlier child item".into());
        let optimized = optimize_merge(&source).unwrap();
        assert_eq!(optimized.nodes.len(), 1);
        assert_eq!(
            optimized.nodes[0].key_items,
            ["Earlier parent item", "Child", "Earlier child item"]
        );
        assert_eq!(
            (optimized.nodes[0].line_start, optimized.nodes[0].line_end),
            (1, 3)
        );
    }

    #[test]
    fn malformed_tree_and_incomplete_coverage_are_rejected() {
        let source = from_text("notes", "# Parent\n## Child\nbody\n");
        let mut invalid = source.clone();
        invalid.nodes[0].line_end = 1;
        assert!(optimize_merge(&invalid).is_err());
        let mut invalid = source.clone();
        invalid.nodes[1].id = invalid.nodes[0].id.clone();
        assert!(optimize_merge(&invalid).is_err());
        let mut invalid = source.clone();
        invalid.nodes[0].parent_id = Some(invalid.nodes[1].id.clone());
        assert!(optimize_merge(&invalid).is_err());
        let mut invalid = from_text("notes", "one\ntwo\n");
        invalid.nodes[0].line_end = 1;
        assert!(optimize_merge(&invalid).is_err());
        invalid.pages[0].number = 2;
        assert!(optimize_merge(&invalid).is_err());
    }

    #[test]
    fn empty_document_and_legacy_serialized_nodes_are_supported() {
        let empty = from_text("empty", "");
        assert!(optimize_merge(&empty).unwrap().nodes.is_empty());
        let node: TreeNode = serde_json::from_str(r#"{"id":"0000","parent_id":null,"title":"legacy","level":1,"page_start":1,"page_end":1,"line_start":1,"line_end":1}"#).unwrap();
        assert!(node.key_items.is_empty());
    }
}
