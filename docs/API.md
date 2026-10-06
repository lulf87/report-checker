# 报告核对工具 HTTP API 设计

> 文档状态：v1 契约与当前本机实现对照  
> 服务范围：本机单用户、同源前后端  
> 基础路径：`/api/v1`（`/healthz` 除外）  
> 公开领域名词：`Case`，界面显示为“核对任务”

当前工程已经实现能力目录、Case/Document/Run 状态、Blob 存储、自动队列 Worker、Finding/Evidence 发布和 ReviewAction 追加：`.venv/bin/python -m mvp.capability_server` 提供 `/healthz`、`/api/v1/capabilities` 和 `/api/v1/rules`；`.venv/bin/python -m mvp.run_state_server` 提供本机 CSRF 会话、Case、Document、Run preflight、Run 快照、规则执行账本、事件、Finding、复核和取消入口；`mvp.run_coordinator` 负责一次 queued Run 的子进程执行与发布闸门。该服务的模式和规则目录来自 `mvp.capabilities`，PTR 保持 `PTR_NOT_VALIDATED` 禁用。

> **当前实现边界（2026-10-05）**：以上代码路由是可运行契约。幂等键、SSE、Artifact 图片、导出、签名和完整分页游标尚未暴露为当前路由；当前事件通过 `GET /api/v1/runs/{run_id}/events` 列表轮询读取，创建接口默认不提供业务幂等保证。下列端点也尚未实现，均属于规划：`GET /api/v1/version`、`GET /api/v1/system/diagnostics`、`GET /api/v1/documents/{document_id}/pages/{page_number}/image`、`POST /api/v1/runs/{run_id}:rerun`、`GET /api/v1/runs`。公开脱敏页 `docs/prototypes/workbench-upload.html` 与本机真实工作台已接入上传、模式选择、preflight、Run 启动和结果轮询。客户端和部署说明应以代码路由为准，规划章节只用于后续设计。

本 API 只暴露结构化、可追溯的事实和状态。服务端拥有模式校验、规则计算、总体聚合、证据路径和人工复核重算的解释权；客户端不能提交机器结论或派生结论。

## 1. 通用约定

### 1.1 传输与媒体类型

- JSON 请求与响应使用 `application/json; charset=utf-8`；
- 文件上传支持 `multipart/form-data`，本机脚本客户端也可使用 JSON `content_base64`；
- PDF 内容使用 `application/pdf` 并支持单区间 HTTP Range；
- SSE 使用 `text/event-stream`（规划；当前实现使用事件列表轮询，不提供 SSE）；
- 证据图使用服务端实际生成的 `image/png` 或 `image/webp`；
- 导出 JSON 使用 `application/json`，导出 HTML 使用 `text/html; charset=utf-8`。

API 不接受客户端提供的本地文件路径、输出目录或 Artifact 相对路径。

### 1.2 ID、时间和枚举

- 所有实体 ID 为服务端生成的不透明 UUID 字符串；
- 时间为 UTC RFC 3339，例如 `2026-09-29T08:30:15.123Z`；
- JSON 字段使用 `snake_case`；
- 枚举值小写；
- `null` 表示尚不存在或不适用，不表示空字符串；
- 页码同时保存 `page_index`（0-based）和 `page_number`（1-based）。

JSON 请求 Schema 默认 `additionalProperties=false`。未知字段返回 `422 REQUEST_VALIDATION_FAILED`；客户端提交已定义的只读字段返回 `422 READ_ONLY_FIELD_SUBMITTED`。两者都不静默忽略。

### 1.3 分页

列表接口统一支持：

```text
limit   1..100，默认 25
cursor  服务端不透明游标，可省略
```

响应：

```json
{
  "items": [],
  "next_cursor": null
}
```

游标与筛选条件绑定。用不同筛选条件重放旧游标返回 `400 INVALID_CURSOR`。

### 1.4 请求追踪

每个响应包含 `X-Request-ID`。客户端可以发送合法的 `X-Request-ID`，服务端也可替换过长或非法值。错误响应体重复返回 `request_id` 便于诊断。

### 1.5 会话与 CSRF（当前实现）

本机模式没有账号登录，所有变更请求须携带启动会话返回的 `X-CSRF-Token`。服务端绑定本机 Host；若请求带有 `Origin` 或 Fetch Metadata，则只接受本机服务或工作台 Origin：

1. `Host` 为 `localhost`、`127.0.0.1` 或 `::1`；
2. `Origin`（若存在）为当前本机服务 Origin，或本项目工作台的 `http://127.0.0.1:8765` / `http://localhost:8765`；
3. `Sec-Fetch-Site`（若存在）为 `same-origin`、`same-site` 或 `none`；
4. 请求头 `X-CSRF-Token` 与当前进程会话匹配。

服务端拒绝非本机 Host 和跨站写请求；工作台从 8765 访问 8767 时使用受限 CORS，响应只允许上述两个本机工作台 Origin，不开放宽泛跨站访问。当前本地脚本可以不发送 `Origin`，但必须发送 CSRF token。`session_id` 仅用于本机审计标识，不构成账号认证。

前端启动时调用：

```http
GET /api/v1/session
```

```json
{
  "csrf_token": "opaque-token",
  "session_id": "opaque-session-id",
  "expires_at": "2026-09-29T20:30:15.123Z"
}
```

`GET` / `HEAD` 不要求 CSRF header。所有 `POST` 要求；失败返回 `403 CSRF_VALIDATION_FAILED`。会话不改变本地单用户边界，也不构成远程访问能力。

### 1.6 幂等（规划）

以下操作的 `Idempotency-Key` 保存与重放语义属于规划，当前实现尚未提供：

- 创建 Case；
- 上传 Document；
- 创建、取消或重跑 Run；
- 追加 ReviewAction。

未来实现可按 `session + method + normalized route + key` 保存请求摘要和响应。当前客户端重试 POST 前应先查询资源，不能假定业务请求幂等。规划规则：

- 相同 key、相同请求摘要：返回第一次成功或确定性失败的相同语义响应，并带 `Idempotency-Replayed: true`；
- 相同 key、不同请求摘要：`409 IDEMPOTENCY_KEY_REUSED`；
- 并发中同一 key 尚未完成：等待有界时间，超时返回 `409 IDEMPOTENCY_REQUEST_IN_PROGRESS`；
- multipart 请求摘要包含角色、Case、文件大小和完整 SHA-256，而不只包含文件名；
- 幂等记录默认保留至少 7 天，保留期是部署配置并在 `/api/v1/version` 中公开。

取消端点本身也是状态幂等：已经处于 `cancel_requested` / `cancelled` 时重复调用不会重复发信号。

### 1.7 乐观并发

可变元数据返回整数 `revision` 和弱 ETag。更新或追加复核时提交当前 revision；过期返回 `409 REVISION_CONFLICT`，响应包含服务端当前 revision。原始 Document、RunInput、机器 Finding 和 Evidence 不允许原位编辑。

## 2. 枚举与公共对象

### 2.1 模式和角色

```text
RunMode:
  report_self
  report_ptr
  report_record_9706_1
  report_record_9706_202

DocumentRole:
  report
  ptr
  record_9706_1
  record_9706_202
```

模式对应的合法输入：

| mode | 必需角色 | 禁止附带角色 | 是否包含 `REPORT-*` 自检 Finding |
|---|---|---|---|
| `report_self` | `report` | 其他全部 | 是 |
| `report_ptr` | `report`, `ptr` | 两种 Record | 否 |
| `report_ptr_report` | `report`, `ptr` | 两种 Record | 禁用 |
| `report_diff` | 尚未冻结 | 全部 | 禁用 |
| `report_record_9706_1` | `report`, `record_9706_1` | PTR、9706.202 | 否 |
| `report_record_9706_202` | `report`, `record_9706_202` | PTR、9706.1 | 否 |

能力目录显式列出四种边界：`report_self` 已启用；`report_ptr` 与 `report_ptr_report` 均以 `enabled=false`、`PTR_NOT_VALIDATED` 禁用；`report_diff` 以 `enabled=false`、`DIFF_NOT_SPECIFIED` 禁用，因为其双输入角色与规则计划尚未冻结。组合模式不能当作 `report_ptr` 别名，差异模式也不能从现有 Record 模式推导；这些请求会在 preflight/create/worker/publish 生命周期按 `MODE_DISABLED` 拒绝。

每个角色恰好一个 Document。创建 Run 时多传、少传、角色不符都拒绝，不自动猜测或选择“最近上传”的文件。
对比模式仍解析并完整显示 Report，也可在对应模式 Finding 中引用 Report 侧比较证据；但不会执行、注入、显示或聚合 `REPORT-*` 自检 Finding。

当前正式比对范围由 [docs/COMPARISON_SCOPE.md](COMPARISON_SCOPE.md) 维护。该矩阵确认 S01–S47 纳入三个启用模式，S48（GB 9706.202 表 3 的文档、页面、原始测量值和重新计算）第一阶段排除。范围纳入不等于每个对象都能自动判定；OCR、手写或映射证据不足时必须产生 `manual`。

当前启用规则计划：`report_self` 为 `REPORT-R01`、`REPORT-R02`、`REPORT-R03`、`REPORT-R04`、`REPORT-R05`、`REPORT-R06`、`REPORT-R07`、`REPORT-R07-B`、`REPORT-R08`、`REPORT-R09`、`REPORT-R10`、`REPORT-R11` 共 12 条；`report_record_9706_1` 含 `RECORD61-IDENTITY`、`RECORD61-BODY-STATUS`、`RECORD61-BODY-NUMERIC`、`RECORD61-BODY-PERCENT`、`RECORD61-SEQUENCE-CONCLUSION`、`RECORD61-SCOPE`、`RECORD61-STRUCTURE`、`RECORD61-NUMERIC-DISCOVERY`、`RECORD61-METADATA`、`RECORD61-NONCONFORMING-ALERT` 共 10 条；`report_record_9706_202` 含 `RECORD202-NUMBER`、`RECORD202-BODY-STATUS`、`RECORD202-BODY-NUMERIC`、`RECORD202-BODY-PERCENT`、`RECORD202-SYMBOLS`、`RECORD202-STRUCTURE`、`RECORD202-SCOPE`、`RECORD202-IDENTITY`、`RECORD202-METADATA`、`RECORD202-NONCONFORMING` 共 10 条。`report_ptr` 的 `PTR-P01` 仅为禁用占位，仍返回 `PTR_NOT_VALIDATED`。

### 2.2 运行生命周期

```text
LifecycleStatus:
  queued
  running
  cancel_requested
  succeeded
  failed
  cancelled
  interrupted
```

允许迁移：

```text
queued -> running | failed | cancelled
running -> succeeded | failed | cancel_requested | interrupted
cancel_requested -> cancelled | failed | interrupted
```

其中 `interrupted` 只由启动恢复对账产生：应用、父进程或主机中断后发现遗留的 `running` / `cancel_requested`。父进程仍存活并观察到 Worker 非零退出、协议错误或发布校验失败时使用 `failed`。终态不可原位回退；重跑创建新 Run。

### 2.3 机器与人工状态

```text
MachineStatus:
  pass
  warning
  manual
  error

ReviewStatus:
  not_required
  pending
  in_progress
  resolved

ReviewResolution:
  consistent
  inconsistent
  indeterminate

ReviewActionType:
  confirm_candidate
  record_observation
  mark_source_unreadable
  withdraw
```

`resolved_status` 使用 `MachineStatus` 同一枚举，但由后端从有效 ReviewAction 和确定性规则派生。

`review_resolution` 是可空字段；没有完成复核时为 JSON `null`，不把 `null` 当作枚举成员。ReviewActionType 表示用户做了什么；ReviewResolution 表示把有效人工观察送入同一规则后得到的关系：匹配为 `consistent`，明确不匹配为 `inconsistent`，源文件仍无法可靠裁决为 `indeterminate`。二者不能混用。

每条计划规则另有执行状态：

```text
RuleExecutionState:
  pending
  running
  succeeded
  not_applicable
  unsupported
  failed
```

规则可靠执行但因证据不足生成 `manual` Finding 时，RuleExecution 仍为 `succeeded`。第一版 ModePlan 不存在“失败后仍可发布”的可选规则：任何 `failed`、终态时仍为 `pending/running` 或无解释地缺失都会使 Run 进入技术 `failed`；只有带稳定原因码的 `not_applicable` / `unsupported` 可以不生成 Finding。技术失败时不计算机器总体状态。

Run 的 `machine_overall_status` 仅在 `lifecycle_status=succeeded` 时有值，聚合优先级为 `error > manual > warning > pass`。合法示例：

```json
{
  "lifecycle_status": "succeeded",
  "machine_overall_status": "error"
}
```

这表示执行正常完成且发现确定性错误。技术失败使用 `failed` / `interrupted`，不生成机器总体结论。

### 2.4 `CaseSummary`

```json
{
  "id": "uuid",
  "name": "SAMPLE-D 核对任务",
  "description": null,
  "created_at": "2026-09-29T08:30:15.123Z",
  "updated_at": "2026-09-29T08:30:15.123Z",
  "revision": 1,
  "document_count": 2,
  "run_count": 1
}
```

### 2.5 `Document`

```json
{
  "id": "uuid",
  "case_id": "uuid",
  "role": "report",
  "original_filename": "SAMPLE-D Draft.pdf",
  "blob": {
    "sha256": "64-hex-characters",
    "size_bytes": 18345678,
    "media_type": "application/pdf"
  },
  "pdf": {
    "page_count": 120,
    "pdf_version": "1.7",
    "encrypted": false
  },
  "preflight": {
    "status": "accepted",
    "warnings": [],
    "template_hint": "known_report_v1"
  },
  "created_at": "2026-09-29T08:31:00.000Z"
}
```

`template_hint` 只用于说明预检观察，不承诺 Run 一定支持该模板。完整模板与规则判定在 Run 内固定并留痕。

### 2.6 `Run`

```json
{
  "id": "uuid",
  "case_id": "uuid",
  "mode": "report_record_9706_1",
  "lifecycle_status": "running",
  "machine_overall_status": null,
  "resolved_overall_status": null,
  "resolved_overall_review_revision": null,
  "resolved_overall_computed_at": null,
  "review_revision": 0,
  "inputs": [
    {
      "role": "report",
      "document_id": "uuid",
      "blob_sha256": "64-hex-characters"
    },
    {
      "role": "record_9706_1",
      "document_id": "uuid",
      "blob_sha256": "64-hex-characters"
    }
  ],
  "rule_bundle": {
    "id": "report-checks-mvp-2026-09-30",
    "sha256": "64-hex-characters"
  },
  "engine_version": "0.2.0",
  "component_versions": [
    {
      "id": "pdf_parser",
      "version": "pymupdf-1.28.2",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "report_template_bundle",
      "version": "known-report-v1",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "record61_template_bundle",
      "version": "gb9706.1-record-v1",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "ink_parser",
      "version": "pymupdf-ink-1.0.0",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "ocr_apple_vision",
      "version": "runtime-detected-version",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "ocr_tesseract",
      "version": "runtime-detected-version",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "evidence_renderer",
      "version": "1.0.0",
      "config_sha256": "64-hex-characters"
    }
  ],
  "scope": {
    "planned_rule_ids": [
      "RECORD61-IDENTITY",
      "RECORD61-BODY-STATUS",
      "RECORD61-BODY-NUMERIC",
      "RECORD61-BODY-PERCENT",
      "RECORD61-SEQUENCE-CONCLUSION",
      "RECORD61-SCOPE",
      "RECORD61-STRUCTURE",
      "RECORD61-NUMERIC-DISCOVERY",
      "RECORD61-METADATA",
      "RECORD61-NONCONFORMING-ALERT"
    ],
    "completed_rule_ids": ["RECORD61-BODY-STATUS"],
    "not_applicable_rule_ids": [],
    "unsupported_rule_ids": []
  },
  "rule_executions": [
    {
      "rule_id": "RECORD61-BODY-STATUS",
      "rule_version": "1.0.0",
      "state": "succeeded",
      "finding_ids": null,
      "started_at": "2026-09-29T08:32:02.000Z",
      "finished_at": "2026-09-29T08:32:03.000Z",
      "reason_code": null
    },
    {
      "rule_id": "RECORD61-BODY-NUMERIC",
      "rule_version": "1.0.0",
      "state": "running",
      "finding_ids": null,
      "started_at": "2026-09-29T08:32:03.000Z",
      "finished_at": null,
      "reason_code": null
    }
  ],
  "progress": {
    "phase": "extract_record",
    "message": "正在提取 Record 表格与 Ink",
    "completed_units": 43,
    "total_units": null,
    "percent": null
  },
  "finding_counts": null,
  "parent_run_id": null,
  "created_at": "2026-09-29T08:32:00.000Z",
  "started_at": "2026-09-29T08:32:01.000Z",
  "finished_at": null,
  "cancel_requested_at": null,
  "failure": null,
  "latest_event_sequence": 18
}
```

上面的 `rule_executions` 片段只展示运行中状态的字段形状；真实响应必须为 `planned_rule_ids` 中的全部规则各返回一条执行记录。当前 9706.1 为 10 条，9706.202 为 10 条。

没有可证明总量时，`total_units` 和 `percent` 必须为 `null`，不能估造百分比。

`rule_executions` 在 Run 创建时按 `planned_rule_ids` 完整生成，因此运行中也必须逐条返回并带固定的 `rule_version`。但运行中的 `succeeded` 只表示该规则已在内部 `work/` 完成，不表示其候选结果已经发布：只要 `lifecycle_status` 不是 `succeeded`，所有 `finding_ids` 和 `finding_counts` 都必须为 `null`，Finding/Evidence 端点也不得返回本次 Run 的候选数据。只有整批 Schema、规则覆盖、证据和制品哈希校验通过，并在最终数据库事务提交后，服务端才填入已发布的 Finding ID 与统计。`failed | cancelled | interrupted` Run 同样不得暴露半成品业务结果。

`component_versions` 是 Run 创建时冻结的不可变复现快照。每个对象至少包含稳定 component ID、实际版本字符串和覆盖该组件有效配置/模型/模板选择的 SHA-256；后续本机组件升级不能改写旧 Run。它必须与已接受 preflight 的 `required_components` 在 component ID 上完全相等；缺项、多项、重复项或 version/config hash 漂移都拒绝创建。比较与散列前按 component ID 做规范排序，响应数组的展示顺序不参与语义。`rule_selection=same` 的重跑要求这些组件配置仍可获得，否则返回明确冲突，不能悄悄换成当前版本。

成功终态的 `finding_counts` 结构固定为：

```json
{
  "total": 2,
  "error": 0,
  "manual": 1,
  "warning": 0,
  "pass": 1
}
```

五个字段均为非负整数，且 `total = error + manual + warning + pass`。计数覆盖本 Run 全部已发布 Finding，不受 `contributes_to_overall` 或 ReviewAction 影响，并必须与 Finding 表按不可变 `machine_status` 聚合的结果一致。运行中以及 `failed | cancelled | interrupted` 时保持 `null`；只允许在发布事务中与 Finding 同时写入。

`review_revision` 是整个 Run 共用的单调递增版本，不是 Finding 局部计数。任一 Finding 的有效 ReviewAction 追加或撤销都会产生新的 Run revision；Finding 只暴露 `last_changed_review_revision`。这样 `resolved_overall_status` 与导出可以唯一引用一次跨 Finding 的复核快照。

当 `review_revision=0` 时，`resolved_overall_status`、`resolved_overall_review_revision` 和 `resolved_overall_computed_at` 均为 `null`。首次有效 ReviewAction 后，服务端对同一 revision 下所有 `contributes_to_overall=true` Finding 的 `resolved_status` 使用 `error > manual > warning > pass` 聚合，并原子写入这三个字段；`resolved_overall_review_revision` 必须等于该快照使用的 Run revision。客户端和导出不得混用不同 revision。

### 2.7 `Finding`

```json
{
  "id": "uuid",
  "run_id": "uuid",
  "finding_version": 1,
  "display_sequence": 3,
  "rule": {
    "id": "RECORD61-BODY-NUMERIC",
    "version": "1.0.0"
  },
  "title": "8.7 漏电流实测值与 Report 一致性",
  "scope": {
    "kind": "inspection_item",
    "key": "8.7.3",
    "label": "漏电流"
  },
  "machine_status": "manual",
  "reason_code": "OCR_CANDIDATES_DISAGREE",
  "summary": "两个独立 OCR 路径未形成一致读数，需要查看 Record 原值",
  "observations": [
    {
      "id": "record-cell-1",
      "source": "record_9706_1",
      "raw_text": null,
      "candidates": [
        {"id": "vision-candidate-1", "engine": "apple_vision", "value": "123.4", "unit": "uA"},
        {"id": "tesseract-candidate-1", "engine": "tesseract", "value": "337.3", "unit": "uA"}
      ]
    }
  ],
  "normalized_values": [],
  "expected": {
    "withheld": true,
    "reason_code": "ANTI_ANCHOR_REVIEW_PENDING"
  },
  "comparison": {
    "performed": false,
    "steps": [],
    "blocked_by": "record-cell-1"
  },
  "reasoning_version": "record61-numeric-v1",
  "evidence_ids": ["uuid-record-crop"],
  "contributes_to_overall": true,
  "review": {
    "status": "pending",
    "resolution": null,
    "last_changed_review_revision": null,
    "input_schema": {
      "kind": "numeric_observation",
      "allowed_units": ["uA", "mA", "A"],
      "allowed_action_types": ["confirm_candidate", "record_observation", "mark_source_unreadable"],
      "candidate_ids": ["vision-candidate-1", "tesseract-candidate-1"]
    }
  },
  "resolved_status": "manual",
  "created_at": "2026-09-29T08:35:00.000Z"
}
```

`summary` 用于列表阅读，不能替代 observations、expected、comparison、reason code 与证据。原始观察和规范化值始终分开。

一个 Finding 只能有一个 `machine_status`。同一 scope 同时需要 R07 的“不符合要求”提示与聚合错误时，服务端发布两个带关联 scope 的 Finding（warning 与 error），分别计入统计，不能把 warning 塞进 error 的任意 details 后从计数中消失。R07 该 warning 固定 `contributes_to_overall=true`；它是唯一非通过 Finding 时 `machine_overall_status=warning`，与聚合 error 共存时两条 Finding 均发布、总体按严重度为 `error`。

一个 Finding/Q 只承载一个可独立定位的 scope；同一规则命中多个独立项目（例如 R07 项目 132、140）必须发布多个 Finding，并各自拥有 `display_sequence` 和 Evidence group。不得用一条规则级摘要隐藏多个用户需要逐项点击的问题。

上例启用了防锚定：在必需 Record 观察尚未提交完整前，公开 Finding DTO 不返回 Report 目标值、未执行的比较步骤或 Report 侧目标 Evidence。内部机器结果仍保存这些数据；ReviewAction 完成并触发重算后，API 才在重算响应及后续 GET 中揭示。该规则同时适用于 Finding 详情、复核聚合端点、Evidence 列表和导出。

### 2.8 `Evidence`

```json
{
  "id": "uuid",
  "finding_id": "uuid",
  "evidence_group_id": "comparison-1",
  "sequence": 1,
  "semantic_role": "source_observation",
  "document_id": "uuid",
  "role": "record_9706_1",
  "source_sha256": "64-hex-characters",
  "page_index": 105,
  "page_number": 106,
  "bbox": [212.4, 341.8, 294.1, 372.0],
  "coordinate_space": "pdf_points",
  "page_width": 595.28,
  "page_height": 841.89,
  "rotation": 0,
  "precision": "exact",
  "extraction_method": "ink_cell_assignment+roi_ocr",
  "text_anchor": "8.7.3",
  "artifact": {
    "id": "uuid-artifact",
    "media_type": "image/png",
    "sha256": "64-hex-characters",
    "width": 980,
    "height": 362
  },
  "created_at": "2026-09-29T08:35:00.000Z"
}
```

坐标规范：页面应用 CropBox 和 Rotation 后，以可见页面左上角为原点，单位为 1/72 英寸，x 向右、y 向下。`bbox=null` 只允许 `precision=page`；区域证据可以有较大的 bbox。`semantic_role` 只允许 `source_observation | comparison_target | context | expected_missing_region`；同一 `evidence_group_id` 内按 `sequence` 导航。客户端不得通过文件名或数组偶然顺序判断来源角色和配对关系。

### 2.9 `ReviewAction`

```json
{
  "id": "uuid",
  "run_id": "uuid",
  "finding_id": "uuid",
  "finding_version": 1,
  "sequence": 1,
  "action": "record_observation",
  "payload": {
    "observation_id": "record-cell-1",
    "raw_value": "123.4",
    "unit": "uA",
    "confirmation": true
  },
  "run_input_hashes": {
    "report": "64-hex-characters",
    "record_9706_1": "64-hex-characters"
  },
  "base_run_review_revision": 0,
  "run_review_revision": 1,
  "actor_id": "<opaque-loopback-session-id>",
  "note": "按 Record 裁剪人工读取",
  "created_at": "2026-09-29T09:00:00.000Z",
  "withdraws_review_action_id": null
}
```

ReviewAction 只追加，不物理修改或删除。当前实现仅持久化由已验证本机会话派生的 opaque `actor_id`（每个状态服务进程固定的 session id）；不接受客户端传入，也不把 `X-Actor-Id` 作为身份来源。`actor_source` 仍是后续用户系统的契约字段，当前响应不伪造该字段。

### 2.10 `Artifact`

```json
{
  "id": "uuid",
  "owner_type": "evidence",
  "owner_id": "uuid",
  "kind": "evidence_crop",
  "media_type": "image/png",
  "size_bytes": 284112,
  "sha256": "64-hex-characters",
  "generator_version": "evidence-renderer-1.0.0",
  "created_at": "2026-09-29T08:35:00.000Z"
}
```

API 不返回 Artifact 的存储相对路径或本机绝对路径。文件内容由受控资源端点按 ID 读取；同一 ID 的字节与哈希不可变。

### 2.11 `RunEvent`

```json
{
  "run_id": "uuid",
  "sequence": 19,
  "type": "phase_started",
  "occurred_at": "2026-09-29T08:34:00.000Z",
  "payload": {
    "phase": "compare",
    "message": "正在执行确定性比对"
  }
}
```

持久事件的 `(run_id, sequence)` 唯一且只增不改。SSE heartbeat 不属于 RunEvent，不占 sequence。

## 3. 系统端点

### 3.1 健康检查

```http
GET /healthz
```

只回答进程和必要本地依赖是否可用，不执行完整 PDF/OCR 自检：

```json
{
  "status": "ok",
  "database": "ok",
  "artifact_store": "ok",
  "coordinator": "ok"
}
```

数据库不可用时返回 `503`。该端点不返回路径、文件名或敏感诊断。

### 3.2 版本（规划；当前未实现）

```http
GET /api/v1/version
```

```json
{
  "api_version": "v1",
  "schema_version": "1.0.0",
  "app_version": "0.2.0",
  "engine_version": "0.2.0",
  "active_rule_bundle_id": "report-checks-mvp-2026-09-30",
  "idempotency_retention_days": 7
}
```

### 3.3 能力

```http
GET /api/v1/capabilities
```

```json
{
  "modes": {
    "report_self": {
      "enabled": true,
      "required_roles": ["report"],
      "includes_report_baseline": true
    },
    "report_ptr": {
      "enabled": false,
      "required_roles": ["report", "ptr"],
      "includes_report_baseline": false,
      "disabled_reason_code": "PTR_NOT_VALIDATED",
      "disabled_message": "PTR 规则与验收基线尚未完成"
    },
    "report_record_9706_1": {
      "enabled": true,
      "required_roles": ["report", "record_9706_1"],
      "includes_report_baseline": false
    },
    "report_record_9706_202": {
      "enabled": true,
      "required_roles": ["report", "record_9706_202"],
      "includes_report_baseline": false
    }
  },
  "uploads": {
    "accepted_media_types": ["application/pdf"],
    "max_file_bytes": 524288000,
    "max_pages": 2000,
    "encrypted_pdf_supported": false
  },
  "page_render": {
    "min_scale": 0.5,
    "max_scale": 3.0,
    "formats": ["png", "webp"]
  },
  "max_concurrent_runs": 1
}
```

为保持 v1 字段形状，`includes_report_baseline` 继续保留；其语义限定为“该模式是否计划并发布 `REPORT-*` 自检 Finding”，不表示是否解析、显示或引用 Report。对比模式必须返回 `false`。

当前只读实现额外返回 `schema_version`、`document_roles`、`forbidden_roles` 和该模式的 `rule_ids`。其中 `rule_ids` 采用已运行扫描器的实际粒度；`report_ptr` 返回 `PTR-P01`，同时以 `enabled=false` 和 `PTR_NOT_VALIDATED` 表示禁用。

限额示例是部署返回值，不是本文承诺的固定产品限额。前端据此做早期提示，后端仍执行权威校验。

### 3.4 只读规则目录

```http
GET /api/v1/rules
```

返回当前服务认识的完整规则目录，供 `/rules` 页面使用；前端不得内置规则版本或启用状态。下方 JSON 只展示字段形状和代表性规则；实际完整列表以当前服务响应和 `mvp.capabilities.RULE_CATALOG` 为准：

```json
{
  "rule_bundle": {
    "id": "report-checks-mvp-2026-09-30",
    "sha256": "64-hex-characters"
  },
  "rules": [
    {
      "id": "REPORT-R01",
      "version": "1.0.0",
      "title": "Report 身份字段一致性",
      "source": "report_baseline",
      "modes": ["report_self"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle"],
      "disabled_reason_code": null
    },
    {
      "id": "REPORT-R07",
      "version": "1.0.0",
      "title": "Report 多行结果与单项结论",
      "source": "report_baseline",
      "modes": ["report_self"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle"],
      "disabled_reason_code": null
    },
    {
      "id": "REPORT-R09",
      "version": "1.0.0",
      "title": "Report 新检验项目序号连续性",
      "source": "report_baseline",
      "modes": ["report_self"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle"],
      "disabled_reason_code": null
    },
    {
      "id": "REPORT-R10",
      "version": "1.0.0",
      "title": "Report 跨页首项续 N 检查",
      "source": "report_baseline",
      "modes": ["report_self"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle"],
      "disabled_reason_code": null
    },
    {
      "id": "REPORT-R11",
      "version": "1.0.0",
      "title": "Report 打印页码连续性",
      "source": "report_baseline",
      "modes": ["report_self"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle"],
      "disabled_reason_code": null
    },
    {
      "id": "PTR-P01",
      "version": null,
      "title": "PTR 声明范围与正文覆盖",
      "source": "mode_specific",
      "modes": ["report_ptr"],
      "catalog_status": "disabled",
      "required_component_ids": [],
      "disabled_reason_code": "PTR_NOT_VALIDATED"
    },
    {
      "id": "RECORD61-BODY-STATUS",
      "version": "1.0.0",
      "title": "9706.1 Record 正文状态与 Report 结论",
      "source": "mode_specific",
      "modes": ["report_record_9706_1"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle", "record61_template_bundle", "ink_parser"],
      "disabled_reason_code": null
    },
    {
      "id": "RECORD61-BODY-NUMERIC",
      "version": "1.0.0",
      "title": "9706.1 Record 实测值、单位换算与 Report",
      "source": "mode_specific",
      "modes": ["report_record_9706_1"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle", "record61_template_bundle", "ink_parser", "ocr_apple_vision", "ocr_tesseract", "evidence_renderer"],
      "disabled_reason_code": null
    },
    {
      "id": "RECORD202-NUMBER",
      "version": "1.0.0",
      "title": "9706.202 Record 逐页报告编号",
      "source": "mode_specific",
      "modes": ["report_record_9706_202"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle", "record202_template_bundle"],
      "disabled_reason_code": null
    },
    {
      "id": "RECORD202-BODY-STATUS",
      "version": "1.0.0",
      "title": "9706.202 Ink 符号与 Report 结果",
      "source": "mode_specific",
      "modes": ["report_record_9706_202"],
      "catalog_status": "enabled",
      "required_component_ids": ["pdf_parser", "report_template_bundle", "record202_template_bundle", "ink_parser"],
      "disabled_reason_code": null
    }
  ]
}
```

`catalog_status` 只允许 `enabled | disabled`。规则目录描述静态能力，不替代针对具体输入的 Run preflight；响应必须包含当前 bundle 内全部规则，而不是只返回已实现模式的摘要。

当前规则目录与上述计划保持一致：Report 自检为 12 条独立规则；9706.1 启用结构、模板外数值发现和元数据对象核对；9706.202 启用图例、结构、字段映射、身份和元数据核对。9706.202 每个正文单元还会在 `RECORD202-BODY-STATUS` 中严格比较项目名称，项目名差异进入 `mismatch`，证据不足进入 `manual`；`scope_ledger` 保存项目、条款、要求、出现次序、数值、身份和元数据的对象级记录，S48 表 3 内容标记为 `excluded`。`REPORT-R09-R10` 仅为早期结果兼容别名，不在当前 Report 自检计划中。上述 ID 与 `mvp.capabilities.RULE_CATALOG` 及 `/api/v1/rules` 返回值保持一致。

### 3.5 系统诊断（规划；当前未实现）

```http
GET /api/v1/system/diagnostics
```

该端点为 `/system` 页面执行有界、只读的组件探测，不处理用户 PDF，也不返回安装路径：

```json
{
  "overall_status": "ready",
  "checked_at": "2026-09-29T08:31:30.000Z",
  "components": [
    {
      "id": "pdf_parser",
      "status": "ready",
      "version": "pymupdf-1.28.2",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_self", "report_ptr", "report_record_9706_1", "report_record_9706_202"],
      "reason_code": null
    },
    {
      "id": "report_template_bundle",
      "status": "ready",
      "version": "known-report-v1",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_self", "report_ptr", "report_record_9706_1", "report_record_9706_202"],
      "reason_code": null
    },
    {
      "id": "record61_template_bundle",
      "status": "ready",
      "version": "gb9706.1-record-v1",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_record_9706_1"],
      "reason_code": null
    },
    {
      "id": "record202_template_bundle",
      "status": "ready",
      "version": "gb9706.202-record-v1",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_record_9706_202"],
      "reason_code": null
    },
    {
      "id": "ink_parser",
      "status": "ready",
      "version": "pymupdf-ink-1.0.0",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_record_9706_1", "report_record_9706_202"],
      "reason_code": null
    },
    {
      "id": "ocr_apple_vision",
      "status": "ready",
      "version": "runtime-detected-version",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_record_9706_1"],
      "reason_code": null
    },
    {
      "id": "ocr_tesseract",
      "status": "ready",
      "version": "runtime-detected-version",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_record_9706_1"],
      "reason_code": null
    },
    {
      "id": "evidence_renderer",
      "status": "ready",
      "version": "1.0.0",
      "config_sha256": "64-hex-characters",
      "required_by_modes": ["report_record_9706_1"],
      "reason_code": null
    }
  ]
}
```

组件 `status` 为 `ready | degraded | unavailable | not_configured`，总体为 `ready | degraded | unavailable`。`components` 返回当前规则目录全部 `required_component_ids` 的精确去重并集，而不是节选；禁用且尚无组件契约的规则不凭空添加组件。`/healthz` 仍只表示服务是否活着；诊断端点才提供规则运行所需的 PDF parser、模板包、Ink parser、Apple Vision、Tesseract 和 Evidence renderer 的实际 readiness、版本和配置哈希。

### 3.6 输入相关 Run preflight 与 Scope Ledger

```http
POST /api/v1/cases/{case_id}/runs:preflight
X-CSRF-Token: <token>
```

该请求只读、不创建 Run、不写 ReviewAction；使用 POST 是因为输入组合较大且不能进入 URL。请求体与创建 Run 的输入槽一致：

```json
{
  "mode": "report_record_9706_1",
  "inputs": {
    "report_document_id": "uuid",
    "record_document_id": "uuid"
  }
}
```

响应把模式、实际 Document、规则包和本机组件状态组合成前端可直接展示的 Scope Ledger：

```json
{
  "case_id": "uuid",
  "mode": "report_record_9706_1",
  "enabled": true,
  "can_create_run": true,
  "rule_bundle": {
    "id": "report-checks-mvp-2026-09-30",
    "sha256": "64-hex-characters"
  },
  "inputs": [
    {
      "role": "report",
      "document_id": "uuid",
      "blob_sha256": "64-hex-characters",
      "page_count": 120,
      "template_status": "supported",
      "reason_code": null
    },
    {
      "role": "record_9706_1",
      "document_id": "uuid",
      "blob_sha256": "64-hex-characters",
      "page_count": 145,
      "template_status": "supported",
      "reason_code": null
    }
  ],
  "planned_rules": [
    {
      "rule_id": "RECORD61-BODY-STATUS",
      "rule_version": "1.0.0",
      "source": "mode_specific",
      "disposition": "ready",
      "reason_code": null
    },
    {
      "rule_id": "RECORD61-BODY-NUMERIC",
      "rule_version": "1.0.0",
      "source": "mode_specific",
      "disposition": "ready",
      "reason_code": null
    }
  ],
  "coverage": {
    "planned": 2,
    "ready": 2,
    "not_applicable": 0,
    "unsupported": 0,
    "blocked": 0
  },
  "not_covered": [
    {
      "code": "RECORD61_SCOPE_PARTIAL",
      "message": "9706.1 全部数值页不在当前已验证范围内"
    }
  ],
  "required_components": [
    {
      "id": "pdf_parser",
      "status": "ready",
      "version": "pymupdf-1.28.2",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "report_template_bundle",
      "status": "ready",
      "version": "known-report-v1",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "record61_template_bundle",
      "status": "ready",
      "version": "gb9706.1-record-v1",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "ink_parser",
      "status": "ready",
      "version": "pymupdf-ink-1.0.0",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "ocr_apple_vision",
      "status": "ready",
      "version": "runtime-detected-version",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "ocr_tesseract",
      "status": "ready",
      "version": "runtime-detected-version",
      "config_sha256": "64-hex-characters"
    },
    {
      "id": "evidence_renderer",
      "status": "ready",
      "version": "1.0.0",
      "config_sha256": "64-hex-characters"
    }
  ],
  "blocking_issues": [],
  "warnings": [
    {
      "code": "PARTIAL_RULE_COVERAGE",
      "message": "本次通过只代表已列出的规则范围"
    }
  ],
  "plan_hash": "64-hex-characters",
  "generated_at": "2026-09-29T08:32:00.000Z"
}
```

`disposition` 只允许 `ready | not_applicable | unsupported | blocked`；每个非 `ready` 项必须有稳定 reason code。`template_status` 只允许 `supported | unknown | unsupported`。`unsupported` 只能在用户已看见未覆盖范围、且同一计划至少有一条 `ready` 规则时创建 Run，并在正式 RuleExecution 中带原因结束。`blocked`、缺少必需组件、角色错误或 Blob 完整性问题使 `can_create_run=false`。`coverage.ready=0` 时也必须 `can_create_run=false`，`blocking_issues` 包含 `NO_EXECUTABLE_RULES`；创建端会重新计算并以 `422 NO_EXECUTABLE_RULES` 拒绝，不创建 queued Run。若规则在运行中才全部落为 `not_applicable/unsupported`、最终没有任何 Finding，Run 以 `failed + NO_EXECUTABLE_RULES + machine_overall_status=null` 结束，不发布空通过。PTR disabled 时 preflight 返回 `200`、`enabled=false`、`can_create_run=false` 和 `PTR_NOT_VALIDATED`，而真正创建仍返回 `409 MODE_DISABLED`。

`required_components` 不是示例子集：它必须等于全部 `disposition=ready` 规则的 `required_component_ids` 精确去重并集，并额外包含导致规则 `blocked` 的缺失或降级组件。`plan_hash` 覆盖 mode、Document ID/哈希、规则 bundle、planned rules、disposition 和该完整集合的 component version/config hash；散列前按 component ID 规范排序，响应数组顺序不作为身份。创建 Run 必须带回该值；后端重新生成并逐项校验，任何集合、版本或哈希漂移均拒绝创建并要求刷新 preflight。preflight 不是内容通过结论，也不产生 Finding。

## 4. Case 端点

### 4.1 创建 Case

```http
POST /api/v1/cases
Idempotency-Key: <uuid>
X-CSRF-Token: <token>
```

```json
{
  "name": "SAMPLE-D 核对任务",
  "description": null
}
```

校验：name 去除首尾空白后为 1–120 个 Unicode 字符；description 最多 2000 字符。成功 `201` 返回 `CaseSummary`，并设置 `Location: /api/v1/cases/{case_id}`。

### 4.2 Case 列表

```http
GET /api/v1/cases?limit=25&cursor=...&query=SAMPLE-D
```

按最近更新时间降序。`query` 只搜索 Case 名称和说明，不搜索 PDF 全文。

### 4.3 Case 详情

```http
GET /api/v1/cases/{case_id}
```

返回 `CaseSummary`，并增加最近 Document、最近 Run 的轻量摘要；不内嵌 Findings 或图片。

第一阶段不提供删除 Case 的 HTTP API，避免误删原始证据；归档能力另行设计。

## 5. Document 端点

### 5.1 上传并创建 Document

```http
POST /api/v1/cases/{case_id}/documents
Content-Type: multipart/form-data
Idempotency-Key: <uuid>
X-CSRF-Token: <token>
```

表单字段：

```text
role  = report | ptr | record_9706_1 | record_9706_202
file  = PDF bytes
```

服务端流式执行以下预检：

1. Case 存在；
2. 上传大小在限额内；
3. magic bytes 和解析器均确认 PDF；
4. SHA-256 计算完成；
5. PDF 可解析、非加密、页数大于 0 且不超过限额；
6. 页面尺寸和对象数量未触发防护限额；
7. 角色枚举有效；
8. 临时文件完整写入后才原子发布 Blob；
9. 创建不可变 Document。

成功返回 `201 Document`。相同 Blob 可去重存储，但每次合法上传仍可创建新的 Document 身份；幂等重放不会重复创建。

典型失败：

- `400 INVALID_PDF`；
- `400 EMPTY_PDF`；
- `409 ENCRYPTED_PDF_NOT_SUPPORTED`；
- `413 FILE_TOO_LARGE`；
- `422 PAGE_LIMIT_EXCEEDED`；
- `422 PDF_COMPLEXITY_LIMIT_EXCEEDED`；
- `422 INVALID_DOCUMENT_ROLE`；
- `507 ARTIFACT_STORE_FULL`。

失败时不创建 Document，quarantine 文件按清理策略删除。

### 5.2 Case 的 Document 列表

```http
GET /api/v1/cases/{case_id}/documents?role=report&limit=25&cursor=...
```

返回 `Document` 摘要，按创建时间降序。

### 5.3 Document 元数据

```http
GET /api/v1/documents/{document_id}
```

返回完整 `Document` 元数据，不返回存储相对路径或本机绝对路径。Evidence 工作台可用它确认原始文件名、角色、哈希和页数。

### 5.4 读取原始 PDF

```http
GET /api/v1/documents/{document_id}/content
Range: bytes=0-1048575
```

返回原始、未修改的 PDF；支持 `200` / `206` / `416`，设置：

```text
Accept-Ranges: bytes
Content-Type: application/pdf
X-Content-Type-Options: nosniff
Content-Security-Policy: sandbox
Content-Disposition: inline; filename*=UTF-8''...
ETag: "<blob-sha256>"
```

禁止从查询参数读取任意文件路径。

这是完整 PDF.js 查看器的权威字节来源，不是下载单个证据裁剪的接口。Report 自检加载完整 Report；对比模式分别加载完整 Report 与完整 PTR/Record。客户端可按 Evidence 的页码和 bbox 跳转高亮，但必须允许用户继续浏览该 Document 的所有页面。

### 5.5 渲染单页图像（规划；当前未实现）

```http
GET /api/v1/documents/{document_id}/pages/{page_number}/image?scale=1.5&format=webp
```

- `page_number` 为 1-based；
- `scale` 允许范围由 capability 返回，服务端限幅；
- `format=png|webp`；
- 响应 ETag 包含 Blob、页码、缩放、旋转与渲染器版本；
- 该图用于预览，不替代 PDF.js 原文件和 Evidence 坐标。

## 6. Run 端点

### 6.1 创建 Run

```http
POST /api/v1/cases/{case_id}/runs
Idempotency-Key: <uuid>
X-CSRF-Token: <token>
```

Report 自检示例：

```json
{
  "mode": "report_self",
  "preflight_plan_hash": "64-hex-characters",
  "inputs": {
    "report_document_id": "uuid"
  }
}
```

9706.1 示例：

```json
{
  "mode": "report_record_9706_1",
  "preflight_plan_hash": "64-hex-characters",
  "inputs": {
    "report_document_id": "uuid",
    "record_document_id": "uuid"
  }
}
```

PTR 示例在 capability disabled 时返回：

```http
409 MODE_DISABLED
```

能力禁用检查先于 preflight hash 必填检查，因此任何 `report_ptr` 创建请求在本阶段都稳定返回 `409 MODE_DISABLED`，不会因缺少 plan hash 变成另一个错误，也不会创建 Run。

错误体的 `details` 至少包含：

```json
{
  "error": {
    "code": "MODE_DISABLED",
    "message": "PTR 检查尚未启用",
    "details": {
      "mode": "report_ptr",
      "disabled_reason_code": "PTR_NOT_VALIDATED"
    },
    "request_id": "uuid",
    "retryable": false
  }
}
```

后端必须校验：

- 所有 Document 属于该 Case；
- 每个 Document 的 role 与 mode 相符；
- 输入数量精确；
- Blob 仍存在且哈希可读；
- capability enabled；
- 选择的规则包存在且完整；
- `preflight_plan_hash` 已提供，且后端按当前 Document 哈希、规则包和组件配置重算后完全一致；
- ModePlan 只包含该 mode 允许的规则；客户端不能指定、关闭或注入其他模式的规则。

普通创建总是固定当前激活规则包；客户端不能指定任意规则包。缺少 preflight hash 返回 `422 RUN_PREFLIGHT_REQUIRED`；规则/组件/输入在展示后发生漂移则返回 `409 RUN_PLAN_CHANGED`，前端必须重新预检，不能静默接受新范围。需要复现旧规则或升级规则时使用重跑端点的受控 `same | active` 选项。

成功 `202` 返回 Run，`lifecycle_status=queued`。队列繁忙不是失败；响应可同时返回 `queue_position` 估计，但该值不进入审计事实。

### 6.2 Run 详情

```http
GET /api/v1/runs/{run_id}
```

返回完整 Run 快照。刷新页面应先调用此端点，再连接 SSE。若 published manifest 或输入 Blob 完整性异常，额外返回 `integrity_status=degraded`，相关导出端点拒绝服务，不能静默重建历史。

### 6.3 取消 Run

```http
POST /api/v1/runs/{run_id}:cancel
Idempotency-Key: <uuid>
X-CSRF-Token: <token>
```

请求体可为空或提供短原因：

```json
{"reason": "用户取消"}
```

- queued：事务内转为 `cancelled`，返回 `200`；
- running：转为 `cancel_requested`，返回 `202`；
- cancel_requested / cancelled：返回当前 Run，`200`；
- succeeded / failed / interrupted：`409 RUN_NOT_CANCELLABLE`。

客户端断开或关闭页面不会自动调用取消。

### 6.4 重跑（规划；当前未实现）

```http
POST /api/v1/runs/{run_id}:rerun
Idempotency-Key: <uuid>
X-CSRF-Token: <token>
```

```json
{
  "rule_selection": "same",
  "preflight_plan_hash": null
}
```

`rule_selection`：

- `same`：沿用原 Run 的规则包和 component versions/config hashes，便于复现；`preflight_plan_hash` 必须为 `null`，任一旧组件快照不可用时返回 `409 REQUIRED_COMPONENT_UNAVAILABLE`；
- `active`：使用当前激活规则包；必须先对原输入调用 Run preflight 并提交新的 `preflight_plan_hash`，明确记录规则和组件版本差异。

重跑复用精确 Document ID 和 Blob 哈希，创建新 Run，并设置 `parent_run_id`。若任一输入 Blob 缺失或哈希不符，返回 `409 RUN_INPUT_UNAVAILABLE`。旧 ReviewAction 不复制到新 Run。

### 6.5 Run 列表（规划；当前未实现）

```http
GET /api/v1/runs?query=SAMPLE-D&mode=report_record_9706_1&lifecycle_status=succeeded&machine_overall_status=error&created_from=2026-09-01T00:00:00%2B08:00&created_to=2026-10-01T00:00:00%2B08:00&limit=25&cursor=...
```

用于全局历史页。可选条件：

- `case_id`：精确 Case UUID；
- `query`：最长 128 个 Unicode 字符，只对可索引的 `report_number_search` 和各 RunInput Document 的 `original_filename_search` 做确定性前缀匹配；
- `mode`、`lifecycle_status`、`machine_overall_status`：精确枚举，可逗号分隔多值；
- `created_from`、`created_to`：带时区 ISO 8601 边界，区间为左闭右开；
- `limit`、`cursor`：稳定游标分页。

`query` 不搜索 PDF 正文、OCR 全文、Finding 摘要或本机路径。`report_number_search` 只由达到可靠性门槛的 Report 身份提取写入；未可靠提取时为 `null`，仍可以用原始文件名前缀找到 Run。结果按 `created_at DESC, id DESC` 排序，cursor 绑定全部筛选条件的哈希；条件改变后不得复用旧 cursor。数据库为 Report 编号搜索键、Document 文件名搜索键、mode/状态/日期建立索引，列表请求不临时重读 PDF。

## 7. SSE 事件流（规划；当前未实现）

```http
GET /api/v1/runs/{run_id}/events
Accept: text/event-stream
Last-Event-ID: 18
```

响应头：

```text
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

持久事件格式：

```text
id: 19
event: phase_started
data: {"run_id":"uuid","sequence":19,"occurred_at":"...","phase":"compare","message":"正在执行确定性比对"}
```

事件类型：

| event | 是否持久化 | 负载 |
|---|---:|---|
| `snapshot` | 否 | 连接建立时的 Run 快照与最新 sequence |
| `run_state_changed` | 是 | 新 lifecycle_status、时间与原因码 |
| `phase_started` | 是 | 阶段标识与可读说明 |
| `phase_progress` | 是 | completed/total/percent；未知值为 null |
| `finding_emitted` | 是 | Finding ID、rule ID、machine status，不内嵌完整证据 |
| `artifact_published` | 是 | Artifact ID、类型和哈希 |
| `heartbeat` | 否 | 连接保活时间戳 |

规则：

- 每个 Run 的持久 sequence 严格递增；
- 服务端先写数据库事务，再向订阅者发送；
- `Last-Event-ID` 之后的持久事件按顺序补发；
- Last-Event-ID 超过服务端最新值返回 `409 EVENT_CURSOR_AHEAD`；
- 太旧且已超出事件保留窗口时先发送 `snapshot`，再从最早可用序列继续，并标记 `history_truncated=true`；
- 客户端必须按 `(run_id, sequence)` 去重；
- SSE 断线不改变 Run；
- Run 终态事件发出后连接可由服务端正常关闭。

## 8. Finding 与 Evidence 端点

### 8.1 Finding 列表

```http
GET /api/v1/runs/{run_id}/findings?machine_status=manual&rule_id=RECORD61-BODY-NUMERIC&limit=50&cursor=...
```

支持筛选：

- `machine_status`：逗号分隔枚举；
- `resolved_status`；
- `review_status`；
- `rule_id`；
- `contributes_to_overall`；
- `has_evidence`。

默认排序为业务严重度 `error, manual, warning, pass`，再按规则定义顺序和 scope；这也是发布时不可变 `display_sequence` 的分配顺序。响应使用 Finding 列表摘要，但保留 ID、`display_sequence`（界面显示为 `Qn`）、状态、reason code、scope、证据数量和 review 摘要。切换筛选、分页和追加 ReviewAction 都不能重新编号。

前端五个选项卡固定为 `全部 | 有问题 | 待人工复核 | 警示 | 没问题`，分别使用无筛选以及 `error | manual | warning | pass` 精确筛选；计数读取 Run 的 `finding_counts`，四种状态独立统计，禁止把 warning 合并进 error 或 pass。

### 8.2 Finding 详情

```http
GET /api/v1/findings/{finding_id}
```

返回 Finding 的 observations、normalized_values、expected、comparison、reasoning_version、证据 ID、Review 输入 Schema 和当前派生状态。若 Finding 启用防锚定且必需源观察尚未完整提交，`expected` 返回 withheld 标记，`comparison` 只返回阻塞原因，Report 侧目标 Evidence ID 不出现在响应中；完成 ReviewAction 后才返回完整内容。

### 8.3 Evidence 列表（规划）

```http
GET /api/v1/findings/{finding_id}/evidence
```

Evidence 通常数量有限，不分页，按 `evidence_group_id, sequence` 返回；若超过配置上限则 Finding 发布校验失败，避免无界响应。点击 Q 后，客户端用每条 Evidence 的 `document_id + page_number + bbox` 驱动完整 PDF 查看器跳页和高亮，并提供组内上一处/下一处导航。Artifact 裁剪只作可选放大辅助。防锚定 Finding 在复核完成前只返回 Record 侧 Evidence；Report 侧目标 Evidence 保持内部可追溯但不通过任何公共 Evidence DTO 泄露，完整 Report 查看器也必须在该复核步骤锁定或遮蔽目标导航。

### 8.4 Evidence 图片（规划）

```http
GET /api/v1/evidence/{evidence_id}/image
```

返回已发布且哈希通过的裁剪图。设置强 ETag 为 Artifact SHA-256。若 Artifact 缺失或哈希不符返回 `409 ARTIFACT_INTEGRITY_ERROR`，不得现场生成一张不同内容冒充历史证据。

客户端点击 Evidence 时应优先通过 `document_id` 打开原 PDF 的 `page_number` 并按坐标高亮；图片仅是便捷预览。

## 9. 人工复核端点

人工复核不会覆盖机器观察或 `machine_status`。ReviewAction 只适用于 `machine_status=manual` 的 Finding，后端只接受其 `review.input_schema` 允许的观察类型。确定性 `pass/warning/error` 不接受 ReviewAction；如未来需要普通备注，应使用不参与状态计算的独立 Annotation 契约。

### 9.1 追加 ReviewAction

```http
POST /api/v1/findings/{finding_id}/reviews
Idempotency-Key: <uuid>
X-CSRF-Token: <token>
```

#### 确认候选

```json
{
  "action": "confirm_candidate",
  "finding_version": 1,
  "base_run_review_revision": 0,
  "candidate_id": "vision-candidate-1",
  "confirmation": true,
  "note": null
}
```

#### 录入源文件观察值

```json
{
  "action": "record_observation",
  "finding_version": 1,
  "base_run_review_revision": 0,
  "observation_id": "record-cell-1",
  "raw_value": "123.4",
  "unit": "uA",
  "confirmation": true,
  "note": "按 Record 裁剪人工读取"
}
```

`confirmation=true` 表示用户已完成界面的二次确认。数值链必须遵守先录后比：提交前端界面不得展示会诱导读取的 Report 目标值；服务端不依赖此 UI 行为保证正确性，但会在审计中记录 action 类型和确认步骤。

`confirm_candidate` 和 `record_observation` 本身不预设复核结论；后端重算后才产生 `consistent` 或 `inconsistent`。`mark_source_unreadable` 产生 `indeterminate`，对应的 `resolved_status` 保持 `manual`。

#### 标记源文件无法辨认

```json
{
  "action": "mark_source_unreadable",
  "finding_version": 1,
  "base_run_review_revision": 0,
  "observation_id": "record-cell-1",
  "confirmation": true,
  "note": "笔迹覆盖，无法可靠读取"
}
```

#### 撤销先前动作

```json
{
  "action": "withdraw",
  "finding_version": 1,
  "base_run_review_revision": 2,
  "review_action_id": "uuid-to-withdraw",
  "note": "录入值看错，撤销后重新读取"
}
```

撤销本身是新 ReviewAction，不删除原动作。只能撤销同一 Finding 的有效动作；若后续动作依赖它，服务端返回 `409 REVIEW_ACTION_HAS_DEPENDENTS`，应先逆序撤销。

一个 Finding 需要复核多个阻塞观察时，只有全部必需观察都有有效动作后，`review.status` 才能成为 `resolved`；否则保持 `in_progress`。撤销最后一个有效观察后，状态按剩余动作重新派生为 `pending` 或 `in_progress`，resolution 相应回到 `null` 或剩余动作对应值。

成功返回 `201`：

```json
{
  "review_action": {},
  "review": {
    "status": "resolved",
    "resolution": "consistent",
    "last_changed_review_revision": 1
  },
  "run_review_revision": 1,
  "resolved_overall_status": "pass",
  "resolved_overall_review_revision": 1,
  "resolved_overall_computed_at": "2026-09-29T09:00:00.100Z",
  "recalculation": {
    "resolved_status": "pass",
    "normalized_values": [
      {"value": "0.3378", "unit": "mA"},
      {"value": "0.12", "unit": "mA", "operation": "report_precision_rounding"}
    ],
    "comparison": {
      "performed": true,
      "matches": true
    }
  }
}
```

响应中的 `resolved_overall_*` 三字段来自同一次 Run 级 revision。示例对应 SAMPLE-D 的 9706.1 Run：另一条 `RECORD61-BODY-STATUS` Finding 为 `pass`，数值 Finding 人工录入两格 `123.4 μA` 并确认一致后也重算为 `pass`，因此复核后总体为 `pass`；原始 `machine_overall_status=manual` 仍保持不变。客户端不能用当前 Finding 的状态直接覆盖 Run 聚合，而应使用服务端对同一 revision 的完整聚合结果。

客户端不能提交 `machine_status`、`resolved_status`、总体状态、单位换算结果或比较结果；出现这些字段返回 `422 READ_ONLY_FIELD_SUBMITTED`。

对非 `manual` Finding 提交任何 ReviewAction 均返回 `409 FINDING_NOT_REVIEWABLE`。若需修正规则或提取器，应生成新规则版本并重跑，而不是人工把确定性错误改成通过。

### 9.2 ReviewAction 列表（规划）

```http
GET /api/v1/findings/{finding_id}/reviews?limit=100&cursor=...
```

按 sequence 升序，返回包括已撤销动作在内的完整追加式历史，以及当前 Run 级 `review_revision`、该 Finding 的 `last_changed_review_revision`、有效动作 ID 和衍生状态。

### 9.3 待复核队列（规划）

```http
GET /api/v1/reviews?case_id=uuid&status=pending,in_progress&mode=report_record_9706_1&limit=25&cursor=...
```

返回 Finding 摘要、Run、模式、reason code、证据缩略图 URL 和复核 Schema。默认仅列出 `machine_status=manual` 且仍需动作的 Finding。

```http
GET /api/v1/reviews/{finding_id}
```

这是复核工作台聚合读取端点：一次返回 Finding、当前 Review 摘要、Evidence 元数据和必要 Document 摘要，不内嵌 PDF 字节或大图。启用防锚定时遵守与 Finding 详情相同的遮蔽规则，不能通过此聚合端点提前取得 Report 目标。

## 10. 导出端点（规划）

当前版本没有导出或签名路由；以下内容是后续设计，不能作为现有服务能力使用。

### 10.1 JSON 导出

```http
GET /api/v1/runs/{run_id}/export.json?review_revision=current
```

仅允许终态 Run。响应包括：

- Schema / app / engine / rule bundle 版本，以及 Run 固化的 component versions/config hashes；
- 输入 Document ID、角色、原始文件名和 Blob SHA-256；
- lifecycle_status 与 machine_overall_status；
- 计划、完成、不适用和不支持的规则范围；
- 完整 Findings 与 Evidence 定位；
- 指定 review revision 之前的 ReviewAction；
- `resolved_status`，以及绑定到同一 revision 的 `resolved_overall_status`、`resolved_overall_review_revision`、`resolved_overall_computed_at`；
- 导出生成时间与内容摘要哈希。

`review_revision=current` 在响应中固化为具体整数。也可请求已存在的整数 revision，以复现历史复核快照。不存在的 revision 返回 `404 REVIEW_REVISION_NOT_FOUND`。

防锚定规则同样适用于导出：请求的 Run review revision 尚未完成所需源观察时，相关 `expected`、未执行比较和 Report 目标 Evidence 仍以 withheld 形式输出；不能通过导出来绕过复核顺序。

### 10.2 HTML 导出

```http
GET /api/v1/runs/{run_id}/export.html?review_revision=current
```

生成只读本地报告，内容与 JSON 同一快照一致。HTML 不嵌入原始 PDF，不包含活动脚本，不从网络加载资源，并显示“机器状态”和“人工复核后衍生状态”两列。

Run 为 `failed` / `cancelled` / `interrupted` 时可导出运行诊断摘要，但不得生成看似完整的核对结论；此时返回 `409 RUN_HAS_NO_COMPLETE_RESULT`，错误 details 提供允许的诊断导出 URL（若已实现）。

## 11. 错误响应

统一格式：

```json
{
  "error": {
    "code": "DOCUMENT_ROLE_MISMATCH",
    "message": "所选文件角色与运行模式不一致",
    "details": {
      "expected_role": "record_9706_1",
      "actual_role": "record_9706_202",
      "document_id": "uuid"
    },
    "request_id": "req-uuid",
    "retryable": false
  }
}
```

`message` 可本地化，客户端逻辑只依赖稳定 `code`。`details` 不返回绝对路径、堆栈、原始 OCR 全文或敏感环境变量。

### 11.1 HTTP 状态映射

| HTTP | 用途 |
|---:|---|
| 400 | JSON、游标、PDF 或参数格式非法 |
| 403 | CSRF / Origin 校验失败 |
| 404 | 实体不存在 |
| 409 | 当前状态、能力、revision、幂等或完整性冲突 |
| 413 | 上传过大 |
| 415 | 媒体类型不支持 |
| 422 | 请求结构合法但领域校验失败 |
| 429 | 本机防护限流或队列上限 |
| 500 | 未分类内部错误；不泄露堆栈 |
| 503 | 数据库、Artifact Store 或 Coordinator 不可用 |
| 507 | 本地存储空间不足 |

### 11.2 稳定错误码

系统与安全：

```text
CSRF_VALIDATION_FAILED
ORIGIN_NOT_ALLOWED
HOST_NOT_ALLOWED
FETCH_METADATA_REJECTED
REQUEST_VALIDATION_FAILED
INVALID_CURSOR
IDEMPOTENCY_KEY_REUSED
IDEMPOTENCY_REQUEST_IN_PROGRESS
REVISION_CONFLICT
SERVICE_NOT_READY
STORAGE_UNAVAILABLE
```

上传与 Document：

```text
UNSUPPORTED_MEDIA_TYPE
INVALID_PDF
EMPTY_PDF
ENCRYPTED_PDF_NOT_SUPPORTED
FILE_TOO_LARGE
PAGE_LIMIT_EXCEEDED
PDF_COMPLEXITY_LIMIT_EXCEEDED
INVALID_DOCUMENT_ROLE
DOCUMENT_ROLE_MISMATCH
BLOB_INTEGRITY_ERROR
ARTIFACT_STORE_FULL
```

Run：

```text
INVALID_RUN_MODE
MISSING_RUN_INPUT
EXTRA_RUN_INPUT
RUN_INPUT_CASE_MISMATCH
RUN_INPUT_UNAVAILABLE
RUN_PREFLIGHT_REQUIRED
RUN_PLAN_CHANGED
NO_EXECUTABLE_RULES
RULE_EXECUTION_NO_FINDING
RULE_EXECUTION_ZERO_RESULT_INVALID
RUN_INPUT_SNAPSHOT_INVALID
RUN_INPUT_HASH_MISMATCH
WORKER_ARTIFACT_INVALID
RUN_DISPATCH_FAILED
REQUIRED_COMPONENT_UNAVAILABLE
MODE_DISABLED
RULE_BUNDLE_UNAVAILABLE
RUN_NOT_CANCELLABLE
RUN_NOT_TERMINAL
EVENT_CURSOR_AHEAD
RUN_HAS_NO_COMPLETE_RESULT
```

Finding、Evidence 与 Review：

```text
FINDING_NOT_REVIEWABLE
INVALID_REVIEW_ACTION
REVIEW_INPUT_SCHEMA_MISMATCH
REVIEW_ACTION_NOT_FOUND
READ_ONLY_FIELD_SUBMITTED
REVIEW_ACTION_HAS_DEPENDENTS
REVIEW_REVISION_NOT_FOUND
EVIDENCE_NOT_AVAILABLE
ARTIFACT_INTEGRITY_ERROR
```

`retryable=true` 只表示原请求在外部状态恢复后可原样重试，不表示客户端应无限重试。领域校验、能力禁用和 revision 冲突通常不可原样重试。

## 12. 发布与完整性规则

Worker 输出只有满足以下条件才可通过 API 可见：

1. manifest Schema 与版本受支持；
2. 所有相对路径规范化后仍位于该 Run 的 work 目录；
3. 每个文件大小、媒体类型和 SHA-256 与 manifest 一致；
4. 所有 Evidence 引用有效 Document 和页码；
5. bbox 在声明页面尺寸范围内；
6. 所有非 pass Finding 有证据或明确的缺失预期区域；
7. planned rules 均有 Finding、not applicable 或明确 unsupported 记录；
8. 任一 planned RuleExecution 为 `failed`、仍为 `pending/running` 或无解释缺失时整次发布失败；第一版没有可吞掉失败的 optional rule；
9. 空 Findings 不会聚合为 pass；
10. 数据库事务与 Artifact 原子发布顺序符合架构文档。

Artifact 读取时再次校验文件存在和尺寸；安全或导出关键路径校验 SHA-256。发现漂移返回完整性错误并停止导出，不覆盖 manifest 或自动接受新哈希。

## 13. 恢复语义

服务启动时：

- 合法 queued Run 保留并重新进入单并发队列；
- 没有活动 Worker 的 `running` / `cancel_requested` Run 变为 `interrupted`；
- `interrupted` / `failed` / `cancelled` 只能通过 rerun 端点创建新 Run；
- 临时 work 内容不通过 Findings / Evidence API 暴露；
- 已 rename 为 `published/` 但不存在已提交数据库 Run/Artifact/manifest 引用的目录，原子移入 `quarantine/orphan-published/` 并记录诊断；绝不自动收养，超过保留期后才清理；
- 已发布 Artifact 哈希异常时，Run 快照标记 `integrity_status=degraded`；
- ReviewAction 和机器 Finding 不因重启重写。

客户端收到网络错误时先用幂等 key 重试或读取实体状态，不能自行假定创建失败。SSE 断开只需重连，不应创建新 Run。

## 14. 最小契约测试矩阵

| 场景 | 预期 |
|---|---|
| 9706.1 preflight 只传 Report | `422 MISSING_RUN_INPUT`，不生成 plan hash |
| 9706.1 preflight 误传 9706.202 Document | `422 DOCUMENT_ROLE_MISMATCH` |
| 创建 Run 未带 preflight plan hash | `422 RUN_PREFLIGHT_REQUIRED`，不创建 Run |
| preflight 后规则包、组件配置或输入哈希漂移 | `409 RUN_PLAN_CHANGED`，要求重新预检 |
| preflight 中 `coverage.ready=0` | `can_create_run=false` + `NO_EXECUTABLE_RULES`，创建端二次校验返回 `422` 且不创建 Run |
| 运行期所有规则最终均不适用/不支持 | `lifecycle_status=failed`、`failure.code=NO_EXECUTABLE_RULES`、machine overall 为 null |
| 创建 PTR Run | `409 MODE_DISABLED`，无空 pass |
| 9706.1 preflight 生成计划 | planned rules 精确为 10 条 `RECORD61-*` 规则（含结构、数值发现和元数据），不得出现 `REPORT-*` |
| 9706.202 preflight 生成计划 | planned rules 精确为 10 条 `RECORD202-*` 规则（含字段映射、身份和元数据），S48 仅保留 `excluded` 记录，不执行表 3 重算 |
| 成功的对比 Run | RuleExecution、Finding、计数、总体状态和导出只覆盖该模式规则，不包含或聚合 `REPORT-*` |
| `report_self` Run | RuleExecution、Finding、计数、总体状态和导出不得包含 RECORD/PTR 规则或 Finding |
| `report_self` Worker 正常发现 R07 错误 | `lifecycle_status=succeeded` 且 `machine_overall_status=error` |
| Worker 崩溃 | `lifecycle_status=failed`，machine overall 为 null |
| 任一 planned RuleExecution 为 failed/悬空/缺失 | 整次发布失败，不把其余规则结果当完整结果公开 |
| Run 仍在 running/cancel/failed/interrupted | finding IDs、统计和候选 Findings/Evidence 不公开 |
| 主进程重启时有 running Run | 转为 `interrupted`，不发布 work 目录 |
| published rename 后、数据库提交前崩溃 | 启动后隔离 orphan published，永不自动收养 |
| 相同 Idempotency-Key 重传相同文件 | 返回同一 Document |
| 相同 Idempotency-Key 换文件 | `409 IDEMPOTENCY_KEY_REUSED` |
| SSE 从已知 sequence 重连 | 仅补发后续持久事件 |
| 完整 Document 使用 Range 读取并跳到 Evidence | 仍可浏览全部页面；按 document/page/bbox 高亮，不退化为只返回裁剪 |
| Finding 按四种 machine status 切换 | `Qn` 不重排；warning/error/manual/pass 计数互不覆盖 |
| 成功 Run 返回 `finding_counts` | `total/error/manual/warning/pass` 均为非负整数，和 Findings 按机器状态聚合完全一致 |
| 9706.1 两条 ready 规则生成计划 | `required_components` 是含 `report_template_bundle` 在内的 7 个所需组件的精确去重并集，Run `component_versions` 的 ID 集合完全相等 |
| `report_self` 的 R07 只出现“不符合要求”且聚合结论正确 | 发布 `contributes_to_overall=true` 的 warning Finding，machine overall 为 warning |
| `report_self` 的 R07 同一 scope 同时有“不符合要求”和聚合错误 | 发布 warning + error 两条 Finding，分别计数，machine overall 为 error |
| 一个 Finding 有跨页或双文档 Evidence | 按 evidence group 与 sequence 返回，两个完整 PDF 均能定位 |
| Evidence bbox 越界 | 发布失败，不出现在 API |
| 对 manual Finding 录入 123.4 uA | 后端换算并重算 resolved status，机器状态不变 |
| 对 pass/warning/error Finding 提交 ReviewAction | `409 FINDING_NOT_REVIEWABLE`，不写入动作 |
| 两个 Finding 基于同一旧 run review revision 并发提交 | 首个原子成功；第二个 `409 REVISION_CONFLICT`，不落动作、不增加 revision |
| review revision 为 0 | resolved overall 三字段均为 null |
| review revision 增加 | resolved overall 的 revision/计算时间与同一快照绑定 |
| 客户端直接提交 resolved status | `422 READ_ONLY_FIELD_SUBMITTED` |
| 标记源文件无法辨认 | review 可 resolved，resolved status 仍为 manual |
| 撤销复核 | 新增 withdraw 动作，历史动作仍可读取 |
| 客户端提交 `actor_id` / `actor_source` | `422 READ_ONLY_FIELD_SUBMITTED`；成功动作的 actor 由本机会话派生 |
| 防锚定 Finding 在必需观察未完成前读取详情、review 聚合、Evidence 和导出 | 四个表面均不泄露 Report 目标、比较结果或目标 Evidence ID |
| Artifact 哈希漂移 | `409 ARTIFACT_INTEGRITY_ERROR`，不现场重生成 |

这些测试是 API 合同的最低门槛，不等同于业务规则、真实 PDF、视觉定位和全流程验收。
