# PTR-P01 规则冻结草案（非发布产物）

日期：2026-10-11

本文只记录审计契约和候选结构，不把 PTR↔Report 离线草案当作正式结果。当前 `report_ptr` 已按三份试点授权启用；`report_ptr_report` 组合别名仍保持 `enabled=false`，原因码为 `PTR_NOT_VALIDATED`。输入、结果和证据的接口合同由 `mvp/ptr_report_contract.py` 提供，正式结果由 `mvp.run_ptr_report` 生成。

当前另有 `mvp/ptr_compare.py` 作为离线比较层：它执行范围、条款、数值、单位、引用和覆盖层的规则核对，并输出不可发布的 Coverage Ledger；`mvp/ptr_ocr.py` 只提供扫描 PTR 的本机 OCR 观察，`mvp/ptr_llm.py` 只验证未来 LLM 的逐字证据。离线草案不能绕过正式 Runner，也不能由 LLM 单独给出通过结论。

## 2026-10-24 离线草案增量

当前离线比较器的执行规则版本为 `ptr-compare-0.4` / `0.4.0-draft`，布局解析器为 `ptr-layout-1.2`。相较本文件最初的候选结构，已补齐以下可复核行为：

- PTR 和 Report 的内联子条款、条款号与标题分列会按页面坐标重建；分列条款、逗号/句点子条款和跨页续项都保留独立节点。
- Report 合并的“标准要求”列保留物理列索引；无边框表格的续行按逻辑条款聚合。1539 的表 6 已可以和 PTR 表 6 做语义矩阵比较，结构证据不足仍为 `manual`。
- Coverage Ledger 为表/附录引用保存物理绑定状态；未解析、多候选或缺少矩阵证据的引用不会自动通过。
- `rules+llm` 只生成文本歧义队列；任务带提示词版本和输入哈希，显式 provider 的验证结果和处理动作进入审计记录。数值、单位、范围、引用和表格硬差异不能由 LLM 清除。
- 12 组同号 PTR/Report 批量运行均满足 `eligible == accounted`、`conserved=true`；扫描 PTR 5780/5782 全部保持 `manual`。

这些增量仍以离线草案形式保留；正式 `report_ptr` Runner 会在能力目录开启后把规则账本转换成可发布的 WorkerResult，草案本身始终不可发布。

## 输入与范围

- 输入角色固定为 `report` 和 `ptr`；两侧在草案中保存路径、页数、页面几何和 SHA-256。
- 正式 Run 的输入字段固定为 `report_document_id` 与 `ptr_document_id`，一对一、创建后不可替换；`record_document_id` 被明确列为禁用角色。preflight 会同时返回 `input_contract` 和按角色排列的 `input_documents`，即使 PTR 仍被阻止创建 Run。
- Report 声明范围只从第三页表格的“检验项目”字段提取证据；不能从文件名、样本号或 PTR 内容反推范围。
- PTR 正文只提取带点号层级的条款候选、引用候选和父子条款候选。候选均带 `role`、物理页、bbox、原文和来源 SHA-256。
- 当前草案不解析手写内容，不把 OCR 近似文本当作事实，不展开被引用表格，也不生成自动一致/不一致结论。

## 当前已落地的范围解析

`mvp/ptr_scope.py` 已把第三页“检验项目”单元格转换成版本化 `ScopeSet` 草案，保留：

- 表格单元格的页码、bbox、SHA-256 和提取方式；
- 普通条款、数字区间和 `a)～e)` 这类字母区间的展开结果；
- “除……”文本/条款排除项；
- “仅检 GB/YY 标准中某些条款”选择器及其所作用的条款；
- 无法解析时的 `manual` 和原因列表。

当前草案入口和固定夹具只产生不可发布的候选结果，不修改能力目录。0596、1539、2795 的黄金范围夹具位于 `tests/fixtures/ptr_scope/`；复杂的“全项目”表达仍保留为人工复核。

## 规则对象

| 对象 | 草案行为 | 未确定时 | 自动通过 |
|---|---|---|---|
| 声明范围 | 记录 Report“检验项目”候选及证据 | `not_evaluated / PTR_NOT_VALIDATED` | 禁止 |
| 引用表格/附录/标准 | 记录“见/参见/依据/符合”等原文和 token 候选 | `manual_required` | 禁止 |
| 父子条款 | 按观察到的点号层级生成候选边；父条款未出现时保留空目标 | `manual_required` | 禁止 |
| 声明范围内缺项 | 预留覆盖比较对象和证据位置 | `not_evaluated / PTR_NOT_VALIDATED` | 禁止 |
| 范围外条款 | 预留越界对象和证据位置 | `not_evaluated / PTR_NOT_VALIDATED` | 禁止 |

## Finding 契约样例

`mvp.run_ptr_report` 已实现正式 Finding/WorkerResult 形状，并受 `report_ptr` 能力开关保护。草案只带一个不可发布的模板，字段冻结为：

```json
{
  "schema_version": "ptr-p01-finding-0.1",
  "id": "PTR-P01-DRAFT-REVIEW-001",
  "rule_id": "PTR-P01",
  "rule_version": "0.1.0-draft",
  "status": "manual",
  "reason_code": "PTR_NOT_VALIDATED",
  "scope": {"scope_id": "...", "kind": "declaration_scope_and_coverage"},
  "details": {
    "decision": "not_evaluated",
    "observations": [],
    "normalized_values": [],
    "expected": null,
    "comparison": null,
    "review_required": true
  },
  "evidence_locations": [
    {
      "role": "report|ptr",
      "pdf_page": 1,
      "bbox": [0, 0, 1, 1],
      "source_sha256": "...",
      "semantic_role": "report_declared_scope|clause_candidate|reference_candidate",
      "text": "原文"
    }
  ],
  "contributes_to_overall": false
}
```

实际草案结果的 `findings` 始终为空，`rule_execution.state=unsupported`，`finding_count=0`，`publishable=false`。这保证草案不能被状态 API 当成空通过 Run 发布。

草案结果还带有 `contract`、`input_contract` 和 `result_contract` 三个快照。Finding 模板同时保留旧原型使用的 `status/evidence_locations`，并补齐正式结果所需的 `machine_status`、`observations`、`normalized_values`、`expected`、`comparison`、`evidence_ids` 和 `review_status`。Evidence 在草案阶段使用确定性的 `draft:<role>:<sha256前16位>` 文档引用，并显式保存 `page_index/page_number`、`pdf_points`、原始哈希和文字锚点；发布态必须替换为真实 Document ID。

## 三组结构观察

机器只做候选提取，未做语义判断：

| 样本 | Report / PTR 页数 | 报告编号 | Report 范围候选 | PTR 条款候选 | 引用候选 | 父子候选边（人工边） |
|---|---:|---|---:|---:|---:|---:|
| 0596 | 37 / 39 | QW2026-596 | 1 | 594 | 439 | 329（1） |
| 1539 | 110 / 26 | QW2025-1539 | 1 | 220 | 1085 | 492（181） |
| 2795 | 120 / 40 | QW2025-2795 | 1 | 247 | 1101 | 634（185） |

这些数量是候选观察数量，不是规则通过数量。逐样本 JSON、证据坐标和输入哈希见 `output/ptr-p01-drafts-20261011/`。

## 实现与验收门槛

草案提取入口为 `mvp/ptr_p01.py`，输入/结果合同入口为 `mvp/ptr_report_contract.py`，测试为 `tests/test_ptr_p01.py`。提取器仍不接入 `mvp.run_modes` 或 Worker；正式目录和 Runner 负责可发布结果，草案入口只允许生成不可发布草案。

正式启用已经完成；后续新增版式或规则版本仍需完成：

1. 选择至少 2–3 份结构显著不同的 PTR，逐条人工确认声明范围、父子条款和每个引用表格/附录的展开结果。
2. 冻结 Report 范围与 PTR RequirementGraph 的匹配键、缺项定义、范围外定义和循环引用处理。
3. 为人工确认过的缺项、额外条款、父条款缺失、中间条款缺失和引用循环建立正反测试夹具。
4. 为正式 Runner 增加有版本的 `PTR-P01`、Evidence/Artifact 清单、WorkerResult 发布校验和 UI 双文档定位测试。
5. 重新执行 preflight/create/worker/publish 全链路，并在通过后更新正式规则版本和签字记录。

草案候选观察仍不能写成正式一致/不一致结论；正式 Runner 的结果必须经过规则、证据和发布闸门校验。
