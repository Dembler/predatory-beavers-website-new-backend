"""Offline column reports escape hostile drafts and keep provenance explicit."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "report_bobrik_column", ROOT / "scripts" / "report_bobrik_column.py"
)
assert SPEC and SPEC.loader
report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report
SPEC.loader.exec_module(report)


def case():
    return json.loads(
        (ROOT / "tests" / "fixtures" / "bobrik_column_cases.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )


def row():
    return {
        "case": case(),
        "draft": {"title": "<script>alert(1)</script>", "lead": "hello"},
        "rendered_paragraphs": [
            {"heading": "bad", "body": "<img src=https://evil.example onerror=alert(1)>"}
        ],
        "raw_content": "</pre><script>steal()</script>",
        "checks": {"issues": []},
        "guard_outcome": "passed_checks",
        "provider_attempted": True,
        "usage": None,
        "latency_ms": 1,
        "review_flags": [],
    }


def run(rows):
    return {
        "manifest": {"selected_cases": len(rows)},
        "rows": rows,
        "incomplete_last_line_ignored": False,
        "unknown_or_in_flight_case_ids": [],
    }


def test_offline_report_escapes_raw_draft_and_marks_synthetic_checks_not_accuracy():
    state = run([row()])
    state["manual_reviews"] = [
        {
            "case_id": row()["case"]["id"],
            "category": "men_week",
            "reviewed_full_draft_and_facts": True,
            "notes": "<script>review()</script>",
        }
    ]
    document, summary = report.render_report(state)
    assert "<script>alert(1)</script>" not in document
    assert "<img src=https://evil.example" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "<script>review()</script>" not in document
    assert "&lt;script&gt;review()&lt;/script&gt;" in document
    assert "default-src 'none'" in document
    assert "смысловой точности" in document
    assert summary["after"]["guard_outcomes"] == {"passed_checks": 1}
    assert summary["after"]["provenance"]["status"] == "subset"
    assert summary["after"]["manual_review"]["families"] == ["men_week"]


def test_same_id_with_changed_case_content_is_not_verified_or_paired():
    before, after = row(), row()
    after["case"] = dict(after["case"], editorial_brief="altered")
    document, summary = report.render_report(run([after]), run([before]))
    assert summary["paired_identical_cases"] == 0
    assert summary["after"]["provenance"]["status"] == "unverified"
    assert "прямое сравнение не подтверждено" in document


def test_independent_review_schema_counts_only_explicit_full_readings():
    state = run([row()])
    state["manual_reviews"] = [
        {
            "case_id": row()["case"]["id"],
            "family": "men_week",
            "review_type": "full_manual_draft_and_factsheet",
        },
        {"case_id": "unread", "family": "privacy", "review_type": "phrase_scan"},
    ]
    summary = report.run_summary(state)["manual_review"]
    assert summary["full_draft_and_facts_readings"] == 1
    assert summary["families"] == ["men_week"]
    assert len(summary["records"]) == 2


def test_partial_last_checkpoint_line_is_visible_as_incomplete(tmp_path):
    path = tmp_path / "responses.jsonl"
    path.write_text(json.dumps(row()) + '\n{"unfinished":', encoding="utf-8")
    values, incomplete = report.read_complete(path)
    assert len(values) == 1
    assert incomplete is True


def test_provider_cost_fields_are_distinct_exact_observations_with_unknown_currency():
    rows = [
        {"usage": {"details": {"cost": 0, "opencode": {"allowance": 0.1, "list_cost": 0.2}}}},
        {"usage": {"details": {"cost": 0, "opencode": {"allowance": 0.2, "list_cost": 0.4}}}},
        {"usage": None},
    ]
    costs = report.provider_costs(rows)
    assert costs["reported_totals"] == {"cost": "0", "allowance": "0.3", "list_cost": "0.6"}
    assert costs["missing_rows"] == {"cost": 1, "allowance": 1, "list_cost": 1}
    assert costs["currency"].startswith("not supplied")


def test_counts_keep_recorded_attempts_invalid_drafts_and_word_target_distinct():
    valid, invalid = row(), row()
    valid["checks"]["word_count"] = 699
    invalid["draft"] = None
    invalid["checks"] = None
    invalid["guard_outcome"] = "invalid_model_response"
    state = run([valid, invalid])
    state["ledger_attempts"] = 3
    state["unknown_or_in_flight_case_ids"] = ["unknown-attempt"]
    summary = report.run_summary(state)
    assert summary["provider_attempts"] == 2
    assert summary["ledger_attempts"] == 3
    assert summary["structured_drafts"] == 1
    assert summary["word_counts"]["reported_drafts"] == 1
    assert summary["word_counts"]["target_700_to_1000"] == 0
    assert summary["word_counts"]["below_700"] == 1
    assert summary["unknown_or_in_flight_case_ids"] == ["unknown-attempt"]
