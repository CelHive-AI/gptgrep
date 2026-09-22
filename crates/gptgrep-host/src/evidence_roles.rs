//! Exact-window projection of private role judgments into bounded evidence.
use anyhow::{Result, ensure};
use gptgrep_core::{EvidenceRoleDiagnostics, Hit};
use serde_json::{Value, json};

pub(crate) const READER_GUIDANCE: &str = "An evidence_role describes only its exact returned window, not answer correctness or whole-question sufficiency. For an issued source_local_incomplete window, resolve the local gap with the existing bounded source tools before relying on that fragment, or state the unresolved limitation. Use only current host-issued node IDs and exact byte cursors: next_offset for following text, or a permitted read/tree operation in the same fixed document for preceding or containing context. A missing forward cursor does not establish completeness. Stay within the existing tool and time budgets; do not invent missing content or offsets. Delivered-set sufficiency remains unassessed.";

pub(crate) fn validate_policy(
    diagnostics: &EvidenceRoleDiagnostics,
    generation: &str,
    question: &str,
) -> Result<()> {
    diagnostics.validate_bound()?;
    ensure!(
        diagnostics.strategy == "jev-evidence-role-v1"
            && diagnostics.generation == generation
            && diagnostics.original_question_sha256 == crate::retrieval::hash(question.as_bytes())
            && diagnostics.score_question_count == diagnostics.candidates.len()
            && diagnostics.choice_question_count == diagnostics.candidates.len()
            && diagnostics.question_count == diagnostics.candidates.len() * 2,
        "host_evidence_role_policy_mismatch"
    );
    Ok(())
}

/// An explicit association prevents a shortened window from borrowing the role
/// of another assessed span, including an overlapping span in the same node.
pub(crate) fn delivered_hit(
    diagnostics: &EvidenceRoleDiagnostics,
    generation: &str,
    assessed: &Hit,
    delivered: &Hit,
) -> Result<Value> {
    let mut value = serde_json::to_value(delivered)?;
    if let Some(hint) = diagnostics.hint_for_delivery(generation, assessed, delivered) {
        value["evidence_role"] = json!(hint.role);
        value["role_assessment_status"] = json!("assessed");
        // Role confidence remains private; Hit.confidence keeps relevance meaning.
    } else {
        value["role_assessment_status"] = json!("unavailable_for_delivered_span");
        // The original relevance judgment also cannot describe changed text.
        value["score"] = Value::Null;
        value["confidence"] = Value::Null;
    }
    Ok(value)
}

#[cfg(test)]
#[path = "evidence_role_tests.rs"]
pub(crate) mod tests;
