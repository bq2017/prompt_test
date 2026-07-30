# -*- coding: utf-8 -*-
"""
@File    : chemistry_difficulty_rating_with_cache.py
@Description:
    基于前缀缓存（Prompt Cache）和高并发的初中化学题目难度批量评级脚本。
    0724 V5.2 image-primary/mintext消融版：保留V5.2难度标准，以题干图和
    解析图作为题面主来源，仅补充结构化小问数量、缺失图片对应文本或人工
    明确提供的image_text_supplement，并逐题记录实际补充字段。
    v4：基于100题人工复核结果，收紧“标准实验题虚高”和“压轴题虚高”，增强金属滤渣滤液、流程/图表/守恒题的拔高识别。
    v5：基于第二轮100题复核结果，小修5类边界：化学史送分、标准实验多问基础、空气含量压强曲线中等、NaHCO3纯度拔高、常见物质转化推断中等。
    v6：基于300题复核结果，小修8类边界：化学发展简史送分、溶液分类基础、CO还原氧化铁+燃烧条件组合中等、陌生复杂方程式配平中等、陌生材料迁移中等、红磷气压曲线中等、标准碳酸钠沉淀纯度表格中等、KClO3单反应质量图中等。
    V9阶段2.1：统一相邻边界定义，删除步骤数量硬门槛，再填写15项审计特征并完成相邻终审；
    取消入口操作与总推理深度、内部依赖与多问依赖之间的错误强绑定。
    本阶段保留图片输入、缓存、重试和阶段1后处理算法，不提前实施教师示例全面整理或后处理重写。
    难度级别：送分题 / 基础题 / 中等题 / 拔高题 / 压轴题。
"""

import os
import sys
import json
import re
import random
import time
import hashlib
import asyncio
import copy
from pathlib import Path
from typing import Sequence
import aiofiles
import aiohttp
import argparse
try:
    import json_repair
except Exception:
    class _JsonRepairFallback:
        @staticmethod
        def loads(text):
            return json.loads(text)
    json_repair = _JsonRepairFallback()
from typing import Dict, Any, Optional, List, Tuple
from tqdm.asyncio import tqdm
from asyncio import Lock, Semaphore
from dotenv import load_dotenv

# -------------------------- 0. API 基础配置 --------------------------
load_dotenv()

API_KEY = os.getenv("API_KEY", "not-needed")
BASE_URL = os.getenv("BASE_URL", "http://172.22.0.35:4466/v1")
if not BASE_URL.endswith("/"):
    BASE_URL += "/"
MODEL_NAME = os.getenv("MODEL_NAME", "doubao-seed-2.0-lite")
_temperature_raw = os.getenv("TEMPERATURE", "").strip()
TEMPERATURE = float(_temperature_raw) if _temperature_raw else None
ENABLE_IMAGE_INPUT = os.getenv("CHEMISTRY_ENABLE_IMAGE_INPUT", "1").strip().lower() in {
    "1", "true", "yes", "on",
}

MAX_SCHEMA_RETRIES = int(os.getenv(
    "CHEMISTRY_EVIDENCE15_V9_SCHEMA_RETRIES",
    os.getenv("CHEMISTRY_CORE12_SCHEMA_RETRIES", "2"),
))
MAX_JSON_PARSE_RETRIES = int(os.getenv(
    "CHEMISTRY_EVIDENCE15_V9_JSON_RETRIES",
    os.getenv("CHEMISTRY_CORE12_JSON_RETRIES", "2"),
))
CORE12_POSTPROCESS_PROFILE = os.getenv(
    "CHEMISTRY_EVIDENCE15_V9_POSTPROCESS_PROFILE",
    os.getenv("CHEMISTRY_CORE12_POSTPROCESS_PROFILE", "evidence15_boundary_rules_v9_stage1"),
).strip()
CORE12_SAFE_RULES_APPROVED = os.getenv(
    "CHEMISTRY_CORE12_SAFE_RULES_APPROVED", "0"
).strip().lower() in {"1", "true", "yes", "on"}

FILE_LOCK = Lock()
CACHE_LOCK = Lock()
CACHE_GET_LOCK = Lock()

CACHE_EXPIRE_DAYS = 6
CACHE_EXPIRE_SECONDS = CACHE_EXPIRE_DAYS * 24 * 3600
CACHE_FILE_PATH = "chemistry_prompt_cache.json"

DIFFICULTY_RATING_PROMPT_PREFIX = ""
DIFFICULTY_RATING_PROMPT_SUFFIX = ""

LEVEL_MAP = {
    "送分题": 1,
    "基础题": 2,
    "中等题": 3,
    "拔高题": 4,
    "压轴题": 5,
}

VALID_LEVELS = set(LEVEL_MAP.keys())

# -------------------------- 1. 提示词加载 --------------------------
def load_prompt_config(prompt_path: str) -> None:
    """动态解析提示词文件，支持 Python 变量格式与纯文本格式。"""
    global DIFFICULTY_RATING_PROMPT_PREFIX, DIFFICULTY_RATING_PROMPT_SUFFIX

    if not os.path.exists(prompt_path):
        print(f"错误: 找不到提示词文件 {prompt_path}！")
        sys.exit(1)

    with open(prompt_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 优先兼容物理/数学已有 Python 变量结构
    try:
        namespace: Dict[str, Any] = {}
        exec(content, namespace)
        prefix = namespace.get("DIFFICULTY_RATING_PROMPT_PREFIX")
        suffix = namespace.get("DIFFICULTY_RATING_PROMPT_SUFFIX")
        if prefix and suffix:
            DIFFICULTY_RATING_PROMPT_PREFIX = str(prefix)
            DIFFICULTY_RATING_PROMPT_SUFFIX = str(suffix)
            print("成功以 Python 变量结构解析提示词")
            return
    except Exception:
        pass

    # 兼容纯文本提示词
    if "## 输入题目信息" in content:
        parts = content.split("## 输入题目信息")
        DIFFICULTY_RATING_PROMPT_PREFIX = parts[0] + "## 输入题目信息"
        DIFFICULTY_RATING_PROMPT_SUFFIX = "\n\n请根据以上信息，对题目进行全面的难度分析和评级。"
        print("成功以纯文本标志位结构切分并解析提示词")
        return

    raise ValueError("提示词格式不正确：既不是有效 Python 变量结构，也没有包含 '## 输入题目信息' 分割标志。")

# -------------------------- 2. 前缀缓存模块 --------------------------
def compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

async def load_cache() -> Dict[str, Any]:
    async with CACHE_LOCK:
        if not os.path.exists(CACHE_FILE_PATH):
            return {}
        try:
            async with aiofiles.open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content) if content else {}
        except Exception as e:
            print(f"加载缓存文件失败: {e}")
            return {}

async def save_cache(cache_data: Dict[str, Any]) -> None:
    async with CACHE_LOCK:
        try:
            async with aiofiles.open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
                await f.write(json.dumps(cache_data, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"保存缓存文件失败: {e}")


def is_cache_valid(cache_entry: Dict[str, Any], current_time: int) -> bool:
    if not cache_entry:
        return False
    if current_time >= int(cache_entry.get("expire_at", 0)):
        return False
    return (
        cache_entry.get("prefix_hash", "") == compute_text_hash(DIFFICULTY_RATING_PROMPT_PREFIX)
        and cache_entry.get("model_name") == MODEL_NAME
    )

async def get_valid_cache() -> Optional[Dict[str, Any]]:
    cache_data = await load_cache()
    cache_entry = cache_data.get("prompt_prefix_cache")
    if is_cache_valid(cache_entry, int(time.time())):
        return cache_entry
    return None

async def set_cache(response_id: str, expire_at: int) -> None:
    cache_data = await load_cache()
    cache_data["prompt_prefix_cache"] = {
        "response_id": response_id,
        "expire_at": expire_at,
        "prefix_hash": compute_text_hash(DIFFICULTY_RATING_PROMPT_PREFIX),
        "model_name": MODEL_NAME,
        "created_at": int(time.time()),
    }
    await save_cache(cache_data)

async def create_prefix_cache(session: aiohttp.ClientSession, retries: int, timeout_sec: int) -> Optional[str]:
    current_time = int(time.time())
    expire_at = current_time + CACHE_EXPIRE_SECONDS

    payload = {
        "model": MODEL_NAME,
        "input": [{"role": "user", "content": DIFFICULTY_RATING_PROMPT_PREFIX}],
        "thinking": {"type": "disabled"},
        "expire_at": expire_at,
        "caching": {"type": "enabled", "prefix": True},
    }

    t1 = time.time()
    for attempt in range(retries):
        try:
            async with session.post(
                f"{BASE_URL}responses",
                json=payload,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    print(f"创建前缀缓存失败 (状态码: {response.status}): {error_text[:200]}")
                    if 400 <= response.status < 500:
                        return None
                    await asyncio.sleep(2 ** attempt)
                    continue

                result = await response.json()
                response_id = result.get("id")
                if response_id:
                    await set_cache(response_id, expire_at)
                    print(f"前缀缓存创建成功，耗时: {time.time() - t1:.2f}秒，缓存ID: {response_id}")
                    return response_id
        except Exception as e:
            backoff = (2 ** attempt) + random.uniform(0, 1)
            if attempt == retries - 1:
                print(f"创建前缀缓存最终失败: {e}")
                return None
            print(f"创建前缀缓存异常，{backoff:.2f}秒后重试: {e}")
            await asyncio.sleep(backoff)
    return None

async def get_or_create_cache(session: aiohttp.ClientSession, retries: int, timeout_sec: int) -> Optional[str]:
    async with CACHE_GET_LOCK:
        cache_entry = await get_valid_cache()
        if cache_entry:
            return cache_entry["response_id"]
        print("未找到有效缓存，正在向服务器创建前缀缓存...")
        return await create_prefix_cache(session, retries, timeout_sec)

# -------------------------- 3. 化学特征 schema 与归一化 --------------------------
FEATURE_DEFAULTS = {
    "step_count": "1-2步",
    "equation_count": "0-1个",
    "calculation_complexity": "口算或直接判断",
    "reasoning_chain": "直接套用",
    "problem_structure": "概念判断",
    "additional_structure": "无",
    "information_carrier": "纯文字",
    "reality_question": "否",
    "subquestion_dependency": "无多问",
    "knowledge_count": "1个",
    "knowledge_diff": "低",
    "cross_module": "同一模块内部",
    "chemistry_process_count": "单一事实",
    "constraint_count": "无约束",
    "evidence_relation": "无证据链",
    "experiment_requirement": "无",
    "graph_table_requirement": "无",
    "error_risk": "无明显易错点",
}

ALLOWED_FEATURE_VALUES = {
    "step_count": {"1-2步", "3-5步", "6-8步", "9-12步", "12步以上"},
    "equation_count": {"0-1个", "2-3个", "4-6个", "7个以上"},
    "calculation_complexity": {"口算或直接判断", "简单笔算", "化学方程式计算或关系式计算", "复杂守恒或图像计算"},
    "reasoning_chain": {"直接套用", "简单因果推理", "多层证据推理", "逆向推理或方案评价"},
    "problem_structure": {"概念判断", "化学用语与分类", "方程式书写", "实验基础操作", "实验探究", "工艺流程", "图像表格分析", "物质推断", "计算综合", "跨模块综合"},
    "additional_structure": {"无", "微观示意图", "实验装置", "流程图", "图像表格", "探究材料", "多模块综合"},
    "information_carrier": {"纯文字", "单图识别", "微观示意图", "实验装置图", "流程图", "图像或表格", "多图表综合"},
    "reality_question": {"是", "否"},
    "subquestion_dependency": {"无多问", "多问但相互独立", "多问且层层递进"},
    "knowledge_count": {"1个", "2-3个", "4个及以上"},
    "knowledge_diff": {"低", "中", "高"},
    "cross_module": {"同一模块内部", "跨模块综合"},
    "chemistry_process_count": {"单一事实", "单一反应", "2-3个反应或过程", "多反应连续转化或流程"},
    "constraint_count": {"无约束", "单一约束", "多约束"},
    "evidence_relation": {"无证据链", "单一现象对应", "多现象证据链", "证据冲突与排除"},
    "experiment_requirement": {"无", "基础操作或读数", "控制变量或现象分析", "方案设计或误差评价"},
    "graph_table_requirement": {"无", "直接读数", "多组比较归纳", "图像反推或拐点分析"},
    "error_risk": {"无明显易错点", "轻微易错点", "明显易错点", "高易错点"},
}

ENUM_NORMALIZE = {
    "equation_count": {
        "公式数量": "0-1个",
        "0个": "0-1个",
        "1个": "0-1个",
        "1-2个": "2-3个",
        "1-3个": "2-3个",
        "2个": "2-3个",
        "3个": "2-3个",
        "4个以上": "4-6个",
        "7个以上方程式": "7个以上",
    },
    "knowledge_count": {
        "1-2个": "2-3个",
        "2个": "2-3个",
        "3个": "2-3个",
        "2-4个": "2-3个",
        "4个以上": "4个及以上",
        "多个": "4个及以上",
    },
    "information_carrier": {
        "图像": "图像或表格",
        "图象": "图像或表格",
        "表格": "图像或表格",
        "实验图": "实验装置图",
        "装置图": "实验装置图",
        "流程": "流程图",
        "流程图+表格": "多图表综合",
        "实验装置图+表格": "多图表综合",
        "实验装置图和图像": "多图表综合",
    },
    "additional_structure": {
        "实验图": "实验装置",
        "实验装置图": "实验装置",
        "图像": "图像表格",
        "表格": "图像表格",
        "图表": "图像表格",
        "流程": "流程图",
        "流程图": "流程图",
        "项目式": "探究材料",
    },
    "experiment_requirement": {
        "方案设计": "方案设计或误差评价",
        "误差分析": "方案设计或误差评价",
        "误差评价": "方案设计或误差评价",
        "控制变量": "控制变量或现象分析",
        "现象分析": "控制变量或现象分析",
        "故障分析": "控制变量或现象分析",
        "数据归纳": "控制变量或现象分析",
    },
    "graph_table_requirement": {
        "图像反推": "图像反推或拐点分析",
        "图象反推": "图像反推或拐点分析",
        "拐点分析": "图像反推或拐点分析",
        "直接读取": "直接读数",
    },
}

def clean_enum_value(value: Any) -> str:
    if value is None:
        return ""
    v = str(value).strip()
    v = (
        v.replace("，", ",")
        .replace("、", ",")
        .replace("；", ";")
        .replace("：", ":")
        .replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\n", "")
        .replace("\t", "")
    )
    return v.strip('";,.:。')


def canonicalize_feature_value(field: str, value: Any) -> str:
    v = clean_enum_value(value)
    if not v:
        return FEATURE_DEFAULTS[field]

    if field == "step_count":
        if "12" in v or "十二" in v:
            return "12步以上"
        if any(k in v for k in ["9-12", "9到12", "九", "十", "11"]):
            return "9-12步"
        if any(k in v for k in ["6-8", "6到8", "六", "七", "八"]):
            return "6-8步"
        if any(k in v for k in ["3-5", "3到5", "三", "四", "五"]):
            return "3-5步"
        return "1-2步"

    if field == "equation_count":
        if "7" in v or "七" in v:
            return "7个以上"
        if any(k in v for k in ["4-6", "4到6", "四", "五", "六", "4个以上"]):
            return "4-6个"
        if any(k in v for k in ["2-3", "2到3", "2个", "3个", "两", "二", "三"]):
            return "2-3个"
        return "0-1个"

    if field == "calculation_complexity":
        if any(k in v for k in ["复杂", "守恒", "图像", "图象", "拐点", "混合物", "差量", "极值", "范围", "分类", "多变量"]):
            return "复杂守恒或图像计算"
        if any(k in v for k in ["方程式", "关系式", "质量守恒", "根据化学方程式"]):
            return "化学方程式计算或关系式计算"
        if any(k in v for k in ["简单", "笔算", "化合价", "相对分子", "质量分数", "代入"]):
            return "简单笔算"
        return "口算或直接判断"

    if field == "reasoning_chain":
        if any(k in v for k in ["逆向", "反推", "方案", "评价", "排除", "冲突", "干扰", "拐点", "先后"]):
            return "逆向推理或方案评价"
        if any(k in v for k in ["多层", "证据", "多步", "链条", "综合", "归纳"]):
            return "多层证据推理"
        if any(k in v for k in ["简单", "因果", "对应"]):
            return "简单因果推理"
        return "直接套用"

    if field == "problem_structure":
        if any(k in v for k in ["跨模块", "综合"]):
            # 若明确是实验/流程/计算综合，优先保留具体结构
            if any(k in v for k in ["流程", "工艺"]):
                return "工艺流程"
            if any(k in v for k in ["实验", "探究"]):
                return "实验探究"
            if any(k in v for k in ["计算", "守恒"]):
                return "计算综合"
            return "跨模块综合"
        if any(k in v for k in ["流程", "工艺", "制备"]):
            return "工艺流程"
        if any(k in v for k in ["图像", "图象", "表格", "曲线"]):
            return "图像表格分析"
        if any(k in v for k in ["推断", "鉴别", "除杂", "共存", "变质", "成分"]):
            return "物质推断"
        if any(k in v for k in ["计算", "守恒", "质量分数", "溶质质量分数", "关系式"]):
            return "计算综合"
        if any(k in v for k in ["实验探究", "猜想", "评价", "反思", "方案"]):
            return "实验探究"
        if any(k in v for k in ["实验", "操作", "仪器", "过滤", "蒸馏"]):
            return "实验基础操作"
        if any(k in v for k in ["方程式", "配平"]):
            return "方程式书写"
        if any(k in v for k in ["化学式", "化学用语", "分类", "化合价", "元素", "微粒", "离子"]):
            return "化学用语与分类"
        return "概念判断"

    if field == "additional_structure":
        if any(k in v for k in ["多模块", "跨模块"]):
            return "多模块综合"
        if any(k in v for k in ["流程", "工艺"]):
            return "流程图"
        if any(k in v for k in ["实验装置", "装置", "仪器"]):
            return "实验装置"
        if any(k in v for k in ["图像", "图象", "表格", "曲线"]):
            return "图像表格"
        if any(k in v for k in ["探究", "项目", "材料", "猜想"]):
            return "探究材料"
        if any(k in v for k in ["微观", "粒子", "结构示意图"]):
            return "微观示意图"
        return "无"

    if field == "information_carrier":
        has_flow = any(k in v for k in ["流程", "工艺"])
        has_exp = any(k in v for k in ["实验装置", "装置图", "实验图"])
        has_micro = any(k in v for k in ["微观", "粒子", "结构示意图"])
        has_graph = any(k in v for k in ["图像", "图象", "曲线", "图"])
        has_table = any(k in v for k in ["表格", "表"])
        if sum([has_flow, has_exp, has_micro, has_graph or has_table]) >= 2:
            return "多图表综合"
        if has_flow:
            return "流程图"
        if has_exp:
            return "实验装置图"
        if has_micro:
            return "微观示意图"
        if has_graph or has_table:
            return "图像或表格"
        if "单图" in v:
            return "单图识别"
        return "纯文字"

    if field == "reality_question":
        if v.lower() in ["true", "yes", "y", "1"] or "是" in v:
            return "是"
        return "否"

    if field == "subquestion_dependency":
        if any(k in v for k in ["层层", "递进", "依赖", "承接"]):
            return "多问且层层递进"
        if any(k in v for k in ["多问", "小题", "独立"]):
            return "多问但相互独立"
        return "无多问"

    if field == "knowledge_count":
        if any(k in v for k in ["4个及以上", "4个以上", "四个", "多个", "多知识点"]):
            return "4个及以上"
        if any(k in v for k in ["2-3", "2到3", "2个", "3个", "两", "二", "三"]):
            return "2-3个"
        if any(k in v for k in ["1个", "一个", "单一"]):
            return "1个"
        return "2-3个"

    if field == "knowledge_diff":
        if any(k in v for k in ["高", "难", "复杂"]):
            return "高"
        if any(k in v for k in ["中", "一般"]):
            return "中"
        return "低"

    if field == "cross_module":
        if "跨" in v or "综合" in v:
            return "跨模块综合"
        return "同一模块内部"

    if field == "chemistry_process_count":
        if any(k in v for k in ["多反应", "连续", "流程", "多阶段", "多步转化", "先后反应"]):
            return "多反应连续转化或流程"
        if any(k in v for k in ["2-3", "2到3", "两个", "三个", "若干", "多过程"]):
            return "2-3个反应或过程"
        if any(k in v for k in ["单一反应", "一个反应", "方程式"]):
            return "单一反应"
        return "单一事实"

    if field == "constraint_count":
        if any(k in v for k in ["多", "多个", "过量", "不足", "先后", "共同约束"]):
            return "多约束"
        if any(k in v for k in ["单", "一个", "有约束", "约束"]):
            return "单一约束"
        return "无约束"

    if field == "evidence_relation":
        if any(k in v for k in ["冲突", "排除", "干扰", "质疑", "反证"]):
            return "证据冲突与排除"
        if any(k in v for k in ["多现象", "多证据", "证据链", "多个现象", "综合现象"]):
            return "多现象证据链"
        if any(k in v for k in ["单一现象", "现象对应", "直接对应"]):
            return "单一现象对应"
        return "无证据链"

    if field == "experiment_requirement":
        if any(k in v for k in ["方案", "设计", "误差", "评价", "反思", "补充实验", "改进", "可靠性"]):
            return "方案设计或误差评价"
        if any(k in v for k in ["控制变量", "现象分析", "对照", "故障", "数据归纳", "探究", "分析"]):
            return "控制变量或现象分析"
        if any(k in v for k in ["读数", "操作", "仪器", "过滤", "蒸馏", "检验"]):
            return "基础操作或读数"
        return "无"

    if field == "graph_table_requirement":
        if any(k in v for k in ["反推", "拐点", "平台", "外推", "曲线关系", "图像分析", "图象分析"]):
            return "图像反推或拐点分析"
        if any(k in v for k in ["多组", "比较", "归纳", "趋势"]):
            return "多组比较归纳"
        if any(k in v for k in ["读数", "读取", "直接"]):
            return "直接读数"
        return "无"

    if field == "error_risk":
        if "高" in v:
            return "高易错点"
        if any(k in v for k in ["明显", "较大", "易错"]):
            return "明显易错点"
        if any(k in v for k in ["轻微", "较小"]):
            return "轻微易错点"
        return "无明显易错点"

    return FEATURE_DEFAULTS[field]


def normalize_feature_keys(features: Dict[str, Any]) -> Dict[str, Any]:
    fixed: Dict[str, Any] = {}
    key_aliases = {
        "formula_count": "equation_count",
        "chemical_equation_count": "equation_count",
        "equations_count": "equation_count",
        "reaction_count": "chemistry_process_count",
        "process_count": "chemistry_process_count",
        "state_count": "chemistry_process_count",
        "variable_relation": "evidence_relation",
    }
    for k, v in (features or {}).items():
        clean_key = str(k).strip().strip('",， \n\t')
        clean_key = key_aliases.get(clean_key, clean_key)
        for standard_key in FEATURE_DEFAULTS.keys():
            if standard_key in clean_key:
                clean_key = standard_key
                break
        fixed[clean_key] = v
    return fixed


def normalize_features(features: Dict[str, Any]) -> Dict[str, Any]:
    features = normalize_feature_keys(features or {})
    normalized: Dict[str, str] = {}
    for field, default in FEATURE_DEFAULTS.items():
        value = features.get(field, default)
        if field in ENUM_NORMALIZE and value in ENUM_NORMALIZE[field]:
            value = ENUM_NORMALIZE[field][value]
        clean_value = clean_enum_value(value)
        if field in ENUM_NORMALIZE and clean_value in ENUM_NORMALIZE[field]:
            value = ENUM_NORMALIZE[field][clean_value]
        if value in ALLOWED_FEATURE_VALUES[field]:
            normalized[field] = value
            continue
        value = canonicalize_feature_value(field, value)
        if value not in ALLOWED_FEATURE_VALUES[field]:
            value = default
        normalized[field] = value

    # 结构联动修正：实验/流程/图像载体应反映到 additional_structure。
    if normalized["problem_structure"] == "工艺流程" and normalized["additional_structure"] == "无":
        normalized["additional_structure"] = "流程图"
    if normalized["problem_structure"] == "实验探究" and normalized["additional_structure"] == "无":
        normalized["additional_structure"] = "探究材料"
    if normalized["problem_structure"] == "图像表格分析" and normalized["additional_structure"] == "无":
        normalized["additional_structure"] = "图像表格"

    return normalized

# -------------------------- 4. 后处理纠偏规则 --------------------------
def normalize_reasoning_schema(rating_result: Dict[str, Any]) -> None:
    reasoning = rating_result.get("reasoning")
    reason = rating_result.get("reason")
    normalized = {
        "core_basis": "",
        "hard_point": "",
        "why_not_lower": "",
        "why_not_higher": "",
    }
    if isinstance(reasoning, dict):
        normalized.update(reasoning)
    elif isinstance(reason, dict):
        normalized.update(reason)
    elif isinstance(reasoning, str) and reasoning:
        normalized["core_basis"] = reasoning
    elif isinstance(reason, str) and reason:
        normalized["core_basis"] = reason
    rating_result["reasoning"] = normalized
    rating_result.pop("reason", None)


def set_level_with_reason(rating_result: Dict[str, Any], level: str, core_basis_prefix: str) -> None:
    """设置后处理难度，并记录可审计的改档轨迹。

    v6.1 说明：
    - 不改变任何分类规则，只把每一次自动升/降档记录到 postprocess_trace；
    - 后续由 sync_reasoning_after_postprocess() 统一同步 why_not_lower / why_not_higher，
      避免最终档位与原始模型解释互相矛盾。
    """
    previous_level = rating_result.get("difficulty_level", "")
    rating_result.setdefault("postprocess_original_level", previous_level)
    rating_result.setdefault("postprocess_trace", [])
    if previous_level != level:
        rating_result["postprocess_trace"].append({
            "from": previous_level,
            "to": level,
            "reason": core_basis_prefix,
        })
    rating_result["postprocess_note"] = core_basis_prefix
    rating_result["difficulty_level"] = level

    reasoning = rating_result.setdefault("reasoning", {
        "core_basis": "",
        "hard_point": "",
        "why_not_lower": "",
        "why_not_higher": "",
    })
    original_basis = reasoning.get("core_basis", "")
    reasoning["core_basis"] = f"【{core_basis_prefix}】。原始依据：{original_basis}"


def sync_coarse_difficulty(rating_result: Dict[str, Any]) -> None:
    level = rating_result.get("difficulty_level", "")
    if level in ["送分题", "基础题"]:
        rating_result["coarse_difficulty"] = "送分/基础区间（1-2档）"
    elif level == "中等题":
        rating_result["coarse_difficulty"] = "基础/中等区间（2-3档）"
    elif level == "拔高题":
        rating_result["coarse_difficulty"] = "中等/拔高区间（3-4档）"
    elif level == "压轴题":
        rating_result["coarse_difficulty"] = "拔高/压轴区间（4-5档）"


def contains_any(text: str, keywords: List[str]) -> bool:
    return any(k in text for k in keywords)


def visible_text(data: Dict[str, Any], include_analysis: bool = False) -> str:
    parts = [str(data.get("stem", "") or ""), str(data.get("options", "") or "")]
    if include_analysis:
        parts.append(str(data.get("analysis", "") or ""))
    for sq in data.get("sub_questions", []) or []:
        if isinstance(sq, dict):
            parts.append(str(sq.get("stem", "") or ""))
            parts.append(str(sq.get("options", "") or ""))
            if include_analysis:
                parts.append(str(sq.get("analysis", "") or ""))
    return "\n".join(parts)


def count_fill_blanks(text: str) -> int:
    return len(re.findall(r"_{2,}|（\s*）|\(\s*\)", text))


def count_subquestions(data: Dict[str, Any]) -> int:
    subqs = data.get("sub_questions", []) or []
    if isinstance(subqs, list) and subqs:
        return len(subqs)
    text = str(data.get("stem", "") or "")
    return max(len(re.findall(r"\([一二三四五六七八九十0-9]+\)|（[一二三四五六七八九十0-9]+）", text)), 0)


LONG_CONTEXT_KEYWORDS = [
    "项目式", "任务一", "任务二", "探究", "猜想", "评价与反思", "提出问题", "实验探究", "实验验证",
    "工艺流程", "流程", "制备", "滤渣", "滤液", "循环", "定量", "滴加", "图像", "图象", "曲线",
    "pH", "溶解度曲线", "离子数目", "变质", "除杂", "鉴别", "推断", "方案设计", "误差分析",
]


def is_long_context_or_new_situation(data: Dict[str, Any]) -> bool:
    stem = str(data.get("stem", "") or "")
    if len(stem) > 260:
        return True
    if contains_any(stem, LONG_CONTEXT_KEYWORDS):
        return True
    if count_subquestions(data) >= 4:
        return True
    return False


def is_trivial_concept_question(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    text = visible_text(data, include_analysis=False)
    return (
        len(text) < 120
        and features.get("step_count") == "1-2步"
        and features.get("equation_count") == "0-1个"
        and features.get("calculation_complexity") == "口算或直接判断"
        and features.get("reasoning_chain") == "直接套用"
        and features.get("knowledge_count") == "1个"
        and features.get("knowledge_diff") == "低"
        and features.get("experiment_requirement") == "无"
        and features.get("graph_table_requirement") == "无"
    )



def is_pure_direct_recall_set(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """低阶直接识记集合题。

    用于纠正“空气成分用途/食品安全/身体健康常识”这类多选项、多填空被误升为基础题的情况。
    注意：只在无实验、无计算、无图表、无方程式推导时生效。
    """
    text = visible_text(data, include_analysis=True)
    simple_no_process = (
        features.get("equation_count") == "0-1个"
        and features.get("calculation_complexity") == "口算或直接判断"
        and features.get("experiment_requirement") == "无"
        and features.get("graph_table_requirement") == "无"
        and features.get("chemistry_process_count") in ["单一事实", "单一反应"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("constraint_count") in ["无约束", "单一约束"]
        and features.get("information_carrier") in ["纯文字", "单图识别", "微观示意图"]
    )
    # 只覆盖“极低阶固定集合直接匹配”，避免把普通多选项概念辨析误降为送分题。
    direct_recall_patterns = [
        "①氧气", "②氮气", "③二氧化碳", "④稀有气体", "从①氧气", "选择适当的物质填空",
        "化学与我们的身体健康息息相关", "食品安全", "霉变大米", "公共场所禁止吸烟", "甲醛", "二氧化硫漂白",
        "化学发展史", "化学发展简史", "发展简史", "化学史", "化学家", "贡献",
        "侯德榜", "屠呦呦", "徐光宪", "张青莲", "门捷列夫", "拉瓦锡", "道尔顿",
    ]
    hard_exclusion_keywords = [
        "化学方程式", "配平", "计算", "质量分数", "溶质质量分数", "实验探究", "方案", "流程", "图像", "图象",
        "滤渣", "滤液", "变质", "推断", "鉴别", "除杂", "金属活动性", "置换"
    ]
    air_component_direct = (
        "①氧气" in text and "②氮气" in text and "③二氧化碳" in text and "④稀有气体" in text
    )
    health_direct = (
        "化学与我们的身体健康息息相关" in text
        or "食品安全" in text
        or ("霉变" in text and "甲醛" in text and "二氧化硫" in text)
    )
    history_direct = (
        "化学发展史" in text
        or "化学发展简史" in text
        or "发展简史" in text
        or "化学史" in text
        or ("化学家" in text and "贡献" in text)
        or contains_any(text, ["侯德榜", "屠呦呦", "徐光宪", "张青莲", "门捷列夫", "拉瓦锡", "道尔顿"])
    )
    if not simple_no_process:
        return False
    if history_direct:
        # 化学史题的解析可能出现“工艺流程/制碱工艺”等词，但它们只是人物贡献表述，不代表题目需要流程分析。
        return True
    return (air_component_direct or health_direct) and not contains_any(text, hard_exclusion_keywords)


def is_low_level_basic_application(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """基础题保护：多个独立基础空、一个方程式、化学式/质量守恒直接应用，不自动升中等。"""
    text = visible_text(data, include_analysis=True)
    if contains_any(text, [
        "滤渣", "滤液", "先后反应", "过量", "不足", "拐点", "平台", "离子数目", "压强变化图", "曲线",
        "方案评价", "质疑", "可靠性", "补充实验", "控制变量", "图像反推", "关系式法", "差量法", "元素守恒",
        "生成等量氢气", "相同质量", "不同金属", "金属用量", "制取氢气", "尾气处理", "节约能源", "炼铁"
    ]):
        return False

    return (
        features.get("step_count") in ["1-2步", "3-5步"]
        and features.get("equation_count") in ["0-1个", "2-3个"]
        and features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]
        and features.get("reasoning_chain") in ["直接套用", "简单因果推理"]
        and features.get("chemistry_process_count") in ["单一事实", "单一反应", "2-3个反应或过程"]
        and features.get("constraint_count") in ["无约束", "单一约束"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("experiment_requirement") in ["无", "基础操作或读数"]
        and features.get("graph_table_requirement") in ["无", "直接读数"]
        and features.get("subquestion_dependency") != "多问且层层递进"
        and features.get("information_carrier") not in ["流程图", "多图表综合"]
    )


def is_standard_experiment_basic(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """标准实验基础题：气体制取、收集、验满、仪器、蜡烛/氧气性质等常规操作。"""
    text = visible_text(data, include_analysis=True)
    standard_keywords = [
        "气体制取", "制取氧气", "制取二氧化碳", "制取氢气", "发生装置", "收集装置", "验满", "检验",
        "试管", "长颈漏斗", "集气瓶", "排水法", "向上排空气", "向下排空气", "蜡烛燃烧", "氧气性质",
        "硫燃烧", "铁丝燃烧", "木炭燃烧", "过滤", "蒸馏", "玻璃棒", "水的净化"
    ]
    hard_exclusion_keywords = [
        "方案评价", "误差", "质疑", "补充实验", "可靠性", "控制变量", "图像", "图象", "曲线", "表格", "滤渣", "滤液",
        "变质", "混合物", "质量分数", "守恒", "关系式", "过量", "不足", "先后反应", "金属活动性", "尾气处理", "炼铁", "氧气含量", "气球", "压强", "制取氢气", "锌粒", "稀硫酸", "多孔隔板"
    ]
    return (
        contains_any(text, standard_keywords)
        and not contains_any(text, hard_exclusion_keywords)
        and features.get("step_count") in ["1-2步", "3-5步", "6-8步"]
        and features.get("equation_count") in ["0-1个", "2-3个"]
        and features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]
        and features.get("experiment_requirement") in ["基础操作或读数", "无"]
        and features.get("graph_table_requirement") in ["无", "直接读数"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("subquestion_dependency") != "多问且层层递进"
    )


def is_standard_experiment_medium_combo(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """标准实验组合题：多个重要实验并列考查，有装置、方程式、现象/压强等综合，但无拔高卡点。"""
    text = visible_text(data, include_analysis=True)
    return (
        features.get("information_carrier") in ["实验装置图", "多图表综合"]
        and count_subquestions(data) >= 4
        and features.get("equation_count") in ["0-1个", "2-3个"]
        and features.get("experiment_requirement") in ["基础操作或读数", "控制变量或现象分析"]
        and features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]
        and contains_any(text, ["装置", "实验", "化学方程式", "气球", "压强", "尾气处理", "炼铁", "氧气含量"])
        and not contains_any(text, ["方案评价", "质疑", "补充实验", "复杂守恒", "图像反推", "拐点", "滤渣", "滤液"])
    )


def is_long_reading_direct_info(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """长阅读材料但只做信息定位/常识填空，不能因材料长自动判中等。"""
    text = visible_text(data, include_analysis=True)
    return (
        len(str(data.get("stem", "") or "")) > 220
        and features.get("step_count") in ["1-2步", "3-5步"]
        and features.get("equation_count") == "0-1个"
        and features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]
        and features.get("experiment_requirement") in ["无", "基础操作或读数"]
        and features.get("graph_table_requirement") in ["无", "直接读数"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("chemistry_process_count") in ["单一事实", "单一反应", "2-3个反应或过程"]
        and not contains_any(text, ["方案评价", "补充实验", "质疑", "滤渣", "滤液", "拐点", "平台", "定量计算", "守恒", "混合物"])
    )


def is_single_path_standard_calculation(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """单线标准计算：沉淀/气体质量反推、纯度计算等，模型单一时最高多为中等。"""
    text = visible_text(data, include_analysis=True)
    return (
        features.get("problem_structure") == "计算综合"
        and features.get("equation_count") in ["0-1个", "2-3个"]
        and features.get("chemistry_process_count") in ["单一反应", "2-3个反应或过程"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("experiment_requirement") in ["无", "基础操作或读数"]
        and features.get("graph_table_requirement") in ["无", "直接读数"]
        and not contains_any(text, ["拐点", "平台", "曲线", "图像", "图象", "多种", "不可能", "极值", "范围", "分类讨论"])
    )


def is_multi_standard_lab_independent_basic(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """多个标准实验操作并列题：加热、过滤、气体制取、验满等独立考查，通常为基础题。"""
    text = visible_text(data, include_analysis=True)
    standard_hits = 0
    for group in [
        ["加热液体", "给液体加热"],
        ["过滤", "滤纸", "漏斗", "玻璃棒"],
        ["制取氧气", "氧气的制取", "实验室制氧"],
        ["制取二氧化碳", "二氧化碳的制取", "实验室制取CO2", "实验室制取二氧化碳"],
        ["验满", "检验", "收集装置", "发生装置"],
    ]:
        if contains_any(text, group):
            standard_hits += 1
    hard_exclusion_keywords = [
        "控制变量", "对照实验", "方案", "方案评价", "误差", "质疑", "可靠性", "补充实验", "改进",
        "压强", "曲线", "图像", "图象", "表格", "质量分数", "纯度", "守恒", "关系式", "差量",
        "滤渣", "滤液", "金属活动性", "过量", "不足", "先后反应", "尾气处理", "炼铁", "产率"
    ]
    return (
        standard_hits >= 2
        and not contains_any(text, hard_exclusion_keywords)
        and features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]
        and features.get("experiment_requirement") in ["无", "基础操作或读数", "控制变量或现象分析"]
        and features.get("graph_table_requirement") in ["无", "直接读数"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
    )


def is_air_oxygen_pressure_standard_medium(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """红磷测空气中氧气含量的压强曲线/气球变化：标准实验图像分析，通常为中等题而非拔高题。"""
    text = visible_text(data, include_analysis=True)
    return (
        contains_any(text, ["红磷", "测定空气中氧气含量", "空气中氧气含量", "氧气含量"])
        and contains_any(text, ["压强", "气压", "压力", "气球", "曲线", "图像", "图象", "图 2", "图2"])
        and not contains_any(text, ["方案评价", "质疑", "补充实验", "误差分析", "复杂守恒", "质量分数", "纯度", "滤渣", "滤液"])
    )


def is_bicarbonate_purity_hard(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """NaHCO3/小苏打性质表格 + 样品纯度/质量分数计算，通常有实验归纳和定量计算卡点，判拔高。"""
    text = visible_text(data, include_analysis=True)
    return (
        contains_any(text, ["NaHCO3", "NaHCO₃", "碳酸氢钠", "小苏打"])
        and contains_any(text, ["纯度", "样品中", "含量"])
        and contains_any(text, ["表格", "数据", "质量差", "反应前后", "反思", "测定"])
        and features.get("calculation_complexity") in ["化学方程式计算或关系式计算", "复杂守恒或图像计算"]
        and (
            features.get("information_carrier") == "多图表综合"
            or features.get("subquestion_dependency") == "多问且层层递进"
            or features.get("graph_table_requirement") == "图像反推或拐点分析"
        )
        and not ("配制一定质量分数" in text and features.get("subquestion_dependency") == "多问但相互独立")
    )


def is_common_substance_network_inference(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """A-E 常见物质转化推断：若需结合转化关系、颜色/黑色固体/CO-CuO等线索，通常至少中等题。"""
    text = visible_text(data, include_analysis=True)
    compact_text = re.sub(r"\s+", "", text)
    has_letters = (
        bool(re.search(r"A[~\-—至到、,，和]+[B-E]", compact_text))
        or contains_any(compact_text, ["A、B、C、D、E", "A～E", "A-E", "A~E", "ABCDE"])
    )
    return (
        has_letters
        and contains_any(text, ["常见物质", "物质转化", "转化关系", "推断", "反应关系", "框图"])
        and not contains_any(text, ["对于化学反应", "A}+\\mathrm{B}", "A+B", "置换反应", "复分解反应", "中和反应"])
    )




def is_solution_classification_basic(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """溶液/非溶液分类辨析：不是纯记忆，一般至少基础题。"""
    text = visible_text(data, include_analysis=True)
    return (
        contains_any(text, ["不属于溶液", "属于溶液", "溶液的是", "溶液的说法", "溶液中"])
        and not contains_any(text, ["溶质质量分数", "质量分数", "曲线", "图像", "图象", "配制", "计算"])
    )


def is_co_reduction_combustion_combo_medium(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """CO还原氧化铁 + 燃烧条件/尾气处理组合实验：超过基础操作，通常中等。"""
    text = visible_text(data, include_analysis=True)
    has_co_reduction = contains_any(text, ["CO", "一氧化碳"]) and contains_any(text, ["Fe2O3", "氧化铁", "还原氧化铁", "炼铁"])
    has_combustion = contains_any(text, ["燃烧条件", "燃烧的条件", "铁粉", "脱脂棉", "红磷", "白磷"])
    has_lab_combo = contains_any(text, ["实验 1", "实验1", "实验 2", "实验2", "尾气处理", "酒精灯", "装置"])
    hard_exclusion = contains_any(text, ["质量分数", "纯度", "守恒", "关系式", "图像反推", "拐点", "滤渣", "滤液", "方案评价"])
    return has_co_reduction and has_combustion and has_lab_combo and not hard_exclusion


def is_complex_equation_balancing_medium(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """陌生复杂方程式配平：需要元素守恒列系数关系，通常中等。"""
    text = visible_text(data, include_analysis=True)
    compact = re.sub(r"\s+", "", text)
    return (
        contains_any(text, ["配平", "化学计量数", "计量数"])
        and (
            contains_any(compact, ["S8", "Ca(OH)2", "CaS5", "CaS2O3"])
            or len(re.findall(r"[A-Z][a-z]?(?:_?\{?\d+\}?|\d*)", compact)) >= 6
        )
        and not contains_any(text, ["选择合适装置", "实验探究", "流程", "滤渣", "滤液"])
    )


def is_unfamiliar_material_transfer_medium(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """陌生材料迁移题：需要根据材料迁移相对分子质量、质量守恒、化合价/氧化还原方向，通常中等。"""
    text = visible_text(data, include_analysis=True)
    return (
        contains_any(text, ["阅读材料", "三氧化二碳", "C2O3", "C 2 O 3", "某星球", "化学性质与一氧化碳相似"])
        and contains_any(text, ["相对分子质量", "质量守恒", "化合价", "氧化", "还原", "酸性"])
        and not contains_any(text, ["图像", "图象", "曲线", "复杂守恒", "多变量", "方案评价"])
    )


def is_standard_precipitation_purity_table_medium(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """碳酸钠样品纯度 + 氯化钙沉淀表格：平台读数 + 单方程式计算，通常中等而非拔高。"""
    text = visible_text(data, include_analysis=True)
    return (
        contains_any(text, ["碳酸钠样品", "Na2CO3", "Na₂CO₃"])
        and contains_any(text, ["纯度", "质量分数", "含量"])
        and contains_any(text, ["氯化钙", "CaCl2", "CaCl₂", "沉淀", "平均分", "四份", "表"])
        and not contains_any(text, ["滤渣", "滤液", "过量不足", "先后反应", "拐点", "曲线", "图像", "图象", "方案评价", "干扰", "混合物中多种"])
    )


def is_single_reaction_decomposition_graph_medium(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """KClO3 单一分解反应质量变化图：常规图像辨析，通常中等而非拔高。"""
    text = visible_text(data, include_analysis=True)
    return (
        contains_any(text, ["KClO3", "KClO₃", "氯酸钾"])
        and contains_any(text, ["MnO2", "MnO₂", "二氧化锰"])
        and contains_any(text, ["分解", "加热", "质量", "图", "图像", "图象", "曲线"])
        and not contains_any(text, ["纯度", "质量分数", "过量", "不足", "滤渣", "滤液", "方案评价", "多反应", "多种金属", "混合物计算"])
    )

def should_downgrade_basic_to_easy(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    # 低阶直接识记集合题允许基础 -> 送分。
    if is_pure_direct_recall_set(features, data):
        return True

    if is_long_context_or_new_situation(data):
        return False

    text = visible_text(data, include_analysis=False)
    if ("下列" in text or "说法" in text or "正确的是" in text or "错误的是" in text) and len(text) > 90:
        return False

    # 多问/多空不再默认降为送分，避免宏观-微观-符号多小问被误降。
    if features.get("subquestion_dependency") != "无多问" or count_subquestions(data) > 0:
        return False

    simple_problem = features.get("problem_structure") in ["概念判断", "化学用语与分类"]
    simple_carrier = features.get("information_carrier") in ["纯文字", "单图识别", "微观示意图"]
    return (
        simple_problem
        and simple_carrier
        and features.get("step_count") == "1-2步"
        and features.get("equation_count") == "0-1个"
        and features.get("calculation_complexity") == "口算或直接判断"
        and features.get("reasoning_chain") == "直接套用"
        and features.get("knowledge_count") == "1个"
        and features.get("knowledge_diff") == "低"
        and features.get("chemistry_process_count") in ["单一事实", "单一反应"]
        and features.get("constraint_count") == "无约束"
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("experiment_requirement") == "无"
        and features.get("graph_table_requirement") == "无"
    )


def should_upgrade_easy_to_basic(features: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    text = visible_text(data, include_analysis=True)
    stem_options = visible_text(data, include_analysis=False)

    if is_pure_direct_recall_set(features, data):
        return []

    if is_solution_classification_basic(features, data):
        reasons.append("溶液/非溶液属于物质分类概念辨析，至少基础题")

    if features.get("step_count") != "1-2步":
        reasons.append(f'解题步骤数为"{features.get("step_count")}"')
    if features.get("knowledge_count") != "1个":
        reasons.append(f'知识点数量为"{features.get("knowledge_count")}"')
    if features.get("equation_count") != "0-1个":
        reasons.append("涉及多个化学方程式或反应关系")
    if features.get("calculation_complexity") in ["简单笔算", "化学方程式计算或关系式计算", "复杂守恒或图像计算"]:
        reasons.append(f'计算复杂度为"{features.get("calculation_complexity")}"')
    if features.get("experiment_requirement") != "无":
        reasons.append("含实验操作、现象分析或探究要求")
    if features.get("information_carrier") in ["实验装置图", "流程图", "图像或表格", "多图表综合"]:
        reasons.append(f'信息载体为"{features.get("information_carrier")}"，不属于单一概念直答')
    if features.get("graph_table_requirement") != "无":
        reasons.append("需要图像/表格处理")
    if features.get("chemistry_process_count") in ["2-3个反应或过程", "多反应连续转化或流程"]:
        reasons.append("涉及多个反应或过程")
    if features.get("evidence_relation") in ["多现象证据链", "证据冲突与排除"]:
        reasons.append("存在证据链分析")

    if features.get("subquestion_dependency") != "无多问":
        reasons.append("存在多个设问，不属于严格单点直答")
    if "宏观" in text and "微观" in text and "符号" in text:
        reasons.append("涉及宏观-微观-符号表征对应，至少基础题")
    if count_subquestions(data) >= 4:
        reasons.append("多小问数量较多")
    if count_fill_blanks(stem_options) >= 4 and features.get("knowledge_count") != "1个":
        reasons.append("多空填空且涉及不同知识点")

    force_basic_keywords = [
        "化合价", "相对分子质量", "质量分数", "溶质质量分数", "配平", "化学方程式", "符号表达式",
        "过滤", "蒸馏", "吸附", "电解水", "制取", "收集", "检验", "除杂", "鉴别",
        "单质", "化合物", "氧化物", "有机物", "酸碱盐", "金属活动性", "置换反应",
    ]
    if contains_any(text, force_basic_keywords) and not is_trivial_concept_question(features, data):
        reasons.append("命中化学基础应用关键词，至少基础题")

    return reasons


def should_upgrade_basic_to_medium(features: Dict[str, Any], data: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    text = visible_text(data, include_analysis=True)

    # 特定常见物质转化推断：先于基础题保护，否则会被“步骤短/单线索”误降。
    if is_common_substance_network_inference(features, data):
        reasons.append("A-E常见物质转化推断需要结合物质特征、转化关系和方程式，达到中等题")
        return reasons
    if is_co_reduction_combustion_combo_medium(features, data):
        reasons.append("CO还原氧化铁与燃烧条件组合实验涉及尾气处理、操作顺序和条件对比，达到中等题")
        return reasons
    if is_complex_equation_balancing_medium(features, data):
        reasons.append("陌生复杂化学方程式配平需要元素守恒列系数关系，达到中等题")
        return reasons
    if is_unfamiliar_material_transfer_medium(features, data):
        reasons.append("陌生材料迁移题需要综合相对分子质量、质量守恒和化合价/氧化还原判断，达到中等题")
        return reasons

    # 先做基础题保护，避免 pH/变质/质量守恒等关键词把独立基础空误升中等。
    if is_low_level_basic_application(features, data) and not is_standard_experiment_medium_combo(features, data):
        return []

    if is_standard_experiment_medium_combo(features, data):
        reasons.append("多个重要实验装置/现象/方程式并列综合，达到中等题")

    if (
        (contains_any(text, ["生成等量氢气", "相同质量", "不同金属", "金属用量"]) and contains_any(text, ["氢气", "H_{2}", "H2"]))
        or ("制取氢气" in text and count_subquestions(data) >= 3)
    ):
        reasons.append("氢气制取中涉及装置/收集/金属与酸反应综合，达到中等题")

    if features.get("step_count") in ["6-8步", "9-12步", "12步以上"]:
        reasons.append(f'步骤数达"{features.get("step_count")}"')
    if features.get("chemistry_process_count") == "多反应连续转化或流程":
        reasons.append("存在多反应连续转化或流程")
    if features.get("calculation_complexity") in ["化学方程式计算或关系式计算", "复杂守恒或图像计算"]:
        reasons.append(f'计算需要"{features.get("calculation_complexity")}"')
    if features.get("experiment_requirement") in ["控制变量或现象分析", "方案设计或误差评价"]:
        reasons.append(f'实验要求为"{features.get("experiment_requirement")}"')
    if features.get("graph_table_requirement") in ["多组比较归纳", "图像反推或拐点分析"]:
        reasons.append(f'图表处理要求为"{features.get("graph_table_requirement")}"')
    if features.get("evidence_relation") in ["多现象证据链", "证据冲突与排除"]:
        reasons.append(f'证据关系为"{features.get("evidence_relation")}"')
    if features.get("subquestion_dependency") == "多问且层层递进":
        reasons.append("多小问层层递进")
    if features.get("information_carrier") in ["流程图", "多图表综合"] and features.get("knowledge_count") != "1个":
        reasons.append("流程/多图表与多知识点结合")

    force_medium_keywords = [
        "控制变量", "对照实验", "催化剂", "探究", "项目式", "任务一", "任务二", "流程", "工艺", "滤渣", "滤液",
        "溶解度曲线", "图像", "图象", "曲线", "成分", "推断", "除杂", "鉴别", "金属活动性",
        "关系式", "混合物", "过量", "不足",
    ]
    if contains_any(text, force_medium_keywords) and (
        features.get("step_count") != "1-2步"
        or features.get("knowledge_count") != "1个"
        or features.get("experiment_requirement") != "无"
        or features.get("graph_table_requirement") != "无"
    ):
        reasons.append("命中实验/流程/图像/推断类中等综合关键词")

    # 基础升中等至少需要一个真实综合触发点；若只有关键词但特征仍是低阶，已被保护。
    return reasons


def should_downgrade_medium_to_basic(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    text = visible_text(data, include_analysis=True)
    if "制取氢气" in text and count_subquestions(data) >= 3:
        return False
    if (
        is_common_substance_network_inference(features, data)
        or is_co_reduction_combustion_combo_medium(features, data)
        or is_complex_equation_balancing_medium(features, data)
        or is_unfamiliar_material_transfer_medium(features, data)
    ):
        return False
    if is_multi_standard_lab_independent_basic(features, data):
        return True
    if is_standard_experiment_basic(features, data):
        return True
    if is_long_reading_direct_info(features, data):
        return True
    if is_low_level_basic_application(features, data):
        return True

    if is_long_context_or_new_situation(data):
        return False
    return (
        features.get("step_count") in ["1-2步", "3-5步"]
        and features.get("equation_count") in ["0-1个", "2-3个"]
        and features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]
        and features.get("reasoning_chain") in ["直接套用", "简单因果推理"]
        and features.get("chemistry_process_count") in ["单一事实", "单一反应", "2-3个反应或过程"]
        and features.get("constraint_count") in ["无约束", "单一约束"]
        and features.get("evidence_relation") in ["无证据链", "单一现象对应"]
        and features.get("experiment_requirement") in ["无", "基础操作或读数"]
        and features.get("graph_table_requirement") in ["无", "直接读数"]
        and features.get("subquestion_dependency") != "多问且层层递进"
    )



def should_upgrade_medium_to_hard(features: Dict[str, Any], data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    strong_reasons: List[str] = []
    support_reasons: List[str] = []
    text = visible_text(data, include_analysis=True)

    hard_keywords = [
        "部分变质", "氢氧化钠", "滴加盐酸", "离子数目", "拐点", "平台", "滤渣", "滤液", "循环物质",
        "方案评价", "可靠性", "质疑", "干扰", "过量", "不足", "先后反应", "差量法", "关系式法", "元素守恒",
        "合金", "混合物", "样品纯度", "纯度", "质量分数", "定量实验", "环保缺陷", "压强变化", "气球"
    ]

    # 单一标准图像/表格计算题停留中等，避免被“图像反推/多约束”误升拔高。
    if (
        is_air_oxygen_pressure_standard_medium(features, data)
        or is_standard_precipitation_purity_table_medium(features, data)
        or is_single_reaction_decomposition_graph_medium(features, data)
    ):
        return False, []

    # 强触发：存在明确卡点。
    if features.get("step_count") in ["9-12步", "12步以上"]:
        strong_reasons.append(f'步骤数达"{features.get("step_count")}"')
    if features.get("equation_count") in ["4-6个", "7个以上"] and features.get("information_carrier") == "多图表综合":
        strong_reasons.append(f'多个方程式与多图表信息结合，方程式数量为"{features.get("equation_count")}"')
    elif features.get("equation_count") in ["4-6个", "7个以上"] and contains_any(text, ["误差", "测定", "流程", "制备", "尾气处理"]):
        strong_reasons.append(f'多个方程式服务于实验/流程综合，方程式数量为"{features.get("equation_count")}"')
    if features.get("calculation_complexity") == "复杂守恒或图像计算":
        strong_reasons.append("需要复杂守恒或图像计算")
    if (
        features.get("graph_table_requirement") == "图像反推或拐点分析"
        and features.get("information_carrier") in ["图像或表格", "多图表综合"]
        and contains_any(text, ["图像", "图象", "曲线", "拐点", "平台", "pH", "压强", "沉淀", "气体质量", "离子数目"])
    ):
        strong_reasons.append("需要图像反推或拐点分析")
    if features.get("evidence_relation") == "证据冲突与排除":
        strong_reasons.append("存在证据冲突与干扰排除")
    if features.get("experiment_requirement") == "方案设计或误差评价":
        strong_reasons.append("需要方案设计、可靠性评价或误差分析")
    if (
        features.get("problem_structure") in ["物质推断", "工艺流程", "计算综合"]
        and features.get("chemistry_process_count") == "多反应连续转化或流程"
        and features.get("constraint_count") == "多约束"
        and contains_any(text, hard_keywords)
    ):
        strong_reasons.append("物质推断/流程/计算中同时出现多反应、多约束和拔高关键词")
    if (
        features.get("information_carrier") == "多图表综合"
        and features.get("experiment_requirement") == "控制变量或现象分析"
        and features.get("calculation_complexity") == "化学方程式计算或关系式计算"
        and contains_any(text, ["样品纯度", "纯度", "质量分数", "测定", "压强变化", "气球"])
    ):
        strong_reasons.append("多图表实验分析与样品纯度/质量分数/压强变化计算结合")
    if is_bicarbonate_purity_hard(features, data):
        strong_reasons.append("NaHCO3/小苏打性质表格与样品纯度计算结合，存在实验归纳和定量计算卡点")
    if (
        contains_any(text, ["自动充气气球", "压强变化", "压强"] )
        and features.get("subquestion_dependency") == "多问且层层递进"
        and features.get("information_carrier") == "多图表综合"
        and features.get("constraint_count") == "多约束"
        and features.get("evidence_relation") == "多现象证据链"
    ):
        strong_reasons.append("项目式气球成分探究需要结合压强图像/数据与多现象证据链反推成分")

    # 支撑触发：单独不足以升拔高，但可与强触发组合。
    if features.get("chemistry_process_count") == "多反应连续转化或流程":
        support_reasons.append("存在多反应连续转化或流程")
    if features.get("constraint_count") == "多约束":
        support_reasons.append("存在过量/不足/先后反应等多约束")
    if features.get("evidence_relation") == "多现象证据链":
        support_reasons.append("存在多现象证据链")
    if features.get("information_carrier") == "多图表综合":
        support_reasons.append("需要整合多图表信息")
    if features.get("knowledge_count") == "4个及以上":
        support_reasons.append("知识点数量达到4个及以上")
    if features.get("subquestion_dependency") == "多问且层层递进":
        support_reasons.append("多小问层层递进")
    if contains_any(text, hard_keywords) and (
        features.get("calculation_complexity") != "口算或直接判断"
        or features.get("evidence_relation") != "无证据链"
        or features.get("experiment_requirement") != "无"
        or features.get("graph_table_requirement") != "无"
    ):
        support_reasons.append("命中变质/流程/图像/守恒/方案评价类拔高关键词")

    # 微观示意图/物质组成结构类选择题，即使模型把“读图”写成反推，也通常停留在中等题。
    if contains_any(text, ["了解物质的组成和结构", "微观示意图", "结构示意图"]) and not contains_any(text, ["滤渣", "滤液", "变质", "质量分数", "方案", "守恒", "压强", "曲线"]):
        return False, strong_reasons + support_reasons

    reasons = strong_reasons + support_reasons
    return len(strong_reasons) >= 1 and len(reasons) >= 2, reasons


def should_downgrade_standard_experiment(features: Dict[str, Any], data: Dict[str, Any]) -> Optional[str]:
    """拔高题降档：只降真正的标准实验/标准单线计算，避免把金属滤渣滤液、流程、图像探究误降。"""
    if is_air_oxygen_pressure_standard_medium(features, data):
        return "中等题"
    if is_standard_precipitation_purity_table_medium(features, data):
        return "中等题"
    if is_single_reaction_decomposition_graph_medium(features, data):
        return "中等题"
    if is_single_path_standard_calculation(features, data):
        return "中等题"

    # 有这些拔高核心结构时，不能按“标准实验”降档。
    if (
        features.get("chemistry_process_count") == "多反应连续转化或流程"
        or features.get("constraint_count") == "多约束"
        or features.get("evidence_relation") in ["多现象证据链", "证据冲突与排除"]
        or features.get("graph_table_requirement") in ["多组比较归纳", "图像反推或拐点分析"]
        or features.get("information_carrier") == "多图表综合"
        or features.get("experiment_requirement") in ["控制变量或现象分析", "方案设计或误差评价"]
    ):
        return None

    if features.get("step_count") in ["1-2步", "3-5步"] and features.get("experiment_requirement") in ["无", "基础操作或读数"]:
        if features.get("calculation_complexity") in ["口算或直接判断", "简单笔算"]:
            return "基础题"
    if (
        features.get("step_count") in ["3-5步", "6-8步"]
        and features.get("calculation_complexity") != "复杂守恒或图像计算"
        and features.get("evidence_relation") != "证据冲突与排除"
        and features.get("experiment_requirement") != "方案设计或误差评价"
        and features.get("graph_table_requirement") != "图像反推或拐点分析"
    ):
        return "中等题"
    return None


def high_level_feature_count(features: Dict[str, Any], data: Dict[str, Any]) -> int:
    count = 0
    if features.get("step_count") == "12步以上":
        count += 1
    if features.get("equation_count") == "7个以上":
        count += 1
    if features.get("knowledge_count") == "4个及以上" and features.get("knowledge_diff") == "高":
        count += 1
    if features.get("chemistry_process_count") == "多反应连续转化或流程":
        count += 1
    if features.get("constraint_count") == "多约束":
        count += 1
    if features.get("evidence_relation") == "证据冲突与排除":
        count += 1
    if features.get("calculation_complexity") == "复杂守恒或图像计算":
        count += 1
    if features.get("experiment_requirement") == "方案设计或误差评价":
        count += 1
    if features.get("graph_table_requirement") == "图像反推或拐点分析":
        count += 1
    if features.get("information_carrier") == "多图表综合":
        count += 1
    if features.get("subquestion_dependency") == "多问且层层递进" and count_subquestions(data) >= 4:
        count += 1
    return count


def has_final_core_combo(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """压轴题核心组合约束。

    压轴不能只靠题干长、流程长、关键词多；必须同时具有：
    A. 复杂计算 / 证据冲突排除 / 方案评价之一；
    B. 图像拐点反推 / 多反应流程 / 多约束之一；
    C. 多问递进且至少 3 个小问。
    """
    text = visible_text(data, include_analysis=True)
    project_final_signal = (
        contains_any(text, ["蒸汽眼罩", "数字传感器", "探究一", "探究二"])
        and features.get("information_carrier") == "多图表综合"
        and features.get("experiment_requirement") == "控制变量或现象分析"
        and features.get("knowledge_count") == "4个及以上"
    )
    quantified_conflict = (
        features.get("evidence_relation") == "证据冲突与排除"
        and features.get("experiment_requirement") == "方案设计或误差评价"
        and contains_any(text, ["定量", "质量分数", "图像", "图象", "曲线", "气体质量", "二氧化碳质量", "氢气质量", "极值", "范围"])
    )
    core_a = (
        features.get("calculation_complexity") == "复杂守恒或图像计算"
        or features.get("graph_table_requirement") == "图像反推或拐点分析"
        or quantified_conflict
        or project_final_signal
    )
    core_b = (
        features.get("graph_table_requirement") == "图像反推或拐点分析"
        or features.get("chemistry_process_count") == "多反应连续转化或流程"
        or features.get("constraint_count") == "多约束"
    )
    core_c = (
        features.get("subquestion_dependency") == "多问且层层递进"
        and count_subquestions(data) >= 3
    )
    return core_a and core_b and core_c


def should_upgrade_hard_to_final(features: Dict[str, Any], data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    high_count = high_level_feature_count(features, data)
    final_core_combo = has_final_core_combo(features, data)

    if high_count >= 5:
        reasons.append(f"高阶化学特征达到 {high_count} 项，接近压轴题密度")
    elif high_count >= 4:
        reasons.append(f"高阶化学特征达到 {high_count} 项")

    if features.get("step_count") == "12步以上":
        reasons.append("解题链条超过12步")
    if count_subquestions(data) >= 5 and features.get("subquestion_dependency") == "多问且层层递进":
        reasons.append("多小问层层递进且数量较多")
    if final_core_combo:
        reasons.append("同时具备复杂证据/计算/方案评价、多反应或多约束、递进多问三类压轴核心结构")

    text = visible_text(data, include_analysis=True)
    final_keywords = [
        "综合", "工艺流程", "制备", "定量实验", "离子数目变化", "方案评价", "误差", "混合物", "合金", "质量分数",
        "变质", "滤渣", "滤液", "循环", "尾气处理", "环保", "关系式", "守恒", "极值", "不可能是",
    ]
    if contains_any(text, final_keywords) and high_count >= 4 and count_subquestions(data) >= 3 and final_core_combo:
        reasons.append("题目具备中考最后综合题属性")

    return len(reasons) >= 2 and high_count >= 4 and final_core_combo, reasons


def should_downgrade_final_to_hard(features: Dict[str, Any], data: Dict[str, Any]) -> bool:
    return high_level_feature_count(features, data) < 4 or not has_final_core_combo(features, data)


def sync_reasoning_after_postprocess(rating_result: Dict[str, Any]) -> None:
    """后处理改档后的解释同步层。

    只在 postprocess_trace 非空时生效；不改变 difficulty_level 和 features。
    目标是解决“最终档位已被后处理改成 X，但 why_not_higher 仍沿用原模型解释”的前后矛盾问题。
    """
    trace = rating_result.get("postprocess_trace") or []
    if not trace:
        return

    final_level = rating_result.get("difficulty_level", "")
    reason_text = "；".join(str(item.get("reason", "")) for item in trace if item.get("reason"))
    if not reason_text:
        reason_text = str(rating_result.get("postprocess_note", "")) or "后处理规则修正"

    reasoning = rating_result.setdefault("reasoning", {
        "core_basis": "",
        "hard_point": "",
        "why_not_lower": "",
        "why_not_higher": "",
    })

    if final_level == "送分题":
        reasoning["why_not_lower"] = "送分题已经是最低难度档，无更低档。"
        reasoning["why_not_higher"] = f"后处理最终判为送分题，原因：{reason_text}。题目只涉及低阶直接识记或常识匹配，不需要提升到基础题。"
    elif final_level == "基础题":
        reasoning["why_not_lower"] = f"后处理最终判为基础题，原因：{reason_text}。题目需要概念辨析、基础化学用语、简单计算或基础实验操作，不能降为送分题。"
        reasoning["why_not_higher"] = "题目缺少中等题所需的多反应链、实验探究、图表归纳、成分推断证据链或守恒计算，因此不需要判为中等题。"
    elif final_level == "中等题":
        reasoning["why_not_lower"] = f"后处理最终判为中等题，原因：{reason_text}。题目存在一定综合性或标准化学分析任务，不能降为基础题。"
        reasoning["why_not_higher"] = "题目路径仍属于常规中考方法，缺少明显拔高卡点，如方案评价、证据冲突排除、复杂守恒、图像拐点反推或多反应多约束，因此不需要判为拔高题。"
    elif final_level == "拔高题":
        reasoning["why_not_lower"] = f"后处理最终判为拔高题，原因：{reason_text}。题目存在明显卡点，不能降为中等题。"
        reasoning["why_not_higher"] = "虽然题目有拔高因素，但尚未同时满足压轴题所需的复杂证据/计算/方案评价、多反应或多约束、递进多问等核心组合，因此不需要判为压轴题。"
    elif final_level == "压轴题":
        reasoning["why_not_lower"] = f"后处理最终判为压轴题，原因：{reason_text}。题目具备多项高阶特征和压轴核心组合，不能降为拔高题。"
        reasoning["why_not_higher"] = "压轴题已经是最高难度档，无更高档。"


def add_feature_audit_flags(rating_result: Dict[str, Any], data: Dict[str, Any]) -> None:
    """增加 feature 审计标记，不参与最终难度决策。

    这些 flag 用于 HTML 人审或离线质量监控。它们只提示“features 可能需要人工关注”，
    不改变 difficulty_level、coarse_difficulty 或后处理分类结果。
    """
    features = rating_result.get("features") or {}
    level = rating_result.get("difficulty_level", "")
    flags: List[str] = []
    text_no_analysis = visible_text(data, include_analysis=False)
    text_all = visible_text(data, include_analysis=True)

    has_image_placeholder = "<image" in text_all.lower() or "[image" in text_all.lower() or "图片" in text_all
    has_image_url = bool(str(data.get("stem_pic_url", "") or "").strip() or str(data.get("analysis_pic_url", "") or "").strip())
    if (has_image_placeholder or has_image_url) and features.get("information_carrier") == "纯文字":
        flags.append("纯文本模式图像信息未进入模型：information_carrier=纯文字，图像类 feature 仅供参考")

    graph_markers = ["图", "图像", "图象", "曲线", "表", "数据", "压强", "气压", "质量变化", "坐标", "如下图", "如图"]
    if contains_any(text_all, graph_markers) and features.get("graph_table_requirement") == "无":
        flags.append("题干/解析存在图表或数据线索，但 graph_table_requirement=无，建议人审确认")

    flow_markers = ["流程", "工艺", "滤渣", "滤液", "转化关系", "框图", "A-G", "A～G", "A~G", "A-E", "A～E", "A~E"]
    if contains_any(text_all, flow_markers) and features.get("additional_structure") == "无":
        flags.append("题干/解析存在流程/框图/推断线索，但 additional_structure=无，建议人审确认")

    high_count = high_level_feature_count(features, data)
    if level == "拔高题" and high_count < 2:
        flags.append(f"拔高题但高阶特征计数偏低({high_count})，可能依赖题型规则或关键词升档")
    if level == "压轴题" and high_count < 4:
        flags.append(f"压轴题但高阶特征计数偏低({high_count})，建议人工复核压轴证据链")

    hard_markers = ["方案评价", "质疑", "可靠性", "补充实验", "干扰", "滤渣", "滤液", "先后反应", "过量", "不足", "拐点", "平台", "极值", "分类讨论", "复杂守恒", "元素守恒"]
    if level in ["送分题", "基础题"] and contains_any(text_all, hard_markers):
        flags.append("低档题中出现拔高关键词，若题目确有证据链/多约束/复杂计算，需人工确认是否低估")

    if rating_result.get("postprocess_trace"):
        flags.append("后处理已改档：reasoning 已按最终档位同步，原始模型解释仅作参考")

    # 去重并保持顺序。
    deduped: List[str] = []
    for flag in flags:
        if flag and flag not in deduped:
            deduped.append(flag)
    rating_result["feature_audit_flags"] = deduped




def infer_level_from_features(features: Dict[str, Any], data: Dict[str, Any]) -> str:
    high = high_level_feature_count(features, data)
    if high >= 4:
        return "压轴题"
    if high >= 2 or features.get("step_count") == "9-12步":
        return "拔高题"
    if (
        features.get("step_count") == "6-8步"
        or features.get("experiment_requirement") in ["控制变量或现象分析", "方案设计或误差评价"]
        or features.get("graph_table_requirement") in ["多组比较归纳", "图像反推或拐点分析"]
        or features.get("evidence_relation") in ["多现象证据链", "证据冲突与排除"]
    ):
        return "中等题"
    if should_downgrade_basic_to_easy(features, data):
        return "送分题"
    return "基础题"

# -------------------------- 5. 构建题目输入与模型调用 --------------------------
def construct_question_content(data: Dict[str, Any]) -> str:
    """将数据记录拼装成标准的打标输入文本；对齐物理脚本，兼容 sub_questions。"""
    parts: List[str] = []
    stem = str(data.get("stem", "") or "").strip()
    options = str(data.get("options", "") or "").strip()
    analysis = str(data.get("analysis", "") or "").strip()

    if stem:
        parts.append(f"【题干】\n{stem}")
    if options:
        parts.append(f"【选项】\n{options}")
    if analysis:
        parts.append(f"【解析】\n{analysis}")

    stem_pic_url = str(data.get("stem_pic_url", "") or "").strip()
    analysis_pic_url = str(data.get("analysis_pic_url", "") or "").strip()
    if stem_pic_url:
        parts.append(f"【题干图片】\n{stem_pic_url}")
    if analysis_pic_url:
        parts.append(f"【解析图片】\n{analysis_pic_url}")

    sub_questions = data.get("sub_questions", []) or []
    if sub_questions:
        try:
            sub_questions.sort(key=lambda x: int(x.get("question_id", 0)) if isinstance(x, dict) else 0)
        except Exception:
            pass
        parts.append("【小题】")
        for i, sq in enumerate(sub_questions, 1):
            parts.append(f"  小题{i}:")
            if isinstance(sq, dict):
                sq_stem = str(sq.get("stem", "") or "").strip()
                sq_options = str(sq.get("options", "") or "").strip()
                sq_analysis = str(sq.get("analysis", "") or "").strip()
                if sq_stem:
                    parts.append(f"    题干: {sq_stem}")
                if sq_options:
                    parts.append(f"    选项: {sq_options}")
                if sq_analysis:
                    parts.append(f"    解析: {sq_analysis}")
            else:
                parts.append(f"    题干: {sq}")

    return "\n\n".join(parts)


def build_minimal_text_supplement(
    data: Dict[str, Any],
    attached_image_fields: Sequence[str],
) -> Tuple[str, List[str]]:
    """只补图片中可能缺失且判题必需的信息，并返回补充字段审计。"""
    sub_questions = data.get("sub_questions", []) or []
    if sub_questions:
        parts = [
            (
                f"【必要结构信息】结构化数据已拆出{len(sub_questions)}个小问。"
                "请结合题干图片检查是否完整覆盖；图片中的真实编号和设问优先。"
            )
        ]
        fields = ["structured_subquestions_provided"]
    else:
        parts = [
            (
                "【必要结构信息】结构化数据未提供已拆分的小问信息。"
                "请以题干图片中实际可辨认的编号、空格和设问为准；"
                "不得把列表为空理解为本题没有小问。"
            )
        ]
        fields = ["structured_subquestions_not_provided"]

    manual = str(data.get("image_text_supplement", "") or "").strip()
    if manual:
        parts.append(f"【人工确认的图片缺失信息】\n{manual}")
        fields.append("image_text_supplement")

    if "stem_pic_url" not in attached_image_fields:
        stem_parts = []
        for key, label in (("stem", "题干"), ("options", "选项")):
            value = str(data.get(key, "") or "").strip()
            if value:
                stem_parts.append(f"{label}: {value}")
                fields.append(key)
        for index, sub_question in enumerate(sub_questions, start=1):
            if isinstance(sub_question, dict):
                value = str(sub_question.get("stem", "") or "").strip()
            else:
                value = str(sub_question or "").strip()
            if value:
                stem_parts.append(f"第{index}小问: {value}")
        if stem_parts:
            parts.append("【题干图片缺失时的必要文字补充】\n" + "\n".join(stem_parts))
            fields.append("subquestion_stems")

    if "analysis_pic_url" not in attached_image_fields:
        analysis_parts = []
        value = str(data.get("analysis", "") or "").strip()
        if value:
            analysis_parts.append(f"整题解析: {value}")
            fields.append("analysis")
        for index, sub_question in enumerate(sub_questions, start=1):
            if isinstance(sub_question, dict):
                sub_analysis = str(sub_question.get("analysis", "") or "").strip()
                if sub_analysis:
                    analysis_parts.append(f"第{index}小问解析: {sub_analysis}")
        if analysis_parts:
            parts.append("【解析图片缺失时的必要文字补充】\n" + "\n".join(analysis_parts))
            fields.append("subquestion_analyses")

    return "\n\n".join(parts), list(dict.fromkeys(fields))


def build_user_content(data: Dict[str, Any], use_images: bool) -> Any:
    """构造图片主输入，并在图片之后追加最小必要文字补充。"""
    dynamic_text = (
        "这是图片主输入实验。请先从图片读取完整题面；后面的必要文字只用于"
        "补齐图片缺口，不得跳过图片直接按文字定档。"
        f"{DIFFICULTY_RATING_PROMPT_SUFFIX}"
    )
    if not use_images:
        return (
            "当前题目没有可用图片，以下完整结构化文字是题面来源。"
            f"{DIFFICULTY_RATING_PROMPT_SUFFIX}\n\n"
            + construct_question_content(data)
        )
    content: List[Dict[str, str]] = [{"type": "input_text", "text": dynamic_text}]
    seen: set[str] = set()
    attached_fields: List[str] = []
    image_labels = {
        "stem_pic_url": (
            "下面是题干图片。请完整读取题干、选项、公式、装置、流程、"
            "表格、坐标和全部小问。"
        ),
        "analysis_pic_url": (
            "下面是解析图片。只用于核对必要解题链、隐含条件、干扰排除、"
            "计算和误差；不得因为看到答案而降低难度。"
        ),
    }
    for key in ("stem_pic_url", "analysis_pic_url"):
        url = str(data.get(key, "") or "").strip()
        if url.startswith(("http://", "https://")) and url not in seen:
            content.append({"type": "input_text", "text": image_labels[key]})
            content.append({"type": "input_image", "image_url": url})
            seen.add(url)
            attached_fields.append(key)
    supplement, _ = build_minimal_text_supplement(data, attached_fields)
    if supplement:
        content.append({"type": "input_text", "text": supplement})
    return content if len(content) > 1 else dynamic_text


def parse_model_response(response_text: str) -> Tuple[Dict[str, Any], str]:
    """Strictly parse one JSON object; do not repair truncated JSON silently."""
    if not response_text or not response_text.strip():
        return {}, "模型返回空文本"
    clean_text = response_text.strip()
    if clean_text.startswith("```json") and clean_text.endswith("```"):
        clean_text = clean_text[len("```json"):-3].strip()
    elif clean_text.startswith("```") and clean_text.endswith("```"):
        clean_text = clean_text[3:-3].strip()
    try:
        parsed = json.loads(clean_text)
    except json.JSONDecodeError as exc:
        return {}, f"JSON解析失败: line={exc.lineno}, column={exc.colno}, message={exc.msg}"
    if not isinstance(parsed, dict):
        return {}, f"JSON顶层必须是对象，实际为{type(parsed).__name__}"
    return parsed, ""


async def call_model_with_cache(
    data: Dict[str, Any],
    session: aiohttp.ClientSession,
    retries: int,
    timeout_sec: int,
    repair_feedback: str = "",
) -> Tuple[Dict[str, Any], str, str, float, int, int, int, Dict[str, Any]]:
    """Call the model once logically, with internal HTTP/network retries."""
    attached_image_fields: List[str] = []
    attached_image_urls: List[str] = []
    for key in ("stem_pic_url", "analysis_pic_url"):
        url = str(data.get(key, "") or "").strip()
        if url.startswith(("http://", "https://")) and url not in attached_image_urls:
            attached_image_fields.append(key)
            attached_image_urls.append(url)
    image_url_count = len(attached_image_urls)
    supplement_text, supplement_fields = build_minimal_text_supplement(data, attached_image_fields)
    structural_fields = {
        "subquestion_count",
        "structured_subquestions_provided",
        "structured_subquestions_not_provided",
    }
    content_text_fields = [field for field in supplement_fields if field not in structural_fields]
    image_status = {
        "question_input_mode": "image_primary_minimal_text",
        "question_text_input_used": bool(content_text_fields),
        "structural_metadata_used": True,
        "text_supplement_fields": supplement_fields,
        "text_supplement_char_count": len(supplement_text),
        "image_input_requested": bool(ENABLE_IMAGE_INPUT and image_url_count),
        "image_input_used": False,
        "image_input_url_count": image_url_count,
        "image_input_fields": attached_image_fields,
        "image_fallback_reason": "",
        "http_retry_count": 0,
    }
    use_images = bool(image_status["image_input_requested"])
    if not use_images:
        image_status["question_input_mode"] = "fulltext_no_image"
        image_status["image_fallback_reason"] = "题目没有可用图片URL，按允许策略使用完整结构化文字"

    response_id = await get_or_create_cache(session, retries, timeout_sec)
    if not response_id:
        return {}, "", "无法获取有效缓存", 0.0, 0, 0, 0, image_status

    total_elapsed = 0.0
    for attempt in range(retries):
        image_status["http_retry_count"] = attempt
        user_content = build_user_content(data, use_images)
        if repair_feedback:
            repair_part = {
                "type": "input_text",
                "text": (
                    "【上次输出修复要求】\n" + repair_feedback
                    + "\n请保持对题目难度的实质判断不变，只修复JSON结构、缺失字段或非法枚举，并重新完整输出一个JSON对象。"
                ),
            }
            if isinstance(user_content, list):
                user_content = [*user_content, repair_part]
            else:
                user_content = [
                    {"type": "input_text", "text": str(user_content)},
                    repair_part,
                ]
        payload = {
            "model": MODEL_NAME,
            "previous_response_id": response_id,
            "input": [{"role": "user", "content": user_content}],
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
                elapsed = time.time() - started
                total_elapsed += elapsed
                if response.status == 200:
                    result = await response.json()
                    output_text = ""
                    for item in result.get("output", []):
                        if item.get("type") == "message":
                            for content_item in item.get("content", []):
                                if content_item.get("type") == "output_text":
                                    output_text = content_item.get("text", "")
                    usage = result.get("usage", {})
                    parsed, parse_error = parse_model_response(output_text)
                    image_status["image_input_used"] = use_images
                    return (
                        parsed,
                        output_text,
                        parse_error,
                        total_elapsed,
                        int(usage.get("input_tokens", 0) or 0),
                        int(usage.get("output_tokens", 0) or 0),
                        int(usage.get("total_tokens", 0) or 0),
                        image_status,
                    )
                error_text = await response.text()
                if response.status == 429:
                    await asyncio.sleep(int(response.headers.get("Retry-After", 5)))
                    continue
                if "InvalidParameter.PreviousResponseNotFound" in error_text:
                    response_id = await create_prefix_cache(session, retries, timeout_sec)
                    if not response_id:
                        break
                    continue
                if response.status >= 500:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                image_status["image_fallback_reason"] = f"HTTP {response.status}: {error_text[:200]}"
                return {}, "", image_status["image_fallback_reason"], total_elapsed, 0, 0, 0, image_status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            total_elapsed += time.time() - started
            if attempt + 1 >= retries:
                return {}, "", f"HTTP/network失败: {exc}", total_elapsed, 0, 0, 0, image_status
            await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
        except Exception as exc:
            total_elapsed += time.time() - started
            if attempt + 1 >= retries:
                return {}, "", f"请求异常: {exc}", total_elapsed, 0, 0, 0, image_status
            await asyncio.sleep(1)
    return {}, "", "HTTP/network重试耗尽", total_elapsed, 0, 0, 0, image_status

# -------------------------- 6. 并发处理 --------------------------
async def process_single_question(
    data: Dict[str, Any],
    session: aiohttp.ClientSession,
    semaphore: Semaphore,
    output_path: str,
    error_path: str,
    retries: int,
    timeout_sec: int,
) -> None:
    """Request, parse and schema-validate; repair invalid output at model side."""
    async with semaphore:
        question_id = data.get("question_id", "unknown")
        total_time = 0.0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        http_retry_count = 0
        json_parse_retry_count = 0
        schema_retry_count = 0
        json_parse_errors: List[str] = []
        schema_validation_errors: List[str] = []
        repair_feedback = ""
        image_status: Dict[str, Any] = {}
        last_raw_text = ""
        last_raw_result: Dict[str, Any] = {}

        while True:
            try:
                (
                    raw_result,
                    raw_text,
                    parse_error,
                    time_use,
                    prompt_tokens,
                    completion_tokens,
                    call_tokens,
                    call_image_status,
                ) = await call_model_with_cache(
                    data,
                    session,
                    retries,
                    timeout_sec,
                    repair_feedback=repair_feedback,
                )
                last_raw_text = raw_text
                last_raw_result = copy.deepcopy(raw_result)
                image_status = call_image_status
                total_time += time_use
                total_prompt_tokens += prompt_tokens
                total_completion_tokens += completion_tokens
                total_tokens += call_tokens
                http_retry_count += int(call_image_status.get("http_retry_count", 0) or 0)

                if parse_error:
                    json_parse_errors.append(parse_error)
                    if json_parse_retry_count >= MAX_JSON_PARSE_RETRIES:
                        raise RuntimeError(
                            f"JSON解析重试耗尽({MAX_JSON_PARSE_RETRIES}): {parse_error}"
                        )
                    json_parse_retry_count += 1
                    repair_feedback = f"上次输出不是完整合法JSON对象：{parse_error}"
                    continue

                try:
                    rating_result = postprocess_chemistry_difficulty(raw_result, data)
                except Core12SchemaError as exc:
                    schema_error = str(exc)
                    schema_validation_errors.append(schema_error)
                    if schema_retry_count >= MAX_SCHEMA_RETRIES:
                        raise RuntimeError(
                            f"schema校验重试耗尽({MAX_SCHEMA_RETRIES}): {schema_error}"
                        ) from exc
                    schema_retry_count += 1
                    repair_feedback = f"上次输出未通过Evidence-15 V9 schema：{schema_error}"
                    continue

                output_data = data.copy()
                output_data["difficulty_rating_raw"] = last_raw_result
                output_data["postprocess_actions"] = copy.deepcopy(
                    rating_result.get("postprocess_actions", [])
                )
                output_data["difficulty_rating"] = rating_result
                output_data["api_time_use"] = round(total_time, 2)
                output_data["api_prompt_tokens"] = total_prompt_tokens
                output_data["api_completion_tokens"] = total_completion_tokens
                output_data["api_total_tokens"] = total_tokens
                output_data.update(image_status)
                output_data["http_retry_count"] = http_retry_count
                output_data["json_parse_retry_count"] = json_parse_retry_count
                output_data["schema_retry_count"] = schema_retry_count
                output_data["json_parse_errors"] = json_parse_errors
                output_data["schema_validation_errors"] = schema_validation_errors
                structured_subquestions = data.get("sub_questions", []) or []
                model_dependency = (
                    (rating_result.get("features") or {}).get("subquestion_dependency", "")
                )
                output_data["input_structure_audit"] = {
                    "structured_subquestions_status": (
                        "provided" if structured_subquestions else "not_provided"
                    ),
                    "structured_subquestion_count": len(structured_subquestions),
                    "image_structure_inference_required": not bool(structured_subquestions),
                    "image_input_used": bool(image_status.get("image_input_used")),
                    "model_subquestion_dependency": model_dependency,
                    "blocking": False,
                    "note": (
                        "结构化小问未提供；模型必须依据题干图片判断，当前仅记录审计，不拒绝结果。"
                        if not structured_subquestions
                        else "结构化小问已提供；数量仅用于离线覆盖审计，不要求与模型输出硬匹配。"
                    ),
                }
                output_data["retry_audit"] = {
                    "http_network": http_retry_count,
                    "json_parse": json_parse_retry_count,
                    "schema_validation": schema_retry_count,
                }
                async with FILE_LOCK:
                    async with aiofiles.open(output_path, "a", encoding="utf-8") as handle:
                        await handle.write(json.dumps(output_data, ensure_ascii=False) + "\n")
                return
            except Exception as exc:
                error_data = data.copy()
                error_data["rating_error"] = f"question_id={question_id}; error={exc}"
                error_data["last_model_text"] = last_raw_text
                error_data["difficulty_rating_raw"] = last_raw_result
                error_data["api_time_use"] = round(total_time, 2)
                error_data["api_prompt_tokens"] = total_prompt_tokens
                error_data["api_completion_tokens"] = total_completion_tokens
                error_data["api_total_tokens"] = total_tokens
                error_data.update(image_status)
                error_data["http_retry_count"] = http_retry_count
                error_data["json_parse_retry_count"] = json_parse_retry_count
                error_data["schema_retry_count"] = schema_retry_count
                error_data["json_parse_errors"] = json_parse_errors
                error_data["schema_validation_errors"] = schema_validation_errors
                async with FILE_LOCK:
                    async with aiofiles.open(error_path, "a", encoding="utf-8") as handle:
                        await handle.write(json.dumps(error_data, ensure_ascii=False) + "\n")
                return


async def process_with_progress(
    data: Dict[str, Any],
    session: aiohttp.ClientSession,
    semaphore: Semaphore,
    pbar: tqdm,
    output_path: str,
    error_path: str,
    retries: int,
    timeout_sec: int,
) -> None:
    await process_single_question(data, session, semaphore, output_path, error_path, retries, timeout_sec)
    pbar.update(1)


def get_processed_question_ids(output_path: str) -> set:
    processed = set()
    if not os.path.exists(output_path):
        return processed
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    qid = item.get("question_id")
                    if qid:
                        processed.add(qid)
                except Exception:
                    continue
    except Exception as e:
        print(f"扫描断点文件出错: {e}")
    return processed

# -------------------------- 7. 主执行流 --------------------------
async def main_batch_run() -> None:
    parser = argparse.ArgumentParser(description="初中化学难度评级多线程并发批量打标脚本 (带 Cache 优化)")
    parser.add_argument("-p", "--prompt", type=str, default=str(DEFAULT_PROMPT), help="化学打标提示词文件路径")
    parser.add_argument("-i", "--input", type=str, default="../data/chemistry_sampled_5000_per_difficulty_v2.jsonl", help="输入待打标 JSONL 数据集路径")
    parser.add_argument("-o", "--output", type=str, default="chemistry_difficulty_rated_results.jsonl", help="输出保存打标结果的 JSONL 路径")
    parser.add_argument("-e", "--error", type=str, default="chemistry_difficulty_errors.jsonl", help="输出保存失败结果的 JSONL 路径")
    parser.add_argument("-c", "--concurrency", type=int, default=30, help="最大并发限制，默认 30")
    parser.add_argument("-t", "--timeout", type=int, default=180, help="单次 API 调用超时时间，默认 180 秒")
    parser.add_argument("-r", "--retries", type=int, default=3, help="失败最大重试次数，默认 3")
    parser.add_argument("-n", "--num", type=int, default=None, help="测试打标的限制数量（留空表示全部打标）")
    parser.add_argument("--seed", type=int, default=42, help="随机抽样/打乱的种子，默认 42")
    args = parser.parse_args()

    random.seed(args.seed)
    load_prompt_config(args.prompt)

    if not os.path.exists(args.input):
        print(f"错误: 输入文件 {args.input} 不存在，终止运行！")
        sys.exit(1)

    print("正在加载待打标数据集...")
    questions: List[Dict[str, Any]] = []
    with open(args.input, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                questions.append(json.loads(line))
            except Exception:
                continue
    print(f"成功加载题目数据，共计 {len(questions)} 道题目。")

    if args.num is not None:
        questions = random.sample(questions, min(args.num, len(questions)))
        print(f"参数 -n 生效，随机抽样其中 {len(questions)} 道题进行测试。")
    else:
        random.shuffle(questions)
        print("全部打标启动：题目次序已随机打乱。")

    processed_ids = get_processed_question_ids(args.output)
    to_process = [q for q in questions if q.get("question_id") not in processed_ids]
    print(f"数据比对完成: 已完成数 {len(processed_ids)}，待处理数 {len(to_process)}")

    if not to_process:
        print("所有题目都已完成打标！")
        return

    semaphore = Semaphore(args.concurrency)
    pbar = tqdm(total=len(to_process), unit="item", desc="Chemistry Rating Progress")

    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        await get_or_create_cache(session, args.retries, args.timeout)
        tasks = [
            asyncio.create_task(
                process_with_progress(q, session, semaphore, pbar, args.output, args.error, args.retries, args.timeout)
            )
            for q in to_process
        ]
        if tasks:
            await asyncio.gather(*tasks)

    pbar.close()
    print("\n✨ 化学多线程批量打标运行结束！")
    print(f"👉 成功保存打标结果至: {os.path.abspath(args.output)}")
    print(f"👉 失败重试错误日志在: {os.path.abspath(args.error)}")



# -------------------------- 8. Evidence-15 V9阶段2.1 image-primary/mintext后处理 --------------------------
# 为隔离输入模态，默认prompt_only；旧语义规则只保留用于非默认复现。
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PROMPT_FILENAME = "evidence15_v9_prompt.txt"
LOCAL_PROMPT = SCRIPT_DIR / PROMPT_FILENAME
PROJECT_PROMPT = PROJECT_ROOT / "prompts" / PROMPT_FILENAME
DEFAULT_PROMPT = LOCAL_PROMPT if LOCAL_PROMPT.exists() else PROJECT_PROMPT
DEFAULT_CACHE = PROJECT_ROOT / "chemistry_evidence15_v9_prompt_cache.json"
POSTPROCESS_PROFILE = os.getenv(
    "CHEMISTRY_0724_V5_2_IMAGE_PRIMARY_POSTPROCESS_PROFILE",
    "prompt_only",
).strip().lower()
VALID_POSTPROCESS_PROFILES = {"safe_v5", "exploratory_v5", "safe_v4", "prompt_only"}
CACHE_FILE_PATH = os.getenv(
    "CHEMISTRY_0724_V5_2_IMAGE_PRIMARY_CACHE_FILE",
    str(DEFAULT_CACHE),
)
LEVELS = ["送分题", "基础题", "中等题", "拔高题", "压轴题"]
COARSE_BY_LEVEL = {
    "送分题": "送分/基础区间（1-2档）",
    "基础题": "送分/基础区间（1-2档）",
    "中等题": "基础/中等区间（2-3档）",
    "拔高题": "中等/拔高区间（3-4档）",
    "压轴题": "拔高/压轴区间（4-5档）",
}

FEATURE_VALUES: dict[str, tuple[str, ...]] = {
    "reasoning_depth": ("0层", "1层", "2-3层", "4-5层", "6层及以上"),
    "reasoning_direction": ("直接识记", "正向推导", "逆向推导", "分类讨论或综合推导"),
    "knowledge_count": ("1个", "2-3个", "4个及以上"),
    "knowledge_relation": ("单一知识点", "同模块简单关联", "同模块深度关联", "跨模块融合", "多模块深度融合"),
    "knowledge_diff": ("低", "中", "高"),
    "representation_conversion": ("无", "一次表征转换", "两类表征往返", "宏观-微观-符号-定量多重转换"),
    "reaction_relation": ("无反应关系", "单一反应", "2-3个并列或简单连续反应", "多反应连续转化", "先后或竞争反应"),
    "excess_deficiency": ("无", "条件直接给定", "需要判断过量不足", "需要分情况讨论"),
    "constraint_count": ("无约束", "单一约束", "多个相互关联约束", "多层嵌套约束"),
    "evidence_relation": ("无证据链", "单一现象对应", "多条清晰证据链", "干扰排除", "证据冲突与筛选"),
    "interference_exclusion": ("无", "单一干扰", "多个干扰", "多层证据冲突"),
    "experiment_requirement": ("无", "基础操作或读数", "控制变量或现象解释", "方案设计或评价", "多阶段探究与定量误差"),
    "graph_table_requirement": ("无", "直接读数", "多组比较归纳", "拐点或分段反推", "多图表耦合建模"),
    "calculation_model": ("无", "口算或直接比例", "单一方程式或关系式", "单一守恒或多反应计算", "多重守恒差量联立或分类"),
    "unfamiliar_information_transfer": ("无", "课内原型", "给定信息直接套用", "迁移后推导", "完全陌生模型现场建立"),
    "solution_operations": ("1-2个", "3-5个", "6-8个", "9-12个", "12个以上"),
    "equation_count": ("0个", "1个", "2-3个", "4个及以上"),
    "problem_structure": ("概念识记", "化学用语与分类", "实验基础", "实验探究", "物质鉴别除杂推断", "酸碱盐与金属", "工艺流程", "图像表格", "定量计算", "跨模块综合"),
    "information_carrier": ("纯文字", "简单示意图", "实验装置图", "流程图", "图像或表格", "多图表或多材料综合"),
    "reality_question": ("是", "否"),
    "subquestion_dependency": ("无多问", "多问但相互独立", "多问且存在前后依赖"),
    "error_risk": ("无明显易错点", "轻微易错点", "明显易错点", "高易错点"),
}

FEATURE_DEFAULTS = {
    key: values[0] for key, values in FEATURE_VALUES.items()
}
FEATURE_DEFAULTS.update({
    "problem_structure": "概念识记",
    "information_carrier": "纯文字",
    "reality_question": "否",
})


CORE_FIELDS = tuple(list(FEATURE_VALUES)[:15])
AUXILIARY_FIELDS = tuple(list(FEATURE_VALUES)[15:])
VALUE_ALIASES: dict[str, dict[str, str]] = {
    "reasoning_depth": {"0": "0层", "1": "1层", "2-3": "2-3层", "4-5": "4-5层", "6层以上": "6层及以上"},
    "reasoning_direction": {
        "综合推导": "分类讨论或综合推导",
        "逆向推导或综合推导": "分类讨论或综合推导",
        "正向推导结合逆向推理": "分类讨论或综合推导",
    },
    "knowledge_count": {"1": "1个", "2-3": "2-3个", "3个": "2-3个", "3个以上": "4个及以上", "3个及以上": "4个及以上", "4个以上": "4个及以上"},
    "knowledge_relation": {"跨模块深度融合": "多模块深度融合"},
    "representation_conversion": {"宏观-定量转换": "两类表征往返"},
    "reaction_relation": {"单一反应判断": "单一反应", "先后反应": "先后或竞争反应", "多反应计算": "多反应连续转化"},
    "experiment_requirement": {
        "现象解释": "控制变量或现象解释",
        "方案现象推导": "控制变量或现象解释",
        "方案现象分析": "控制变量或现象解释",
        "方案设计": "方案设计或评价",
        "方案评价": "方案设计或评价",
        "方案设计与评价": "方案设计或评价",
    },
    "calculation_model": {
        "单一关系式": "单一方程式或关系式",
        "多反应计算": "单一守恒或多反应计算",
        "单一守恒多反应计算": "单一守恒或多反应计算",
        "单一高阶守恒多反应计算": "单一守恒或多反应计算",
    },
    "solution_operations": {"1-2步": "1-2个", "3-5步": "3-5个", "6-8步": "6-8个", "9-12步": "9-12个", "12步以上": "12个以上"},
    "equation_count": {"0-1个": "0个", "2-3个": "2-3个", "4个以上": "4个及以上"},
    "subquestion_dependency": {"多问且层层递进": "多问且存在前后依赖"},
    "problem_structure": {"物质分类": "化学用语与分类", "化学与生活": "跨模块综合"},
    "information_carrier": {"表格": "图像或表格"},
}


def clean(value: Any) -> str:
    return str(value or "").strip().strip('"\'，,。；; ')


def normalize_features_v3(features: Any) -> tuple[dict[str, str], list[dict[str, str]]]:
    source = features if isinstance(features, dict) else {}
    normalized: dict[str, str] = {}
    warnings: list[dict[str, str]] = []
    for key, allowed in FEATURE_VALUES.items():
        raw = clean(source.get(key))
        value = VALUE_ALIASES.get(key, {}).get(raw, raw)
        if value in allowed:
            normalized[key] = value
        else:
            normalized[key] = FEATURE_DEFAULTS[key]
            warnings.append({"field": key, "raw_value": raw, "fallback": FEATURE_DEFAULTS[key]})
    for key in source:
        if key not in FEATURE_VALUES:
            warnings.append({"field": clean(key), "raw_value": clean(source[key]), "fallback": "ignored_unknown_field"})
    return normalized, warnings


def full_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("stem", "options", "analysis", "sub_questions"):
        value = data.get(key)
        if isinstance(value, dict):
            parts.extend(str(item) for item in value.values())
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def rank_at_least(features: dict[str, str], key: str, minimum: str) -> bool:
    values = FEATURE_VALUES[key]
    return values.index(features[key]) >= values.index(minimum)


def normalize_reasoning(result: dict[str, Any]) -> None:
    raw = result.get("reasoning", result.get("reason", {}))
    fields = ("core_basis", "hard_point", "why_not_lower", "why_not_higher")
    if isinstance(raw, dict):
        result["reasoning"] = {key: clean(raw.get(key)) for key in fields}
    else:
        result["reasoning"] = {
            "core_basis": clean(raw),
            "hard_point": "",
            "why_not_lower": "",
            "why_not_higher": "",
        }


def normalize_subquestions(result: dict[str, Any]) -> None:
    structure = clean(result.get("question_structure"))
    result["question_structure"] = structure if structure in {"单一设问题", "复合题"} else "单一设问题"
    confidence = clean(result.get("confidence"))
    result["confidence"] = confidence if confidence in {"高", "中", "低"} else "中"
    result["highest_difficulty_subquestion"] = clean(result.get("highest_difficulty_subquestion")) or "整题"
    items: list[dict[str, str]] = []
    raw_items = result.get("subquestion_analysis")
    if isinstance(raw_items, list):
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            level = clean(raw.get("difficulty_level"))
            if level not in LEVELS:
                continue
            items.append({
                # Prompt 使用 sub_question；同时兼容历史输出 subquestion。
                "subquestion": clean(raw.get("sub_question", raw.get("subquestion"))) or "整题",
                "coarse_difficulty": COARSE_BY_LEVEL[level],
                "difficulty_level": level,
                "core_basis": clean(raw.get("core_basis")),
            })
    result["subquestion_analysis"] = items


def subquestion_levels(result: dict[str, Any]) -> list[str]:
    return [
        item["difficulty_level"]
        for item in result.get("subquestion_analysis", [])
        if item.get("difficulty_level") in LEVELS
    ]


def highest_subquestion_level(result: dict[str, Any]) -> str | None:
    levels = subquestion_levels(result)
    return max(levels, key=LEVELS.index) if levels else None


def semantic_signals(data: dict[str, Any]) -> dict[str, bool]:
    text = full_text(data)
    has_reaction_context = contains_any(text, [
        "反应", "试剂", "溶液", "沉淀", "滤渣", "滤液", "酸", "碱", "盐",
        "金属", "气体", "生成", "剩余", "恰好完全",
    ])
    return {
        "direct_recall": contains_any(text, ["属于", "元素符号", "化学式表示", "俗称", "含量", "名称是", "用途是"]),
        "equation": contains_any(text, ["化学方程式", "反应方程式"]),
        "experiment": contains_any(text, ["实验", "装置", "操作", "现象", "控制变量", "对照"]),
        "experiment_design": contains_any(text, ["设计实验", "实验方案", "评价方案", "改进方案", "误差分析", "是否合理"]),
        "identify_infer": contains_any(text, ["鉴别", "检验", "除杂", "推断", "滤渣", "滤液", "可能含有"]),
        # “摄入量不足”“营养不足”等自然语言不能被误判为反应物不足。
        "reaction_sequence": has_reaction_context and contains_any(text, [
            "先加入", "再加入", "依次加入", "逐滴加入", "过量试剂", "反应物不足",
            "恰好完全反应", "反应先后", "先反应", "后反应",
        ]),
        "graph": contains_any(text, ["图像", "曲线", "坐标", "拐点", "变化图", "关系图"]),
        "graph_inference": contains_any(text, ["拐点", "分段", "平台", "斜率", "反推", "变化趋势"]),
        "process": contains_any(text, ["流程", "工艺", "制备", "转化关系"]),
        "conservation": contains_any(text, ["质量守恒", "元素守恒", "差量", "联立", "分类讨论"]),
        # 普通条件句“若……”不是分类讨论；必须存在明确分支或多种可能性。
        "branching": contains_any(text, ["分类讨论", "分情况", "分别讨论", "否则", "可能有也可能没有", "至少存在两种情况"]),
        "multi_question": bool(re.search(r"[（(][1-9一二三四五六七八九][）)]", text)),
    }


def medium_evidence(f: dict[str, str], sem: dict[str, bool]) -> tuple[list[str], list[str]]:
    structural: list[str] = []
    semantic: list[str] = []
    checks = [
        (rank_at_least(f, "reasoning_depth", "2-3层"), "2-3层以上必要推导"),
        (rank_at_least(f, "knowledge_relation", "同模块深度关联"), "知识点深度关联"),
        (rank_at_least(f, "representation_conversion", "两类表征往返"), "两类表征往返"),
        (rank_at_least(f, "reaction_relation", "2-3个并列或简单连续反应"), "多个反应参与"),
        (rank_at_least(f, "constraint_count", "多个相互关联约束"), "多个关联约束"),
        (rank_at_least(f, "evidence_relation", "多条清晰证据链"), "多条证据链"),
        (rank_at_least(f, "experiment_requirement", "控制变量或现象解释"), "控制变量或现象解释"),
        (rank_at_least(f, "graph_table_requirement", "多组比较归纳"), "多组图表归纳"),
        (rank_at_least(f, "calculation_model", "单一方程式或关系式"), "方程式或关系式计算"),
        (rank_at_least(f, "unfamiliar_information_transfer", "迁移后推导"), "信息迁移推导"),
    ]
    structural.extend(reason for yes, reason in checks if yes)
    for yes, reason in [
        (sem["experiment"], "题目含实验分析语义"),
        (sem["identify_infer"], "题目含鉴别、除杂或推断任务"),
        (sem["reaction_sequence"], "题目含反应顺序或过量条件"),
        (sem["graph"], "题目含图像关系"),
        (sem["process"], "题目含工艺流程"),
    ]:
        if yes:
            semantic.append(reason)
    return structural, semantic


def hard_evidence(f: dict[str, str], sem: dict[str, bool]) -> tuple[list[str], list[str]]:
    structural: list[str] = []
    semantic: list[str] = []
    checks = [
        (rank_at_least(f, "reasoning_depth", "4-5层"), "4-5层以上推导"),
        (rank_at_least(f, "reasoning_direction", "逆向推导"), "逆向或综合推导"),
        (rank_at_least(f, "knowledge_relation", "跨模块融合"), "跨模块融合"),
        (rank_at_least(f, "representation_conversion", "宏观-微观-符号-定量多重转换"), "多重表征转换"),
        (rank_at_least(f, "reaction_relation", "多反应连续转化"), "多反应连续或竞争"),
        (rank_at_least(f, "excess_deficiency", "需要判断过量不足"), "需判断过量不足"),
        (rank_at_least(f, "constraint_count", "多个相互关联约束"), "多个关联约束"),
        (rank_at_least(f, "evidence_relation", "干扰排除"), "证据干扰排除"),
        (rank_at_least(f, "interference_exclusion", "多个干扰"), "多个干扰排除"),
        (rank_at_least(f, "experiment_requirement", "方案设计或评价"), "方案设计或评价"),
        (rank_at_least(f, "graph_table_requirement", "拐点或分段反推"), "图像拐点或分段反推"),
        (rank_at_least(f, "calculation_model", "单一守恒或多反应计算"), "守恒或多反应计算"),
        (rank_at_least(f, "unfamiliar_information_transfer", "迁移后推导"), "陌生信息迁移"),
    ]
    structural.extend(reason for yes, reason in checks if yes)
    for yes, reason in [
        (sem["experiment_design"], "题目要求实验设计、评价或误差分析"),
        (sem["reaction_sequence"], "题目存在反应先后或过量不足"),
        (sem["graph_inference"], "题目要求图像拐点或分段推断"),
        (sem["conservation"], "题目要求守恒、差量或联立"),
        (sem["branching"], "题目存在分类讨论"),
    ]:
        if yes:
            semantic.append(reason)
    return structural, semantic


def final_evidence(f: dict[str, str], sem: dict[str, bool]) -> tuple[list[str], list[str]]:
    checks = [
        (rank_at_least(f, "reasoning_depth", "6层及以上"), "6层以上推导"),
        (rank_at_least(f, "reasoning_direction", "分类讨论或综合推导"), "分类讨论或综合推导"),
        (rank_at_least(f, "knowledge_relation", "多模块深度融合"), "多模块深度融合"),
        (rank_at_least(f, "constraint_count", "多层嵌套约束"), "多层嵌套约束"),
        (rank_at_least(f, "evidence_relation", "证据冲突与筛选"), "证据冲突与筛选"),
        (rank_at_least(f, "interference_exclusion", "多层证据冲突"), "多层证据冲突"),
        (rank_at_least(f, "experiment_requirement", "多阶段探究与定量误差"), "多阶段探究与定量误差"),
        (rank_at_least(f, "graph_table_requirement", "多图表耦合建模"), "多图表耦合建模"),
        (rank_at_least(f, "calculation_model", "多重守恒差量联立或分类"), "多重守恒、差量、联立或分类"),
        (rank_at_least(f, "unfamiliar_information_transfer", "完全陌生模型现场建立"), "陌生模型现场建立"),
    ]
    structural = [reason for yes, reason in checks if yes]
    semantic = [reason for yes, reason in [
        (sem["branching"] and sem["conservation"], "分类讨论与复杂定量共同出现"),
        (sem["experiment_design"] and sem["conservation"], "实验设计与定量误差共同出现"),
        (sem["graph_inference"] and sem["reaction_sequence"], "图像反推与反应先后共同出现"),
    ] if yes]
    return structural, semantic


def low_structure(f: dict[str, str]) -> bool:
    return all((
        f["reasoning_depth"] in {"0层", "1层"},
        f["reasoning_direction"] in {"直接识记", "正向推导"},
        f["knowledge_count"] == "1个",
        f["knowledge_relation"] in {"单一知识点", "同模块简单关联"},
        f["representation_conversion"] in {"无", "一次表征转换"},
        f["reaction_relation"] in {"无反应关系", "单一反应"},
        f["constraint_count"] in {"无约束", "单一约束"},
        f["evidence_relation"] in {"无证据链", "单一现象对应"},
        f["experiment_requirement"] in {"无", "基础操作或读数"},
        f["graph_table_requirement"] in {"无", "直接读数"},
        f["calculation_model"] in {"无", "口算或直接比例"},
        f["unfamiliar_information_transfer"] in {"无", "课内原型", "给定信息直接套用"},
    ))


def conventional_medium_guard(f: dict[str, str], result: dict[str, Any]) -> bool:
    """教师理由口径下的常规3档保护。

    防止模型把选择题中的普通选项排除写成“多约束/多干扰”，或把标准单反应
    pH 曲线写成“分段反推”，进而被后处理机械升为4档。
    """
    return bool(
        f["reasoning_depth"] == "2-3层"
        and f["reasoning_direction"] == "正向推导"
        and f["knowledge_relation"] in {"单一知识点", "同模块简单关联", "同模块深度关联"}
        and f["reaction_relation"] in {"无反应关系", "单一反应", "2-3个并列或简单连续反应"}
        and f["excess_deficiency"] in {"无", "条件直接给定"}
        and f["calculation_model"] in {"无", "口算或直接比例", "单一方程式或关系式"}
        and f["experiment_requirement"] not in {"方案设计或评价", "多阶段探究与定量误差"}
        and f["unfamiliar_information_transfer"] in {"无", "课内原型", "给定信息直接套用"}
        and highest_subquestion_level(result) in {None, "送分题", "基础题", "中等题"}
    )


def direct_recall(f: dict[str, str], sem: dict[str, bool]) -> bool:
    feature_direct = bool(
        f["reasoning_depth"] == "0层"
        and f["reasoning_direction"] == "直接识记"
        and f["knowledge_count"] == "1个"
        and f["knowledge_relation"] == "单一知识点"
        and f["representation_conversion"] == "无"
        and f["reaction_relation"] == "无反应关系"
        and f["constraint_count"] == "无约束"
        and f["evidence_relation"] == "无证据链"
        and f["experiment_requirement"] == "无"
        and f["graph_table_requirement"] == "无"
        and f["calculation_model"] == "无"
    )
    # 教师理由显示：元素含量、仪器、实验现象、图标、科学家成就等纯识记题
    # 未必命中固定词表。只要特征严格为0层，且题面没有高阶任务语义，即可认定为直接识记。
    higher_task = any((
        sem["experiment_design"], sem["identify_infer"], sem["reaction_sequence"],
        sem["graph_inference"], sem["process"], sem["conservation"], sem["branching"],
    ))
    return feature_direct and (sem["direct_recall"] or not higher_task)


def teacher_direct_judgement(
    f: dict[str, str], result: dict[str, Any], data: dict[str, Any]
) -> bool:
    """按全量教师标签口径识别“熟悉事实直接判断”的1档题。

    全量回放显示，模型经常把课内现象、物质类别、化学变化、环保常识等
    直接判断描述成“1层应用”并判为2档。这里不依赖关键词直接降档，而是
    要求22项特征同时呈现单知识点、无计算、无实验任务、无表征转换、
    无反应关系的低结构；同时排除要求说明理由和含多组编号陈述的题目。
    """
    text = full_text(data)
    options = clean(data.get("options"))
    circled_statement_count = len(re.findall(r"[①②③④⑤⑥⑦⑧⑨]", text))
    # 表格题有时把四组事实写进 stem，而 options 退化成 A.A/B.B/C.C/D.D。
    # 这种题必须联合核验异质陈述，不能被当成单事实直答题降到1档。
    placeholder_options = bool(re.fullmatch(
        r"\s*A[.、．]\s*A\s*B[.、．]\s*B\s*C[.、．]\s*C\s*D[.、．]\s*D\s*",
        options,
    ))
    embedded_option_table = placeholder_options or bool(
        re.search(r"A.+B.+C.+D.+", clean(data.get("stem")))
        and contains_any(clean(data.get("stem")), ["事实", "解释", "说法", "用途"])
    )
    # 教师边界样本表明，下列任务虽然只有一个核心知识点，却要求把机理或
    # 多项性质用于新情境，属于实质的一步应用，应保留2档。
    conductivity_application = "导电" in text and contains_any(text, ["离子", "带电粒子", "自由移动"])
    gas_selection_application = (
        contains_any(text, ["飞艇", "气囊", "填充气体"])
        # “使气球升空”只需把密度性质用于新情境，单一性质已构成一步应用。
        and sum(term in text for term in ["密度", "稳定", "可燃", "助燃", "安全"]) >= 1
    )
    combustion_mechanism_application = (
        "燃烧" in text and contains_any(text, ["接触面积", "架空", "着火点"])
    )
    indicator_or_ph_application = any((
        "pH" in text or "ph" in text,
        contains_any(text, ["石蕊", "酚酞", "指示剂"])
        and contains_any(text, ["变红", "变蓝", "酸性", "碱性"]),
    ))
    particle_or_material_application = contains_any(text, [
        "最外层电子", "容易失去电子", "容易得到电子", "粒子结构", "原子结构示意图",
        "由分子构成", "由原子构成", "由离子构成", "同素异形体",
    ])
    carbon_policy_application = contains_any(text, ["碳达峰", "碳中和"]) and contains_any(
        text, ["推进", "不利于", "研发", "碳封存"]
    )
    chemical_change_with_auxiliary_property = (
        "化学变化" in text
        and contains_any(text, ["除锈", "钝化", "酸性试剂", "腐蚀", "氧化膜"])
    )
    substantive_one_step_application = any((
        conductivity_application,
        gas_selection_application,
        combustion_mechanism_application,
        indicator_or_ph_application,
        particle_or_material_application,
        carbon_policy_application,
        chemical_change_with_auxiliary_property,
    ))
    raw_problem_structure = clean(result.get("features_raw", {}).get("problem_structure"))
    return bool(
        result.get("question_structure") == "单一设问题"
        and f["reasoning_depth"] == "1层"
        and f["knowledge_count"] == "1个"
        and f["problem_structure"] == "概念识记"
        # 不能把未知/越界的题型值回退成“概念识记”后据此降档。
        and raw_problem_structure == "概念识记"
        and f["representation_conversion"] == "无"
        and f["reaction_relation"] == "无反应关系"
        and f["experiment_requirement"] == "无"
        and f["calculation_model"] == "无"
        and f["solution_operations"] == "1-2个"
        and "理由是" not in text
        and circled_statement_count < 3
        and not embedded_option_table
        and not substantive_one_step_application
    )


def set_level(result: dict[str, Any], new_level: str, rule: str, structural: list[str], semantic: list[str]) -> None:
    old_level = result["difficulty_level"]
    if abs(LEVELS.index(new_level) - LEVELS.index(old_level)) != 1:
        raise ValueError(f"v3 后处理禁止非相邻改档: {old_level} -> {new_level}")
    action = {
        "from": old_level,
        "to": new_level,
        "rule": rule,
        "structural_evidence": structural,
        "semantic_evidence": semantic,
    }
    result["difficulty_level"] = new_level
    result["coarse_difficulty"] = COARSE_BY_LEVEL[new_level]
    result["postprocess_trace"].append(action)
    result["postprocess_actions"].append(copy.deepcopy(action))


def sync_reasoning(result: dict[str, Any]) -> None:
    if not result.get("postprocess_trace"):
        return
    action = result["postprocess_trace"][-1]
    evidence = action["structural_evidence"] + action["semantic_evidence"]
    explanation = "；".join(evidence) or action["rule"]
    reasoning = result["reasoning"]
    reasoning["core_basis"] = f"后处理按 {action['rule']} 将{action['from']}校准为{action['to']}：{explanation}。"
    index = LEVELS.index(result["difficulty_level"])
    reasoning["why_not_lower"] = (
        f"存在支持{result['difficulty_level']}的联合证据：{explanation}。"
        if index > 0 else "已经是最低难度档。"
    )
    reasoning["why_not_higher"] = (
        "尚未同时满足更高档所需的核心联合证据。"
        if index < len(LEVELS) - 1 else "已经是最高难度档。"
    )


def prepare_result(result: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if not result:
        return result
    original_level = clean(result.get("difficulty_level"))
    raw_features = copy.deepcopy(result.get("features"))
    features, warnings = normalize_features_v3(raw_features)
    result["features_raw"] = raw_features
    result["features"] = features
    result["feature_normalization_warnings"] = warnings
    normalize_reasoning(result)
    normalize_subquestions(result)
    result["postprocess_original_level"] = original_level
    result["postprocess_trace"] = []
    result["postprocess_actions"] = []
    result["feature_schema_version"] = "chemistry_0724_v5_2_22_features_image_primary_mintext"
    result["calibration_basis"] = "teacher_reason_rubric_clean578_v52_image_primary_mintext_ablation"
    result["postprocess_profile"] = "0724_v5_2_image_primary_mintext_prompt_only"
    result["feature_audit_flags"] = []

    if original_level not in LEVELS:
        result["difficulty_level"] = highest_subquestion_level(result) or "中等题"
        action = {
            "from": original_level,
            "to": result["difficulty_level"],
            "rule": "invalid_level_repair",
            "structural_evidence": ["模型没有返回合法整题难度"],
            "semantic_evidence": [],
        }
        result["postprocess_trace"].append(action)
        result["postprocess_actions"].append(copy.deepcopy(action))
    result["coarse_difficulty"] = COARSE_BY_LEVEL[result["difficulty_level"]]
    if warnings:
        result["feature_audit_flags"].append(f"有{len(warnings)}个特征值被规范化、回退或忽略")
    if result["question_structure"] == "复合题" and not result["subquestion_analysis"]:
        result["feature_audit_flags"].append("模型声明为复合题但未返回合法 subquestion_analysis")
    expected_subquestions = len(data.get("sub_questions", []) or [])
    actual_subquestions = len(result.get("subquestion_analysis", []) or [])
    if expected_subquestions and actual_subquestions < expected_subquestions:
        result["feature_audit_flags"].append(
            f"小问覆盖不足: 输入{expected_subquestions}问，模型仅分析{actual_subquestions}问"
        )
    image_urls = [
        str(data.get(key, "") or "").strip()
        for key in ("stem_pic_url", "analysis_pic_url")
        if str(data.get(key, "") or "").strip()
    ]
    if image_urls and not ENABLE_IMAGE_INPUT:
        result["feature_audit_flags"].append("题目包含图片URL，但本次未启用CHEMISTRY_ENABLE_IMAGE_INPUT")
    redline_check = result.get("five_level_redline_check")
    if isinstance(redline_check, dict):
        triggered = clean(redline_check.get("triggered"))
        if triggered == "是" and result.get("difficulty_level") != "压轴题":
            result["feature_audit_flags"].append("模型自报触发5档红线，但最终档位不是压轴题")
    if semantic_signals(data)["multi_question"] and result["question_structure"] == "单一设问题":
        result["feature_audit_flags"].append("题目疑似包含多个小问，但 question_structure=单一设问题")
    basis = result["reasoning"].get("core_basis", "")
    if result["difficulty_level"] in {"拔高题", "压轴题"} and contains_any(
        basis, ["2-3层正向", "无高阶卡点", "不存在高阶卡点", "证据关系清晰，无复杂"]
    ):
        result["feature_audit_flags"].append("理由描述为常规2-3层或无高阶卡点，但最终给出4-5档，存在跨字段矛盾")
    if (
        result["question_structure"] == "复合题"
        and result["features"]["subquestion_dependency"] == "多问但相互独立"
        and result["features"]["knowledge_count"] == "4个及以上"
    ):
        result["feature_audit_flags"].append("独立多问却统计4个及以上知识点，可能错误累计不同小问")
    return result


def postprocess_prompt_only(result: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    return prepare_result(result, data)


def postprocess_safe(result: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    result = prepare_result(result, data)
    if not result:
        return result
    result["postprocess_profile"] = f"0724_v5_2_image_primary_mintext_{POSTPROCESS_PROFILE}"
    # 非法档位已经在 prepare_result 中修复；禁止再级联执行第二次改档。
    if result["postprocess_original_level"] not in LEVELS:
        sync_reasoning(result)
        return result
    raw_level = result["difficulty_level"]
    f = result["features"]
    sem = semantic_signals(data)
    sub_high = highest_subquestion_level(result)
    medium_struct, medium_sem = medium_evidence(f, sem)
    hard_struct, hard_sem = hard_evidence(f, sem)
    final_struct, final_sem = final_evidence(f, sem)
    decisive_final_context = any((
        rank_at_least(f, "unfamiliar_information_transfer", "迁移后推导"),
        rank_at_least(f, "reasoning_direction", "分类讨论或综合推导"),
        rank_at_least(f, "graph_table_requirement", "拐点或分段反推"),
        f["calculation_model"] == "多重守恒差量联立或分类",
        f["constraint_count"] == "多层嵌套约束",
    ))
    decisive_hard_context = any((
        rank_at_least(f, "reasoning_depth", "4-5层"),
        rank_at_least(f, "reasoning_direction", "逆向推导"),
        rank_at_least(f, "knowledge_relation", "跨模块融合"),
        rank_at_least(f, "representation_conversion", "宏观-微观-符号-定量多重转换"),
        rank_at_least(f, "reaction_relation", "多反应连续转化"),
        rank_at_least(f, "experiment_requirement", "方案设计或评价"),
        rank_at_least(f, "calculation_model", "单一守恒或多反应计算"),
        rank_at_least(f, "unfamiliar_information_transfer", "迁移后推导"),
        sem["experiment_design"],
        sem["conservation"],
        sem["branching"],
    ))

    # 每个分支都锚定 raw_level；命中后不再执行其他规则。
    if raw_level == "送分题":
        substantive = len(medium_struct) >= 1 or sem["experiment_design"] or sem["reaction_sequence"]
        if not direct_recall(f, sem) and substantive:
            # clean614 回放中该规则唯一一次命中即把正确1档改错。1档样本存在
            # “并列重复识记但题面很长”的教师口径，故这里只记录风险，不自动升档。
            result["feature_audit_flags"].append(
                "送分题含一步应用或中等结构信号，v5保守策略未自动升档"
            )

    elif raw_level == "基础题":
        # V5.1 clean578回放：2→1共改7题，3题改对、3题改错，另有1题
        # 从3→2进一步恶化为3→1，精确ACC净收益为0且新增严重偏差。
        # safe_v5冻结自动2→1；safe_v4仅用于复现旧结果。
        if POSTPROCESS_PROFILE != "safe_v4":
            if direct_recall(f, sem) or teacher_direct_judgement(f, result, data):
                result["feature_audit_flags"].append(
                    "疑似直接识记或熟悉事实，但V5.2冻结无净收益的基础题→送分题自动降档"
                )
        elif direct_recall(f, sem) and sub_high in {None, "送分题", "基础题"}:
            set_level(result, "送分题", "basic_to_easy_strict_direct_recall", ["0层单知识点直接识记"], ["题干为课内事实直接判断"])
        elif teacher_direct_judgement(f, result, data) and sub_high in {None, "送分题", "基础题"}:
            set_level(
                result,
                "送分题",
                "basic_to_easy_teacher_direct_judgement",
                ["1层单知识点、无计算、无实验任务且无表征或反应转换"],
                ["教师口径下属于熟悉课内事实的直接判断"],
            )
        elif POSTPROCESS_PROFILE == "safe_v4" and (
            (
                sub_high in {"中等题", "拔高题", "压轴题"}
                and len(medium_struct) >= 1
            )
            or (len(medium_struct) >= 2 and len(medium_sem) >= 1)
        ):
            set_level(result, "中等题", "basic_to_medium_joint_structure", medium_struct[:5], medium_sem[:3])

    elif raw_level == "中等题":
        standard_operation_choice = (
            POSTPROCESS_PROFILE in {"safe_v5", "exploratory_v5"}
            and f["problem_structure"] == "实验基础"
            and f["reaction_relation"] == "无反应关系"
            and f["experiment_requirement"] == "基础操作或读数"
            and f["calculation_model"] == "无"
            and f["graph_table_requirement"] in {"无", "直接读数"}
            and f["subquestion_dependency"] in {"无多问", "多问但相互独立"}
        )
        if standard_operation_choice:
            set_level(
                result,
                "基础题",
                "medium_to_basic_parallel_standard_operations",
                ["实验基础", "无反应推导与计算", "基础操作或读数", "独立选项"],
                ["每个选项仅需一条课内操作规则独立核验"],
            )
        elif POSTPROCESS_PROFILE == "safe_v4" and (
            low_structure(f)
            and not hard_sem
            # 教师理由中简单装置、仪器用途和基础实验应用可为2档；
            # “实验/图像”题型名称本身不能阻止低结构题降档，只拦截实质高阶任务。
            and not any((sem["experiment_design"], sem["identify_infer"], sem["reaction_sequence"], sem["graph_inference"], sem["process"]))
            and sub_high in {None, "送分题", "基础题"}
        ):
            set_level(result, "基础题", "medium_to_basic_low_structure_guard", ["核心特征均处于低结构范围"], [])
        elif POSTPROCESS_PROFILE == "safe_v4" and not conventional_medium_guard(f, result) and (
            (sub_high in {"拔高题", "压轴题"} and len(hard_struct) >= 1)
            or (len(hard_struct) >= 2 and len(hard_sem) >= 1)
        ) and (
            f["subquestion_dependency"] == "无多问"
            and rank_at_least(f, "graph_table_requirement", "多组比较归纳")
            and decisive_hard_context
        ):
            set_level(result, "拔高题", "medium_to_hard_joint_structure", hard_struct[:6], hard_sem[:3])

    elif raw_level == "拔高题":
        # safe_v5只使用V5.1全量回放中8/8为标准5档的深耦合交集。
        # 不使用题干长度、题型名称或单一feature；一次只升相邻一档。
        deep_continuous_cross_module = (
            f["reasoning_direction"] == "正向推导"
            and f["knowledge_relation"] in {"跨模块融合", "多模块深度融合"}
            and f["reaction_relation"] == "多反应连续转化"
            and f["constraint_count"] in {"多个相互关联约束", "多层嵌套约束"}
            and f["calculation_model"] != "无"
            and f["evidence_relation"] != "无证据链"
        )
        # exploratory_v5额外开放第二个8/8交集，只用于消融，不作为默认配置。
        evidence_interference_cross_module = (
            f["knowledge_relation"] in {"跨模块融合", "多模块深度融合"}
            and f["evidence_relation"] == "多条清晰证据链"
            and f["interference_exclusion"] == "单一干扰"
            and f["constraint_count"] in {"多个相互关联约束", "多层嵌套约束"}
        )
        if (
            POSTPROCESS_PROFILE in {"safe_v5", "exploratory_v5"}
            and deep_continuous_cross_module
        ):
            set_level(
                result,
                "压轴题",
                "hard_to_final_deep_continuous_cross_module",
                ["跨模块融合", "多反应连续转化", "多个关联约束", "非空定量模型"],
                ["反应、证据和定量结果形成不可拆分的连续任务链"],
            )
        elif (
            POSTPROCESS_PROFILE == "exploratory_v5"
            and evidence_interference_cross_module
        ):
            set_level(
                result,
                "压轴题",
                "hard_to_final_cross_evidence_interference",
                ["跨模块融合", "多条清晰证据链", "单一实质干扰", "多个关联约束"],
                ["证据排除会改变后续定量或成分判断"],
            )
        # safe_v4保留V5.1旧规则，便于公平复现实验结果。
        strict_final_chain = (
            f["knowledge_relation"] in {"跨模块融合", "多模块深度融合"}
            and rank_at_least(f, "calculation_model", "单一守恒或多反应计算")
            and f["subquestion_dependency"] == "多问且存在前后依赖"
        )
        if not result["postprocess_trace"] and POSTPROCESS_PROFILE == "safe_v4" and strict_final_chain:
            set_level(
                result,
                "压轴题",
                "hard_to_final_cross_module_dependent_calculation",
                ["跨模块融合", "守恒或多反应计算", "多问且存在前后依赖"],
                ["前置小问结论进入后续定量模型"],
            )
        elif not result["postprocess_trace"] and (
            # 教师难度释义规定5档压轴红线“满足任意1条”。当模型已把最高难
            # 小问判为5档时，只需一个完整5档结构证据；否则仍要求结构与语义
            # 联合，避免把题型关键词机械升档。
            (sub_high == "压轴题" and len(final_struct) >= 1 and decisive_final_context)
            or (
                len(final_struct) >= 1
                and len(final_sem) >= 1
                and len(hard_struct) >= 3
                and decisive_final_context
            )
        ):
            # 其余宽泛5档信号仍只审计，避免把正确4档机械升为5档。
            result["feature_audit_flags"].append(
                "具备疑似5档联合证据，但未满足V5.2当前配置的严格联合结构，未自动升档"
            )
        elif (
            POSTPROCESS_PROFILE == "safe_v4"
            and len(hard_struct) == 0
            and not hard_sem
            and sub_high in {None, "送分题", "基础题", "中等题"}
        ):
            set_level(result, "中等题", "hard_to_medium_missing_hard_evidence", ["缺少拔高档核心结构证据"], [])

    elif raw_level == "压轴题":
        # 一个完整的5档结构证据即可满足教师释义；只有完全缺少压轴结构、
        # 语义和小问支持时才降档。
        if (
            POSTPROCESS_PROFILE == "safe_v4"
            and len(final_struct) == 0
            and not final_sem
            and sub_high != "压轴题"
        ):
            set_level(result, "拔高题", "final_to_hard_missing_coupled_evidence", final_struct, [])

    # 仅审计不改档：辅助字段不得成为唯一升档依据。
    if not result["postprocess_trace"] and any(
        f[key] != FEATURE_DEFAULTS[key] for key in AUXILIARY_FIELDS
    ) and not any(f[key] != FEATURE_DEFAULTS[key] for key in CORE_FIELDS):
        result["feature_audit_flags"].append("仅辅助特征非默认值，按 Prompt 约束未据此升档")
    sync_reasoning(result)
    return result





# -------------------------- 9. Evidence-15 V9阶段2.1严格生产契约 --------------------------
#
# 本版保留图片输入、缓存、重试和并发，只替换生产JSON与后处理入口：
#   1. 模型只输出features/coarse_difficulty/reasoning/difficulty_level；
#   2. 小问关系在Prompt内部完成，不再用源数据sub_questions数量拒绝模型结果；
#   3. 既有定向规则只读取严格的15项枚举，最多调整一个相邻档；
#   4. 不读取题目ID、教师标签或离线错题清单。
from chemistry_evidence15_v9_schema import (  # noqa: E402
    AUDIT_FEATURE_FIELDS,
    AUDIT_FEATURE_VALUES,
    CORE_FEATURE_FIELDS,
    CORE_FEATURE_VALUES,
    Core12SchemaError,
    apply_data_aware_boundary_rules,
    apply_targeted_evidence_rules,
    convert_legacy_features,
    validate_and_prepare_result,
)

ALLOW_LEGACY_CORE12_SCHEMA = os.getenv(
    "CHEMISTRY_EVIDENCE15_V9_ALLOW_LEGACY_SCHEMA",
    os.getenv("CHEMISTRY_CORE12_ALLOW_LEGACY_SCHEMA", "0"),
).strip().lower() in {"1", "true", "yes", "on"}


def postprocess_chemistry_difficulty(
    rating_result: Dict[str, Any],
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """The only production postprocess entry.

    schema_only只校验结构；V9阶段2.1继续沿用阶段1后处理，按15项证据交集
    最多调整一个相邻档，并完整记录postprocess_actions。
    """
    if CORE12_POSTPROCESS_PROFILE not in {
        "evidence15_v9_schema_only",
        "core12_schema_only",
        "core12_targeted_rules_v3",
        "core12_dataaware_rules_v5",
        "evidence15_boundary_rules_v6",
        "evidence15_boundary_rules_v9_stage1",
    }:
        raise Core12SchemaError(
            f"未知Evidence-15 V9后处理profile: {CORE12_POSTPROCESS_PROFILE!r}"
        )
    prepared = validate_and_prepare_result(
        rating_result,
        data,
        allow_legacy=ALLOW_LEGACY_CORE12_SCHEMA,
    )
    if CORE12_POSTPROCESS_PROFILE == "core12_targeted_rules_v3":
        return apply_targeted_evidence_rules(prepared)
    if CORE12_POSTPROCESS_PROFILE in {
        "core12_dataaware_rules_v5",
        "evidence15_boundary_rules_v6",
        "evidence15_boundary_rules_v9_stage1",
    }:
        return apply_data_aware_boundary_rules(prepared, data)
    prepared["postprocess_profile"] = "evidence15_v9_schema_only"
    prepared["automatic_level_change_applied"] = False
    return prepared

if __name__ == "__main__":
    print("Evidence-15 V9阶段2.2: 1↔2教材原型识别与真实推导边界收窄")
    print(f"Evidence-15 V9阶段2.2后处理配置: {CORE12_POSTPROCESS_PROFILE}（算法不变，同时报告raw与final）")
    print(f"Evidence-15 V9旧结构兼容: {'enabled' if ALLOW_LEGACY_CORE12_SCHEMA else 'disabled'}")
    print(f"Evidence-15 V9 image-primary图像输入: {'enabled' if ENABLE_IMAGE_INPUT else 'disabled'}")
    start_time = time.time()
    try:
        asyncio.run(main_batch_run())
    except KeyboardInterrupt:
        print("\n收到键盘中断信号，程序已安全退出。")
    except Exception as e:
        print(f"\n批量运行中遇到未捕获异常: {e}")
    print(f"本次打标运行耗时: {round((time.time() - start_time) / 60, 2)} 分钟。")
