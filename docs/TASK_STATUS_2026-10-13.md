# 当前任务状态：2948 Report + GB 9706.1 Record

更新时间：2026-10-13

## 已完成

- 修复跨行斜线按质心错误归属上一行的问题；现在优先按笔迹与复选框的实际交集归属状态行。
- Record 中双横杠或横跨整个状态窗格的斜线解释为“不适用”，与 Report 的 `/` 或 `——` 等价。
- Record 状态框真正空白时报告 `record_status_blank` error。
- 笔迹靠近复选框但没有落入复选框时报告 `status_mark_near_checkbox_unresolved`，保留人工复核，不自动推断为“不适用”。
- 2948 的结果已重新生成并同步到真实工作台目录。

## 当前 2948 GB 9706.1 结果

Ledger：一致 919，不一致 5，待复核 93，不适用 37，排除 5；Record Coverage `1262/1262`，Report Coverage `980/980`。

当前结果文件：

`output/full-records-current-20261011/2948/report-record-9706-1/result.json`

## 用户点名项目

| Q | 当前结论 | 原因 |
|---:|---|---|
| 62 | 通过 | 双横杠注销线解释为“不适用”，与 Report `/` 一致 |
| 574 | 错误 | Record 状态框真正空白，`record_status_blank` |
| 741 | 待复核 | Record 行存在，但 Report 中没有唯一对应行，`record_row_missing_in_report` |
| 941 | 待复核 | 序列 52 中存在近框但未落框笔迹，`record_status_near_checkbox_unresolved` |
| 952 | 待复核 | 序列 63 中存在近框但未落框笔迹，`record_status_near_checkbox_unresolved` |
| 964 | 错误 | 序列 75 的 Record 状态框为空，`record_status_blank` |
| 980 | 通过 | 整个 Record 状态窗格斜线解释为“不适用”，与 Report `/` 一致 |
| 982 | 通过 | 整个 Record 状态窗格斜线解释为“不适用”，序列结论一致 |
| 992 | 待复核 | 原始记录笔迹靠近复选框但没有落入格内，不能自动确认为“不适用” |
| 1011 | 待复核 | 8.7 数值单元格存在笔迹，但 Apple Vision/Tesseract 没有形成一致数值 |
| 1012 | 待复核 | 8.7 数值单元格的两处候选值均未形成双通道一致结果 |
| 1013 | 待复核 | 8.7 数值单元格的两处候选值均未形成双通道一致结果 |

## 尚未关闭

- 3 个原有状态值不一致：`RECORD61-BODY-S052-E037`、`RECORD61-BODY-S052-E038`、`RECORD61-BODY-S061-E008`。
- Q741 的 Report/Record 条款映射仍需业务确认，当前保留人工复核。
- Q1011–Q1013 需要人工读取原始数值，或增加更可靠的局部数值识别通道。

修复前快照：

- `output/backups/cross-row-status-pre-fix-20261013/`
- `output/backups/status-semantics-pre-fix-20261013/`
