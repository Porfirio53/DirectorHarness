#!/usr/bin/env python3
"""Offline-migrate MCP-Persona full-run metadata and audit comparability."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from openharness.rehearsal.mcp_persona_rehearsal import (
    VERIFIED52_PROTOCOL_ID,
    VERIFIED52_WRITER_ARM,
    verified52_experiment_arm,
)
from openharness.rehearsal.mcp_persona_runtime import execution_runtime_fingerprint
from scripts.run_mcp_persona_week1 import summarize_existing_output


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def normalize_external_environment(value: Any) -> dict[str, Any]:
    """Convert legacy optional-evaluator environment metadata to a clear status."""

    if isinstance(value, Mapping) and value.get("status") in {
        "not_requested",
        "missing",
        "failed",
        "captured",
    }:
        return dict(value)
    if not isinstance(value, Mapping) or not value:
        return {
            "role": "optional_official_evaluator_probe",
            "status": "not_requested",
            "captured": False,
            "migrated_from_legacy": True,
        }

    details: Any = value.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = None
    if value.get("returncode") == 0 and isinstance(details, Mapping):
        return {
            "role": "optional_official_evaluator_probe",
            "status": "captured",
            "captured": True,
            "python": value.get("python"),
            "python_version": details.get("python"),
            "packages": details.get("packages"),
            "migrated_from_legacy": True,
        }
    return {
        "role": "optional_official_evaluator_probe",
        "status": "failed",
        "captured": False,
        "python": value.get("python"),
        "returncode": value.get("returncode"),
        "migrated_from_legacy": True,
    }


def migrate_output_metadata(output_dir: Path) -> dict[str, Any]:
    """Apply the schema-v2 runtime and arm-neutral readiness metadata offline."""

    output_dir = output_dir.resolve()
    run_config_path = output_dir / "run-config.json"
    manifest_path = output_dir / "version-manifest.json"
    run_config = _read_json(run_config_path)
    manifest = _read_json(manifest_path)

    mcp_persona = manifest.get("mcp_persona")
    if not isinstance(mcp_persona, dict):
        raise ValueError(f"{manifest_path} has no MCP-Persona metadata")
    mcp_persona["external_environment"] = normalize_external_environment(
        mcp_persona.get("external_environment")
    )
    fingerprint = execution_runtime_fingerprint(manifest)
    manifest["execution_runtime"] = fingerprint
    manifest["provenance_policy"] = {
        "source_metadata_recorded": True,
        "source_lock_enforced": False,
        "optional_evaluator_environment_part_of_actor_runtime": False,
    }
    _write_json(manifest_path, manifest)

    previous_fingerprint = run_config.get("runtime_fingerprint_sha256")
    if previous_fingerprint and run_config.get("runtime_fingerprint_schema") != 2:
        run_config["legacy_runtime_fingerprint_sha256"] = previous_fingerprint
    arm = verified52_experiment_arm(run_config)
    if arm is None:
        raise ValueError(f"{run_config_path} is not an exact Verified52 arm")
    run_config["protocol_id"] = VERIFIED52_PROTOCOL_ID
    run_config["experiment_arm"] = arm
    run_config["runtime_fingerprint_schema"] = fingerprint["schema_version"]
    run_config["runtime_fingerprint_scope"] = fingerprint["scope"]
    run_config["runtime_fingerprint_sha256"] = fingerprint["sha256"]
    run_config["execution_core_source_sha256"] = fingerprint[
        "execution_core_source_sha256"
    ]
    run_config["source_lock_enforced"] = False
    _write_json(run_config_path, run_config)

    baseline_summary = summarize_existing_output(output_dir)
    semantic_summary_path = output_dir / "semantic-summary.json"
    if semantic_summary_path.is_file():
        semantic_summary = _read_json(semantic_summary_path)
        semantic_summary["dataset_id"] = run_config["dataset_id"]
        semantic_summary["protocol_id"] = VERIFIED52_PROTOCOL_ID
        semantic_summary["experiment_arm"] = arm
        _write_json(semantic_summary_path, semantic_summary)

    return {
        "output_dir": str(output_dir),
        "protocol_id": VERIFIED52_PROTOCOL_ID,
        "experiment_arm": arm,
        "runtime_fingerprint_scope": fingerprint["scope"],
        "runtime_fingerprint_sha256": fingerprint["sha256"],
        "baseline_ready": baseline_summary.get("baseline_ready"),
    }


def _runner_sha256(run_config: Mapping[str, Any]) -> str | None:
    hashes = run_config.get("source_sha256")
    if not isinstance(hashes, Mapping):
        return None
    for name, digest in hashes.items():
        if str(name).replace("\\", "/").endswith(
            "scripts/run_mcp_persona_week1.py"
        ):
            return str(digest)
    return None


def _recorded_commit_runner_matches(run_config: Mapping[str, Any]) -> bool:
    metadata = run_config.get("openharness_git")
    if not isinstance(metadata, Mapping):
        return False
    root = Path(str(metadata.get("root") or ""))
    commit = str(metadata.get("commit") or "")
    expected = _runner_sha256(run_config)
    if not root.is_dir() or not commit or not expected:
        return False
    completed = subprocess.run(
        ["git", "show", f"{commit}:scripts/run_mcp_persona_week1.py"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return (
        completed.returncode == 0
        and hashlib.sha256(completed.stdout).hexdigest() == expected
    )


def _step_summary(output_dir: Path) -> dict[str, Any] | None:
    for name in ("baseline2-summary.json", "rehearsal-step-summary.json"):
        path = output_dir / name
        if path.is_file():
            return _read_json(path)
    return None


def build_comparability_audit(
    original_output: Path,
    writer_output: Path,
) -> dict[str, Any]:
    """Classify what the two historical full runs can and cannot support."""

    original_output = original_output.resolve()
    writer_output = writer_output.resolve()
    original_config = _read_json(original_output / "run-config.json")
    writer_config = _read_json(writer_output / "run-config.json")
    original_summary = _read_json(original_output / "baseline-summary.json")
    writer_summary = _read_json(writer_output / "baseline-summary.json")
    original_manifest = _read_json(original_output / "version-manifest.json")
    writer_manifest = _read_json(writer_output / "version-manifest.json")
    original_step = _step_summary(original_output)
    writer_step = _step_summary(writer_output)

    original_runtime = original_manifest.get("execution_runtime")
    writer_runtime = writer_manifest.get("execution_runtime")
    runtime_match = (
        isinstance(original_runtime, Mapping)
        and isinstance(writer_runtime, Mapping)
        and original_runtime.get("sha256") == writer_runtime.get("sha256")
    )
    evaluator_match = (
        isinstance(original_step, Mapping)
        and isinstance(writer_step, Mapping)
        and original_step.get("evaluator_sha256")
        and original_step.get("evaluator_sha256")
        == writer_step.get("evaluator_sha256")
    )
    spec_match = (
        isinstance(original_step, Mapping)
        and isinstance(writer_step, Mapping)
        and original_step.get("spec_sha256")
        and original_step.get("spec_sha256") == writer_step.get("spec_sha256")
    )
    original_runner_provenance = _recorded_commit_runner_matches(original_config)
    writer_runner_provenance = _recorded_commit_runner_matches(writer_config)
    original_runner_sha = _runner_sha256(original_config)
    writer_runner_sha = _runner_sha256(writer_config)
    historical_runner_source_match = bool(
        original_runner_sha
        and writer_runner_sha
        and original_runner_sha == writer_runner_sha
    )
    strict_runner_provenance = (
        original_runner_provenance and writer_runner_provenance
    )
    descriptive_ready = bool(
        original_summary.get("verified52_ready")
        and writer_summary.get("verified52_ready")
        and runtime_match
        and evaluator_match
        and spec_match
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol_id": VERIFIED52_PROTOCOL_ID,
        "source_lock_policy": {
            "enforced": False,
            "note": (
                "Repository commits, dirty paths, source hashes, and Writer archive "
                "hashes are provenance only and do not block future runs or resumes."
            ),
        },
        "arms": {
            "original": {
                "output_dir": str(original_output),
                "baseline_ready": original_summary.get("baseline_ready"),
                "verified52_ready": original_summary.get("verified52_ready"),
                "recorded_runner_sha256": original_runner_sha,
                "recorded_commit_contains_runner": original_runner_provenance,
            },
            VERIFIED52_WRITER_ARM: {
                "output_dir": str(writer_output),
                "baseline_ready": writer_summary.get("baseline_ready"),
                "verified52_ready": writer_summary.get("verified52_ready"),
                "writer_gate_passed": writer_summary.get("writer_smoke_gate", {}).get(
                    "passed"
                ),
                "recorded_runner_sha256": writer_runner_sha,
                "recorded_commit_contains_runner": writer_runner_provenance,
            },
        },
        "comparability": {
            "shared_actor_execution_core_match": runtime_match,
            "execution_core_source_match": runtime_match,
            "historical_runner_source_match": historical_runner_source_match,
            "offline_evaluator_match": bool(evaluator_match),
            "offline_spec_match": bool(spec_match),
            "offline_scoring_comparable": bool(evaluator_match and spec_match),
            "strict_historical_runner_provenance_complete": strict_runner_provenance,
            "descriptive_comparison_ready": descriptive_ready,
            "strict_paired_causal_claim_ready": False,
        },
        "resolutions": {
            "runtime_evaluator_ambiguity": {
                "resolved": bool(runtime_match and evaluator_match and spec_match),
                "explanation": (
                    "The optional official-evaluator virtualenv is excluded from Actor "
                    "identity, shared execution-core paths are canonicalized, and both "
                    "arms are scored with the same offline evaluator/spec. Arm-specific "
                    "runner differences remain explicit provenance rather than being "
                    "hidden inside the shared-core fingerprint."
                ),
            },
            "writer_baseline_ready": {
                "resolved": writer_summary.get("verified52_ready") is True,
                "explanation": (
                    "Readiness is now arm-neutral: complete 52x2 slots plus the Writer "
                    "gate qualify the Writer arm, while formal_original_baseline remains "
                    "Original-only."
                ),
            },
        },
        "limitations": [
            (
                "The Original run's recorded runner SHA does not match its recorded Git "
                "commit, so strict historical execution provenance cannot be repaired "
                "retroactively without rerunning."
            ),
            (
                "The runs are suitable for transparent descriptive comparison, not a "
                "strict paired causal claim."
            ),
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-output", type=Path, required=True)
    parser.add_argument("--writer-output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    migrated: list[dict[str, Any]] = []
    if args.apply:
        migrated = [
            migrate_output_metadata(args.original_output),
            migrate_output_metadata(args.writer_output),
        ]
    audit = build_comparability_audit(
        args.original_output,
        args.writer_output,
    )
    audit["metadata_migrated"] = migrated
    if args.apply:
        _write_json(args.audit_output.resolve(), audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0 if audit["resolutions"]["writer_baseline_ready"]["resolved"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
