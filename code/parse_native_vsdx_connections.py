#!/usr/bin/env python3
"""Extract a connection table directly from native VSDX connector/glue data."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def default_run_output_dir(input_path: Path, timestamp: str | None = None) -> Path:
    """Return a unique per-run output directory under the project output root."""
    stamp = timestamp or datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    return PROJECT_ROOT / 'output' / f'{Path(input_path).stem}_{stamp}'


VISIO_NS = "http://schemas.microsoft.com/office/visio/2012/main"
V = f"{{{VISIO_NS}}}"
PORT_RE = re.compile(r"^[mshp]\d+$", re.I)
PAD_RE = re.compile(
    r"^(PAD(?:[_ ]+)(?:DIG|OSC)(?:[_ ]+)IO)\((.+)[)）]$", re.I
)
PAGE_RE = re.compile(r"visio/pages/page(\d+)\.xml$")
FIELDS = [
    "序号",
    "连接器ID",
    "页面",
    "源模块口名",
    "源模块",
    "目标端口名",
    "目标模块",
    "总线/信协议",
    "数据宽度",
    "方向",
    "箭头颜色",
]


@dataclass(frozen=True)
class ParseResult:
    source: str
    rows: tuple[dict[str, object], ...]
    connector_count: int
    complete_connections: int
    missing_source: int
    missing_target: int
    unresolved_shapes: tuple[str, ...]
    semantic_warnings: tuple[dict[str, object], ...]
    vision_calls: tuple[dict[str, object], ...]
    vision_total_seconds: float
    semantic_entities: tuple[dict[str, object], ...] = ()
    module_candidates: tuple[dict[str, object], ...] = ()
    top_port_candidates: tuple[dict[str, object], ...] = ()
    terminal_shape_count: int = 0
    classified_terminal_shape_count: int = 0
    requested_shape_count: int = 0
    classified_requested_shape_count: int = 0


@dataclass(frozen=True)
class OutputPaths:
    csv: Path
    markdown: Path
    report: Path
    wia_workbook: Path
    uncertain: Path
    semantic_entities: Path


def write_friendly_aliases(paths: OutputPaths, output_dir: Path) -> dict[str, Path]:
    """Expose stable user-facing filenames alongside stem-specific artifacts."""
    aliases = {
        'connections.csv': paths.csv,
        'connections.xlsx': paths.wia_workbook,
        'validation.json': paths.report,
        'semantic_entities.json': paths.semantic_entities,
        'questions.json': paths.uncertain,
        'report.md': paths.markdown,
    }
    result = {}
    for name, source in aliases.items():
        target = output_dir / name
        shutil.copyfile(source, target)
        result[name] = target
    return result


def _cells(shape: ET.Element) -> dict[str, ET.Element]:
    return {cell.get("N", ""): cell for cell in shape.findall(f"{V}Cell")}


def _number(shape: ET.Element, name: str, default: float = 0.0) -> float:
    cell = _cells(shape).get(name)
    try:
        return float(cell.get("V", str(default))) if cell is not None else default
    except ValueError:
        return default


def _text(shape: ET.Element) -> str:
    text = shape.find(f"{V}Text")
    if text is None:
        return ""
    value = html.unescape("".join(text.itertext()))
    return re.sub(r"\s+", " ", value).strip()


def _normalize_pad_label(value: str) -> str:
    """Normalize visually equivalent PAD labels used in native VSDX text."""
    return re.sub(r"[ _]+", "_", value.strip()).upper()


def _box(shape: ET.Element) -> tuple[float, float, float, float]:
    pin_x, pin_y = _number(shape, "PinX"), _number(shape, "PinY")
    width, height = _number(shape, "Width"), _number(shape, "Height")
    loc_x = _number(shape, "LocPinX", width / 2)
    loc_y = _number(shape, "LocPinY", height / 2)
    return pin_x - loc_x, pin_y - loc_y, width, height


def _contains(outer: ET.Element, inner: ET.Element) -> bool:
    x, y, width, height = _box(outer)
    ix, iy, iwidth, iheight = _box(inner)
    center_x, center_y = ix + iwidth / 2, iy + iheight / 2
    return x <= center_x <= x + width and y <= center_y <= y + height


def _point_distance_to_shape(shape: ET.Element, point: tuple[float, float]) -> float:
    x, y, width, height = _box(shape)
    point_x, point_y = point
    delta_x = max(x - point_x, 0.0, point_x - (x + width))
    delta_y = max(y - point_y, 0.0, point_y - (y + height))
    return (delta_x * delta_x + delta_y * delta_y) ** 0.5


def _endpoint_shape(
    glued_shape: ET.Element | None,
    connector: ET.Element,
    endpoint: str,
    top_shapes: list[ET.Element],
) -> ET.Element | None:
    """Resolve external glue points from their actual page position.

    Visio permits a connection point owned by one shape to sit outside that
    shape. In that case ToSheet names the connection-point owner, not the
    module drawn at the connector endpoint.
    """
    point = (_number(connector, f"{endpoint}X"), _number(connector, f"{endpoint}Y"))
    tolerance = 1e-5
    if glued_shape is not None and _point_distance_to_shape(glued_shape, point) <= tolerance:
        return glued_shape

    candidates = [
        shape
        for shape in top_shapes
        if "BeginX" not in _cells(shape)
        and _text(shape)
        and _point_distance_to_shape(shape, point) <= tolerance
    ]
    if len(candidates) != 1:
        return glued_shape
    return candidates[0]


def _container_owners(top_shapes: list[ET.Element]) -> dict[ET.Element, str]:
    """Name blank bus containers from their top-edge title shape."""
    owners = {}
    for container in top_shapes:
        if _text(container) or "BeginX" in _cells(container):
            continue
        contained_ports = [
            shape
            for shape in top_shapes
            if shape is not container
            and PORT_RE.fullmatch(_text(shape))
            and _contains(container, shape)
        ]
        if len(contained_ports) < 3:
            continue
        titles = [
            shape
            for shape in top_shapes
            if shape is not container
            and _text(shape)
            and not PORT_RE.fullmatch(_text(shape))
            and _contains(container, shape)
        ]
        if not titles:
            continue
        # Visio Y grows upward. Architecture containers place their title
        # nearest the top border, above the port rows and contained modules.
        title = max(titles, key=lambda shape: _box(shape)[1] + _box(shape)[3] / 2)
        owners[container] = _text(title)
    return owners


def _endpoint(
    shape: ET.Element | None,
    top_shapes: list[ET.Element],
    container_owners: dict[ET.Element, str],
) -> tuple[str, str]:
    if shape is None:
        return "/", "/"
    label = _text(shape)
    pad = PAD_RE.fullmatch(label)
    if pad:
        return _normalize_pad_label(pad.group(1)), _normalize_pad_label(pad.group(2))
    if not PORT_RE.fullmatch(label):
        return label or "/", "/"

    area = _box(shape)[2] * _box(shape)[3]
    containers = [
        candidate
        for candidate in container_owners
        if _box(candidate)[2] * _box(candidate)[3] > area
        and _contains(candidate, shape)
    ]
    if containers:
        container = min(
            containers, key=lambda candidate: _box(candidate)[2] * _box(candidate)[3]
        )
        return container_owners[container], label

    owners = [
        candidate
        for candidate in top_shapes
        if candidate is not shape
        and _text(candidate)
        and not PORT_RE.fullmatch(_text(candidate))
        and _box(candidate)[2] * _box(candidate)[3] > area
        and _contains(candidate, shape)
    ]
    owner = min(owners, key=lambda candidate: _box(candidate)[2] * _box(candidate)[3]) if owners else None
    return (_text(owner) if owner is not None else "/"), label


def _direction(connector: ET.Element, visual_direction: str = "/") -> str:
    cells = _cells(connector)
    begin = cells.get("BeginArrow")
    end = cells.get("EndArrow")
    has_begin = begin is not None and begin.get("V", "0") not in {"", "0"}
    has_end = end is not None and end.get("V", "0") not in {"", "0"}
    if has_begin and has_end:
        return "双向"
    if has_end:
        return "源→目标"
    if has_begin:
        return "目标→源"
    return visual_direction


def _geometry_arrow_sides(shape: ET.Element) -> tuple[bool, bool]:
    """Return whether a native Geometry path has left and right arrow tips."""
    has_left = has_right = False
    for candidate in shape.iter(f"{V}Shape"):
        width = _number(candidate, "Width")
        if width <= 0:
            continue
        for section in candidate.findall(f"{V}Section[@N='Geometry']"):
            points = []
            for row in section.findall(f"{V}Row"):
                if row.get("T") not in {"MoveTo", "LineTo"}:
                    continue
                row_cells = {
                    cell.get("N", ""): cell for cell in row.findall(f"{V}Cell")
                }
                try:
                    points.append(
                        (float(row_cells["X"].get("V", "")),
                         float(row_cells["Y"].get("V", "")))
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if len(points) < 3:
                continue
            tolerance = max(width * 1e-6, 1e-9)
            for index, (x, y) in enumerate(points):
                before = points[index - 1]
                after = points[(index + 1) % len(points)]
                straddles_tip = (before[1] < y < after[1]) or (after[1] < y < before[1])
                if not straddles_tip:
                    continue
                if abs(x) <= tolerance and before[0] > x + tolerance and after[0] > x + tolerance:
                    has_left = True
                if abs(x - width) <= tolerance and before[0] < x - tolerance and after[0] < x - tolerance:
                    has_right = True
    return has_left, has_right


def _linked_visual_directions(page: ET.Element) -> dict[str, str]:
    """Read arrow direction from native visual Groups linked to connectors."""
    result = {}
    for group in page.findall(f".//{V}Shape[@Type='Group']"):
        pin_x = _cells(group).get("PinX")
        formula = pin_x.get("F", "") if pin_x is not None else ""
        match = re.fullmatch(r"Sheet\.(\d+)!PinX", formula)
        if not match:
            continue
        has_begin, has_end = _geometry_arrow_sides(group)
        if _number(group, "FlipX"):
            has_begin, has_end = has_end, has_begin
        if has_begin and has_end:
            result[match.group(1)] = "双向"
        elif has_end:
            result[match.group(1)] = "源→目标"
        elif has_begin:
            result[match.group(1)] = "目标→源"
    return result


def _linked_visual_text_shape_ids(page: ET.Element) -> dict[str, tuple[str, ...]]:
    """Map connectors to non-empty XML text Shapes in their linked visual Group."""
    result = {}
    for group in page.findall(f".//{V}Shape[@Type='Group']"):
        pin_x = _cells(group).get("PinX")
        formula = pin_x.get("F", "") if pin_x is not None else ""
        match = re.fullmatch(r"Sheet\.(\d+)!PinX", formula)
        if not match:
            continue
        ids = tuple(
            shape.get("ID", "")
            for shape in group.iter(f"{V}Shape")
            if shape is not group and shape.get("ID") and _text(shape)
        )
        if ids:
            result[match.group(1)] = ids
    return result


def _linked_visual_connector_ids(page: ET.Element) -> set[str]:
    linked = set()
    for group in page.findall(f".//{V}Shape[@Type='Group']"):
        pin_x = _cells(group).get("PinX")
        formula = pin_x.get("F", "") if pin_x is not None else ""
        match = re.fullmatch(r"Sheet\.(\d+)!PinX", formula)
        if match:
            linked.add(match.group(1))
    return linked


def _add_resolved_endpoints(page: ET.Element, context: dict) -> None:
    """Attach coordinate-resolved endpoint Shape IDs to the XML registry.

    ``endpoints`` retains the literal Connect/ToSheet facts.  The additional
    ``resolved_endpoints`` records which existing Shape is actually located at
    each glued connector endpoint when an external connection point is owned by
    a remote Shape.  No endpoint is created when the corresponding Glue is
    absent.
    """
    shapes = {s.get("ID"): s for s in page.findall(f".//{V}Shape")}
    top_parent = page.find(f"{V}Shapes")
    top_shapes = top_parent.findall(f"{V}Shape") if top_parent is not None else []
    for connector_id, connector_context in context['connectors'].items():
        connector = shapes[connector_id]
        raw = connector_context['endpoints']
        resolved = {}
        for cell, endpoint in (('BeginX', 'Begin'), ('EndX', 'End')):
            glued_id = raw.get(cell)
            if not glued_id:
                continue
            shape = _endpoint_shape(shapes.get(glued_id), connector, endpoint, top_shapes)
            if shape is not None:
                resolved[cell] = shape.get('ID')
        connector_context['resolved_endpoints'] = resolved


def _page_rows(page: ET.Element, page_number: int) -> tuple[list[dict[str, object]], list[str]]:
    all_shapes = {shape.get("ID", ""): shape for shape in page.findall(f".//{V}Shape")}
    top_parent = page.find(f"{V}Shapes")
    top_shapes = top_parent.findall(f"{V}Shape") if top_parent is not None else []
    container_owners = _container_owners(top_shapes)
    connectors = {
        shape_id: shape
        for shape_id, shape in all_shapes.items()
        if "BeginX" in _cells(shape)
    }
    visual_directions = _linked_visual_directions(page)
    linked_visual = _linked_visual_connector_ids(page)
    endpoints: dict[str, dict[str, str]] = {shape_id: {} for shape_id in connectors}
    for connect in page.findall(f".//{V}Connect"):
        connector_id = connect.get("FromSheet", "")
        from_cell = connect.get("FromCell", "")
        if connector_id in endpoints and from_cell in {"BeginX", "EndX"}:
            endpoints[connector_id][from_cell] = connect.get("ToSheet", "")

    rows = []
    unresolved = []
    for connector_id, connector in sorted(connectors.items(), key=lambda item: int(item[0])):
        source_shape = _endpoint_shape(
            all_shapes.get(endpoints[connector_id].get("BeginX", "")),
            connector,
            "Begin",
            top_shapes,
        )
        target_shape = _endpoint_shape(
            all_shapes.get(endpoints[connector_id].get("EndX", "")),
            connector,
            "End",
            top_shapes,
        )
        source_module, source_port = _endpoint(source_shape, top_shapes, container_owners)
        target_module, target_port = _endpoint(target_shape, top_shapes, container_owners)
        if source_shape is not None and source_module == "/":
            unresolved.append(f"page{page_number}:shape{source_shape.get('ID')}")
        if target_shape is not None and target_module == "/":
            unresolved.append(f"page{page_number}:shape{target_shape.get('ID')}")
        visual = connector_id in linked_visual
        rows.append(
            {
                "连接器ID": f"page{page_number}:{connector_id}",
                "页面": page_number,
                "源模块口名": source_port,
                "源模块": source_module,
                "目标端口名": target_port,
                "目标模块": target_module,
                "总线/信协议": "/",
                "数据宽度": "/",
                "方向": visual_directions.get(connector_id, _direction(connector)),
                "箭头颜色": _arrow_color(connector, page),
            }
        )
    return rows, unresolved


def _arrow_color(connector, page):
    candidates = [connector]
    # A linked visual group's actual painted children represent its arrow.
    for group in page.findall(f".//{V}Shape[@Type='Group']"):
        pin = _cells(group).get("PinX")
        if pin is not None and pin.get("F") == f"Sheet.{connector.get('ID')}!PinX":
            candidates = list(group.iter(f"{V}Shape")) + candidates
    colors = []
    for shape in candidates:
        cell = _cells(shape).get("LineColor")
        value = cell.get("V", "") if cell is not None else ""
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            color = tuple(int(value[i:i+2], 16) for i in (1,3,5))
        else:
            match = re.fullmatch(r"RGB\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", value, re.I)
            color = tuple(map(int, match.groups())) if match else None
        if color is not None and all(0 <= n <= 255 for n in color):
            colors.append(color)
    return colors[0] if colors and len(set(colors)) == 1 else "/"


def semantic_page_rows(
    page, page_number, response, context=None, warnings=None, entity_catalog=None
):
    from page_semantics import registry, validate, endpoint, DIRECTIONS
    context = context or registry(page, page_number)
    if any('resolved_endpoints' not in c for c in context['connectors'].values()):
        _add_resolved_endpoints(page, context)
    sm, cm = validate(response, context, warnings=warnings)
    shapes = {s.get("ID"): s for s in page.findall(f".//{V}Shape")}
    linked_visual = _linked_visual_connector_ids(page)
    linked_visual_directions = _linked_visual_directions(page)
    linked_visual_text_ids = _linked_visual_text_shape_ids(page)
    rows, unresolved = [], []
    for cid, c in sorted(context['connectors'].items(), key=lambda item: int(item[0])):
        ends = c['endpoints']
        resolved_ends = c['resolved_endpoints']
        begin_shape_id = resolved_ends.get('BeginX')
        end_shape_id = resolved_ends.get('EndX')
        source, sport = endpoint(begin_shape_id, sm, context)
        target, tport = endpoint(end_shape_id, sm, context)
        if semantics := cm.get(cid, {}):
            begin_port = semantics.get('begin_port_shape_id')
            end_port = semantics.get('end_port_shape_id')
            if begin_port:
                _, sport = endpoint(begin_port, sm, context)
            if end_port:
                _, tport = endpoint(end_port, sm, context)
        for end, module in [('BeginX',source),('EndX',target)]:
            if module == '/':
                unresolved.append(f"page{page_number}:shape{ends.get(end, 'missing')}")
        semantics = cm.get(cid, {})
        protocol = '/' if semantics.get('protocol') == 'unknown' else semantics.get('protocol', '/')
        width = '/' if semantics.get('width') == 'unknown' else semantics.get('width', '/')
        linked_ids = linked_visual_text_ids.get(cid, ())
        if protocol == '/':
            values = list(dict.fromkeys(
                context['shapes'][sid]['text'] for sid in linked_ids
                if sid in sm and sm[sid].get('role') == 'protocol_label'
                and context['shapes'][sid]['text']
            ))
            if len(values) == 1:
                protocol = values[0]
            elif len(values) > 1:
                warnings.append({
                    'page': page_number, 'connector_id': cid,
                    'field': 'protocol', 'shape_ids': list(linked_ids),
                    'reason': 'multiple linked protocol labels',
                })
        if protocol != '/':
            protocol = _canonical_catalog_name(
                entity_catalog, protocol, 'protocol'
            ) or protocol
        if width == '/':
            values = list(dict.fromkeys(
                context['shapes'][sid]['text'] for sid in linked_ids
                if sid in sm and sm[sid].get('role') == 'width_annotation'
                and context['shapes'][sid]['text']
            ))
            if len(values) == 1:
                width = values[0]
            elif len(values) > 1:
                warnings.append({
                    'page': page_number, 'connector_id': cid,
                    'field': 'width', 'shape_ids': list(linked_ids),
                    'reason': 'multiple linked width labels',
                })
        visual_direction = DIRECTIONS[semantics.get('visual_direction', 'unknown')]
        native_direction = _direction(shapes[cid])
        geometry_direction = linked_visual_directions.get(cid, '/')
        direction = geometry_direction if geometry_direction != '/' else native_direction
        if geometry_direction == '/' and cid in linked_visual and visual_direction != '/':
            direction = visual_direction
        elif direction == '/':
            direction = visual_direction
        rows.append({'连接器ID':f'page{page_number}:{cid}', '页面':page_number,
                     '源模块口名':sport, '源模块':source, '目标端口名':tport, '目标模块':target,
                     '总线/信协议':protocol,
                     '数据宽度':width,
                     '方向':direction, '箭头颜色':_arrow_color(shapes[cid], page)})
    return rows, unresolved


def _catalog_entity_for_text(entity_catalog, text, entity_type):
    normalized = text.strip().casefold()
    if not normalized or not entity_catalog:
        return None
    matches = []
    for entity in entity_catalog.get('entities', []):
        if entity.get('entity_type') != entity_type:
            continue
        names = [entity.get('canonical_name'), *entity.get('aliases', [])]
        if normalized in {
            str(name).strip().casefold() for name in names if isinstance(name, str)
        }:
            matches.append(entity)
    return matches[0] if len(matches) == 1 else None


def _canonical_catalog_name(entity_catalog, text, entity_type):
    entity = _catalog_entity_for_text(entity_catalog, text, entity_type)
    return entity.get('canonical_name') if entity else None


def _semantic_output_candidates(
    response, context, entity_catalog=None, validation_warnings=()
):
    """Preserve validated Shape semantics for the formal WIA output stage."""
    from page_semantics import validate

    shape_semantics, _ = validate(response, context, warnings=[])
    registry_shapes = context['shapes']
    unresolved_owner_ids = {
        warning['shape_id'] for warning in validation_warnings
        if warning.get('field') == 'owner_shape_id'
        and warning.get('shape_id') in shape_semantics
    }

    def visible_name(shape_id):
        semantic = shape_semantics.get(shape_id, {})
        title_id = semantic.get('name_shape_id')
        if not title_id:
            titles = [
                sid for sid, item in shape_semantics.items()
                if item.get('role') == 'module_title'
                and item.get('owner_shape_id') == shape_id
            ]
            title_id = titles[0] if len(titles) == 1 else shape_id
        return registry_shapes.get(title_id, {}).get('text', '').strip()

    entities = []
    modules = []
    top_ports = []
    for shape_id, semantic in shape_semantics.items():
        role = semantic.get('role')
        text = registry_shapes.get(shape_id, {}).get('text', '').strip()
        entities.append({
            'page': context['page_number'],
            'shape_id': shape_id,
            'role': role,
            'semantic_status': (
                'unresolved' if shape_id in unresolved_owner_ids else 'resolved'
            ),
            'role_source': 'vlm',
            'normalization_source': (
                'entity_catalog'
                if _catalog_entity_for_text(
                    entity_catalog, text, 'module' if role in {'module', 'module_container'} else 'port'
                )
                else 'visible_text'
            ),
            'visible_text': text,
            'owner_shape_id': semantic.get('owner_shape_id', ''),
            'name_shape_id': semantic.get('name_shape_id', ''),
            'parent_group_id': registry_shapes.get(shape_id, {}).get('parent_group_id'),
            'linked_connector_id': registry_shapes.get(shape_id, {}).get(
                'hints', {}
            ).get('linked_connector_id'),
        })
        if role in {'module', 'module_container'}:
            name = visible_name(shape_id)
            entity = _catalog_entity_for_text(entity_catalog, name, 'module') or {}
            owner_id = semantic.get('owner_shape_id')
            modules.append({
                'page': context['page_number'],
                'shape_id': shape_id,
                'visible_name': name,
                'parent_instance_name': (
                    entity.get('parent_instance_name')
                    or (visible_name(owner_id) if owner_id else '')
                ),
                'module_name': entity.get('module_name'),
                'instance_name': entity.get('instance_name'),
                'source_type': (entity_catalog or {}).get('source', {}).get('type', ''),
                'is_simulated': bool(
                    (entity_catalog or {}).get('source', {}).get('is_simulated', False)
                ),
            })
        elif role == 'port':
            port_name = text
            entity = _catalog_entity_for_text(entity_catalog, port_name, 'port') or {}
            if semantic.get('owner_shape_id') and entity.get('is_top_level') is not True:
                continue
            top_ports.append({
                'page': context['page_number'],
                'shape_id': shape_id,
                'port_name': entity.get('port_name') or port_name,
                'direction': entity.get('direction'),
                'width': entity.get('width'),
                'visible_name': text,
                'source_type': (entity_catalog or {}).get('source', {}).get('type', ''),
                'is_simulated': bool(
                    (entity_catalog or {}).get('source', {}).get('is_simulated', False)
                ),
            })
    return entities, modules, top_ports


def parse_vsdx(
    path: Path, *, resolver=None, image_provider=None,
    debug_dir: Path | None = None, vision_fallback_only: bool = False,
    entity_catalog: dict | None = None, semantic_retry_attempts: int = 2, _clock=None,
) -> ParseResult:
    rows: list[dict[str, object]] = []
    unresolved: list[str] = []
    semantic_warnings: list[dict[str, object]] = []
    vision_calls: list[dict[str, object]] = []
    semantic_entities: list[dict[str, object]] = []
    module_candidates: list[dict[str, object]] = []
    top_port_candidates: list[dict[str, object]] = []
    terminal_shape_count = 0
    classified_terminal_shape_count = 0
    requested_shape_count = 0
    classified_requested_shape_count = 0
    clock = _clock or time.perf_counter
    with zipfile.ZipFile(path) as archive:
        page_names = sorted(
            (name for name in archive.namelist() if PAGE_RE.fullmatch(name)),
            key=lambda name: int(PAGE_RE.fullmatch(name).group(1)),
        )
        for page_name in page_names:
            page_number = int(PAGE_RE.fullmatch(page_name).group(1))
            page = ET.fromstring(archive.read(page_name))
            if resolver is None:
                page_rows, page_unresolved = _page_rows(page, page_number)
                for row in page_rows:
                    row['源模块口名'] = '/'
                    row['源模块'] = '/'
                    row['目标端口名'] = '/'
                    row['目标模块'] = '/'
                    row['总线/信协议'] = '/'
                    row['数据宽度'] = '/'
                page_unresolved = [
                    f"page{page_number}:shape{connect.get('ToSheet', 'missing')}"
                    for connect in page.findall(f".//{V}Connect")
                    if connect.get('FromCell') in {'BeginX', 'EndX'}
                ]
            else:
                from page_semantics import (
                    completion_retry_context,
                    endpoint_role_request_context,
                    id_overlay,
                    registry,
                )
                if image_provider is None:
                    raise ValueError("Visual parsing requires a full-page image provider")
                context = registry(page, page_number)
                _add_resolved_endpoints(page, context)
                if not context['connectors']:
                    page_rows, page_unresolved = [], []
                    rows.extend(page_rows)
                    unresolved.extend(page_unresolved)
                    continue
                image = Path(image_provider(page_number))
                if not image.is_file():
                    raise FileNotFoundError(image)
                request_context = endpoint_role_request_context(
                    context, entity_catalog=entity_catalog
                )
                terminal_ids = set(request_context['terminal_shape_ids'])
                required_shape_ids = set(request_context['shapes'])
                context['required_shape_ids'] = sorted(required_shape_ids)
                terminal_shape_count += len(terminal_ids)
                requested_shape_count += len(required_shape_ids)
                if semantic_retry_attempts < 0:
                    raise ValueError('semantic_retry_attempts must not be negative')
                call_started = clock()
                vision_error = None
                retry_count = 0
                try:
                    raw_response = resolver.resolve_page(image, request_context)
                except Exception as error:
                    vision_error = error
                    raw_response = {'shapes': [], 'connections': []}
                response_shapes = [
                    shape for shape in raw_response.get('shapes', [])
                    if shape.get('shape_id') in required_shape_ids
                ]
                classified_ids = {
                    shape.get('shape_id') for shape in response_shapes
                }
                missing_ids = required_shape_ids - classified_ids
                while vision_error is None and missing_ids and retry_count < semantic_retry_attempts:
                    retry_count += 1
                    retry_context = completion_retry_context(request_context, missing_ids)
                    try:
                        retry_response = resolver.resolve_page(image, retry_context)
                    except Exception as error:
                        vision_error = error
                        break
                    additions = [
                        shape for shape in retry_response.get('shapes', [])
                        if shape.get('shape_id') in missing_ids
                    ]
                    response_shapes.extend(additions)
                    classified_ids.update(shape.get('shape_id') for shape in additions)
                    missing_ids = required_shape_ids - classified_ids
                call_seconds = round(clock() - call_started, 6)
                response = {'shapes': response_shapes, 'connections': []}
                call_timing = {
                    'page': page_number,
                    'seconds': call_seconds,
                    'status': 'failed' if vision_error else 'resolved',
                }
                if retry_count:
                    call_timing['semantic_retry_attempts'] = retry_count
                    call_timing['missing_shape_ids_after_retry'] = sorted(missing_ids)
                vision_calls.append(call_timing)
                page_warnings: list[dict[str, object]] = []
                if vision_error is not None:
                    page_warnings.append({
                        'page': page_number,
                        'field': 'vision_failure',
                        'reason': f'{type(vision_error).__name__}: {vision_error}',
                    })
                debug_payload = {
                    'registry': context,
                    'semantics': response,
                    'vision_call_seconds': call_seconds,
                    'semantic_warnings': page_warnings,
                }
                if vision_error is not None:
                    debug_payload['vision_error'] = (
                        f'{type(vision_error).__name__}: {vision_error}'
                    )
                if debug_dir is not None:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    debug_path = debug_dir / f"page{page_number}.json"
                    debug_path.write_text(
                        json.dumps(debug_payload, ensure_ascii=False, indent=2),
                        encoding='utf-8',
                    )
                try:
                    page_rows, page_unresolved = semantic_page_rows(
                        page, page_number, response, context, warnings=page_warnings,
                        entity_catalog=entity_catalog,
                    )
                    page_entities, page_modules, page_top_ports = _semantic_output_candidates(
                        response, context, entity_catalog=entity_catalog,
                        validation_warnings=page_warnings,
                    )
                    semantic_entities.extend(page_entities)
                    module_candidates.extend(page_modules)
                    top_port_candidates.extend(page_top_ports)
                    classified_ids = {shape['shape_id'] for shape in response['shapes']}
                    classified_terminal_shape_count += len(terminal_ids & classified_ids)
                    classified_requested_shape_count += len(required_shape_ids & classified_ids)
                    if vision_error is None and any(
                        warning.get('field') in {
                            'terminal_classification', 'shape_classification'
                        }
                        for warning in page_warnings
                    ):
                        call_timing['status'] = 'incomplete'
                except Exception as error:
                    if debug_dir is not None:
                        debug_payload['validation_error'] = (
                            f'{type(error).__name__}: {error}'
                        )
                        debug_path.write_text(
                            json.dumps(debug_payload, ensure_ascii=False, indent=2),
                            encoding='utf-8',
                        )
                    raise
                semantic_warnings.extend(page_warnings)
                if debug_dir is not None:
                    debug_path.write_text(
                        json.dumps(debug_payload, ensure_ascii=False, indent=2),
                        encoding='utf-8',
                    )
                    id_overlay(image, context, debug_dir / f"page{page_number}_ids.png")
            rows.extend(page_rows)
            unresolved.extend(page_unresolved)

    for index, row in enumerate(rows, 1):
        row["序号"] = index
    missing_source = sum(
        row["源模块"] == "/" for row in rows
    )
    missing_target = sum(
        row["目标模块"] == "/" for row in rows
    )
    def unique_candidates(candidates):
        seen = set()
        unique = []
        for candidate in candidates:
            key = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
        return tuple(unique)

    return ParseResult(
        source=str(path),
        rows=tuple(rows),
        connector_count=len(rows),
        complete_connections=sum(
            row["源模块"] != "/" and row["目标模块"] != "/"
            for row in rows
        ),
        missing_source=missing_source,
        missing_target=missing_target,
        unresolved_shapes=tuple(sorted(set(unresolved))),
        semantic_warnings=tuple(semantic_warnings),
        vision_calls=tuple(vision_calls),
        vision_total_seconds=round(
            sum(float(call['seconds']) for call in vision_calls), 6
        ),
        semantic_entities=unique_candidates(semantic_entities),
        module_candidates=unique_candidates(module_candidates),
        top_port_candidates=unique_candidates(top_port_candidates),
        terminal_shape_count=terminal_shape_count,
        classified_terminal_shape_count=classified_terminal_shape_count,
        requested_shape_count=requested_shape_count,
        classified_requested_shape_count=classified_requested_shape_count,
    )


def _observed_diagram_profile(semantic_entities):
    roles = Counter(str(entity.get('role')) for entity in semantic_entities)
    if any(
        PAD_RE.fullmatch(str(entity.get('visible_text', '')))
        for entity in semantic_entities
    ):
        return 'soc_top_and_pad_connectivity'
    if roles['protocol_label'] or (
        roles['port'] >= 3 and roles['module_container'] >= 1
    ):
        return 'bus_architecture'
    return 'connection_diagram'


def write_outputs(result: ParseResult, output_dir: Path, stem: str) -> OutputPaths:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = OutputPaths(
        csv=output_dir / f"{stem}_连线表.csv",
        markdown=output_dir / f"{stem}_连线表.md",
        report=output_dir / f"{stem}_解析校验.json",
        wia_workbook=output_dir / f"{stem}_wia输出.xlsx",
        uncertain=output_dir / f"{stem}_疑问项.json",
        semantic_entities=output_dir / f"{stem}_语义实体.json",
    )
    with paths.csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(result.rows)
    with paths.markdown.open("w", encoding="utf-8") as stream:
        stream.write(f"# {stem} 连线表\n\n")
        stream.write(
            f"> 原生 VSDX 连接器 {result.connector_count} 条；完整双端连接 "
            f"{result.complete_connections} 条；缺少源端 {result.missing_source} 条；"
            f"缺少目标端 {result.missing_target} 条。\n\n"
        )
        stream.write("| " + " | ".join(FIELDS) + " |\n")
        stream.write("|" + "|".join("---" for _ in FIELDS) + "|\n")
        for row in result.rows:
            stream.write(
                "| "
                + " | ".join(str(row[field]).replace("|", "\\|") for field in FIELDS)
                + " |\n"
            )
    entity_roles = {
        role: sum(entity.get('role') == role for entity in result.semantic_entities)
        for role in sorted({str(entity.get('role')) for entity in result.semantic_entities})
    }
    observed_profile = _observed_diagram_profile(result.semantic_entities)
    connection_port_values = {
        str(row[field])
        for row in result.rows
        for field in ('源模块口名', '目标端口名')
        if row.get(field) not in (None, '', '/')
    }
    visible_port_entities = [
        entity for entity in result.semantic_entities if entity.get('role') == 'port'
    ]
    report = {
        "source": result.source,
        "connector_count": result.connector_count,
        "complete_connections": result.complete_connections,
        "missing_source": result.missing_source,
        "missing_target": result.missing_target,
        "unresolved_shapes": result.unresolved_shapes,
        "semantic_warnings": result.semantic_warnings,
        "vision_calls": result.vision_calls,
        "vision_total_seconds": result.vision_total_seconds,
        "terminal_classification_coverage": {
            "required": result.terminal_shape_count,
            "classified": result.classified_terminal_shape_count,
            "missing": result.terminal_shape_count - result.classified_terminal_shape_count,
            "ratio": (
                result.classified_terminal_shape_count / result.terminal_shape_count
                if result.terminal_shape_count else 1.0
            ),
        },
        "requested_shape_classification_coverage": {
            "required": result.requested_shape_count,
            "classified": result.classified_requested_shape_count,
            "missing": result.requested_shape_count - result.classified_requested_shape_count,
            "ratio": (
                result.classified_requested_shape_count / result.requested_shape_count
                if result.requested_shape_count else 1.0
            ),
        },
        "semantic_entity_count": len(result.semantic_entities),
        "module_candidate_count": len(result.module_candidates),
        "top_port_candidate_count": len(result.top_port_candidates),
        "observed_diagram_profile": observed_profile,
        "visible_information_coverage": {
            "semantic_entity_count": len(result.semantic_entities),
            "nonempty_text_entity_count": sum(
                bool(entity.get('visible_text')) for entity in result.semantic_entities
            ),
            "roles": entity_roles,
            "port_shape_count": len(visible_port_entities),
            "port_shapes_used_by_visible_text": sum(
                str(entity.get('visible_text', '')) in connection_port_values
                for entity in visible_port_entities
                if entity.get('visible_text')
            ),
            "connection_endpoint_cells": len(result.rows) * 2,
            "connection_endpoint_cells_with_port_name": sum(
                row.get(field) not in (None, '', '/')
                for row in result.rows
                for field in ('源模块口名', '目标端口名')
            ),
            "protocol_label_shape_count": entity_roles.get('protocol_label', 0),
        },
    }
    paths.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    from diagram_output_model import build_diagram_output
    from wia_output import write_wia_workbook
    visible_modules = sorted({
        str(row[field])
        for row in result.rows
        for field in ("源模块", "目标模块")
        if row.get(field) not in (None, "", "/")
    })
    module_candidates = result.module_candidates or tuple({
        "shape_id": "", "visible_name": name,
        "parent_instance_name": "", "module_name": None,
        "instance_name": None,
    } for name in visible_modules)
    formal = build_diagram_output(
        connection_rows=result.rows,
        module_candidates=module_candidates,
        top_port_candidates=result.top_port_candidates,
    )
    write_wia_workbook(
        paths.wia_workbook,
        hierarchy_rows=formal.hierarchy,
        port_rows=formal.ports,
        connect_rows=formal.connections,
    )
    semantic_questions = [
        {
            'record_type': 'semantic',
            'reason': warning['reason'],
            'evidence': warning,
        }
        for warning in result.semantic_warnings
        if warning.get('field') in {
            'owner_shape_id', 'terminal_classification', 'shape_classification'
        }
    ]
    paths.uncertain.write_text(
        json.dumps([*formal.uncertain, *semantic_questions], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths.semantic_entities.write_text(
        json.dumps(result.semantic_entities, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("vsdx", type=Path)
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=None,
        help="Output directory (default: a new timestamped directory under <project>/output)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--vision-config', type=Path, help='Configurable HTTP transport JSON')
    group.add_argument('--semantic-json', type=Path, help='Replay page-number keyed model responses')
    parser.add_argument('--page-images', type=Path, help='Full-page exports named page1.png, page2.png, ...')
    parser.add_argument('--debug-dir', type=Path)
    parser.add_argument(
        '--entity-catalog', type=Path,
        help='Document-derived entity JSON used as role-classification hints',
    )
    parser.add_argument(
        '--vision-fallback-only', action='store_true',
        help='Deprecated compatibility flag; relevant pages always run one role-only vision task',
    )
    parser.add_argument('--render-config', type=Path, help='JSON argv command using {vsdx}, {output_dir}; exports pageN.png')
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.output_dir is None:
        args.output_dir = default_run_output_dir(args.vsdx)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.vision_config is None and args.semantic_json is None:
        default_config = PROJECT_ROOT / 'config' / 'vision.json'
        if default_config.is_file():
            args.vision_config = default_config
    vision_config = None
    entity_catalog = None
    resolver = None
    if args.entity_catalog is not None:
        entity_catalog = json.loads(args.entity_catalog.read_text(encoding='utf-8'))
        if not isinstance(entity_catalog, dict) or not isinstance(entity_catalog.get('entities'), list):
            parser.error('--entity-catalog must be a JSON object containing an entities array')
    if args.vision_config is not None or args.semantic_json is not None:
        from page_semantics import HttpVisionResolver, JsonResolver
        vision_config = (json.loads(args.vision_config.read_text(encoding='utf-8'))
                         if args.vision_config is not None else None)
        resolver = (HttpVisionResolver(vision_config)
                    if args.vision_config is not None else JsonResolver(args.semantic_json))
    render_temp = None
    auto_render_pages = resolver is not None and args.page_images is None
    if auto_render_pages:
        render_temp = tempfile.TemporaryDirectory(prefix='vsdx-pages-')
        args.page_images = Path(render_temp.name)
    if resolver and args.debug_dir is None:
        args.debug_dir = args.output_dir / 'debug'

    rendered_pages = False

    def image_provider(page_number):
        nonlocal rendered_pages
        if auto_render_pages and not rendered_pages:
            from render_pages import render_vsdx_pages
            render_vsdx_pages(
                args.vsdx, args.page_images, config_path=args.render_config,
            )
            rendered_pages = True
        return args.page_images / f'page{page_number}.png'

    try:
        result = parse_vsdx(
            args.vsdx, resolver=resolver,
            image_provider=image_provider if resolver else None,
            debug_dir=args.debug_dir,
            vision_fallback_only=args.vision_fallback_only,
            entity_catalog=entity_catalog,
            semantic_retry_attempts=(vision_config or {}).get(
                'semantic_retry_attempts', 2
            ),
        )
        paths = write_outputs(result, args.output_dir, args.vsdx.stem)
        friendly = write_friendly_aliases(paths, args.output_dir)
    finally:
        if render_temp is not None:
            render_temp.cleanup()
    print(
        json.dumps(
            {
                "connectors": result.connector_count,
                "complete": result.complete_connections,
                "missing_source": result.missing_source,
                "missing_target": result.missing_target,
                "semantic_warnings": result.semantic_warnings,
                "vision_calls": result.vision_calls,
                "vision_total_seconds": result.vision_total_seconds,
                "csv": str(paths.csv),
                "markdown": str(paths.markdown),
                "report": str(paths.report),
                "wia_workbook": str(paths.wia_workbook),
                "semantic_entities": str(paths.semantic_entities),
                "uncertain": str(paths.uncertain),
                "user_outputs": {key: str(value) for key, value in friendly.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
