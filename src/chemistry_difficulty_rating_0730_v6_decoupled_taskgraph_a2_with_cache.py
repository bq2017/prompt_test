"""Chemistry V6 decoupled task-graph A2 two-pass experiment.

Pass 1 sees no difficulty rubric and reconstructs the question/task graph.
Pass 2 uses a separate prefix cache and receives the validated reconstruction
to assign a level. No automatic postprocessing can change that level.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiofiles
import aiohttp
from asyncio import Lock, Semaphore
from dotenv import load_dotenv
from tqdm.asyncio import tqdm

from chemistry_two_pass_taskgraph_schema import (
    TwoPassSchemaError,
    UnreconstructableInputError,
    validate_rating,
    validate_reconstruction,
)


load_dotenv()
API_KEY = os.getenv("API_KEY", "not-needed")
BASE_URL = os.getenv("BASE_URL", "http://172.22.0.35:4466/v1").rstrip("/") + "/"
MODEL_NAME = os.getenv("MODEL_NAME", "doubao-seed-2.0-lite")
TEMPERATURE_RAW = os.getenv("TEMPERATURE", "").strip()
TEMPERATURE = float(TEMPERATURE_RAW) if TEMPERATURE_RAW else None
ENABLE_IMAGE_INPUT = os.getenv("CHEMISTRY_ENABLE_IMAGE_INPUT", "1").lower() in {"1", "true", "yes", "on"}
MAX_JSON_RETRIES = int(os.getenv("CHEMISTRY_A2_JSON_RETRIES", "2"))
MAX_SCHEMA_RETRIES = int(os.getenv("CHEMISTRY_A2_SCHEMA_RETRIES", "2"))
CACHE_EXPIRE_SECONDS = 6 * 24 * 3600

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_TASK_PROMPT = PROJECT_ROOT / "prompts" / "0730初中化学任务重建提示词_v6_decoupled_taskgraph_a2.txt"
DEFAULT_RATING_PROMPT = PROJECT_ROOT / "prompts" / "0730初中化学难度打标提示词_v6_decoupled_taskgraph_a2.txt"
DEFAULT_CACHE = PROJECT_ROOT / "chemistry_v6_decoupled_taskgraph_a2_prompt_cache.json"
CACHE_FILE_PATH = Path(os.getenv("CHEMISTRY_A2_CACHE_FILE", str(DEFAULT_CACHE)))

FILE_LOCK = Lock()
CACHE_LOCK = Lock()
CACHE_CREATION_LOCK = Lock()


def load_prompt(path: str, prefix_name: str, suffix_name: str) -> tuple[str, str]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"找不到Prompt文件: {source}")
    namespace: dict[str, Any] = {}
    exec(source.read_text(encoding="utf-8"), namespace)
    prefix = str(namespace.get(prefix_name, "")).strip()
    suffix = str(namespace.get(suffix_name, "")).strip()
    if not prefix or not suffix:
        raise ValueError(f"Prompt缺少{prefix_name}或{suffix_name}: {source}")
    return prefix, suffix


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def load_cache_file() -> dict[str, Any]:
    async with CACHE_LOCK:
        if not CACHE_FILE_PATH.is_file():
            return {}
        try:
            async with aiofiles.open(CACHE_FILE_PATH, "r", encoding="utf-8") as handle:
                return json.loads(await handle.read() or "{}")
        except Exception:
            return {}


async def save_cache_file(data: Mapping[str, Any]) -> None:
    async with CACHE_LOCK:
        CACHE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(CACHE_FILE_PATH, "w", encoding="utf-8") as handle:
            await handle.write(json.dumps(data, ensure_ascii=False, indent=2))


def cache_entry_valid(entry: Any, prefix: str) -> bool:
    return (
        isinstance(entry, Mapping)
        and int(entry.get("expire_at", 0)) > int(time.time())
        and entry.get("prefix_hash") == sha256_text(prefix)
        and entry.get("model_name") == MODEL_NAME
        and bool(entry.get("response_id"))
    )


async def create_prefix_cache(
    session: aiohttp.ClientSession,
    stage: str,
    prefix: str,
    retries: int,
    timeout_sec: int,
) -> str:
    expire_at = int(time.time()) + CACHE_EXPIRE_SECONDS
    payload = {
        "model": MODEL_NAME,
        "input": [{"role": "user", "content": prefix}],
        "thinking": {"type": "disabled"},
        "expire_at": expire_at,
        "caching": {"type": "enabled", "prefix": True},
    }
    for attempt in range(retries):
        try:
            async with session.post(
                f"{BASE_URL}responses",
                json=payload,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    response_id = str(result.get("id", ""))
                    if response_id:
                        data = await load_cache_file()
                        data[stage] = {
                            "response_id": response_id,
                            "expire_at": expire_at,
                            "prefix_hash": sha256_text(prefix),
                            "model_name": MODEL_NAME,
                            "created_at": int(time.time()),
                        }
                        await save_cache_file(data)
                        print(f"{stage}前缀缓存创建成功: {response_id}")
                        return response_id
                error = await response.text()
                if 400 <= response.status < 500:
                    raise RuntimeError(f"{stage}缓存创建失败 HTTP {response.status}: {error[:300]}")
        except Exception:
            if attempt + 1 >= retries:
                raise
            await asyncio.sleep((2 ** attempt) + random.random())
    raise RuntimeError(f"{stage}缓存创建重试耗尽")


async def get_or_create_prefix_cache(
    session: aiohttp.ClientSession,
    stage: str,
    prefix: str,
    retries: int,
    timeout_sec: int,
    *,
    force: bool = False,
) -> str:
    async with CACHE_CREATION_LOCK:
        data = await load_cache_file()
        entry = data.get(stage)
        if not force and cache_entry_valid(entry, prefix):
            return str(entry["response_id"])
        return await create_prefix_cache(session, stage, prefix, retries, timeout_sec)


def construct_question_text(data: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for field, label in (("stem", "题干"), ("options", "选项"), ("analysis", "解析")):
        value = str(data.get(field, "") or "").strip()
        if value:
            parts.append(f"【{label}】\n{value}")
    subquestions = data.get("sub_questions") or []
    if subquestions:
        parts.append("【结构化小问】")
        for index, subquestion in enumerate(subquestions, 1):
            if isinstance(subquestion, Mapping):
                fields = []
                for key, label in (("stem", "题干"), ("options", "选项"), ("analysis", "解析")):
                    value = str(subquestion.get(key, "") or "").strip()
                    if value:
                        fields.append(f"{label}: {value}")
                parts.append(f"第{index}小问\n" + "\n".join(fields))
            else:
                parts.append(f"第{index}小问: {subquestion}")
    else:
        parts.append("【结构化小问状态】未提供拆分；必须以完整题干及图片中的真实设问为准。")
    return "\n\n".join(parts)


def image_urls(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field, label in (("stem_pic_url", "题干图片"), ("analysis_pic_url", "解析图片")):
        url = str(data.get(field, "") or "").strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            result.append((label, url))
            seen.add(url)
    return result


def build_stage_content(
    data: Mapping[str, Any],
    suffix: str,
    stage: str,
    reconstruction: Mapping[str, Any] | None = None,
    repair_feedback: str = "",
) -> list[dict[str, str]]:
    text = construct_question_text(data)
    instruction = (
        "【当前阶段】独立题面与任务重建。本阶段绝不输出难度档位。"
        if stage == "task_reconstruction"
        else "【当前阶段】独立难度定档。第一阶段从未看到难度档位；请依据原题和已校验任务重建定档。"
    )
    content: list[dict[str, str]] = [{"type": "input_text", "text": instruction + "\n" + suffix}]
    urls = image_urls(data) if ENABLE_IMAGE_INPUT else []
    for label, url in urls:
        content.append({"type": "input_text", "text": f"【{label}】请读取其中公式、装置、流程、图表和空间关系。"})
        content.append({"type": "input_image", "image_url": url})
    content.append({"type": "input_text", "text": "【完整结构化文字参考】\n" + text})
    if reconstruction is not None:
        content.append({
            "type": "input_text",
            "text": "【第一阶段已校验任务重建】\n" + json.dumps(reconstruction, ensure_ascii=False, separators=(",", ":")),
        })
    if repair_feedback:
        content.append({
            "type": "input_text",
            "text": (
                "【上次输出修复要求】\n" + repair_feedback
                + "\n只修复JSON结构或合同冲突；重新核对题面，不得为通过校验而编造事实。完整输出一个JSON对象。"
            ),
        })
    return content


def parse_response_text(result: Mapping[str, Any]) -> str:
    texts: list[str] = []
    for item in result.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if part.get("type") == "output_text":
                texts.append(str(part.get("text", "")))
    return "".join(texts).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```json") and clean.endswith("```"):
        clean = clean[7:-3].strip()
    elif clean.startswith("```") and clean.endswith("```"):
        clean = clean[3:-3].strip()
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise ValueError("JSON顶层必须是对象")
    return value


async def call_stage(
    data: Mapping[str, Any],
    session: aiohttp.ClientSession,
    stage: str,
    prefix: str,
    suffix: str,
    retries: int,
    timeout_sec: int,
    reconstruction: Mapping[str, Any] | None = None,
    repair_feedback: str = "",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    response_id = await get_or_create_prefix_cache(session, stage, prefix, retries, timeout_sec)
    total_elapsed = 0.0
    http_retries = 0
    for attempt in range(retries):
        payload: dict[str, Any] = {
            "model": MODEL_NAME,
            "previous_response_id": response_id,
            "input": [{
                "role": "user",
                "content": build_stage_content(data, suffix, stage, reconstruction, repair_feedback),
            }],
            "thinking": {"type": "disabled"},
        }
        if TEMPERATURE is not None:
            payload["temperature"] = TEMPERATURE
        started = time.time()
        try:
            async with session.post(
                f"{BASE_URL}responses",
                json=payload,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as response:
                total_elapsed += time.time() - started
                if response.status == 200:
                    result = await response.json()
                    raw_text = parse_response_text(result)
                    parsed = parse_json_object(raw_text)
                    usage = result.get("usage", {}) or {}
                    audit = {
                        "elapsed_seconds": round(total_elapsed, 2),
                        "input_tokens": int(usage.get("input_tokens", 0) or 0),
                        "output_tokens": int(usage.get("output_tokens", 0) or 0),
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                        "http_retry_count": http_retries,
                    }
                    return parsed, raw_text, audit
                error_text = await response.text()
                if "InvalidParameter.PreviousResponseNotFound" in error_text:
                    response_id = await get_or_create_prefix_cache(
                        session, stage, prefix, retries, timeout_sec, force=True
                    )
                    http_retries += 1
                    continue
                if response.status == 429 or response.status >= 500:
                    http_retries += 1
                    await asyncio.sleep((2 ** attempt) + random.random())
                    continue
                raise RuntimeError(f"{stage} HTTP {response.status}: {error_text[:500]}")
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as exc:
            total_elapsed += max(0.0, time.time() - started)
            if isinstance(exc, (json.JSONDecodeError, ValueError)):
                raise
            if attempt + 1 >= retries:
                raise RuntimeError(f"{stage}网络重试耗尽: {exc}") from exc
            http_retries += 1
            await asyncio.sleep((2 ** attempt) + random.random())
    raise RuntimeError(f"{stage}调用重试耗尽")


async def run_validated_stage(
    data: Mapping[str, Any],
    session: aiohttp.ClientSession,
    stage: str,
    prefix: str,
    suffix: str,
    validator: Any,
    retries: int,
    timeout_sec: int,
    reconstruction: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any], list[str], list[str]]:
    json_errors: list[str] = []
    schema_errors: list[str] = []
    repair_feedback = ""
    json_count = 0
    schema_count = 0
    aggregate = {"elapsed_seconds": 0.0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "http_retry_count": 0}
    last_raw: dict[str, Any] = {}
    last_text = ""
    while True:
        try:
            raw, raw_text, audit = await call_stage(
                data, session, stage, prefix, suffix, retries, timeout_sec,
                reconstruction=reconstruction, repair_feedback=repair_feedback,
            )
            last_raw, last_text = raw, raw_text
            for key in aggregate:
                aggregate[key] += audit[key]
        except (json.JSONDecodeError, ValueError) as exc:
            message = f"JSON解析失败: {exc}"
            json_errors.append(message)
            if json_count >= MAX_JSON_RETRIES:
                raise RuntimeError(f"{stage} JSON重试耗尽: {message}") from exc
            json_count += 1
            repair_feedback = message
            continue
        try:
            validated = validator(raw)
            aggregate["json_parse_retry_count"] = json_count
            aggregate["schema_retry_count"] = schema_count
            return validated, last_raw, last_text, aggregate, json_errors, schema_errors
        except (TwoPassSchemaError, UnreconstructableInputError) as exc:
            if isinstance(exc, UnreconstructableInputError):
                raise
            message = str(exc)
            schema_errors.append(message)
            if schema_count >= MAX_SCHEMA_RETRIES:
                raise RuntimeError(f"{stage} schema重试耗尽: {message}") from exc
            schema_count += 1
            repair_feedback = f"上次输出未通过{stage}合同：{message}"


async def append_jsonl(path: str, data: Mapping[str, Any]) -> None:
    async with FILE_LOCK:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "a", encoding="utf-8") as handle:
            await handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def merge_stage_audits(task: Mapping[str, Any], rating: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("elapsed_seconds", "input_tokens", "output_tokens", "total_tokens", "http_retry_count", "json_parse_retry_count", "schema_retry_count")
    return {key: task.get(key, 0) + rating.get(key, 0) for key in keys}


async def process_question(
    data: dict[str, Any],
    session: aiohttp.ClientSession,
    semaphore: Semaphore,
    output_path: str,
    error_path: str,
    task_prefix: str,
    task_suffix: str,
    rating_prefix: str,
    rating_suffix: str,
    retries: int,
    timeout_sec: int,
) -> None:
    async with semaphore:
        current_stage = "task_reconstruction"
        task_raw: dict[str, Any] = {}
        rating_raw: dict[str, Any] = {}
        task_text = ""
        rating_text = ""
        task_audit: dict[str, Any] = {}
        rating_audit: dict[str, Any] = {}
        try:
            reconstruction, task_raw, task_text, task_audit, task_json_errors, task_schema_errors = await run_validated_stage(
                data, session, "task_reconstruction", task_prefix, task_suffix,
                validate_reconstruction, retries, timeout_sec,
            )

            current_stage = "difficulty_rating"
            def rating_validator(value: Any) -> dict[str, Any]:
                return validate_rating(value, reconstruction)

            rating, rating_raw, rating_text, rating_audit, rating_json_errors, rating_schema_errors = await run_validated_stage(
                data, session, "difficulty_rating", rating_prefix, rating_suffix,
                rating_validator, retries, timeout_sec, reconstruction=reconstruction,
            )
            output = copy.deepcopy(data)
            output.update({
                "task_reconstruction_raw": task_raw,
                "task_reconstruction": reconstruction,
                "difficulty_rating_raw": rating_raw,
                "difficulty_rating": rating,
                "postprocess_actions": [],
                "two_pass_architecture": True,
                "decision_isolation": "separate_calls_separate_prefix_caches",
                "automatic_level_change_applied": False,
                "question_input_mode": "image_text_grounded" if image_urls(data) and ENABLE_IMAGE_INPUT else "fulltext_no_image",
                "image_input_requested": bool(image_urls(data) and ENABLE_IMAGE_INPUT),
                "image_input_used": bool(image_urls(data) and ENABLE_IMAGE_INPUT),
                "image_input_url_count": len(image_urls(data)) if ENABLE_IMAGE_INPUT else 0,
                "task_stage_audit": task_audit,
                "rating_stage_audit": rating_audit,
                "retry_audit": merge_stage_audits(task_audit, rating_audit),
                "api_time_use": round(float(task_audit.get("elapsed_seconds", 0)) + float(rating_audit.get("elapsed_seconds", 0)), 2),
                "api_prompt_tokens": int(task_audit.get("input_tokens", 0)) + int(rating_audit.get("input_tokens", 0)),
                "api_completion_tokens": int(task_audit.get("output_tokens", 0)) + int(rating_audit.get("output_tokens", 0)),
                "api_total_tokens": int(task_audit.get("total_tokens", 0)) + int(rating_audit.get("total_tokens", 0)),
                "task_json_errors": task_json_errors,
                "task_schema_errors": task_schema_errors,
                "rating_json_errors": rating_json_errors,
                "rating_schema_errors": rating_schema_errors,
            })
            await append_jsonl(output_path, output)
        except Exception as exc:
            error = copy.deepcopy(data)
            error.update({
                "rating_error": f"question_id={data.get('question_id', 'unknown')}; error={exc}",
                "failed_stage": current_stage,
                "task_reconstruction_raw": task_raw,
                "difficulty_rating_raw": rating_raw,
                "last_task_model_text": task_text,
                "last_rating_model_text": rating_text,
                "task_stage_audit": task_audit,
                "rating_stage_audit": rating_audit,
            })
            await append_jsonl(error_path, error)


def processed_ids(path: str) -> set[Any]:
    result: set[Any] = set()
    source = Path(path)
    if not source.is_file():
        return result
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
                if value.get("question_id") is not None:
                    result.add(value["question_id"])
            except Exception:
                continue
    return result


async def main_batch_run() -> None:
    parser = argparse.ArgumentParser(description="初中化学A2双阶段任务重建与难度打标")
    parser.add_argument("--task-prompt", default=str(DEFAULT_TASK_PROMPT))
    parser.add_argument("--rating-prompt", default=str(DEFAULT_RATING_PROMPT))
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-e", "--error", required=True)
    parser.add_argument("-c", "--concurrency", type=int, default=12)
    parser.add_argument("-t", "--timeout", type=int, default=240)
    parser.add_argument("-r", "--retries", type=int, default=3)
    parser.add_argument("-n", "--num", type=int)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    task_prefix, task_suffix = load_prompt(args.task_prompt, "TASK_RECONSTRUCTION_PROMPT_PREFIX", "TASK_RECONSTRUCTION_PROMPT_SUFFIX")
    rating_prefix, rating_suffix = load_prompt(args.rating_prompt, "DIFFICULTY_RATING_PROMPT_PREFIX", "DIFFICULTY_RATING_PROMPT_SUFFIX")
    if any(level in task_prefix for level in ("送分题", "基础题", "中等题", "拔高题", "压轴题")):
        raise RuntimeError("任务重建Prompt泄露了难度档位，已阻止实验")

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
    done = processed_ids(args.output)
    questions = [question for question in questions if question.get("question_id") not in done]
    print(f"数据比对完成: 已完成数 {len(done)}，待处理数 {len(questions)}")
    if not questions:
        return

    semaphore = Semaphore(args.concurrency)
    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    progress = tqdm(total=len(questions), unit="item", desc="Chemistry A2 Two-pass Progress")
    async with aiohttp.ClientSession(connector=connector) as session:
        await get_or_create_prefix_cache(session, "task_reconstruction", task_prefix, args.retries, args.timeout)
        await get_or_create_prefix_cache(session, "difficulty_rating", rating_prefix, args.retries, args.timeout)

        async def wrapped(question: dict[str, Any]) -> None:
            await process_question(
                question, session, semaphore, args.output, args.error,
                task_prefix, task_suffix, rating_prefix, rating_suffix,
                args.retries, args.timeout,
            )
            progress.update(1)

        await asyncio.gather(*(wrapped(question) for question in questions))
    progress.close()
    print("\nA2双阶段化学打标运行结束。")
    print(f"成功结果: {Path(args.output).resolve()}")
    print(f"失败日志: {Path(args.error).resolve()}")


if __name__ == "__main__":
    started = time.time()
    asyncio.run(main_batch_run())
    print(f"总耗时: {(time.time() - started) / 60:.2f} 分钟")
