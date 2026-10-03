# 报告核对工具架构设计

> 文档状态：设计基线（尚未实现）  
> 适用范围：本机单用户 HTML 工具第一阶段  
> 公开领域名词：`Case`，界面显示为“核对任务”  
> API 前缀：`/api/v1`

## 1. 目标与完成边界

本项目从零设计，不继承旧项目的架构、代码、接口、数据模型、阈值或测试。当前 MVP 中已经由真实样本独立验证的算法事实，可以在建立回归基线后通过适配层逐项迁移；迁移不得同时改变规则语义。

系统目标是把 PDF 中可追溯的观察事实转化为确定性的检查结论，并在自动提取不可靠时，把最小必要证据交给人工复核。系统本身不修改原始 PDF，不使用 Report 的目标结果反推 PTR 或 Record 的模糊内容，也不让 OCR、分类模型或人工备注直接覆盖自动判定。

第一阶段的完成边界：

- 仅在本机运行并监听 `127.0.0.1`；
- 原始 PDF 只读并按内容哈希存储；
- 四种模式独立创建、独立运行、独立保存；
- 四种模式的 Finding 完全隔离；只有 `report_self` 执行 REPORT-* 自检，比对模式仍解析并显示 Report 作为比较输入，但不执行、注入或聚合 REPORT-* Finding；
- Report 自检、Report + GB 9706.1 Record、Report + GB 9706.202 Record 可逐项迁移已验证能力；
- Report + PTR 在规则、样本与验收基线完成前保持 capability disabled，不能返回空的“通过”；
- 正式检查运行时不调用 LLM、Agent 或远程推理服务；本地 OCR/专用分类器只产生观察候选；
- 自动状态、人工复核状态和运行生命周期永久分离；
- 不引入账号、云服务、Redis、Celery、PostgreSQL、Docker或微服务。

第一阶段不承诺任意机构或任意版式 PDF 的通用解析，不开发手写中文“符合／不符合／不适用”识别器，也不把人工复核包装成自动通过。

## 2. 关键设计决策

### 2.1 采用单体应用与隔离 Worker

```text
浏览器
React + TypeScript + 本地 PDF.js
        │ 同源 HTTP / SSE
        ▼
FastAPI 单体应用（Uvicorn 单 worker，仅 127.0.0.1）
 ├─ API、静态前端、输入预检、CSRF
 ├─ SQLite：状态、索引、事件和审计记录
 ├─ 本地不可变 Blob / Artifact Store
 └─ Job Coordinator：并发数 1、唯一数据库写入者
        │ 启动隔离 Python 子进程；JSONL 单向回传
        ▼
确定性检查 Worker
 ├─ 原生 PDF 文本、矢量表格和 Ink
 ├─ Apple Vision / Tesseract 局部 OCR
 ├─ 模板定位、结构化提取和规则计算
 └─ 临时证据、候选结果和进度事件
```

采用单体而非分布式系统的原因是：工作负载为单机、单用户、包含本机 OCR 和 PDF 处理，分布式队列不会增加业务正确性，反而会引入部署、恢复和数据一致性成本。

PDF 处理放在独立子进程中，因为底层解析库、OCR 或单个异常文件可能崩溃、超时或持续占用内存。API 进程不能在请求线程中直接运行检查器，也不能直接把当前含硬编码样本和输出清理行为的 `run_sample()` 暴露为 HTTP 接口。

### 2.2 唯一写入者

FastAPI 父进程中的 Coordinator 是 SQLite 的唯一业务写入者。Worker：

- 只读指定的不可变输入 Blob；
- 只写自己的 `work/` 临时目录；
- 通过 stdout JSONL 发送结构化事件；
- 不持有数据库连接；
- 不写其他 Run 的目录；
- 不直接发布最终制品。

父进程只有在 Worker 输出通过 JSON Schema、路径、哈希和证据完整性校验后，才把文件原子移动至发布目录，并在同一数据库事务中提交 Run、Finding、Evidence、Artifact 与 RunEvent 索引。

### 2.3 三条独立状态轴

运行是否完成、机器发现了什么、人工是否完成复核是三个不同问题，不得压缩为一个“最终状态”。

```text
Run.lifecycle_status
queued ──cancel──────────→ cancelled
  └─dispatch→ running ──publish─────→ succeeded
               ├─technical failure─→ failed
               └─cancel request────→ cancel_requested ──safe stop→ cancelled

启动恢复发现孤立的 running / cancel_requested → interrupted

Finding.machine_status / Run.machine_overall_status
pass | warning | manual | error

Finding.review_status
not_required | pending | in_progress | resolved
```

`lifecycle_status=succeeded` 与 `machine_overall_status=error` 可以同时存在：前者表示程序按设计执行完成，后者表示发现了确定性内容错误。技术异常使用 `failed` 或 `interrupted` 表达，不能伪装成业务 `error`，也不能把业务错误导致的成功运行标成 `failed`。

人工复核后的衍生字段固定为 `resolved_status`。它由人工提交的观察事实和同一套确定性规则重新计算，不允许客户端直接提交，也不设置含糊的状态别名。

### 2.4 证据优先与保守降级

每个非 `pass` Finding 必须有可定位证据或明确的“预期出现区域”。自动结论仅在输入身份、模板、字段归属、提取结果和规则计算均可靠时产生；任何一环不确定时降级为 `manual`。

证据定位分三级：

1. 精确：页内矩形或表格单元格；
2. 区域：已知应出现的页面区域，但无法可靠锁定文字；
3. 页面：只能可靠定位到页面。

不得为了视觉效果伪造矩形。

## 3. 四种独立运行模式

| API mode | 界面名称 | 必需输入 | 固定检查组成 | 第一阶段能力 |
|---|---|---|---|---|
| `report_self` | Report 自检 | Report | Report 解析 + REPORT-* 自检规则 | enabled |
| `report_ptr` | Report + PTR 检查 | Report、PTR | Report/PTR 解析 + PTR 模式规则；不运行 REPORT-* | disabled |
| `report_record_9706_1` | Report + 9706.1 Record 检查 | Report、9706.1 Record | Report/Record 解析 + 9706.1 模式规则；不运行 REPORT-* | enabled |
| `report_record_9706_202` | Report + 9706.202 Record 检查 | Report、9706.202 Record | Report/Record 解析 + 9706.202 模式规则；不运行 REPORT-* | enabled |

Report 解析结果可以按输入哈希和解析器版本复用，但 Finding、RuleExecution、计数、机器总体状态、复核后总体状态和导出均严格属于单个 mode。比对 Finding 可以引用 Report 与 PTR/Record 双方证据；这不等于执行 Report 自检。四种模式间不自动联动，不提供“一键全部检查”。

PTR capability disabled 时：

- 前端显示“尚未启用”并阻止提交；
- 后端仍必须再次拒绝创建 Run，并返回 `409 MODE_DISABLED`；
- 不创建 queued Run；
- 不生成空 Findings；
- 不允许总体状态为 `pass`。

## 4. 领域模型

### 4.1 实体关系

```text
Case 1 ── * Document * ── 1 Blob
Case 1 ── * Run 1 ── * RunInput * ── 1 Document
Run  1 ── * RuleExecution
Run  1 ── * Finding * ── * Evidence
Run  1 ── * Artifact
Run  1 ── * RunEvent
Document 1 ── * DocumentExtraction
Finding 1 ── * ReviewAction
```

### 4.2 `Case`（核对任务）

保存一个用户工作单元的名称、说明、创建时间和最近活动时间。Case 是组织容器，不代表某次运行，也不拥有可变的“当前文件”；每次 Run 都固定引用确切 Document。

### 4.3 `Blob`

Blob 是按 SHA-256 标识的不可变字节对象，至少包含：

- `id`；
- `sha256`；
- `size_bytes`；
- 服务端检测到的媒体类型；
- 相对存储路径；
- 创建时间。

相同 PDF 可复用同一 Blob，但 Document 身份仍分开，以保留上传时角色、原始文件名和所属 Case。Blob 文件一经发布不得原位修改。

### 4.4 `Document`

Document 是 Case 内对 Blob 的一次有角色引用。角色固定为：

- `report`；
- `ptr`；
- `record_9706_1`；
- `record_9706_202`。

Document 记录原始文件名、页数、PDF 版本、加密状态、预检结果和创建时间。角色不可原位更改；角色选错时创建新的 Document 引用，旧记录保留用于审计或显式归档。

### 4.5 `Run` 与 `RunInput`

Run 是一次不可变输入、规则版本和执行环境的快照。核心字段：

- `mode`；
- `lifecycle_status`；
- `machine_overall_status`；
- `rule_bundle_id` 与规则清单哈希；
- `engine_version`；
- `component_versions`：PDF 解析器、模板包、OCR 后端和 Evidence renderer 的版本与配置哈希；
- `created_at`、`started_at`、`finished_at`；
- `cancel_requested_at`；
- `parent_run_id`（重跑来源）；
- `failure_code` 与已脱敏诊断；
- 当前阶段和可解释进度。

Run 还保存每条计划规则的 `RuleExecution`：`rule_id`、`rule_version`、执行状态、起止时间、Finding IDs、降级或失败原因。执行状态为 `pending | running | succeeded | not_applicable | unsupported | failed`。第一版本不设“可选但失败也可发布”的计划规则：任何 `failed`、终态时仍为 `pending/running` 或无解释地缺失都会使整个 Run 技术失败；只有带稳定原因码的 `not_applicable` / `unsupported` 可以不生成 Finding。`manual` 是 Finding 的业务状态，不是规则执行失败。这样规则因程序遗漏而没有产生 Finding 时，会在发布校验阶段失败，而不会从结果中静默消失。

Run 的 `component_versions` 必须与已接受 preflight 中 `required_components` 的稳定 ID 集合完全相等，不能只保存代表性组件。该集合由所有准备执行规则的 `required_component_ids` 精确去重得到；每项冻结实际版本和配置哈希，一并进入 `plan_hash`。比较与散列前按 component ID 做规范排序，API 数组展示顺序本身没有语义。规则目录、preflight 与 Run 快照三处集合不闭合时拒绝创建 Run。

`RuleExecution` 不是 Run JSON 中的临时字段，而是 SQLite 中的独立持久化账本。每行至少保存 `run_id`、`plan_order`、`rule_id`、`rule_version`、`state`、`started_at`、`finished_at`、`reason_code` 和已发布 Finding 关联；`UNIQUE(run_id, plan_order)` 与 `UNIQUE(run_id, rule_id)` 防止重复计划。Finding 通过外键指向其 RuleExecution，候选 Finding ID 只能在发布事务成功后关联，运行中不暴露 Worker 半成品。

Run 维护唯一的单调递增 `review_revision`。Run 内任一有效 ReviewAction 追加或撤销都会产生新的 revision；导出和 `resolved_overall_status` 都必须声明基于哪个 Run 级 revision，不能把 Finding 局部版本与整个 Run 的复核快照混为一谈。

RunInput 保存角色、Document、Blob 哈希和输入顺序。Run 创建后不能替换输入；重新上传或改变规则必须创建新 Run。Run 另保存可索引的 `report_number_raw/report_number_search`：只在 Report 身份提取达到规则可靠性门槛后写入原文及确定性搜索键；不可靠时保持 `null`，不用 OCR 猜测值建索引。文件名搜索来自关联 Document 的 `original_filename`，不现场扫描 PDF 正文。

### 4.6 `Finding`

Finding 是一个规则作用于明确范围后的结构化结果，不能只保存一段摘要。至少保存：

- `rule_id`、`rule_version`、`reason_code`；
- `display_sequence`：Run 发布时按固定排序一次性分配，用于界面稳定显示 `Q1、Q2……`；
- `scope`：项目、条款、字段、页或文档范围；
- `machine_status`；
- 原始观察 `observations`；
- 规范化但不覆盖原文的 `normalized_values`；
- `expected`；
- 逐步 `comparison`；
- 使用的算法、阈值或单位换算版本；
- `evidence_ids`；
- `review_status`、当前 `review_resolution`；
- 后端派生的 `resolved_status`；
- 是否计入总体状态。

保留原始字符串、运算符、单位、精度和格式。任何规范化都必须作为单独字段可见，不能把原值抹去。

一个 Finding 只有一个 `machine_status`。同一业务项目同时出现“必须提示的事实”和“确定性聚合错误”时应生成两个有父子/同 scope 关联的 Finding；例如 R07 的“不符合要求”提示使用 `contributes_to_overall=true` 的 warning Finding，结论聚合不一致另用 error Finding，二者分别计数，不能让 error 覆盖 warning。当 Run 只有该 warning 而无 error/manual 时总体为 `warning`；同时有聚合 error 时仍保留两条 Finding，总体按严重度为 `error`。

一个 Finding 还必须对应一个可单独解释和定位的 scope。同一规则命中多个相互独立的项目、字段或缺失点时分别发布 Finding/Q；旧 MVP 中按规则汇总的一条 Finding 可以在迁移时映射成多个正式 Finding，但必须保留到原 28 条语义基线的映射并重新冻结物化计数。

`display_sequence` 在发布事务中按 `error → manual → warning → pass`、ModePlan 规则顺序、scope 稳定顺序分配，之后不可因筛选、分页或 ReviewAction 改写。它只是本 Run 内的显示定位，不替代 Finding UUID，也不跨 Run 保证相同编号。

### 4.7 `Evidence`

Evidence 必须显式标识来源，不允许前端通过文件名猜测来自 Report、PTR 还是 Record：

```text
document_id
role
source_sha256
page_index         # 0-based，供程序稳定索引
page_number        # 1-based，供界面显示
bbox               # [x0, y0, x1, y1]，可为空
coordinate_space   # 固定为 pdf_points
page_width
page_height
rotation
precision          # exact | region | page
artifact_id
artifact_sha256
extraction_method
text_anchor
evidence_group_id  # 一个 Q 内关联多页/多文档证据
sequence           # 组内顺序
semantic_role      # source_observation | comparison_target | context | expected_missing_region
```

`pdf_points` 的规范是：以页面应用 CropBox 和 Rotation 后的可见页面左上角为原点，单位为 1/72 英寸，x 向右、y 向下；`page_width` 与 `page_height` 必须对应同一可见坐标系。前端使用 PDF.js viewport 做显式变换。若提取器使用其他原始坐标系，Worker 必须在输出前转换并保存原始变换元数据。

`evidence_group_id` 与组内顺序使一个 Q 的跨页、双文档和多对多证据能够成组导航；前端不得靠文件名或数组偶然顺序配对。证据截图是派生制品和可选放大辅助，不能替代原始 Document 定位或完整 PDF。截图哈希、裁剪参数和生成器版本必须保存。内部 `ArtifactRecord` 持有受控的 `storage_relpath`，只供发布校验和文件服务使用；公开 `EvidenceResponse` 只返回 `artifact_id`、哈希、媒体类型和尺寸，不返回任何本机路径。

### 4.8 `DocumentExtraction`

可复用的文档级派生结果，如原生文字、表格几何、Ink 轨迹、页面图像和 OCR 候选，以 `(blob_sha256, extractor_bundle_hash, template_id, template_version, extraction_kind)` 唯一标识。五个键列均为 `NOT NULL`；与模板无关的提取使用保留哨兵 `template_id="__none__"`、`template_version="__none__"`，不能使用 SQLite `NULL` 绕过唯一约束。它只能缓存观察事实，不缓存某个 Run 的业务结论。提取器、模板或配置变化时生成新记录，绝不覆盖旧缓存。

### 4.9 `ReviewAction`

人工复核是追加式审计日志，不修改 Finding 的机器读取和 `machine_status`。允许的动作只描述用户对源文件的观察：

- 确认一个既有候选；
- 录入在源文件中看到的值及单位；
- 标记源文件无法辨认；
- 撤销自己先前的复核动作。

ReviewAction 只接受 `machine_status=manual` 的 Finding；确定性 `pass/warning/error` 不进入复核队列，也不接受普通备注。若未来需要注释，应设计不影响状态的独立 Annotation，而不是扩展 ReviewAction。

不提供“把错误改成通过”“忽略规则”或直接提交 `resolved_status` 的动作。每条动作保存 `finding_version`、完整 `run_input_hashes`、`base_run_review_revision`、新生成的 `run_review_revision`、本机操作者标识、时间、旧候选和新观察。撤销通过追加新动作实现，不删除历史动作。

### 4.10 `Artifact`

Artifact 是一次 Run 或一次可复用提取产生的不可变文件，例如证据裁图、完整 Finding JSON、导出快照或页面缓存。它保存 `kind`、所属实体、媒体类型、字节数、SHA-256、相对路径、生成器版本和创建时间。Artifact API 不返回真实磁盘路径；内容只能由 ID 经数据库解析。任何内容变化都必须创建新 Artifact，不能更新原哈希。

### 4.11 `RunEvent`

RunEvent 保存 `run_id` 内单调递增的 `sequence`、事件类型、经 Schema 校验的有界负载和发生时间。状态变化、阶段、进度、Finding 发布和 Artifact 发布为持久事件；连接 heartbeat 不持久化。RunEvent 是 UI 恢复和审计顺序的依据，但不能取代 Run/Finding 的当前快照。

## 5. 状态计算

### 5.1 Finding 机器状态

| 值 | 含义 |
|---|---|
| `pass` | 已可靠提取并满足规则 |
| `warning` | 已可靠提取，存在必须展示但不构成规则错误的情况 |
| `manual` | 证据存在但不足以自动定案，或模板/归属/识别不可靠 |
| `error` | 已可靠提取并确认不满足规则 |

不适用项不伪造 `pass` Finding；其覆盖信息记录在 Run scope 中。未执行的规则不能参与总体聚合。

### 5.2 Run 总体机器状态

仅在 `lifecycle_status=succeeded` 后计算。严重度顺序：

```text
error > manual > warning > pass
```

聚合只使用 `contributes_to_overall=true` 的 Finding。若任一 planned rule 没有得到 Finding 或带稳定原因码的 `not_applicable/unsupported` 覆盖记录，发布校验失败，Run 进入 `failed`，不能因空集合变成 `pass`。Preflight 中 `coverage.ready=0` 时必须返回 `can_create_run=false` 与 `NO_EXECUTABLE_RULES`；若运行期所有规则最终均为 `not_applicable/unsupported` 而没有任何 Finding，也不发布 `succeeded`，而是以同一原因码进入 `failed`、`machine_overall_status=null`。

### 5.3 人工复核与 `resolved_status`

- `machine_status != manual`：默认 `review_status=not_required`，`resolved_status=machine_status`；人工备注不改变它。
- `machine_status = manual` 且尚无有效观察：`review_status=pending`，`resolved_status=manual`。
- 已开始但未提交完整观察：`review_status=in_progress`，`resolved_status=manual`。
- 提交完整观察后，后端按同一规则重算：`review_status=resolved`，`review_resolution` 为 `consistent | inconsistent | indeterminate`，`resolved_status` 为重算结果。
- 若用户标记“源文件无法辨认”，复核可以结束，`review_resolution=indeterminate`，但 `resolved_status` 仍为 `manual`。
- 输入 Blob 哈希变化时不能继承旧复核；新 Document 和新 Run 重新进入复核流程。

`review_revision=0` 时 Run 的 `resolved_overall_status`、对应 revision 和计算时间均为 `null`；此时机器总体状态已经完整表达结果，不制造一份重复的“复核后”状态。首次产生有效 ReviewAction 后，后端在同一个 Run review revision 上，使用 `error > manual > warning > pass` 聚合所有 `contributes_to_overall=true` Finding 的 `resolved_status`，保存 `resolved_overall_status`、`resolved_overall_review_revision` 与 `resolved_overall_computed_at`。三者必须与 `machine_overall_status` 并列展示；任何读取和导出都不得把不同 revision 的 Finding 拼接成一个总体快照。

## 6. 文件与数据库布局

建议应用数据根目录与源码分开，可由配置显式指定。所有数据库路径均保存相对路径：

```text
app-data/
├─ db/
│  └─ checker.sqlite3
├─ blobs/
│  └─ sha256/ab/cd/<full-sha256>.pdf
├─ runs/<run-id>/
│  ├─ work/                 # 未发布，崩溃后可清理
│  ├─ published/
│  │  ├─ manifest.json
│  │  ├─ findings.json
│  │  └─ evidence/...
│  └─ logs/worker.jsonl
├─ extractions/<blob-sha>/<extractor-hash>/...
├─ quarantine/              # 上传预检期间
└─ backups/
```

SQLite 使用 WAL、外键和 busy timeout，但单进程仍是唯一业务写入者。主要表：

- `cases`；
- `blobs`、`documents`；
- `document_extractions`；
- `runs`、`run_inputs`、`rule_executions`；
- `findings`、`evidence`、`artifacts`；
- `review_actions`、`review_snapshots`；
- `run_events`；
- `idempotency_records`；
- `schema_migrations`。

历史列表使用预计算的 `runs.report_number_search` 和 `documents.original_filename_search`，并对 mode、lifecycle/machine overall、`created_at` 建组合索引。搜索请求不允许临时扫描 PDF、OCR 全文或 Artifact。

大文件和截图不存 SQLite BLOB，只存哈希、大小、媒体类型和相对路径。数据库事务提交前必须保证发布文件已经以临时名写完、`fsync` 并原子 rename；若数据库提交失败，孤立文件由启动时审计回收，不视为已发布。

## 7. 运行数据流

### 7.1 上传与预检

```text
multipart 流式上传
→ quarantine 中的随机临时文件
→ 计算 SHA-256 / 大小 / magic bytes
→ PDF 解析、加密检查、页数和结构限额
→ 角色与文件类型预检
→ 原子发布 Blob
→ 创建 Document
```

文件名只作显示用途，不能拼接成本地路径。加密、损坏、非 PDF、超过配置限额或零页文件在创建 Document 前拒绝。失败临时文件安全清理；原始上传永不被 OCR 或修复工具原位写入。

### 7.2 创建与执行 Run

```text
校验 capability、模式、输入角色和文件身份
→ 事务创建 queued Run / RunInput / 初始事件
→ Coordinator 按 FIFO 取一个 Run
→ 生成只读输入清单和专属 work 目录
→ 启动 Worker 子进程
→ 校验并持久化 JSONL 事件
→ Worker 写候选 manifest
→ Schema、哈希、路径、规则覆盖与证据完整性校验
→ 原子发布 Artifact
→ 单事务写入 Finding / Evidence / Artifact / 终态事件
→ lifecycle_status=succeeded 并计算 machine_overall_status
```

Worker 事件不得包含可执行指令。父进程只接受白名单事件类型、相对路径和有界字段长度；未知字段按 Schema 策略拒绝或忽略，不能执行。

Worker 在运行中产生的 Finding 候选属于内部 work 数据，不通过公共 SSE 暴露。公共 `finding_emitted` 事件只在对应 Finding 和 Evidence 已通过发布校验并提交数据库后产生，因此客户端不会收到指向 404 或半成品证据的 Finding ID。

### 7.3 提取与检查流水线

每个模式的处理顺序固定为：

1. 文件身份与支持模板判定；
2. 原生文字、矢量表格、Ink 与页面几何提取；
3. 已知模板配准和单元格归属；
4. 仅对必要 ROI 做本地 OCR/小模型识别；
5. 保存互相独立的候选和置信依据；
6. 结构化原始观察；
7. 规范化与单位换算，但保留原值；
8. 确定性规则计算；
9. 生成 Finding 与完整证据链；
10. 对不可靠环节生成 `manual`，不猜测结果。

9706.1 手写数值复核遵守“先录后比”：普通结果页可浏览完整双方文档；进入源观察步骤后，当前工作台只展示完整 Record、裁剪和候选，临时锁定完整 Report 窗格与目标导航；二次确认后才恢复完整 Report，并显示目标页、聚合、换算与比较结果。这样降低目标值诱导源文件读取；由于同一 Case 的文件页仍可打开 Report，这不是安全隔离。

### 7.4 取消

- queued Run 可立即变为 `cancelled`；
- running Run 先变为 `cancel_requested`，Coordinator 向进程发送温和终止信号；
- Worker 在当前安全点退出；超时后父进程终止该子进程；
- 未通过发布校验的 work 文件不进入正式 Artifact；
- cancelled Run 保留输入、事件与取消原因，不生成伪结论。

## 8. 前后端边界

前端负责：

- 模式选择、固定角色槽和本机预检提示；
- 展示 capability、只读规则目录、输入相关 Run preflight/Scope Ledger、系统诊断、Run 生命周期和真实阶段；
- 在工作台常驻范围摘要，明确完整覆盖或部分覆盖/未覆盖；详细 Scope Ledger 可按需展开，但不得把覆盖边界整体隐藏；
- 使用本地 PDF.js 读取并浏览完整原始 Document；Report 自检使用完整 Report，对比模式使用完整 Report + PTR/Record 双查看器；
- PDF.js viewer 顶部同时区分“当前实际浏览页”和“当前 Q 定位页”；实际页必须由 viewer 的页变更事件持续同步，不能在用户自由翻页后继续把旧定位页标成当前页；
- 通过 HTTP Range 按需读取，只渲染当前视区及有界邻近页，取消过期页面任务并释放远端 canvas/text layer；完整浏览权不得被误实现为全文档一次性常驻渲染；
- 按 `全部 / error / manual / warning / pass` 选项卡筛选稳定 Q 列表，并按 Evidence group 完成跳页、坐标变换、高亮和上一处/下一处导航；筛选只影响 Q 列表，不能自动替换当前 Q 或重建查看器。非空筛选不含当前 Q 时保留上下文并显示提示；计数为 0 时隐藏 Q 定位和判定详情但仍保留完整文档查看器，不得将空 Finding 误表示为文档不可见；
- 以双文档为默认主画面，检查项和判定/复核作为按需覆盖式抽屉；抽屉开合、任务信息展开和窄屏文档页签切换不得改变 viewer identity，也不得丢失任一文档的页码、滚动、缩放、旋转、当前 Q 或高亮状态；
- 显示原始观察、规范化、比较过程和状态；
- 收集受 Schema 限制的人工观察；
- SSE 断线重连与页面刷新后的快照恢复。

后端负责并拥有最终解释权：

- 文件真实性、加密和限额校验；
- 模式与输入角色校验；
- 根据 mode、Document 哈希、规则包与组件状态生成带哈希的 Run preflight 计划，并在创建 Run 时重新验证；
- 规则目录、组件诊断、规则选择、状态计算和总体聚合；
- 坐标与证据完整性校验；
- 复核动作授权、版本冲突和确定性重算；
- Artifact 哈希、路径和访问控制；
- 恢复、取消和重跑语义。

前端不得用 Evidence 裁剪替代完整 PDF，不得硬编码规则版本、未覆盖范围或 OCR/解析器可用性，也不得自行计算 `machine_status`、`resolved_status` 或单位比较结果；不得因 SSE 断开把 Run 视为失败，也不得用客户端文件名判定角色。Run 创建仍由后端权威重算 preflight；页面展示过期计划时不能继续提交。

## 9. SSE 与进度

所有对用户有意义的运行事件先持久化再发送，并为每个 Run 分配单调递增 `sequence`。SSE `id` 使用该 sequence，支持 `Last-Event-ID` 补发。

事件包括：

- `snapshot`；
- `run_state_changed`；
- `phase_started`；
- `phase_progress`；
- `finding_emitted`；
- `artifact_published`；
- `heartbeat`。

断开 SSE 不取消任务。浏览器重连或刷新时先读取 Run 快照，再从最后 sequence 继续。只有存在可证明的工作总量时才显示百分比；否则显示当前阶段、已完成数量和不确定总量，不能伪造 0–100% 进度。

## 10. 失败、恢复与重跑

### 10.1 失败分类

- 输入失败：上传阶段拒绝，不创建 Document；
- 能力或角色失败：拒绝创建 Run；
- Worker 技术失败：Run `failed`，保留诊断和已发布前事件；
- 进程/主机中断：启动恢复时将遗留 `running` / `cancel_requested` 标为 `interrupted`；
- 业务不一致：Run `succeeded`，机器状态 `error`；
- 不确定识别：Run `succeeded`，机器状态 `manual`。

### 10.2 启动恢复

启动时按以下顺序执行：

1. SQLite 完整性、Schema 版本和应用数据根路径检查；
2. 将无活动进程对应的 `running` / `cancel_requested` Run 标为 `interrupted`；
3. 校验 published manifest 与文件哈希；
4. 隔离哈希不符的 Artifact，并把相关 Run 标为不可导出，不能静默重建历史；
5. 清理没有数据库引用且超过保留期的 quarantine/work 文件；
6. 若发现已经原子 rename 为 `published/`、但没有已提交 Run/Artifact/manifest 数据库引用的目录，先原子移入 `quarantine/orphan-published/` 并记录诊断；绝不自动收养为历史结果，超过保留期后才清理；
7. 恢复合法 queued Run，由 Coordinator 继续调度。

`interrupted`、`failed`、`cancelled` Run 不原位恢复执行。重跑创建新 Run，并通过 `parent_run_id` 关联；用户可选择沿用原规则包或使用当前激活规则包。

### 10.3 数据备份

备份采用 SQLite 在线备份 API 加 Artifact manifest 清单。备份不复制正在写入的临时 work 文件。恢复后必须执行数据库完整性、Blob 哈希和 Artifact manifest 校验，任何缺失项明确标记，不自动伪造。

## 11. 安全与隐私

即使只监听 loopback，也按本地敏感文档工具处理：

- 监听地址硬编码或启动时强校验为 `127.0.0.1`，拒绝意外暴露到 `0.0.0.0`、局域网地址或 IPv6 任意地址；
- 校验 Host / Origin / Fetch Metadata，拒绝非本机 Host 和 DNS rebinding 形式的请求；
- 前后端同源；所有变更请求校验 Origin、SameSite session cookie 和 CSRF token；
- 严格 CSP，不从 CDN 加载脚本、字体、OCR 或 PDF.js；
- 上传按 magic bytes 和解析结果校验，不信任扩展名或浏览器 MIME；
- 服务端生成路径，禁止 `..`、绝对路径、symlink 逃逸和客户端指定输出路径；
- PDF 内容只作为数据渲染，禁用嵌入脚本、自动外链和附件执行；
- Content-Disposition 对原文件名做安全编码；
- 日志默认不记录整段 OCR 文本、姓名、地址和原始 PDF 字节；
- Worker 使用最小环境变量、独立工作目录、资源限制和执行超时；
- Artifact 访问必须通过数据库 ID 解析，并再次验证所属 Case/Run 与实际路径。

本机单用户模式不设置账号系统，但这不等于可以省略 CSRF、路径校验或内容类型校验。

## 12. 性能与资源边界

- Coordinator 并发数固定为 1，避免 OCR 抢占内存并简化恢复；
- 上传、哈希和文件响应均流式处理；
- PDF 原文件支持 HTTP Range，避免每次完整读取；
- 页面图像按 `(blob_sha, page, scale, rotation, renderer_version)` 缓存；
- OCR 只针对必要 ROI，不默认全页 OCR；
- DocumentExtraction 复用必须同时匹配 Blob 和提取器配置哈希；
- API 不在单个列表响应中内嵌大量证据图；使用分页和独立资源端点；
- 每个 Run 的 CPU 时间、内存、输出文件数、单 Artifact 大小和总输出大小均有配置上限。

首期性能验收应使用当前真实长报告测量，不预先承诺未经验证的秒数或吞吐量。

## 13. 可观测性与审计

- 每个 HTTP 请求有 `request_id`；
- 每个 Run 有持久化 event sequence；
- Worker 日志为有界 JSONL，记录阶段、耗时、规则和错误码；
- Finding 保存规则版本、原始观察、转换步骤与证据；
- ReviewAction 追加保存，不做物理删除；
- 软件版本、规则包哈希、OCR/提取器版本进入 Run；
- 导出物带 Schema 版本、生成时间、输入哈希和 review revision。

运行日志用于诊断，Finding/Evidence/ReviewAction 才是业务审计事实。不能仅凭日志重建或改变历史结论。

## 14. MVP 能力迁移策略

当前 MVP 不是目标架构的一部分，也不能直接包成 HTTP 服务。迁移遵循：

1. 冻结四份真实样本的输入哈希、28 条已审核唯一语义断言和证据；
2. 定义新 Finding/Evidence Schema 与规则版本；
3. 为一个规则编写纯输入/输出适配器；
4. 在独立 Worker 中接入该规则；
5. 对四样本比较业务结论、原始观察和证据定位；
6. 等价后再迁移下一规则；
7. 任何规则语义变化单独评审并生成新 rule version。

正式独立模式回归为 10 个 Run：4 个 Report 自检、3 个 Report + 9706.1、3 个 Report + 9706.202。按当前未拆分规则粒度，Report 自检 16 条、9706.1 对比 6 条、9706.202 对比 6 条，共 28 条 Run Finding，与原 MVP 的 28 条唯一语义基线逐项对应。R09/R10 或聚合异常若为精确定位而拆分，必须先冻结新计数再更新门槛。

禁止在同一提交中同时搬运大量解析代码、修改业务规则并替换验收预期，因为这会失去差异来源。

## 15. 必须验证的架构不变量

1. 原始 Blob 的 SHA-256 在上传后永不变化；
2. Worker 无数据库写权限；
3. 任一 Run 只能读取自己的 RunInput；
4. 未通过发布校验的结果不会出现在 Findings API；
5. PTR disabled 时不能创建 Run 或得到 pass；
6. 每个 Run 只发布其 mode 允许的 Finding；`report_self` 不含 RECORD/PTR Finding，比对模式不含 REPORT-* 自检 Finding；
7. `succeeded + error` 可正常保存和展示；
8. 技术失败不生成业务 pass/error；
9. ReviewAction 不修改机器原值和 `machine_status`；
10. `resolved_status` 只能由后端规则重算；
11. Evidence 可由 `document_id + page_index + bbox` 重现定位；
12. SSE 重连不丢持久事件，也不重复改变状态；
13. 幂等重试不会重复创建 Case、Document、Run 或 ReviewAction；
14. 文件名、PDF 内容或 Worker 输出不能造成路径逃逸；
15. 规则缺失或 Finding 空集合不能聚合为 pass。

满足这些不变量后，才可以进入完整前端联调；其中任何一项尚未验证，都必须在交付状态中标为未验证。
