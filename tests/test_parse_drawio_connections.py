import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "code"))

from parse_drawio_connections import (
    PROJECT_ROOT,
    build_arg_parser,
    default_run_output_dir,
    parse_drawio,
    write_outputs,
)


class DrawioCommandLinePathTest(unittest.TestCase):
    def test_output_is_created_from_timestamped_input_stem(self):
        args = build_arg_parser().parse_args(["sample.drawio"])

        self.assertIsNone(args.output_dir)
        self.assertEqual(
            PROJECT_ROOT / "output" / "sample_20260918_143025_123456",
            default_run_output_dir(Path("sample.drawio"), timestamp="20260918_143025_123456"),
        )


class DrawioConnectionParserTest(unittest.TestCase):
    def test_writes_rows_with_the_shared_connection_schema(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="source" parent="1" vertex="1" value="source_top"><mxGeometry x="0" y="0" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="target" parent="1" vertex="1" value="target_top"><mxGeometry x="200" y="0" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" source="source" target="target" style="endArrow=classic;"/>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.drawio"
            source.write_text(xml, encoding="utf-8")
            result = parse_drawio(source)
            csv_path, report_path = write_outputs(result, Path(tmp) / "out", "sample")

            header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
            friendly_outputs = {
                path.name for path in (Path(tmp) / "out").iterdir()
            }

        self.assertIn("箭头颜色", header)
        self.assertNotIn("连线颜色/线型", header)
        self.assertTrue(report_path.name.endswith("_解析校验.json"))
        self.assertTrue(
            {"connections.csv", "connections.xlsx", "validation.json", "questions.json", "report.md"}
            <= friendly_outputs
        )

    def test_reads_protocol_from_edge_value_and_edge_label_child(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="a" parent="1" vertex="1" value="a_top"><mxGeometry x="0" y="0" width="50" height="50" as="geometry"/></mxCell>
        <mxCell id="b" parent="1" vertex="1" value="b_top"><mxGeometry x="100" y="0" width="50" height="50" as="geometry"/></mxCell>
        <mxCell id="direct" parent="1" edge="1" source="a" target="b" value="AXI"/>
        <mxCell id="child" parent="1" edge="1" source="a" target="b"/>
        <mxCell id="label" parent="child" vertex="1" value="ACE-Lite"><mxGeometry relative="1" as="geometry"/></mxCell>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual("AXI", result.rows[0]["总线/信协议"])
        self.assertEqual("ACE-Lite", result.rows[1]["总线/信协议"])

    def test_recovers_nearest_geometric_container_hierarchy_and_skips_text_labels(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="top" parent="1" vertex="1" value="CSS_TOP" style="rounded=0"><mxGeometry x="0" y="0" width="500" height="500" as="geometry"/></mxCell>
        <mxCell id="main" parent="1" vertex="1" value="CSS-Main" style="rounded=0"><mxGeometry x="20" y="20" width="460" height="400" as="geometry"/></mxCell>
        <mxCell id="cluster" parent="1" vertex="1" value="8x ctrhs" style="rounded=0"><mxGeometry x="100" y="100" width="200" height="200" as="geometry"/></mxCell>
        <mxCell id="core" parent="1" vertex="1" value="Core" style="rounded=0"><mxGeometry x="120" y="130" width="80" height="120" as="geometry"/></mxCell>
        <mxCell id="rts" parent="1" vertex="1" value="RTS" style="rounded=0"><mxGeometry x="130" y="140" width="40" height="20" as="geometry"/></mxCell>
        <mxCell id="caption" parent="1" vertex="1" value="CHI" style="text;html=1"><mxGeometry x="105" y="105" width="50" height="20" as="geometry"/></mxCell>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        parents = {row["instance_name"]: row["parent module name"] for row in result.hierarchy}
        self.assertEqual("", parents["CSS_TOP"])
        self.assertEqual("CSS_TOP", parents["CSS-Main"])
        self.assertEqual("CSS-Main", parents["8x ctrhs"])
        self.assertEqual("8x ctrhs", parents["Core"])
        self.assertEqual("Core", parents["RTS"])
        self.assertNotIn("CHI", parents)

    def test_resolves_group_owned_ports_pad_labels_and_arrows(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="g1" parent="1" vertex="1" style="group"><mxGeometry x="10" y="10" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="mod" parent="g1" vertex="1" value="usi0_sec_top"><mxGeometry width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="port" parent="g1" vertex="1" value="sclk"><mxGeometry x="80" y="15" width="20" height="20" as="geometry"/></mxCell>
        <mxCell id="pad" parent="1" vertex="1" value="PAD_DIG_IO(PAD_USI0_SCLK)"><mxGeometry x="200" y="10" width="80" height="30" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" source="port" target="pad" style="endArrow=classic;startArrow=none;"/>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual(1, result.connector_count)
        row = result.rows[0]
        self.assertEqual(("sclk", "usi0_sec_top"), (row["源模块口名"], row["源模块"]))
        self.assertEqual(("PAD_USI0_SCLK", "PAD_DIG_IO"), (row["目标端口名"], row["目标模块"]))
        self.assertEqual("源→目标", row["方向"])

    def test_keeps_free_floating_endpoint_unresolved(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="node" parent="1" vertex="1" value="dmac_top"><mxGeometry width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" target="node" style="endArrow=classic;"/>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual(1, result.missing_source)
        self.assertEqual(("/", "/"), (result.rows[0]["源模块口名"], result.rows[0]["源模块"]))

    def test_infers_an_unbound_endpoint_when_its_point_touches_one_shape(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="source" parent="1" vertex="1" value="source_top"><mxGeometry x="10" y="10" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="target" parent="1" vertex="1" value="target_top"><mxGeometry x="200" y="10" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" target="target" style="endArrow=classic;">
          <mxGeometry relative="1" as="geometry"><mxPoint x="110" y="35" as="sourcePoint"/></mxGeometry>
        </mxCell>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual(1, result.complete_connections)
        self.assertEqual(0, result.missing_source)
        self.assertEqual("source_top", result.rows[0]["源模块"])
        self.assertEqual("geometric", result.endpoint_evidence[0]["source_evidence"])
        self.assertEqual("native", result.endpoint_evidence[0]["target_evidence"])
        self.assertEqual("source", result.endpoint_evidence[0]["source_shape_id"])
        self.assertEqual("target", result.endpoint_evidence[0]["target_shape_id"])

    def test_does_not_infer_a_free_point_inside_a_large_container(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="container" parent="1" vertex="1" value="CSS_TOP"><mxGeometry x="100" y="100" width="600" height="600" as="geometry"/></mxCell>
        <mxCell id="target" parent="1" vertex="1" value="target_top"><mxGeometry x="800" y="100" width="100" height="50" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" target="target" style="endArrow=classic;">
          <mxGeometry relative="1" as="geometry"><mxPoint x="300" y="300" as="sourcePoint"/></mxGeometry>
        </mxCell>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual(1, result.missing_source)
        self.assertEqual("/", result.rows[0]["源模块"])
        self.assertEqual(1, result.native_missing_source)
        self.assertEqual(0, result.inferred_source)
        self.assertEqual(
            ({"connector_id": "drawio:edge", "endpoint": "source"},),
            result.unresolved_endpoints,
        )

    def test_flags_unbound_unlabeled_edge_with_duplicate_reference_points(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="a" parent="1" vertex="1" value="a_top"><mxGeometry x="0" y="0" width="50" height="50" as="geometry"/></mxCell>
        <mxCell id="b" parent="1" vertex="1" value="b_top"><mxGeometry x="300" y="300" width="50" height="50" as="geometry"/></mxCell>
        <mxCell id="bound" parent="1" edge="1" source="a" target="b" value="AXI"><mxGeometry relative="1" as="geometry"><mxPoint x="100" y="100" as="sourcePoint"/><mxPoint x="200" y="200" as="targetPoint"/></mxGeometry></mxCell>
        <mxCell id="free" parent="1" edge="1"><mxGeometry relative="1" as="geometry"><mxPoint x="100" y="100" as="sourcePoint"/><mxPoint x="200" y="200" as="targetPoint"/></mxGeometry></mxCell>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        issue = next(
            item for item in result.questions
            if item.get("connector_id") == "drawio:free"
            and item.get("reason") == "possible duplicate or legacy edge"
        )
        self.assertEqual("possible duplicate or legacy edge", issue["reason"])
        self.assertEqual(["drawio:bound"], issue["matching_connectors"])

    def test_grouped_module_is_not_mistaken_for_a_port_of_larger_sibling(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="g" parent="1" vertex="1" style="group"><mxGeometry width="300" height="100" as="geometry"/></mxCell>
        <mxCell id="large" parent="g" vertex="1" value="dmac_top"><mxGeometry width="200" height="80" as="geometry"/></mxCell>
        <mxCell id="module" parent="g" vertex="1" value="tim1_sec_top"><mxGeometry x="210" width="80" height="30" as="geometry"/></mxCell>
        <mxCell id="target" parent="1" vertex="1" value="PAD_DIG_IO(PAD_TIM1)"><mxGeometry x="400" width="80" height="30" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" source="module" target="target" style="endArrow=classic;"/>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual("tim1_sec_top", result.rows[0]["源模块"])
        self.assertEqual("/", result.rows[0]["源模块口名"])

    def test_separate_top_level_box_is_not_owned_by_a_larger_sibling(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="large" parent="1" vertex="1" value="CSS_TOP"><mxGeometry x="150" y="100" width="600" height="600" as="geometry"/></mxCell>
        <mxCell id="source" parent="1" vertex="1" value="DDR SubSystem"><mxGeometry x="10" y="300" width="80" height="80" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" source="source" target="large" style="endArrow=classic;"/>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual("DDR SubSystem", result.rows[0]["源模块"])
        self.assertEqual("/", result.rows[0]["源模块口名"])

    def test_module_name_with_internal_space_before_top_remains_a_module(self):
        xml = """<mxfile><diagram><mxGraphModel><root>
        <mxCell id="0"/><mxCell id="1" parent="0"/>
        <mxCell id="g" parent="1" vertex="1" style="group"><mxGeometry width="300" height="100" as="geometry"/></mxCell>
        <mxCell id="large" parent="g" vertex="1" value="dmac_top"><mxGeometry width="200" height="80" as="geometry"/></mxCell>
        <mxCell id="module" parent="g" vertex="1" value="ahb_dum my top"><mxGeometry x="210" width="80" height="30" as="geometry"/></mxCell>
        <mxCell id="target" parent="1" vertex="1" value="sms_top"><mxGeometry x="400" width="80" height="30" as="geometry"/></mxCell>
        <mxCell id="edge" parent="1" edge="1" source="module" target="target" style="endArrow=classic;startArrow=classic;"/>
        </root></mxGraphModel></diagram></mxfile>"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.drawio"
            path.write_text(xml, encoding="utf-8")
            result = parse_drawio(path)

        self.assertEqual("ahb_dum my top", result.rows[0]["源模块"])
        self.assertEqual("/", result.rows[0]["源模块口名"])


if __name__ == "__main__":
    unittest.main()
