# PTR 与 Report 核对验证报告

日期：2026-10-24
分支：`codex/ptr-report-comparison`
规则草案：`PTR-P01@0.5.0-draft`

本报告早期回归曾使用 `0.4.0-draft`；当前可复核结果、验收门和正式能力目录统一以 `0.5.0-draft` 为准，历史 Run 仍保留原版本快照。

## 已验证的执行链

当前比较入口按以下顺序运行：

1. 读取 Report 第三页的 `ScopeSet`，展开纳入、排除和“仅检”范围。
2. 按 PDF 页面坐标读取 PTR 2.x 条款；条款保留父子关系、原文、页码、bbox、SHA-256 和提取方式。
3. 按 Report 表格物理列读取“标准要求”，包括合并的第二要求列；检验结果列不会混进要求文本。
4. 对文本、数值、单位、标准/表格引用和覆盖关系执行规则比较。
5. 表格引用如果没有双方可确认的行列矩阵，状态为 `manual`；OCR 候选也只能为 `manual`。
6. `rules+llm` 只生成文本歧义队列；表格结构不足、数值、单位、范围和引用硬差异不会被送入可清除文本结论的队列。LLM 引文必须逐字存在于原文，且不能修改数值、单位、范围或引用。
7. 覆盖审计会显式记录声明范围中没有任何双方节点的 `SCOPE_ITEM_UNACCOUNTED`；表格/附录多候选、缺失绑定和循环引用进入 `reference_audit` 并保持人工复核。

本轮补充了内联条款拆分：当 PDF 把子条款写成 `2.10.1.2.1，…`、`2.10.1.2.2. …`、`2.11.4碰撞保护` 或独立行 `a)`/`b)` 时，PTR 和 Report 两侧都会按条款号建立独立节点；只有换行/标点边界且候选是当前条款的子项或同级项才拆分，`符合：2.1.4 的要求` 仍保留在正文。0775 的四个子条款和 0780 的字母子项现在均能建立双方节点并保持唯一账本身份，`eligible == accounted` 且 `conserved=true`。

另有 `mvp.ptr_draft_runner.run_ptr_draft()` 将上述结果写成可浏览的 `result.json` 和 `index.html`。它使用独立的 `report_ptr_draft` mode 名称，不注册到正式 Run/Worker，且始终 `publishable=false`。`mvp.ptr_compare.run_llm_reviews()` 提供显式 provider 注入点；没有注入 provider 时不会调用模型。

规则比较草案仍保持 `publishable=false`；完成业务授权后，正式 `report_ptr` Runner 会把同一份规则账本转换为正式 WorkerResult，硬字段和 Evidence 形状不变。

## 样本回归结果

以下是项目中 12 组同号 PTR/Report 的批量冒烟结果。它们证明解析链可以运行并形成完整账本，不代表所有条款已经通过业务验收。

| 样本 | PTR 提取 | 结果 | 账本保守性 |
|---|---|---|---|
| 0596 | 原生文字 | `mismatch/manual/excluded` | 通过 |
| 0775 | 原生文字 | `mismatch/manual/excluded` | 通过 |
| 0778 | 原生文字 | `mismatch/manual/excluded` | 通过 |
| 0780 | 原生文字、深层字母子项 | `mismatch/manual/excluded` | 通过 |
| 1539 | 原生文字、表格 | `mismatch/manual/excluded` | 通过 |
| 2795 | 原生文字、附录引用 | `mismatch/manual` | 通过 |
| 2948 | 原生文字、表格 | `mismatch` | 通过 |
| 3940 | 原生文字、复杂表格 | `mismatch/manual/excluded` | 通过 |
| 4788 | 原生文字、复杂表格 | `mismatch/manual/excluded` | 通过 |
| 5332 | 原生文字、表格 | `mismatch/manual` | 通过 |
| 5780 | OCR 候选 | 全部 `manual` | 通过 |
| 5782 | OCR 候选 | 全部 `manual` | 通过 |

## 关键人工验收断言

### 1539

- `2.2.2`：PTR 和 Report 都引用表 6，但当前双方矩阵没有达到完全一致的结构证据，进入 `manual`。
- `2.2.6`：PTR 和 Report 都是“单个脉冲最大输出能量小于 258mJ”；单位归一后为同一数值事实，状态为 `matched`。
- `2.6`：软件功能表跨页物理行已按组件列和功能列重建，续行归并后与 PTR 表 6 的语义矩阵一致，状态为 `matched / TABLE_SEMANTIC_NORMALIZED_MATCH`；`2.2.2` 仍因波形表结构证据不足保留 `manual`。

### 2948

- PTR 的 `2.4.1`、`2.4.2` 条款号与标题在 PDF 中分列，按同一视觉行合并后分别进入账本，均与 Report 匹配。
- 同一 Report 序号下的 `2.1.13`、`2.1.14` 续行不会再被前条款的表格状态吞掉；补充 `a)`/`b)` 子项后，当前 2948 试点账本 34 个纳入项全部 `matched`。

### 0775

- 文本措辞差异保守进入人工队列；当前规则账本为 148 个 `matched`、3 个确定性硬差异、48 个 `manual`，没有把措辞差异直接当成硬错误。

### 2795

- `2.5`：正文写 `5MΩ`，Report 单独单位栏写 `GΩ`，状态为 `mismatch / UNIT_MISMATCH`。
- `2.6.1`：引用附录 A，但当前没有可绑定的双方完整矩阵，状态为 `manual / TABLE_CONTENT_NOT_EXTRACTED`。

### 0780

- `2.3.6.6` 和 `2.3.13.1` 的深层条款进入账本。
- PTR 与 Report 节点出现同一条款号时，账本 ID 仍唯一；`eligible == accounted` 且 `conserved=true`。

### 5780、5782

- 原生文字层为空时，本机 OCR 生成页码、bbox、置信度和后端信息。
- OCR 候选不会直接参与 `matched` 或 `mismatch`，比较结果全部保持 `manual`。

## 2026-10-25 规则更新补充

比较规则现为 `PTR-P01@0.5.0-draft`，布局解析器为 `ptr-layout-1.3`。本轮回归将 Report 中明确标注的运动距离缩放、测试/模式参数和型号行从接受标准比较视图分离，并把页码、图号、文件编号和协议编号排除出测量数值；这些原文仍保留在 `report_auxiliary_lines` 和 Evidence 中。单位判断增加峰值后缀、复合单位和带幂单位的安全归一，`MΩ/GΩ` 与 `m³/mm³` 等倍率差异继续硬报风险。2948 仍为 34/34 自动一致；1539 的 `Vp/Ap` 与 `V/A` 在峰值语义明确时一致；2795 的 `MΩ/GΩ` 和 0775 的 `m³/mm³` 仍保持单位不一致。0775 当前账本为 150 个 `matched`、1 个确定性硬差异、48 个 `manual`，其中原先由运动距离缩放造成的两条数值误报已消除。

## 测试证据

在项目 `.venv`（Python 3.13）中，PTR 比较相关回归通过：

```text
test_ptr_compare.py  31 tests OK
test_ptr_scope.py     7 tests OK
test_ptr_p01.py       6 tests OK
test_ptr_draft_runner.py  2 tests OK
test_ptr_review_package.py  1 test OK
test_ptr_acceptance.py  6 tests OK
test_ptr_lifecycle_gate.py  5 tests OK
test_ptr_enablement.py  3 tests OK
test_ptr_formal_runner.py  3 tests OK
PTR/启用链路合计 64 tests OK
capabilities/run_modes/run_state/run_store/workbench  53 tests OK（2 skipped）
真实 2948 formal Runner → RunStore.publish_result  34 Finding，succeeded，SHA-256 未变化
unittest discover -s tests -q  394 tests；4 failures/2 errors 为仓库既有私有 OCR 派生文件、workbench-preview.html 和照片 OCR 基线缺失
git diff --check       OK
```

另对 `素材/ptr` 与 `素材/report` 中的 12 组同号样本批量运行 `comparison_mode="rules"`：每组均满足 `eligible == accounted`、`conserved=true`；5780、5782 的 OCR PTR 全部保持 `manual`。这批量结果用于发现解析回归，不能替代三份试点样本的逐条人工验收。

三份试点的人工验收包已由 `mvp.ptr_review_package` 生成到 `output/ptr-review-package-20261024-v5/`，每个样本包含 `manifest.json`、`review.csv` 和 `review.md`；包内机器状态、证据、规则版本和源文件哈希只读保留。验收门 JSON 保留启用前的 `ready_for_signoff / can_enable_report_ptr=false` 快照；随后生成的 `ptr-business-signoff.json` 和 `enablement-decision.json` 记录了本次授权与启用决定。

## 2026-10-26 版本对齐补充

- `mvp/ptr_report_contract.py` 现在提供唯一的 `PTR-P01@0.5.0-draft` 版本源，比较器、能力目录、验收门和 Finding 结果均引用该版本。
- 按新规则重新生成的三份包位于 `output/ptr-review-package-20261026-v7/`：2948 为 `34/0/0/0`，0775 为 `150/1/48/1`，1539 为 `20/0/1/24`；三份 Coverage Ledger 均守恒，并包含正文差异片段。
- v7 验收门已生成但保持 `blocked`，原因是新规则版本尚未完成逐条人工复核；v5 的旧版 `0.4.0-draft` 签字继续作为历史记录保留，不能直接复用于 v7。

业务负责人逐条确认的项目、签字记录和启用后维护要求见 [PTR 正式启用签字清单](PTR_SIGNOFF_CHECKLIST_2026-10-24.md)。

本轮增加了验收门与正式 Run 生命周期的闭环诊断。验收门文件记录的是启用前状态：三份试点均为 `ready`，当时目录中的 `report_ptr` 为 `disabled`，下一步为 `BUSINESS_SIGNOFF_REQUIRED`。随后业务授权文件通过 `mvp.ptr_enablement` 校验，当前目录已切换为 enabled；正式 Runner、Worker、coordinator、publish 和双角色 Evidence 已用真实 2948 样本回归通过。

启用后的持续维护事项：

- 新增 PTR 版式、样本或规则版本时，重新做逐条人工复核和 Coverage Ledger 守恒检查；
- 无边框/跨页表格、引用表格/附录和循环引用继续保留正反回归；
- 规则+LLM 真正接入供应商时，记录模型、提示词、输入哈希、验证结果和人工动作；
- 每次正式 Runner、Worker、发布闸门或双文档定位代码变更后，重新跑对应回归。

规则草案仍不能作为正式结果；正式模式已按授权启用，后续新增版式或规则版本必须重新走同一套签字和发布回归。
