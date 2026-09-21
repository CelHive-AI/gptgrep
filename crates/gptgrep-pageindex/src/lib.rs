//! Document trees with explicit line and physical-page provenance.
//!
//! This is a GPTgrep structural implementation inspired by PageIndex's range
//! semantics. LiteParse supplies the PDF layout. It is not a port of the full
//! PageIndex Flash classifier; see THIRD_PARTY.md and README.md.

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fmt;

mod optimizer;
pub use optimizer::optimize_merge;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Page {
    pub number: u32,
    pub text: String,
    pub line_start: usize,
    pub line_end: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TreeNode {
    pub id: String,
    pub parent_id: Option<String>,
    pub title: String,
    pub level: u32,
    pub page_start: u32,
    pub page_end: u32,
    pub line_start: usize,
    pub line_end: usize,
    /// Original headings retained when deterministic optimization removes nodes.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub key_items: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ParsedDocument {
    pub title: String,
    pub text: String,
    pub pages: Vec<Page>,
    pub nodes: Vec<TreeNode>,
    /// `plaintext` means line numbers refer to the original UTF-8 file.
    /// Other parsers use the canonical extracted `text` and physical pages.
    pub parser: String,
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct PageInput {
    /// One-based physical page ordinal. Inputs must be complete and ordered.
    pub number: u32,
    pub text: String,
}

#[derive(Clone, Debug)]
pub struct Heading {
    pub title: String,
    pub level: u32,
    pub page: u32,
    /// One-based line within the corresponding input page.
    pub line: usize,
}

#[derive(Clone, Debug)]
pub struct Bookmark {
    pub title: String,
    pub level: u32,
    /// One-based physical page; convert a parser's zero-based index first.
    pub page: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BuildError(pub String);

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for BuildError {}

#[derive(Clone)]
struct Candidate {
    title: String,
    level: u32,
    line: usize,
    page: u32,
    // Bookmarks without a matching source heading have page-level precision.
    // A preceding section shares that boundary page, avoiding lost evidence.
    page_boundary: bool,
}

/// Parse Markdown headings without changing any source text or line numbers.
/// Code blocks, frontmatter, and headings inside quotations/lists are excluded.
/// Other UTF-8 formats also get a usable whole-file fallback.
pub fn from_text(title: &str, text: &str) -> ParsedDocument {
    if text.is_empty() {
        return ParsedDocument {
            title: title.into(),
            text: String::new(),
            pages: Vec::new(),
            nodes: Vec::new(),
            parser: "plaintext".into(),
            warnings: Vec::new(),
        };
    }
    let page = Page {
        number: 1,
        text: text.into(),
        line_start: 1,
        line_end: text.lines().count().max(1),
    };
    let options = Options::ENABLE_YAML_STYLE_METADATA_BLOCKS
        | Options::ENABLE_PLUSES_DELIMITED_METADATA_BLOCKS;
    let mut candidates = Vec::new();
    let mut active: Option<Candidate> = None;
    let mut nested = 0usize;
    // Precompute line starts once; source offsets from the parser are byte offsets.
    let mut starts = vec![0];
    starts.extend(text.match_indices('\n').map(|(index, _)| index + 1));
    for (event, range) in Parser::new_ext(text, options).into_offset_iter() {
        match event {
            Event::Start(Tag::BlockQuote(_) | Tag::List(_) | Tag::Item) => nested += 1,
            Event::End(TagEnd::BlockQuote(_) | TagEnd::List(_) | TagEnd::Item) => {
                nested = nested.saturating_sub(1);
            }
            Event::Start(Tag::Heading { level, .. }) if nested == 0 => {
                active = Some(Candidate {
                    title: String::new(),
                    level: level as u32,
                    line: starts.partition_point(|start| *start <= range.start),
                    page: 1,
                    page_boundary: false,
                });
            }
            Event::Text(value) | Event::Code(value) => {
                if let Some(heading) = &mut active {
                    heading.title.push_str(&value);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if let Some(heading) = &mut active {
                    heading.title.push(' ');
                }
            }
            Event::End(TagEnd::Heading(_)) => {
                if let Some(mut heading) = active.take() {
                    heading.title = heading.title.trim().into();
                    if !heading.title.is_empty() {
                        candidates.push(heading);
                    }
                }
            }
            _ => {}
        }
    }
    let pages = vec![page];
    let nodes = build_nodes(title, &pages, candidates);
    ParsedDocument {
        title: title.into(),
        text: text.into(),
        pages,
        nodes,
        parser: "plaintext".into(),
        warnings: Vec::new(),
    }
}

/// Build a tree over canonical extracted page text.
///
/// Each page has at least one canonical line (including blank pages), newline
/// conventions are normalized, and the document ends with a newline. Valid
/// bookmarks provide the hierarchy frame; located layout headings fill in its
/// sections. Unlocated bookmark boundaries share a page with the previous
/// section. This is a LiteParse-derived profile, not Flash algorithm parity.
pub fn from_pages(
    title: &str,
    inputs: &[PageInput],
    headings: &[Heading],
    bookmarks: &[Bookmark],
    parser: &str,
) -> Result<ParsedDocument, BuildError> {
    let mut pages = Vec::with_capacity(inputs.len());
    let mut text = String::new();
    let mut next_line = 1usize;
    for (index, input) in inputs.iter().enumerate() {
        if input.number as usize != index + 1 {
            return Err(BuildError(format!(
                "incomplete or unordered pages: expected {}, found {}",
                index + 1,
                input.number
            )));
        }
        let lines: Vec<&str> = input.text.lines().collect();
        let count = lines.len().max(1);
        let canonical = lines.join("\n");
        text.push_str(&canonical);
        text.push('\n');
        pages.push(Page {
            number: input.number,
            text: canonical,
            line_start: next_line,
            line_end: next_line + count - 1,
        });
        next_line += count;
    }
    let mut layout = Vec::with_capacity(headings.len());
    for heading in headings {
        let page = heading
            .page
            .checked_sub(1)
            .and_then(|index| pages.get(index as usize))
            .ok_or_else(|| {
                BuildError(format!("heading references absent page {}", heading.page))
            })?;
        if heading.level == 0
            || heading.line == 0
            || heading.line > page.line_end - page.line_start + 1
        {
            return Err(BuildError(format!(
                "invalid heading coordinates on page {}",
                heading.page
            )));
        }
        if !heading.title.trim().is_empty() {
            layout.push(Candidate {
                title: heading.title.trim().into(),
                level: heading.level,
                line: page.line_start + heading.line - 1,
                page: heading.page,
                page_boundary: false,
            });
        }
    }
    let mut warnings = Vec::new();
    let mut frames: Vec<Candidate> = Vec::new();
    let mut levels = Vec::new();
    let mut previous_page = 0;
    let mut rejected = 0;
    let mut coarse = 0;
    for bookmark in bookmarks {
        if bookmark.page == 0
            || bookmark.page as usize > pages.len()
            || bookmark.page < previous_page
            || bookmark.level == 0
            || bookmark.title.trim().is_empty()
            || generic_bookmark(&bookmark.title)
        {
            rejected += 1;
            continue;
        }
        previous_page = bookmark.page;
        while levels.last().is_some_and(|level| *level >= bookmark.level) {
            levels.pop();
        }
        levels.push(bookmark.level);
        let page = &pages[bookmark.page as usize - 1];
        let located = locate_heading(&page.text, &bookmark.title);
        coarse += usize::from(located.is_none());
        frames.push(Candidate {
            title: bookmark.title.trim().into(),
            level: levels.len() as u32,
            line: page.line_start + located.unwrap_or(1) - 1,
            page: bookmark.page,
            page_boundary: located.is_none(),
        });
    }
    if rejected > 0 {
        warnings.push(format!("ignored_invalid_or_generic_bookmarks:{rejected}"));
    }
    if coarse > 0 {
        warnings.push(format!("bookmarks_with_page_only_boundaries:{coarse}"));
    }
    // Bookmarks are a frame, not duplicate sections. Preserve layout headings
    // absent from that frame as children of the active bookmark.
    frames.sort_by_key(|frame| (frame.line, frame.level));
    let frame_titles: HashSet<_> = frames
        .iter()
        .map(|frame| (frame.page, normalized(&frame.title)))
        .collect();
    let mut candidates = frames.clone();
    for mut heading in layout {
        if frame_titles.contains(&(heading.page, normalized(&heading.title))) {
            continue;
        }
        let active = frames.partition_point(|frame| frame.line <= heading.line);
        if let Some(frame) = active.checked_sub(1).and_then(|index| frames.get(index)) {
            heading.level = frame.level.saturating_add(heading.level);
        }
        candidates.push(heading);
    }
    candidates.sort_by_key(|candidate| (candidate.line, candidate.level));
    candidates.dedup_by(|a, b| {
        a.line == b.line && a.level == b.level && normalized(&a.title) == normalized(&b.title)
    });
    if candidates.is_empty() && !pages.is_empty() {
        warnings.push("no_detected_hierarchy:page_fallback".into());
    }
    if !pages.is_empty() && text.trim().is_empty() {
        warnings.push("document_has_no_text".into());
    }
    let nodes = build_nodes(title, &pages, candidates);
    Ok(ParsedDocument {
        title: title.into(),
        text,
        pages,
        nodes,
        parser: parser.into(),
        warnings,
    })
}

/// Locate a layout heading in source page text, including wrapped headings.
/// Punctuation/markup and whitespace are ignored only for matching; the source
/// text and title are not rewritten. A missing match stays missing.
pub fn locate_heading(text: &str, title: &str) -> Option<usize> {
    let target = normalized(title);
    if target.is_empty() {
        return None;
    }
    let lines: Vec<String> = text.lines().map(normalized).collect();
    for (index, line) in lines.iter().enumerate() {
        if line.is_empty() || !target.starts_with(line) {
            continue;
        }
        let mut joined = String::new();
        // Layout headings are short; a bounded match avoids scanning prose.
        for part in lines.iter().skip(index).take(8) {
            joined.push_str(part);
            if joined == target {
                return Some(index + 1);
            }
            if !target.starts_with(&joined) {
                break;
            }
        }
    }
    None
}

fn normalized(text: &str) -> String {
    text.chars()
        .filter(|value| value.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn generic_bookmark(title: &str) -> bool {
    let key = normalized(title);
    key.chars().all(|value| value.is_numeric())
        || ["page", "slide", "folie", "documentpage"]
            .iter()
            .any(|prefix| {
                key.strip_prefix(prefix)
                    .is_some_and(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_numeric()))
            })
}

fn build_nodes(title: &str, pages: &[Page], candidates: Vec<Candidate>) -> Vec<TreeNode> {
    let Some(last_page) = pages.last() else {
        return Vec::new();
    };
    if candidates.is_empty() {
        return pages
            .iter()
            .enumerate()
            .map(|(index, page)| TreeNode {
                id: format!("{index:04}"),
                parent_id: None,
                title: if pages.len() == 1 {
                    title.into()
                } else {
                    format!("Page {}", page.number)
                },
                level: 1,
                page_start: page.number,
                page_end: page.number,
                line_start: page.line_start,
                line_end: page.line_end,
                key_items: Vec::new(),
            })
            .collect();
    }
    let mut nodes: Vec<TreeNode> = Vec::with_capacity(candidates.len() + 1);
    let mut parents: Vec<Option<usize>> = Vec::with_capacity(candidates.len() + 1);
    let first = &candidates[0];
    if first.line > 1 {
        nodes.push(TreeNode {
            id: "0000".into(),
            parent_id: None,
            title: "Preface".into(),
            level: 1,
            page_start: 1,
            page_end: page_for_line(pages, first.line - 1),
            line_start: 1,
            line_end: first.line - 1,
            key_items: Vec::new(),
        });
        parents.push(None);
    }
    let mut stack: Vec<usize> = Vec::new();
    for candidate in candidates {
        while stack
            .last()
            .is_some_and(|index| nodes[*index].level >= candidate.level)
        {
            let index = stack.pop().expect("nonempty stack checked above");
            let boundary = if candidate.page_boundary {
                pages[candidate.page as usize - 1].line_end
            } else {
                candidate.line.saturating_sub(1)
            };
            nodes[index].line_end = boundary.max(nodes[index].line_start);
        }
        let parent = stack.last().copied();
        let index = nodes.len();
        nodes.push(TreeNode {
            id: format!("{index:04}"),
            parent_id: parent.map(|index| nodes[index].id.clone()),
            title: candidate.title,
            level: candidate.level,
            page_start: candidate.page,
            page_end: last_page.number,
            line_start: candidate.line,
            line_end: last_page.line_end,
            key_items: Vec::new(),
        });
        parents.push(parent);
        stack.push(index);
    }
    // A parent's span includes every descendant. Shared page boundaries are
    // intentional; line/page provenance never depends on non-overlapping nodes.
    for index in (0..nodes.len()).rev() {
        if let Some(parent) = parents[index] {
            nodes[parent].line_end = nodes[parent].line_end.max(nodes[index].line_end);
        }
        nodes[index].page_end = page_for_line(pages, nodes[index].line_end);
    }
    nodes
}

fn page_for_line(pages: &[Page], line: usize) -> u32 {
    pages[pages
        .partition_point(|page| page.line_start <= line)
        .saturating_sub(1)]
    .number
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_coverage(document: &ParsedDocument) {
        let total = document.text.lines().count();
        for line in 1..=total {
            assert!(
                document.nodes.iter().any(|node| {
                    node.parent_id.is_none() && node.line_start <= line && line <= node.line_end
                }),
                "line {line} uncovered"
            );
        }
        for node in &document.nodes {
            assert!(node.line_start >= 1 && node.line_start <= node.line_end);
            assert!(node.line_end <= total);
            if let Some(parent) = &node.parent_id {
                let parent = document
                    .nodes
                    .iter()
                    .find(|item| &item.id == parent)
                    .unwrap();
                assert!(parent.line_start <= node.line_start && parent.line_end >= node.line_end);
            }
        }
    }

    #[test]
    fn source_lines_and_nested_headings_are_preserved() {
        let text = "intro\r\n# First\r\nbody\r\n## Child\r\nchild body\r\n# Last\r\nend\r\n";
        let doc = from_text("doc", text);
        assert_eq!(doc.text, text);
        assert_eq!(doc.parser, "plaintext");
        assert_eq!(doc.nodes.len(), 4);
        assert_eq!(doc.nodes[1].line_start, 2);
        assert_eq!(doc.nodes[1].line_end, 5);
        assert_eq!(doc.nodes[2].parent_id.as_deref(), Some("0001"));
        assert_eq!(doc.nodes[3].line_start, 6);
        assert_coverage(&doc);
    }

    #[test]
    fn metadata_code_and_quoted_headings_are_not_sections() {
        let text = "---\nname: example\n# metadata\n---\n```md\n# code\n```\n> # quoted\n\n标题 **示例**\n======\nbody\n\n小节\n----\nend";
        let doc = from_text("doc", text);
        let titles: Vec<_> = doc.nodes.iter().map(|node| node.title.as_str()).collect();
        assert_eq!(titles, ["Preface", "标题 示例", "小节"]);
        assert_eq!(doc.nodes[1].line_start, 10);
        assert_eq!(doc.nodes[2].parent_id.as_deref(), Some("0001"));
        assert_coverage(&doc);
    }

    #[test]
    fn empty_and_headingless_plaintext_have_honest_ranges() {
        let empty = from_text("empty", "");
        assert!(empty.nodes.is_empty() && empty.pages.is_empty());
        let doc = from_text("notes", "one\n二\n");
        assert_eq!(doc.nodes[0].title, "notes");
        assert_eq!(doc.nodes[0].line_end, 2);
        assert_coverage(&doc);
    }

    #[test]
    fn pages_include_blank_pages_and_same_page_sections() {
        let pages = vec![
            PageInput {
                number: 1,
                text: "Overview\nbody\nDetail\nmore".into(),
            },
            PageInput {
                number: 2,
                text: "".into(),
            },
            PageInput {
                number: 3,
                text: "Conclusion\nfinal".into(),
            },
        ];
        let headings = vec![
            Heading {
                title: "Overview".into(),
                level: 1,
                page: 1,
                line: 1,
            },
            Heading {
                title: "Detail".into(),
                level: 2,
                page: 1,
                line: 3,
            },
            Heading {
                title: "Conclusion".into(),
                level: 1,
                page: 3,
                line: 1,
            },
        ];
        let doc = from_pages("doc", &pages, &headings, &[], "test-layout").unwrap();
        assert_eq!(doc.pages[1].line_start, 5);
        assert_eq!(doc.pages[1].line_end, 5);
        assert_eq!(doc.nodes[0].page_end, 2);
        assert_eq!(doc.nodes[2].line_start, 6);
        assert_coverage(&doc);
    }

    #[test]
    fn coarse_bookmarks_share_boundaries_and_invalid_entries_are_reported() {
        let pages: Vec<_> = (1..=3)
            .map(|number| PageInput {
                number,
                text: "body\ntext".into(),
            })
            .collect();
        let bookmarks = vec![
            Bookmark {
                title: "Introduction".into(),
                level: 1,
                page: 1,
            },
            Bookmark {
                title: "Conclusion".into(),
                level: 1,
                page: 3,
            },
            Bookmark {
                title: "Backward".into(),
                level: 2,
                page: 2,
            },
            Bookmark {
                title: "Page 3".into(),
                level: 1,
                page: 3,
            },
        ];
        let doc = from_pages("doc", &pages, &[], &bookmarks, "test-layout").unwrap();
        assert_eq!(doc.nodes.len(), 2);
        assert_eq!(doc.nodes[0].page_end, 3);
        assert!(
            doc.warnings
                .contains(&"ignored_invalid_or_generic_bookmarks:2".into())
        );
        assert_coverage(&doc);
    }

    #[test]
    fn invalid_page_and_heading_coordinates_fail() {
        let missing = [PageInput {
            number: 2,
            text: "text".into(),
        }];
        assert!(from_pages("doc", &missing, &[], &[], "test").is_err());
        let pages = [PageInput {
            number: 1,
            text: "text".into(),
        }];
        let headings = [Heading {
            title: "Oops".into(),
            level: 1,
            page: 1,
            line: 2,
        }];
        assert!(from_pages("doc", &pages, &headings, &[], "test").is_err());
    }

    #[test]
    fn wrapped_unicode_heading_location_is_bounded_and_exact() {
        assert_eq!(
            locate_heading("body\n数据 处理\n流水线\nend", "数据处理流水线"),
            Some(2)
        );
        assert_eq!(locate_heading("unrelated body", "absent"), None);
        assert_eq!(
            locate_heading("before\nA *heading*\n", "A heading"),
            Some(2)
        );
    }

    #[test]
    fn layout_sections_fill_bookmark_frame_without_duplicate_headings() {
        let pages = [
            PageInput {
                number: 1,
                text: "Part One\nbody".into(),
            },
            PageInput {
                number: 2,
                text: "Child Section\nchild body".into(),
            },
            PageInput {
                number: 3,
                text: "Part Two\nend".into(),
            },
        ];
        let bookmarks = [
            Bookmark {
                title: "Part One".into(),
                level: 1,
                page: 1,
            },
            Bookmark {
                title: "Part Two".into(),
                level: 1,
                page: 3,
            },
        ];
        let headings = [
            Heading {
                title: "Part One".into(),
                level: 1,
                page: 1,
                line: 1,
            },
            Heading {
                title: "Child Section".into(),
                level: 1,
                page: 2,
                line: 1,
            },
        ];
        let document = from_pages("doc", &pages, &headings, &bookmarks, "test").unwrap();
        assert_eq!(document.nodes.len(), 3);
        assert_eq!(document.nodes[1].parent_id.as_deref(), Some("0000"));
        assert_eq!(document.nodes[0].page_end, 2);
        assert_coverage(&document);
    }
}
