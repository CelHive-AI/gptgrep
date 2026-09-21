"""Read the predeclared split from provenance metadata; no task membership lives in code."""
from pathlib import Path
import locks

REFERENCE = Path(__file__).resolve().parents[2] / "evals/pageindex-reference.json"


def load(size: int, question_sha256: str) -> tuple[dict[str, set[int]], str]:
    value = locks.read_json(REFERENCE)
    if value["questions_sha256"] != question_sha256 or value["question_count"] != size:
        raise ValueError("Cohort manifest does not match the pinned question file")
    selected = [row["row_index_zero_based"] for row in value["smoke_subset"]]
    if len(set(selected)) != len(selected) or any(type(row) is not int or not 0 <= row < size for row in selected):
        raise ValueError("Invalid predeclared development split")
    if len(selected) != value["smoke_subset_question_count"]:
        raise ValueError("Development split count differs from its declaration")
    development = set(selected)
    return {"development8": development, "heldout54": set(range(size)) - development}, locks.digest(REFERENCE)


def select(value: str, size: int, groups: dict[str, set[int]]) -> list[int]:
    if value == "all":
        selected = list(range(size))
    elif value == "dev8":
        selected = sorted(groups["development8"])
    elif value in groups:
        selected = sorted(groups[value])
    else:
        selected = sorted(set(int(row) for row in value.split(",")))
    if not selected or any(not 0 <= row < size for row in selected):
        raise ValueError("Invalid selected source rows")
    return selected

