from __future__ import annotations

import zipfile
from decimal import Decimal

import pytest
from openpyxl import Workbook
from PIL import Image

from consumable_assistant.attachments import (
    arithmetic_value,
    image_payload,
    tables,
    uploaded_frame,
)


def test_all_sheets_keep_original_rows_and_merged_text(tmp_path):
    path = tmp_path / "清单.xlsx"
    book = Workbook()
    first = book.active
    first.title = "第一组"
    first.append(["实验准备清单"])
    first.append(["名称", "规格型号", "所需数量", "数量单位"])
    first.append(["烧杯", "100mL", 2, "个"])
    first.append([None, "100mL", 3, "个"])
    first.merge_cells("A3:A4")
    second = book.create_sheet("第二组")
    second.append(["物品名称", "规格", "用量", "单位"])
    second.append(["移液管", "5mL", 4, "支"])
    book.save(path)
    frame = uploaded_frame(path)
    assert frame["耗材名称"].tolist() == ["烧杯", "烧杯", "移液管"]
    assert frame["数量"].tolist() == [2, 3, 4]
    assert frame.attrs["sources"] == [
        {"source_sheet": "第一组", "source_row": 3},
        {"source_sheet": "第一组", "source_row": 4},
        {"source_sheet": "第二组", "source_row": 2},
    ]


def test_formula_cannot_become_inventory_quantity(tmp_path):
    path = tmp_path / "formula.xlsx"
    book = Workbook()
    book.active.append(["名称", "数量"])
    book.active.append(["烧杯", "=2+3"])
    book.save(path)
    parsed = tables(path)[0]
    assert parsed["formulas"] == ["B2"]
    assert parsed["records"][0]["values"]["数量"] is None
    with pytest.raises(ValueError, match="包含公式"):
        uploaded_frame(path)


def test_bounded_formula_evaluation_keeps_decimal_precision_and_rejects_unsupported_forms(tmp_path):
    path = tmp_path / "精确公式.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["名称", "数量", "计算", "除零", "函数", "文本引用"])
    sheet.append([
        "烧杯",
        "=0.123456789123456789+0.000000000000000001",
        "=B2*3",
        "=1/0",
        "=SUM(B2:B2)",
        "=A2+1",
    ])
    book.save(path)

    parsed = tables(path, arithmetic=True)[0]
    values = parsed["records"][0]["values"]
    assert values["数量"] == Decimal("0.123456789123456790")
    assert values["计算"] == Decimal("0.370370367370370370")
    assert values["除零"] is None
    assert parsed["formulas"] == ["D2", "E2", "F2"]
    with pytest.raises(ValueError, match="包含公式"):
        uploaded_frame(path, arithmetic=True)


def test_bounded_formula_evaluation_rejects_cycles():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "=B1+1"
    sheet["B1"] = "=A1+1"
    with pytest.raises(ValueError, match="循环"):
        arithmetic_value(sheet, "A1")


def test_duplicate_headers_and_unknown_nonempty_sheets_require_review(tmp_path):
    path = tmp_path / "duplicate.csv"
    path.write_text("名称,数量,数量\n烧杯,2,3\n")
    with pytest.raises(ValueError, match="重复表头"):
        tables(path)
    path.write_text("实验材料\n烧杯二个\n")
    with pytest.raises(ValueError, match="未识别名称列"):
        uploaded_frame(path)


def test_csv_gb18030_and_tab_separator(tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_bytes("名称\t数量\t单位\n烧杯\t3\t个\n".encode("gb18030"))
    frame = uploaded_frame(path)
    assert frame.iloc[0].to_dict() == {"耗材名称": "烧杯", "数量": "3", "单位": "个"}


def test_oversized_archive_rejected_before_decoding(tmp_path):
    path = tmp_path / "large.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.xml", bytes(50 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="50 MB"):
        tables(path)


def test_image_model_copy_does_not_change_original(tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGBA", (2400, 1200), "white").save(path)
    original = path.read_bytes()
    result = image_payload(path, "photo.png")
    assert result["name"] == "photo.png"
    assert result["data"]
    assert path.read_bytes() == original
