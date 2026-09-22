import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "code"))

from diagram_output_model import build_diagram_output


class DiagramOutputModelTest(unittest.TestCase):
    def test_maps_complete_connection_to_exact_connect_contract(self):
        output = build_diagram_output(connection_rows=[{
            "连接器ID": "page1:9", "源模块口名": "M_AXI", "源模块": "u_cpu_0",
            "目标端口名": "S_AXI", "目标模块": "u_mem_0",
            "总线/信协议": "AXI4", "方向": "源→目标",
        }])

        self.assertEqual(({
            "No.": 1,
            "initiator_module_name(发起端实例名/模块名)": "u_cpu_0",
            "initiator_port": "M_AXI",
            "target_module_name": "u_mem_0",
            "target_port": "S_AXI",
            "bus_protocol": "AXI4",
            "direction": "源→目标",
        },), output.connections)
        self.assertEqual((), output.uncertain)

    def test_blank_optional_values_stay_blank(self):
        output = build_diagram_output(connection_rows=[{
            "连接器ID": "page1:9", "源模块口名": "/", "源模块": "source",
            "目标端口名": "/", "目标模块": "target",
            "总线/信协议": "/", "方向": "/",
        }])

        row = output.connections[0]
        self.assertEqual("", row["initiator_port"])
        self.assertEqual("", row["target_port"])
        self.assertEqual("", row["bus_protocol"])
        self.assertEqual("", row["direction"])

    def test_missing_endpoint_is_withheld_and_reported_as_uncertain(self):
        output = build_diagram_output(connection_rows=[{
            "连接器ID": "page1:9", "源模块口名": "/", "源模块": "/",
            "目标端口名": "/", "目标模块": "target",
            "总线/信协议": "/", "方向": "/",
        }])

        self.assertEqual((), output.connections)
        self.assertEqual("connection", output.uncertain[0]["record_type"])
        self.assertEqual("missing module endpoint", output.uncertain[0]["reason"])
        self.assertEqual("page1:9", output.uncertain[0]["evidence"]["connector_id"])

    def test_ambiguous_module_name_and_instance_is_not_guessed(self):
        output = build_diagram_output(
            connection_rows=[],
            module_candidates=[{
                "shape_id": "7", "visible_name": "core_top",
                "parent_instance_name": "top", "module_name": None,
                "instance_name": None,
            }],
        )

        self.assertEqual((), output.hierarchy)
        self.assertEqual("hierarchy", output.uncertain[0]["record_type"])
        self.assertEqual("module_name and instance_name are not distinguishable", output.uncertain[0]["reason"])

    def test_visually_equivalent_module_spellings_are_flagged_not_merged(self):
        output = build_diagram_output(
            connection_rows=[],
            module_candidates=[
                {"shape_id": "7", "visible_name": "ahb_dummy top"},
                {"shape_id": "8", "visible_name": "ahb_dummy_top"},
            ],
        )

        issue = next(item for item in output.uncertain if item["record_type"] == "naming")
        self.assertEqual("visually equivalent module names differ", issue["reason"])
        self.assertEqual(["ahb_dummy top", "ahb_dummy_top"], issue["evidence"]["variants"])


if __name__ == "__main__":
    unittest.main()
