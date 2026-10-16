# storage-access-bootstrap（统一 Backend）

为 `knowmart-backend` 声明对象存储访问关系。声明文件位于 `config/access.json`，默认生成 `knowmart-backend-s3` Secret/ConfigMap。

```bash
./storage-access-bootstrap.sh --cluster KIND
```

实例化后必须把 app、backend、资源名和 bucket 名替换为实例身份；Secret 只授予实际需要对象存储的运行角色。业务数据继续遵循唯一写入方原则。
