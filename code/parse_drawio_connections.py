#!/usr/bin/env python3
"""Extract architecture connections from an uncompressed draw.io XML file."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from parse_native_vsdx_connections import (
    FIELDS, PAD_RE, _normalize_pad_label, default_run_output_dir,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


PORT_RE = re.compile(r"^[msp]\d+$", re.I)
MODULE_RE = re.compile(r"(?:[_ ]top$|matrix)", re.I)


@dataclass(frozen=True)
class DrawioParseResult:
    source: str
    rows: tuple[dict[str, object], ...]
    connector_count: int
    complete_connections: int
    missing_source: int
    missing_target: int


def _label(cell: ET.Element | None) -> str:
    if cell is None:
        return ""
    raw = cell.get("value", "") or ""
    port = re.match(r"^([msp]\d+)", raw, re.I)
    if port:
        return port.group(1)
    decoded = urllib.parse.unquote(raw)
    text = re.sub(r"<[^>]+>", "", decoded)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _area(cell: ET.Element) -> float:
    geometry = cell.find("mxGeometry")
    if geometry is None:
        return 0.0
    try:
        return float(geometry.get("width", "0")) * float(geometry.get("height", "0"))
    except ValueError:
        return 0.0


def _endpoint(
    cell: ET.Element | None,
    children: dict[str, list[ET.Element]],
) -> tuple[str, str]:
    if cell is None:
        return "/", "/"
    label = _label(cell)
    pad = PAD_RE.fullmatch(label)
    if pad:
        return _normalize_pad_label(pad.group(1)), _normalize_pad_label(pad.group(2))
    if MODULE_RE.search(label):
        return label, "/"

    parent = cell.get("parent", "")
    siblings = [
        candidate
        for candidate in children.get(parent, [])
        if candidate is not cell
        and candidate.get("vertex") == "1"
        and _label(candidate)
        and _area(candidate) > _area(cell)
    ]
    if siblings:
        owner = max(siblings, key=_area)
        return _label(owner), label or "/"
    return label or "/", "/"


def _style_map(style: str) -> dict[str, str]:
    values = {}
    for part in style.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values


def _direction(style: str) -> str:
    values = _style_map(style)
    begin = values.get("startArrow", "none") not in {"", "none", "0"}
    end = values.get("endArrow", "none") not in {"", "none", "0"}
    if begin and end:
        return "双向"
    if end:
        return "源→目标"
    if begin:
        return "目标→源"
    return "/"


def parse_drawio(path: Path) -> DrawioParseResult:
    root = ET.parse(path).getroot()
    cells = root.findall(".//mxCell")
    by_id = {cell.get("id", ""): cell for cell in cells}
    children: dict[str, list[ET.Element]] = {}
    for cell in cells:
        children.setdefault(cell.get("parent", ""), []).append(cell)

    rows = []
    for edge in (cell for cell in cells if cell.get("edge") == "1"):
        source_module, source_port = _endpoint(by_id.get(edge.get("source", "")), children)
        target_module, target_port = _endpoint(by_id.get(edge.get("target", "")), children)
        style = edge.get("style", "") or ""
        rows.append({
            "连接器ID": f"drawio:{edge.get('id', '')}",
            "页面": 1,
            "源模块口名": source_port,
            "源模块": source_module,
            "目标端口名": target_port,
            "目标模块": target_module,
            "总线/信协议": "/",
            "数据宽度": "/",
            "方向": _direction(style),
            "连线颜色/线型": "黑色虚线" if _style_map(style).get("dashed") == "1" else "黑色实线",
        })

    for index, row in enumerate(rows, 1):
        row["序号"] = index
    missing_source = sum(row["源模块"] == "/" for row in rows)
    missing_target = sum(row["目标模块"] == "/" for row in rows)
    return DrawioParseResult(
        source=str(path),
        rows=tuple(rows),
        connector_count=len(rows),
        complete_connections=sum(row["源模块"] != "/" and row["目标模块"] != "/" for row in rows),
        missing_source=missing_source,
        missing_target=missing_target,
    )


def write_outputs(result: DrawioParseResult, output_dir: Path, stem: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{stem}_连线表.csv"
    report_path = output_dir / f"{stem}_解析校验.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(result.rows)
    report_path.write_text(json.dumps({
        "source": result.source,
        "connector_count": result.connector_count,
        "complete_connections": result.complete_connections,
        "missing_source": result.missing_source,
        "missing_target": result.missing_target,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return csv_path, report_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drawio", type=Path)
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=None,
        help="Output directory (default: a new timestamped directory under <project>/output)",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = default_run_output_dir(args.drawio)
    result = parse_drawio(args.drawio)
    csv_path, report_path = write_outputs(result, args.output_dir, args.drawio.stem)
    print(json.dumps({
        "connectors": result.connector_count,
        "complete": result.complete_connections,
        "missing_source": result.missing_source,
        "missing_target": result.missing_target,
        "csv": str(csv_path),
        "report": str(report_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
