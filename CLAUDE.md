# knowmart-backend — 局部编码规则

> 进入本目录时自动叠加。**本文件只约束局部编码。**
> 项目全貌见 `../../k8s/docs/project-guide/overall-architecture.md`；
> 本仓细节见 `../../k8s/docs/project-guide/repos/knowmart-app.md`。
> **改代码前先读 `../../k8s/docs/dev-investment-agent/tree-build/rules/constraints.md`**（39 条，按主题分组）。
> 与代码冲突时以代码为准。

## 技术栈

Python ≥3.12 · FastAPI · async SQLAlchemy · Alembic · pydantic-settings ·
Redis（会话）· Celery（异步任务）· uv（依赖，锁文件 `uv.lock`）

## 进程入口：四角色一镜像

**同一个镜像按不同命令启动四种进程**，入口都在 `app/app/bootstrap/`：

| 角色 | 入口 |
| --- | --- |
| API | `app/bootstrap/api.py` — `create_app()` 工厂在这里 |
| Worker | `app/bootstrap/worker.py`（Celery） |
| Scheduler | `app/bootstrap/scheduler.py`（Celery beat） |
| Migration | `app/bootstrap/migration.py`（一次性） |

⚠ `app/app/main.py` 只有 5 行，是向后兼容的 ASGI 转发，**不是真正的入口**，不要改它。

## 目录结构

```
app/
├── core/config.py          Pydantic Settings + 生产期硬校验（配错则进程起不来）
├── app/bootstrap/          四个运行角色入口
├── app/domain/             领域模型、状态机、命令
├── app/application/        服务编排、DTO、ports（接口定义）
├── app/infrastructure/     ORM、外部适配、Celery、存储（ports 实现）
├── app/interfaces/
│   ├── http/               模板面：admin/web 认证、diagnostics、web interaction
│   ├── schemas/            请求/响应 schema
│   └── errors/             统一 problem+json 处理
├── app/tasks/              Celery 任务
├── alembic/versions/       迁移链（单链线性，见下）
└── tests/                  含 test_kernel_invariants.py
```

**本仓没有 `interfaces/endpoints/`**——模板不含领域。三个实例仓把自己的业务路由
放在 `endpoints/`，模板提供的通用面留在 `http/`。往模板加东西前先确认它是否真的
属于"每个实例都需要"，否则应该加到实例仓。

同理 `domain/{models,repositories,services}/` 在本仓是空骨架，等实例填。

## 会让你失败的规则

| 规则 | 后果 |
| --- | --- |
| `app/application/` 不得出现 `app.interfaces` 字符串 | `test_kernel_invariants` 失败 |
| 领域层不引用任何外层；应用层不引用基础设施层、接口层、组装层 | `test_layering` 失败 |
| 应用层与领域层不直接引用数据库、网络、消息这类库（`sqlalchemy`、`httpx`、`redis`、`celery`、`fastapi` 等） | `test_layering` 失败 |
| 接口层的三个面（`http/admin`、`http/web`、`http/internal`）互不引用 | `test_layering` 失败 |
| 改迁移必须同步改 `tests/test_kernel_invariants.py` 里的文件名清单 | 测试失败（清单是逐字比对的） |
| 迁移链必须单链线性，恰好一个 `down_revision = None` | 测试失败 |
| `pyproject.toml` 与 `uv.lock` 的 version 必须同为 `2.0.0` | 测试主动断言，须与正式发布别名一致 |
| 凭据一律经 pydantic-settings 从环境变量读 | 生产期配置校验会拒绝 |
| `.dockerignore` 须排除 `app/.env`、`app/.env.*`、`app/tests` | 测试失败 |

**配置错误的表现是启动抛异常，不是运行期降级**——`core/config.py` 有约 35 处生产期
硬校验。改配置后先起一次进程验证。

## 三件套

```bash
cd app
uv sync --frozen
uv run ruff check .
uv run pyright
uv run pytest -q
```

## 分层的自动检查

契约在 `app/pyproject.toml` 的 `[tool.importlinter]`，共四条；`pytest` 会跑它，也可以单独跑：

```bash
cd app
uv run lint-imports
```

要用数据库或外部服务：先在 `app/application/ports/` 写接口，实现放 `app/infrastructure/`，
在 `app/bootstrap/` 里接上。不要往 `ignore_imports` 里加新的条目——那是旧账的放行清单，只减不增。

## 跨应用跳转

一个应用把用户带到另一个应用的某个页面，只有一种做法。规则在 `app/app/domain/cross_app.py`。

| 项 | 约定 |
| --- | --- |
| 形式 | 普通链接，新标签页 |
| 链接里带 | 业务参数、`from`（从哪个应用来）、`ref`（来处的引用） |
| 链接里不带 | 身份、令牌、回去的地址 |
| 回去的地址 | 目标应用按 `from` 在自己的配置里查，把 `ref` 填进去。从不从链接里取 |
| 链接带来的参数 | 不可信。不认识的应用、不合规则的引用当作没带，不报错 |
| 配置 | `CROSS_APP_SOURCES_JSON`（谁可以带人来、各自的回跳地址）、`CROSS_APP_TARGETS_JSON`（带人去哪、各自网页端的地址）。都可不配 |
| 接口 | `GET /api/web/v1/cross-app/links`、`GET /api/web/v1/cross-app/origin?from=&ref=` |
| 拼链接 | 只在网页端的一处拼。后端不拼去别的应用的链接 |

## 动手前

1. 读 `app/interfaces/endpoints/` 与 `app/interfaces/http/` 确认现有路由
2. 读 `app/infrastructure/models/` 确认数据字段
3. 改模型必须生成 Alembic 迁移，不手改表结构
4. 跨 App 契约的 schema 真源在 provider 仓，本仓若是 consumer 只改锁文件，
   且必须双端测试通过——见 `../../k8s/docs/project-guide/topics/contracts.md`
