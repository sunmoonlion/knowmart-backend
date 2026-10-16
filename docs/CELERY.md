# Backend 异步任务与运行角色

`knowmart-backend` 是统一后端源码，也是唯一后端镜像来源。部署时按职责启动四类独立工作负载：

| 角色 | 启动入口 | 职责 | 是否接收 HTTP 业务流量 |
|---|---|---|---|
| API | `python -m app.bootstrap.api` | Web、Admin、Internal API；投递异步任务 | 是 |
| Worker | `python -m app.bootstrap.worker` | 消费 Celery 队列并执行任务 | 否 |
| Scheduler | `python -m app.bootstrap.scheduler` | 周期任务调度 | 否 |
| Migration | `python -m app.bootstrap.migration` | Alembic 升级门禁，执行后退出 | 否 |

四个角色必须使用同一构建产物，但分别配置 Deployment/Job、ServiceAccount、资源限额、扩缩容与网络权限。不要创建独立 Worker 源码仓库，也不要恢复旧的 `celeryworker-knowmart-admin-backend` 或 `nodebullworker-knowmart-web-backend` 目录。

## Broker 与最小权限

- API 使用 producer 凭据，只允许向约定交换机或队列发布。
- Worker 使用 consumer 凭据，只允许消费、确认其负责的队列。
- Scheduler 只获得发布周期任务所需的最小权限。
- Migration 不需要 Broker 凭据。
- 应用只读取 `CELERY_BROKER_URL`；不同角色由部署层注入不同值。
- Broker URL、密码和令牌只能来自 Secret，不得写入镜像、ConfigMap 或日志。

默认队列由 `CELERY_QUEUE` 指定。模板只注册 `app.tasks.ping`，实例应用在自己的 Backend 内增加领域任务模块。

## 本地验证

```bash
cd app
uv run celery -A app.worker.celery_app inspect ping
uv run celery -A app.worker.celery_app call app.tasks.ping
```

启动 Worker：

```bash
cd app
uv run python -m app.bootstrap.worker
```

## Kubernetes 验收

`k8s-deployment` 生成 API、Worker、Scheduler 和 Migration 资源。验收必须确认：

1. 四种角色引用同一个不可变镜像 digest；
2. API、Worker、Scheduler 的 ServiceAccount 不相同；
3. Migration Job 成功后才允许 API/Worker rollout；
4. Worker 能消费真实任务，失败可重试且无重复副作用；
5. Scheduler 多副本或重启时不会重复调度；
6. API 不持有 consumer 凭据，Worker 不暴露业务 Service。

Worker 是否常驻、最小副本数和 HPA/KEDA 策略属于实例应用的容量决策，不属于源码拆仓决策。
