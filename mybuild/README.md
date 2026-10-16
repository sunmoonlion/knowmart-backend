# knowmart-backend 镜像构建

统一 Backend 的 API、Worker、Scheduler、Migration 使用同一镜像，以不同启动命令运行。

```bash
cd mybuild
./build-image.sh --tag architecture-v2-dev
./push-image.sh --tag architecture-v2-dev
```

构建上下文是 `knowmart-backend/` 根目录，Dockerfile 位于 `mybuild/Dockerfile`。本地默认镜像为 `knowmart-backend:architecture-v2-dev`；发布时必须使用通过完整门禁后确定的不可变版本和 digest，禁止由开发构建覆盖稳定标签。

构建变量统一使用：

- `BACKEND_IMAGE`
- `BACKEND_TAG`
- `BACKEND_IMAGE_REGISTRY`
- `BACKEND_IMAGE_PROJECT`

实例化后分别改成 `{app}-backend`，不要恢复旧的 `*_ADMIN_BACKEND_*` 变量。

网络受限环境可在命令前临时设置标准 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`；脚本只把它们作为 Docker 预定义构建参数传递，不把代理地址写入仓库配置。
