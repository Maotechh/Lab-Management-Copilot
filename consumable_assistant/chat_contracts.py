"""Explicit model-facing input contracts for the existing business functions."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

ID = Annotated[int, Field(gt=0)]
Text = Annotated[str, Field(min_length=1, max_length=500)]
Quantity = str | int | float
State = Literal["ready", "cleaning", "repair", "quarantine", "damaged", "lost"]
StockState = Literal["ready", "cleaning", "repair", "quarantine"]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scoped(Payload):
    category: Text | None = None
    lab: Text | None = None
    location_code: Text | None = None


class Stock(Scoped):
    source_quote: Text | None = None
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


class Line(Scoped):
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
    action: Literal["draft", "place", "order", "receive", "cancel"] = "order"
    item_id: ID | None = None
    order_id: ID | None = None
    quantity: Quantity | None = None
    unit: str | None = None
    owner: str | None = None
    expected_at: str | None = None
    note: str | None = None


class PreparationLine(Scoped):
    item_ids: list[ID] = Field(default_factory=list, max_length=100)
    item_name: Text | None = None
    spec: Text | None = None
    action: Literal["consume", "borrow"]
    unit: str | None = None
    quantity: Quantity | None = None
    per_person: Quantity = "0"
    per_group: Quantity = "0"
    fixed: Quantity = "0"
    spare: Quantity = "0"
    source: str | None = None


class Preparation(Schedule):
    lines: list[PreparationLine] = Field(min_length=1, max_length=200)


class PlanStep(Payload):
    kind: str
    payload: dict


class MixedPlan(Payload):
    operations: list[PlanStep] = Field(min_length=1, max_length=200)


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


def contract(model_name, **fields):
    return create_model(model_name, __base__=Payload, **fields)


CONTRACTS = {
    "stock": Stock,
    "bulk": contract("Bulk", operations=(list[Stock], Field(min_length=1, max_length=200))),
    "purchase": Purchase,
    "procurement": contract("Procurement", demand_id=(ID, ...),
                            action=(Literal["place", "receive", "cancel"], ...),
                            quantity=(Quantity | None, None), owner=(str | None, None),
                            expected_at=(str | None, None), note=(str | None, None),
                            lab=(str | None, None), location_code=(str | None, None),
                            category=(str | None, None)),
    "schedule": Schedule,
    "preparation": Preparation,
    "preparation_update": contract("PreparationUpdate", preparation_id=(ID, ...),
                                   action=(Literal["replenish", "cancel", "revise"], ...), note=(Text, ...),
                                   name=(Text | None, None), course=(Text | None, None), venue=(Text | None, None),
                                   starts_at=(Text | None, None), ends_at=(Text | None, None),
                                   people=(Annotated[int, Field(ge=0)] | None, None),
                                   group_size=(Annotated[int, Field(gt=0)] | None, None),
                                   lines=(list[PreparationLine] | None, Field(default=None, min_length=1, max_length=200))),
    "mixed": MixedPlan,
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
    "restore_item": contract("RestoreItem", item_id=(ID, ...), note=(Text, ...)),
    "prepare": contract("Prepare", category=(Text | None, None), lab=(Text | None, None),
                        location_code=(Text | None, None), preview_id=(Text, ...), selected_rows=(list[ID], ...), actions=(dict[str, str], {}),
                        edits=(dict[str, contract("ChecklistEdit",
                            required_quantity=(Quantity | None, None), item_id=(ID | None, None),
                            name=(Text | None, None), spec=(str | None, None), unit=(Text | None, None),
                        )], {}), borrower=(Text | None, None), purpose=(Text | None, None),
                        destination=(Text | None, None), due_at=(Text | None, None),
                        repeat_reason=(Annotated[str, Field(max_length=300)] | None, None)),
    "prepare_remap": contract("PrepareRemap", preview_id=(Text, ...), row_number=(ID, ...), item_id=(ID, ...)),
    "undo": contract("Undo", event_id=(ID, ...)),
    "undo_batch": contract("UndoBatch", batch_id=(Text, ...), item_ids=(list[ID], ...)),
    "archive_item": contract("ArchiveItem", item_id=(ID, ...), note=(Text, ...)),
    "save_reference": contract("SaveReference", preview_id=(Text, ...)),
    "remove_reference": contract("RemoveReference", file_id=(ID, ...)),
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
    if kind == "mixed":
        for step in result["operations"]:
            if step["kind"] == "mixed":
                raise ValueError("混合计划不能嵌套")
            step["payload"] = validate_payload(step["kind"], step["payload"])
    if kind == "purchase":
        action = result.get("action", "order")
        required = {"draft": ("item_id", "quantity"), "place": ("order_id", "owner", "expected_at"),
                    "order": ("item_id", "quantity", "owner", "expected_at"),
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


CONTRACT_PROMPT_SPEC = """
业务预览（business_preview）payload 参数规范（严禁传入未定义字段或 version/order_version/item_versions；缺少必填项须先追问，禁止猜测）：
- 通用枚举：State="ready"|"cleaning"|"repair"|"quarantine"|"damaged"|"lost"；StockState="ready"|"cleaning"|"repair"|"quarantine"。
- stock（单笔库存操作）：必填 item_id(正整数)、action("inbound"|"consume"|"borrow"|"return"|"adjust"|"count"|"threshold"|"state")、quantity(数字)；可选 category、lab、location_code、source_quote、unit、note。条件必填：borrow 须填 borrower、purpose、destination、due_at；return 须填 loan_id、return_state(State)；state 须填 from_state(State)、to_state(State)；adjust 或 count 须填 note。
- bulk（批量库存操作）：必填 operations=[stock对象, ...]。
- mixed（混合多步计划）：必填 operations=[{"kind": 非mixed操作类型, "payload": 对应对象}, ...]。
- purchase（已有库存耗材采购）：可选 action("draft"|"place"|"order"|"receive"|"cancel"，默认"order")、unit、note。条件必填：draft→item_id、quantity；order→item_id、quantity、owner、expected_at；place→order_id、owner、expected_at；receive→order_id、quantity；cancel→order_id、note。
- procurement（新耗材独立采购需求）：必填 demand_id、action("place"|"receive"|"cancel")；可选 quantity、owner、expected_at、note、lab、location_code、category（receive 须填实际到货 quantity、lab、location_code）。
- schedule（实验排课预留）：必填 name、course、venue、starts_at、ends_at、lines=[Line对象]；可选 people(默认0)、group_size(默认1)。Line：必填 item_id、action("consume"|"borrow")；可选 category、lab、location_code、unit、quantity、per_person、per_group、fixed、spare。
- preparation（完整实验准备）：必填 name、course、venue、starts_at、ends_at、lines=[PreparationLine对象]；可选 people(默认0)、group_size(默认1)。PreparationLine：必填 action("consume"|"borrow")；可选 item_ids(整数列表，无匹配库存的新耗材传[])、item_name、spec、unit、category、lab、location_code、quantity、per_person、per_group、fixed、spare、source。
- preparation_update（实验准备变更）：必填 preparation_id、action("replenish"|"cancel"|"revise")、note；revise 可选传 name、course、venue、starts_at、ends_at、people、group_size、lines。
- execute_schedule（执行排课取用）：必填 schedule_id；可选 borrower。
- cancel_schedule（取消排课）：必填 schedule_id、note。
- template（课程模板）：必填 name、course、lines=[Line对象]；更新已有模板时可选 template_id。
- create_item（新增耗材建档）：必填 item_name、unit、lab、location_code；可选 category、spec、brand、remark、threshold(默认"0")。
- metadata（修改耗材资料）：必填 item_id；可选 item_name、category、lab、location_code、spec、brand、remark、usage_type("consumable"|"reusable"|"unclassified")、conversions({包装单位: 基础单位数量})。
- transfer（跨位置转移）：必填 item_id、quantity、target_lab、target_location；可选 unit、state(StockState，默认"ready")、note。
- archive_item / restore_item（停用/恢复耗材）：必填 item_id、note。
- archive_location（停用位置）：必填 lab、location_code、note。
- location_details（位置说明与照片）：必填 lab、location_code；可选 description(默认"")、photo_attachment_id。
- alert（库存提醒处理）：必填 item_id、event_id、action("acknowledge"|"snooze"|"remind")；snooze 必填 until。
- import（确认表格导入）：必填 preview_id；可选 allow_duplicate(布尔，默认false)、note。
- prepare（确认课堂清单登记）：必填 preview_id、selected_rows(行号正整数列表)；可选 category、lab、location_code、actions({行号: "consume"|"borrow"})、edits({行号: {required_quantity, item_id, name, spec, unit}})、borrower、purpose、destination、due_at、repeat_reason。
- prepare_remap（重映射清单条目）：必填 preview_id、row_number、item_id。
- undo（撤销单笔库存事件）：必填 event_id。
- undo_batch（撤销导入批次）：必填 batch_id、item_ids(整数列表)。
- save_reference（保存历史位置参考）：必填 preview_id。
- remove_reference（移除历史参考文件）：必填 file_id。
""".strip()


def prompt_contracts() -> str:
    return "\n\n" + CONTRACT_PROMPT_SPEC
