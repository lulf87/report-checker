# 前后端实施计划

> 状态：工程实施蓝图 v0.1  
> 本文定义未来怎么做、怎样验证和何时停止；当前轮次只交付设计，不创建正式前后端工程。

> **当前范围修订（2026-10-03）**：正式比对范围以 [docs/COMPARISON_SCOPE.md](COMPARISON_SCOPE.md) 和 `mvp.capabilities` 当前目录为准。Report 自检现为 12 条独立规则（R01–R11，含 R07-B）；9706.1 现启用身份、状态、数值、百分比、序列结论、范围、结构、模板外数值发现、元数据和不符合警示共 10 条规则；9706.202 现启用编号、状态、数值、百分比、图例、结构、字段范围、身份、元数据和不符合警示共 10 条规则，并在正文单元严格比较项目名称。S01–S47 纳入，S48（表 3 独立内容）第一阶段排除。本文后续较早的“未拆分规则粒度/28 条 Finding”数字属于历史实施基线，不能作为当前范围或当前自动通过数量。

## 1. 已有基础与可信边界

当前目录已有一个用于路线验证的无 LLM MVP：

- `mvp/checker.py`：PyMuPDF、表格/Ink、Apple Vision、Tesseract 和确定性规则的单文件验证实现；
- `mvp/run_samples.py`：四个固定样本的批量入口；
- `tests/`：纯规则和固定样本集成测试；
- `output/mvp-*`：四份机器结果及证据图；
- `output/independent-audit-gpt56-sol-ultra-20260928/`：对现有合并 MVP 的 28/28 条唯一语义 Finding 的独立审核。

该 MVP 可以证明：

- 已实现 Report、9706.1、9706.202 目标链在四份固定样本上判断正确；
- 传统 PDF 解析、Ink 几何和局部 OCR 能承担主流程；
- 9706.1 手写数字在识别链不一致时应保守退出到人工复核。

该 MVP 不能证明：

- 未知 PTR 格式已解决；
- 未知模板或所有规则具有通用性；
- 当前单文件脚本、结果 JSON 或 HTML 可以直接成为产品接口；
- 产品已具备并发、恢复、安全、数据库、前端或正式版本迁移能力。

## 2. Step 0：先挑战范围

第一版不应同时解决“完整 PTR 理解、所有 Report 规则、所有 Record 模板、批量处理和多人审批”。这会把解析不确定性、产品状态管理和基础工程风险混在一起，无法判断失败来自哪里。

因此按以下顺序实施：

1. 先冻结通用数据契约和四样本回归基线；
2. 再把三个已验证模式产品化；
3. 完成人工复核、恢复和导出闭环；
4. 最后单独开发 PTR，直到验收前保持 `disabled`。

第一阶段对外显示四个模式，但真正可运行的是：

- `report_self`；
- `report_record_9706_1`；
- `report_record_9706_202`。

`report_ptr` 只能展示能力说明和“尚未启用”，后端创建 Run 必须返回稳定错误码，不能生成零 Finding 的假通过结果。

## 3. 目标工程结构

建议在正式实施时新建以下结构；现有 `mvp/` 保留为回归参考，不在原位继续堆产品代码。

```text
backend/
  app/
    main.py                 # FastAPI 装配和静态资源托管
    api/                    # /api/v1 路由、请求/响应模型
    domain/                 # Case/Run/Finding/ReviewAction/ReviewState 领域类型与聚合规则
    services/               # 用例编排、预检、发布、复核重算
    persistence/            # SQLite repositories 和迁移
    storage/                # blob/run artifact 原子读写
    capabilities/           # ModePlan 与规则包注册
    worker_protocol/        # JobSpec、事件、WorkerResult Schema
  worker/
    main.py                 # 独立子进程入口
    extractors/             # PDF文字、表格、Ink、局部OCR
    rules/                  # Report / 9706.1 / 9706.202 / PTR
    evidence/               # 坐标、裁图和清单生成
  tests/
frontend/
  src/
    api/                    # 类型化客户端和 SSE 适配
    app/                    # 路由、Shell、全局边界
    features/               # cases/runs/findings/reviews/rules/system
    components/pdf/         # PDF.js viewer 和 EvidenceOverlay
    components/status/      # 状态文案和图标
    styles/                 # token、网格、响应式、打印样式
  tests/
schemas/
  worker-job.schema.json
  worker-event.schema.json
  worker-result.schema.json
data/                       # 运行时生成，默认不纳入源码
```

正式目录名可在项目初始化时微调，但模块边界不得退化为“HTTP 入口直接调用当前 `run_sample()`”。

## 4. 数据流与信任边界

```text
浏览器
  │ 选择本地 PDF / 查看证据 / 提交观察事实
  ▼
FastAPI 父进程（唯一数据库写者）
  ├─ 输入流式哈希、PDF预检、Blob原子保存
  ├─ SQLite事务、Case/Document/Run状态
  ├─ Job Coordinator（并发数 1）
  ├─ Worker输出Schema与证据完整性校验
  └─ SSE读取持久化RunEvent
          │ JobSpec（只含不可变输入与版本快照）
          ▼
独立 Python Worker 子进程
  ├─ PDF原生文字、矢量表格、Ink
  ├─ Apple Vision/Tesseract局部候选
  ├─ 确定性规则与单位/精度过程
  ├─ work/ 临时制品
  └─ JSONL事件 + WorkerResult
          │
          ▼
父进程验证后原子发布 published/
```

信任规则：

- 浏览器输入不可信；后端重新校验所有角色、ID、枚举和复核值；
- Worker 输出也不直接可信；父进程验证 Schema、Run ID、输入哈希、证据引用和制品哈希；
- 只有父进程写 SQLite；Worker 不持有数据库写连接；
- 原始 Blob 永不交给规则代码以写方式打开；
- 前端不得从摘要文本或文件名推导状态与证据来源。

每个 Run 还必须保存 `rule_executions` 账本。账本在开始时由静态 ModePlan 生成，逐条记录计划 Rule 的 `pending | running | succeeded | not_applicable | unsupported | failed`、版本、开始/结束时间和原因码。发布前必须证明每个计划 Rule 都有终态；否则某条检查可能因代码遗漏而“消失”，Run 却被错误发布为完成。

```text
ModePlan
  ├─ report_self              = report_parse + report_rules
  ├─ report_ptr               = report_parse + ptr_parse + ptr_expand + ptr_rules [disabled]
  ├─ report_record_9706_1     = report_parse + record61_parse + record61_rules
  └─ report_record_9706_202   = report_parse + record202_parse + record202_rules
```

第一版使用显式静态映射，不开发通用 DAG、动态插件发现或规则 DSL。`report_parse` 是比对规则取得 Report 操作数和证据的依赖，不会在比对 Run 中触发 `report_rules` 或生成 REPORT-* Finding。

## 5. Run 与发布状态机

```text
POST create run
    │
    ▼
 queued ──cancel──► cancelled
    │ coordinator取出
    ▼
 running ──cancel request──► cancel_requested ──安全边界──► cancelled
    │
    ├─ 父进程观察到Worker/协议/发布故障 ─► failed
    ├─ 父进程或主机中断；启动发现孤立运行态 ─► interrupted
    │
    └─ result + artifacts完整
          ▼
      validate
          ├─ 不通过：failed，work/保留诊断但不发布业务结果
          └─ 通过：原子rename → published/ → succeeded
```

内容状态只在 `succeeded` 后有效。机器找到确定性错误时仍然 `succeeded + machine_overall_status=error`。系统故障不能伪装成内容 `error`，依赖缺失也不能伪装成 OCR 不确定的 `manual`。

## 6. 当前 MVP 的迁移策略

迁移原则是“先封装并证明等价，再改善规则”，不能在同一个提交里同时搬代码、改 Schema、改阈值和改业务结论。

### 6.1 可作为候选迁移的算法事实

- Report 表格识别、项目聚合和跨页序号观察；
- R07 结论聚合的纯函数；
- 9706.1 状态列聚合、数值单元格定位、单位换算和精度步骤；
- 9706.202 图例核实、Ink 归格和符号比较；
- 证据裁图所需的 PDF 坐标观察。

每一项仍需：拆成纯函数或受控服务、补类型、补源文档身份、补失败原因码、对四样本做等价回归。

### 6.2 不可直接暴露或复用的部分

| 当前部分 | 原因 | 替代方案 |
|---|---|---|
| `run_sample()` | 固定样本配置、直接清理输出目录、同步长任务 | 新 RunCoordinator + 不可变 RunInput + Worker JobSpec |
| `SAMPLE_CONFIGS` | 把样本页码和业务能力硬编码在程序里 | 版本化 extractor/template capability；未知模板保守退出 |
| 任意形状的 `details` | 无正式 Schema，前端只能猜结构 | Finding observation/normalization/comparison 的判别联合类型 |
| 当前 Evidence | 已保存 Run、规则执行、源文档哈希和输入快照；仍缺公开 Document ID、坐标空间元数据与 Artifact manifest | 正式 Evidence Schema + Artifact manifest |
| 当前 HTML renderer | 展示、规则和结果数据耦合 | React 前端；HTML 导出由已发布结果快照生成 |
| `mvp/reviews/*.json` | 人评与机器结果边界不够正式 | 追加式 ReviewAction + 后端重算 |
| 输出目录删除重建 | 可能覆盖历史并破坏恢复 | 每 Run 独立目录、work/published 分离、原子发布 |

### 6.3 等价迁移门槛

每迁移一个规则包，必须同时满足：

1. 四个适用样本的原始输入 SHA-256 与基线一致；
2. Finding 的业务状态、关键观察、异常定位和总体聚合与已审核基线等价；
3. 新 Evidence 增加来源和哈希，但不能丢失旧证据能够证明的事实；
4. 不适用样本明确跳过，不生成假 Finding；
5. 结果差异必须由规则变更记录解释，不能静默更新 golden。

## 7. 分阶段实施

### 阶段 0：冻结契约和回归基线

目标：在写 UI 前先冻结 ModePlan、Worker 协议和 Finding/Evidence/ReviewAction/ReviewState 类型。

工作：

- 定义 Case、Blob、Document、Run、RunInput、Finding、Evidence、Artifact、RunEvent、ReviewAction；
- 固定三条状态轴和总体聚合；
- 冻结 RuleCatalog、SystemDiagnostics 和输入相关 RunPreflight/Scope Ledger 契约；
- 固定 Run 的 component versions/config hashes，以及 resolved overall 的 review revision/计算时间语义；
- 建立 JSON Schema 与生成的 Python/TypeScript 类型；
- 将四份现有 result.json 转成只读回归夹具，另保留输入哈希；
- 定义 capability 响应，PTR 为 `disabled`；
- 记录当前环境版本和 OCR 后端可用性。

退出门槛：Schema 往返验证通过；Python/TypeScript 枚举一致；preflight plan hash 漂移、组件不可用、模式 Finding 隔离和 PTR disabled 契约测试通过；四样本基线可枚举 28 条唯一语义断言；独立模式回归计划明确为 10 个 Run、按当前未拆分规则粒度共 28 条 Run Finding；规则级旧 Finding 若为支持逐项 Q 而拆分，已有一对多映射且新的精确计数已经单独冻结；任何字段歧义已解决。

### 阶段 1：持久化、制品库与运行框架

目标：先证明任务能安全创建、运行、取消、重启恢复和原子发布。

工作：

- SQLite 迁移和 repository；
- 内容寻址 Blob 存储与流式 SHA-256；
- PDF 预检；
- 单并发协调器和独立 Worker；
- JSONL 事件、SSE 与 `Last-Event-ID`；
- `work/` → 校验 → `published/` 原子发布；
- 启动时把遗留 `running` 对账为 `interrupted`。
- 启动时隔离 rename 成功但数据库事务未提交的 orphan `published/`，绝不自动收养。

退出门槛：使用一个无业务规则的测试 Worker 完成成功、失败、取消、崩溃、应用重启、磁盘写失败和 SSE 重连测试；不存在半发布结果。

### 阶段 2：迁移三个已验证检查模式

目标：在正式 Run 框架中复现当前机器结果。

顺序：

1. `report_self`；
2. `report_record_9706_1`；
3. `report_record_9706_202`。

工作：

- 将提取和规则分层；
- 生成正式 Finding/Evidence；
- 对比模式复用 Report IR 作为比较输入，但只生成该模式的 RECORD/PTR Finding；
- 引入规则包版本、模板识别状态和明确的退出原因；
- Evidence 发布前验证源文档、页码、bbox 和 artifact hash；
- 暂不实现人工录入，只保留 `manual` 结果。

退出门槛：完成 4 个 `report_self`、3 个 `report_record_9706_1`、3 个 `report_record_9706_202`，共 10 个独立 Run；按当前未拆分规则粒度物化 28 条 Run Finding，并逐项映射回 28 条已审核语义断言且无回归。`report_self` 不出现 RECORD/PTR Finding，对比模式不出现 REPORT-* Finding，三个模式绝不串跑；PTR 后端仍拒绝。规则拆分会改变物化数时，先冻结并审查新的预期计数。

### 阶段 3：前端基础闭环

目标：完成新建、进度、结果总览和文档优先的证据工作台。

工作：

- React/TypeScript/Vite 基础工程；
- 同源 API 客户端、规则目录、系统诊断、Run preflight、SSE 缓存更新与断线恢复；
- 四模式卡片、固定角色文件槽和预检页；
- Scope Ledger；
- Run 记录、五个状态选项卡、独立状态统计和稳定 `Qn` Finding 筛选；
- 主工作台常驻极窄范围摘要，明确“完整覆盖”或“部分覆盖 / 未覆盖”；规则版本、统计和覆盖明细放入按需任务信息层；
- 紧凑常驻上下文条，以及默认收起、一次只打开一个的检查项抽屉和判定/复核抽屉；抽屉采用覆盖方式，不得挤压、卸载或重建完整文档 viewer；
- 本地 PDF.js 完整文档查看器：Report 自检加载完整 Report，对比模式加载完整 Report + PTR/Record；支持全页浏览、双窗格、页码跳转、坐标高亮、Evidence group 上一处/下一处导航，裁剪仅作放大辅助；查看器须分开显示当前实际浏览页与当前 Q 定位页，并由 PDF.js 页变更事件持续同步实际页；完整可浏览使用 Range + 视区懒渲染实现，含过期渲染取消、远端页 canvas/text layer 释放和双窗格内存回归测试；
- 规则与系统状态页；
- 窄屏降级和键盘可访问性。

退出门槛：SAMPLE-D 与 SAMPLE-C 的全部关键 Finding 可从稳定 `Qn` 列表打开到完整源 PDF 的正确角色、正确物理页和正确高亮，同时仍能浏览其他页面；用户自由翻页后实际页码实时更新且 Q 定位页仍独立保留；`error/manual/warning/pass` 四个选项卡计数和筛选互不覆盖，切换时不替换当前 Q 或重建查看器；非空筛选不含当前 Q 时保留上下文并提示，计数为 0 时只清空 Q 列表、隐藏 Q 定位和判定详情而不隐藏完整文档；检查项/判定抽屉开合和窄屏文档页签切换前后 viewer identity、双方页码、滚动、缩放、旋转与当前 Q 均保持；刷新不丢 Run；PTR 不能开始；页面无整体横向溢出。

### 阶段 4：追加式人工复核和确定性重算

目标：闭合 9706.1 手写数值的安全复核流程。

工作：

- ReviewAction API、审计字段和并发版本检查；
- 只允许对可复核的 `manual` Finding 提交受控观察结构；
- 先录后比的两阶段界面；
- 单位解析、聚合、精度处理使用同一规则服务；
- `machine_status`、`review_resolution`、`resolved_status` 并列展示；
- 新复核记录追加保存，旧记录不覆盖。

退出门槛：SAMPLE-D 输入两个 `123.4 μA` 后，9706.1 对比 Run 的数值 Finding 重算一致；该 Run 只包含 `RECORD-*` 规则，机器总体和复核后总体均按该 Run 的规则计算。Report 项目 132、140 的 `REPORT-R07` 错误只在单独的 `report_self` Run 验证，不进入对比 Run 的 Finding、计数或总体状态。提交前界面和 API 响应均不泄露 Report 目标值到录入步骤；无法辨认时保持 `indeterminate/manual`。

### 阶段 5：导出、诊断和恢复

目标：让一次运行可以脱离浏览器复核和归档。

工作：

- 从 published 快照生成 JSON 和自包含/目录式 HTML 导出；
- 导出清单含 Schema、规则版本、源文件哈希和 artifact hash；
- 系统诊断页；
- 数据库备份/恢复说明和只读完整性检查；
- 旧 Schema 只读迁移策略；
- 操作日志和错误包导出。

退出门槛：导出与 API 快照一致；断开服务后 HTML 仍可浏览已嵌入证据或明确携带完整目录；恢复后历史机器状态、复核链和哈希不变。

### 阶段 6：PTR 单独实现与启用门槛

目标：完成“Report 声明范围 → PTR 引用展开 → Report 正文覆盖”。

工作：

- 从 Report 明确提取声明范围；
- 将 PTR 正文、前文、附录、表格引用展开为 RequirementGraph；
- 区分父级检验项目与子级标准要求；
- 支持一对多、多对一映射，但不接受未经确认的同义改写、受控等价或编号点号宽松化；
- 对格式未知或引用循环保留证据并进入人工复核；
- 建立多份真实 PTR 的独立验收集。

启用门槛：至少 2–3 份结构显著不同的真实 PTR 完成逐条人工对照；声明范围内缺项、范围外条款、引用表格、父子条款和中间缺项均有测试；能力从后端配置显式切换为 `enabled`。在此前不得启用按钮。

### 阶段 7：发布前硬化

目标：形成可交付的本机版本，而不是宣称“通用生产系统”。

工作：

- 依赖锁定、许可证清单和可重复构建；
- macOS 安装/启动方式、端口占用和退出处理；
- 恶意/异常 PDF、路径穿越、HTML 转义和大文件压力测试；
- 数据迁移、备份和卸载边界；
- 完整回归、可访问性和人工验收记录。

退出门槛：发布清单全部通过，残余能力边界在 UI、文档和导出中一致；不使用“所有报告均可自动检查”等超出证据的表述。

## 8. 测试图

```text
纯规则单元测试
  ├─ 原文保真/占位符/严格边界
  ├─ 状态聚合
  └─ 单位与精度
        │
提取器契约测试
  ├─ text/table/Ink/bbox
  ├─ OCR候选与保守退出
  └─ Evidence来源与坐标
        │
Worker协议与故障测试
  ├─ Schema/事件顺序/取消
  ├─ 崩溃/重启/原子发布
  └─ artifact hash
        │
四样本真实PDF回归
  ├─ SAMPLE-B/SAMPLE-D/SAMPLE-C/SAMPLE-A
  ├─ 28条唯一语义断言等价
  └─ 10个独立Run / 当前规则粒度28条Finding完整且模式隔离
        │
API集成测试
  ├─ capability/rules/diagnostics/preflight/上传/幂等/SSE
  ├─ plan hash漂移/component snapshot/resolved revision
  ├─ PTR拒绝/权限边界
  └─ 追加式复核
        │
前端组件与E2E
  ├─ 状态轴/路由/刷新恢复
  ├─ 五状态tab/Q序号稳定性
  ├─ 完整PDF/坐标/跨页/双文档/Evidence group
  ├─ 辅助抽屉/任务信息/文档页签不重建viewer或丢浏览状态
  ├─ 先录后比
  └─ 键盘/200%/窄屏
```

Golden 不是可随意重录的截图。任何业务状态变化必须对应：规则版本变化、用户确认依据、差异报告和独立复核。

## 9. 验收矩阵

| 场景 | 必须观察到的结果 | 禁止结果 |
|---|---|---|
| SAMPLE-D / Report + 9706.1 | 9706.1 对比 Run 成功；数值为 manual；复核数值一致后该 Run 的 resolved 状态按 `RECORD-*` Finding 重算；Report 项目 132/140 的 R07 error 仅在单独的 `report_self` Run 中验证 | 人工复核把机器 manual 改写成 pass；把 `REPORT-R07` 注入对比 Run 或据此改变其总体状态 |
| SAMPLE-C / Report + 9706.202 | 项目 3 error；24/24 页编号明确空白；`201.15.101.9` 确定性不一致 | 将空白描述成 OCR 失败；提供人工“忽略错误”按钮 |
| 完整文档与 Q 定位 | Report 自检可浏览完整 Report；对比模式始终保留 Report + PTR/Record 两个文档槽；点击 Q 跳到全部相关页/bbox；切状态 tab 只筛 Q 而不重建查看器或丢失页码/滚动/缩放；检查项/判定抽屉开合、任务信息展开和窄屏文档页签切换保持 viewer identity 与双方浏览状态；当前 Q 不在非空筛选中时提示但保留上下文，零项筛选只隐藏 Q 定位和详情；裁剪只作辅助 | 只显示裁剪图；单侧 Q 使另一文档槽消失；辅助面板或 tab 操作重建 PDF、丢浏览状态、更换 Q 或使 Q 重编号；零项筛选卸载文档；warning 被合并到 error/pass |
| SAMPLE-B / 9706.1 | 结构与状态链可自动裁决；手写值未达门槛时 manual | 用 Report 值反推 Record；因肉眼可读而冒充自动通过 |
| SAMPLE-A / 9706.1 | 正确选择适用数值页/格；识别不足时 manual | 占位图触发错误数值比较；缺证据仍 pass |
| PTR 第一阶段 | capability 为 disabled；前端不可开始；API 明确拒绝 | 创建成功、零 Finding、overall pass |
| 父进程存活时 Worker 崩溃 | Run 为 failed；无 published 结果 | 生命周期 succeeded；展示旧或半成品结果 |
| 应用或主机中断后重启 | 启动对账将孤立运行态标记 interrupted | 原地假装继续；误标为内容 error |
| SSE 断线 | Run 继续；重连后快照和事件一致 | 自动重建重复 Run；断线即取消 |

## 10. 失败模式与处理

| 失败模式 | 系统行为 |
|---|---|
| 非 PDF、损坏或加密 PDF | 预检拒绝，保留明确原因，不创建 Run |
| 必需角色缺失或角色冲突 | 请求校验失败，不猜测文件角色 |
| OCR 后端未安装/不可调用 | capability 或 Run 前置检查失败；不能伪装为内容 manual |
| preflight 后规则、组件配置或输入发生漂移 | 创建 Run 返回 `RUN_PLAN_CHANGED`，要求刷新 Scope Ledger |
| OCR 正常运行但候选互相矛盾 | Finding 为 manual，保存全部候选和裁图 |
| 未知模板或单元格归属不可靠 | 保守 manual；若该规则本来就在声明的未覆盖范围则记 `unsupported`，不能静默略过 |
| 父进程观察到 Worker 非零退出 | 标记 failed，保留事件与 work 诊断；用户显式 rerun |
| 应用/父进程/主机退出后恢复 | 将遗留 running/cancel_requested 标记 interrupted；用户显式 rerun |
| 磁盘写满或无权限 | Run failed；不发布；原始 Blob 若已原子保存则保持可用 |
| WorkerResult Schema 错误 | 拒绝发布并记录协议错误 |
| published rename 后数据库事务失败 | 启动审计隔离 orphan published，永不自动纳入历史结果 |
| Evidence 文件缺失或哈希不符 | 拒绝发布，不能只隐藏图片继续显示 pass |
| SSE 断线 | 只影响实时展示；Run 不取消；按事件序号恢复 |
| 数据库短暂锁定 | 有限退避重试；超限后失败并保留诊断，不无限阻塞 |
| Blob 哈希复核失败 | 标记存储完整性故障，禁止后续运行 |
| 取消请求 | Worker 在安全检查点退出；不得留下 succeeded 半结果 |
| 导出 HTML 含源文本 | 统一转义，禁止把 PDF 文本当 HTML 执行 |

## 11. 性能与资源边界

第一版以确定性和可恢复性优先，不追求并行吞吐：

- Worker 并发固定为 1；多 Run 排队；
- SQLite 不存 PDF、裁图或大段二进制；
- Blob 按内容哈希去重；提取缓存唯一键固定为 `(blob_sha256, extractor_bundle_hash, template_id, template_version, extraction_kind)`，五列均为 `NOT NULL`，无模板时使用受控 `__none__` 哨兵，任一提取器、模板、配置或产物类型变化都生成新记录；
- PDF.js 只渲染当前页和邻页，及时释放离屏 canvas；
- 后端按页/阶段流式处理，避免一次把整份 PDF 渲染进内存；
- SSE 事件做持久化序号，前端不保留无限日志；
- 不知道总页内工作量时不显示虚假百分比。

不在设计阶段捏造“每份多少秒”的 SLA。阶段 0 先记录四份现有最大样本的页面数、文件大小、峰值内存、各阶段耗时和制品体积，再据实冻结：

- 单文件字节上限；
- 单文件页数上限；
- Run 总页数上限；
- 事件和制品保留策略；
- 超限时的明确预检错误。

最低工程验收是：当前最大真实组合能在目标 Mac 上完成，不出现失控内存增长；检查期间 UI 可继续查看历史结果；刷新后可恢复当前 Run 状态。

## 12. 安全与本机边界

- Uvicorn 单 worker，只绑定 `127.0.0.1`；
- 前后端同源，禁止任意 CORS；
- 启动时生成会话级 CSRF token，所有写请求验证；
- 文件名只作显示元数据，存储路径完全由内部 ID/哈希生成；
- 防止 `..`、绝对路径、符号链接和 Content-Disposition 注入；
- 校验 PDF 头、解析结果、加密状态和配置化资源限制；
- API 只允许访问数据库已登记且属于该 Case/Run 的文件；
- 导出与日志默认不发送到网络；
- 原始 PDF、OCR 和复核记录都留在本机；
- 日志不记录完整文档正文，只记录必要 ID、阶段、原因码和安全摘要。

## 13. 明确不在第一版范围

- 账号、角色、登录、电子签名和审批流；
- 云端同步、远程访问和多人协作；
- Redis、Celery、PostgreSQL、Docker 或微服务；
- 批量目录扫描、多 Run 并发和夜间队列；
- 在工具内编辑 Report、PTR 或 Record；
- 一键忽略确定性错误或人工直接选择“通过”；
- LLM 聊天、Agent 判定、在线标准检索；
- 专门的手写“不符合/不适用”中文识别；
- 深色模式；
- 手机端复杂双 PDF 复核；
- 9706.202 表 3；
- 未完成验收前的 PTR 自动检查；
- 把四种模式合并成一次“一键全部检查”。

## 14. 正式实现的完成条件

只有以下项目全部具备证据，才能说第一版实现完成：

1. 三个启用模式端到端运行并独立保存；
2. 四份真实样本回归与已审核 Finding 等价；
3. Case/Document/Run/Finding/Evidence/ReviewAction/ReviewState 的 Schema 和数据库迁移稳定；
4. Run 成功、内容错误、人工待复核和系统失败在 UI/API 中不混淆；
5. PDF 证据能定位到正确源文件、页码和坐标，制品哈希完整；
6. 完整 PDF 可浏览；五状态选项卡和稳定 Q 序号可从 Finding 精确跳转并高亮所有相关文档位置，裁剪不替代全文；
7. 9706.1 先录后比可工作，且不改写自动状态；
8. 取消、崩溃、重启、SSE 重连、磁盘失败和证据缺失均有测试；
9. JSON/HTML 导出与发布快照一致；
10. PTR 始终明确 disabled，直至其独立验收完成；
11. 规则目录、系统诊断与输入相关 preflight 能驱动前端，不依赖硬编码规则版本或组件状态；每个可创建计划的规则依赖组件精确去重并集必须与 Run 复现快照完全闭合；
12. 使用文档明确列出已覆盖、未覆盖和真实验证范围。

完成这些条件后仍应称为“本机第一版”，不能据此声称覆盖所有未知格式。
