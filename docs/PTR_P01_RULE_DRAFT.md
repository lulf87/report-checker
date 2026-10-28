# PTR-P01 规则冻结草案（非发布产物）

日期：2026-10-11

本文只记录审计契约和候选结构，不把 PTR↔Report 离线草案当作正式结果。当前 `report_ptr` 已按三份试点授权启用；`report_ptr_report` 组合别名仍保持 `enabled=false`，原因码为 `PTR_NOT_VALIDATED`。输入、结果和证据的接口合同由 `mvp/ptr_report_contract.py` 提供，正式结果由 `mvp.run_ptr_report` 生成。

当前另有 `mvp/ptr_compare.py` 作为离线比较层：它执行范围、条款、数值、单位、引用和覆盖层的规则核对，并输出不可发布的 Coverage Ledger；`mvp/ptr_ocr.py` 只提供扫描 PTR 的本机 OCR 观察，`mvp/ptr_llm.py` 只验证未来 LLM 的逐字证据。离线草案不能绕过正式 Runner，也不能由 LLM 单独给出通过结论。

当前 PTR-P01 的唯一规则版本为 `0.5.1-draft`。合同、比较器、能力目录、验收门和结果中的 `rule_version` 均从同一版本源生成；历史结果仍保留其生成时的版本快照。

## 2026-10-27 增量（0.5.1-draft）

布局解析器升级为 `ptr-layout-1.4`。文本严格比对不一致后，比较器会依次尝试下列“安全等价规则”。只有在去掉对应内容后两侧文本**完全相等**时才判一致，并在 `text_comparison.equivalence_rules` 中记录用到的每条规则，原始差异 `text_diff` 照常保留供界面展示：

| 规则 | 去掉的内容 | 限制条件 |
|---|---|---|
| `PTR_RUNNING_HEADER` | PTR 页眉/页脚 | 只认在 ≥40%（且至少 3）页上整行重复出现的行；不写死公司名 |
| `PTR_FIGURE_LABELS` | “图N…”图注及其前面插图内的短标签 | 标签行 ≤20 字、无句末标点、不含“应/须/表N”，遇到要求句即停止 |
| `REPORT_UNIT_LINE` | Report“单位：X”行 | X 必须在 PTR 该条文本中出现 |
| `REPORT_RESULT_LABELS` | Report 结果子行标签（X方向/Y方向、Yaw/Pitch 等） | ≥2 行、每行 ≤10 字、无数字和要求词、成组平行（同前缀/同后缀/纯字母），且位于单元格末尾或紧跟“单位：”行 |
| `PUNCTUATION_ONLY` | 标点、项目符号 | 数字间的“.”“,”保留 |
| `REPORT_REPEATED_TEXT` | Report 重复写的同一要求 | 被去掉的句子必须是前文已出现的内容 |
| `REPORT_EXPANDED_REFERENCE` | Report 展开写出的被引条款原文（如“2.5.7 a)…”） | 展开内容必须与 PTR 对应条款一致 |

同时修正三处抽取问题：“2.85 个周期/°”类数值不再被识别为条款号；Report 中“470066：0°±3°。”这类“型号：要求”行不再被当成型号子行标签丢弃（子行标签改为通用的 5～8 位型号 + 左路/右路）；“表1：…”全角冒号题注可以被识别。

### 表格按行展开核对（`mvp/ptr_table_rows.py`）

起搏器类 PTR 把参数放在一张表里，条款只写“应符合表 N 的要求”；Report 则把对应参数行展开成“设置：…/标称值：…/允差：…”。整表指纹比不上时，比较器先做按行核对：

- 用条款标题（冒号前部分，去掉单位括号和①②脚注）在被引表中找**唯一**一行；Report 展开中必须出现该行的参数名，否则不执行（防止条款号错位时拿错行比较）。
- Report 的“标签：值”中，标签必须是 PTR 表头（或型号列表格中的参数名），值归一化后必须与该行对应单元格**完全相同**；`+20/-25%` 与 `+20%/-25%`、`± 20`（单位在参数名里）与 `±20ms` 视为相同。
- Report 把一个 PTR 单元格拆成多段（如按 240/500/2000 Ω 负载分写）时，各段按 PTR 原文界定，并且必须**按顺序拼回**完整的 PTR 单元格。
- 测试点行（`@240Ω`、`0.25V、0.5V：±0.25 V`）中的数字必须出现在 PTR 条款或表中，或落在该参数的取值范围内；负载电阻只有在 PTR 条款本身写了负载时才要求一致。
- 跨页续表（下一页顶部、列数相同、无题注）继承上一张表的标签，视为同一张表；同名题注出现多次时，优先使用所在章节（祖先条款）内的那张。
- 核对通过后，从 Report 文本中只去掉已核对的展开行及其不含数字的小标题，**剩余文本仍按文本/数值/单位规则比较**，因此展开之外的差异（如 0596 2.1.2 的 1000Ω/2000Ω）会作为硬差异报出。
- 结果写入 `table_comparison.row_expansion.cells`，每个单元格带 `status`、`reason_code`，未一致的单元格附最接近的 PTR 值与逐字差异 `diff`。

### 实体表格按行名比对

PTR 与 Report 都印出表格、但整表指纹不同（列被拆分、多了“关节编号”列、PTR 为无边框文字表）时，按“行名 → 值”比较：

- PTR 每行拆成行名和值（文字表按首个空白拆分；一格多行视为多个子行）；同名行（如表 7、表 8 都有“近红外”）按出现顺序合并。
- Report 每行中找到与 PTR 行名相同的那一格，其后各格为值；同一行名在 Report 中出现多次（如左、右 MTM 分成两张表）按顺序拼接。
- 值归一化后必须相同；仅小数末位零不同（`0.70`/`0.7`）记为 `CELL_NUMERIC_EQUAL`。数字不同为硬差异；PTR 行在 Report 中缺失为硬差异；Report 多出的数据行、PTR 行名不足两个字（竖排合并单元格未拆开）为人工确认。Report 表内的题注和重复表头行不参与比较，但表内题注计入 Report 的表格引用。

### 条款号错位

Report 某条的标题与 PTR 同号条款不同，却与同一父条款下另一条 PTR 的标题相同时，判为 `CLAUSE_NUMBER_SHIFTED`（硬差异），并在 `clause_shift.message` 中写明对应的 PTR 条款号。

此外，归一化补充了数学减号 `−`（U+2212）→ `-`。

## 2026-10-24 离线草案增量

当前离线比较器的执行规则版本为 `ptr-compare-0.5` / `0.5.0-draft`，布局解析器为 `ptr-layout-1.3`。相较本文件最初的候选结构，已补齐以下可复核行为：

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
  "rule_version": "0.5.0-draft",
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
