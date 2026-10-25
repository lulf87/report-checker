# PTR 与 Report 正式启用签字清单

这份清单用于业务负责人在打开 `report_ptr` 前逐条确认。它不是自动开关，也不把 Codex 离线复核代替业务签字。

## 当前机器证据

- 当前分支：`codex/ptr-report-comparison`
- 比较草案：`PTR-P01@0.4.0-draft`
- 三份试点包：`output/ptr-review-package-20261024-v5/`
- 验收门输出：`output/ptr-review-package-20261024-v5/acceptance-gate.json`
- 验收门当前状态：`ready_for_signoff`
- `can_enable_report_ptr`：`false`
- 下一步：`BUSINESS_SIGNOFF_REQUIRED`
- 三份源 PDF 的 SHA-256 已与验收包记录一致；三份 Coverage Ledger 均满足 `eligible == accounted`、`conserved=true`。

## 试点机器摘要

| 样本 | Ledger 总数 | matched | mismatch | manual | excluded | 源哈希 | 业务复核 |
|---|---:|---:|---:|---:|---:|---|---|
| 2948 | 34 | 34 | 0 | 0 | 0 | 已核对 | 待签字 |
| 0775 | 200 | 148 | 3 | 48 | 1 | 已核对 | 待签字 |
| 1539 | 45 | 19 | 1 | 1 | 24 | 已核对 | 待签字 |

`matched` 只表示规则证据达到一致；`mismatch` 是规则确认的差异；`manual` 仍需要人工判断；`excluded` 是范围明确排除。以上四类都必须保留在签字范围内，不能只签通过项。

## 逐样本签字

### 2948

- [ ] 我已逐条查看 `2948/review.csv` 和对应 PTR/Report 页码、坐标及原文。
- [ ] 我确认范围、父子条款、表格/附录引用和 `matched/mismatch/manual/excluded` 处置。
- [ ] 我确认本样本可作为 PTR 正式启用的试点证据。
- 复核人：
- 日期：
- 备注：

### 0775

- [ ] 我已逐条查看 `0775/review.csv` 和对应 PTR/Report 页码、坐标及原文。
- [ ] 我确认长 Report 中的产品技术要求范围、内联子条款和重复物理观察。
- [ ] 我确认 3 个确定性差异、48 个人工项和 1 个范围外项的处置。
- [ ] 我确认本样本可作为 PTR 正式启用的试点证据。
- 复核人：
- 日期：
- 备注：

### 1539

- [ ] 我已逐条查看 `1539/review.csv` 和对应 PTR/Report 页码、坐标及原文。
- [ ] 我确认表 6、跨页续行、单位归一和引用绑定结果。
- [ ] 我确认 1 个确定性差异、1 个人工项和 24 个范围外项的处置。
- [ ] 我确认本样本可作为 PTR 正式启用的试点证据。
- 复核人：
- 日期：
- 备注：

## 总体批准

- [ ] 三份试点均已由业务负责人签字。
- [ ] 正反样本和缺项、额外项、父条款缺失、中间条款缺失、表格/附录缺失及循环引用的处置已冻结。
- [ ] 我理解规则+LLM 仍以规则为主，LLM 不能单独产生通过结论，也不能覆盖硬差异。
- [ ] 我授权将 `report_ptr` 从 `disabled` 切换为 `enabled`，并重新运行 preflight、create、Worker、coordinator、publish 和双文档定位回归。

总体批准人：

批准日期：

批准备注：

在上述签字完成前，正式能力必须继续保持 `MODE_DISABLED / PTR_NOT_VALIDATED`；草案结果不能写入正式 Run，也不能作为业务通过结论。
