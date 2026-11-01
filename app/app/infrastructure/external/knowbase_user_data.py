"""knowbase 的资料库（契约 user-data/v2，SDD 0043 第 4.3 节）：小店选资料、成交建使用权
（不复制）。

资料不再按应用分区，真源都在 knowbase；knowmart 没有上传，卖家在 knowbase 总栏把资料
「交给 knowmart」（用途 `market`），资料就进 knowbase 的 knowmart 栏，小店从这一栏选。
两种身份：
- 用户本人：请求带换来的用户令牌（`AuthService.user_token`，audience 是 knowbase
  在 Casdoor 里的客户端编号，配置 `KNOWBASE_USER_AUDIENCE`）；knowbase 按令牌的 `azp`
  认是本应用代办，只给看本应用自己的栏（`GET /columns/knowmart`）。
- 服务：按跨应用约定的「目标:关系」服务身份，环境变量 `KNOWBASE_INTERNAL_URL`、
  `KNOWBASE_USER_DATA_PURCHASE_SERVICE_*`（关系 user-data-purchase），见
  service_client.py：成交后请 knowbase 给买家建使用权，只到成交那一版，不复制；
  后台定时成批核条目里的资料还交给着 knowmart、还能卖（没有卖家本人的令牌，用同一个
  服务身份；`POST /service/market-offers/check`，knowbase 那头待加，形状见
  offer_states）；收清单时硬查、后台定时核专家所绑的数据（同一个服务身份；
  `POST /service/expert-bindings/check`，0043 第 4.3 节）。
knowbase 回 4xx 抛 KnowbaseRefused（带它的 code，调用方转成自己的话）；不可达、5xx 抛
ServiceUnavailableError。令牌只在内存里，不进日志。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.application.errors.exceptions import (
    BadGatewayError,
    ConcurrencyConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.infrastructure.external.service_client import ServiceClient, service_relation

BASE = "/api/internal/v2/user-data"
COLUMN = "knowmart"  # knowbase 里本应用的栏：用户交给 knowmart 的资料
SALE = "sale"  # 资料的许可档「可以出售」：只有这一档能上架
OFFER_STATES = frozenset({"offered", "not_for_sale", "gone"})
BINDING_STATES = frozenset({"live", "deleted", "disabled", "gone"})


class KnowbaseRefused(Exception):
    """knowbase 拒了：资料不在、不许出售、交易编号不对……"""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class KnowbaseLibrary:
    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
        **service_options: Any,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._transport = transport
        self._timeout = timeout
        self._service_options = {"transport": transport, **service_options}
        self._services: dict[str, ServiceClient] = {}

    # —— 用户本人 ——

    async def column(self, user_token: str) -> list[dict[str, Any]]:
        """本人交给 knowmart、自己还有使用权的资料（knowbase 书架条目的形状）。"""
        return await self._send("GET", f"/columns/{COLUMN}", user_token)

    # —— 服务 ——

    async def purchase(
        self,
        *,
        seller: str,
        material_id: str,
        buyer: str,
        transaction_ref: str,
        resale_allowed: bool = False,
    ) -> dict[str, Any]:
        token = await self._service_token("user-data-purchase")
        return await self._send(
            "POST",
            "/purchases",
            token,
            json={
                "seller": seller,
                "material_id": material_id,
                "buyer": buyer,
                "transaction_ref": transaction_ref,
                "resale_allowed": resale_allowed,
            },
        )

    async def offer_states(self, items: list[dict[str, str]]) -> Any:
        """成批核（卖家, 资料）：请求 {"items": [{"seller", "material_id"}]}（1 到 200
        份），回 {"items": [{"seller", "material_id", "state"}]}，state 是 offered（还
        交给着 knowmart、是卖家自己的、许可「可以出售」）、not_for_sale（还在，许可
        改低了）、gone（删了、不是卖家的、不再交给 knowmart）。只读。"""
        token = await self._service_token("user-data-purchase")
        return await self._send(
            "POST", "/service/market-offers/check", token, json={"items": items}
        )

    async def binding_states(self, items: list[dict[str, Any]]) -> Any:
        """成批核专家所绑的（作者, 资料, 版本）：请求 {"items": [{"owner",
        "material_id", "version_no"}]}（1 到 200 份），回 {"items": [{..., "state",
        "license", "shareable"}]}，state 是 live / deleted / disabled / gone。只读。"""
        token = await self._service_token("user-data-purchase")
        return await self._send(
            "POST", "/service/expert-bindings/check", token, json={"items": items}
        )

    # —— 内部 ——

    def _base(self) -> str:
        url = self._environ.get("KNOWBASE_INTERNAL_URL", "").strip()
        if urlsplit(url).scheme != "https":
            raise ServiceUnavailableError(
                "knowbase is not configured", code="knowbase_unconfigured"
            )
        return url.rstrip("/") + BASE

    async def _service_token(self, relation: str) -> str:
        client = self._services.get(relation)
        if client is None:
            found = service_relation("knowbase", relation, self._environ)
            if found is None:
                raise ServiceUnavailableError(
                    "knowbase is not configured", code="knowbase_unconfigured"
                )
            client = ServiceClient(found, **self._service_options)
            self._services[relation] = client
        return await client.token()

    async def _send(
        self, method: str, path: str, token: str, *, json: Any = None
    ) -> Any:
        url = self._base() + path
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                "knowbase is unreachable", code="knowbase_unavailable"
            ) from exc
        if response.status_code >= 500:
            raise ServiceUnavailableError(
                "knowbase failed", code="knowbase_unavailable"
            )
        if response.status_code >= 400:
            try:
                answer = response.json()
            except ValueError:
                answer = None
            code = answer.get("code") if isinstance(answer, dict) else None
            raise KnowbaseRefused(response.status_code, str(code or "knowbase_refused"))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()


def material_view(entry: dict[str, Any]) -> dict[str, Any]:
    """knowbase 书架条目 → 小店选资料要的几样：名字、当前那一版、许可、能不能卖。

    能卖 = 卖家自己的（买来的不能再卖：knowbase 只认所有者出售）、没删、许可是「可以
    出售」（只有自己上传的能改成这一档，公开原件、采集来的、产出都不行）。
    """
    material = entry.get("material") or {}
    version = entry.get("version") or {}
    license_ = str(material.get("license") or "")
    mine = material.get("mine") is True
    return {
        "material_id": str(material.get("material_id")),
        "name": str(material.get("name") or ""),
        "version_no": int(version.get("version_no") or 0),
        "media_type": str(version.get("media_type") or ""),
        "size_bytes": int(version.get("size_bytes") or 0),
        "sha256": str(version.get("sha256") or ""),
        "license": license_,
        "mine": mine,
        "sellable": mine and license_ == SALE and material.get("deleted") is not True,
    }


class KnowbaseVault:
    """端口 KnowledgeVault 的实现（SDD 0043 第 4.3 节）。

    列卖家交给 knowmart 的资料、成交后给买家建使用权。
    """

    def __init__(self, client: KnowbaseLibrary) -> None:
        self.client = client

    async def materials(self, user_token: str) -> list[dict[str, Any]]:
        try:
            entries = await self.client.column(user_token)
        except KnowbaseRefused as exc:
            raise BadGatewayError("knowbase refused", code=exc.code) from None
        return [material_view(e) for e in entries or [] if isinstance(e, dict)]

    async def offer_states(
        self, items: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        if not items:
            return {}
        try:
            answer = await self.client.offer_states(
                [{"seller": seller, "material_id": m} for seller, m in items]
            )
        except KnowbaseRefused as exc:
            raise BadGatewayError("knowbase refused", code=exc.code) from None
        rows = answer.get("items") if isinstance(answer, dict) else None
        asked = set(items)
        out: dict[tuple[str, str], str] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            key = (str(row.get("seller")), str(row.get("material_id")))
            # 只认问过的、认得的状态；别的当没回（不据此下架）
            if key in asked and row.get("state") in OFFER_STATES:
                out[key] = str(row["state"])
        return out

    async def binding_states(
        self, items: Sequence[tuple[str, str, int, str | None]]
    ) -> list[dict[str, Any]]:
        """端口 BindingChecker：按问的顺序回每份的状态；回包对不上就当 knowbase 出错
        （不据此收清单、不据此改在售状态）。"""
        if not items:
            return []
        try:
            answer = await self.client.binding_states(
                [
                    {"owner": owner, "material_id": m, "version_no": v}
                    | ({"basis_ref": ref} if ref else {})
                    for owner, m, v, ref in items
                ]
            )
        except KnowbaseRefused as exc:
            raise BadGatewayError("knowbase refused", code=exc.code) from None
        rows = answer.get("items") if isinstance(answer, dict) else None
        if not isinstance(rows, list) or len(rows) != len(items):
            raise ServiceUnavailableError(
                "knowbase answered the binding check partly",
                code="knowbase_unavailable",
            )
        out: list[dict[str, Any]] = []
        for (owner, material_id, version_no, _), row in zip(items, rows, strict=True):
            if (
                not isinstance(row, dict)
                or (row.get("owner"), row.get("material_id"), row.get("version_no"))
                != (owner, material_id, version_no)
                or row.get("state") not in BINDING_STATES
            ):
                raise ServiceUnavailableError(
                    "knowbase answered the binding check out of order",
                    code="knowbase_unavailable",
                )
            out.append(
                {
                    "state": row["state"],
                    "license": row.get("license"),
                    "frozen_license": row.get("frozen_license"),
                    "shareable": row.get("shareable") is True,
                }
            )
        return out

    async def purchase(
        self,
        *,
        seller: str,
        material_id: str,
        buyer: str,
        transaction_ref: str,
        resale_allowed: bool = False,
    ) -> dict[str, Any]:
        try:
            return await self.client.purchase(
                seller=seller,
                material_id=material_id,
                buyer=buyer,
                transaction_ref=transaction_ref,
                resale_allowed=resale_allowed,
            )
        except KnowbaseRefused as exc:
            if exc.status == 404:
                raise NotFoundError(
                    "The seller's material is no longer there", code="item_gone"
                ) from None
            if exc.code == "not_for_sale":
                # 卖家上架以后把许可改低了：这一笔建不成，照实告诉买家
                raise ConcurrencyConflictError(
                    "The seller no longer sells this material",
                    code="item_not_for_sale",
                ) from None
            raise BadGatewayError("knowbase refused", code=exc.code) from None
