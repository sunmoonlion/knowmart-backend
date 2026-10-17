# 公共可靠投递：Luna 实施与接入合同

任务来源：2026-09-11 所有者要求继续补齐模板和其他 App，完成后从 Luna 合入本地 master，再同步所有 worktree，以 master 为后续开发基线。

## 边界与约束

| 约束 | 实施 |
| --- | --- |
| C1/C2 | 领域状态与命令同事务；消费幂等、有限重试、死信、重放、周期性对账 |
| D1/D2/D6/D7 | 每 App 独立数据库、单线迁移；沿用 outbox_message/inbox_message，不新建业务主档；更新迁移门禁 |
| D8 | 独立 Migration Job；运行角色不自动迁移 |
| I1/C3/C4 | Admin/Web/Internal 共用应用用例；不改跨 App provider schema；运行现有双端契约测试 |
| R6 | 公共实现先进模板；通过后串行接入 Info、Knowledge、Knowbench；领域 handler 为显式扩展点 |
| T4/T5 | 子仓提交先推送可达分支，再更新父仓 gitlink；交付列出各仓提交 |

本任务交付源码基线与验证，不代表生产镜像已发布或业务数据库已迁移。既有正式发布清单仍描述其历史镜像；新的源码锁须与父仓 gitlink 一致，不把新源码冒充旧镜像。

## 使用合同

应用服务调用 `enqueue_task(session, topic, key, payload, deduplication_key)`，在同一 session 提交领域变更。函数不自行提交，也不访问 broker。相同业务键、相同意图返回原消息；不同意图拒绝。

`delivery_handlers.py` 是实例唯一的任务注册扩展点。消费者从数据库读取消息，Celery 参数只携带消息 UUID，不能提供任意函数名或替换业务载荷。模板没有业务 handler。

同 topic + aggregate_key 的执行使用 PostgreSQL 单调 epoch 租约，独立心跳续租。每次消费者 session 提交前校验 owner、epoch、消息、过期时间并锁定租约行，旧 worker 的写事务回滚。完成与 inbox 在同一事务提交；领域中间进度可以独立提交，但 handler 必须能从已提交进度安全恢复。业务终态、外部派生对象使用稳定业务标识；不得把 Inbox 当成外部系统恰好一次执行的证明。

`assert_execution_current` 在外部操作入口校验本地执行权；远端动作仍需目标侧幂等或查回执协议。未提供该协议的不可逆操作不得直接注册为可自动重试 handler。

Scheduler 每 5 秒扫描。发布成功但缺消费回执且无有效执行的命令重新排队；投递异常、持续无回执、反复 claim 后失联均有次数上限。死信通过 `python -m app.cli.durable_delivery dead-letters` 检查；`replay --message-id UUID` 显式重放，保留 inbox；`reconcile` 手动对账。网络错误只记录类型，防止凭据进入错误账。

## 切换与回滚

停止接收新任务并停止旧 Worker/Scheduler，盘点旧在途任务；运行实例迁移将旧持久命令映射到公共 Outbox。先备份与测试恢复，再以同版本部署 API/Worker/Scheduler。旧直接投递入口必须停用；禁止新旧执行器混跑。各 App 扩展迁移负责旧任务语义，不由模板猜测。

回滚先停止新入口和所有执行者，保留并导出待处理命令、dead-letter、inbox 和执行记录。恢复已验证的数据库备份与同版本代码；不得仅降级新表后让旧代码接管未完成的新命令。测试中的 migration downgrade/upgrade 验证 DDL 可逆，不代替业务数据备份恢复。

## 验证入口

在 app 目录执行 `ruff check .`、`pyright`、`DELIVERY_TEST_DATABASE_URL=<独立 *_tests 库> pytest -q`。数据库测试建立随机 schema，执行真实迁移链，覆盖事务回滚、冲突意图、并发消费、心跳、旧租约拒写、丢消息恢复、死信重放和迁移往返；没有指定测试库时明确跳过，不得称数据库验收通过。
