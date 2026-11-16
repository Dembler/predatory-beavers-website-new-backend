"""Offline report safety, pairing and incomplete checkpoint handling."""

import hashlib
import importlib.util
import json
import random
import sys
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "report_bobrik_evaluation.py"
SPEC = importlib.util.spec_from_file_location("report_bobrik_evaluation", SCRIPT)
assert SPEC and SPEC.loader
report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report
SPEC.loader.exec_module(report)


def row(case_id="case-1", answer="Ответ по истории", latency=200):
    return {
        "case": {
            "id": case_id,
            "question": "Когда основан клуб?",
            "category": "history",
            "tone": "polite",
            "base_id": "base-1",
        },
        "raw_content": json.dumps({"text": answer}),
        "parsed": {"text": answer},
        "displayed_answer": answer,
        "citations": [{"title": "История", "url": "/club"}],
        "retrieved_sources": [],
        "guard_outcome": "accepted",
        "review_flags": [],
        "provider_attempted": True,
        "provider_model": "fake-model",
        "gateway_error": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 7,
            "details": {"opencode": {"billing_source": "go"}},
        },
        "latency_ms": latency,
    }


def run(rows):
    return {
        "manifest": {
            "model": "fake-model",
            "corpus_sha256": "frozen-corpus",
            "clock_utc": "2026-11-16T20:00:00+00:00",
        },
        "rows": rows,
        "unknown_or_in_flight_case_ids": [],
        "incomplete_last_line_ignored": False,
    }


class CaptureHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


def test_untrusted_model_and_case_text_cannot_create_html_or_script_nodes():
    payload = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
    unsafe = row(answer=payload)
    unsafe["case"].update(question=payload, category='history" onmouseover="alert(3)')
    unsafe["citations"] = [{"title": payload, "url": "javascript:alert(4)"}]
    unsafe["retrieved_sources"] = [{"text": payload}]
    rendered = report.render_html(run([unsafe]), None)
    parsed = CaptureHTML()
    parsed.feed(rendered)
    assert parsed.tags.count("script") == 1  # Only the fixed, local filter script.
    assert "img" not in parsed.tags
    assert "a" not in parsed.tags
    assert not any(name.startswith("on") for name, _ in parsed.attrs)
    assert payload in "".join(parsed.text)
    assert "default-src &#" not in rendered
    assert "default-src 'none'" in rendered


def test_pairing_uses_case_ids_and_retains_missing_results_in_both_directions():
    before = run([row("one"), row("two")])
    after = run([row("two", "Исправленный ответ"), row("three")])
    pairs = report.paired_rows(before, after)
    assert [
        (left["case"]["id"] if left else None, right["case"]["id"] if right else None)
        for left, right in pairs
    ] == [("one", None), ("two", "two"), (None, "three")]
    rendered = report.render_html(before, after)
    assert rendered.count('class="case"') == 3
    assert "Исправленный ответ" in rendered
    assert "Нет результата." in rendered
    assert "оценка смысловой точности" in rendered


def test_report_counts_provider_errors_separately_from_guard_fallbacks_and_unknown_usage():
    accepted = row("one", latency=100)
    fallback = row("two", latency=300)
    fallback["guard_outcome"] = "unsupported_numbers_fallback"
    failed = row("three", latency=900)
    failed.update(
        guard_outcome="gateway_error", usage=None, gateway_error={"code": "llm_unavailable"}
    )
    summary = report.run_summary(run([accepted, fallback, failed]))
    assert summary["fallbacks"] == 1
    assert summary["gateway_errors"] == {"llm_unavailable": 1}
    assert summary["missing_usage_cases"] == 1
    assert summary["input_tokens_reported"] == 24
    assert summary["latency_p50_ms"] == 300
    assert summary["latency_p95_ms"] == 900
    assert summary["billing_sources"] == {"go": 2}


def test_live_checkpoint_ignores_partial_utf8_last_line_without_corrupting_previous_rows(tmp_path):
    checkpoint = tmp_path / "responses.jsonl"
    first = (json.dumps(row(), ensure_ascii=False) + "\n").encode("utf-8")
    checkpoint.write_bytes(first + '{"text":"Боб'.encode()[:-1])
    rows, incomplete = report.read_complete_jsonl(checkpoint)
    assert rows == [row()]
    assert incomplete is True
    checkpoint.write_bytes(first + b"not JSON\n")
    with pytest.raises(ValueError, match="Malformed complete"):
        report.read_complete_jsonl(checkpoint)


def test_generate_report_is_offline_and_markdown_neutralizes_generated_markup(tmp_path):
    source = tmp_path / "before"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(run([])["manifest"]), encoding="utf-8")
    unsafe = row(answer="![x](https://tracker.invalid/pixel)<script>alert(1)</script>")
    (source / "responses.jsonl").write_text(json.dumps(unsafe) + "\n", encoding="utf-8")
    (source / "attempts.jsonl").write_text(
        '{"case_id":"case-1"}\n{"case_id":"unknown-paid"}\n',
        encoding="utf-8",
    )
    output = tmp_path / "reports"
    summary = report.generate_report(source, None, output)
    assert sorted(path.name for path in output.iterdir()) == [
        "report.html",
        "report.md",
        "summary.json",
    ]
    assert summary["before"]["unknown_or_in_flight_case_ids"] == ["unknown-paid"]
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "![x](https://tracker.invalid/pixel)" not in markdown
    assert "<script>" not in markdown
    assert "200 базовых ситуаций" not in markdown  # Do not invent provenance for arbitrary data.


def test_provenance_accepts_shuffled_complete_case_payloads_not_file_order_or_hash():
    cases, incomplete = report.read_complete_jsonl(report.DATASET)
    assert incomplete is False
    assert len(cases) == 1000
    random.Random(765).shuffle(cases)
    shuffled = run([{"case": case} for case in cases])
    shuffled["manifest"]["cases_sha256"] = "different-raw-file-hash-after-shuffling"

    summary = report.run_summary(shuffled)

    assert summary["dataset_match"] == {
        "status": "full",
        "matched_cases": 1000,
        "canonical_cases": 1000,
    }
    assert "OpenCode" in summary["dataset_provenance"]
    assert "200 базовых ситуаций" in summary["dataset_provenance"]
    assert "Подмножество" not in summary["dataset_provenance"]


@pytest.mark.parametrize("field", ["question", "expectations"])
def test_provenance_rejects_same_id_with_changed_payload_even_with_canonical_hash(field):
    cases, _ = report.read_complete_jsonl(report.DATASET)
    modified = deepcopy(cases[0])
    modified[field] = "changed question" if field == "question" else ["different expectation"]
    changed = run([{"case": modified}])
    changed["manifest"]["cases_sha256"] = hashlib.sha256(report.DATASET.read_bytes()).hexdigest()

    summary = report.run_summary(changed)

    assert summary["dataset_match"]["status"] == "unverified"
    assert "OpenCode" not in summary["dataset_provenance"]
    assert "200 базовых ситуаций" not in summary["dataset_provenance"]


def test_provenance_labels_exact_partial_checkpoint_as_subset_without_claiming_full_run():
    cases, _ = report.read_complete_jsonl(report.DATASET)
    partial = run([{"case": cases[7]}, {"case": cases[1]}])

    summary = report.run_summary(partial)

    assert summary["dataset_match"] == {
        "status": "subset",
        "matched_cases": 2,
        "canonical_cases": 1000,
    }
    assert "Подмножество" in summary["dataset_provenance"]
    assert "2 из 1000" in summary["dataset_provenance"]
    assert "Полного прогона" in summary["dataset_provenance"]
