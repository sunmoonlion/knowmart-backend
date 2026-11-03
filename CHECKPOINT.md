# 2026-11-02 WP-10 单元 1：动态清单外层

基线 731cf3deaf2dab6627455bc2e0e00024d46add25，分支 feat/dynamic-methods。

新增 KnowbotManifestV3，显式动态目录运行版本；固定 v2 的生成 schema 逐字不变，规范 JSON/摘要与基线相同，v1 仍拒绝。公共外层字段与冻结绑定校验由泛型 envelope 复用；v3 只核骨架，不在商城复制 knowbench 的完整运行模型。父仓新增 v3 schema 与清单，knowlab/knowbench 持消费锁。

完整本地 PostgreSQL 回归 531 passed / 5 skipped（/tmp/luna-wp10-mart-full.txt），包括动态清单交付幂等、同版本不可改及召回目录原样；改动文件 ruff、pyright 通过。全仓既有 pyright 4 / ruff 17，隔离解包 HEAD 重跑逐条比对无新增（/tmp/luna-wp10-knowmart-baseline-comparison.json）；不能报告全仓静态检查通过。遗留问题在 market、delivery、repository 等，随总体验收清理。

本分支未发布、不改部署源。当前工作台拒绝动态执行，清单存取通过不等于专家可运行；WP-10 的规划、Attempt、视图及前端尚待后续单元完成。发布仍交 qwen。


## 2026-11-03 动态实施 G2/G4：公开能力目录投影

新增内部expert-directory/v2，完整公开scope/能力/确切版本摘要/费用；不输出方法源、内部绑定、私有数据。旧目录默认响应不变；超容量明确失败不截断宣称完整。详情复用既有labelled结构显示公开能力、条件与交付，v3亦按问题确认数据。

提供方schema生成锁与消费者比对通过；目录/详情20 passed，变更文件Ruff通过。全仓Ruff另有11个基线问题、Pyright有4个非本次文件错误（market.py、delivery.py、repositories/knowbots.py、market_routes.py），未混修；变更文件类型检查另列。未发布、未添加真实专家资产。
