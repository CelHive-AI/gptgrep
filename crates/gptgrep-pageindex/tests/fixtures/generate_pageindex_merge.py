#!/usr/bin/env python3
"""Regenerate pure-operator golden fixtures from the pinned PageIndex source.

Only selected AST function definitions are executed. The PageIndex package is
never imported; no provider, PDF parser, SDK or model calls are possible through
this selected dependency closure. Supply the source checkout's tree_optimize.py.
"""

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path

REVISION = "9c4c3ff2ddd2cbd70501997635578064f2b7c7c3"
FUNCTIONS = {
    "note", "flatten", "subtree_end", "is_frontier", "relabel", "pages_of",
    "S", "S_residual", "tree_cost", "frontier_costs", "tree_cost_via_frontier",
    "page_label", "union_title", "merge_same_page", "merge",
}


def node(title, start, end, children=None, key_items=None):
    value = {"title": title, "start_index": start, "end_index": end}
    if children:
        value["nodes"] = children
    if key_items:
        value["key_items"] = key_items
    return value


def generate(source):
    tree = ast.parse(source.decode("utf-8"))
    selected = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in FUNCTIONS]
    if {item.name for item in selected} != FUNCTIONS:
        raise ValueError("source does not expose the pinned deterministic function closure")
    namespace = {"ROUTING_COST": 1, "TITLE_MAX_CHARS": 200}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "pageindex-pure-merge", "exec"), namespace)
    fixtures = [
        ("leaf_no_change", 4, [node("Leaf", 1, 4)]),
        ("useful_partition_retained", 10, [node("Root", 1, 10, [node("Left", 1, 5), node("Right", 6, 10)])]),
        ("tie_collapses", 3, [node("Root", 1, 3, [node("Child", 1, 2)])]),
        ("bottom_up_chain", 2, [node("Root", 1, 2, [node("Middle", 1, 2, [node("Leaf", 1, 1, key_items=["Prior leaf title"])])])]),
        ("overlap_uses_page_union", 8, [node("Root", 1, 8, [node("Left", 2, 5), node("Right", 4, 7)])]),
        ("residual_dominates", 10, [node("Root", 1, 10, [node("Tiny child", 5, 5)])]),
        ("same_page_siblings", 2, [node("A", 1, 1, key_items=["A old"]), node("B", 1, 1), node("C", 2, 2)]),
        ("same_multi_page_span", 3, [node("A", 1, 3), node("B", 1, 3)]),
        ("same_span_after_bottom_up", 2, [node("A", 1, 2, [node("A child", 1, 1)]), node("B", 1, 2, [node("B child", 2, 2)])]),
        ("unicode_title_length_is_characters", 1, [node("数" * 80, 1, 1), node("据" * 80, 1, 1)]),
        ("oversize_union_title_falls_back", 1, [node("数" * 101, 1, 1), node("据" * 101, 1, 1)]),
    ]
    output = []
    for name, page_count, structure in fixtures:
        namespace["relabel"](structure)
        costs = [{
            "node_id": item["node_id"],
            "span": namespace["S"](item),
            "residual": namespace["S_residual"](item),
            "cost": namespace["tree_cost"](item),
            "frontier_cost": namespace["tree_cost_via_frontier"](item),
        } for item, _ in namespace["flatten"](structure)]
        expected = copy.deepcopy(structure)
        for _ in range(len(costs) + 1):
            same = namespace["merge_same_page"](expected, [])
            collapsed = namespace["merge"](expected, 1, [], set())
            if not same and not collapsed:
                break
        else:
            raise ValueError("operators did not reach a fixed point")
        namespace["relabel"](expected)
        output.append({"name": name, "page_count": page_count, "input_tree": structure, "costs_before": costs, "expected_tree": expected})
    return {
        "schema": "gptgrep.pageindex-merge-fixtures.v1",
        "source_repository": "https://github.com/VectifyAI/PageIndex",
        "source_revision": REVISION,
        "source_file": "pageindex/tree_optimize.py",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "source_license": "MIT",
        "method": "execute selected upstream pure operators until fixed point, with routing cost 1 and title limit 200",
        "cases": output,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.source.read_bytes()
    # This guard binds the fixture generator to the researched source revision.
    if hashlib.sha256(source).hexdigest() != "3112940bf4c4e4add0203316575e1426cf06b375a97e67d415129af7c119061a":
        raise SystemExit("source digest differs from the researched PageIndex revision")
    args.output.write_text(json.dumps(generate(source), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
