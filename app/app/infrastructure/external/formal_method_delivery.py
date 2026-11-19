"""Fetch the exact version from the runtime using a dedicated service identity."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.application.errors.exceptions import (
    BadGatewayError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.domain.market import FormalReference
from app.infrastructure.external.service_client import ServiceClient, service_relation


class DeliveryDocument(BaseModel):
    # Signed source notices, version strings and file paths are verbatim.
    model_config = ConfigDict(extra="forbid")


class DeliveryTerms(DeliveryDocument):
    origin: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
    notices: list[Annotated[str, Field(min_length=1, max_length=8192)]] = Field(
        max_length=16
    )


class SkillDependency(DeliveryDocument):
    method: FormalReference
    version: str = Field(min_length=1, max_length=20000)
    manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    terms: DeliveryTerms


class DeliveredFile(DeliveryDocument):
    path: str = Field(min_length=1, max_length=500)
    size: int = Field(strict=True, ge=0, le=512 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=128)
    data_base64: str = Field(max_length=699052, repr=False)


class DeliveredSkill(SkillDependency):
    files: list[DeliveredFile] = Field(min_length=1, max_length=256)


class DeliveryReceipt(DeliveryDocument):
    contract: Literal["knowbench/formal-method-delivery/v1"]
    owner_subject: str
    reference: FormalReference
    delivery_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=20000)
    purpose: str = Field(min_length=1, max_length=20000)
    skill_dependencies: list[SkillDependency] = Field(max_length=1001)
    package: DeliveredSkill | None = None


class FormalMethodDelivery:
    async def check(self, owner, reference, skill=None):
        relation = service_relation("knowbench", "method-delivery")
        if relation is None:
            raise ServiceUnavailableError("Formal method delivery is not configured")
        requested = {"owner_subject": owner, "reference": reference}
        if skill is not None:
            requested["skill"] = skill
        status, body = await ServiceClient(relation).request(
            "POST",
            "/formal-method-delivery",
            json=requested,
            accept=(400, 403, 404, 409, 422),
        )
        if status >= 400:
            raise NotFoundError("Formal version is unavailable")
        try:
            receipt = DeliveryReceipt.model_validate(body)
        except ValidationError:
            raise BadGatewayError("Invalid formal delivery response") from None
        if (
            receipt.owner_subject != owner
            or receipt.reference.model_dump(mode="json") != reference
            or (receipt.package is None) != (skill is None)
        ):
            raise BadGatewayError("Different formal method delivery")
        return receipt.model_dump(mode="json", exclude_none=True)
