# 修复后工程审查记录（2026-10-05）

基线为 `ea1f1c2`；本轮修复随后完成了本地数据库迁移和联调，代码在提交前仍保留于当前工作树。

## 结论

当前可以进入提交和发布前验证。Report 自检、静态工作台和持久化 Run 链路已通过本地验证；9706.1 的未验证模板变体会发布为 `manual` 输入变体结果，不再伪装成 Worker 崩溃。

## 2026-10-06 继续修复

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 根路径 `/`、短路径和 `/docs/prototypes/...` 历史路径均可由安全静态服务器提供，旧 PDF.js 依赖保持可加载 | `tests.test_static_server`（含实际 HTTP 集成） |
| VERIFIED | 9706.1 状态库存 `813/native812/alternate1` 被转为结构化 `manual` Finding；其余 9 条规则记录 `unsupported` 与稳定原因码 | `tests.test_input_variant`；真实 Report + Record 临时持久化 Coordinator Run：`succeeded/manual` |
| VERIFIED | 数值约束支持显式括号区间、阈值、单位换算、精度和显式极性；无边界语义的 `~`、`至`、`±` 保持 `manual` | `tests.test_record_full_numeric_semantics`（7 条） |
| VERIFIED | 原始 SQLite 已备份并从 v7 迁移到 v8；关键表数量保持 `20/21/20/236/192/3055/76/0/22`，`quick_check=ok`、外键检查为空 | `output/backups/pre-v8-*/migration-report.json` |
| VERIFIED | 全量测试（范围补齐前） | `232 tests OK` |

## 2026-10-06 范围补齐与界面修复

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 9706.1 状态框区分选中、空白、注销线、歧义和异体字形；字段级 Ledger 包含项目、条款、要求、建议、条件、单位和结果；缺失、额外、歧义映射使用独立原因码 | `tests.test_record61_scope_expanded`、`tests.test_full_record_61` |
| VERIFIED | 9706.202 发布映射库存，明确 38 项、Record 逻辑行和 Report 物理行的缺失、额外、歧义情况；未映射 Report 行进入 manual scope Ledger；数值接受标准进入对象级 Ledger | `tests.test_record202_scope_expanded`、`tests.test_full_record_202` |
| VERIFIED | 上传工作台总体 warning 徽标；真实工作台 warning 状态、筛选、计数、颜色和 Report self scope ledger 校验 | `tests.test_workbench_upload`、`tests.test_workbench_real` |
| VERIFIED | 真实工作台 2795 Report self 静态结果按当前规则重新生成：pass 7 / manual 4 / error 1；旧目录已移入 `output/backups/report-self-legacy/` | `output/report-self-unified-20260930-v3/2795/result.json` |
| VERIFIED | 范围与界面补丁局部回归 | `79 tests OK` |
| VERIFIED | 最终全量测试 | `246 tests OK` |

## P0/P1 关闭项

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | `succeeded` 规则不得以零 Finding 发布；`not_applicable/unsupported` 必须零 Finding 和非空 `reason_code`；全不可执行仍拒绝 | `tests.test_run_store`、`tests.test_worker_protocol` |
| VERIFIED | `cancel_requested` 与 Worker 失败、发布事务 CAS 竞态均收束为 `cancelled`，不覆盖为 `failed` 或长期悬挂 | `tests.test_cancel_race`（3 条并发/竞态测试） |
| VERIFIED | Worker Evidence/Artifact 路径必须在 output 目录内；文件存在、大小、SHA-256 进入原子 `artifact-manifest.json` | `tests.test_worker_protocol`（完整/缺失 Artifact） |
| VERIFIED | Evidence 绑定 Run 输入的 `document_id`、Blob SHA、页码和页面几何；bbox 必须在页面范围内 | `tests.test_run_store`、`tests.test_document_store` |
| VERIFIED | ReviewAction 按动作校验观察；派生状态由服务端计算；撤销只接受同 Finding 的当前有效动作；actor 不信任客户端头 | `tests.test_review_actions`、`tests.test_run_state_server` 静态断言 |
| VERIFIED | SQLite v6→当前版本迁移记录、traceability 回填、Review revision 回放、未来版本/缺版本/坏数据拒绝 | `tests.test_schema_migrations`（4 条） |
| VERIFIED | 队列 Run 启动时可重新提交；绑定/提交异常会显式失败并留下事件，不留无主 queued Run | 临时 SQLite/blob/output + 独立子进程实测：queued→succeeded，Finding 12、Evidence 91、manifest 存在 |
| VERIFIED | 静态工作台仅回环 + allowlist，阻断路径穿越、SQLite、`.git`、`.aws`、未列出的 output | `tests.test_static_server`、`tests.test_workbench_real` |

## 模式边界

- `report_self`：已启用，Report 自检。
- `report_ptr`：`MODE_DISABLED / PTR_NOT_VALIDATED`，本轮及后续禁用。
- `report_ptr_report`：显式列入能力目录，沿用 `MODE_DISABLED / PTR_NOT_VALIDATED`，不执行组合模式。
- `report_diff`：显式列入能力目录，`MODE_DISABLED / DIFF_NOT_SPECIFIED`；输入角色和规则计划未冻结，不从 Record 或 PTR 推导。

## 未关闭项和偏差

| 优先级 | 状态 | 风险与最小后续动作 |
|---|---|---|
| VERIFIED | HTTP Host/Origin/Sec-Fetch/CSRF/过期 token、actor spoof、取消和本机 CORS | `tests.test_run_state_server` 12 项；能力服务 3 项；正式 loopback 审批执行通过 |
| VERIFIED | 真实 Worker 子进程和 queued 跨进程恢复 | 临时持久库 + 1539 Report：子进程恢复并 succeeded，`finding_counts={error:0,manual:4,pass:8,warning:0}` |
| P1 | VERIFIED | 原始持久化库已在备份后迁移到 schema v8；备份位于 `output/backups/pre-v8-*/run-state-v7.sqlite3`，迁移报告记录了计数和完整性检查。 |
| P2 | VERIFIED（文档已校准） | `/version`、诊断、单页图片、rerun、全局 Run 列表、SSE、业务幂等、导出仍是规划；API 文档已明确未实现，不能按已交付宣称。 |
| P2 | PARTIAL | Record 9706.1/202 的 S25/S26/S30–S33/S38/S42/S43 仍需字段、发现范围或判定细节扩展；范围表已改为 `partial`。 |
| AUTHOR_INPUT_NEEDED | 阻断启用 | `report_diff` 的两输入角色、规则计划和结果语义尚未由需求冻结；保持禁用，不自行推导。 |

## 验证记录

- `.venv/bin/python -m unittest tests.test_schema_migrations tests.test_document_store tests.test_run_store tests.test_worker_protocol tests.test_cancel_race tests.test_review_actions tests.test_static_server tests.test_workbench_real tests.test_workbench_contract tests.test_workbench_upload tests.test_comparison_scope tests.test_capabilities.CapabilityCatalogTests tests.test_run_modes.FourModeDispatcherTests tests.test_run_state_server tests.test_capabilities.CapabilityServerTests`：**100 passed**。
- `FourModeDispatcherTests`：**6 passed**；`ReportSelfRunnerTests`：**2 passed**。
- `compileall` 与 `git diff --check`：通过。
- SQLite backup API 独立副本：v7→v8，原计数 `20/21/20/236/192/3055/0` 保持不变，`quick_check=ok`、`foreign_key_check=[]`；故障副本回滚后版本仍为 v7、迁移记录为 0。
- 网络绑定测试已通过正式审批在 loopback 临时端口运行；数据库迁移前后均保留了可恢复备份。真实工作台文件包含本机私有样例清单，继续由 `.gitignore` 排除；公开仓库提交的是上传工作台和规则/服务代码。
