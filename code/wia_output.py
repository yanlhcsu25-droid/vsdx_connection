"""Write WIA results by filling the authoritative workbook template."""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
M = f"{{{MAIN_NS}}}"
R = f"{{{REL_NS}}}"
P = f"{{{PKG_NS}}}"
C = f"{{{CONTENT_NS}}}"
TEMPLATE = Path(__file__).resolve().parents[1] / "config" / "wia_output_template.xlsx"

HIERARCHY_HEADERS = ("module_name", "instance_name", "parent module name")
PORT_HEADERS = ("port_name", "direction", "width")
CONNECT_HEADERS = (
    "No.",
    "initiator_module_name(发起端实例名/模块名)",
    "initiator_port",
    "target_module_name",
    "target_port",
    "bus_protocol",
    "direction",
)
CONNECTION_ISSUE_HEADERS = (
    "页面",
    "连接器ID",
    "问题类型",
    "Begin端模块",
    "Begin端Shape ID",
    "BeginX",
    "BeginY",
    "End端模块",
    "End端Shape ID",
    "EndX",
    "EndY",
)


def _column_name(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _filled_sheet(template_xml, headers, rows):
    root = ET.fromstring(template_xml)
    sheet_data = root.find(M + "sheetData")
    template_rows = sheet_data.findall(M + "row")
    header = template_rows[0]
    header_cells = header.findall(M + "c")
    for index, value in enumerate(headers, 1):
        if index <= len(header_cells):
            cell = header_cells[index - 1]
        else:
            cell = ET.SubElement(header, M + "c", {
                "r": f"{_column_name(index)}1"
            })
        for child in list(cell):
            cell.remove(child)
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, M + "is")
        ET.SubElement(inline, M + "t").text = value
    data_styles = {}
    if len(template_rows) > 1:
        for cell in template_rows[1].findall(M + "c"):
            column = re.match(r"[A-Z]+", cell.get("r", "")).group()
            if cell.get("s") is not None:
                data_styles[column] = cell.get("s")
    for row in template_rows[1:]:
        sheet_data.remove(row)

    for row_number, values in enumerate(rows, 2):
        row = ET.SubElement(sheet_data, M + "row", {"r": str(row_number)})
        for column_number, field in enumerate(headers, 1):
            column = _column_name(column_number)
            attrs = {"r": f"{column}{row_number}"}
            if column in data_styles:
                attrs["s"] = data_styles[column]
            value = values.get(field, "")
            if value is None:
                value = ""
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cell = ET.SubElement(row, M + "c", attrs)
                ET.SubElement(cell, M + "v").text = str(value)
            else:
                attrs["t"] = "inlineStr"
                cell = ET.SubElement(row, M + "c", attrs)
                inline = ET.SubElement(cell, M + "is")
                text = ET.SubElement(inline, M + "t", {
                    "{http://www.w3.org/XML/1998/namespace}space": "preserve"
                })
                text.text = str(value)

    dimension = root.find(M + "dimension")
    last_row = max(1, len(rows) + 1)
    reference = f"A1:{_column_name(len(headers))}{last_row}"
    if dimension is None:
        dimension = ET.Element(M + "dimension", {"ref": reference})
        root.insert(0, dimension)
    else:
        dimension.set("ref", reference)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_wia_workbook(
    path, hierarchy_rows=(), port_rows=(), connect_rows=(),
    connection_issue_rows=(),
):
    """Fill confirmed rows and structural connection issues into the WIA workbook."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheets = {
        "hierarchy": (HIERARCHY_HEADERS, list(hierarchy_rows)),
        "port": (PORT_HEADERS, list(port_rows)),
        "connect": (CONNECT_HEADERS, list(connect_rows)),
        "connection_issues": (CONNECTION_ISSUE_HEADERS, list(connection_issue_rows)),
    }
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"WIA workbook template is missing: {TEMPLATE}")

    ET.register_namespace("", MAIN_NS)
    ET.register_namespace("r", REL_NS)
    with zipfile.ZipFile(TEMPLATE) as source:
        workbook = ET.fromstring(source.read("xl/workbook.xml"))
        relationships = ET.fromstring(source.read("xl/_rels/workbook.xml.rels"))
        content_types = ET.fromstring(source.read("[Content_Types].xml"))
        targets = {item.get("Id"): item.get("Target") for item in relationships}
        replacements = {}
        existing_sheet_names = set()
        for sheet in workbook.find(M + "sheets"):
            name = sheet.get("name")
            existing_sheet_names.add(name)
            if name not in sheets:
                continue
            target = targets[sheet.get(R + "id")].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            headers, rows = sheets[name]
            replacements[target] = _filled_sheet(source.read(target), headers, rows)

        extra_files = {}
        if "connection_issues" not in existing_sheet_names:
            sheet_ids = [int(sheet.get("sheetId")) for sheet in workbook.find(M + "sheets")]
            relationship_ids = [
                int(match.group(1))
                for relationship in relationships
                if (match := re.fullmatch(r"rId(\d+)", relationship.get("Id", "")))
            ]
            new_sheet_id = max(sheet_ids, default=0) + 1
            new_relationship_id = f"rId{max(relationship_ids, default=0) + 1}"
            new_target = f"worksheets/sheet{new_sheet_id}.xml"
            new_path = "xl/" + new_target
            ET.SubElement(workbook.find(M + "sheets"), M + "sheet", {
                "name": "connection_issues",
                "sheetId": str(new_sheet_id),
                R + "id": new_relationship_id,
            })
            ET.SubElement(relationships, P + "Relationship", {
                "Id": new_relationship_id,
                "Type": (
                    "http://schemas.openxmlformats.org/officeDocument/2006/"
                    "relationships/worksheet"
                ),
                "Target": new_target,
            })
            ET.SubElement(content_types, C + "Override", {
                "PartName": "/" + new_path,
                "ContentType": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.worksheet+xml"
                ),
            })
            headers, rows = sheets["connection_issues"]
            extra_files[new_path] = _filled_sheet(
                source.read("xl/worksheets/sheet3.xml"), headers, rows
            )
            replacements["xl/workbook.xml"] = ET.tostring(
                workbook, encoding="utf-8", xml_declaration=True
            )
            replacements["xl/_rels/workbook.xml.rels"] = ET.tostring(
                relationships, encoding="utf-8", xml_declaration=True
            )
            replacements["[Content_Types].xml"] = ET.tostring(
                content_types, encoding="utf-8", xml_declaration=True
            )

        with zipfile.ZipFile(path, "w") as output:
            for item in source.infolist():
                output.writestr(item, replacements.get(item.filename, source.read(item.filename)))
            for name, data in extra_files.items():
                output.writestr(name, data)
    return path
