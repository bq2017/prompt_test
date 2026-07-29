"""Chemistry V7 single-call independent rating + post-hoc Evidence-15 runner."""

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
from chemistry_postprocess_v7_decoupled import (
    VALID_PROFILES,
    postprocess_chemistry_difficulty,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "0730初中化学难度打标提示词_v7_decoupled_evidence15.txt"
DEFAULT_CACHE = PROJECT_ROOT / "chemistry_v7_decoupled_evidence15_prompt_cache.json"
MAX_JSON_RETRIES = int(os.getenv("CHEMISTRY_V7_JSON_RETRIES", "2"))
MAX_SCHEMA_RETRIES = int(os.getenv("CHEMISTRY_V7_SCHEMA_RETRIES", "2"))
DEFAULT_CONCURRENCY = int(os.getenv("CHEMISTRY_V7_CONCURRENCY", "30"))
POSTPROCESS_PROFILE = os.getenv("CHEMISTRY_V7_POSTPROCESS_PROFILE", "conservative_v1").strip().lower()
STAGE = "v7_single_call_decoupled_evidence15"


def load_prompt(path: str) -> tuple[str, str]:
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"找不到Prompt文件: {source}")
    namespace: dict[str, Any] = {"__file__": str(source)}
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
    prefix = str(namespace.get("DIFFICULTY_RATING_PROMPT_PREFIX", "")).strip()
    suffix = str(namespace.get("DIFFICULTY_RATING_PROMPT_SUFFIX", "")).strip()
    if not prefix or not suffix:
        raise ValueError(f"V7 Prompt缺少PREFIX或SUFFIX: {source}")
    return prefix, suffix


def build_v7_content(
    data: Mapping[str, Any],
    suffix: str,
    stage: str,
    reconstruction: Mapping[str, Any] | None = None,
    repair_feedback: str = "",
) -> list[dict[str, str]]:
    del reconstruction
    if stage != STAGE:
        raise ValueError(f"V7不支持阶段: {stage}")
    content: list[dict[str, str]] = [{
        "type": "input_text",
        "text": (
            "【当前阶段】V7单调用独立主定档。先理解真实任务并冻结difficulty_level，"
            "冻结后再填写Evidence-15；只输出合同要求的JSON。\n" + suffix
        ),
    }]
    urls = transport.image_urls(data) if transport.ENABLE_IMAGE_INPUT else []
    for label, url in urls:
        content.append({
            "type": "input_text",
            "text": f"【{label}】请读取公式、装置连接、流程箭头、曲线、表格和空间关系。",
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
                + "\n只修复JSON、字段或枚举问题；保持独立主档位判断，不得为了后处理规则编造feature。"
                  "请完整重输一个JSON对象。"
            ),
        })
    return content


transport.build_stage_content = build_v7_content
transport.CACHE_FILE_PATH = Path(os.getenv("CHEMISTRY_V7_CACHE_FILE", str(DEFAULT_CACHE)))


def _with_evaluator_fields(result: dict[str, Any]) -> dict[str, Any]:
    """Bridge the supplied postprocessor to the repository evaluator contract."""
    prepared = copy.deepcopy(result)
    raw_level = prepared.get("difficulty_level_raw")
    final_level = prepared.get("difficulty_level")
    prepared["postprocess_original_level"] = raw_level
    prepared["postprocess_final_level"] = final_level
    prepared["postprocess_trace"] = copy.deepcopy(prepared.get("postprocess_actions", []))
    prepared["automatic_level_change_applied"] = bool(
        raw_level and final_level and raw_level != final_level
    )
    return prepared


def _empty_audit() -> dict[str, Any]:
    return {
        "elapsed_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "http_retry_count": 0,
    }


async def run_v7_stage(
    data: Mapping[str, Any],
    session: aiohttp.ClientSession,
    prefix: str,
    suffix: str,
    retries: int,
    timeout_sec: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any], list[str], list[str]]:
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
                data, session, STAGE, prefix, suffix, retries, timeout_sec,
                repair_feedback=repair_feedback,
            )
            last_raw, last_text = raw, raw_text
            for key in aggregate:
                aggregate[key] += audit[key]
        except (json.JSONDecodeError, ValueError) as exc:
            message = f"JSON解析失败: {exc}"
            json_errors.append(message)
            if json_retry_count >= MAX_JSON_RETRIES:
                raise RuntimeError(f"V7 JSON重试耗尽: {message}") from exc
            json_retry_count += 1
            repair_feedback = message
            continue
        try:
            prompt_only = _with_evaluator_fields(
                postprocess_chemistry_difficulty(raw, dict(data), profile="prompt_only")
            )
            final = _with_evaluator_fields(
                postprocess_chemistry_difficulty(raw, dict(data), profile=POSTPROCESS_PROFILE)
            )
            aggregate["json_parse_retry_count"] = json_retry_count
            aggregate["schema_retry_count"] = schema_retry_count
            return final, prompt_only, last_raw, last_text, aggregate, json_errors, schema_errors
        except (ValueError, TypeError, AssertionError) as exc:
            message = str(exc)
            schema_errors.append(message)
            if schema_retry_count >= MAX_SCHEMA_RETRIES:
                raise RuntimeError(f"V7合同/后处理重试耗尽: {message}") from exc
            schema_retry_count += 1
            repair_feedback = f"上次输出未通过V7合同：{message}"


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
            final, prompt_only, raw, raw_text, audit, json_errors, schema_errors = await run_v7_stage(
                data, session, prefix, suffix, retries, timeout_sec
            )
            urls = transport.image_urls(data) if transport.ENABLE_IMAGE_INPUT else []
            output = copy.deepcopy(data)
            output.update({
                "difficulty_rating_raw": raw,
                "difficulty_rating_prompt_only": prompt_only,
                "difficulty_rating": final,
                "postprocess_actions": copy.deepcopy(final.get("postprocess_actions", [])),
                "v7_decoupled_evidence15_architecture": True,
                "decision_architecture": "single_call_freeze_level_then_evidence15",
                "automatic_level_change_applied": bool(final.get("automatic_level_change_applied")),
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
    if POSTPROCESS_PROFILE not in VALID_PROFILES:
        raise ValueError(f"未知V7后处理profile: {POSTPROCESS_PROFILE}")
    parser = argparse.ArgumentParser(description="初中化学V7独立主定档与后置Evidence-15")
    parser.add_argument("-p", "--prompt", default=str(DEFAULT_PROMPT))
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-e", "--error", required=True)
    parser.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("-t", "--timeout", type=int, default=300)
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
    progress = tqdm(total=len(questions), unit="item", desc="Chemistry V7 Decoupled-E15 Progress")
    async with aiohttp.ClientSession(connector=connector) as session:
        await transport.get_or_create_prefix_cache(session, STAGE, prefix, args.retries, args.timeout)

        async def wrapped(question: dict[str, Any]) -> None:
            await process_question(
                question, session, semaphore, args.output, args.error,
                prefix, suffix, args.retries, args.timeout,
            )
            progress.update(1)

        await asyncio.gather(*(wrapped(question) for question in questions))
    progress.close()
    print("\nV7独立主定档与后置Evidence-15运行结束。")
    print(f"成功结果: {Path(args.output).resolve()}")
    print(f"失败日志: {Path(args.error).resolve()}")


if __name__ == "__main__":
    started = time.time()
    asyncio.run(main_batch_run())
    print(f"总耗时: {(time.time() - started) / 60:.2f} 分钟")
