"""Bounded spreadsheet and image decoding. Uploaded code and formulas never run."""
from __future__ import annotations

import base64
import csv
import io
import zipfile
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from PIL import Image, ImageOps

MAX_CELLS = 200_000
ALIASES = {
    "名称": "耗材名称", "物品": "耗材名称", "耗材": "耗材名称", "产品名": "耗材名称",
    "物品名称": "耗材名称", "所需数量": "数量", "需求数量": "数量", "用量": "数量", "耗材数量": "数量",
    "数量单位": "单位", "规格型号": "规格", "型号": "规格", "耗材位置": "位置", "存放地": "位置",
}


def tables(path: Path) -> list[dict]:
    """Read all sheets and source coordinates with no office subprocess."""
    suffix = path.suffix.lower()
    sheets = []
    if suffix == ".csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = []
        cells = 0
        for row in csv.reader(io.StringIO(text), dialect):
            cells += len(row)
            if cells > MAX_CELLS:
                raise ValueError("表格超过二十万个单元格")
            rows.append(row)
        sheets.append({"sheet": "CSV", "rows": rows, "formulas": []})
    elif suffix == ".xlsx":
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("表格解压后超过 50 MB")
        workbook = load_workbook(path, data_only=False, keep_links=False)
        try:
            cells = 0
            for sheet in workbook:
                cells += sheet.max_row * sheet.max_column
                if cells > MAX_CELLS:
                    raise ValueError("表格超过二十万个单元格")
                rows = [[cell.value for cell in row] for row in sheet]
                formulas = []
                for row in sheet:
                    for cell in row:
                        if cell.data_type == "f":
                            formulas.append(cell.coordinate)
                            rows[cell.row - 1][cell.column - 1] = None
                # Text merged headings repeat; numeric merged quantities require review.
                for merged in sheet.merged_cells.ranges:
                    value = rows[merged.min_row - 1][merged.min_col - 1]
                    if isinstance(value, str):
                        for row in range(merged.min_row, merged.max_row + 1):
                            for col in range(merged.min_col, merged.max_col + 1):
                                rows[row - 1][col - 1] = value
                sheets.append({"sheet": sheet.title, "rows": rows, "formulas": formulas})
        finally:
            workbook.close()
    elif suffix == ".xls":
        import xlrd

        workbook = xlrd.open_workbook(path, on_demand=True)
        try:
            cells = 0
            for sheet in workbook.sheets():
                cells += sheet.nrows * sheet.ncols
                if cells > MAX_CELLS:
                    raise ValueError("表格超过二十万个单元格")
                sheets.append({"sheet": sheet.name, "rows": [sheet.row_values(i) for i in range(sheet.nrows)], "formulas": []})
        finally:
            workbook.release_resources()
    else:
        raise ValueError("请使用 XLS、XLSX 或 CSV 表格")
    output = []
    for sheet in sheets:
        rows = sheet["rows"]
        header = next((i for i, row in enumerate(rows[:30]) if any(str(v).strip() in {"耗材名称", "名称", "产品名", "物品", "耗材", "物品名称"} for v in row)), None)
        if header is None:
            output.append({**sheet, "header_row": None, "columns": [], "records": []})
            continue
        columns = [str(v).strip() if v is not None else f"未命名列{i + 1}" for i, v in enumerate(rows[header])]
        if len(columns) != len(set(columns)):
            raise ValueError(f"工作表 {sheet['sheet']} 存在重复表头，请核对列名")
        records = [{"source_sheet": sheet["sheet"], "source_row": i + 1, "values": dict(zip(columns, row, strict=False))} for i, row in enumerate(rows) if i > header and any(v is not None and v != "" for v in row)]
        output.append({"sheet": sheet["sheet"], "header_row": header + 1, "columns": columns, "records": records, "formulas": sheet["formulas"], "rows": rows if header is None else []})
    return output


def uploaded_frame(path: Path) -> pd.DataFrame:
    records, sources = [], []
    for sheet in tables(path):
        if sheet["formulas"]:
            raise ValueError(f"工作表 {sheet['sheet']} 包含公式（{'、'.join(sheet['formulas'][:10])}），请核对并改为明确数值后上传")
        if sheet["header_row"] is None:
            if any(any(v is not None and v != "" for v in row) for row in sheet["rows"]):
                raise ValueError(f"工作表 {sheet['sheet']} 未识别名称列，请核对表头")
            continue
        for row in sheet["records"]:
            values = {}
            for key, value in row["values"].items():
                target = ALIASES.get(key, key)
                if target in values and values[target] not in (None, "") and value not in (None, ""):
                    raise ValueError(f"工作表 {sheet['sheet']} 的第 {row['source_row']} 行存在含义重复的列")
                if value not in (None, "") or target not in values:
                    values[target] = value
            records.append(values)
            sources.append({"source_sheet": row["source_sheet"], "source_row": row["source_row"]})
    frame = pd.DataFrame(records)
    frame.attrs["sources"] = sources
    return frame


def image_payload(path: Path, name: str) -> dict:
    """Normalize a selected image for the school model; keep its original file."""
    if path.suffix.lower() in {".heic", ".heif"}:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    with Image.open(path) as source:
        if source.width * source.height > 20_000_000:
            raise ValueError("照片尺寸超过两千万像素")
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((2048, 2048))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
    return {"name": name, "data": base64.b64encode(output.getvalue()).decode("ascii")}
