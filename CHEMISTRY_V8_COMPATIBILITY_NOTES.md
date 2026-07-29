# 化学 V8 Prompt 与后处理兼容性说明

## 结论

V8 Prompt不能直接接入旧V6的`Evidence-15 / Core-12`自动改档后处理，也不能直接接入A3紧凑任务结构Schema。它可以通过V8专用Schema接入当前“只审计、不自动改档”的后处理策略。

## 不直接兼容的字段

- V8输出`primary_dimensions`，A3输出`rating_dimensions`；
- V8输出完整`task_graph`，A3输出紧凑`task_structure`；
- V8节点新增`input`，边新增`transferred_object`；
- V8支持`shared_models`和七个`audit_attributes`；
- 旧Evidence/Core-12规则依赖`reasoning_depth`、`reaction_relation`、`calculation_model`等字段，V8没有这些字段。

把V8审计属性强行映射成Evidence-15会重新引入同源证据重复计数和feature抽取误差，因此本实验不做伪映射。

## V8专用后处理

专用Schema负责：

- 严格校验五个主维度枚举；
- 校验节点ID、边引用、共享模型引用和有向无环图；
- 计算节点数、依赖边数、共享模型数、最长有向路径和跨问边数；
- 记录送分题却存在连续依赖、中等以上却只有单节点、拔高无依赖边、压轴缺少耦合等张力。

上述张力只生成审计旗标。首轮实验中`postprocess_original_level`、`postprocess_final_level`和`difficulty_level`保持一致，因此raw与final ACC应完全相同。旗标用于逐题分析，不能证明净收益前不启用自动改档。

## 实验应回答的问题

1. V8完整任务图是否比A3紧凑结构提高原始ACC；
2. Schema失败和重试是否显著增加；
3. 单调用完整任务图的速度是否可接受；
4. 审计旗标能否准确定位Prompt高估、低估或任务图漏抽；
5. 若未来启用窄规则，必须以逐题人工裁定和重复运行证明净收益。
