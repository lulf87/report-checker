# 报告核对工具：需求与规则基线

> 文档状态：实现与需求基线草案 v0.1  
> 基线日期：2026-10-24  
> 适用模式：`report_self`、`report_record_9706_1`、`report_record_9706_202`

本文把当前项目已经写入代码、范围矩阵和测试的内容整理成一份可追溯基线。它用于回答“当前版本承诺检查什么、如何判定、证据如何保存、哪些内容仍未闭合”，不把已编码自动等同于已完成正式验收。

## 1. 基线来源和优先级

同一事项发生冲突时，按以下顺序处理：

1. 当前运行入口和规则目录：[`mvp/capabilities.py`](../mvp/capabilities.py)、[`mvp/run_modes.py`](../mvp/run_modes.py)。
2. 规则实现：[`mvp/run_report_self.py`](../mvp/run_report_self.py)、[`mvp/report_front_rules.py`](../mvp/report_front_rules.py)、[`mvp/report_format_rules.py`](../mvp/report_format_rules.py)、[`mvp/record_full.py`](../mvp/record_full.py)、[`mvp/full_record_61.py`](../mvp/full_record_61.py)、[`mvp/full_record_202.py`](../mvp/full_record_202.py)。
3. 范围和验收矩阵：[`docs/COMPARISON_SCOPE.md`](COMPARISON_SCOPE.md)。
4. 固定样本测试和运行结果：[`tests/`](../tests/)、`output/` 下的本地结果。
5. README、CHANGELOG 和历史计划只用于说明背景，不能覆盖当前规则目录和范围矩阵。

若代码目录和范围矩阵的状态不同，必须先更新本文件、范围矩阵和规则目录，再把规则标记为 `validated`。

## 2. 共同产品要求

| 编号 | 要求 | 当前基线 |
| --- | --- | --- |
| PR-01 | 三种模式独立运行、独立保存结果，Finding 不跨模式混入 | `report_self` 只产生 `REPORT-*`；两个 Record 模式只产生对应 `RECORD61-*` 或 `RECORD202-*` |
| PR-02 | 输入 PDF 只读，运行前后校验 SHA-256 | 统一 runner 和两个完整 Record runner 均保存输入哈希与完整性结果 |
| PR-03 | 自动解析不调用 LLM、Agent 或远程推理服务 | OCR 和笔迹处理在本机完成；不可靠证据进入 `manual` |
| PR-04 | 每个纳入对象必须进入 Coverage Ledger | `eligible == accounted`、边覆盖不重复且 `conserved == true` 才能发布完整运行 |
| PR-05 | 结果必须能回到源文档页码和坐标 | `Finding`、`Evidence`、Ledger 保存两侧原始值和位置；错误和人工复核不能只有摘要文字 |
| PR-06 | 不确定时保守退出 | 不用 Report 目标值反推 Record 的模糊状态或手写数值；不能把 `manual` 当作通过 |
| PR-07 | 只承诺已登记的模板和版式 | 未知模板、行数变化、模板指纹变化或无法唯一映射时阻断或转 `manual` |
| PR-08 | 未纳入范围的对象必须有明确原因 | 使用 `not_applicable` 或 `excluded`，保留范围依据；当前 9706.202 表 3 独立内容为第一阶段排除项 |

## 3. 三种模式的需求和规则计划

### 3.1 Report 自检

**输入**：1 份 Report。  
**规则计划**：18 条，`REPORT-R01`–`REPORT-R17` 加独立的 `REPORT-R07-B`。  
**范围**：S09–S21、S49–S54，以及共用的 S01–S08。  
**输出**：每条规则一个 `Finding`，每个范围对象进入 Report 自检 Ledger。

| 规则 | 需求摘要 | 当前实现/证据 |
| --- | --- | --- |
| R01 | 首页、封面和第三页身份字段一致 | `run_report_self.py`、`tests/test_run_modes.py` |
| R02–R04 | 样品描述、中文标签、生产日期和照片页字段核对 | `full_report_photo.py`、`photo_ocr.py`；OCR 不可靠时 `manual` |
| R05–R06 | 适用实物对象有实物照片和中文标签照片 | `full_report_photo.py` |
| R07 | 多行检验结果与单项结论聚合一致 | `run_report_self.py`、`checker.py` |
| R07-B | 实测结果符合同行接受标准，支持单位、区间、允差、精度和极性语义 | `mvp/full_report_attempt.py`、`mvp/run_report_self.py` |
| R08 | 项目、条款、标准要求、结果和单项结论无漏填 | `report_format_rules.py` |
| R09 | 新检验项目序号连续、无重复或跳号 | `checker.py`、`run_report_self.py` |
| R10 | 每页表头和跨页“续 N”标记 | `checker.py`、`report_headerless` 相关测试 |
| R11 | 打印页码和总页数连续 | `checker.py` |
| R12 | 首页“检验项目”范围、区间、分隔符、除外条款和标准编号写法 | `report_front_rules.py` |
| R13 | 首页“检验依据”“检验结论”固定措辞及公司名称一致 | `report_front_rules.py` |
| R14 | 到样、检验、签发日期格式、先后和封面版本 | `report_front_rules.py` |
| R15 | 页眉报告编号、样品编号和总页数 | `report_front_rules.py` |
| R16 | 职务、括号全/半角、送样/抽样字段内部一致 | `report_front_rules.py` |
| R17 | 首页样品信息可在照片页标签或铭牌中找到 | `full_report_photo.py`、本机 OCR |

### 3.2 Report + GB 9706.1 Record

**输入**：1 份 Report 加 1 份 GB 9706.1 Record。  
**规则计划**：10 条 `RECORD61-*` 规则。  
**已知模板范围**：Record 物理页 6–96、851 个状态行；Report 序号 1–117，序号 118/第 17 章按当前模板记为 `not_applicable`。

| 规则 | 需求摘要 | 当前状态 |
| --- | --- | --- |
| `RECORD61-IDENTITY` | 报告编号、制造商、样品名称、型号规格、出厂编号与 Report 实际值比对 | 已实现并有固定样本测试 |
| `RECORD61-BODY-STATUS` | 851 个状态框、空白、注销线、异体方框和状态与 Report 结果的映射 | 核心状态链已实现；范围矩阵 S25 仍为 `partial` |
| `RECORD61-BODY-NUMERIC` | 记录数值、单位换算、区间、精度、极性和最大值处理 | 已覆盖当前已知数值块；S30/S31 仍为 `partial` |
| `RECORD61-BODY-PERCENT` | 百分比抄录和最终百分比比较 | 已实现；发现范围仍受已知模板约束 |
| `RECORD61-SEQUENCE-CONCLUSION` | Record 父行/子行聚合与 Report 序号结论一致 | 已实现，包含序号 118 的不适用处置 |
| `RECORD61-SCOPE` | Record 行与 Report 项目、条款、要求、建议、条件、结果的唯一映射 | 已实现保守映射；字段级完整性和异常分类对应 S26、S32、S33，仍为 `partial` |
| `RECORD61-STRUCTURE` | 页码、打印页码、表头和模板结构 | 已实现；结构变化时人工复核或阻断 |
| `RECORD61-NUMERIC-DISCOVERY` | 发现模板外的可识别数值目标，不能静默漏掉 | 已登记并产生 Ledger 对象；仍以当前已知模板为边界 |
| `RECORD61-METADATA` | 日期、仪器、人员、签字、备注等元数据 | 规则 ID 保留；当前 Q1053–Q1058 明确为 `not_applicable` |
| `RECORD61-NONCONFORMING-ALERT` | Record 明确“不符合”产生独立警示 | 已实现并有测试 |

### 3.3 Report + GB 9706.202 Record

**输入**：1 份 Report 加 1 份 GB 9706.202 Record。  
**规则计划**：10 条 `RECORD202-*` 规则。  
**已知模板范围**：表 2 第 1–24 页、38 个项目、175 个 Record 正文逻辑单元、176 个 Report 物理行；每份运行还核对 24 个页级报告编号对象。

| 规则 | 需求摘要 | 当前状态 |
| --- | --- | --- |
| `RECORD202-NUMBER` | 24 页 Record 报告编号逐页与 Report 一致 | 已实现 |
| `RECORD202-BODY-STATUS` | 项目、父条款、标准要求、出现次序和 `√/×/△/` 状态逐项比对 | 状态语义和已知别名已实现；完整行级映射对应 S38，仍为 `partial` |
| `RECORD202-BODY-NUMERIC` | 可唯一读取的数值、单位、接受标准、上下限和区间比较 | 已实现当前模板路径；S42/S43 仍为 `partial` |
| `RECORD202-BODY-PERCENT` | 百分比抄录和区间端点比较 | 已实现当前模板路径；发现范围仍受模板限制 |
| `RECORD202-SYMBOLS` | 先核验四种状态符号的图例语义，再使用符号判定 | 已实现 |
| `RECORD202-STRUCTURE` | 表头、页码、行连续性和模板结构 | 已实现 |
| `RECORD202-SCOPE` | 项目、条款、要求、出现次序和模板指纹映射 | 已实现固定身份指纹和已知版式变体 |
| `RECORD202-IDENTITY` | 固定身份和产品字段与 Report 比对 | 当前模板没有固定字段时保留 `not_applicable` |
| `RECORD202-METADATA` | 日期、仪器、人员、签字、备注等元数据 | 当前模板缺少固定字段时保留 `not_applicable` |
| `RECORD202-NONCONFORMING` | `×` 生成独立不符合警示，同时保留正文一致性判断 | 已实现 |

表 2 中“见表 3”的状态行仍参加状态比对；表 3 独立文档、页面、原始测量值和重新计算结果属于 `excluded_phase1`，不应在当前版本宣称已覆盖。

## 4. 判定语义

### 4.1 覆盖处置

| 处置 | 含义 |
| --- | --- |
| `matched` | 两侧证据充分且一致 |
| `mismatch` | 两侧证据充分且不一致 |
| `manual` | 对象已定位，但 OCR、手写、极性、状态或映射证据不足 |
| `not_applicable` | 当前模板或双方内容明确不适用，并保留原因 |
| `excluded` | 已经确认不在本阶段范围，并保留排除原因 |

`manual` 不是通过；`succeeded` 只表示运行完整结束，不表示文档内容全部正确。

### 4.2 Record 状态映射

- 9706.1：`符合` 接受“符合要求”或实际测量值；`不符合` 对应“不符合要求”；`不适用` 对应“——”。Report 的固定模板斜线 `/` 与“——”按同一不适用状态处理。
- 9706.202：`√` 接受“符合要求”或实际测量值；`×` 对应“不符合要求”；`△` 对应“——”；`/` 对应 `/`。当前模板中 `△` 与 Report `/` 的等价处置已写入规则和测试。
- Record 状态聚合按真实父子层级进行；不能用行顺序、比例或 Report 目标值生成伪配对。

### 4.3 数值语义

- 支持已登记单位和 SI 词头换算。
- 按 Report 显示精度和 `ROUND_HALF_UP` 处理可比较数值。
- 明确的阈值、区间、百分比和最大值规则可自动判定；端点语义、极性或数值来源不明确时转 `manual`。
- Record 手写数值必须满足当前规定的独立识别或几何证据门槛；不能根据 Report 预期值直接补写 Record 读数。

## 5. 验收证据和当前缺口

### 已有证据

- `tests/test_comparison_scope.py`：范围项、规则目录和模式计划可追溯。
- `tests/test_record_full_rules.py`：状态、数值、百分比和 Coverage 守恒语义。
- `tests/test_full_record_61.py`：9706.1 固定样本、851 状态行、映射和数值路径。
- `tests/test_full_record_202.py`：9706.202 固定样本、199 个覆盖对象、模板指纹、符号和数值路径。
- `tests/test_report_scope_expanded.py`、`tests/test_run_modes.py`：Report 自检 18 条规则、模式隔离和完整 runner 契约。

### 尚未闭合的验收项

1. 范围矩阵中的 9706.1 S25、S26、S30–S33 仍需决定是补齐实现还是正式缩小范围。
2. 范围矩阵中的 9706.202 S38、S42、S43 仍需补齐行级映射和数值发现/判定边界。
3. 9706.1 元数据 Q1053–Q1058 是否长期排除，需要在需求基线中明确业务决定，而不只保留代码注释。
4. `RULE_CATALOG` 当前把相关规则标成 `validated`，但范围矩阵仍有 `partial`；两处必须统一。
5. 当前全套测试不能在干净检出中复现：缺少被忽略的照片 OCR 派生文件和已被设计文档引用的 `workbench-preview.html`；照片 OCR 集成基线也需要重新固定。
6. `RULE_CATALOG` 仍保留历史兼容别名 `REPORT-R09-R10`，它不在当前 Report 自检规则计划中；应明确其只读兼容用途或在正式发布前清理。

在以上项目关闭前，本基线的整体状态为“已编码、已在已知样本验证、部分范围待闭合”，不能标记为正式发布版需求。

## 6. 规则变更控制

任何改变判定语义、模板指纹、阈值、单位、OCR 采纳门槛、映射策略或范围的提交，必须同时完成：

1. 增加或更新对应规则版本；
2. 更新本文件和 `docs/COMPARISON_SCOPE.md` 的状态与验收口径；
3. 更新固定样本测试和结果差异说明；
4. 更新 `CHANGELOG.md`；
5. 重新生成规则包身份和 Run preflight 计划哈希；
6. 在发布提交或 tag 中记录基线日期和可复现测试命令。

当前 `rule_bundle_sha256()` 只覆盖规则目录元数据。正式发布前应把规则实现版本、规则资源或提交身份纳入可复现快照，避免代码语义变化但规则计划哈希不变。

## 7. 与现有文档的关系

- [`DESIGN.md`](../DESIGN.md)：产品与交互设计。
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md)：系统架构边界和状态模型。
- [`docs/API.md`](API.md)：HTTP API 契约与当前实现对照。
- [`docs/COMPARISON_SCOPE.md`](COMPARISON_SCOPE.md)：范围确认、状态矩阵和验收口径。
- [`docs/IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)：工程实施蓝图，不替代本需求基线。
- [`docs/PLAN_2026-10-20.md`](PLAN_2026-10-20.md)：历史改进计划，不替代当前规则状态。

