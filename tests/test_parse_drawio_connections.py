import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "code"))

from parse_drawio_connections import PROJECT_ROOT, build_arg_parser, default_run_output_dir, parse_drawio


class DrawioCommandLinePathTest(unittest.TestCase):
    def test_output_is_created_from_timestamped_input_stem(self):
        args = build_arg_parser().parse_args(["sample.drawio"])

        self.assertIsNone(args.output_dir)
        self.assertEqual(
            PROJECT_ROOT / "output" / "sample_20260918_143025_123456",
            default_run_output_dir(Path("sample.drawio"), timestamp="20260918_143025_123456"),
        )


class DrawioConnectionParserTest(unittest.TestCase):
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
