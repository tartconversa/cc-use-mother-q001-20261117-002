# CycleFlow 机器人样机迭代数据服务

CycleFlow 面向样机研发团队，接收设计修订、部件批次、生产工单和质量试验等独立来源的事件。SQLite 同时保存原始事件身份和派生的业务状态。报表将设计版本、工单、工序耗时、物料批次、试验结果和质量问题连接起来，供团队解释每次迭代的交付效率。

## 运行

依赖版本锁定在 `requirements.txt`。在项目目录运行 `python -m unittest discover -s tests -v` 执行业务测试，`python -m compileall -q cycleflow` 检查源码，`python -m cycleflow.demo` 运行合成数据验收。安装所列依赖后，可设置 `CYCLEFLOW_DB` 为可写的数据库路径，再运行 `uvicorn cycleflow.api:app` 启动 HTTP 服务。命令行帮助为 `python -m cycleflow --db cycleflow.sqlite3 --help`。

演示仅使用临时数据库和本地合成产品。它分两次导入同一清单，验证试验良率，冻结月报，并审计存储的状态链；正常运行无需访问外部服务。

## 事件协议

向 `/events` 提交 `source`、`external_id`、`kind`、带时区的 `occurred_at` 和对象类型的 `payload`。相同事件重放返回既有编号；相同身份提交不同内容返回冲突。缺少前置对象或暂时缺料的事件进入待处理状态，可调用 `/events/{id}/retry` 补齐后重试。其他业务无效事件仍保留拒绝原因。批量接口每次使用稳定的批次编号和完整事件数组；系统核对清单指纹，从已提交的行继续。

事件类型包括 `design.release`、`design.retire`、`lot.receive`、`lot.quarantine`、`job.open`、`job.stage_start`、`job.stage_finish`、`job.scrap`、`plan.create`、`test.record`，以及修订处置事件 `revision.effect`、`revision.disposition`、`revision.withdraw`。设计包含产品、版本、部件数量及可选的父版本。部件批次包含料号、供应商和正整数数量。工单绑定一个设计和产量，成功预留物料后依次经过加工、装配和集成。试验计划绑定精确的产品版本，受测数量必须覆盖该工单全部单位。

## 修订生效与在制处置

修订发布时，团队用 `revision.effect` 登记其生效范围：一份在制工单清单，工单须未完工、当前版本低于该修订且开工不晚于生效时间。随后用 `revision.disposition` 登记对范围内单个工单的处置决定：`continue` 表示沿用开工版本，`upgrade` 表示把工单的有效设计切换到新修订，两者都必须附理由；同一工单同一时间只有一条生效决定，再决定前须先撤回。`revision.withdraw` 撤回工单当前生效的决定，有效设计随即还原；已完工或已试验的工单不可再撤回。处置只切换有效设计：原领料、加工与试验血缘保持不动，事件从不改写。试验按工单当前有效版本校验计划，报表按有效版本汇总且每个工单只汇总一次。报告的 `revision_impact` 段逐项说明范围内工单沿用旧版、转入新版或仍待处置，附理由、来源事件与撤回历史；范围内在制工单缺少生效决定时报告标记 `pending_disposition` 并阻止发布。`/effects/{design_id}` 可查看某次生效登记与其处置进展。

## 报表与发布

请求 `/reports/{product}?window_start=...&window_end=...` 获取当前视图。时间窗含起点、不含终点，以工单创建时间决定归属。报表提供按版本汇总、良率、工序时长、等待时长、来源事件编号和质量问题。周度、返工分组、瓶颈和供应商视图使用相同的已接受事实。供应商试验归因可以重叠，不能跨供应商直接求和。

向 `/releases` 提交发布编号、产品、时间窗和预览返回的 `expected_fingerprint`。未完成工单、范围内未处置工单或窗口内未解决事件阻止发布。发布后保留原报告和来源事件头，后续修订处置不改写已发布快照；`/releases/{id}/freshness` 比较当前视图与冻结视图。`/quality/audit` 检查事件身份、设计版本链、物料预留、工序顺序、试验记录、修订生效范围、处置决定与发布摘要。
