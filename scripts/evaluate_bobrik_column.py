"""Checkpointed paid evaluation of the production weekly-column pipeline.

Inputs are explicitly fictional public-data-shaped fixtures, never database writes.
Transport errors are recorded, not automatically retried. Passing deterministic
checks is not a semantic accuracy score; raw drafts remain available for review.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from predatory_beavers.modules.editorial import facts, generation
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import GatewayError, HttpLLMGateway, LLMGateway
from predatory_beavers.modules.mascot.knowledge import KnowledgeDoc

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "fixtures" / "bobrik_column_cases.jsonl"
SCHEMA_VERSION = 1


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid case JSON at line {line_number}") from error
        if not isinstance(case, dict):
            raise ValueError(f"Case {line_number} must be an object")
        for field in (
            "id",
            "base_id",
            "category",
            "brief_mode",
            "editorial_brief",
            "week_start",
            "clock_utc",
        ):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f"Case {line_number}: invalid {field}")
        if case["id"] in seen:
            raise ValueError(f"Duplicate case id: {case['id']}")
        if case.get("product_key") != "weekly_column" or case.get("fixture_kind") != "synthetic":
            raise ValueError("Only explicitly synthetic weekly_column cases are supported")
        if len(case["editorial_brief"]) > 1500:
            raise ValueError(f"Case {case['id']}: brief exceeds production limit")
        week_start = date.fromisoformat(case["week_start"])
        clock = datetime.fromisoformat(case["clock_utc"])
        if week_start.weekday() != 0 or clock.tzinfo is None:
            raise ValueError("A Monday scope and timezone-aware pinned clock are required")
        scope = case.get("scope")
        if not isinstance(scope, dict) or scope.get("team_scope") not in {"men", "women", "both"}:
            raise ValueError("Invalid team scope")
        if scope.get("discipline") not in {"5x5", "3x3", "phygital", "all"}:
            raise ValueError("Invalid discipline scope")
        if not isinstance(case.get("documents"), list) or not isinstance(
            case.get("expect", {}), dict
        ):
            raise ValueError("Invalid documents/expectations")
        deserialize_documents(case["documents"])
        seen.add(case["id"])
        cases.append(case)
    if not cases:
        raise ValueError("No cases found")
    return cases


def deserialize_documents(values: list[dict[str, Any]]) -> list[KnowledgeDoc]:
    result = []
    for value in values:
        fields = dict(value)
        if fields.get("starts_at"):
            fields["starts_at"] = datetime.fromisoformat(fields["starts_at"])
        result.append(KnowledgeDoc(**fields))
    return result


def build_sheet(case: dict[str, Any]) -> facts.FactSheet:
    return facts.build_fact_sheet(
        deserialize_documents(case["documents"]),
        date.fromisoformat(case["week_start"]),
        team_scope=case["scope"]["team_scope"],
        discipline=case["scope"]["discipline"],
        now=datetime.fromisoformat(case["clock_utc"]),
    )


def pipeline_source() -> dict[str, str]:
    from predatory_beavers.modules.mascot import gateway

    return {module.__name__: inspect.getsource(module) for module in (facts, generation, gateway)}


def prepare_snapshot(
    out_dir: Path,
    cases: list[dict[str, Any]],
    config: MascotConfig,
    *,
    resume: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    source = pipeline_source()
    identity = {
        "cases_sha256": digest(cases),
        "pipeline_source_sha256": digest(source),
        "harness_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": config.llm_chat_model,
        "provider": config.llm_provider,
        "provider_base_url": config.llm_base_url,
        "max_output_tokens": config.max_output_tokens,
        "deadline_seconds": config.deadline_seconds,
    }
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        if not resume:
            raise ValueError("Output already exists; use a new directory or --resume")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(manifest.get(key) != value for key, value in identity.items()):
            raise ValueError("Resume rejected: input, model, pipeline or harness changed")
        snapshots = json.loads((out_dir / "fact-sheets.json").read_text(encoding="utf-8"))
        if digest(snapshots) != manifest["fact_sheets_sha256"]:
            raise ValueError("Resume rejected: frozen FactSheets changed")
        return snapshots, manifest
    if resume or any(out_dir.iterdir()):
        raise ValueError("A new run needs an empty directory; resume needs a manifest")
    snapshots = {case["id"]: build_sheet(case).model_dump(mode="json") for case in cases}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "product_key": "weekly_column",
        "fixture_kind": "synthetic",
        "created_at": datetime.now(UTC).isoformat(),
        "selected_cases": len(cases),
        "base_situations": len({case["base_id"] for case in cases}),
        "automatic_retries": 0,
        "database_writes": False,
        "publication_attempts": 0,
        "fact_sheets_sha256": digest(snapshots),
        **identity,
        "note": (
            "200 synthetic base situations x 5 editorial briefs. Checks are not semantic accuracy."
        ),
    }
    write_json(out_dir / "cases.json", cases)
    write_json(out_dir / "fact-sheets.json", snapshots)
    write_json(out_dir / "pipeline-source.json", source)
    write_json(manifest_path, manifest)
    return snapshots, manifest


def review_flags(case: dict[str, Any], rendered: str, checks: dict[str, Any] | None) -> list[str]:
    flags = []
    if checks:
        if not checks.get("passed"):
            flags.append("blocking_checks_failed")
        count = checks.get("word_count", 0)
        if not 700 <= count <= 1000:
            flags.append("outside_target_word_count")
    if any(
        term.casefold() in rendered.casefold()
        for term in case.get("expect", {}).get("forbids_any", [])
    ):
        flags.append("fixture_forbidden_text_present")
    if re.search(
        r"(?:<script|javascript:|evil\.example|ADMIN OVERRIDE|private\.person@)", rendered, re.I
    ):
        flags.append("untrusted_payload_present")
    if re.search(r"\b(?:бля\w*|хуй\w*|хуя\w*|пизд\w*|еба\w*)\b", rendered, re.I):
        flags.append("profanity_present")
    return flags


async def evaluate_case(
    case: dict[str, Any],
    sheet_json: dict[str, Any],
    config: MascotConfig,
    gateway: LLMGateway,
) -> dict[str, Any]:
    started = time.monotonic()
    sheet = facts.FactSheet.model_validate(sheet_json)
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "case": case,
        "fact_sheet": sheet_json,
        "started_at": datetime.now(UTC).isoformat(),
        "provider_attempted": True,
        "raw_content": None,
        "draft": None,
        "checks": None,
        "rendered_paragraphs": [],
        "guard_outcome": None,
        "gateway_error": None,
        "provider_model": None,
        "usage": None,
        "review_flags": [],
        "auto_published": False,
    }

    async def not_cancelled() -> bool:
        return False

    try:
        generated = await generation.generate_column(
            sheet,
            case["editorial_brief"],
            gateway,
            deadline=datetime.now(UTC) + timedelta(seconds=config.deadline_seconds),
            cancel_check=not_cancelled,
        )
    except GatewayError as error:
        row["gateway_error"] = {
            "code": error.code,
            "retryable": error.retryable,
            "outcome": "unknown",
            "automatic_retries": 0,
        }
        row["guard_outcome"] = "gateway_error"
    else:
        row.update(
            raw_content=generated.raw_content,
            provider_model=generated.model,
            usage=asdict(generated.usage),
        )
        if generated.draft is None:
            row["guard_outcome"] = "invalid_model_response"
        else:
            checks = generation.check_draft(generated.draft, sheet)
            row["draft"] = generated.draft.model_dump(mode="json")
            row["checks"] = checks.model_dump(mode="json")
            row["rendered_paragraphs"] = generation.render_paragraphs(generated.draft, sheet)
            row["guard_outcome"] = "passed_checks" if checks.passed else "blocked_checks"
            text = "\n".join(str(part) for part in row["rendered_paragraphs"])
            row["review_flags"] = review_flags(case, text, row["checks"])
    row["latency_ms"] = round((time.monotonic() - started) * 1000)
    row["finished_at"] = datetime.now(UTC).isoformat()
    return row


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Damaged ledger {path.name} line {line_number}; inspect before resume"
            ) from error
        if not isinstance(value, dict):
            raise ValueError("Ledger rows must be objects")
        rows.append(value)
    return rows


def summarize(rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    latencies = sorted(row["latency_ms"] for row in rows)
    return {
        "selected_cases": total,
        "completed_cases": len(rows),
        "fixture_kind": "synthetic",
        "base_situations": len({row["case"]["base_id"] for row in rows}),
        "provider_attempts": sum(row["provider_attempted"] for row in rows),
        "guard_outcomes": dict(Counter(row["guard_outcome"] for row in rows)),
        "gateway_errors": dict(
            Counter(row["gateway_error"]["code"] for row in rows if row["gateway_error"])
        ),
        "categories": dict(Counter(row["case"]["category"] for row in rows)),
        "brief_modes": dict(Counter(row["case"]["brief_mode"] for row in rows)),
        "check_issues": dict(
            Counter(issue for row in rows for issue in (row.get("checks") or {}).get("issues", []))
        ),
        "review_flags": dict(Counter(flag for row in rows for flag in row["review_flags"])),
        "input_tokens": sum((row.get("usage") or {}).get("input_tokens") or 0 for row in rows),
        "output_tokens": sum((row.get("usage") or {}).get("output_tokens") or 0 for row in rows),
        "usage_unreported_cases": sum(row["usage"] is None for row in rows),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies
        else None,
        "note": "Passing checks is not semantic accuracy. Human review is required.",
    }


async def run_cases(
    cases: list[dict[str, Any]],
    sheets: dict[str, dict[str, Any]],
    config: MascotConfig,
    gateway: LLMGateway,
    out_dir: Path,
    *,
    concurrency: int = 3,
    max_consecutive_errors: int = 5,
) -> dict[str, Any]:
    responses_path, attempts_path = out_dir / "responses.jsonl", out_dir / "attempts.jsonl"
    rows, attempts = read_ledger(responses_path), read_ledger(attempts_path)
    completed = [row.get("case", {}).get("id") for row in rows]
    attempted = [row.get("case_id") for row in attempts]
    for identities in (completed, attempted):
        if any(not isinstance(item, str) for item in identities) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("Missing or duplicate ledger case id")
    selected_ids = {case["id"] for case in cases}
    if set(completed + attempted) - selected_ids:
        raise ValueError("Ledger contains cases outside the selected snapshot")
    unknown = sorted(str(case_id) for case_id in set(attempted) - set(completed))
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for case in cases:
        if case["id"] not in set(completed + attempted):
            queue.put_nowait(case)
    stopped = asyncio.Event()
    consecutive_errors = 0
    with (
        responses_path.open("a", encoding="utf-8", newline="\n") as output,
        attempts_path.open("a", encoding="utf-8", newline="\n") as ledger,
    ):

        async def worker() -> None:
            nonlocal consecutive_errors
            while not stopped.is_set():
                try:
                    case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                ledger.write(
                    encoded({"case_id": case["id"], "started_at": datetime.now(UTC).isoformat()})
                    + "\n"
                )
                ledger.flush()
                os.fsync(ledger.fileno())
                row = await evaluate_case(case, sheets[case["id"]], config, gateway)
                output.write(encoded(row) + "\n")
                output.flush()
                os.fsync(output.fileno())
                rows.append(row)
                consecutive_errors = consecutive_errors + 1 if row["gateway_error"] else 0
                if consecutive_errors >= max_consecutive_errors:
                    stopped.set()
                print(
                    json.dumps(
                        {
                            "completed": len(rows),
                            "selected": len(cases),
                            "case_id": case["id"],
                            "outcome": row["guard_outcome"],
                            "latency_ms": row["latency_ms"],
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    summary = summarize(rows, len(cases))
    summary.update(
        stopped_after_gateway_errors=stopped.is_set(), unknown_outcomes_skipped_on_resume=unknown
    )
    write_json(out_dir / "summary.json", summary)
    return summary


async def async_main(arguments: argparse.Namespace) -> None:
    from bobrik_local import prepare_environment

    from predatory_beavers.settings import get_settings

    cases = load_cases(arguments.cases.resolve())
    if arguments.case_ids:
        requested = set(arguments.case_ids.split(","))
        if requested - {case["id"] for case in cases}:
            raise ValueError("Unknown --case-ids value")
        cases = [case for case in cases if case["id"] in requested]
    if arguments.limit is not None:
        cases = cases[: arguments.limit]
    prepare_environment()
    get_settings.cache_clear()
    config = MascotConfig.from_settings(get_settings())
    config = replace(
        config, llm_chat_model=config.llm_editor_model, max_output_tokens=4000, deadline_seconds=120
    )
    if not config.llm_api_key:
        raise ValueError("Configure the local paid Bobrik provider key first")
    sheets, _ = prepare_snapshot(
        arguments.out_dir.resolve(), cases, config, resume=arguments.resume
    )
    summary = await run_cases(
        cases,
        sheets,
        config,
        HttpLLMGateway(config, max_content_chars=36_000),
        arguments.out_dir.resolve(),
        concurrency=arguments.concurrency,
    )
    print(json.dumps(summary, ensure_ascii=True, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DATASET)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-ids")
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=3)
    parser.add_argument("--resume", action="store_true")
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit <= 0:
        parser.error("--limit must be positive")
    try:
        asyncio.run(async_main(arguments))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
