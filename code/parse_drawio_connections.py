#!/usr/bin/env python3
"""Extract instance graphs and connections from native draw.io XML files."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
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
    native_missing_source: int
    native_missing_target: int
    inferred_source: int
    inferred_target: int
    unresolved_endpoints: tuple[dict[str, str], ...]
    endpoint_evidence: tuple[dict[str, str], ...]
    questions: tuple[dict[str, object], ...]
    hierarchy: tuple[dict[str, str], ...]
    graph: dict[str, object]


def _label(cell: ET.Element | None) -> str:
    if cell is None:
        return ""
    raw = cell.get("value", "") or ""
    decoded = urllib.parse.unquote(raw)
    text = re.sub(r"<[^>]+>", "", decoded)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _edge_point(edge: ET.Element, name: str) -> tuple[float, float] | None:
    geometry = edge.find("mxGeometry")
    if geometry is None:
        return None
    point = geometry.find(f"mxPoint[@as='{name}']")
    if point is None:
        return None
    try:
        return float(point.get("x", "")), float(point.get("y", ""))
    except (KeyError, ValueError):
        return None


def _style_map(style: str) -> dict[str, str]:
    values = {}
    for part in style.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values


def _contains_bounds(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    tolerance: float = 0.01,
) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ox <= ix + tolerance
        and oy <= iy + tolerance
        and ox + ow >= ix + iw - tolerance
        and oy + oh >= iy + ih - tolerance
        and ow * oh > iw * ih
    )


def parse_drawio(path: Path) -> DrawioParseResult:
    from drawio_graph import read_pages, build_graph
    pages = read_pages(path)
    graph = build_graph(pages)
    page_ids = [{cell.get("id") for cell in page.iter("mxCell")} for page in pages]
    rows = []
    endpoint_evidence = []
    unresolved_endpoints = []
    questions = []
    directions = {"bidirectional": "双向", "source_to_target": "源→目标",
                  "target_to_source": "目标→源", "unknown": "unknown"}
    for edge in graph["connections"]:
        rows.append({
            "连接器ID": edge["connector_id"], "页面": edge["page"],
            "源模块口名": edge["source_port_name"] or "/",
            "源模块": edge["source_module_name"] or "/",
            "目标端口名": edge["target_port_name"] or "/",
            "目标模块": edge["target_module_name"] or "/",
            "总线/信协议": edge["protocol"] or "/", "数据宽度": "/",
            "方向": directions[edge["direction"]], "箭头颜色": "(0, 0, 0)",
            "序号": len(rows) + 1,
        })
        endpoint_evidence.append({
            "connector_id": edge["connector_id"],
            "source_evidence": edge["source_evidence"],
            "source_shape_id": edge["source_resolved_shape_id"],
            "target_evidence": edge["target_evidence"],
            "target_shape_id": edge["target_resolved_shape_id"],
        })
        for endpoint in ("source", "target"):
            if not edge[endpoint + "_module_id"]:
                item = {"connector_id": edge["connector_id"], "endpoint": endpoint}
                unresolved_endpoints.append(item)
                questions.append({**item, "reason": "unresolved endpoint"})
        if edge["status"] == "needs_review":
            questions.append({"connector_id": edge["connector_id"], "reason": edge["reason"],
                "candidate_source_id": edge["candidate_source_id"],
                "candidate_target_id": edge["candidate_target_id"]})
    # Retain existing duplicate/legacy reference-point diagnostic.
    for page_number, page in enumerate(pages, 1):
        signatures = {}
        for edge in page.iter("mxCell"):
            if edge.get("edge") != "1":
                continue
            points = (_edge_point(edge, "sourcePoint"), _edge_point(edge, "targetPoint"))
            if all(point is not None for point in points):
                signatures.setdefault(points, []).append(edge)
        for signature, edges in signatures.items():
            if len(edges) < 2:
                continue
            def connector(edge):
                return "drawio:" + (f"page{page_number}:" if len(pages) > 1 else "") + edge.get("id", "")
            for edge in edges:
                if not edge.get("source") and not edge.get("target") and not _label(edge):
                    questions.append({"connector_id": connector(edge),
                        "reason": "possible duplicate or legacy edge",
                        "matching_connectors": [connector(other) for other in edges if other is not edge],
                        "reference_points": {"source":list(signature[0]), "target":list(signature[1])}})
    return DrawioParseResult(
        source=str(path), rows=tuple(rows), connector_count=len(rows),
        complete_connections=sum(bool(e["source_module_id"] and e["target_module_id"]) for e in graph["connections"]),
        missing_source=sum(not e["source_module_id"] for e in graph["connections"]),
        missing_target=sum(not e["target_module_id"] for e in graph["connections"]),
        native_missing_source=sum(e["original_source_id"] not in page_ids[e["page"]-1] for e in graph["connections"]),
        native_missing_target=sum(e["original_target_id"] not in page_ids[e["page"]-1] for e in graph["connections"]),
        inferred_source=sum(e["source_evidence"] == "geometric" for e in graph["connections"]),
        inferred_target=sum(e["target_evidence"] == "geometric" for e in graph["connections"]),
        unresolved_endpoints=tuple(unresolved_endpoints), endpoint_evidence=tuple(endpoint_evidence),
        questions=tuple(questions), hierarchy=tuple(graph["hierarchy"]), graph=graph,
    )


def write_outputs(result: DrawioParseResult, output_dir: Path, stem: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{stem}_连线表.csv"
    report_path = output_dir / f"{stem}_解析校验.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(result.rows)
    report = {
        "source": result.source,
        "connector_count": result.connector_count,
        "complete_connections": result.complete_connections,
        "missing_source": result.missing_source,
        "missing_target": result.missing_target,
        "native_missing_source": result.native_missing_source,
        "native_missing_target": result.native_missing_target,
        "inferred_source": result.inferred_source,
        "inferred_target": result.inferred_target,
        "unresolved_endpoints": result.unresolved_endpoints,
        "endpoint_evidence": result.endpoint_evidence,
        "questions": result.questions,
        "hierarchy_count": len(result.hierarchy),
        "port_count": len(result.graph["ports"]),
        "connection_issue_count": len(result.graph["connection_issues"]),
        "hierarchy_issues": result.graph["hierarchy_issues"],
        "structured_graph": "structured_graph.json",
        "schema_version": result.graph["schema_version"],
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    markdown_path = output_dir / f"{stem}_连线表.md"
    with markdown_path.open("w", encoding="utf-8") as stream:
        stream.write(f"# {stem} 连线表\n\n")
        stream.write(
            f"> Draw.io 连接器 {result.connector_count} 条；完整双端连接 "
            f"{result.complete_connections} 条；几何补全源端 {result.inferred_source} 条；"
            f"几何补全目标端 {result.inferred_target} 条；仍缺源端 {result.missing_source} 条；"
            f"仍缺目标端 {result.missing_target} 条。\n\n"
        )
        stream.write("| " + " | ".join(FIELDS) + " |\n")
        stream.write("|" + "|".join("---" for _ in FIELDS) + "|\n")
        for row in result.rows:
            stream.write(
                "| "
                + " | ".join(str(row[field]).replace("|", "\\|") for field in FIELDS)
                + " |\n"
            )

    questions_path = output_dir / f"{stem}_疑问项.json"
    questions_path.write_text(
        json.dumps(result.questions, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    from wia_output import write_wia_workbook
    graph = result.graph
    (output_dir / "structured_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    connect_rows = []
    for edge in graph["connections"]:
        if not edge["source_module_id"] or not edge["target_module_id"]:
            continue
        connect_rows.append({**edge, "No.":len(connect_rows)+1,
            "initiator_module_name(发起端实例名/模块名)":edge["source_module_name"],
            "initiator_port":edge["source_port_name"], "target_module_name":edge["target_module_name"],
            "target_port":edge["target_port_name"], "bus_protocol":edge["protocol"]})
    issue_rows = [{**edge, "页面":edge["page"], "连接器ID":edge["connector_id"],
        "问题类型":edge["reason"], "Begin端模块":edge["source_module_name"],
        "Begin端Shape ID":edge["original_source_id"], "End端模块":edge["target_module_name"],
        "End端Shape ID":edge["original_target_id"],
        "BeginX":edge["source_point"][0] if edge["source_point"] else "",
        "BeginY":edge["source_point"][1] if edge["source_point"] else "",
        "EndX":edge["target_point"][0] if edge["target_point"] else "",
        "EndY":edge["target_point"][1] if edge["target_point"] else ""}
        for edge in graph["connection_issues"]]
    workbook_path = output_dir / f"{stem}_wia输出.xlsx"
    write_wia_workbook(workbook_path, hierarchy_rows=graph["hierarchy"],
        port_rows=graph["ports"], connect_rows=connect_rows,
        connection_issue_rows=issue_rows, include_connection_issues=True,
        extended_drawio=True)

    aliases = {
        "connections.csv": csv_path,
        "connections.xlsx": workbook_path,
        "validation.json": report_path,
        "questions.json": questions_path,
        "report.md": markdown_path,
    }
    for name, source in aliases.items():
        shutil.copyfile(source, output_dir / name)
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
