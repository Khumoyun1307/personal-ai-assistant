"""Immutable prompt inventory and historical lookup; synthetic/static data only."""

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest

from assistant.agent import prompt_registry
from assistant.agent.draft_owner_schemas import OwnerDraftResult, OwnerRevisionResult
from assistant.agent.draft_schemas import DraftResult
from assistant.agent.monitor_schemas import MonitorVerdicts
from assistant.agent.schemas import output_model_for
from assistant.agent.triage_schemas import TriageResult

ROOT = Path(__file__).resolve().parents[2]
# Retain the initial inventory even when a version loses its last static caller.
HISTORICAL = {
    *(f"owner_request.v{number}" for number in range(1, 11)),
    *(f"summarize.v{number}" for number in range(1, 8)),
    *(f"draft_owner.v{number}" for number in range(1, 4)),
    "draft_reply.v1",
    "triage.v1",
    "evaluate_monitor.v1",
}
# Deliberately nonexistent labels in report-comparison rejection tests, not assets.
SYNTHETIC_COMPARISONS = {
    "tests/unit/test_eval_triage.py": {("triage", "v2")},
    "tests/unit/test_eval_monitors.py": {("evaluate_monitor", "v2")},
}


def test_every_prompt_matches_manifest_and_registry() -> None:
    manifest = prompt_registry.manifest()
    assert manifest.keys() >= HISTORICAL
    expected_paths = set()
    for label, asset in manifest.items():
        name, version = label.rsplit(".", 1)
        path = prompt_registry.asset_path(name, version)
        expected_paths.add(path)
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == asset.sha256
        assert prompt_registry.load_bytes(name, version) == raw
        assert prompt_registry.load(name, version).text == raw.decode("utf-8").strip()
        assert asset.active == (prompt_registry.ACTIVE.get(name) == version)
        assert (path.parent == prompt_registry.PROMPT_DIR) == asset.active
        assert bool(asset.structured_outputs) == (name != "owner_request")
        for record in asset.eval_records:
            assert record.startswith("docs/evals/")
            assert (ROOT / record).is_file()
            report = json.loads((ROOT / record).read_text(encoding="utf-8"))
            assert label in report["prompts"]
    actual_paths = set(prompt_registry.PROMPT_DIR.rglob("*.v*.md"))
    assert actual_paths == expected_paths
    for name, version in prompt_registry.ACTIVE.items():
        assert prompt_registry.active(name).label == f"{name}.{version}"


def test_static_prompt_references_cannot_outlive_manifest_entries() -> None:
    families = "|".join(map(re.escape, prompt_registry.ACTIVE))
    labels = re.compile(rf"\b(?:{families})\.v[1-9][0-9]*\b")
    referenced: set[str] = set()
    for directory in ("src", "tests", "evals", "docs/evals"):
        for path in (ROOT / directory).rglob("*"):
            if path.suffix not in {".py", ".json", ".jsonl", ".md"}:
                continue
            text = path.read_text(encoding="utf-8")
            found = set(labels.findall(text))
            synthetic = {
                f"{name}.{version}"
                for name, version in SYNTHETIC_COMPARISONS.get(
                    path.relative_to(ROOT).as_posix(), set()
                )
            }
            referenced.update(found - synthetic)
            if path.suffix != ".py":
                continue
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "load" or len(node.args) < 2:
                    continue
                name, version = node.args[:2]
                if (
                    isinstance(name, ast.Constant)
                    and isinstance(version, ast.Constant)
                    and name.value in prompt_registry.ACTIVE
                    and isinstance(version.value, str)
                ):
                    referenced.add(f"{name.value}.{version.value}")
    assert referenced <= prompt_registry.manifest().keys()


def test_manifest_schema_pairings_match_runtime_models() -> None:
    manifest = prompt_registry.manifest()
    for label, asset in manifest.items():
        name, version = label.rsplit(".", 1)
        if name == "summarize":
            model = output_model_for(prompt_registry.load(name, version))
            assert asset.structured_outputs == [f"{model.__module__}.{model.__name__}"]
    expected = {
        "draft_reply": [DraftResult],
        "draft_owner": [OwnerDraftResult, OwnerRevisionResult],
        "triage": [TriageResult],
        "evaluate_monitor": [MonitorVerdicts],
    }
    for name, models in expected.items():
        label = prompt_registry.active(name).label
        assert manifest[label].structured_outputs == [
            f"{model.__module__}.{model.__name__}" for model in models
        ]


def test_archived_prompt_tampering_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    label = "owner_request.v1"
    asset = prompt_registry.manifest()[label]
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / f"{label}.md").write_bytes(b"changed synthetic prompt")
    monkeypatch.setattr(prompt_registry, "PROMPT_DIR", tmp_path)
    monkeypatch.setattr(prompt_registry, "manifest", lambda: {label: asset})
    with pytest.raises(ValueError, match="hash mismatch"):
        prompt_registry.load_bytes("owner_request", "v1")


@pytest.mark.parametrize("name", ["../owner_request", "unknown"])
def test_unknown_prompt_does_not_fall_back_to_arbitrary_files(name: str) -> None:
    with pytest.raises(KeyError):
        prompt_registry.load_bytes(name, "v1")
