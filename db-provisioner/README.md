# db-provisioner

> **维护**：本目录位于 **knowmart-backend** 内，为统一 Backend 配套 **`db-access-bootstrap` 的唯一 `dbctl` 来源**；与独立 `k8s` 仓库无联动。迁移 Backend 仓库时请一并带走本目录。

统一数据库开通工具（`mongodb` / `postgresql` / `redis`），支持：

- `k8s` 场景：输出为 Kubernetes Secret
- `external` 场景：输出为本地 `.env` 文件

后续可按驱动机制扩展 `neo4j` 等其它引擎。

## 目录

- `bin/dbctl`：统一入口命令
- `bin/init-service-provision-template.sh`：为业务组件一键初始化 `db-access-bootstrap` 模板
- `drivers/`：数据库驱动实现
- `adapters/`：输出适配（k8s / external）
- `examples/`：示例配置
- `lib/common.sh`：公共函数
- `templates/db-access-bootstrap-template/`：通用组件模板（可复制到任意服务目录）

## 快速开始

以下命令均在 **db-provisioner 根目录**执行。若本目录下尚无 `examples/`，可将 `--config` 指到同 backend 的 `db-access-bootstrap/config/` 中对应 `*.external.env` 或 `*.k8s.env`。

```bash
chmod +x ./bin/dbctl
chmod +x ./bin/init-service-provision-template.sh
```

### 初始化“组件模板项目”（推荐）

```bash
./bin/init-service-provision-template.sh \
  --target-dir /home/zym/app/your-service \
  --service-name your-service \
  --namespace your-k8s-namespace \
  --pg-db-name your_db \
  --pg-db-user your_user \
  --pg-db-password your_password
```

执行后会在组件目录生成：

- `db-access-bootstrap/setup-external-db-access.sh`
- `db-access-bootstrap/desetup-external-db-access.sh`
- `db-access-bootstrap/config/*.env`

### MongoDB + k8s

```bash
./bin/dbctl \
  --config ./examples/mongodb-k8s.env \
  --target k8s
```

### PostgreSQL + external

```bash
./bin/dbctl \
  --config ./examples/postgresql-external.env \
  --target external
```

### PostgreSQL + k8s

```bash
./bin/dbctl \
  --config ./examples/postgresql-k8s.env \
  --target k8s
```

### Redis + dry-run

```bash
./bin/dbctl \
  --config ./examples/redis-k8s.env \
  --dry-run
```

### Rotate password（复用 provision 流程）

```bash
./bin/dbctl \
  --config ./examples/mongodb-k8s.env \
  --action rotate-password
```

### Deprovision（删除租户用户与输出凭据）

```bash
./bin/dbctl \
  --config ./examples/postgresql-external.env \
  --action deprovision --target external
```

### Deprovision 并删库（危险）

在配置文件中设置：

```env
DEPROVISION_DROP_DATABASE=true
```

然后执行 `--action deprovision`。  
Redis 场景需要再额外设置：

```env
REDIS_ALLOW_FLUSH_DB=true
```

## 配置约定（通用字段）

所有配置文件均为 `.env` 格式（`KEY=VALUE`）：

- `SERVICE_NAME`：服务名
- `ENVIRONMENT`：环境（dev/staging/prod）
- `TARGET_MODE`：默认输出目标（`k8s` / `external`）
- `DB_ENGINE`：`mongodb` / `postgresql` / `redis`
- `DB_HOST`、`DB_PORT`
- `APP_DB_NAME`、`APP_DB_USER`、`APP_DB_PASSWORD`

### MongoDB 驱动字段

- `MONGO_ADMIN_URI`（可选，优先）
- 或 `MONGO_ADMIN_USER` + `MONGO_ADMIN_PASSWORD` + `MONGO_AUTH_DB`（默认 `admin`）
- `DB_TLS_ENABLED`（`true/false`）

### PostgreSQL 驱动字段

- `PG_ADMIN_USER`、`PG_ADMIN_PASSWORD`
- `PG_ADMIN_DB`（默认 `postgres`）
- `PG_SSLMODE`（默认 `prefer`）

### Redis 驱动字段

- `REDIS_ADMIN_USER`、`REDIS_ADMIN_PASSWORD`
- `REDIS_DB_INDEX`
- `REDIS_KEY_PREFIX`（默认 `${SERVICE_NAME}:*`）：**可空格分隔多个 key 模式**，例如 `"session:* knowmart:*"`，须覆盖应用实际写入的 key（Nest BFF 会话为 `session:*`，仅 `knowmart:*` 会导致无法 `SET session:`）。
- `REDIS_ACL_CATEGORY`（默认含 `+@read +@write +@connection` 等）：**须含 `+@connection`**，否则 ACL 用户无法执行 `PING`，ioredis 连接会失败；NodeBull/Bull worker 还须 **`+@pubsub`**（`nodebull-redis.k8s.env` 已默认包含）。执行机必须使用支持 `--user` 的 `redis-cli`（Redis CLI >= 6）。
- `REDIS_CHANNEL_PREFIX`（可选）：Pub/Sub channel 模式，空格分隔，与 `REDIS_KEY_PREFIX` 语法相同；未设置且 `REDIS_ACL_CATEGORY` 含 `+@pubsub` 时，自动沿用 `REDIS_KEY_PREFIX`（Bull 的 `psubscribe` 需要 `&前缀` 权限）。
- `resetchannels` 会由 Redis 驱动固定放在 channel 授权之前；即使误写进 `REDIS_ACL_CATEGORY`，驱动也会先剥离，避免清空刚授予的 `&...` 权限。

### k8s 输出字段

- `OUTPUT_NAMESPACE`（默认 `default`）
- `OUTPUT_SECRET_NAME`（默认 `${SERVICE_NAME}-${DB_ENGINE}-conn`）

### external 输出字段

- `OUTPUT_ENV_FILE`（默认 `/tmp/${SERVICE_NAME}-${DB_ENGINE}.env`）

### Precheck 字段

- `DB_PRECHECK_ENABLED`（默认 `true`）
- `DB_PRECHECK_TIMEOUT_SECONDS`（默认 `60`）
- `DB_PRECHECK_INTERVAL_SECONDS`（默认 `3`）
- `K8S_PRECHECK_ENABLED`（默认 `false`）
- `K8S_PRECHECK_NAMESPACE`（默认 `default`）
- `K8S_PRECHECK_LABEL_SELECTOR`（如 `app.kubernetes.io/name=mongodb`）
- `K8S_PRECHECK_TIMEOUT_SECONDS`（默认 `120`）
- `K8S_PRECHECK_INTERVAL_SECONDS`（默认 `3`）

## 行为说明

- 幂等：重复执行会更新密码/权限，不会重复创建冲突对象
- 前置检查：执行顺序为 `k8s pod 级（可选） -> 数据库连接级（默认开启）`
  - k8s pod 级：`kubectl wait --for=condition=Ready pod -l <selector>`
  - MongoDB：`mongosh db.adminCommand({ ping: 1 })`
  - PostgreSQL：`pg_isready`
  - Redis：`redis-cli PING`

SunMoonAI 当前建议 selector（默认 `project_id=sunmoonai`）：
- MongoDB：`app.kubernetes.io/instance=mongodb-sunmoonai`
- PostgreSQL：`app.kubernetes.io/instance=sunmoonai`
- Redis：`app.kubernetes.io/instance=redis-sunmoonai`
- 输出字段统一包含：
  - `DB_ENGINE`
  - `DB_HOST`
  - `DB_PORT`
  - `APP_DB_NAME`
  - `APP_DB_USER`
  - `APP_DB_PASSWORD`
  - `APP_DB_URI`

## 安全提醒（非常重要）

- **不要把生产密码写进 repo**：示例配置中的 `change_me`/演示密码仅用于开发验证。
- **k8s 场景推荐**：通过 Secret/密管把敏感变量注入到执行环境中（例如 `PG_ADMIN_PASSWORD`、`APP_DB_PASSWORD`、`REDIS_PASSWORD`），让配置文件只保存非敏感参数。
- **Redis 要求**：
  - 业务服务不支持 Redis ACL username：使用 `REDIS_AUTH_ONLY=true`（只做密码认证/连通检查并写 Secret，不创建 ACL 用户）。
  - 执行机 `redis-cli` 太旧（无 `--user`）：请升级 `redis-cli` 到 >= 6；不再提供旧版客户端回退逻辑。
- **回收风险**：`deprovision` 默认只回收用户/凭据不会删库；若开启删库开关，请确保不会误删数据。

## 扩展新引擎（例如 Neo4j）

1. 新增 `drivers/neo4j.sh`
2. 实现 `neo4j_validate`、`neo4j_provision`
3. 在 `bin/dbctl` 的 `run_driver` 中新增分支

建议保持驱动接口一致：`validate + provision + deprovision`，并输出 `APP_DB_URI`。

## action 语义

- `provision`：创建/更新数据库租户（幂等）
- `reconcile`：等同 `provision`（便于 CI 声明式调用）
- `rotate-password`：等同 `provision`（通过修改 `APP_DB_PASSWORD` 完成改密）
- `deprovision`：
  - MongoDB：删除应用用户（数据库本身保留）
  - PostgreSQL：删除应用角色（数据库本身保留）
  - Redis：删除 ACL 用户
  - 输出凭据同时回收（k8s 删除 Secret；external 删除 env 文件）
  - 若 `DEPROVISION_DROP_DATABASE=true`：
    - MongoDB：额外执行 `dropDatabase()`
    - PostgreSQL：额外执行 `dropdb`（先终止连接）
    - Redis：仅在 `REDIS_ALLOW_FLUSH_DB=true` 时执行 `FLUSHDB ASYNC`
