# 报告核对工具 · Claude Code 工作说明

本文件供 Claude Code 在本目录启动时自动读取。最后更新：2026-11-08（由云端会话交接）。

## 项目是什么

本地、确定性（纯规则）的 PDF 核对工具，面向医疗器械检验报告：

- **报告自检**（`report_self`）
- **报告 ↔ GB 9706.1 原始记录**（`report_record_9706_1`）
- **报告 ↔ GB 9706.202 原始记录**（`report_record_9706_202`）
- **报告 ↔ PTR 产品技术要求**（`report_ptr`，规则 PTR-P01，当前 `0.5.1-draft`，定义在 `mvp/ptr_report_contract.py`）

GitHub：`lulf87/report-checker`（**公开仓库**）。

## 启动与运行

- 启动：双击 `启动报告核对工具.command`（或程序坞里的“报告核对工具.app”）。会起两个服务：
  - `mvp.run_state_server` → 8767（API、SQLite：`output/run-state.sqlite3`）
  - `mvp.static_server --mode upload` → 8765（页面：`docs/prototypes/workbench-upload.html`）
- 核对任务在子进程（worker）里跑：改 `mvp/` 里的规则代码不需要重启；**改 `run_state_server.py`、`static_server.py` 需要重启**；改页面后浏览器按 ⌘+Shift+R。
- 日志：`output/logs/run-state-server.log`、`output/logs/static-upload.log`。
- Python 用 `.venv/bin/python`（uv 管理，Python ≥3.12）。**PyMuPDF 必须 `<2`**：2.x 读表格单元格的文字顺序不同，会让 PTR 检验范围解析全部失败。

## 测试

```bash
.venv/bin/python -m unittest tests.test_ptr_compare            # 约 2–7 分钟，需要 素材/
.venv/bin/python -m unittest tests.test_ptr_safe_equivalence tests.test_ptr_table_rows
.venv/bin/python -m unittest tests.test_ptr_draft_runner tests.test_ptr_formal_runner
.venv/bin/python -m unittest tests.test_run_state_server tests.test_workbench_upload
```

页面脚本语法检查：把 `workbench-upload.html` 最后一个 `<script type="module">` 的内容取出来后 `node --check`。
`test_workbench_contract` 在改动前就失败（环境问题），不是回归。

## 必须遵守的工作约定

1. **不能让原来 OK 的变成不 OK。** 每家 PTR 写法都不同。改任何 PTR 规则后：
   - 先用**上一个提交**（`git worktree add /tmp/prev HEAD`，在其中软链 `素材`）跑全部样本作为基线，再用新代码跑；
   - 逐条比较 `coverage_ledger` 的 `decision`：原“一致”不得变成待确认/不一致，原“不一致”不得消失；
   - 有变化的条目逐条说明原因，写进提交说明。
   - 跑单个样本：`compare_ptr_report(report_pdf, ptr_pdf, comparison_mode="rules", ocr_mode="auto")`（`mvp/ptr_compare.py`），样本在 `素材/report/<编号>/`、`素材/ptr/<编号>/`。
   - 当前样本（13 组）：0596 0775 0778 0780 1539 2170 2795 2948 3940 4788 5332 5780 5782（5780、5782 为扫描件 PTR）。
2. **安全等价规则只在“完全相等”时放行**，并在 `text_comparison.equivalence_rules` 里记录用到的规则；表格必须逐格/逐行核对，不得只比“看起来像”。规则说明同步写进 `docs/PTR_P01_RULE_DRAFT.md`。
3. **公开仓库**：绝不提交 `素材/`、`output/`、PDF、docx、图片、数据库，也不提交真实客户/公司名称（测试数据要匿名化）。
4. 不要把 GitHub token 或任何 API key 写进聊天或代码；测试用 key 放在 git 忽略的本地配置文件里。
5. 不要运行 `playwright install`。
6. 提交信息用中文，说明“改了什么、为什么、回归结果”。

## 标准答案（gold，2026-11-08 起）

- `素材/gold/<编号>.json`：每条 `expected`（matched/mismatch/manual/excluded）、`acceptable`、`status`（draft=工具结果预填未复核 / confirmed=人工确认）、`issue`。只有 confirmed 算答案。`素材/expected/` 是早期工具的输出快照，**不是答案**（如 2795 2.5 被记成一致）。
- 工具：`mvp/ptr_gold.py`（`run` / `draft` / `review-page` / `merge` / `check`），复核页模板 `mvp/ptr_gold_review.html`，生成到 `output/gold-review/index.html`，用 `.claude/launch.json` 的 `gold-review`（8790 端口）打开。
- 改规则后：`.venv/bin/python -m mvp.ptr_gold check` 对答案（漏报、不该放行时退出码 1），未复核条目仍按上面的回归流程比较。
- `status=claude`：Claude 逐条判断（带 `claude.confidence`/`reason`），与人工确认分开统计；人工确认优先。复核页默认只显示“Claude 不确定 + 抽查样本”。
- 进度（2026-11-08）：52 条人工确认（含 13 条不确定项裁定、24 条抽查，抽查 23/24 与 Claude 一致）；944 条 Claude 已判（A/B/C 逐条看文字，表格和扫描件对照 PDF/页面图像；D/E 用脚本核对）。
- 用户裁定口径：与 PTR 文字不同即“不一致”（删“等”/“应”、简写、图题少字、漏写标称值、“动作时候”≠“动作时”、“0.7”≠“0.70”都算）；PTR 笔误而报告写对（0596 “0Dplus”）也报不一致；只列送检型号且全部相同 → 待确认；不检条款（结果“/”）的抄录错误 → 排除；PTR 自身目录/正文不一致 → 排除；父条款只有标题、内容在子条款核对 → 一致；页眉页脚、报告单位行和结果小标题、全半角括号、冒号、换行空格 → 忽略。
- 已按裁定改规则（11-08，未提交）：删除 `ACTION_TIME_WORDING`；表格数值相等但写法不同改为 `CELL_NUMBER_WRITTEN_DIFFERENTLY`（不一致）。13 组只变 3 条：0775 2.3.4.1 一致→不一致，0778 2.1.10/2.1.10d 一致→待确认。`check` 无漏判。
- 新规则 `TEXT_WORDING_MISMATCH`（11-08，未提交）：非 OCR、每处差异只是 ≤4 个汉字（去标点括号）且数字/单位/引用一致 → 不一致。13 组只变 11 条（0778 2.1.1/a/b、2.1.10/d、2.1.20.2，0780 2.3.4.2/f、2.3.5.1、2.3.5.2/a），全部待确认→不一致且与答案相符，无新增误报。
- 修误报（11-08，未提交）：条款号与字母分格（`report_clause_cell`）、`REPORT_PARENT_TITLE`、单位拆开（5M Ω、10μ⏎g/g、3.6×103 Pa）、`UNIT_LABEL_WITHOUT_PRINTED_VALUE`、表格写成文字时数值成组比较、`PARENT_TABLES_CHECKED_IN_CHILDREN`。13 组只变 18 条：16 条不一致→一致，0778 2.1.7/2.1.11 不一致→待确认；全部与答案相符。测试 `tests/test_ptr_false_alarms.py`。
- 对答案剩余：误报 0；待确认但应为不一致 6 条（0596 2.1.1 表内 PTR 笔误、1539 2.2.2 图题大段缺失、2170 三条扫描件、4788 2.1.11 少“应”同时报告加了注释）；多余待确认 28 条。
- 用户觉得复核页太技术化看不懂；需要用户判断时，用直白的选择题问。
- Claude 新发现、待用户确认的真问题：3940 2.1.6 报告缺检验范围内的“干扰转复频率”；0780 2.3.5.1“材质力”、2.3.5.2/2.3.5.2a“转向白如”报告笔误；0596 2.1.1 即 PTR“0Dplus/DDIR”笔误。

## 当前状态（2026-11-08）

- 分支：`codex/ptr-rules-0.5.1`（已推送；`main` 尚未合并这条分支）。
- 已提交的最近工作：
  - `b1d9149` 修复 9706.1/9706.202 预检报 500（`run_state_server.py` 缺少 `Mapping` 导入，问题源于 `dfb389a`）。**`main` 等其他分支如包含 `dfb389a`，合并时需要带上这个修复。**
  - `267bb79` 及之前：PTR 目录行跳过、附表题注、标准附录不当表格引用、条款展开引用、表格按行名/按行展开逐格核对、条款号错位检测、结果页逐格展示等。
- **未提交的改动（用户本人在 11-03～11-04 完成，在工作区）**：
  - `mvp/ptr_compare.py`：PTR 同号多条款 → `AMBIGUOUS_PTR_MAPPING`；无编号表格 → `UNLABELLED_TABLE_BINDING_UNVERIFIED`；~~新等价规则 `ACTION_TIME_WORDING`~~（11-08 按用户裁定删除）；每条结果增加 `mapping`。
  - `mvp/ptr_layout.py`（`ptr-layout-1.6`）+ 新文件 `mvp/ptr_ocr_sections.py`：扫描件 PTR 先定位第 2 章正文再解析，目录独有条款标为 `OCR_TOC_BODY_DISCREPANCY`。
  - `mvp/ptr_table_rows.py`：报告表格“备注”列在严格来源核验下可补行名。
  - `mvp/full_report_attempt.py`（+677 行）：Grip 开口角度表核对。
  - `docs/prototypes/workbench-upload.html` + 新文件 `docs/prototypes/evidence-cards.mjs`：按证据页查看、导出完整证据清单、标题改为“PTR 条款 X ↔ Report 条款 Y · 结果序号 N”。
  - 检查结果：语法与静态检查通过；PTR/服务端/R07 测试通过；`test_workbench_upload` 1 条失败——测试仍期望旧标题“Report 接受标准”，需要更新期望。
  - 回归（对比 `b1d9149`，13 组）：原“不一致”无一丢失；**11 条由“一致”变“待确认”，全部来自 `UNLABELLED_TABLE_BINDING_UNVERIFIED`**：0775 的 2.9.1.1～2.9.1.3、2.9.2、2.12.2.1、2.12.3.1（这 6 条此前已由 `REPORT_TABLE_AS_TEXT` 逐行核对表格内容）；0778 的 2.1.3b、2.1.14、2.1.15；2948 的 2.1.2；5332 的 2.1.3。变好 2 条：0778 的 2.1.10、2.1.10d（`ACTION_TIME_WORDING`）。4788 的 2.2.4 由“排除”变“待确认”（`AMBIGUOUS_PTR_MAPPING`）。

## 待办（按优先级）

1. ~~给 `UNLABELLED_TABLE_BINDING_UNVERIFIED` 加例外~~（11-08 已完成，未提交）：每张 PTR 表都经 `REPORT_TABLE_AS_TEXT` 核对 → `UNLABELLED_TABLE_VERIFIED_AS_TEXT`；`TABLE_ROW_KEYS` 每格都是 `CELL_EQUAL`/`CELL_NUMERIC_EQUAL` → `TABLE_CELLS_MATCH` + `unlabelled_table`。回归：恢复 7 条（0775 六条、2948 2.1.2），不一致无丢失；仍待确认 4 条：0778 2.1.3b/2.1.14/2.1.15（行名无法拆分或型号为列），5332 2.1.3（PTR 竖向合并单元格“≥15”，靠 `ROW_CELLS_IN_PTR_TEXT` 才能对上，按严格规则不放行；可考虑给 `_ptr_table_rows` 加竖向合并单元格继承）。
2. 为未提交改动补测试：Grip 角度核对、`ptr_ocr_sections`、`evidence-cards.mjs`、`AMBIGUOUS_PTR_MAPPING`；更新 `test_workbench_upload` 的标题期望。
3. 提交并推送这批改动（提交说明附回归结果）。
4. 其余已知项：0775 2.5.2.9（PTR 竖排合并单元格）、4788 2.1.8（PTR 表格行跨页断开）仍为待确认；2170 的附表 1/2 内容待人工；5780/5782 扫描件 PTR 全部待确认。

## 已确认的真问题（不要被规则“修掉”）

- 0775 2.10.2.1：要求“≥0.06m³”，报告“单位：mm³”。
- 0596：报告删去 PTR 2.1.8“输入阻抗”后条款号整体前移（报告 2.1.8～2.1.12 = PTR 2.1.9～2.1.13）；2.1.2 PTR“1000Ω”报告写“2000Ω”；2.10/2.11 标准年份 2015 vs 2024；PTR 6232 型号“0Dplus/DDIR”疑似笔误。
- 2170 2.3.1.2.1/2.3.1.2.2、2795：要求为 MΩ、报告“单位：GΩ”。
