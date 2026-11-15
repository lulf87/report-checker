# 报告核对工具 · Claude Code 工作说明

本文件供 Claude Code 在本目录启动时自动读取。最后整理：2026-11-14。
只写“现在是什么样、必须遵守什么”；每次改动的经过看 `git log` 和 `CHANGELOG.md`，规则细节看 `docs/PTR_P01_RULE_DRAFT.md`。

## 项目是什么

本地、确定性（纯规则）的 PDF 核对工具，面向医疗器械检验报告：

- **报告自检**（`report_self`）
- **报告 ↔ GB 9706.1 原始记录**（`report_record_9706_1`）
- **报告 ↔ GB 9706.202 原始记录**（`report_record_9706_202`）
- **报告 ↔ PTR 产品技术要求**（`report_ptr`，规则 PTR-P01，当前 `0.5.1-draft`，定义在 `mvp/ptr_report_contract.py`）

GitHub：`lulf87/report-checker`（**公开仓库**）。直接在 `main` 上工作。

## 启动与运行

- 启动：双击 `启动报告核对工具.command`（或程序坞里的“报告核对工具.app”）。会起两个服务：
  - `mvp.run_state_server` → 8767（API、SQLite：`output/run-state.sqlite3`）
  - `mvp.static_server --mode upload` → 8765（页面：`docs/prototypes/workbench-upload.html`）
- 核对任务在子进程（worker）里跑：改 `mvp/` 里的规则代码不需要重启；**改 `run_state_server.py`、`static_server.py` 需要重启**；改页面后浏览器按 ⌘+Shift+R。
- 日志：`output/logs/run-state-server.log`、`output/logs/static-upload.log`。
- Python 用 `.venv/bin/python`（uv 管理，Python ≥3.12）。**PyMuPDF 必须 `<2`**：2.x 读表格单元格的文字顺序不同，会让 PTR 检验范围解析全部失败。
- **`mvp/` 里不要写裸的 `import fitz`**，一律 `try: import pymupdf as fitz / except ImportError: import fitz`。裸导入会让 PyMuPDF 把弃用警告打印到标准输出；调度器把 Worker 的标准输出当成一行 JSON 解析，多一行就是所有核对都报 `WORKER_PROTOCOL_INVALID`（11-13 出过）。Worker 现在把 `mvp.run_modes` 的导入也放在输出重定向里，但这条约定仍要遵守。改完 Worker 会导入的模块后跑 `tests.test_worker_protocol`。
- 状态数据库是 schema v9；旧版本代码打不开迁移后的库（`UNSUPPORTED_SCHEMA_VERSION`）。
- 表格/OCR 缓存在 `output/table-cache/`（`mvp/table_cache.py`）：键 = PDF 哈希 + PyMuPDF/OCR 版本 + 参数，**与规则代码无关，改规则不用清缓存**；删掉整个目录即可重建；`REPORT_CHECKER_TABLE_CACHE=off` 关闭。改了缓存的存储内容要把 `FORMAT_VERSION` 加一。

## 测试

```bash
.venv/bin/python -m unittest tests.test_ptr_compare            # 需要 素材/，有缓存后约 30 秒
.venv/bin/python -m unittest tests.test_ptr_safe_equivalence tests.test_ptr_table_rows tests.test_ptr_false_alarms
.venv/bin/python -m unittest tests.test_ptr_missed_mismatches tests.test_ptr_fewer_manual tests.test_ptr_ocr_misread tests.test_ptr_ocr_sections
.venv/bin/python -m unittest tests.test_ptr_draft_runner tests.test_ptr_formal_runner tests.test_ptr_gold
.venv/bin/python -m unittest tests.test_run_state_server tests.test_workbench_upload tests.test_worker_protocol tests.test_table_cache tests.test_run_modes
```

- 页面脚本语法检查：把 `workbench-upload.html` 最后一个 `<script type="module">` 的内容取出来后 `node --check`。
- `test_workbench_contract` 一直失败（环境问题），不是回归。
- 依赖 `素材/` 的测试在没有样本的机器上会跳过；仓库没有自动检查（CI）。

## 必须遵守的工作约定

1. **不能让原来 OK 的变成不 OK。** 每家 PTR 写法都不同。改任何 PTR 规则后：
   - 跑 `.venv/bin/python -m mvp.ptr_gold diff`（默认拿 HEAD 当基线，比较未提交的改动；也可写 `diff <提交>`）。它在临时 worktree 里用基线代码跑全部样本、再用当前代码跑，逐条比较判定；有缓存时几秒钟。
   - 原“一致”不得变成待确认/不一致，原“不一致”不得消失：出现在“变差，需要处理”里的条目退出码为 1，必须处理。
   - 再跑 `.venv/bin/python -m mvp.ptr_gold check` 对答案（漏报、不该放行时退出码 1）。
   - 有变化的条目逐条说明原因，写进提交说明。
   - 跑单个样本：`compare_ptr_report(report_pdf, ptr_pdf, comparison_mode="rules", ocr_mode="auto")`（`mvp/ptr_compare.py`），样本在 `素材/report/<编号>/`、`素材/ptr/<编号>/`。
   - 当前样本（13 组）：0596 0775 0778 0780 1539 2170 2795 2948 3940 4788 5332 5780 5782（2170、5780、5782 的 PTR 是扫描件）。
2. **安全等价规则只在“完全相等”时放行**，并在 `text_comparison.equivalence_rules` 里记录用到的规则；表格必须逐格/逐行核对，不得只比“看起来像”。新规则的说明同步写进 `docs/PTR_P01_RULE_DRAFT.md`。
3. **扫描件（OCR）只能放行，不能报“不一致”**，唯一例外：报告自己印的数值单位和“单位：X”行互相矛盾（`REPORT_UNIT_CONTRADICTS_OWN_VALUE`，用户 11-14 同意）。
4. **公开仓库**：绝不提交 `素材/`、`output/`、PDF、docx、图片、数据库，也不提交真实客户/公司/产品名称（测试数据和文档里的例子要匿名化）。
5. 不要把 GitHub token 或任何 API key 写进聊天或代码；测试用 key 放在 git 忽略的本地配置文件里。
6. 不要运行 `playwright install`。
7. 提交信息用中文，说明“改了什么、为什么、回归结果”。只在用户要求时提交和推送。
8. 规则版本 `0.5.1-draft` 不要自行升级：升版本会牵动验收签字，由用户决定。
9. 用户觉得复核页太技术化；需要用户判断时，用直白的选择题问，说明用白话。

## 用户裁定口径

- **文字**：与 PTR 文字不同即“不一致”——删“等”/“应”、简写、图题少字、漏写标称值、“动作时候”≠“动作时”、“0.7”≠“0.70”都算。PTR 笔误而报告写对也报不一致。
- **忽略**：页眉页脚、报告单位行和结果小标题、全半角括号、冒号、换行空格。
- **整行挪位（11-14）**：报告把 PTR 的整行原样挪到另一处、逐字相同 → 一致；挪了又改字、少一行、只挪半句不适用。
- **排除**：不检条款（结果“/”）的抄录错误；PTR 自身目录/正文不一致；PTR 重复的条款号且不在检验范围、报告也没有。
- **父条款**只有标题、内容在子条款核对 → 一致。
- **11-14 逐条确认**：报告末尾多一行结果小标题（“±X 方向：”）→ 一致；报告照抄表格脚注、逐字相同 → 一致；只检表里一项但照抄整张表、内容相同 → 一致；PTR 条款名与 PTR 自己表格的行名写法不同（多一个“的”）→ 不一致；检验范围内的条款报告没写 → 不一致；报告抄错字 → 不一致。
- **按型号分行的表格（11-13）**：报告只抄第三页登记的送检型号那几行且数值相同 → 一致；登记写“见样品描述栏”时仍待确认。
- **扫描件（11-13、11-14）**：两个 OCR 引擎各自与报告一致 → 一致；或一个引擎与报告一致、另一个只读错个别字符（每处 ≤3 字符）→ 一致。“两侧都是数字”“两侧都是纯字母”的差异不放行是 Claude 实现时加的限制，不是用户要求。

## 标准答案（gold）

- `素材/gold/<编号>.json`：每条 `expected`（matched/mismatch/manual/excluded）、`status`（`confirmed`=人工确认 / `claude`=Claude 逐条判断）、`issue`。人工确认优先。`素材/expected/` 是早期工具输出的快照，**不是答案**。
- 工具：`mvp/ptr_gold.py`（`run` / `draft` / `review-page` / `merge` / `claude` / `check` / `diff`）。复核页生成到 `output/gold-review/index.html`，用 `.claude/launch.json` 的 `gold-review`（8790 端口）打开。
- 进度：996 条里 61 条人工确认，935 条是 Claude 判的。**“与答案相符”多数时候是对着 Claude 自己的判断**，汇报时要说清楚是哪一种。

## 当前结果（2026-11-14，13 组共 996 条）

一致 549 / 不一致 33 / 待确认 21（文字版 10、扫描件 11），其余为排除。`ptr_gold check` 无漏判。

**文字版待确认 10 条**（都没有安全的自动判法）：
- 0778 2.1.3b（报告省略名称列）、2.1.14（一张表拆成三张）、2.1.7 / 2.1.11（“单位：X”对应的数值在表格里）
- 0780 2.3.1.3 / 2.3.6.6b（报告只列送检型号并省略列；登记型号写“见样品描述栏”，样品描述表在第 4～5 页，可读出 38 个型号）、2.3.9.5（报告侧型号列没读成表格）
- 0596 2.1.5（公式换算出的范围）、0775 2.5.2.9（PTR 竖排合并单元格）、3940 2.1.8（条款写“心室不应期”，表格行名是“右室不应期”）

**扫描件待确认 11 条**：
- 真问题 1 条，受“不能报不一致”限制：2170 2.1.2.1（表2 长度允差）
- 表格 3 条：2170 2.1.1.2、5782 2.1.2、2.1.3
- 两个引擎都没读对：2170 2.3.1.5、5782 2.3.3、2.5.2.2；一个引擎多读出几个字符：5780 2.1.13
- 按答案应排除 3 条：2170 2.3.2、5782 2.1.5a、2.1.5b

## 实现上容易踩的地方

- `ptr_layout._clean_requirement_text` 会删掉“标准要求”栏里单独一行的 5～8 位数字（当作样品结果标签）。按型号分行的报告表格的行名因此另存在 `model_number_cells`，`ptr_compare._compare_text_keyed_by_model` 先带型号比一次，**只在完全一致时采用**。直接保留型号会让 0780 2.3.1.3、2.3.6.6b 变成误报的不一致。
- 表格展开的顺序：`compare_row_expansion` → `compare_multi_row_expansion` → `compare_parameter_column` → `compare_table_cells`（`mvp/ptr_table_rows.py`）。
- 扫描件：`extract_ptr_layout` 把其余 OCR 引擎各自分段的结果放在 `ocr_alternate_nodes`，`ptr_compare._ocr_dual_engine_confirmation` 决定能否放行。
- 报告在一条里展开印出所引用的条款时：重复的条款号行由 `ptr_layout._expanding_referrer` 归到引用它的条款，`ptr_compare._compare_with_expanded_references` 逐条与对应 PTR 条款核对（全部一致才放行）。
- 结果页的证据框坐标是 PyMuPDF 的“显示方向、左上角起”的坐标，画框时只按比例缩放（扫描件页面常带 /Rotate，不能再按未旋转页面换算）。
- 已知较松：参数列表格（`compare_parameter_column`）不要求报告抄其余列，只有 0596 2.1.6.1 一个样本支持。
- 已知问题：不是检验报告的 PDF 做报告自检会以 `ValueError: at least one rectangle is required` 失败，提示不直白。

## 待办（按优先级）

1. 请用户确认 Claude 判的答案，优先 11-13、11-14 新放行的条目和 Claude 没把握的条目。
2. 补测试：Grip 开口角度表核对（`full_report_attempt.py`）、`docs/prototypes/evidence-cards.mjs`、`AMBIGUOUS_PTR_MAPPING`。
3. 非报告 PDF 的出错提示改成直白说明。
4. 自动检查（CI）：只能覆盖不依赖 `素材/` 的测试。
5. 次要：模块之间的循环引用、`output/` 清理、旧文档归档。

## 已确认的真问题（不要被规则“修掉”）

- 0775 2.10.2.1：要求“≥0.06m³”，报告“单位：mm³”。
- 0596：报告删去 PTR 2.1.8“输入阻抗”后条款号整体前移（报告 2.1.8～2.1.12 = PTR 2.1.9～2.1.13）；2.1.2 PTR“1000Ω”报告写“2000Ω”；2.10 / 2.11 标准年份 2015 vs 2024；2.1.1 PTR 表内“0Dplus/DDIR”疑似笔误而报告写“Dplus/DDIR”。
- 1539 2.2.2：图 1 的标题报告少了型号预设名。
- 3940：2.1.6“干扰转复频率”在检验范围内而报告没写；2.1.15 PTR 条款名与表格行名不一致。
- 0780 2.3.5.1“材质力”、2.3.5.2“转向白如”：报告抄错字。
- 4788 2.1.11：报告少了“应”。
- 2170 2.3.1.2.1 / 2.3.1.2.2、2795：要求为 MΩ、报告“单位：GΩ”。
