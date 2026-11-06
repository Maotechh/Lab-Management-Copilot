"""Explicit model-facing input contracts for the existing business functions."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

ID = Annotated[int, Field(gt=0)]
Text = Annotated[str, Field(min_length=1, max_length=500)]
Quantity = str | int | float
State = Literal["ready", "cleaning", "repair", "quarantine", "damaged", "lost"]
StockState = Literal["ready", "cleaning", "repair", "quarantine"]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stock(Payload):
    item_id: ID
    action: Literal["inbound", "consume", "borrow", "return", "adjust", "count", "threshold", "state"]
    quantity: Quantity
    unit: str | None = None
    note: str | None = None
    borrower: str | None = None
    purpose: str | None = None
    destination: str | None = None
    due_at: str | None = None
    loan_id: ID | None = None
    return_state: State | None = None
    from_state: State | None = None
    to_state: State | None = None

    def model_post_init(self, __context) -> None:
        required = {
            "borrow": ("borrower", "purpose", "destination", "due_at"),
            "return": ("loan_id", "return_state"),
            "state": ("from_state", "to_state"),
            "adjust": ("note",),
            "count": ("note",),
        }.get(self.action, ())
        if any(getattr(self, field) in (None, "") for field in required):
            raise ValueError("此库存操作缺少必要资料：" + "、".join(required))


class Line(Payload):
    item_id: ID
    action: Literal["consume", "borrow"]
    unit: str | None = None
    quantity: Quantity | None = None
    per_person: Quantity = "0"
    per_group: Quantity = "0"
    fixed: Quantity = "0"
    spare: Quantity = "0"


class Schedule(Payload):
    name: Text
    course: Text
    venue: Text
    starts_at: Text
    ends_at: Text
    people: Annotated[int, Field(ge=0)] = 0
    group_size: Annotated[int, Field(gt=0)] = 1
    lines: list[Line] = Field(min_length=1, max_length=200)


class Template(Payload):
    name: Text
    course: Text
    lines: list[Line] = Field(min_length=1, max_length=200)
    template_id: ID | None = None


class Purchase(Payload):
    action: Literal["order", "receive", "cancel"] = "order"
    item_id: ID | None = None
    order_id: ID | None = None
    quantity: Quantity | None = None
    unit: str | None = None
    owner: str | None = None
    expected_at: str | None = None
    note: str | None = None


class Item(Payload):
    item_name: Text
    unit: Text
    category: str | None = None
    lab: Text
    location_code: Text
    spec: str | None = None
    brand: str | None = None
    remark: str | None = None
    threshold: Quantity = "0"


class Metadata(Payload):
    item_id: ID
    item_name: str | None = None
    category: str | None = None
    lab: str | None = None
    location_code: str | None = None
    spec: str | None = None
    brand: str | None = None
    remark: str | None = None
    usage_type: Literal["consumable", "reusable", "unclassified"] | None = None
    conversions: dict[str, Quantity] | None = None


def contract(name, **fields):
    return create_model(name, __base__=Payload, **fields)


CONTRACTS = {
    "stock": Stock,
    "bulk": contract("Bulk", operations=(list[Stock], Field(min_length=1, max_length=200))),
    "purchase": Purchase,
    "schedule": Schedule,
    "template": Template,
    "create_item": Item,
    "metadata": Metadata,
    "transfer": contract("Transfer", item_id=(ID, ...), quantity=(Quantity, ...), unit=(str | None, None),
                         target_lab=(Text, ...), target_location=(Text, ...), state=(StockState, "ready"), note=(str | None, None)),
    "execute_schedule": contract("ExecuteSchedule", schedule_id=(ID, ...), borrower=(str | None, None)),
    "cancel_schedule": contract("CancelSchedule", schedule_id=(ID, ...), note=(Text, ...)),
    "archive_location": contract("ArchiveLocation", lab=(Text, ...), location_code=(Text, ...), note=(Text, ...)),
    "location_details": contract("LocationDetails", lab=(Text, ...), location_code=(Text, ...),
                                description=(str, ""), photo_attachment_id=(str | None, None)),
    "alert": contract("Alert", item_id=(ID, ...), event_id=(ID, ...),
                      action=(Literal["acknowledge", "snooze", "remind"], ...), until=(str | None, None)),
    "import": contract("Import", preview_id=(Text, ...), allow_duplicate=(bool, False), note=(str | None, None)),
    "prepare": contract("Prepare", preview_id=(Text, ...), selected_rows=(list[ID], ...), actions=(dict[str, str], {})),
    "prepare_remap": contract("PrepareRemap", preview_id=(Text, ...), row_number=(ID, ...), item_id=(ID, ...)),
    "undo": contract("Undo", event_id=(ID, ...)),
}


def validate_payload(kind: str, data: dict) -> dict:
    if kind not in CONTRACTS:
        raise ValueError("业务操作类型无效")
    # Versions are supplied by the server after the referenced records have
    # been checked, never accepted from the model.
    data = _without_server_versions(data)
    result = CONTRACTS[kind].model_validate(data).model_dump(exclude_none=True, exclude_unset=True)
    if kind == "bulk":
        for row in result["operations"]:
            validate_payload("stock", row)
    if kind == "purchase":
        action = result.get("action", "order")
        required = {"order": ("item_id", "quantity", "owner", "expected_at"),
                    "receive": ("order_id", "quantity"), "cancel": ("order_id", "note")}[action]
        if any(result.get(key) in (None, "") for key in required):
            raise ValueError("采购操作缺少必要信息：" + "、".join(required))
    if kind == "alert" and result.get("action") == "snooze" and not result.get("until"):
        raise ValueError("暂缓提醒需要填写截止时间")
    if kind == "prepare" and not result.get("selected_rows"):
        raise ValueError("请至少选择一项准备清单条目")
    return result


def _without_server_versions(value):
    if isinstance(value, dict):
        return {
            key: _without_server_versions(item)
            for key, item in value.items()
            if key not in {"version", "order_version", "item_versions"}
        }
    if isinstance(value, list):
        return [_without_server_versions(item) for item in value]
    return value


def prompt_contracts() -> str:
    schemas = {kind: model.model_json_schema() for kind, model in CONTRACTS.items()}
    return "\n业务 payload 的 JSON Schema（未提供的信息必须询问，禁止猜测）：\n" + json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
