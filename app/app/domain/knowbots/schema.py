"""从外层模型生成 `knowbot-manifest/v1` 的 JSON Schema。

契约文件在父仓 contracts/knowbot/v1/。模型是唯一真源；文件由这里生成，测试核两者
逐字一致。改了模型就在后端目录下重新生成：
python -m app.domain.knowbots.schema \
    > ../../contracts/knowbot/v1/knowbot-manifest.schema.json
"""

from __future__ import annotations

import json
from typing import Any

from app.domain.knowbots.manifest import KnowbotManifestV1

SCHEMA_ID = (
    "https://contracts.sunmoonai.com/knowmart/knowbot/v1/knowbot-manifest.schema.json"
)
TITLE = "knowbot-manifest v1 (outer envelope; `pack` is owned by the executor)"


def manifest_json_schema() -> dict[str, Any]:
    body = KnowbotManifestV1.model_json_schema(by_alias=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **body,
        "title": TITLE,
    }


def render() -> str:
    return json.dumps(manifest_json_schema(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    print(render(), end="")
