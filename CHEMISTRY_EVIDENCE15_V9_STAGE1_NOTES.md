# Evidence-15 V9 阶段1包

本包只实施阶段1的必要修改，不提前改写教师边界和后处理算法。

## 阶段1已修改

1. 版本统一为 Evidence-15 V9 阶段1。
2. Prompt由七步改为六步：先核对真实解题任务和完成相邻定档，再填写15项审计特征。
3. 删除“任务图”“任务边”和“三点五”等结构表述，统一使用“核对真实解题任务”“前后依赖”。
4. 15项特征连续编号1—15，不再使用“3项入口特征+12项核心特征”的混合说法。
5. 连续依赖必须同时满足：前一步产生新结论；后一步必须使用该结论。
6. Schema不再强制入口操作决定总推理深度，也不再强制内部依赖等于跨小问依赖。
7. 输出JSON字段和15项枚举保持不变。
8. 既有V6后处理算法保持不变，仅使用V9阶段1版本标识。
9. Runner默认并发数为30。

## 阶段1未修改

- 未处理教师示例之间的具体口径冲突。
- 未压缩和去重全部教师示例。
- 未重写1↔2、2↔3、3↔4、4↔5边界。
- 未增加任务图、依赖边JSON或第二次模型调用。
- 未根据591题错题增加定向规则。

## 主要文件

- `prompts/evidence15_v9_prompt.txt`
- `src/chemistry_evidence15_v9_schema.py`
- `src/chemistry_difficulty_rating_evidence15_v9_with_cache.py`
- `tests/test_prompt_evidence15_v9_contract.py`
- `tests/test_evidence15_v9_schema_retry.py`
- `tools/run_chemistry_evidence15_v9_stage1_teacher0724_591.sh`

## 建议验证顺序

```bash
PYTHONPATH=src python -m unittest -v \
  tests.test_prompt_evidence15_v9_contract \
  tests.test_evidence15_v9_schema_retry
```

阶段1人工审查通过后再进入阶段2；当前不建议直接依据一次591题结果新增后处理规则。
