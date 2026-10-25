# PTR 与 Report 核对验证报告

日期：2026-10-24
分支：`codex/ptr-report-comparison`
规则草案：`PTR-P01@0.2.0-draft`

## 已验证的执行链

当前比较入口按以下顺序运行：

1. 读取 Report 第三页的 `ScopeSet`，展开纳入、排除和“仅检”范围。
2. 按 PDF 页面坐标读取 PTR 2.x 条款；条款保留父子关系、原文、页码、bbox、SHA-256 和提取方式。
3. 按 Report 表格物理列读取“标准要求”，包括合并的第二要求列；检验结果列不会混进要求文本。
4. 对文本、数值、单位、标准/表格引用和覆盖关系执行规则比较。
5. 表格引用如果没有双方可确认的行列矩阵，状态为 `manual`；OCR 候选也只能为 `manual`。
6. `rules+llm` 只生成最小人工队列。LLM 引文必须逐字存在于原文，且不能修改数值、单位、范围或引用。

另有 `mvp.ptr_draft_runner.run_ptr_draft()` 将上述结果写成可浏览的 `result.json` 和 `index.html`。它使用独立的 `report_ptr_draft` mode 名称，不注册到正式 Run/Worker，且始终 `publishable=false`。`mvp.ptr_compare.run_llm_reviews()` 提供显式 provider 注入点；没有注入 provider 时不会调用模型。

所有结果仍写为不可发布草案，`publishable=false`，正式 `report_ptr` capability 仍保持 `PTR_NOT_VALIDATED`。

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
- `2.6`：软件功能表已提取为物理行证据；由于表格内容仍需行列语义复核，文本/表格进入人工队列。

### 2795

- `2.5`：正文写 `5MΩ`，Report 单独单位栏写 `GΩ`，状态为 `mismatch / UNIT_MISMATCH`。
- `2.6.1`：引用附录 A，但当前没有可绑定的双方完整矩阵，状态为 `manual / TABLE_CONTENT_NOT_EXTRACTED`。

### 0780

- `2.3.6.6` 和 `2.3.13.1` 的深层条款进入账本。
- PTR 与 Report 节点出现同一条款号时，账本 ID 仍唯一；`eligible == accounted` 且 `conserved=true`。

### 5780、5782

- 原生文字层为空时，本机 OCR 生成页码、bbox、置信度和后端信息。
- OCR 候选不会直接参与 `matched` 或 `mismatch`，比较结果全部保持 `manual`。

## 测试证据

在项目 `.venv`（Python 3.13）中通过：

```text
test_ptr_compare.py  14 tests OK
test_ptr_scope.py     7 tests OK
test_ptr_p01.py       6 tests OK
git diff --check       OK
```

正式启用前仍需完成：

- 2948、0775、1539 的逐条人工标注和正反样本冻结；
- 无边框/跨页表格的行列语义确认；
- 引用表格和附录的唯一绑定、缺表和循环引用测试；
- 选择实际 LLM 供应商后，记录模型、提示词、输入哈希、验证结果和人工动作；
- 通过正式 Runner、Worker、发布闸门和双文档定位回归。

在这些门槛完成前，不打开 `report_ptr` 按钮，也不把草案状态写成正式业务结论。
