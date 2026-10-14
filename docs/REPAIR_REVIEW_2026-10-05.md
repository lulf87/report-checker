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

## 2026-10-10 GB 9706.202 项目名称别名修正

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | Record 的“电压、电流或能量的限制”和 Report 的“电压、电流和能量的限制”按已确认模板别名等价比较；两侧原文仍保留在 Finding 和 Scope Ledger 中 | `mvp/full_record_202.py`、`tests.test_full_record_202`、`tests.test_record202_scope_expanded` |
| VERIFIED | 1539、2795、2948 三份 GB 9706.202 结果已重新生成；第 14 项 16 个子行均不再产生项目名称 mismatch | `output/full-records-manual-reduction-20260930-v1/`、`output/full-records-complete-20260930/` |
| VERIFIED | 真实工作台和正式总览已刷新；2795 的错误 Finding 从 21 降为 5，错误 Ledger 从 44 降为 28 | `tests.test_workbench_real`、`output/full-records-manual-reduction-20260930-v1/summary.md` |
| VERIFIED | 修复后全量回归 | `253 tests OK` |

## 2026-10-11 Mac 真实样例双模式复跑

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 1347、1539、2948 的 GB 9706.1 Record 复跑完成；总体分别为 `manual`、`manual`、`error`，Coverage 均守恒，输入 PDF SHA-256 前后不变 | `output/full-records-current-20261011/*/report-record-9706-1/result.json` |
| VERIFIED | 1539、2795、2948 的 GB 9706.202 Record 复跑与当前结果一致；总体分别为 `manual`、`error`、`error`，项目别名修正后的 2795 Finding/Ledger 错误数为 5/28 | `output/full-records-current-20261011/*/report-record-9706-202/result.json` |
| VERIFIED | 真实工作台清单切换到六份当前复跑结果；静态服务 allowlist、工作台契约和静态服务测试通过 | `docs/prototypes/workbench-real.html`、`tests.test_workbench_real`、`tests.test_static_server` |

## 2026-10-11 素材全量扫描与可运行配对复跑

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | `素材/` 共扫描 50 个文件：31 个 PDF（13 份 Report、3 份 GB 9706.1 Record、3 份 GB 9706.202 Record、12 份 PTR）、10 个 expected JSON 和 9 个 `.DS_Store`；全部 PDF 的 SHA-256 已保存 | `output/full-records-all-20261011/inventory.md`、`素材-pdf-sha256.txt` |
| VERIFIED | 六组 Report + Record 配对全部运行完成，六组 Coverage 均守恒，六组输入 PDF SHA-256 前后均 unchanged | `output/full-records-all-20261011/summary.md`、`inventory.json` |
| VERIFIED | 全量测试恢复忽略的 2795 OCR 衍生夹具后通过：253 tests OK；`compileall`、`git diff --check` 通过 | `.venv/bin/python -m unittest discover -s tests` |
| BLOCKED_BY_INPUT | 9 份 Report 没有 GB 9706.1/9706.202 Record，12 份 PTR 仍为 `PTR_NOT_VALIDATED`；不自行伪造配对 | `output/full-records-all-20261011/inventory.md` |

## 2026-10-11 PTR ↔ Report 前置尝试

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 12 组同号 PTR/Report 已执行 `report_ptr` CLI 前置尝试；全部返回 `MODE_DISABLED` / `PTR_NOT_VALIDATED`，未创建输出、未生成 Finding | `output/ptr-report-preflight-20261011/summary.md` |
| VERIFIED | 12 份 Report 均可确定性解析 `QW<year>-<suffix>` 报告编号；PTR PDF 可打开并记录页数，但未凭文本或 OCR 推导 PTR 规则 | `output/ptr-report-preflight-20261011/summary.json` |
| BLOCKED_BY_RULES | `PTR-P01` 现在只有 `0.1.0-draft` 合同版本，仍无可发布 runner；缺少声明范围、引用展开、父子条款映射、覆盖规则和 2–3 份结构差异 PTR 的逐条人工验收集 | `mvp/capabilities.py`、`docs/IMPLEMENTATION_PLAN.md` 阶段 6 |
| INFO | 项目没有独立的 PK 号规则；现有“报告编号”解析只服务 Report/Record 身份核对，不推导 PTR 规则 | `mvp/checker.py`、`mvp/full_record_61.py` |

## 2026-10-11 PTR-P01 禁用态骨架

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 0596、1539、2795 生成 RequirementGraph 候选草案；保存 Report 声明范围候选、PTR 条款候选、引用候选、父子候选边、输入页几何和 SHA-256 | `output/ptr-p01-drafts-20261011/summary.md`、`docs/PTR_P01_RULE_DRAFT.md` |
| VERIFIED | 草案 Finding 契约包含规则版本、reason code、scope、观察、比较字段和 Evidence 坐标，但 `publishable=false`、`findings=[]` | `mvp/ptr_p01.py`、`tests/test_ptr_p01.py`（6 tests OK） |
| BLOCKED_BY_RULES | 未实现声明范围语义匹配、引用表格展开、缺项/范围外判定、正式 Worker 发布；未接入 capability，PTR 仍不可启用 | `mvp/capabilities.py`、`docs/PTR_P01_RULE_DRAFT.md` |

## 2026-10-11 PTR ↔ Report 合同接线

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 固定 `report_document_id → report`、`ptr_document_id → ptr` 一对一输入槽，明确禁止 Record 角色；能力目录、preflight、Run 输入、Worker JobSpec 和前端上传映射共用该合同 | `mvp/ptr_report_contract.py`、`mvp/capabilities.py`、`mvp/run_state_server.py`、`mvp/worker_protocol.py`、`docs/prototypes/workbench-upload.html` |
| VERIFIED | preflight 对真实 0596 Report/PTR 返回 Report 37 页、PTR 39 页及各自 Blob SHA-256；规则计划为 `PTR-P01@0.1.0-draft`，最终以 `PTR_NOT_VALIDATED` 阻止创建 | 本机 8767 preflight 实测；`tests.test_run_state_server` |
| VERIFIED | 禁用态草案结果增加 `contract`、`input_contract`、`result_contract`，Finding 模板补齐机器状态、观察、比较、证据 ID 和复核状态；Evidence 显式保存文档引用、0/1 起算页码、`pdf_points` 和原文锚点 | `mvp/ptr_p01.py`、`tests.test_ptr_p01.py`、`docs/PTR_P01_RULE_DRAFT.md` |
| VERIFIED | 全量回归 262 项通过，`git diff --check` 和 Python 编译检查通过；8765/8767 已重启加载新合同 | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` |
| BLOCKED_BY_RULES | 仍未完成 PTR 语义执行、引用展开、缺项/范围外判定和多份结构差异 PTR 的人工验收，因此没有启用按钮或发布 Finding | `PTR_NOT_VALIDATED` |

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

## 2026-10-13 2948 GB 9706.1 状态语义与跨行笔迹修复

| 状态 | 项目 | 证据 |
|---|---|---|
| VERIFIED | 状态笔迹先按实际点/线段与复选框的交集归属到状态行；没有唯一复选框交集时才回退到质心归属，修复页 17 的 7.2.17 跨行斜线误归 | `mvp/full_record_61.py`、`tests.test_full_record_61.FullRecord61Tests.test_2948_cross_row_status_mark_binds_to_the_next_checkbox_row` |
| VERIFIED | 双横杠或整格斜线识别为 Record“不适用”，与 Report `/` 或 `——` 等价；问题 130 `RECORD61-BODY-S041-E004` 与 `RECORD61-CONCLUSION-S041` 均为 matched | `output/full-records-current-20261011/2948/report-record-9706-1/result.json` |
| VERIFIED | 真正空白的状态框进入 `record_status_blank` error；靠近但未落入复选框的笔迹进入 `status_mark_near_checkbox_unresolved` manual，避免把不规范原始记录自动判成“不适用” | `mvp/full_record_61.py`、`tests.test_full_record_61` |
| VERIFIED | 2948 GB 9706.1 当前 Ledger 为一致 919 / 不一致 5 / 待复核 93 / N/A 37；本轮前结果保存在 `output/backups/status-semantics-pre-fix-20261013/` | `output/full-records-current-20261011/summary.md`、备份目录 |
| VERIFIED | Q62、Q980、Q982 的整格注销/斜线语义已收敛为通过；Q574、Q964 的空白状态为 error；Q741 为 Report 行映射缺失，Q941/Q952/Q992 为近框笔迹人工复核；Q1011–Q1013 为数值双通道识别未收敛 | `docs/TASK_STATUS_2026-10-13.md`、当前 `result.json` |
