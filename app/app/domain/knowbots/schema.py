"""从模型生成 knowmart 当提供方的两份 JSON Schema。

- 固定清单 `knowbot-manifest/v2` 与动态清单 v3：契约文件在父仓 contracts/knowbot/v2/
  及 v3/（v1 已删，decisions ⑰）；
- 交付请求 `knowbot-delivery/v1`（带类目，0032 待定第一项）：契约文件在父仓
  contracts/knowbot-delivery/v1/。

模型是唯一真源；文件由这里生成，测试核两者逐字一致。改了模型就在后端目录下重新生成：
python -m app.domain.knowbots.schema 2 \
    > ../../contracts/knowbot/v2/knowbot-manifest.schema.json
python -m app.domain.knowbots.schema delivery \
    > ../../contracts/knowbot-delivery/v1/knowbot-delivery.schema.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from app.domain.knowbots.delivery import KnowbotDeliveryV1
from app.domain.knowbots.manifest import KnowbotManifestV2, KnowbotManifestV3

MODELS: dict[int, type] = {2: KnowbotManifestV2, 3: KnowbotManifestV3}


def schema_id(major: int) -> str:
    return (
        "https://contracts.sunmoonai.com/knowmart/knowbot/"
        f"v{major}/knowbot-manifest.schema.json"
    )


def title(major: int) -> str:
    return (
        f"knowbot-manifest v{major} (outer envelope; `pack` is owned by the executor)"
    )


def manifest_json_schema(major: int = 2) -> dict[str, Any]:
    body = MODELS[major].model_json_schema(by_alias=True)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id(major),
        **body,
        "title": title(major),
    }


def render(major: int = 2) -> str:
    return json.dumps(manifest_json_schema(major), ensure_ascii=False, indent=2) + "\n"


DELIVERY_ID = (
    "https://contracts.sunmoonai.com/knowmart/knowbot-delivery/v1/"
    "knowbot-delivery.schema.json"
)


def render_delivery() -> str:
    body = KnowbotDeliveryV1.model_json_schema(by_alias=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": DELIVERY_ID,
        **body,
        "title": "knowbot-delivery v1 (knowlab delivers an expert version)",
    }
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "2"
    print(render_delivery() if which == "delivery" else render(int(which)), end="")
