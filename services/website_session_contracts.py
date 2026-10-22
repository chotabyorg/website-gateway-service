"""Closed Website wire profile mirrored from Conversation PRs37/40/41.

These are transport DTOs only; authority, history and delivery remain in Conversation.
"""
from datetime import datetime
from typing import Annotated, Literal
import unicodedata
from uuid import UUID

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field, IPvAnyAddress, SecretStr,
                      StrictBool, field_serializer, model_validator)


def validate_owner(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 255 or not value.strip()
            or value == "*" or len(value.encode("utf-8")) > 1020
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)):
        raise ValueError("invalid owner")
    return value


def validate_text(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 16384 or "\x00" in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
            or len(value.encode("utf-8")) > 32768):
        raise ValueError("invalid text")
    return value


OwnerId = Annotated[str, BeforeValidator(validate_owner)]
TextContent = Annotated[str, BeforeValidator(validate_text)]
PositiveCounter = Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WebsiteSessionScope(Contract):
    owner_id: OwnerId
    integration_id: UUID
    agent_id: UUID
    binding_generation: PositiveCounter


class WebsiteSessionCreate(WebsiteSessionScope):
    client_ip: IPvAnyAddress = Field(repr=False)


class WebsiteSessionCreated(Contract):
    session_id: UUID
    session_token: SecretStr = Field(repr=False)
    expires_at: datetime

    @field_serializer("session_token", when_used="json")
    def wire_token(self, value: SecretStr) -> str:
        return value.get_secret_value()


class WebsiteSessionAccess(WebsiteSessionScope):
    session_id: UUID
    session_token: SecretStr = Field(repr=False)


class WebsiteSessionAdmission(WebsiteSessionAccess):
    message_id: UUID
    text: TextContent


class WebsitePublicFrame(Contract):
    delivery_id: UUID
    actor: Literal["AGENT", "HUMAN"]
    text: TextContent


class WebsiteDeliveryDescriptor(Contract):
    outbound_intent_id: UUID
    outbound_ordinal: PositiveCounter
    source_kind: Literal["AGENT_TURN", "OUTBOUND_MESSAGE"]
    source_id: UUID
    response_attempt_id: UUID
    delivery_id: UUID


class WebsiteDeliveryRecovery(Contract):
    delivery_id: UUID
    status: Literal["PENDING", "WRITE_IN_PROGRESS", "TRANSPORT_ACCEPTED", "FAILED",
                    "UNKNOWN", "SUPERSEDED"]
    writer_state: Literal["NONE", "LIVE", "ENDED", "RETIRED"]


class WebsiteDeliveryWorkResult(Contract):
    state: Literal["EMPTY", "SEND_CANDIDATE", "RECOVER_ONLY", "FRAME_TOO_LARGE"]
    item: WebsiteDeliveryDescriptor | None = None
    frame: WebsitePublicFrame | None = None
    recovery: WebsiteDeliveryRecovery | None = None

    @model_validator(mode="after")
    def closed_shape(self) -> "WebsiteDeliveryWorkResult":
        if self.state == "EMPTY":
            valid = self.item is None and self.frame is None and self.recovery is None
        elif self.state == "SEND_CANDIDATE":
            valid = self.item is not None and self.frame is not None and self.recovery is None
        else:
            valid = self.item is not None and self.frame is None and self.recovery is not None
        if not valid:
            raise ValueError("invalid delivery work shape")
        return self


class WebsiteDeliveryBegin(WebsiteSessionAccess, WebsiteDeliveryDescriptor):
    supplier_instance_id: UUID
    process_incarnation_id: UUID
    recover_only: StrictBool


class WebsiteDeliveryBegun(Contract):
    state: Literal["ACQUIRED", "RECOVER_ONLY", "NO_SEND", "FRAME_TOO_LARGE"]
    item: WebsiteDeliveryDescriptor
    send: StrictBool
    replayed: StrictBool
    recovery: WebsiteDeliveryRecovery
    frame: WebsitePublicFrame | None = None

    @model_validator(mode="after")
    def acquisition_shape(self) -> "WebsiteDeliveryBegun":
        if self.send != (self.state == "ACQUIRED") or self.send != (self.frame is not None):
            raise ValueError("invalid acquisition shape")
        return self


class WebsiteWriterEnd(Contract):
    kind: Literal["TASK_JOINED"]
    supplier_instance_id: UUID
    process_incarnation_id: UUID


class WebsiteDeliveryComplete(WebsiteDeliveryBegin):
    outcome: Literal["WRITE_COMPLETED", "WRITE_FAILED", "UNKNOWN"]
    error_code: Literal["ORIGINAL_WRITE_REJECTED", "TRANSPORT_ERROR", "TIMEOUT"] | None = None
    writer_end: WebsiteWriterEnd | None = None

    @model_validator(mode="after")
    def original_evidence(self) -> "WebsiteDeliveryComplete":
        if self.outcome == "WRITE_COMPLETED":
            valid = self.error_code is None
        elif self.outcome == "WRITE_FAILED":
            valid = self.error_code == "ORIGINAL_WRITE_REJECTED"
        else:
            valid = self.error_code in {"TRANSPORT_ERROR", "TIMEOUT"}
        if not valid or (self.writer_end is not None and (
            self.writer_end.supplier_instance_id != self.supplier_instance_id
            or self.writer_end.process_incarnation_id != self.process_incarnation_id)):
            raise ValueError("invalid original writer evidence")
        return self


class WebsiteDeliveryCompleted(Contract):
    recovery: WebsiteDeliveryRecovery
    replayed: StrictBool


class WebsiteHistoryRequest(WebsiteSessionAccess):
    cursor: UUID | None = None


class WebsiteHistoryMessage(Contract):
    id: UUID
    actor: Literal["CUSTOMER", "AGENT", "HUMAN"]
    text: TextContent
    client_message_id: UUID | None = None


class WebsiteHistoryPage(Contract):
    state: Literal["PAGE", "FRAME_TOO_LARGE"] = "PAGE"
    items: list[WebsiteHistoryMessage] = Field(max_length=50)
    next_cursor: UUID | None = None
    oversized_message_id: UUID | None = None

    @model_validator(mode="after")
    def closed_shape(self) -> "WebsiteHistoryPage":
        if ((self.state == "FRAME_TOO_LARGE") != (self.oversized_message_id is not None)
                or (self.state == "FRAME_TOO_LARGE" and self.items)):
            raise ValueError("invalid history page shape")
        return self


class WebsiteAdmissionReceipt(Contract):
    route: Literal["MANAGED"]
    admission_id: UUID
    replayed: StrictBool
