"""Checkpointed live evaluation of Bobrik, using only the isolated public corpus.

Run with the backend venv. The runner deliberately makes no automatic provider
retries. Results are observations plus heuristic review flags, not a quality score.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from predatory_beavers.modules.mascot import agent, knowledge, validation
from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import GatewayError, HttpLLMGateway, LLMGateway
from predatory_beavers.modules.mascot.worker import MascotWorker

ROOT = Path(__file__).resolve().parents[1]
ISOLATED_DATABASE = ROOT / "data" / "bobrik-local.db"
SCHEMA_VERSION = 1


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Reject malformed/duplicate cases before any paid request can happen."""
    cases: list[dict[str, Any]] = []
    ids: set[str] = set()
    questions: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON at case line {line_number}") from error
        if not isinstance(case, dict):
            raise ValueError(f"Case line {line_number} must be an object")
        for field in ("id", "question", "category", "tone"):
            value = case.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Case line {line_number}: missing string {field}")
        if len(case["question"]) > 1000:
            raise ValueError(f"Case {case['id']}: question exceeds production limit")
        normalized_question = " ".join(case["question"].casefold().split())
        if case["id"] in ids or normalized_question in questions:
            raise ValueError(f"Duplicate case id or question at line {line_number}")
        context = case.get("context", {})
        checks = case.get("checks", {})
        if not isinstance(context, dict) or not isinstance(checks, dict):
            raise ValueError(f"Case {case['id']}: context/checks must be objects")
        for field in ("requires_any", "forbids_any"):
            terms = checks.get(field, [])
            if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
                raise ValueError(f"Case {case['id']}: checks.{field} must be strings")
        ids.add(case["id"])
        questions.add(normalized_question)
        cases.append(case)
    if not cases:
        raise ValueError("No cases found")
    return cases


def document_json(doc: knowledge.KnowledgeDoc) -> dict[str, Any]:
    result = asdict(doc)
    if doc.starts_at is not None:
        result["starts_at"] = doc.starts_at.isoformat()
    return result


def document_from_json(value: dict[str, Any]) -> knowledge.KnowledgeDoc:
    fields = dict(value)
    if fields.get("starts_at"):
        fields["starts_at"] = datetime.fromisoformat(fields["starts_at"])
    return knowledge.KnowledgeDoc(**fields)


async def read_public_corpus(
    database: Path = ISOLATED_DATABASE, *, now: datetime | None = None
) -> list[knowledge.KnowledgeDoc]:
    """SQLite's mode=ro prevents every DB write, including accidental mutations."""
    resolved, isolated = await asyncio.gather(
        asyncio.to_thread(database.resolve),
        asyncio.to_thread(ISOLATED_DATABASE.resolve),
    )
    if resolved != isolated:
        raise ValueError("Only data/bobrik-local.db can be evaluated")
    if not resolved.is_file():
        raise ValueError("Initialize isolated data/bobrik-local.db first")
    # Do not use the app engine helper: this connection must stay read-only.
    url = f"sqlite+aiosqlite:///{resolved.as_uri()}?mode=ro&uri=true"
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with async_sessionmaker(engine)() as session:
            return await knowledge.build_corpus(session, now=now)
    finally:
        await engine.dispose()


def prepare_snapshot(
    out_dir: Path,
    cases_path: Path,
    config: MascotConfig,
    documents: list[knowledge.KnowledgeDoc],
    *,
    resume: bool,
    clock: datetime | None = None,
) -> tuple[list[knowledge.KnowledgeDoc], datetime, dict[str, Any]]:
    """Resume uses the original corpus and clock; changed prompts cannot mix runs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    snapshot_path = out_dir / "corpus.json"
    source_modules = (agent, knowledge, validation, sys.modules[MascotWorker.__module__])
    source_text = "\n\n".join(inspect.getsource(module) for module in source_modules)
    identity = {
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "system_prompt_sha256": digest(agent.SYSTEM_PROMPT),
        "pipeline_source_sha256": digest(source_text),
        "harness_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": config.llm_chat_model,
        "provider": config.llm_provider,
        "provider_base_url": config.llm_base_url,
        "max_output_tokens": config.max_output_tokens,
        "deadline_seconds": config.deadline_seconds,
    }
    if manifest_path.exists():
        if not resume:
            raise ValueError("Output already exists; choose a new directory or --resume")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(manifest.get(field) != value for field, value in identity.items()):
            raise ValueError("Resume rejected: cases, model, prompt, or pipeline changed")
        serialized = json.loads(snapshot_path.read_text(encoding="utf-8"))
        frozen_docs = [document_from_json(doc) for doc in serialized]
        if knowledge.corpus_hash(frozen_docs) != manifest["corpus_sha256"]:
            raise ValueError("Resume rejected: corpus snapshot hash changed")
        return frozen_docs, datetime.fromisoformat(manifest["clock_utc"]), manifest
    if resume:
        raise ValueError("--resume requires an existing manifest")
    if any(out_dir.iterdir()):
        raise ValueError("Output directory must be empty for a new evaluation")
    current = clock or datetime.now(UTC)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "clock_utc": current.isoformat(),
        "database": "data/bobrik-local.db (read-only public corpus snapshot)",
        "corpus_sha256": knowledge.corpus_hash(documents),
        "corpus_document_count": len(documents),
        "automatic_retries": 0,
        "temperature": 0.2,
        **identity,
    }
    write_json(snapshot_path, [document_json(doc) for doc in documents])
    write_json(
        out_dir / "prompt-source.json",
        {
            "system_prompt": agent.SYSTEM_PROMPT,
            "pipeline_source": source_text,
            "note": "Public source and prompt only; no keys or raw environment values.",
        },
    )
    write_json(manifest_path, manifest)
    return documents, current, manifest


def review_flags(case: dict[str, Any], answer: str, sources: list[Any]) -> list[str]:
    """Heuristics select answers for human review; flags never mean proven failure."""
    flags: list[str] = []
    checks = case.get("checks", {})
    normalized = knowledge.normalize_text(answer)
    required = checks.get("requires_any", [])
    if required and not any(knowledge.normalize_text(term) in normalized for term in required):
        flags.append("expected_term_missing")
    if any(knowledge.normalize_text(term) in normalized for term in checks.get("forbids_any", [])):
        flags.append("forbidden_term_present")
    if checks.get("source_required") and not sources:
        flags.append("expected_citation_missing")
    if checks.get("expects_clarification") and not re.search(
        r"\?|уточн|како[йеи]|кого|какую|каком", normalized
    ):
        flags.append("expected_clarification_missing")
    if re.search(r"\b(?:бля\w*|хуй\w*|хуя\w*|пизд\w*|еба\w*|ебну\w*)\b", normalized):
        flags.append("answer_profanity")
    if len(answer) > 1200:
        flags.append("long_answer")
    return flags


async def evaluate_case(
    case: dict[str, Any],
    documents: list[knowledge.KnowledgeDoc],
    clock: datetime,
    config: MascotConfig,
    gateway: LLMGateway,
) -> dict[str, Any]:
    started = time.monotonic()
    context_team = case.get("context", {}).get("team")
    passages = knowledge.retrieve(
        case["question"],
        documents,
        now=clock,
        team_category=context_team if isinstance(context_team, str) else None,
    )
    user_prompt = agent.build_user_prompt(case["question"], passages, now=clock)
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "case": case,
        "started_at": datetime.now(UTC).isoformat(),
        "clock_utc": clock.isoformat(),
        "retrieved_sources": [document_json(doc) for doc in passages],
        "user_prompt_sha256": digest(user_prompt),
        "raw_content": None,
        "parsed": None,
        "displayed_answer": None,
        "citations": [],
        "guard_outcome": None,
        "gateway_error": None,
        "provider_attempted": False,
        "provider_model": None,
        "usage": None,
        "review_flags": [],
    }
    if agent.is_greeting(case["question"]):
        row.update(displayed_answer=agent.GREETING_REPLY, guard_outcome="greeting_shortcut")
    else:
        row["provider_attempted"] = True

        async def not_cancelled() -> bool:
            return False

        try:
            result = await gateway.complete(
                system=agent.SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=config.max_output_tokens,
                temperature=0.2,
                deadline=datetime.now(UTC) + timedelta(seconds=config.deadline_seconds),
                cancel_check=not_cancelled,
                session_id=f"bobrik-eval-{digest(case['id'])[:24]}",
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
            row["raw_content"] = result.content
            row["provider_model"] = result.model
            row["usage"] = asdict(result.usage)
            reply = agent.parse_reply(result.content)
            row["parsed"] = asdict(reply) if reply is not None else None
            cited, valid = MascotWorker._resolve_citations(
                [] if reply is None else reply.source_ids,
                passages,
            )
            if reply is None or not valid:
                support = validation.safety_fallback(case["question"])
                row["guard_outcome"] = (
                    "safety_fallback_invalid_response" if support else "invalid_model_response"
                )
                row["displayed_answer"] = support
            else:
                issues = validation.reply_grounding_issues(
                    reply.text, cited, question=case["question"], now=clock
                )
                row["grounding_issues"] = issues
                row["unsupported_numbers"] = [
                    issue.removeprefix("unsupported_number:")
                    for issue in issues
                    if issue.startswith("unsupported_number:")
                ]
                if issues:
                    support = validation.safety_fallback(case["question"])
                    row.update(
                        displayed_answer=support or agent.SAFE_FALLBACK_REPLY,
                        citations=[] if support else agent.SAFE_FALLBACK_CITATIONS,
                        guard_outcome=(
                            "safety_fallback_grounding"
                            if support
                            else "unsupported_numbers_fallback"
                        ),
                    )
                else:
                    row.update(
                        displayed_answer=reply.text,
                        citations=[{"title": doc.title, "url": doc.url} for doc in cited],
                        guard_outcome="accepted",
                    )
                    if not passages:
                        row["review_flags"].append("no_retrieved_sources")
                    if not cited:
                        row["review_flags"].append("no_cited_sources")
    if row["displayed_answer"] is not None:
        row["review_flags"].extend(review_flags(case, row["displayed_answer"], row["citations"]))
    row["latency_ms"] = round((time.monotonic() - started) * 1000)
    row["finished_at"] = datetime.now(UTC).isoformat()
    return row


def read_checkpoint(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            message = f"Damaged checkpoint line {line_number}; inspect before resuming"
            raise ValueError(message) from error
        if not isinstance(row, dict) or not isinstance(row.get("case", {}).get("id"), str):
            raise ValueError(f"Invalid checkpoint line {line_number}")
        rows.append(row)
    ids = [row["case"]["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate ids in checkpoint")
    return rows


def summarize(rows: list[dict[str, Any]], total_cases: int) -> dict[str, Any]:
    latencies = sorted(row["latency_ms"] for row in rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "note": "Heuristic flags require human review; they are not accuracy scores.",
        "selected_cases": total_cases,
        "completed_cases": len(rows),
        "provider_attempts": sum(row["provider_attempted"] for row in rows),
        "guard_outcomes": dict(Counter(row["guard_outcome"] for row in rows)),
        "gateway_errors": dict(
            Counter(row["gateway_error"]["code"] for row in rows if row["gateway_error"])
        ),
        "review_flags": dict(Counter(flag for row in rows for flag in row["review_flags"])),
        "categories": dict(Counter(row["case"]["category"] for row in rows)),
        "tones": dict(Counter(row["case"]["tone"] for row in rows)),
        "input_tokens": sum((row.get("usage") or {}).get("input_tokens") or 0 for row in rows),
        "output_tokens": sum((row.get("usage") or {}).get("output_tokens") or 0 for row in rows),
        "usage_unreported_cases": sum(
            row["provider_attempted"] and row["usage"] is None for row in rows
        ),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
        "latency_p95_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        if latencies
        else None,
    }


async def run_cases(
    cases: list[dict[str, Any]],
    documents: list[knowledge.KnowledgeDoc],
    clock: datetime,
    config: MascotConfig,
    gateway: LLMGateway,
    out_dir: Path,
    *,
    concurrency: int = 3,
    max_consecutive_errors: int = 5,
) -> dict[str, Any]:
    checkpoint = out_dir / "responses.jsonl"
    rows = read_checkpoint(checkpoint)
    completed = {row["case"]["id"] for row in rows}
    attempts_path = out_dir / "attempts.jsonl"
    attempted: set[str] = set()
    if attempts_path.exists():
        for line in attempts_path.read_text(encoding="utf-8").splitlines():
            try:
                attempt = json.loads(line)
                case_id = attempt["case_id"]
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise ValueError("Damaged attempts ledger; inspect before resuming") from error
            if not isinstance(case_id, str) or case_id in attempted:
                raise ValueError("Invalid or duplicate id in attempts ledger")
            attempted.add(case_id)
    unknown_before_resume = sorted(attempted - completed)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for case in cases:
        if case["id"] not in completed | attempted:
            queue.put_nowait(case)
    consecutive_errors = 0
    stop = asyncio.Event()
    with (
        checkpoint.open("a", encoding="utf-8", newline="\n") as output,
        attempts_path.open("a", encoding="utf-8", newline="\n") as attempts,
    ):

        async def worker() -> None:
            nonlocal consecutive_errors
            while not stop.is_set():
                try:
                    case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                # Durable intent comes before a possibly billed call. A process
                # crash cannot cause --resume to silently bill the same case.
                attempts.write(
                    json.dumps(
                        {
                            "case_id": case["id"],
                            "started_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    + "\n"
                )
                attempts.flush()
                row = await evaluate_case(case, documents, clock, config, gateway)
                # No await between checkpoint write and append: event-loop atomic.
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                rows.append(row)
                consecutive_errors = consecutive_errors + 1 if row["gateway_error"] else 0
                if consecutive_errors >= max_consecutive_errors:
                    stop.set()
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
    summary["stopped_after_gateway_errors"] = stop.is_set()
    summary["unknown_outcomes_skipped_on_resume"] = unknown_before_resume
    write_json(out_dir / "summary.json", summary)
    return summary


async def async_main(arguments: argparse.Namespace) -> None:
    from bobrik_local import prepare_environment

    from predatory_beavers.settings import get_settings

    cases_path = arguments.cases.resolve()
    cases = load_cases(cases_path)
    if arguments.case_ids:
        requested = set(arguments.case_ids.split(","))
        if requested - {case["id"] for case in cases}:
            raise ValueError("Unknown --case-ids value")
        cases = [case for case in cases if case["id"] in requested]
    if arguments.limit is not None:
        cases = cases[: arguments.limit]
    clock = datetime.fromisoformat(arguments.clock_utc) if arguments.clock_utc else None
    if clock is not None and clock.tzinfo is None:
        raise ValueError("--clock-utc must include a timezone offset")
    prepare_environment()
    get_settings.cache_clear()
    config = MascotConfig.from_settings(get_settings())
    if not config.llm_api_key:
        raise ValueError("Configure the local Bobrik provider key first")
    documents = [] if arguments.resume else await read_public_corpus(now=clock)
    documents, clock, _ = prepare_snapshot(
        arguments.out_dir.resolve(),
        cases_path,
        config,
        documents,
        resume=arguments.resume,
        clock=clock,
    )
    summary = await run_cases(
        cases,
        documents,
        clock,
        config,
        HttpLLMGateway(config),
        arguments.out_dir.resolve(),
        concurrency=arguments.concurrency,
    )
    print(json.dumps(summary, ensure_ascii=True, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-ids", help="Comma-separated case ids")
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=3)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--clock-utc", help="Pinned ISO timestamp for paired evaluations")
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit <= 0:
        parser.error("--limit must be positive")
    try:
        asyncio.run(async_main(arguments))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
