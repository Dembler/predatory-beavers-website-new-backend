"""Render an escaped offline report for weekly-column evaluation checkpoints.

No API calls, model judging or database access. Checks are engineering gates;
semantic quality must be assessed from the recorded article and frozen facts.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "fixtures" / "bobrik_column_cases.jsonl"
DISCLAIMER = (
    "Синтетические тестовые данные, не реальные результаты клуба. "
    "200 базовых ситуаций × 5 редакторских заданий = 1000 вариантов. "
    "Пройденные проверки не являются оценкой смысловой точности; флаги требуют ручного разбора."
)


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_complete(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    data = path.read_bytes()
    end = data.rfind(b"\n") + 1
    incomplete = bool(data[end:].strip())
    rows = []
    for line in data[:end].decode("utf-8-sig").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Checkpoint rows must be objects")
            rows.append(value)
    return rows, incomplete


def load_run(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (
        manifest.get("product_key") != "weekly_column"
        or manifest.get("fixture_kind") != "synthetic"
    ):
        raise ValueError("This report accepts explicitly synthetic weekly-column runs only")
    rows, partial = read_complete(directory / "responses.jsonl")
    attempts, partial_attempts = read_complete(directory / "attempts.jsonl")
    reviews, partial_reviews = read_complete(directory / "manual-review.jsonl")
    ids = [row.get("case", {}).get("id") for row in rows]
    if any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Checkpoint contains missing or duplicate case ids")
    unknown = sorted(
        str(case_id)
        for case_id in {
            row.get("case_id") for row in attempts if isinstance(row.get("case_id"), str)
        }
        - set(ids)
    )
    return {
        "manifest": manifest,
        "rows": rows,
        "ledger_attempts": len(attempts),
        "manual_reviews": reviews,
        "incomplete_manual_review_line_ignored": partial_reviews,
        "incomplete_last_line_ignored": partial or partial_attempts,
        "unknown_or_in_flight_case_ids": unknown,
    }


def provenance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not DATASET.exists():
        return {"status": "unverified", "matched_cases": 0}
    cases, partial = read_complete(DATASET)
    canonical = {case.get("id"): case for case in cases}
    if partial or len(canonical) != len(cases) or not cases or not rows:
        return {"status": "unverified", "matched_cases": 0}
    if any(row.get("case") != canonical.get(row.get("case", {}).get("id")) for row in rows):
        return {"status": "unverified", "matched_cases": 0}
    return {
        "status": "full" if len(rows) == len(canonical) else "subset",
        "matched_cases": len(rows),
        "canonical_cases": len(canonical),
    }


def provider_costs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum exact provider observations; never invent currency or subscription cost."""
    totals = {"cost": Decimal(0), "allowance": Decimal(0), "list_cost": Decimal(0)}
    reported: Counter[str] = Counter()
    for row in rows:
        details = (row.get("usage") or {}).get("details", {})
        opencode = details.get("opencode", {})
        for key, value in (
            ("cost", details.get("cost")),
            ("allowance", opencode.get("allowance")),
            ("list_cost", opencode.get("list_cost")),
        ):
            if isinstance(value, bool) or value is None:
                continue
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                continue
            if amount.is_finite() and amount >= 0:
                totals[key] += amount
                reported[key] += 1
    return {
        "reported_totals": {key: str(value) for key, value in totals.items()},
        "reported_rows": dict(reported),
        "missing_rows": {key: len(rows) - reported[key] for key in totals},
        "currency": "not supplied in recorded usage payload",
        "note": (
            "cost, allowance and list_cost are distinct provider fields. "
            "A recorded Go cost of zero does not measure subscription price or mean no usage."
        ),
    }


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    rows = run["rows"]
    latency = sorted(
        row["latency_ms"] for row in rows if isinstance(row.get("latency_ms"), (int, float))
    )
    words = sorted(
        row["checks"]["word_count"]
        for row in rows
        if isinstance((row.get("checks") or {}).get("word_count"), int)
    )
    reviews = run.get("manual_reviews", [])
    full_reviews = [
        review
        for review in reviews
        if review.get("reviewed_full_draft_and_facts") is True
        or review.get("review_type") == "full_manual_draft_and_factsheet"
    ]
    return {
        "completed_cases": len(rows),
        "selected_cases": run["manifest"].get("selected_cases"),
        "base_situations": len({row["case"]["base_id"] for row in rows}),
        "provider_attempts": sum(bool(row.get("provider_attempted")) for row in rows),
        "ledger_attempts": run.get("ledger_attempts"),
        "structured_drafts": sum(isinstance(row.get("draft"), dict) for row in rows),
        "word_counts": {
            "reported_drafts": len(words),
            "minimum": words[0] if words else None,
            "median": words[len(words) // 2] if words else None,
            "maximum": words[-1] if words else None,
            "target_700_to_1000": sum(700 <= count <= 1000 for count in words),
            "below_700": sum(count < 700 for count in words),
            "above_1000": sum(count > 1000 for count in words),
        },
        "manual_review": {
            "full_draft_and_facts_readings": len(full_reviews),
            "distinct_case_ids": sorted({review["case_id"] for review in full_reviews}),
            "families": sorted(
                {review.get("category") or review["family"] for review in full_reviews}
            ),
            "sampling": "targeted, non-random; not a semantic accuracy estimate",
            "records": reviews,
            "incomplete_last_line_ignored": run.get("incomplete_manual_review_line_ignored", False),
        },
        "guard_outcomes": dict(Counter(row.get("guard_outcome") for row in rows)),
        "gateway_errors": dict(
            Counter(row["gateway_error"].get("code") for row in rows if row.get("gateway_error"))
        ),
        "check_issues": dict(
            Counter(issue for row in rows for issue in (row.get("checks") or {}).get("issues", []))
        ),
        "categories": dict(Counter(row["case"].get("category") for row in rows)),
        "outcomes_by_category": {
            category: dict(
                Counter(
                    row.get("guard_outcome") for row in rows if row["case"]["category"] == category
                )
            )
            for category in sorted({row["case"]["category"] for row in rows})
        },
        "brief_modes": dict(Counter(row["case"].get("brief_mode") for row in rows)),
        "review_flags": dict(Counter(flag for row in rows for flag in row.get("review_flags", []))),
        "input_tokens": sum((row.get("usage") or {}).get("input_tokens") or 0 for row in rows),
        "output_tokens": sum((row.get("usage") or {}).get("output_tokens") or 0 for row in rows),
        "usage_unreported_cases": sum(
            row.get("provider_attempted") and row.get("usage") is None for row in rows
        ),
        "billing_sources": dict(
            Counter(
                (row.get("usage") or {})
                .get("details", {})
                .get("opencode", {})
                .get("billing_source", "unreported")
                for row in rows
            )
        ),
        "provider_costs": provider_costs(rows),
        "latency_p50_ms": latency[len(latency) // 2] if latency else None,
        "latency_p95_ms": latency[min(len(latency) - 1, int(len(latency) * 0.95))]
        if latency
        else None,
        "incomplete_last_line_ignored": run["incomplete_last_line_ignored"],
        "unknown_or_in_flight_case_ids": run["unknown_or_in_flight_case_ids"],
        "provenance": provenance(rows),
        "manifest": run["manifest"],
    }


def article_html(row: dict[str, Any] | None, label: str) -> str:
    if row is None:
        return f"<section><h3>{escaped(label)}</h3><p>Этот вариант не записан в данном прогоне.</p></section>"  # noqa: E501
    draft = row.get("draft") or {}
    paragraphs = row.get("rendered_paragraphs") or []
    text = f"<h4>{escaped(draft.get('title', 'Текст не получен'))}</h4><p>{escaped(draft.get('lead', ''))}</p>"  # noqa: E501
    for part in paragraphs:
        if isinstance(part, dict):
            text += f"<h5>{escaped(part.get('heading', ''))}</h5><p class='prose'>{escaped(part.get('body', ''))}</p>"  # noqa: E501
    details = "".join(
        f"<details><summary>{escaped(title)}</summary><pre>{escaped(pretty(value))}</pre></details>"
        for title, value in (
            (
                "Проверки и флаги",
                {
                    "checks": row.get("checks"),
                    "review_flags": row.get("review_flags"),
                    "gateway_error": row.get("gateway_error"),
                },
            ),
            ("FactSheet и точные источники", row.get("fact_sheet")),
            ("Структурированный черновик", row.get("draft")),
            ("Сырой ответ модели", row.get("raw_content")),
            (
                "Модель и usage",
                {
                    "model": row.get("provider_model"),
                    "usage": row.get("usage"),
                    "latency_ms": row.get("latency_ms"),
                },
            ),
        )
    )
    return f"<section><h3>{escaped(label)} · {escaped(row.get('guard_outcome'))}</h3>{text}{details}</section>"  # noqa: E501


def render_report(
    after: dict[str, Any], before: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    previous = {row["case"]["id"]: row for row in before["rows"]} if before else {}
    current = {row["case"]["id"]: row for row in after["rows"]}
    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "note": DISCLAIMER,
        "after": run_summary(after),
    }
    if before:
        summary["before"] = run_summary(before)
        summary["paired_identical_cases"] = sum(
            row["case"] == previous[key]["case"] for key, row in current.items() if key in previous
        )
    items = []
    for case_id in sorted(set(current) | set(previous)):
        row = current.get(case_id) or previous[case_id]
        case = row["case"]
        label = f"{case_id} · {case['category']} · {case['brief_mode']}"
        content = article_html(previous.get(case_id), "До") if before else ""
        content += article_html(current.get(case_id), "После" if before else "Результат")
        manual = [
            review for review in after.get("manual_reviews", []) if review.get("case_id") == case_id
        ]
        if manual:
            content += (
                "<details><summary>Ручное чтение этого варианта</summary><pre>"
                + escaped(pretty(manual))
                + "</pre></details>"
            )
        if (
            case_id in previous
            and case_id in current
            and previous[case_id]["case"] != current[case_id]["case"]
        ):
            content = (
                "<p class='warning'>Содержимое case изменилось: прямое сравнение не подтверждено.</p>"  # noqa: E501
                + content
            )
        items.append(
            f"<article data-category='{escaped(case['category'])}' data-outcome='{escaped((current.get(case_id) or {}).get('guard_outcome', 'missing'))}'>"  # noqa: E501
            f"<h2>{escaped(label)}</h2><p>{escaped(case['editorial_brief'])}</p>"
            f"<p>Неделя: {escaped(case['week_start'])}; scope: {escaped(pretty(case['scope']))}</p>"
            f"<div class='columns'>{content}</div></article>"
        )
    categories = sorted(
        {row["case"]["category"] for row in list(current.values()) + list(previous.values())}
    )
    options = "".join(
        f"<option value='{escaped(value)}'>{escaped(value)}</option>" for value in categories
    )
    script = """const search=document.querySelector('#search'),category=document.querySelector('#category'),outcome=document.querySelector('#outcome'),count=document.querySelector('#count');function filter(){let shown=0;for(const article of document.querySelectorAll('article')){const match=(!category.value||article.dataset.category===category.value)&&(!outcome.value||article.dataset.outcome===outcome.value)&&article.textContent.toLocaleLowerCase().includes(search.value.toLocaleLowerCase());article.hidden=!match;if(match)shown++;}count.textContent=String(shown);}for(const input of [search,category,outcome])input.addEventListener('input',filter);filter();"""  # noqa: E501
    document = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"  # noqa: E501
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"  # noqa: E501
        "<title>Бобрик · проверка колонки</title><style>body{font:16px/1.55 system-ui;margin:0 auto;padding:24px;max-width:1500px;background:#f6f8fc;color:#172138}h1,h2,h3,h4,h5{line-height:1.25}h2{font-size:20px}h5{font-size:17px}article,header{background:white;border:1px solid #cad4e4;border-radius:12px;padding:22px;margin-bottom:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 monospace}details{margin:14px 0}summary{cursor:pointer;font-weight:600}.columns{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:24px}.prose{white-space:pre-wrap}input,select{padding:9px;border:1px solid #74859c;border-radius:6px;max-width:100%;font:inherit}nav{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.warning{color:#8b2818}[hidden]{display:none!important}</style>"  # noqa: E501
        f"<header><h1>Проверка «Бобровой недели»</h1><p>{escaped(DISCLAIMER)}</p>"
        f"<details><summary>Итоги и происхождение данных</summary><pre>{escaped(pretty(summary))}</pre></details>"  # noqa: E501
        f"<nav><label>Поиск <input id='search' type='search'></label><label>Семейство <select id='category'><option value=''>Все</option>{options}</select></label>"  # noqa: E501
        "<label>Проверки <select id='outcome'><option value=''>Все</option><option>passed_checks</option><option>blocked_checks</option><option>invalid_model_response</option><option>gateway_error</option><option>missing</option></select></label><span>Показано: <strong id='count'></strong></span></nav></header>"  # noqa: E501
        + "".join(items)
        + f"<script>{script}</script></html>"
    )
    return document, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    arguments = parser.parse_args()
    after = load_run(arguments.after)
    before = load_run(arguments.before) if arguments.before else None
    document, summary = render_report(after, before)
    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    (arguments.out_dir / "report.html").write_text(document, encoding="utf-8")
    (arguments.out_dir / "summary.json").write_text(pretty(summary) + "\n", encoding="utf-8")
    (arguments.out_dir / "report.md").write_text(
        "# Проверка колонки Бобрика\n\n"
        + DISCLAIMER
        + "\n\n```json\n"
        + pretty(summary)
        + "\n```\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "completed_cases": summary["after"]["completed_cases"],
                "report": str(arguments.out_dir / "report.html"),
            }
        )
    )


if __name__ == "__main__":
    main()
