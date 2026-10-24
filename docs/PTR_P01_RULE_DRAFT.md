# PTR-P01 规则冻结草案（禁用态）

日期：2026-10-11

本文只冻结审计契约和待人工验收的候选结构，不把 PTR↔Report 草案当作已启用规则。`report_ptr`、`report_ptr_report` 仍保持 `enabled=false`，原因码为 `PTR_NOT_VALIDATED`。输入、结果和证据的接口合同由 `mvp/ptr_report_contract.py` 提供；这一步接通的是字段和生命周期边界，不是 PTR 语义验收。

## 输入与范围

- 输入角色固定为 `report` 和 `ptr`；两侧在草案中保存路径、页数、页面几何和 SHA-256。
- 正式 Run 的输入字段固定为 `report_document_id` 与 `ptr_document_id`，一对一、创建后不可替换；`record_document_id` 被明确列为禁用角色。preflight 会同时返回 `input_contract` 和按角色排列的 `input_documents`，即使 PTR 仍被阻止创建 Run。
- Report 声明范围只从“检验项目”字段提取候选证据；不能从文件名、样本号或 PTR 内容反推范围。
- PTR 正文只提取带点号层级的条款候选、引用候选和父子条款候选。候选均带 `role`、物理页、bbox、原文和来源 SHA-256。
- 当前草案不解析手写内容，不把 OCR 近似文本当作事实，不展开被引用表格，也不生成自动一致/不一致结论。

## 规则对象

| 对象 | 草案行为 | 未确定时 | 自动通过 |
|---|---|---|---|
| 声明范围 | 记录 Report“检验项目”候选及证据 | `not_evaluated / PTR_NOT_VALIDATED` | 禁止 |
| 引用表格/附录/标准 | 记录“见/参见/依据/符合”等原文和 token 候选 | `manual_required` | 禁止 |
| 父子条款 | 按观察到的点号层级生成候选边；父条款未出现时保留空目标 | `manual_required` | 禁止 |
| 声明范围内缺项 | 预留覆盖比较对象和证据位置 | `not_evaluated / PTR_NOT_VALIDATED` | 禁止 |
| 范围外条款 | 预留越界对象和证据位置 | `not_evaluated / PTR_NOT_VALIDATED` | 禁止 |

## Finding 契约样例

正式 Runner 尚未发布 Finding。禁用态草案只带一个不可发布的模板，字段冻结为：

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
| 0596 | 37 / 39 | QW2026-596 | 2 | 594 | 439 | 329（1） |
| 1539 | 110 / 26 | QW2025-1539 | 2 | 220 | 1085 | 492（181） |
| 2795 | 120 / 40 | QW2025-2795 | 2 | 247 | 1101 | 634（185） |

这些数量是候选观察数量，不是规则通过数量。逐样本 JSON、证据坐标和输入哈希见 `output/ptr-p01-drafts-20261011/`。

## 实现与验收门槛

草案提取入口为 `mvp/ptr_p01.py`，输入/结果合同入口为 `mvp/ptr_report_contract.py`，测试为 `tests/test_ptr_p01.py`。提取器没有接入 `mvp.run_modes` 或 Worker；能力目录只公开禁用态合同，当前只允许生成禁用态草案。

启用前仍必须完成：

1. 选择至少 2–3 份结构显著不同的 PTR，逐条人工确认声明范围、父子条款和每个引用表格/附录的展开结果。
2. 冻结 Report 范围与 PTR RequirementGraph 的匹配键、缺项定义、范围外定义和循环引用处理。
3. 为人工确认过的缺项、额外条款、父条款缺失、中间条款缺失和引用循环建立正反测试夹具。
4. 为正式 Runner 增加有版本的 `PTR-P01`、Evidence/Artifact 清单、WorkerResult 发布校验和 UI 双文档定位测试。
5. 重新执行 preflight/create/worker/publish 全链路；通过后才可把 capability 切换为 `enabled`。

在上述门槛完成前，不启用按钮、不创建 PTR Run，也不把候选观察写成一致/不一致结论。
