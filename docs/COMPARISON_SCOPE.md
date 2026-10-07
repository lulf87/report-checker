# 报告核对工具：正式比对范围矩阵

**版本**：2026-10-02  
**确认状态**：用户已确认 S01–S47 纳入正式范围；S48 在第一阶段排除。  
**适用模式**：`report_self`、`report_record_9706_1`、`report_record_9706_202`。  
**PTR**：`report_ptr` 仍保持禁用，原因是 `PTR_NOT_VALIDATED`；当前目录规则 `PTR-P01` 仅作为禁用占位，不将 PTR 自动比对视为已交付能力。

`report_ptr_report` 与 `report_diff` 已在能力目录中显式保留为禁用边界：前者沿用 `PTR_NOT_VALIDATED`，后者使用 `DIFF_NOT_SPECIFIED`。两者仍没有可执行规则计划；输入角色和规则冻结前必须在 preflight/create/worker/publish 全链路拒绝，不能从现有 `report_ptr` 或 Record 模式推导。

本文件是范围确认文件，不把“已列入范围”表述为“已经全部自动实现”。“实现状态”用于说明当前规则目录中已有的规则和仍需接入的检查项。`confirmed` 仅表示范围已确认；`validated` 表示当前实现、运行结果、证据与测试已闭合；`partial` 表示已接入但仍有字段、语义、发现范围或判定细节待扩展；`planned` 表示已纳入范围但尚未接入当前规则目录。纳入范围的对象在运行结果中必须进入 Coverage Ledger；无法可靠识别时记为 `manual`，不能静默跳过。

## 结果处置

| 结果 | 含义 |
| --- | --- |
| `matched` | 两侧证据充分且一致 |
| `mismatch` | 两侧证据充分且不一致 |
| `manual` | 对象已定位，但 OCR、手写、语义或映射证据不足，需要人工确认 |
| `not_applicable` | 当前模板明确不适用，并记录依据 |
| `excluded` | 经范围确认排除，并记录排除原因 |

每个范围对象都需要保留两侧原始值、页码、坐标或证据引用，以及对应的规则执行记录。Coverage 守恒只表示对象没有漏记，不表示对象自动通过。

## 范围总表

状态说明：`confirmed` 表示已确认纳入；`excluded_phase1` 表示第一阶段排除；`planned` 表示已纳入范围但尚未接入当前规则目录；`partial` 表示已接入但字段、语义、发现范围或判定细节仍待扩展；`validated` 表示当前实现、运行结果、证据与测试已闭合。

### A. 三个模式共用基础检查（S01–S08）

| 编号 | 比对对象 | 适用模式 | 状态 | 当前规则映射 |
| --- | --- | --- | --- | --- |
| S01 | PDF 可打开、未加密、未损坏 | 三个启用模式 | confirmed | 基础运行契约 |
| S02 | 文件 SHA-256 在运行前后保持一致 | 三个启用模式 | confirmed | 输入快照契约 |
| S03 | 页数、打印页码、总页数连续 | 三个启用模式 | confirmed | 共用页码契约 |
| S04 | 表头、列结构、表格数量符合当前模板 | 三个启用模式 | confirmed | 模板结构契约 |
| S05 | 序号、项目行、跨页续项无缺失、重复或跳号 | 三个启用模式 | confirmed | 共用序列契约 |
| S06 | 每个对象进入 Coverage Ledger，并有明确处置状态 | 三个启用模式 | confirmed | Coverage Ledger |
| S07 | 缺少对象、增加对象或模板变化时停止强行匹配 | 三个启用模式 | confirmed | 映射安全契约 |
| S08 | 结果保留 Report/Record 两侧页码、坐标和原始值 | 三个启用模式 | confirmed | Evidence 契约 |

### B. Report 自检（S09–S21）

| 编号 | 比对对象 | 状态 | 当前规则映射 |
| --- | --- | --- | --- |
| S09 | 首页与第三页的报告编号、委托方、样品名称、型号规格 | confirmed | `REPORT-R01` / validated |
| S10 | 第三页扩展字段：产品编号、批号、生产日期、委托方地址等 | confirmed | `REPORT-R02` / validated |
| S11 | 生产日期存在、格式正确、各位置一致 | confirmed | `REPORT-R03` / validated |
| S12 | 样品描述表与中文标签的对象、型号、序列号/批号、生产日期、失效日期 | confirmed | `REPORT-R04` / validated |
| S13 | 每个适用的实物样品对象均有对应实物照片 | confirmed | `REPORT-R05` / validated |
| S14 | 每个适用的实物样品对象均有对应中文标签照片 | confirmed | `REPORT-R06` / validated |
| S15 | 项目、标准条款、标准要求、检验结果、单项结论无漏填 | confirmed | `REPORT-R08` / validated |
| S16 | 多行检验结果聚合后与单项结论一致 | confirmed | `REPORT-R07` / validated |
| S17 | 检验结果数值符合同行接受标准 | confirmed | `REPORT-R07-B` / validated |
| S18 | 数值比较检查单位、上下限、误差、精度、符号和极性 | confirmed | `REPORT-R07-B` / validated |
| S19 | 正式项目序号连续、无重复、无漏项 | confirmed | `REPORT-R09` / validated |
| S20 | 跨页项目正确使用“续 N”标记 | confirmed | `REPORT-R10` / validated |
| S21 | 打印页码和“共 N 页第 M 页”连续完整 | confirmed | `REPORT-R11` / validated |

### C. GB 9706.1 Record + Report（S22–S35）

| 编号 | 比对对象 | 状态 | 当前规则映射 |
| --- | --- | --- | --- |
| S22 | Record 首页报告编号、制造商、样品名称、型号规格、出厂编号与 Report 实际值 | confirmed | `RECORD61-IDENTITY` / validated |
| S23 | Record 页码、打印页码、表头和模板结构完整 | confirmed | `RECORD61-STRUCTURE` / validated；范围映射仍由 `RECORD61-SCOPE` 支撑 |
| S24 | 物理页 6–96 的全部 851 个状态框均被识别 | confirmed | `RECORD61-BODY-STATUS` / validated |
| S25 | 状态框选中列、空白、注销线和异体状态框正确识别 | confirmed | `RECORD61-BODY-STATUS` / partial；需扩展状态语义 |
| S26 | 每个 Record 状态行映射到 Report 项目、条款、标准要求、建议/条件、检验结果 | confirmed | `RECORD61-SCOPE` / partial；需扩展字段级映射 |
| S27 | Record“符合、不符合、不适用”等状态与 Report 结果一致 | confirmed | `RECORD61-BODY-STATUS` / validated |
| S28 | Report 序号 1–117 的每个检验结果与 Record 状态聚合结果一致 | confirmed | `RECORD61-SEQUENCE-CONCLUSION` / validated |
| S29 | Report 序号 118 / 第 17 章按当前模板规则处置为 `not_applicable` | confirmed | `RECORD61-SEQUENCE-CONCLUSION` / validated |
| S30 | 当前 5 个数值块之外，当前已知模板中的全部可识别数值和百分比单元格 | confirmed | `RECORD61-BODY-NUMERIC`、`RECORD61-BODY-PERCENT` / partial；需扩展发现范围 |
| S31 | 数值单位换算、上下限、区间、精度、极性、最大值和聚合方式 | confirmed | `RECORD61-BODY-NUMERIC` / partial；需扩展判定细节 |
| S32 | Record 与 Report 的项目、条款、要求、建议、条件、单位、检验结果文本逐字段一致 | confirmed | `RECORD61-SCOPE` / partial；需扩展字段级比对 |
| S33 | Record/Report 缺失行、额外行、无法唯一映射行或模板版本变化 | confirmed | `RECORD61-SCOPE` / partial；需扩展异常分类 |
| S34 | Record 中“不符合”生成独立不符合警示 | confirmed | `RECORD61-NONCONFORMING-ALERT` / validated |
| S35 | 日期、检测仪器、检测人员、复核人员、签字、备注等元数据 | confirmed | `RECORD61-METADATA` / validated |

### D. GB 9706.202 Record + Report（S36–S48）

| 编号 | 比对对象 | 状态 | 当前规则映射 |
| --- | --- | --- | --- |
| S36 | Record 24 页报告编号逐页与 Report 一致 | confirmed | `RECORD202-NUMBER` / validated |
| S37 | `√`、`×`、`△`、`/` 图例的实际语义先完成核验 | confirmed | `RECORD202-SYMBOLS` / validated |
| S38 | 表 2 的 38 个项目、175 个 Record 逻辑行与 Report 176 个物理行正确映射 | confirmed | `RECORD202-BODY-STATUS` / partial；需扩展行级映射 |
| S39 | 项目名称、父条款号、标准要求和出现次序逐项一致；项目名采用严格相等判定 | confirmed | `RECORD202-BODY-STATUS` / validated；项目名严格比较已进入每个正文单元 |
| S40 | `√`、`×`、`△`、`/` 状态与 Report 检验结果一致 | confirmed | `RECORD202-BODY-STATUS` / validated |
| S41 | `×` 生成独立不符合警示 | confirmed | `RECORD202-NONCONFORMING` / validated |
| S42 | 所有可识别数值和百分比纳入比对 | confirmed | `RECORD202-BODY-NUMERIC`、`RECORD202-BODY-PERCENT` / partial；需扩展发现范围 |
| S43 | 数值接受标准、单位、精度、上下限、误差和区间 | confirmed | `RECORD202-BODY-NUMERIC` / partial；需扩展判定细节 |
| S44 | 表头、页码、行连续性、项目字段和结果字段完整 | confirmed | `RECORD202-STRUCTURE` / validated |
| S45 | 首页或固定位置的身份信息、产品信息、型号、批号等与 Report 比对 | confirmed | `RECORD202-IDENTITY` / validated；当前模板缺少 Record 固定字段时记录 `not_applicable` |
| S46 | 日期、检测仪器、检测人员、复核人员、签字、备注等元数据 | confirmed | `RECORD202-METADATA` / validated；当前模板缺少 Record 固定字段时记录 `not_applicable` |
| S47 | 表 2 中“见表 3”的状态行继续参加状态比对 | confirmed | `RECORD202-BODY-STATUS` / validated |
| S48 | 表 3 的文档、页面、原始测量值和重新计算结果 | excluded_phase1 | `not_applicable`；排除原因：第一阶段暂不实现表 3 独立内容核对 |

## 模式和规则目录约束

模式目录仍由 [mvp/capabilities.py](../mvp/capabilities.py) 维护。当前四个模式的生命周期状态如下：

| 模式 | 状态 | 本矩阵关系 |
| --- | --- | --- |
| `report_self` | enabled | S09–S21，以及 S01–S08 |
| `report_ptr` | disabled (`PTR_NOT_VALIDATED`) | 不纳入本次范围确认 |
| `report_record_9706_1` | enabled | S22–S35，以及 S01–S08 |
| `report_record_9706_202` | enabled | S36–S47，以及 S01–S08 |

当前规则目录中的规则 ID 必须在实现或范围文档中保持可追溯。新增范围项应先补充规则 ID、输入证据、结果处置和测试，再进入 enabled 规则计划。现有规则 ID 与本矩阵的对应关系见上表。当前目录中 `REPORT-R09-R10` 是早期兼容别名，不在现行 Report 自检 12 条规则计划中。9706.1 的模板外数值目标由 `RECORD61-NUMERIC-DISCOVERY` 显式记录；日期、仪器、人员、签字和备注由 `RECORD61-METADATA` 记录。9706.202 的项目字段由 `RECORD202-SCOPE` 记录，固定身份和元数据由 `RECORD202-IDENTITY`、`RECORD202-METADATA` 记录；当前模板不提供 Record 固定字段时用 `not_applicable` 保留依据。9706.202 的项目名称严格比较在 `RECORD202-SCOPE` 与正文 `RECORD202-BODY-STATUS` 中执行；规则结果仍可能因 OCR、符号或映射证据不足而为 `manual`。

Report 照片规则的适用对象按样品描述表逐行判定。备注明确写明“本次检测未使用”或“本次检验未使用”的条目，以及名称明确表示软件、模块、升级包、证书或功能的非实物条目，仍进入 Coverage Ledger，但照片存在性和照片内容规则记录为 `not_applicable`，并保留具体原因。其余实物条目继续按实物照片、中文标签照片和照片内容逐项核对；照片证据或 OCR 不足时记录为 `manual`。

## 验收口径

1. S01–S47 每次运行都必须在 Ledger 中有对象级记录；S48 必须记录为 `excluded` 或 `not_applicable`，并保留排除原因。
2. `matched`、`mismatch`、`manual` 三种结果都必须保存证据；`manual` 不能按通过处理。
3. 任何未能唯一映射、模板结构变化、对象缺失或输入证据不足的情况，进入 `manual` 或阻断匹配，不能静默丢弃。
4. 一个规则可以产生多个对象级 Finding；规则级汇总不代替对象级覆盖。
5. 只有在规则目录、执行结果、Coverage Ledger、Evidence 和测试均与本矩阵一致后，才可将对应待实现项标记为 `validated`。
