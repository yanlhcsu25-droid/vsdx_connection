import csv
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "code"))

from parse_native_vsdx_connections import (
    PROJECT_ROOT,
    build_arg_parser,
    default_run_output_dir,
    parse_vsdx,
    write_outputs,
)


FIXTURE = (
    HERE.parents[1]
    / "examples"
    / "architecture1"
    / "architecture1_connected_visual_fixed_route_ch10.vsdx"
)
NATIVE_ERROR_FIXTURE = (
    HERE.parents[1] / "examples" / "architecture1" / "architecture1_native_error.vsdx"
)
NATIVE_RIGHT_FIXTURE = (
    HERE.parents[1] / "examples" / "architecture1" / "architecture1_native_right.vsdx"
)


class CommandLinePathTest(unittest.TestCase):
    def test_output_is_created_from_timestamped_input_stem(self):
        args = build_arg_parser().parse_args([str(NATIVE_RIGHT_FIXTURE)])

        self.assertIsNone(args.output_dir)
        run_dir = default_run_output_dir(NATIVE_RIGHT_FIXTURE, timestamp="20260918_143025_123456")
        self.assertEqual(
            PROJECT_ROOT / "output" / "architecture1_native_right_20260918_143025_123456",
            run_dir,
        )


@unittest.skipUnless(
    NATIVE_ERROR_FIXTURE.exists() and NATIVE_RIGHT_FIXTURE.exists(),
    "native VSDX fixtures unavailable",
)
class NativeVsdxXmlOnlyTest(unittest.TestCase):
    def test_xml_only_mode_does_not_classify_compound_pad_labels(self):
        rows = {
            row["连接器ID"]: row for row in parse_vsdx(NATIVE_ERROR_FIXTURE).rows
        }

        self.assertEqual(
            ("/", "/"),
            (rows["page1:352"]["目标模块"], rows["page1:352"]["目标端口名"]),
        )

    def test_xml_only_mode_preserves_rows_but_withholds_endpoint_semantics(self):
        rows = {
            row["连接器ID"]: row for row in parse_vsdx(NATIVE_RIGHT_FIXTURE).rows
        }

        self.assertEqual(
            ("/", "/", "/"),
            (
                rows["page1:30710253"]["源模块"],
                rows["page1:30710253"]["目标模块"],
                rows["page1:30710253"]["目标端口名"],
            ),
        )


@unittest.skipUnless(FIXTURE.exists(), "connected VSDX fixture unavailable")
class NativeVsdxConnectionParserTest(unittest.TestCase):
    def test_extracts_each_native_connector_exactly_once(self):
        result = parse_vsdx(FIXTURE)

        self.assertEqual(122, len(result.rows))
        self.assertEqual(122, len({row["连接器ID"] for row in result.rows}))
        self.assertEqual(118, result.complete_connections)
        self.assertEqual(3, result.missing_source)
        self.assertEqual(2, result.missing_target)

    def test_resolves_known_module_and_port_relationships(self):
        result = parse_vsdx(FIXTURE)
        relationships = {
            (
                row["源模块"],
                row["源模块口名"],
                row["目标模块"],
                row["目标端口名"],
            )
            for row in result.rows
        }

        self.assertIn(("core_top", "s0", "ahb_matrix_7_12_main", "m0"), relationships)
        self.assertIn(
            ("pwm_sec_top", "/", "PAD_DIG_IO", "PAD_PWM_CH10"), relationships
        )

    def test_writes_csv_markdown_and_validation_report(self):
        result = parse_vsdx(FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_outputs(result, Path(tmp), "architecture1")
            self.assertTrue(paths.csv.exists())
            self.assertTrue(paths.markdown.exists())
            self.assertTrue(paths.report.exists())
            with paths.csv.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual(122, len(rows))
        self.assertEqual("1", rows[0]["序号"])


if __name__ == "__main__":
    unittest.main()
