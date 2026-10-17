# 只读投递观测（B7d / B7h / B7j）

在后端 app 目录运行：

```bash
python -m app.cli.delivery_metrics
python -m app.cli.delivery_metrics --format prometheus
```

入口只打印 JSON v1 或 Prometheus text 0.0.4，不启动 HTTP 服务、不写采集文件。
使用现有 Settings 的本 App 数据库连接；操作人必须已有对应 DB 权限，不能把命令
或凭据交给浏览器。公共观测只需相关表 SELECT；领域 observer 另需其租约表 SELECT。
不调用 claim、pump、reconcile、replay 或 GC，不联 broker，不依赖 Web 登录。

## 统计语义

`delivery_observers.py` 是显式领域扩展点：默认包装现有 DurableTasks 的 handler 清单；
领域应装配实际 Delivery 实例，不能复制 topic/consumer/lease 的影子配置。
模板没有领域 topic 是正常空清单；未注册消息单独报聚合数量，不将 DB topic 自动注册。
Knowbench 加入实际 AgentDelivery：使用 agent.executor 回执和 Agent 租约，notification
没有业务 Inbox，发布成功即满足传输完成；绝不把 notification 当必需 Agent 回执。

| gauge 字段 | 含义 |
| --- | --- |
| messages / incomplete_messages | 保留记录数 / 未达到该 policy 完成条件的记录数 |
| retained_receipt_messages | 当前保留的 Outbox 中，存在精确 consumer/message_id 已提交 Inbox 回执的消息数；不统计错误 consumer、孤立回执或无回执策略 |
| latest_receipt_recorded_timestamp_seconds | 上述回执 processed_at 的最大 Unix 秒；无匹配回执为 0，需与回执数量一起解释；不是精确提交时间或 Worker 心跳 |
| scheduled_messages | 未完成且 immutable not-before 尚未到期，不等于退避 available_at |
| publishable_messages | 已到期、无活跃执行/死信、未完成，pending 或发布租约过期且退避已到期 |
| awaiting_receipt_messages | published 但缺必需业务回执，可能仍正常执行或已有死信 |
| active_execution_messages | 未完成且该 policy 执行租约有效的消息数，不是 Worker/进程数 |
| expired_publisher_leases | 未完成 delivering 且发布租约过期，不是执行租约数 |
| dead_letter_messages | 尚未显式 replay 的死信记录数 |
| reconcile_candidates | published、缺必需回执、超过该 policy lease_seconds、无活跃执行/死信 |
| oldest_due_incomplete_age_seconds | 已到期、未完成且非死信的最老年龄，起点取 created_at/not-before 较晚者；包含正常执行及退避中的任务 |
| unregistered_messages | 不在任何 observer topic 中的 Outbox 数量；须调查，不等于可安全删除 |
| snapshot_timestamp_seconds | 采集开始的 DB 时间；用于识别过时文本，不能当心跳证明 |

所有计数均为 gauge；保留窗/回滚/归档会改变数值，不使用 `_total` 假装不可回退 counter。
无消息 ID、用户、dataset、payload、错误正文或动态数据库标签。标签只来自经过界限
校验的代码 policy/topic（最多 16 policies、128 topics）；重叠 topic 配置拒绝。
快照为 REPEATABLE READ / READ ONLY，整体 2 秒协作式预算，单条 SQL 1.5 秒；
租约检查沿用实际 policy 的 DB 时钟，年龄基准为快照开始时间，不声称所有时钟调用同一纳秒。
权限、缺表、非法时间字段、超时失败时退出 2，只打印通用错误，不打印半份或假零指标。
取消向上传播。大账本扫描仍有成本，超时不是性能容量证明，需要按实际规模验收。

## 受保护 HTTP 采集（B7h）

B7j 的两个回执字段同样由此入口和原 CLI 输出，保持既有 JSON v1 的加法字段；
不改原字段/鉴权或创建新端点。回执只在提交后对另一事务可见，失败回滚不计入，
相同 consumer/message_id 重复消费不增；无回执 hint 的发布成功不当作 Worker 回执。
`processed_at` 由现有数据库写入规则产生（默认 NOW，即写事务开始时间），不是
commit timestamp 或单调序号。墙钟回拨、乱序完成可使新增回执没有推进最大时间；
有限未来值原样反映，非有限时间使整个采集失败，不静默当成健康值。
未声明完成回执的当前长任务、空闲、凭据/采集失败不能单靠“无新回执”区分。
指标按 policy/topic 聚合，不标识哪一个 Worker，也不证明业务结果正确、完整产品 Task
成功或所有 Worker 都健康；例如领域 handler 已记录终止失败后正常返回也可能留回执。
归档/恢复或删除关联 Outbox 后数量/最大时间可能下降，不能对 gauge 使用 counter 的
rate/increase 语义。进展判定须联合采集新鲜度、待处理数、长任务策略和保留/恢复事件；
不将该值直接绑定 readiness/liveness 或自动重启。

测试专用 Worker 入口在 tests/，只替换数据库适配和合成 handler；真实共用 Celery
execute、DurableTasks 租约/提交及 Inbox 路径不替换。真实 prefork 子进程暂停但父进程
pong 仍通时无新增回执，恢复后提交才增加；失败回滚与重复投递不虚增。合成数据库
和计数器证明的是共享执行链路，不是当前业务部署、真实 Provider 或业务结果验收。

API 新增 `GET /api/internal/v1/delivery/metrics`，返回完整 Prometheus text 0.0.4。
CLI 保留原行为，HTTP 不调用 CLI 初始化/关闭函数，而使用 API lifespan 管理的连接池。
复用同一个 collector / domain observer，语义、静态标签边界和只读事务不变；不接收
SQL、topic 或数据库地址参数。接口不执行投递、重放、删除或其他修改，也不支持写方法。

必须以 `Authorization: Bearer ...` 提交现有 OIDC 验证器可验证的签名服务 JWT：
issuer、有效期、signature 和本 App `service_auth_audience` 校验不变；subject 精确
命中 `service_auth_subject_bindings`，令牌实际携带 `delivery:observe`，且全部 scope
都在该 subject 绑定内。缺身份/无效令牌为 401，未绑定/缺 scope/超配 scope 为 403；
授权失败前不访问数据库。Cookie 或浏览器 audience 不可替代服务身份。
本包不默认授权任何现有 subject，不创建客户端/凭据、不改部署 Secret；正式接线需为
采集器配置专用主体、短期令牌刷新、每 App 的 audience/最小绑定及允许访问的网络路径。
不要把令牌放 URL、前端、文档或日志，也不把浏览器登录口改成采集入口。

每 API 进程最多一个在途数据库采集，额外请求立即 503
`delivery_observation_busy`，不在进程里堆积等待队列；这是本进程负载保护，不是集群锁
或全局限流，多个 API 副本仍可各自扫描。鉴权在准入前，OIDC 请求沿用身份模块自身
超时；2 秒 collector 预算不包括鉴权，不宣称整个 HTTP 请求只有 2 秒。
数据库/策略/渲染异常返回 503 `delivery_observation_failed`，只有安全错误码进入日志，
不输出原 SQL/参数/DSN、不输出半份指标或假零值。取消传播并释放准入；完成后才发响应，
无服务器快照缓存。成功、401/403、503、405 响应均 `Cache-Control: no-store`。

采集器必须检查 HTTP 状态和快照时间；503/断连不能沿用旧值当本次成功。快照是 DB
时间，DB 与采集器的时钟偏差也需独立监测，未来时间戳不能直接当“新鲜”；无消息也不
等于消费健康。接口已有可采集源码不等于已有 Prometheus 作业、告警路由或在线身份。

## 仍待上线接线

HTTP 已补受保护 scrape 入口；CLI 本身仍不是 textfile collector 定时作业、告警路由或
Pod 健康探针。后续须确定采集 principal、周期、实际部署/网络边界、告警阈值和
接收者，再实测断采/凭据撤销/DB 故障/告警到达。可优先观察未注册消息、死信、待对账数量
及最老未完成年龄；阈值必须考虑长任务/计划延迟，不能默认零容忍并误报运行中任务。
不得把旧文本保留当本次采集成功；CLI 消费者须检查新鲜度和退出码，若使用 textfile
仍须原子写入与过期清除；HTTP 消费者须检查新鲜度和状态码。

空积压不证明 Worker 消费队列正确，有效租约不证明业务有进展，旧租约保留行不等于故障。
未涵盖 RabbitMQ queue 指标、Scheduler heartbeat、retrieval/run/SSE 的产品指标；这些
仍由 B7/N4 对应范围接收，未因本次观测输出而完成。归档/删除也尚未获准或实现。

文本按 [Prometheus 官方 exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/)
组织 HELP/TYPE 与唯一标签样本，末尾换行；不引入进程内累加器或第二份持久化业务账。
