"""Editorial dataset/ledger tests use a fake transport and never bill requests."""

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from predatory_beavers.modules.mascot.config import MascotConfig
from predatory_beavers.modules.mascot.gateway import (
    CompletionResult,
    CompletionUsage,
    GatewayDeadlineExceeded,
    GatewayUnavailable,
)
from predatory_beavers.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def import_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = import_script("build_bobrik_column_cases")
runner = import_script("evaluate_bobrik_column")


def configuration():
    config = MascotConfig.from_settings(Settings(_env_file=None, env="test"))
    return replace(
        config,
        llm_api_key="not-real",
        llm_chat_model="fake-editor",
        max_output_tokens=4000,
        deadline_seconds=120,
    )


class FakeGateway:
    def __init__(self, content="not json", error=None):
        self.content = content
        self.error = error
        self.requests = []

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return CompletionResult(
            content=self.content,
            model="fake-editor",
            usage=CompletionUsage(input_tokens=100, output_tokens=20),
        )


def test_dataset_is_reproducible_balanced_and_editorial_specific():
    cases = builder.build_cases()
    fixture = runner.load_cases(ROOT / "tests" / "fixtures" / "bobrik_column_cases.jsonl")
    assert cases == fixture
    assert len(cases) == len({case["id"] for case in cases}) == 1000
    assert len({case["base_id"] for case in cases}) == 200
    assert set(case["product_key"] for case in cases) == {"weekly_column"}
    assert set(case["fixture_kind"] for case in cases) == {"synthetic"}
    assert {family: sum(c["category"] == family for c in cases) for family in builder.FAMILIES} == {
        family: 50 for family in builder.FAMILIES
    }
    assert max(len(case["editorial_brief"]) for case in cases) <= 1500
    for case in cases:
        sheet = runner.build_sheet(case)
        assert sheet.week_start == case["week_start"]
        assert sheet.team_scope == case["scope"]["team_scope"]
        assert sheet.fingerprint


def test_case_loader_rejects_duplicate_and_chat_payloads(tmp_path):
    case = builder.build_cases()[0]
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case) + "\n" + json.dumps(case), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        runner.load_cases(path)
    path.write_text(json.dumps(dict(case, product_key="chat")), encoding="utf-8")
    with pytest.raises(ValueError, match="weekly_column"):
        runner.load_cases(path)
    path.write_text(json.dumps(dict(case, editorial_brief="x" * 1501)), encoding="utf-8")
    with pytest.raises(ValueError, match="production limit"):
        runner.load_cases(path)


@pytest.mark.asyncio
async def test_actual_production_generation_path_records_invalid_json_and_usage():
    case = builder.build_cases()[0]
    sheet = runner.build_sheet(case).model_dump(mode="json")
    gateway = FakeGateway()
    row = await runner.evaluate_case(case, sheet, configuration(), gateway)
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request["max_tokens"] == 4000
    assert "frozen_fact_sheet" in request["user"]
    assert row["guard_outcome"] == "invalid_model_response"
    assert row["raw_content"] == "not json"
    assert row["usage"]["input_tokens"] == 100
    assert row["auto_published"] is False


@pytest.mark.asyncio
async def test_gateway_failure_is_not_retried_and_resume_skips_unknown_outcome(tmp_path):
    cases = builder.build_cases()[:3]
    sheets = {case["id"]: runner.build_sheet(case).model_dump(mode="json") for case in cases}
    gateway = FakeGateway(error=GatewayUnavailable())
    summary = await runner.run_cases(cases[:1], sheets, configuration(), gateway, tmp_path)
    assert len(gateway.requests) == 1
    assert summary["gateway_errors"] == {"llm_unavailable": 1}
    with (tmp_path / "attempts.jsonl").open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps({"case_id": cases[1]["id"]}) + "\n")
    summary = await runner.run_cases(cases, sheets, configuration(), gateway, tmp_path)
    assert len(gateway.requests) == 2
    assert summary["completed_cases"] == 2
    assert summary["unknown_outcomes_skipped_on_resume"] == [cases[1]["id"]]


@pytest.mark.asyncio
async def test_repeated_timeouts_open_runner_circuit_without_retrying_paid_attempts(tmp_path):
    cases = builder.build_cases()[:5]
    sheets = {case["id"]: runner.build_sheet(case).model_dump(mode="json") for case in cases}
    gateway = FakeGateway(error=GatewayDeadlineExceeded())
    summary = await runner.run_cases(
        cases,
        sheets,
        configuration(),
        gateway,
        tmp_path,
        concurrency=1,
        max_consecutive_errors=2,
    )
    assert len(gateway.requests) == 2
    assert summary["completed_cases"] == 2
    assert summary["gateway_errors"] == {"deadline_exceeded": 2}
    assert summary["stopped_after_gateway_errors"] is True
    assert summary["usage_unreported_cases"] == 2


def test_snapshot_pins_case_content_and_facts_before_any_paid_call(tmp_path):
    cases = builder.build_cases()[:1]
    config = configuration()
    sheets, manifest = runner.prepare_snapshot(tmp_path, cases, config, resume=False)
    resumed, _ = runner.prepare_snapshot(tmp_path, cases, config, resume=True)
    assert resumed == sheets
    assert manifest["publication_attempts"] == 0
    with pytest.raises(ValueError, match="changed"):
        runner.prepare_snapshot(
            tmp_path, [dict(cases[0], editorial_brief="changed")], config, resume=True
        )
    stored = tmp_path / "fact-sheets.json"
    frozen = json.loads(stored.read_text(encoding="utf-8"))
    frozen[cases[0]["id"]]["as_of"] = "changed"
    stored.write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(ValueError, match="FactSheets changed"):
        runner.prepare_snapshot(tmp_path, cases, config, resume=True)
