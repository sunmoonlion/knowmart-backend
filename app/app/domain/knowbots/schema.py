"""从外层模型生成 `knowbot-manifest` v1、v2 的 JSON Schema。

契约文件在父仓 contracts/knowbot/v1/、v2/。模型是唯一真源；文件由这里生成，测试核
两者逐字一致。改了模型就在后端目录下重新生成：
python -m app.domain.knowbots.schema \
    > ../../contracts/knowbot/v1/knowbot-manifest.schema.json
python -m app.domain.knowbots.schema 2 \
    > ../../contracts/knowbot/v2/knowbot-manifest.schema.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.domain.knowbots.manifest import KnowbotManifestV1, KnowbotManifestV2

MODELS: dict[int, type] = {1: KnowbotManifestV1, 2: KnowbotManifestV2}


def schema_id(major: int) -> str:
    return (
        "https://contracts.sunmoonai.com/knowmart/knowbot/"
        f"v{major}/knowbot-manifest.schema.json"
    )


def title(major: int) -> str:
    return (
        f"knowbot-manifest v{major} (outer envelope; `pack` is owned by the executor)"
    )


def manifest_json_schema(major: int = 1) -> dict[str, Any]:
    body = MODELS[major].model_json_schema(by_alias=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id(major),
        **body,
        "title": title(major),
    }


def render(major: int = 1) -> str:
    return json.dumps(manifest_json_schema(major), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    print(render(int(sys.argv[1]) if len(sys.argv) > 1 else 1), end="")
