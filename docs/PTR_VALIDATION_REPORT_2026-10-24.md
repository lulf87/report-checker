# PTR 与 Report 核对验证报告

日期：2026-10-24
分支：`codex/ptr-report-comparison`
规则草案：`PTR-P01@0.3.0-draft`

## 已验证的执行链

当前比较入口按以下顺序运行：

1. 读取 Report 第三页的 `ScopeSet`，展开纳入、排除和“仅检”范围。
2. 按 PDF 页面坐标读取 PTR 2.x 条款；条款保留父子关系、原文、页码、bbox、SHA-256 和提取方式。
3. 按 Report 表格物理列读取“标准要求”，包括合并的第二要求列；检验结果列不会混进要求文本。
4. 对文本、数值、单位、标准/表格引用和覆盖关系执行规则比较。
5. 表格引用如果没有双方可确认的行列矩阵，状态为 `manual`；OCR 候选也只能为 `manual`。
6. `rules+llm` 只生成文本歧义队列；表格结构不足、数值、单位、范围和引用硬差异不会被送入可清除文本结论的队列。LLM 引文必须逐字存在于原文，且不能修改数值、单位、范围或引用。

本轮补充了内联条款拆分：当 PDF 把子条款写成 `2.10.1.2.1，…`、`2.10.1.2.2. …` 或 `2.11.4碰撞保护` 时，PTR 和 Report 两侧都会按条款号建立独立节点；只有换行/标点边界且候选是当前条款的子项或同级项才拆分，`符合：2.1.4 的要求` 仍保留在正文。0775 的四个子条款现在均有双方节点和唯一账本映射，`eligible == accounted` 且 `conserved=true`。

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
- `2.6`：软件功能表跨页物理行已按组件列和功能列重建，续行归并后与 PTR 表 6 的语义矩阵一致，状态为 `matched / TABLE_SEMANTIC_NORMALIZED_MATCH`；`2.2.2` 仍因波形表结构证据不足保留 `manual`。

### 2948

- PTR 的 `2.4.1`、`2.4.2` 条款号与标题在 PDF 中分列，按同一视觉行合并后分别进入账本，均与 Report 匹配。
- 同一 Report 序号下的 `2.1.13`、`2.1.14` 续行不会再被前条款的表格状态吞掉；当前 2948 试点账本 22 个纳入项全部 `matched`。

### 0775

- 文本措辞差异保守进入人工队列；当前规则账本为 141 个 `matched`、3 个确定性硬差异、47 个 `manual`，没有把措辞差异直接当成硬错误。

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

在项目 `.venv`（Python 3.13）中，PTR 比较相关回归通过：

```text
test_ptr_compare.py  24 tests OK
test_ptr_scope.py     7 tests OK
test_ptr_p01.py       6 tests OK
test_ptr_draft_runner.py  2 tests OK
test_ptr_review_package.py  1 test OK
git diff --check       OK
```

另对 `素材/ptr` 与 `素材/report` 中的 12 组同号样本批量运行 `comparison_mode="rules"`：每组均满足 `eligible == accounted`、`conserved=true`；5780、5782 的 OCR PTR 全部保持 `manual`。这批量结果用于发现解析回归，不能替代三份试点样本的逐条人工验收。

三份试点的人工验收包已由 `mvp.ptr_review_package` 生成到 `output/ptr-review-package-20261024-v3/`，每个样本包含 `manifest.json`、`review.csv` 和 `review.md`；包内机器状态、证据和源文件哈希只读保留，复核栏默认为 `pending`，填写不会改变正式能力状态。

正式启用前仍需完成：

- 2948、0775、1539 的逐条人工标注和正反样本冻结；
- 无边框/跨页表格的行列语义确认；
- 引用表格和附录的唯一绑定、缺表和循环引用测试；
- 选择实际 LLM 供应商后，记录模型、提示词、输入哈希、验证结果和人工动作；
- 通过正式 Runner、Worker、发布闸门和双文档定位回归。

在这些门槛完成前，不打开 `report_ptr` 按钮，也不把草案状态写成正式业务结论。
