"""Build an offline, escaped HTML/Markdown report from Bobrik evaluation checkpoints.

No network, provider requests, credentials or semantic accuracy grading. Reports
can be regenerated while an evaluation is running; only complete JSONL lines
are included and an unfinished last line is explicitly reported.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "fixtures" / "bobrik_eval_cases.jsonl"
DISCLAIMER = (
    "Флаги — эвристики для ручной проверки, а не оценка смысловой точности. "
    "Принятый guard ответ не обязательно верен; отсутствие флага не доказывает качество. "
    "Здесь показаны только записанные результаты, без дополнительных вызовов модели."
)


def escaped(value: Any) -> str:
    return html.escape(str(value), quote=True)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_complete_jsonl(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    data = path.read_bytes()
    complete_end = data.rfind(b"\n") + 1
    truncated = bool(data[complete_end:].strip())
    complete = data[:complete_end].decode("utf-8-sig")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(complete.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Malformed complete JSONL line {line_number}: {path.name}") from error
        if not isinstance(row, dict):
            raise ValueError(f"JSONL line {line_number} must be an object")
        rows.append(row)
    return rows, truncated


def load_run(directory: Path) -> dict[str, Any]:
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    rows, incomplete = read_complete_jsonl(directory / "responses.jsonl")
    ids: set[str] = set()
    for row in rows:
        case_id = row.get("case", {}).get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("Missing or duplicate case id in responses")
        ids.add(case_id)
    attempts, incomplete_attempts = read_complete_jsonl(directory / "attempts.jsonl")
    attempted_ids = {attempt.get("case_id") for attempt in attempts}
    unknown = sorted(case_id for case_id in attempted_ids - ids if isinstance(case_id, str))
    return {
        "directory": str(directory),
        "manifest": manifest,
        "rows": rows,
        "incomplete_last_line_ignored": incomplete or incomplete_attempts,
        "unknown_or_in_flight_case_ids": unknown,
    }


def dataset_provenance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify complete case payloads; checkpoint order and manifest hash are insufficient."""
    unknown = {
        "status": "unverified",
        "matched_cases": 0,
        "canonical_cases": None,
        "description": (
            "Происхождение записанных вопросов отдельно не подтверждено. "
            "Фактические base_id, категории и тональности перечислены в статистике."
        ),
    }
    if not DATASET.is_file():
        return unknown
    fixture, incomplete = read_complete_jsonl(DATASET)
    canonical = {case.get("id"): case for case in fixture}
    if incomplete or not canonical or len(canonical) != len(fixture):
        return unknown
    unknown["canonical_cases"] = len(canonical)
    observed_ids: set[str] = set()
    for row in rows:
        case = row.get("case", {})
        case_id = case.get("id")
        if (
            not isinstance(case_id, str)
            or case_id in observed_ids
            or case_id not in canonical
            or case != canonical[case_id]
        ):
            return unknown
        observed_ids.add(case_id)
    if not observed_ids:
        return unknown
    design = (
        "Батарея подготовлена совместно с OpenCode: "
        "200 базовых ситуаций × 5 вариантов тона = 1000 вопросов. "
        "Это вариации 200 ситуаций, а не 1000 независимых сценариев."
    )
    complete = observed_ids == set(canonical)
    return {
        "status": "full" if complete else "subset",
        "matched_cases": len(observed_ids),
        "canonical_cases": len(canonical),
        "description": (
            "Основная батарея: содержимое всех объектов case совпадает с эталоном. "
            if complete
            else f"Подмножество основной батареи: записано и сверено {len(observed_ids)} "
            f"из {len(canonical)} объектов case. Полного прогона в этом отчёте пока нет. "
        )
        + design,
    }


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    rows = run["rows"]
    latencies = sorted(
        row["latency_ms"] for row in rows if isinstance(row.get("latency_ms"), (int, float))
    )
    usage_rows = [row["usage"] for row in rows if isinstance(row.get("usage"), dict)]
    bases = {row["case"].get("base_id") for row in rows if row["case"].get("base_id")}
    billing_sources = Counter(
        usage.get("details", {}).get("opencode", {}).get("billing_source", "unreported")
        for usage in usage_rows
    )
    provenance = dataset_provenance(rows)
    return {
        "completed_cases": len(rows),
        "base_situations_observed": len(bases),
        "categories": dict(Counter(row["case"].get("category", "unknown") for row in rows)),
        "tones": dict(Counter(row["case"].get("tone", "unknown") for row in rows)),
        "provider_attempts": sum(bool(row.get("provider_attempted")) for row in rows),
        "guard_outcomes": dict(Counter(row.get("guard_outcome", "unknown") for row in rows)),
        "fallbacks": sum("fallback" in str(row.get("guard_outcome")) for row in rows),
        "gateway_errors": dict(
            Counter(
                row["gateway_error"].get("code", "unknown")
                for row in rows
                if isinstance(row.get("gateway_error"), dict)
            )
        ),
        "review_flags": dict(Counter(flag for row in rows for flag in row.get("review_flags", []))),
        "input_tokens_reported": sum(usage.get("input_tokens") or 0 for usage in usage_rows),
        "output_tokens_reported": sum(usage.get("output_tokens") or 0 for usage in usage_rows),
        "missing_usage_cases": sum(
            bool(row.get("provider_attempted")) and row.get("usage") is None for row in rows
        ),
        "billing_sources": dict(billing_sources),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies
        else None,
        "unknown_or_in_flight_case_ids": run["unknown_or_in_flight_case_ids"],
        "incomplete_last_line_ignored": run["incomplete_last_line_ignored"],
        "dataset_provenance": provenance["description"],
        "dataset_match": {key: value for key, value in provenance.items() if key != "description"},
    }


def answer_panel(row: dict[str, Any] | None, label: str) -> str:
    if row is None:
        return (
            f'<section class="answer missing"><h3>{escaped(label)}</h3>'
            "<p>Нет результата.</p></section>"
        )
    outcome = row.get("guard_outcome", "unknown")
    answer = row.get("displayed_answer")
    rendered_answer = escaped(answer) if answer is not None else "Показанного ответа нет."
    citations = row.get("citations", [])
    citation_items = "".join(
        f"<li>{escaped(item.get('title', ''))} <code>{escaped(item.get('url', ''))}</code></li>"
        for item in citations
    )
    flags = ", ".join(row.get("review_flags", [])) or "нет"
    details = "".join(
        f"<details><summary>{escaped(title)}</summary><pre>{escaped(value)}</pre></details>"
        for title, value in (
            (
                "Исходный ответ модели",
                row.get("raw_content") or "Нет вызова или ответа провайдера.",
            ),
            ("Разобранный JSON", json_text(row.get("parsed"))),
            ("Найденные публичные источники", json_text(row.get("retrieved_sources", []))),
            (
                "Расход токенов и ошибка шлюза",
                json_text(
                    {
                        "usage": row.get("usage"),
                        "gateway_error": row.get("gateway_error"),
                        "unsupported_numbers": row.get("unsupported_numbers", []),
                        "provider_model": row.get("provider_model"),
                        "user_prompt_sha256": row.get("user_prompt_sha256"),
                    }
                ),
            ),
        )
    )
    return (
        f'<section class="answer"><h3>{escaped(label)}</h3>'
        f'<p class="meta">{escaped(outcome)} · {escaped(row.get("latency_ms", "?"))} мс</p>'
        f'<div class="displayed">{rendered_answer}</div>'
        f'<ul class="citations">{citation_items}</ul>'
        f'<p class="flags">Эвристические флаги: {escaped(flags)}</p>{details}</section>'
    )


def paired_rows(before: dict[str, Any], after: dict[str, Any] | None) -> list[tuple[Any, Any]]:
    left = {row["case"]["id"]: row for row in before["rows"]}
    right = {row["case"]["id"]: row for row in after["rows"]} if after else {}
    case_ids = list(left) + [case_id for case_id in right if case_id not in left]
    return [(left.get(case_id), right.get(case_id)) for case_id in case_ids]


def summary_card(label: str, run: dict[str, Any], summary: dict[str, Any]) -> str:
    manifest = run["manifest"]
    metrics = (
        ("Записано результатов", summary["completed_cases"]),
        ("Вызовов провайдера", summary["provider_attempts"]),
        ("Fallback ответов", summary["fallbacks"]),
        ("Ошибок шлюза", sum(summary["gateway_errors"].values())),
        (
            "Токены вход / выход",
            f"{summary['input_tokens_reported']} / {summary['output_tokens_reported']}",
        ),
        ("Задержка p50 / p95, мс", f"{summary['latency_p50_ms']} / {summary['latency_p95_ms']}"),
        ("Расход не сообщил провайдер", summary["missing_usage_cases"]),
        ("Ещё в работе или исход неизвестен", len(summary["unknown_or_in_flight_case_ids"])),
    )
    items = "".join(f"<dt>{escaped(key)}</dt><dd>{escaped(value)}</dd>" for key, value in metrics)
    return (
        f'<section class="summary-card"><h2>{escaped(label)}</h2><dl>{items}</dl>'
        f"<p>{escaped(summary['dataset_provenance'])}</p>"
        f'<p class="meta">Модель: {escaped(manifest.get("model"))}<br>'
        f"Часы снимка UTC: {escaped(manifest.get('clock_utc'))}<br>"
        f"Корпус: <code>{escaped(manifest.get('corpus_sha256'))}</code><br>"
        f"Промпт: <code>{escaped(manifest.get('system_prompt_sha256'))}</code></p>"
        "<details><summary>Вся статистика и происхождение</summary>"
        f"<pre>{escaped(json_text({'summary': summary, 'manifest': manifest}))}</pre>"
        "</details></section>"
    )


CSS = """
:root{color-scheme:light;font:15px/1.5 system-ui,sans-serif;color:#152c30;background:#edf3f1}
*{box-sizing:border-box}body{margin:0}main{max-width:1500px;margin:auto;padding:24px}
h1{font-size:28px;margin:0 0 10px}h2{font-size:20px}h3{font-size:16px;margin:0 0 8px}
p{margin:10px 0}code{overflow-wrap:anywhere;font-size:12px}pre{white-space:pre-wrap;
overflow-wrap:anywhere;background:#eff4f3;padding:12px;border-radius:8px;font-size:12px}
.notice{padding:14px 18px;border-left:4px solid #9c6819;background:#fff4dc;border-radius:8px}
.summary-grid,.answers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.summary-card,.case{background:white;border:1px solid #cadbd5;border-radius:12px;padding:18px}
dl{display:grid;grid-template-columns:1fr auto;gap:6px 12px}dd{margin:0;font-weight:650}
.toolbar{position:sticky;top:0;z-index:1;padding:14px 0;background:#edf3f1ee;
display:flex;gap:10px;flex-wrap:wrap;backdrop-filter:blur(8px)}
label{display:grid;gap:3px;font-size:12px}input,select,button{font:inherit;padding:8px;
border:1px solid #8aada1;border-radius:6px;background:white;max-width:100%}
input{min-width:280px}.case{margin:14px 0}.question{font-size:18px;white-space:pre-wrap;
font-weight:600}.meta,.flags{font-size:12px;color:#476363}.displayed{white-space:pre-wrap;
padding:12px;background:#f4f8f6;border-radius:8px;min-height:70px}.answer{min-width:0}
.citations{padding-left:20px;font-size:13px}details{margin:8px 0}summary{cursor:pointer}
.missing{color:#61716c}.single .answers,.single .summary-grid{grid-template-columns:1fr}
[hidden]{display:none!important}.count{align-self:end;padding:8px}footer{margin-top:20px}
@media(max-width:750px){main{padding:12px}.summary-grid,.answers{grid-template-columns:1fr}
.toolbar{position:static}input{min-width:180px}h1{font-size:24px}}
"""

JS = """
'use strict';
const search = document.getElementById('search');
const category = document.getElementById('category');
const tone = document.getElementById('tone');
const outcome = document.getElementById('outcome');
const flagged = document.getElementById('flagged');
const cards = [...document.querySelectorAll('.case')];
function filter() {
  const term = search.value.toLocaleLowerCase('ru').trim();
  let shown = 0;
  for (const card of cards) {
    const ok = (!term || card.dataset.search.includes(term)) &&
      (!category.value || card.dataset.category === category.value) &&
      (!tone.value || card.dataset.tone === tone.value) &&
      (!outcome.value || card.dataset.outcomes.split(' ').includes(outcome.value)) &&
      (!flagged.checked || card.dataset.flagged === 'true');
    card.hidden = !ok;
    if (ok) shown++;
  }
  document.getElementById('count').textContent = `Показано ${shown} из ${cards.length}`;
}
for (const element of [search, category, tone, outcome, flagged]) {
  element.addEventListener('input', filter);
}
document.getElementById('reset').addEventListener('click', () => {
  search.value = ''; category.value = ''; tone.value = ''; outcome.value = '';
  flagged.checked = false; filter();
});
filter();
"""


def render_html(before: dict[str, Any], after: dict[str, Any] | None) -> str:
    summaries = [("До", before, run_summary(before))]
    if after:
        summaries.append(("После", after, run_summary(after)))
    pairs = paired_rows(before, after)
    categories: set[str] = set()
    tones: set[str] = set()
    outcomes: set[str] = set()
    cards: list[str] = []
    for left, right in pairs:
        row = left or right
        case = row["case"]
        category, tone = case.get("category", "unknown"), case.get("tone", "unknown")
        pair_outcomes = [
            str(item.get("guard_outcome", "unknown")) for item in (left, right) if item
        ]
        categories.add(category)
        tones.add(tone)
        outcomes.update(pair_outcomes)
        searchable = json_text({"case": case, "before": left, "after": right}).lower()
        flagged = any(item.get("review_flags") for item in (left, right) if item)
        right_panel = answer_panel(right, "После") if after else ""
        changed_question = ""
        if left and right and left["case"]["question"] != right["case"]["question"]:
            changed_question = (
                '<p class="notice">Внимание: текст вопроса после изменения отличается: '
                f"{escaped(right['case']['question'])}</p>"
            )
        cards.append(
            f'<article class="case" data-category="{escaped(category)}" '
            f'data-tone="{escaped(tone)}" data-outcomes="{escaped(" ".join(pair_outcomes))}" '
            f'data-flagged="{str(flagged).lower()}" data-search="{escaped(searchable)}">'
            f'<p class="meta">{escaped(case["id"])} · {escaped(category)} · {escaped(tone)}</p>'
            f'<div class="question">{escaped(case["question"])}</div>{changed_question}'
            "<details><summary>Сценарий и ожидаемое поведение</summary>"
            f"<pre>{escaped(json_text(case))}</pre></details>"
            f'<div class="answers">{answer_panel(left, "До")}{right_panel}</div></article>'
        )

    def select(field: str, label: str, values: set[str]) -> str:
        options = "".join(
            f'<option value="{escaped(value)}">{escaped(value)}</option>'
            for value in sorted(values)
        )
        return (
            f'<label>{label}<select id="{field}"><option value="">Все</option>'
            f"{options}</select></label>"
        )

    summary_cards = "".join(summary_card(label, run, summary) for label, run, summary in summaries)
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'\">"
        f"<title>Проверка ответов Бобрика</title><style>{CSS}</style></head>"
        f'<body class="{"comparison" if after else "single"}"><main>'
        "<h1>Ответы Бобрика: журнал проверки</h1>"
        f'<p class="notice">{escaped(DISCLAIMER)}</p>'
        f'<div class="summary-grid">{summary_cards}</div>'
        '<div class="toolbar"><label>Поиск по ID, вопросу, ответу и источникам'
        '<input id="search" type="search" placeholder="Например, captain или расписание"></label>'
        f"{select('category', 'Категория', categories)}{select('tone', 'Тон', tones)}"
        f"{select('outcome', 'Результат guard', outcomes)}"
        '<label>Только с флагами<input id="flagged" type="checkbox"></label>'
        '<button id="reset" type="button">Сбросить</button>'
        '<span class="count" id="count" aria-live="polite"></span></div>'
        f'{"".join(cards)}<footer class="meta">Сформировано UTC: '
        f"{escaped(datetime.now(UTC).isoformat())}. Файл работает без сети.</footer>"
        f"</main><script>{JS}</script></body></html>"
    )


def markdown_safe(value: Any) -> str:
    # Escape raw HTML and Markdown punctuation, including generated links/images.
    rendered = html.escape(str(value), quote=False)
    for character in "\\`*_{}[]()#+-.!|":
        rendered = rendered.replace(character, "\\" + character)
    return rendered


def render_markdown(before: dict[str, Any], after: dict[str, Any] | None) -> str:
    lines = ["# Проверка ответов Бобрика", "", DISCLAIMER, ""]
    for label, run in [("До", before), ("После", after)]:
        if run is None:
            continue
        summary = run_summary(run)
        lines.extend(
            [
                f"## {label}",
                "",
                summary["dataset_provenance"],
                "",
                f"Записано: {summary['completed_cases']}; вызовов: {summary['provider_attempts']}; "
                f"fallback: {summary['fallbacks']}; "
                f"ошибок шлюза: {sum(summary['gateway_errors'].values())}.",
                f"Токены вход / выход: {summary['input_tokens_reported']} / "
                f"{summary['output_tokens_reported']}. "
                f"p50 / p95: {summary['latency_p50_ms']} / {summary['latency_p95_ms']} мс.",
                "",
                "Guard outcomes: " + markdown_safe(json.dumps(summary["guard_outcomes"])),
                "",
                "Эвристические флаги: " + markdown_safe(json.dumps(summary["review_flags"])),
                "",
                "Corpus SHA256: " + markdown_safe(run["manifest"].get("corpus_sha256")),
                "",
            ]
        )
    lines.extend(
        [
            "## Все вопросы и показанные ответы",
            "",
            "Исходные JSON и найденные источники доступны в report.html и responses.jsonl.",
            "",
        ]
    )
    for left, right in paired_rows(before, after):
        case = (left or right)["case"]
        lines.extend([f"### {markdown_safe(case['id'])}", "", markdown_safe(case["question"]), ""])
        for label, row in [("До", left), ("После", right)]:
            if label == "После" and after is None:
                continue
            answer = row.get("displayed_answer") if row else None
            outcome = row.get("guard_outcome") if row else "нет результата"
            lines.extend(
                [
                    f"**{label} · {markdown_safe(outcome)}**",
                    "",
                    markdown_safe(answer or "Показанного ответа нет."),
                    "",
                ]
            )
    return "\n".join(lines)


def generate_report(before_dir: Path, after_dir: Path | None, out_dir: Path) -> dict[str, Any]:
    before = load_run(before_dir)
    after = load_run(after_dir) if after_dir else None
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "before": run_summary(before),
        "after": run_summary(after) if after else None,
        "note": DISCLAIMER,
    }
    (out_dir / "summary.json").write_text(json_text(summary) + "\n", encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(before, after), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(before, after), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        summary = generate_report(arguments.before, arguments.after, arguments.out_dir)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "before_cases": summary["before"]["completed_cases"],
                "after_cases": summary["after"]["completed_cases"] if summary["after"] else None,
                "report": str((arguments.out_dir / "report.html").resolve()),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
