import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "code"))

from wia_output import write_wia_workbook


MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def workbook_values(path):
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target") for rel in relationships}
        result = {}
        for sheet in workbook.find(f"{MAIN}sheets"):
            target = targets[sheet.get(f"{REL}id")].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            xml = ET.fromstring(archive.read(target))
            rows = []
            for row in xml.findall(f".//{MAIN}row"):
                values = []
                for cell in row.findall(f"{MAIN}c"):
                    text = cell.find(f"{MAIN}is/{MAIN}t")
                    number = cell.find(f"{MAIN}v")
                    if text is not None:
                        values.append(text.text)
                    elif number is not None:
                        value = float(number.text)
                        values.append(int(value) if value.is_integer() else value)
                    else:
                        values.append(None)
                rows.append(values)
            result[sheet.get("name")] = rows
        return result


class WiaWorkbookTest(unittest.TestCase):
    def test_writes_connection_issues_without_changing_connect_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.xlsx"
            write_wia_workbook(
                path,
                hierarchy_rows=[{
                    "module_name": "cpu", "instance_name": "u_cpu_0",
                    "parent module name": "top",
                }],
                port_rows=[{"port_name": "addr", "direction": "input", "width": "32"}],
                connect_rows=[{
                    "No.": 1,
                    "initiator_module_name(发起端实例名/模块名)": "u_cpu_0",
                    "initiator_port": "M_AXI", "target_module_name": "u_mem_0",
                    "target_port": "S_AXI", "bus_protocol": "AXI4",
                    "direction": "Master->Slave",
                }],
                connection_issue_rows=[{
                    "页面": 1,
                    "连接器ID": "page1:9",
                    "问题类型": "begin_missing",
                    "Begin端模块": "/",
                    "Begin端Shape ID": "/",
                    "BeginX": 9.815,
                    "BeginY": 12.276,
                    "End端模块": "NPC",
                    "End端Shape ID": "1",
                    "EndX": 10.25,
                    "EndY": 12.276,
                }],
            )

            values = workbook_values(path)

        self.assertEqual(
            ["hierarchy", "port", "connect", "connection_issues"],
            list(values),
        )
        self.assertEqual(
            ["module_name", "instance_name", "parent module name"],
            values["hierarchy"][0],
        )
        self.assertEqual(["cpu", "u_cpu_0", "top"], values["hierarchy"][1])
        self.assertEqual(["port_name", "direction", "width"], values["port"][0])
        self.assertEqual(["addr", "input", "32"], values["port"][1])
        self.assertEqual(
            ["No.", "initiator_module_name(发起端实例名/模块名)", "initiator_port",
             "target_module_name", "target_port", "bus_protocol", "direction"],
            values["connect"][0],
        )
        self.assertEqual(1, values["connect"][1][0])
        self.assertEqual(
            ["页面", "连接器ID", "问题类型", "Begin端模块", "Begin端Shape ID",
             "BeginX", "BeginY", "End端模块", "End端Shape ID", "EndX", "EndY"],
            values["connection_issues"][0],
        )
        self.assertEqual(
            [1, "page1:9", "begin_missing", "/", "/", 9.815, 12.276,
             "NPC", "1", 10.25, 12.276],
            values["connection_issues"][1],
        )


if __name__ == "__main__":
    unittest.main()
