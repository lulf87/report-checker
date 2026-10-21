# 报告核对工具：Report / Record 全量比对

**版本 1.1.0（2026-10-21）**：Report 自检、Report + GB 9706.1 Record、Report + GB 9706.202 Record 三个模式可用；PTR 比对尚未启用，留待后续版本。更新内容见 [CHANGELOG.md](CHANGELOG.md)。

这是从零搭建的本机工具，不继承旧项目的架构、代码、规则、阈值、接口或测试。公开仓库只包含代码、文档、工作台和测试源码；本地 PDF 素材、生成结果、SQLite 状态库和样本复核记录不随仓库发布。使用真实样本运行时，请把本地素材放在项目约定的 `素材/` 目录。

当前统一模式目录包含四种检测要求：

- `report_self`：Report 自检，已接入统一调度器，执行 18 条已登记 Report 规则（R01–R17，含独立 R07-B）。
- `report_ptr`：PTR + Report 比对，已登记为禁用能力，返回 `MODE_DISABLED / PTR_NOT_VALIDATED`，不生成空结果。
- `report_record_9706_1`：Report + GB 9706.1 Record 比对。
- `report_record_9706_202`：Report + GB 9706.202 Record 比对。

当前已完成并实际运行的可执行模式为 Report 自检（18 条规则）和两个 Record 比对模式：

- `Report 自检`
- `Report + GB 9706.1 Record`
- `Report + GB 9706.202 Record`

这里的“全量”是指：Record 模式对当前已知模板的纳入范围建立逐行 Coverage Ledger，Report 自检对已登记的 18 条规则逐条完成执行记录。具体纳入项、排除项和验收口径见 [正式比对范围矩阵](docs/COMPARISON_SCOPE.md)。自动解析和比对运行时不调用 LLM 或 Agent；原始 PDF 只读，runner 会在运行前后校验文件 SHA-256。

PTR 当前保持禁用状态。两个 Record 模式不混入 `REPORT-*` 自检 Finding；Report 自检只发布 `REPORT-*` Finding。

## Coverage Ledger

每个纳入范围的身份字段、正文行、实测单元格、结论行或页级编号都必须进入 Coverage Ledger。`eligible == accounted` 且 `conserved == true` 才能发布为完整运行；不允许静默跳过。Ledger 使用五种处置：

- `matched`：现有机器证据足以确认 Record 与 Report 一致。
- `mismatch`：现有机器证据足以确认两者不一致。
- `manual`：定位已经完成，但 OCR、手写值、极性、状态或映射证据不足，必须人工复核；该状态不是自动通过。
- `not_applicable`：对象已进入范围清单，但当前 Record 模板没有可比内容。
- `excluded`：对象已被明确识别并按既定范围排除，保留排除原因，不参与一致性结论。

同一结果中的 Finding 是面向问题查看的输出，Ledger 是逐对象覆盖和守恒依据。总览中的计数格式为 `Finding / Ledger`，两者不应互相替代。

## Report 自检

Report 自检当前固定执行以下 18 条规则，每条规则独立产生 RuleExecution 和 Finding：

- `REPORT-R01`：首页与第三页身份字段；
- `REPORT-R02`：样品描述字段与中文标签；
- `REPORT-R03`：生产日期值与格式；
- `REPORT-R04`：样品描述字段在照片页的覆盖；
- `REPORT-R05`：每个对象的实物照片；
- `REPORT-R06`：每个对象的中文标签照片；
- `REPORT-R07`：多行检验结果与单项结论；
- `REPORT-R07-B`：实测结果与同行接受标准；
- `REPORT-R08`：检验项目字段漏填；
- `REPORT-R09`：新检验项目序号连续性；
- `REPORT-R10`：检验结果表每页都有表头，跨页首项续 N 检查（缺少表头的页面按前面页面的表头继续核对，同时报格式问题）；
- `REPORT-R11`：打印页码连续性；
- `REPORT-R12`：首页“检验项目”写法（区间“～”、分隔“、”、连续条款区间、除外条款顺序、禁用“全性能/全项目”、标准编号连字符）；
- `REPORT-R13`：首页“检验依据”“检验结论”写法（产品技术要求全称、及变更对比表、“标准”、“提供”、“所检项目”、依据与结论一致、公司名称与委托方一致）；
- `REPORT-R14`：到样/检验/签发日期格式（不补零）与先后，已签发报告的封面版本号；
- `REPORT-R15`：每页页眉报告编号、样品编号与报告编号对应、总页数与实际页数；
- `REPORT-R16`：职务“授权签字人”、单位名称括号全/半角一致、送样/抽样字段内部一致；
- `REPORT-R17`：首页样品名称、型号规格、产品编号/批号、生产日期、生产单位、委托方及地址能在照片页标签（或铭牌）中找到。

R12–R16 的口径常量（封面版本表、除外条款顺序、职务名称等）集中在 [mvp/report_front_rules.py](mvp/report_front_rules.py) 顶部；只核对 Report 内部一致性，不核对合同和抽样单。

### 照片页文字识别（R02–R04、R17）

照片页的标签和铭牌文字由本机 OCR 识别，不联网、不调用大模型，实现位于 [mvp/photo_ocr.py](mvp/photo_ocr.py)：

- macOS 上使用系统自带的 Apple Vision（`ocrmac`）；
- 所有平台可用开源的 RapidOCR（`rapidocr-onnxruntime`，已加入依赖，模型随包安装）；
- 两个引擎都在时各识别一遍，作为两路独立读数；只装了一个时按单路处理。

判定口径：样品描述表和首页的值（忽略排版空白、全/半角）能在照片识别文字中找到即为一致；序列号/批号、日期在两路识别读出同一个不同值时判为不一致；其余差异转人工复核，并列出识别到的候选值。样品描述表没有该列、单元格为空或报告没有样品描述表时，不执行相应检查；报告填“/”时，照片标签上也没有该字段值才算一致；备注列不与照片比对。

识别结果写入每次运行目录的 `artifacts/photo-ocr.json`，`result.json` 的 `engine.photo_ocr` 记录所用引擎和版本。设置环境变量 `REPORT_PHOTO_OCR=off` 可关闭照片识别，此时 R02–R04、R17 中需要照片文字的项目保持人工复核。

无法可靠识别的字段或数值进入 `manual`，不能按自动通过处理。早期 `REPORT-R09-R10` 结果只作为兼容历史数据，当前规则计划使用独立的 R09 和 R10。

R02、R03、R04、R05、R06 的照片核对按样品对象适用性执行。备注明确标注“本次检测未使用”或“本次检验未使用”的条目，以及名称明确属于软件、模块、升级包、证书或功能的非实物条目，保留在 Coverage Ledger 中并记为 `not_applicable`；实物条目继续核对照片存在性、照片内容和日期/字段证据。

## Report + GB 9706.1 Record

当前已知模板的完整范围为：

- Record 首页 5 个身份字段，包括报告编号、制造商、样品名称、型号规格和出厂编号；手写字段仅在双 OCR 结果可靠一致时自动裁决。
- Record 物理页 6–96 的全部 851 个状态行，包括第 70、94 页的异体状态框（含 Wingdings 2 以“£”编码的方框）。
- Record 页码、打印页码、表头和模板结构由 `RECORD61-STRUCTURE` 单独记录；结构异常转人工复核或阻断映射。
- Report 序号 1–117 的正文双向覆盖，以及 117 个独立检验结果比对；Report 自检单独核对检验结果与单项结论。
- `4.11`、`8.6`、`8.7`、`9.6`、`16.6` 的数值或最终百分比目标；支持单位换算、区间判断、最大绝对值、Report 显示精度和 `ROUND_HALF_UP`。
- `4.11` 同一单元格内多个百分比按出现顺序映射；整格注销线从适用数值集合排除。
- Record 父行和子行结果按真实层级聚合；缺少唯一 Report 锚点时保持单边 Coverage，不用顺序或比例生成伪配对。
- Report 序号 118 / 第 17 章在当前 Record 模板中没有对应行，明确处置为 `not_applicable`。

数值来源 ID 同时记录物理页、打印页、重复页 occurrence、表格、行和列。Report 出现极性而 Record 没有明确极性证据时保持 `manual`，不会用 Report 值反推 Record 手写值。

## Report + GB 9706.202 Record

当前已知模板的完整范围为：

- GB 9706.202 表 2 的第 1–24 页、38 个项目和 175 个 Record 正文逻辑单元。
- 176 个 Report 正文物理行；项目 16 首行按一对二关系进入 Coverage。
- Record 24 页报告编号逐页核对；加上正文后，每份样本为 199 个 Record 覆盖对象和 177 个 Report 覆盖对象。
- `√`、`×`、`△`、`/` 状态符号与 Report 结果严格映射；`×` 另外产生不符合警示。
- 四种状态符号先由 `RECORD202-SYMBOLS` 核验；Record/Report 页码、表头和模板结构由 `RECORD202-STRUCTURE` 单独记录。
- 每个正文单元比较 Record 与 Report 的项目名称；已冻结的“电压、电流或/和能量的限制”项目别名按等价处理并保留双方原文，其他名称差异进入 `mismatch`，证据不足进入 `manual`。
- 唯一可机器读取的数值、单位换算、百分比抄录和百分比区间比较。
- 表 2 中“见表 3”的状态行继续参加比对；表 3 文档、页面及从表 3 原始测量值重新计算结果不在本模式范围。

模板映射按项目、父条款、出现次序和冻结的要求结构共同确认；单项映射证据异常时转为 `manual`，整体行清单不符合已知模板时停止运行，不强行匹配。

## 手写被拼合的 Record

部分导出（首次见于 iOS 27 导出的 3944）不再保留手写批注（Ink），手写只作为页面图形存在，同时附带一层不可见的手写识别文字，表格底纹还会多出一层内缩的重复色块。工具先检查 Record 是否含有 Ink 批注：有就按原方式读取；没有时由 `mvp/record_flattened.py` 在内存中整理后再交给同一套 9706.1 / 9706.202 规则，原文件不改动：

- 每个手写笔画（曲线填充图形，或 ≥0.8 pt 的直线工具笔画）转换为一条隐藏的 Ink 批注；9706.1 使用笔画轮廓（判断是否勾到方框），9706.202 使用还原的笔迹中线（区分 √、△、×、/）；
- 删除只以不可见方式出现的手写识别文字层（如“一”“注机上了”），避免混入表格文字；
- 删除与外层同色、完全内含的重复底纹，避免产生多余的表格列；内部含有其他图形（如手写）的底纹保留不动。

验证：把 2948 / 1539 的 Ink 批注拼合进页面后与原文件对比，9706.1 的 1054 项结果完全一致；9706.202 无新增错误，个别临界笔画在“一致”和“人工复核”之间变化（2948：1 项复核→一致；1539：3 项一致→复核）。运行结果的 `extraction.input_adapter` 记录手写来源和处理统计。

## 安装与单样本运行

安装项目依赖：

```bash
uv sync
```

统一模式调度器：

```bash
.venv/bin/python -m mvp.run_modes \
  --mode report_self \
  --report "/absolute/path/to/report.pdf" \
  --output "output/report-self-unified-20260930/<sample>"
```

PTR 模式会返回结构化的 `MODE_DISABLED` 错误，并保留 `PTR_NOT_VALIDATED` 原因码。

## 能力与规则目录服务

能力目录已经集中登记四种模式的启用状态、输入角色、规则计划和 PTR 原因码。启动只读本机服务：

```bash
.venv/bin/python -m mvp.capability_server --host 127.0.0.1 --port 8766
```

接口如下：

- `GET /healthz`：服务健康状态。
- `GET /api/v1/capabilities`：四种模式、输入角色、启用状态和规则 ID。
- `GET /api/v1/rules`：已验证规则的名称、规则族和适用模式。

服务只返回静态目录，不接收文件、不创建运行、不写入项目目录。统一运行仍使用 `mvp.run_modes`；PTR 仍由调度器和目录同时标记为 `PTR_NOT_VALIDATED`。

## Case / Run 状态持久化

SQLite 生命周期骨架位于 [mvp/run_store.py](mvp/run_store.py)，本机状态 API 位于 [mvp/run_state_server.py](mvp/run_state_server.py)：

```bash
.venv/bin/python -m mvp.run_state_server \
  --host 127.0.0.1 \
  --port 8767 \
  --database output/run-state.sqlite3
```

当前接口支持：

- `GET /api/v1/session`：取得本机写请求 CSRF token。
- `POST /api/v1/cases`、`GET /api/v1/cases`、`GET /api/v1/cases/{case_id}`：保存和读取 Case 元数据。
- `POST /api/v1/cases/{case_id}/runs:preflight`：按当前模式和规则包生成可复算的计划哈希。
- `POST /api/v1/cases/{case_id}/runs`、`GET /api/v1/runs/{run_id}`：创建 queued Run 和读取快照。
- `POST /api/v1/cases/{case_id}/documents`、`GET /api/v1/cases/{case_id}/documents`、`GET /api/v1/documents/{document_id}`、`GET /api/v1/documents/{document_id}/content`：上传、列出、读取 PDF Document，内容端点支持单区间 Range；Blob 按 SHA-256 去重。
- `GET /api/v1/runs/{run_id}/events`：读取持久事件账本。
- `GET /api/v1/runs/{run_id}/rule-executions`：读取该 Run 的规则执行计划和状态。
- `GET /api/v1/runs/{run_id}/findings`、`GET /api/v1/findings/{finding_id}`：读取已发布 Finding 与 Evidence。
- `GET /api/v1/runs/{run_id}/reviews`、`POST /api/v1/findings/{finding_id}/reviews`：读取和追加人工复核动作。
- `POST /api/v1/runs/{run_id}:cancel`：执行 queued 取消或 running 取消请求。

状态 API 默认严格要求每个输入 Document 已上传、属于当前 Case 且角色匹配；`allow_unresolved_inputs=True` 仅允许内存测试服务显式开启，文件数据库和生产入口始终拒绝 unresolved Run。创建 Run、上传 Document 和追加 ReviewAction 当前没有业务幂等键，事件通过列表接口轮询读取。

Run 生命周期已固定为 `queued → running → succeeded / failed / cancel_requested / interrupted`，并支持启动时把遗留的 `running` 和 `cancel_requested` 对账为 `interrupted`。Worker 协议位于 [mvp/worker_protocol.py](mvp/worker_protocol.py)，独立进程入口位于 [mvp/run_worker.py](mvp/run_worker.py)，同步协调器位于 [mvp/run_coordinator.py](mvp/run_coordinator.py)。状态 API 创建 Run 后自动排队单个 Worker；发布事务同时写入 Finding、Evidence、摘要和事件，复核动作追加到同一审计账本。

运行一份 GB 9706.1 Record：

```bash
.venv/bin/python -m mvp.run_full_records \
  --mode report_record_9706_1 \
  --report "/absolute/path/to/report.pdf" \
  --record "/absolute/path/to/gb9706.1-record.pdf" \
  --output "output/full-records-current-20261011/<sample>/report-record-9706-1"
```

运行一份 GB 9706.202 Record：

```bash
.venv/bin/python -m mvp.run_full_records \
  --mode report_record_9706_202 \
  --report "/absolute/path/to/report.pdf" \
  --record "/absolute/path/to/gb9706.202-record.pdf" \
  --output "output/full-records-current-20261011/<sample>/report-record-9706-202"
```

每个单样本目录生成：

- `result.json`：文件哈希、引擎信息、Coverage、Ledger、Finding 和证据坐标。
- `index.html`：该样本的结果明细。

9706.1 模式需要 OCR 时还会生成 `record61-ocr/` 局部证据图目录。

四份 Report 自检基线结果位于：

```text
output/report-self-unified-20260930-v3/
```

该目录现已按当前 runner 更新为 18 条规则的正式结果（2026-10-20 新增 R12–R16 前的 12 条规则结果保存在 `output/backups/report-self-v3-pre-front-rules-20261020/`，接入照片 OCR 前的 17 条规则结果保存在 `output/backups/report-self-v3-pre-photo-ocr-20261020/`）；旧的四规则快照保存在 `output/report-self-unified-20260930-v3-legacy/`，仅用于历史追溯。9706.1 和 9706.202 结果目录分别按当前 10 条规则计划生成。

## 正式结果与总览

当前六份正式运行位于：

```text
output/full-records-current-20261011/
```

样本组合为：

- GB 9706.1：`SAMPLE-A`、`SAMPLE-B`、`SAMPLE-D`。
- GB 9706.202：`SAMPLE-B`、`SAMPLE-C`、`SAMPLE-D`。

重新生成总览：

```bash
.venv/bin/python -m mvp.full_records_overview \
  --root output/full-records-current-20261011
```

总览产物为：

```text
output/full-records-current-20261011/index.html
output/full-records-current-20261011/summary.md
output/full-records-current-20261011/summary.json
```

当前正式结果快照如下。表内一致、不一致、待复核和不适用均为 Ledger 数量；Coverage 为 `eligible / accounted`。

| 样本 | 模式 | 一致 | 不一致 | 待复核 | 不适用 | Record Coverage | Report Coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| SAMPLE-A | GB 9706.1 | 970 | 0 | 18 | 54 | 1028 / 1028 | 972 / 972 |
| SAMPLE-B | GB 9706.1 | 970 | 1 | 20 | 55 | 1039 / 1039 | 972 / 972 |
| SAMPLE-D | GB 9706.1 | 964 | 5 | 42 | 37 | 1256 / 1256 | 974 / 974 |
| SAMPLE-B | GB 9706.202 | 189 | 0 | 10 | 0 | 199 / 199 | 177 / 177 |
| SAMPLE-C | GB 9706.202 | 158 | 28 | 13 | 0 | 199 / 199 | 177 / 177 |
| SAMPLE-D | GB 9706.202 | 183 | 1 | 15 | 0 | 199 / 199 | 177 / 177 |

当前真实工作台已经指向 `output/full-records-current-20261011/` 的六份复跑结果，六份 Record Ledger 数量如下：

| 样本 | 模式 | 一致 | 不一致 | 待复核 | 不适用 |
|---|---|---:|---:|---:|---:|
| SAMPLE-A | GB 9706.1 | 970 | 0 | 18 | 54 |
| SAMPLE-B | GB 9706.1 | 970 | 1 | 20 | 55 |
| SAMPLE-D | GB 9706.1 | 964 | 5 | 42 | 37 |
| SAMPLE-B | GB 9706.202 | 189 | 0 | 10 | 0 |
| SAMPLE-C | GB 9706.202 | 158 | 28 | 13 | 0 |
| SAMPLE-D | GB 9706.202 | 183 | 1 | 15 | 0 |

当前结果按项目字段比较重新计算；已冻结的“电压、电流或/和能量的限制”项目别名按等价处理并保留双方原文，其他项目名称差异进入 `mismatch`；图例和结构核验作为独立 Finding 保留。2948 的 GB 9706.1 当前正式结果已排除 Record 封面五项身份字段和 Q1053–Q1058 元数据，Ledger 为一致 964 / 不一致 5 / 待复核 42 / N/A 37；双横杠或整格斜线按 Record“不适用”与 Report `/` 等价处理，真正空白状态进入不一致；近框笔迹先由 Record 几何关系确定唯一最近状态，再由对应 Report 结果做上下文辅助确认，状态候选与 Report 冲突、多框或无法唯一定位时仍人工复核；数值项增加了严格的 Report 引导小数点恢复，只在每个适用数值格都有唯一候选且最大值换算后仍一致时自动确认；条款栏误写父级编号但正文唯一指向14.13时，按唯一上下文映射到 Report 14.13行。旧的 241 条结果保存在 `output/backups/full-records-current-20261011-pre-unify-20261011/`，本轮修复前的临时结果保存在 `output/backups/cross-row-status-pre-fix-20261013/`，状态语义修复前结果保存在 `output/backups/status-semantics-pre-fix-20261013/`，上下文近框规则启用前结果保存在 `output/backups/near-checkbox-context-pre-fix-20261016/`，本轮元数据排除和数值引导规则启用前结果保存在 `output/backups/metadata-numeric-guided-pre-fix-20261017/`。完整运行总览位于 `output/full-records-current-20261011/index.html`。

六份运行的 Coverage 均守恒，但每份都仍含 `manual` 或 `mismatch`，因此“运行完整”不等于“样本自动通过”。

## 脱敏上传工作台

公开仓库可直接使用的上传页面位于：

```text
docs/prototypes/workbench-upload.html
```

启动状态 API 和静态 HTTP 服务后打开：

```bash
.venv/bin/python -m mvp.run_state_server --port 8767
.venv/bin/python -m mvp.static_server --mode upload --bind 127.0.0.1 --port 8765
```

访问 `http://127.0.0.1:8765/` 进入报告核对前端；也可以直接访问 `http://127.0.0.1:8765/workbench-upload.html`。历史地址 `http://127.0.0.1:8765/docs/prototypes/workbench-upload.html` 继续兼容。安全静态启动器固定监听 `127.0.0.1`，上传模式只允许 `docs/prototypes` 下的页面和依赖，不会暴露项目根目录中的素材、运行结果、SQLite 数据库或 Git 元数据。选择 Report 自检、GB 9706.1 Record 或 GB 9706.202 Record，上传对应 PDF 后页面会创建 Case、上传 Document、执行 preflight、创建 Run，并轮询展示 Finding。PTR 选项保留为禁用状态并显示 `PTR_NOT_VALIDATED`。页面不引用项目素材、生成结果或真实样本。

9706.1 Record 如果不是已验证的 851 状态框模板，会生成一条带双侧证据的 `manual` 输入变体 Finding，并将未执行的规则标记为 `unsupported`，保留实际库存、模板期望和复核动作。Worker 不会把该情况报告成通用运行崩溃。

上传页面契约测试：

```bash
.venv/bin/python -m unittest tests.test_workbench_upload -v
```

静态启动器的回环、路径穿越和敏感路径阻断测试：

```bash
.venv/bin/python -m unittest tests.test_static_server -v
```

## 真实数据核对工作台

本机工作区的真实数据页面位于：

```text
docs/prototypes/workbench-real.html
```

该页面只读加载 10 份本地正式 `result.json`，依赖未随公开仓库发布的 PDF 素材和生成结果。公开仓库保留规则、服务和结果契约源码；真实工作台只在拥有对应本地数据的环境中运行。

必须在本项目根目录启动本机 HTTP 服务；8765 和 8767 不能继续复用其他副本启动的旧进程：

```bash
.venv/bin/python -m mvp.static_server --mode real --bind 127.0.0.1 --port 8765
```

当前 2948 的正式 GB 9706.1 结果固定读取 `output/full-records-current-20261011/2948/report-record-9706-1/result.json`；变更结果前先保留旧快照，避免工作台在不同副本和旧标签之间切换。

然后打开：

```text
http://127.0.0.1:8765/workbench-real.html
```

真实工作台的历史地址 `http://127.0.0.1:8765/docs/prototypes/workbench-real.html` 继续兼容；根路径在 `--mode real` 下会直接打开真实工作台。

真实模式仍只监听回环地址；启动器从页面中的 10 份运行清单读取 `result.json`，并且只允许这些结果声明的证据图片和 `素材/` 下源 PDF。项目根目录、未列入清单的 `output` 文件、SQLite、`.git` 和 `.aws` 均不会被静态服务读取。

通用 Record 数值比较层支持显式括号区间、阈值、单位换算、报告精度和显式极性判定；未声明端点包含关系的 `~`、`至`、`±` 文本进入人工复核。两个完整 Record 扫描器仍按各自模板路径逐步接入这些语义，未接入的字段继续保留 `partial` 状态。

不要直接双击 HTML 文件；`file://` 会受到浏览器本地资源读取限制。页面也会在这种情况下显示上述启动提示。

已接入的查看能力包括：

- 10 份正式运行选择，以及 Report 自检、GB 9706.1、GB 9706.202 三种可执行模式独立切换。
- PTR + Report 模式显示 `PTR_NOT_VALIDATED`，保持禁用状态。
- “有问题”“需复核”“没问题”状态筛选和 Finding 搜索。
- 顶部紧凑分开展示 `Finding/Q` 与 `Ledger` 正式计数；前者说明可点击的问题条目数量，后者说明逐对象的一致、不一致、待复核、不适用和排除数量，二者不互相替代。
- 点击 Finding 后，Report 自检打开完整 Report 单栏；Record 比对打开完整 Report 与 Record 双栏，并按 `pdf_page + bbox` 定位和高亮证据。
- Report 与 Record 独立翻页、页码输入和缩放；同一 Finding 含多个证据页时可以逐项切换。
- 加载时按模式校验结果模式、规则前缀、`status_counts`、Report 自检 Coverage 或 Record Ledger；拒绝计数不守恒、跨模式 Finding 和错误证据坐标。

工作台契约测试：

```bash
.venv/bin/python -m unittest tests.test_workbench_real -v
```

## 当前边界

- 当前实现只对项目内已经冻结并验证的已知模板负责；遇到未知版式、结构身份变化或行数变化时，不声明可以通用处理。
- Apple Vision 与 Tesseract 未形成可靠一致结果的手写字段或数值保持 `manual`；机器不会根据 Report 预期值反推 Record 内容。
- PTR 比对保持禁用，待独立规则验收后启用。
- 真实数据工作台当前接入 4 份 Report 自检和 6 份 Record 正式运行。
- 本机状态 API 已保存人工复核动作、Run 级 review revision 和复核后总体状态；签署和导出仍按产品页面单独实现。
- 当前 API 文档中的幂等键、SSE、Artifact 图片、导出和签名接口属于规划内容，代码路由与本 README 的已实现列表保持一致。
- 当前 10 份运行使用明确允许清单；新增样本或新的结果目录需要先完成同等 Coverage 与契约验证，再加入工作台。
