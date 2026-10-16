# db-access-bootstrap（统一 Backend）

用于为 `knowmart-backend` 声明并生成 PostgreSQL、MongoDB 与 Redis 的接入配置。统一 Backend 的 Web、Admin 与 Internal application layer 共享同一数据库所有权边界；运行角色按最小权限分别取得连接信息。

配置位于 `config/`：

- `*.k8s.env`：生成 Kubernetes Secret；
- `*.external.env`：生成本地临时环境文件；
- `common.env`：公共开关。

脚本通过同级 `db-provisioner/bin/dbctl` 完成声明式配置。输出中的密码或完整连接串不得提交到 Git。

```bash
cd knowmart-backend/db-access-bootstrap
./postgresql-access-bootstrap.sh --cluster KIND
./mongodb-access-bootstrap.sh --cluster KIND
./redis-access-bootstrap.sh --cluster KIND
```

实例化后，`SERVICE_NAME`、输出 Secret 和临时文件名应统一替换为 `{app}-backend`。
