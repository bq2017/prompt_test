"""Run the independent Stage5.2-B audit on allowlisted raw-level-4 candidates."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from tqdm.asyncio import tqdm


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import chemistry_difficulty_rating_evidence15_v9_with_cache as api_runner
from chemistry_stage52_b_schema import (
    SCHEMA_VERSION,
    Stage52BAuditError,
    derive_composite_burden,
    failed_gates,
    validate_b_audit,
)


DEFAULT_PROMPT = ROOT / "prompts" / "evidence15_v9_stage52_b_audit_prompt.txt"
WRITE_LOCK = asyncio.Lock()
FORBIDDEN_INPUT_FIELDS = {
    "difficulty",
    "difficulty_rating",
    "difficulty_rating_raw",
    "features",
    "reasoning",
    "core_basis",
    "why_not_higher",
    "standard_level",
    "teacher_reason",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行Stage5.2独立B审计")
    parser.add_argument("--input", required=True, help="白名单B候选JSONL")
    parser.add_argument("--output", required=True, help="B审计成功JSONL")
    parser.add_argument("--error", required=True, help="B审计失败JSONL")
    parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("--concurrency", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--schema-retries", type=int, default=2)
    parser.add_argument(
        "--cache-file",
        default=str(ROOT / "chemistry_stage52_b_prompt_cache.json"),
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{number} 顶层不是JSON对象")
            leaked = sorted(FORBIDDEN_INPUT_FIELDS & set(item))
            if leaked:
                raise ValueError(f"{path}:{number} 泄漏禁止字段: {leaked}")
            question_id = str(item.get("question_id", "") or "").strip()
            if not question_id:
                raise ValueError(f"{path}:{number} 缺少question_id")
            if question_id in seen:
                raise ValueError(f"{path} 存在重复question_id: {question_id}")
            seen.add(question_id)
            items.append(item)
    return items


def processed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        str(item.get("question_id", "") or "").strip()
        for item in load_jsonl(path)
        if item.get("question_id")
    }


async def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    async with WRITE_LOCK:
        async with aiofiles.open(path, "a", encoding="utf-8") as handle:
            await handle.write(json.dumps(item, ensure_ascii=False) + "\n")


async def audit_one(
    candidate: dict[str, Any],
    *,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    output: Path,
    error: Path,
    retries: int,
    timeout: int,
    schema_retries: int,
) -> None:
    async with semaphore:
        feedback = ""
        raw_text = ""
        parse_errors: list[str] = []
        schema_errors: list[str] = []
        total_time = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        image_status: dict[str, Any] = {}

        for attempt in range(schema_retries + 1):
            try:
                (
                    raw,
                    raw_text,
                    parse_error,
                    elapsed,
                    input_tokens,
                    output_tokens,
                    tokens,
                    image_status,
                ) = await api_runner.call_model_with_cache(
                    candidate,
                    session,
                    retries,
                    timeout,
                    repair_feedback=feedback,
                )
                total_time += elapsed
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                total_tokens += tokens
                if parse_error:
                    parse_errors.append(parse_error)
                    feedback = (
                        f"上次输出不是完整JSON对象：{parse_error}。"
                        "只修复JSON，不改变B审计实质判断。"
                    )
                    continue
                try:
                    audit = validate_b_audit(raw)
                except Stage52BAuditError as exc:
                    schema_errors.append(str(exc))
                    feedback = (
                        f"上次B审计未通过schema：{exc}。"
                        "只修复字段、类型或枚举，不改变实质判断。"
                    )
                    continue

                result = {
                    "question_id": candidate["question_id"],
                    "stage52_b_schema_version": SCHEMA_VERSION,
                    "stage52_b_audit": audit,
                    "stage52_b_trigger": derive_composite_burden(audit),
                    "stage52_b_failed_gates": failed_gates(audit),
                    "api_time_use": round(total_time, 3),
                    "api_prompt_tokens": total_input_tokens,
                    "api_completion_tokens": total_output_tokens,
                    "api_total_tokens": total_tokens,
                    "json_parse_errors": parse_errors,
                    "schema_validation_errors": schema_errors,
                    **image_status,
                }
                await append_jsonl(output, result)
                return
            except Exception as exc:
                feedback = (
                    f"上次调用失败：{exc}。请重新输出完整B审计JSON，"
                    "不要输出最终难度档位。"
                )
                schema_errors.append(str(exc))

        await append_jsonl(
            error,
            {
                "question_id": candidate.get("question_id"),
                "stage52_b_schema_version": SCHEMA_VERSION,
                "rating_error": "B审计JSON/schema重试耗尽",
                "last_model_text": raw_text,
                "json_parse_errors": parse_errors,
                "schema_validation_errors": schema_errors,
                "api_time_use": round(total_time, 3),
                "api_prompt_tokens": total_input_tokens,
                "api_completion_tokens": total_output_tokens,
                "api_total_tokens": total_tokens,
                **image_status,
            },
        )


async def run(args: argparse.Namespace) -> None:
    input_path = Path(args.input).resolve()
    output = Path(args.output).resolve()
    error = Path(args.error).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    error.parent.mkdir(parents=True, exist_ok=True)
    output.touch(exist_ok=True)
    error.touch(exist_ok=True)

    api_runner.CACHE_FILE_PATH = str(Path(args.cache_file).resolve())
    api_runner.load_prompt_config(str(Path(args.prompt).resolve()))
    candidates = load_jsonl(input_path)
    done = processed_ids(output) | processed_ids(error)
    pending = [
        item
        for item in candidates
        if str(item["question_id"]) not in done
    ]
    print(f"B候选数: {len(candidates)}")
    print(f"已处理: {len(done)}，待处理: {len(pending)}")
    if not pending:
        return

    semaphore = asyncio.Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        await api_runner.get_or_create_cache(session, args.retries, args.timeout)
        tasks = [
            audit_one(
                candidate,
                session=session,
                semaphore=semaphore,
                output=output,
                error=error,
                retries=args.retries,
                timeout=args.timeout,
                schema_retries=args.schema_retries,
            )
            for candidate in pending
        ]
        for future in tqdm.as_completed(
            tasks, total=len(tasks), desc="Stage5.2 B audit", unit="item"
        ):
            await future


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
