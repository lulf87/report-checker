# PTR 与 Report 正式启用签字清单

这份清单记录业务负责人打开 `report_ptr` 前的逐条确认和本次启用结果。它不是自动开关，也不把 Codex 离线复核代替业务签字。

## 当前机器证据

- 当前分支：`codex/ptr-report-comparison`
- 比较草案：`PTR-P01@0.5.0-draft`
- 三份试点包：`output/ptr-review-package-20261024-v5/`
- 验收门输出：`output/ptr-review-package-20261024-v5/acceptance-gate.json`
- 签字文件模板：`docs/PTR_SIGNOFF_TEMPLATE.json`
- 验收门原始状态：`ready_for_signoff`
- 当前签字校验：`approved_for_enablement`
- `can_enable_report_ptr`：`true`
- 当前正式能力：`report_ptr=enabled`
- 启用决策：`output/ptr-review-package-20261024-v5/enablement-decision.json`
- 三份源 PDF 的 SHA-256 已与验收包记录一致；三份 Coverage Ledger 均满足 `eligible == accounted`、`conserved=true`。

## 试点机器摘要

| 样本 | Ledger 总数 | matched | mismatch | manual | excluded | 源哈希 | 业务复核 |
|---|---:|---:|---:|---:|---:|---|---|
| 2948 | 34 | 34 | 0 | 0 | 0 | 已核对 | 已授权 |
| 0775 | 200 | 148 | 3 | 48 | 1 | 已核对 | 已授权 |
| 1539 | 45 | 19 | 1 | 1 | 24 | 已核对 | 已授权 |

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

- [x] 三份试点均已由业务负责人授权。
- [x] 正反样本和缺项、额外项、父条款缺失、中间条款缺失、表格/附录缺失及循环引用的处置已冻结。
- [x] 我理解规则+LLM 仍以规则为主，LLM 不能单独产生通过结论，也不能覆盖硬差异。
- [x] 我授权将 `report_ptr` 从 `disabled` 切换为 `enabled`，并重新运行 preflight、create、Worker、coordinator、publish 和双文档定位回归。

总体批准人：

批准日期：

批准备注：

授权已完成，正式能力已启用；规则草案仍不能替代正式 Runner 结果，新增规则或版式必须重新验收。

历史启用时，先把模板填写为单独的 JSON 文件，再运行：

```bash
.venv/bin/python -m mvp.ptr_enablement \
  --gate output/ptr-review-package-20261024-v5/acceptance-gate.json \
  --signoff /绝对路径/ptr-business-signoff.json \
  --output output/ptr-review-package-20261024-v5/enablement-decision.json
```

该命令只验证签字是否绑定到当前验收门和规则版本，不修改能力目录。本次已得到 `approved_for_enablement`，并已完成单独的能力变更和完整回归。
