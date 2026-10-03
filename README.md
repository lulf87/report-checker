# 报告核对工具：Report / Record 全量比对

这是从零搭建的本机工具，不继承旧项目的架构、代码、规则、阈值、接口或测试。公开仓库只包含代码、文档、工作台和测试源码；本地 PDF 素材、生成结果、SQLite 状态库和样本复核记录不随仓库发布。使用真实样本运行时，请把本地素材放在项目约定的 `素材/` 目录。

当前统一模式目录包含四种检测要求：

- `report_self`：Report 自检，已接入统一调度器，执行 12 条已登记 Report 规则（R01–R11，含独立 R07-B）。
- `report_ptr`：PTR + Report 比对，已登记为禁用能力，返回 `MODE_DISABLED / PTR_NOT_VALIDATED`，不生成空结果。
- `report_record_9706_1`：Report + GB 9706.1 Record 比对。
- `report_record_9706_202`：Report + GB 9706.202 Record 比对。

当前已完成并实际运行的可执行模式为 Report 自检（12 条规则）和两个 Record 比对模式：

- `Report 自检`
- `Report + GB 9706.1 Record`
- `Report + GB 9706.202 Record`

这里的“全量”是指：Record 模式对当前已知模板的纳入范围建立逐行 Coverage Ledger，Report 自检对已登记的 12 条规则逐条完成执行记录。具体纳入项、排除项和验收口径见 [正式比对范围矩阵](docs/COMPARISON_SCOPE.md)。自动解析和比对运行时不调用 LLM 或 Agent；原始 PDF 只读，runner 会在运行前后校验文件 SHA-256。

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

Report 自检当前固定执行以下 12 条规则，每条规则独立产生 RuleExecution 和 Finding：

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
- `REPORT-R10`：跨页首项续 N 检查；
- `REPORT-R11`：打印页码连续性。

无法可靠识别的字段或数值进入 `manual`，不能按自动通过处理。早期 `REPORT-R09-R10` 结果只作为兼容历史数据，当前规则计划使用独立的 R09 和 R10。

## Report + GB 9706.1 Record

当前已知模板的完整范围为：

- Record 首页 5 个身份字段，包括报告编号、制造商、样品名称、型号规格和出厂编号；手写字段仅在双 OCR 结果可靠一致时自动裁决。
- Record 物理页 6–96 的全部 851 个状态行，包括第 70、94 页的异体状态框。
- Record 页码、打印页码、表头和模板结构由 `RECORD61-STRUCTURE` 单独记录；结构异常转人工复核或阻断映射。
- Report 序号 1–117 的正文双向覆盖，以及 117 个独立单项结论比对。
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
- 每个正文单元严格比较 Record 与 Report 的项目名称；不一致进入 `mismatch`，证据不足进入 `manual`。
- 唯一可机器读取的数值、单位换算、百分比抄录和百分比区间比较。
- 表 2 中“见表 3”的状态行继续参加比对；表 3 文档、页面及从表 3 原始测量值重新计算结果不在本模式范围。

模板映射按项目、父条款、出现次序和冻结的要求结构共同确认；单项映射证据异常时转为 `manual`，整体行清单不符合已知模板时停止运行，不强行匹配。

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
  --output "output/full-records-complete-20260930/<sample>/report-record-9706-1"
```

运行一份 GB 9706.202 Record：

```bash
.venv/bin/python -m mvp.run_full_records \
  --mode report_record_9706_202 \
  --report "/absolute/path/to/report.pdf" \
  --record "/absolute/path/to/gb9706.202-record.pdf" \
  --output "output/full-records-complete-20260930/<sample>/report-record-9706-202"
```

每个单样本目录生成：

- `result.json`：文件哈希、引擎信息、Coverage、Ledger、Finding 和证据坐标。
- `index.html`：该样本的结果明细。

9706.1 模式需要 OCR 时还会生成 `record61-ocr/` 局部证据图目录。

四份 Report 自检基线结果位于：

```text
output/report-self-unified-20260930-v3/
```

该目录现已按当前 runner 更新为 12 条规则的正式结果；旧的四规则快照保存在 `output/report-self-unified-20260930-v3-legacy/`，仅用于历史追溯。9706.1 和 9706.202 结果目录分别按当前 10 条规则计划生成。

## 正式结果与总览

当前六份正式运行位于：

```text
output/full-records-complete-20260930/
```

样本组合为：

- GB 9706.1：`SAMPLE-A`、`SAMPLE-B`、`SAMPLE-D`。
- GB 9706.202：`SAMPLE-B`、`SAMPLE-C`、`SAMPLE-D`。

重新生成总览：

```bash
.venv/bin/python -m mvp.full_records_overview \
  --root output/full-records-complete-20260930
```

总览产物为：

```text
output/full-records-complete-20260930/index.html
output/full-records-complete-20260930/summary.md
output/full-records-complete-20260930/summary.json
```

当前正式结果快照如下。表内一致、不一致、待复核和不适用均为 Ledger 数量；Coverage 为 `eligible / accounted`。

| 样本 | 模式 | 一致 | 不一致 | 待复核 | 不适用 | Record Coverage | Report Coverage |
|---|---|---:|---:|---:|---:|---:|---:|
| SAMPLE-A | GB 9706.1 | 847 | 0 | 250 | 1 | 1034 / 1034 | 978 / 978 |
| SAMPLE-B | GB 9706.1 | 825 | 0 | 277 | 1 | 1045 / 1045 | 978 / 978 |
| SAMPLE-D | GB 9706.1 | 800 | 3 | 300 | 1 | 1262 / 1262 | 980 / 980 |
| SAMPLE-B | GB 9706.202 | 175 | 16 | 8 | 0 | 199 / 199 | 177 / 177 |
| SAMPLE-C | GB 9706.202 | 144 | 44 | 11 | 0 | 199 / 199 | 177 / 177 |
| SAMPLE-D | GB 9706.202 | 169 | 17 | 13 | 0 | 199 / 199 | 177 / 177 |

当前真实工作台已经指向 `output/full-records-manual-reduction-20260930-v1/` 的六份正式结果，六份 Record Ledger 数量如下：

| 样本 | 模式 | 一致 | 不一致 | 待复核 | 不适用 |
|---|---|---:|---:|---:|---:|
| SAMPLE-A | GB 9706.1 | 847 | 0 | 250 | 1 |
| SAMPLE-B | GB 9706.1 | 825 | 0 | 277 | 1 |
| SAMPLE-D | GB 9706.1 | 800 | 3 | 300 | 1 |
| SAMPLE-B | GB 9706.202 | 175 | 16 | 8 | 0 |
| SAMPLE-C | GB 9706.202 | 144 | 44 | 11 | 0 |
| SAMPLE-D | GB 9706.202 | 169 | 17 | 13 | 0 |

当前结果按严格项目字段比较重新计算；项目名称差异会进入 `mismatch`，图例和结构核验作为独立 Finding 保留。完整运行总览位于 `output/full-records-manual-reduction-20260930-v1/index.html`。

六份运行的 Coverage 均守恒，但每份都仍含 `manual` 或 `mismatch`，因此“运行完整”不等于“样本自动通过”。

## 脱敏上传工作台

公开仓库可直接使用的上传页面位于：

```text
docs/prototypes/workbench-upload.html
```

启动状态 API 和静态 HTTP 服务后打开：

```bash
.venv/bin/python -m mvp.run_state_server --port 8767
python3 -m http.server 8765
```

访问 `http://127.0.0.1:8765/docs/prototypes/workbench-upload.html`，选择 Report 自检、GB 9706.1 Record 或 GB 9706.202 Record，上传对应 PDF 后页面会创建 Case、上传 Document、执行 preflight、创建 Run，并轮询展示 Finding。PTR 选项保留为禁用状态并显示 `PTR_NOT_VALIDATED`。页面不引用项目素材、生成结果或真实样本。

上传页面契约测试：

```bash
.venv/bin/python -m unittest tests.test_workbench_upload -v
```

## 真实数据核对工作台

本机工作区的真实数据页面位于：

```text
docs/prototypes/workbench-real.html
```

该页面只读加载 10 份本地正式 `result.json`，依赖未随公开仓库发布的 PDF 素材和生成结果。公开仓库保留规则、服务和结果契约源码；真实工作台只在拥有对应本地数据的环境中运行。

在项目根目录启动本机 HTTP 服务：

```bash
python3 -m http.server 8765
```

然后打开：

```text
http://127.0.0.1:8765/docs/prototypes/workbench-real.html
```

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
