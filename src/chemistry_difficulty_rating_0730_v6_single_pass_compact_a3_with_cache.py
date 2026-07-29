"""Chemistry V6 A3 single-pass compact production candidate.

The model reconstructs the question, identifies the rated target, and rates it
in one call.  It outputs only a compact task structure rather than a full task
graph.  Program-side checks are audit-only and never change the model level.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import random
import time
from pathlib import Path
from typing import Any, Mapping

import aiohttp
from asyncio import Semaphore
from tqdm.asyncio import tqdm

import chemistry_difficulty_rating_0730_v6_decoupled_taskgraph_a2_with_cache as transport
from chemistry_single_pass_compact_schema import (
    CompactSchemaError,
    UnrateableInputError,
    apply_observe_only_audit,
    validate_and_prepare_compact_result,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "0730初中化学难度打标提示词_v6_single_pass_compact_a3.txt"
DEFAULT_CACHE = PROJECT_ROOT / "chemistry_v6_single_pass_compact_a3_prompt_cache.json"

MAX_JSON_RETRIES = int(os.getenv("CHEMISTRY_A3_JSON_RETRIES", "2"))
MAX_SCHEMA_RETRIES = int(os.getenv("CHEMISTRY_A3_SCHEMA_RETRIES", "2"))
DEFAULT_CONCURRENCY = int(os.getenv("CHEMISTRY_A3_CONCURRENCY", "20"))
STAGE = "compact_single_pass_rating"


def load_prompt(path: str) -> tuple[str, str]:
    """Execute a compositional prompt with a real ``__file__`` value."""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"找不到Prompt文件: {source}")
    namespace: dict[str, Any] = {"__file__": str(source)}
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
    prefix = str(namespace.get("DIFFICULTY_RATING_PROMPT_PREFIX", "")).strip()
    suffix = str(namespace.get("DIFFICULTY_RATING_PROMPT_SUFFIX", "")).strip()
    if not prefix or not suffix:
        raise ValueError(f"Prompt缺少DIFFICULTY_RATING_PROMPT_PREFIX或SUFFIX: {source}")
    return prefix, suffix


def build_compact_content(
    data: Mapping[str, Any],
    suffix: str,
    stage: str,
    reconstruction: Mapping[str, Any] | None = None,
    repair_feedback: str = "",
) -> list[dict[str, str]]:
    """Build one image-grounded request; reconstruction is deliberately absent."""
    del reconstruction
    if stage != STAGE:
        raise ValueError(f"A3不支持阶段: {stage}")
    instruction = (
        "【当前阶段】单调用紧凑定档。先在内部完整重建题面和最小任务结构，"
        "再依据教师标准定档；只输出合同要求的紧凑JSON，不输出完整任务图或推理草稿。"
    )
    content: list[dict[str, str]] = [
        {"type": "input_text", "text": instruction + "\n" + suffix}
    ]
    urls = transport.image_urls(data) if transport.ENABLE_IMAGE_INPUT else []
    for label, url in urls:
        content.append({
            "type": "input_text",
            "text": f"【{label}】请读取公式、装置、流程箭头、曲线、表格和空间关系。",
        })
        content.append({"type": "input_image", "image_url": url})
    content.append({
        "type": "input_text",
        "text": "【完整结构化文字参考】\n" + transport.construct_question_text(data),
    })
    if repair_feedback:
        content.append({
            "type": "input_text",
            "text": (
                "【上次输出修复要求】\n" + repair_feedback
                + "\n只修复JSON结构或合同冲突；重新核对原题，不得为通过校验而编造依赖或升降档。"
                  "完整输出一个JSON对象。"
            ),
        })
    return content


# Reuse the already-tested HTTP/prefix-cache transport, but replace its request
# composer and cache location.  There is still exactly one model call per try.
transport.build_stage_content = build_compact_content
transport.CACHE_FILE_PATH = Path(
    os.getenv("CHEMISTRY_A3_CACHE_FILE", str(DEFAULT_CACHE))
)


def _empty_audit() -> dict[str, Any]:
    return {
        "elapsed_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "http_retry_count": 0,
    }


async def run_compact_stage(
    data: Mapping[str, Any],
    session: aiohttp.ClientSession,
    prefix: str,
    suffix: str,
    retries: int,
    timeout_sec: int,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any], list[str], list[str]]:
    """Call, validate, and narrowly retry malformed JSON/schema output."""
    json_errors: list[str] = []
    schema_errors: list[str] = []
    repair_feedback = ""
    json_retry_count = 0
    schema_retry_count = 0
    aggregate = _empty_audit()
    last_raw: dict[str, Any] = {}
    last_text = ""

    while True:
        try:
            raw, raw_text, audit = await transport.call_stage(
                data,
                session,
                STAGE,
                prefix,
                suffix,
                retries,
                timeout_sec,
                repair_feedback=repair_feedback,
            )
            last_raw, last_text = raw, raw_text
            for key in aggregate:
                aggregate[key] += audit[key]
        except (json.JSONDecodeError, ValueError) as exc:
            message = f"JSON解析失败: {exc}"
            json_errors.append(message)
            if json_retry_count >= MAX_JSON_RETRIES:
                raise RuntimeError(f"A3 JSON重试耗尽: {message}") from exc
            json_retry_count += 1
            repair_feedback = message
            continue

        try:
            validated = apply_observe_only_audit(
                validate_and_prepare_compact_result(raw)
            )
            aggregate["json_parse_retry_count"] = json_retry_count
            aggregate["schema_retry_count"] = schema_retry_count
            return validated, last_raw, last_text, aggregate, json_errors, schema_errors
        except UnrateableInputError:
            raise
        except CompactSchemaError as exc:
            message = str(exc)
            schema_errors.append(message)
            if schema_retry_count >= MAX_SCHEMA_RETRIES:
                raise RuntimeError(f"A3 schema重试耗尽: {message}") from exc
            schema_retry_count += 1
            repair_feedback = f"上次输出未通过A3紧凑合同：{message}"


async def process_question(
    data: dict[str, Any],
    session: aiohttp.ClientSession,
    semaphore: Semaphore,
    output_path: str,
    error_path: str,
    prefix: str,
    suffix: str,
    retries: int,
    timeout_sec: int,
) -> None:
    async with semaphore:
        raw: dict[str, Any] = {}
        raw_text = ""
        audit: dict[str, Any] = {}
        try:
            rating, raw, raw_text, audit, json_errors, schema_errors = await run_compact_stage(
                data, session, prefix, suffix, retries, timeout_sec
            )
            urls = transport.image_urls(data) if transport.ENABLE_IMAGE_INPUT else []
            output = copy.deepcopy(data)
            output.update({
                "difficulty_rating_raw": raw,
                "difficulty_rating": rating,
                "postprocess_actions": [],
                "single_pass_compact_architecture": True,
                "decision_architecture": "single_call_internal_reconstruction_compact_audit",
                "automatic_level_change_applied": False,
                "question_input_mode": "image_text_grounded" if urls else "fulltext_no_image",
                "image_input_requested": bool(urls),
                "image_input_used": bool(urls),
                "image_input_url_count": len(urls),
                "rating_stage_audit": audit,
                "retry_audit": audit,
                "api_time_use": round(float(audit.get("elapsed_seconds", 0)), 2),
                "api_prompt_tokens": int(audit.get("input_tokens", 0)),
                "api_completion_tokens": int(audit.get("output_tokens", 0)),
                "api_total_tokens": int(audit.get("total_tokens", 0)),
                "json_errors": json_errors,
                "schema_errors": schema_errors,
            })
            await transport.append_jsonl(output_path, output)
        except Exception as exc:
            error = copy.deepcopy(data)
            error.update({
                "rating_error": f"question_id={data.get('question_id', 'unknown')}; error={exc}",
                "failed_stage": STAGE,
                "difficulty_rating_raw": raw,
                "last_model_text": raw_text,
                "rating_stage_audit": audit,
            })
            await transport.append_jsonl(error_path, error)


async def main_batch_run() -> None:
    parser = argparse.ArgumentParser(description="初中化学V6 A3单调用紧凑任务结构难度打标")
    parser.add_argument("-p", "--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-e", "--error", required=True)
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("-t", "--timeout", type=int, default=240)
    parser.add_argument("-r", "--retries", type=int, default=3)
    parser.add_argument("-n", "--num", type=int)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    prefix, suffix = load_prompt(args.prompt)
    questions: list[dict[str, Any]] = []
    with open(args.input, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                questions.append(json.loads(line))
    print(f"成功加载题目数据，共计 {len(questions)} 道题目。")

    random.seed(args.seed)
    if args.num is not None:
        questions = random.sample(questions, min(args.num, len(questions)))
    else:
        random.shuffle(questions)
    done = transport.processed_ids(args.output)
    questions = [question for question in questions if question.get("question_id") not in done]
    print(f"数据比对完成: 已完成数 {len(done)}，待处理数 {len(questions)}")
    if not questions:
        return

    semaphore = Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    progress = tqdm(total=len(questions), unit="item", desc="Chemistry A3 Single-pass Progress")
    async with aiohttp.ClientSession(connector=connector) as session:
        await transport.get_or_create_prefix_cache(
            session, STAGE, prefix, args.retries, args.timeout
        )

        async def wrapped(question: dict[str, Any]) -> None:
            await process_question(
                question,
                session,
                semaphore,
                args.output,
                args.error,
                prefix,
                suffix,
                args.retries,
                args.timeout,
            )
            progress.update(1)

        await asyncio.gather(*(wrapped(question) for question in questions))
    progress.close()
    print("\nA3单调用紧凑化学打标运行结束。")
    print(f"成功结果: {Path(args.output).resolve()}")
    print(f"失败日志: {Path(args.error).resolve()}")


if __name__ == "__main__":
    started = time.time()
    asyncio.run(main_batch_run())
    print(f"总耗时: {(time.time() - started) / 60:.2f} 分钟")
