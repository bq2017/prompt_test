"""Write the immutable provenance manifest for one Stage5.2-B run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path_text: str) -> dict[str, str]:
    path = Path(path_text).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return {"path": str(path), "sha256": sha256(path)}


def prompt_record(path_text: str) -> dict[str, str]:
    record = file_record(path_text)
    namespace: dict[str, object] = {}
    source = Path(path_text).resolve()
    previous_cwd = Path.cwd()
    try:
        os.chdir(source.parents[1])
        exec(source.read_text(encoding="utf-8"), namespace)
    finally:
        os.chdir(previous_cwd)
    prefix = str(namespace["DIFFICULTY_RATING_PROMPT_PREFIX"])
    suffix = str(namespace["DIFFICULTY_RATING_PROMPT_SUFFIX"])
    effective = hashlib.sha256(
        (prefix + "\n<STAGE52_PROMPT_SUFFIX>\n" + suffix).encode("utf-8")
    ).hexdigest()
    record["effective_prompt_sha256"] = effective
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成Stage5.2-B运行清单")
    parser.add_argument("--output", required=True)
    parser.add_argument("--stage5-results", required=True)
    parser.add_argument("--input-data", required=True)
    parser.add_argument("--stage5-prompt", required=True)
    parser.add_argument("--b-prompt", required=True)
    parser.add_argument("--candidate-script", required=True)
    parser.add_argument("--mapping-script", required=True)
    parser.add_argument("--audit-runner", required=True)
    parser.add_argument("--schema-file", required=True)
    parser.add_argument("--schema-version", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--temperature", default="")
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--stage5-postprocess-profile", required=True)
    parser.add_argument("--run-name", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = {
        "protocol": "stage52_b_frozen_stage5_independent_audit_v1",
        "run_name": args.run_name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_name": args.model_name,
        "temperature": args.temperature if args.temperature != "" else None,
        "concurrency": args.concurrency,
        "stage5_postprocess_profile": args.stage5_postprocess_profile,
        "b_schema_version": args.schema_version,
        "files": {
            "input_data": file_record(args.input_data),
            "stage5_results": file_record(args.stage5_results),
            "stage5_prompt": prompt_record(args.stage5_prompt),
            "b_audit_prompt": prompt_record(args.b_prompt),
            "candidate_extractor": file_record(args.candidate_script),
            "deterministic_mapper": file_record(args.mapping_script),
            "b_audit_runner": file_record(args.audit_runner),
            "b_schema": file_record(args.schema_file),
        },
        "frozen_rules": {
            "stage5_compact_modified": False,
            "b_only_receives_allowlisted_question_material": True,
            "stage5_reasoning_or_features_passed_to_b": False,
            "teacher_label_or_reason_passed_to_b": False,
            "t_mapping": "raw Stage5 level 4 and deterministic B trigger -> level 5",
            "automatic_4_to_5_postprocess": "audit_only",
            "net_benefit_formula": "B新增教师5 - B新增所有非5",
        },
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"运行清单: {output}")


if __name__ == "__main__":
    main()
