"""Conservative domain model for the formal WIA output contract."""

from __future__ import annotations

import re
from dataclasses import dataclass


def _visible(value):
    return "" if value in (None, "", "/") else str(value)


@dataclass(frozen=True)
class DiagramOutput:
    hierarchy: tuple[dict[str, object], ...]
    ports: tuple[dict[str, object], ...]
    connections: tuple[dict[str, object], ...]
    uncertain: tuple[dict[str, object], ...]


def build_diagram_output(
    *, connection_rows, module_candidates=(), top_port_candidates=()
) -> DiagramOutput:
    """Build formal rows and withhold records lacking required evidence."""
    hierarchy = []
    ports = []
    connections = []
    uncertain = []

    spelling_groups = {}
    for candidate in module_candidates:
        visible_name = _visible(candidate.get("visible_name"))
        if visible_name:
            key = re.sub(r"[\s_]+", "_", visible_name.casefold()).strip("_")
            spelling_groups.setdefault(key, set()).add(visible_name)
    for normalized, variants in sorted(spelling_groups.items()):
        if len(variants) > 1:
            uncertain.append({
                "record_type": "naming",
                "reason": "visually equivalent module names differ",
                "evidence": {
                    "normalized_name": normalized,
                    "variants": sorted(variants),
                },
            })

    for candidate in module_candidates:
        module_name = _visible(candidate.get("module_name"))
        instance_name = _visible(candidate.get("instance_name"))
        parent = _visible(candidate.get("parent_instance_name"))
        if not module_name or not instance_name:
            uncertain.append({
                "record_type": "hierarchy",
                "reason": "module_name and instance_name are not distinguishable",
                "evidence": {
                    "shape_id": candidate.get("shape_id", ""),
                    "visible_name": candidate.get("visible_name", ""),
                    "parent_instance_name": parent,
                },
            })
            continue
        hierarchy.append({
            "module_name": module_name,
            "instance_name": instance_name,
            "parent module name": parent,
        })

    for candidate in top_port_candidates:
        port_name = _visible(candidate.get("port_name"))
        direction = _visible(candidate.get("direction"))
        if not port_name or direction not in {"input", "output", "inout"}:
            uncertain.append({
                "record_type": "port",
                "reason": "top-level port name or direction is uncertain",
                "evidence": dict(candidate),
            })
            continue
        ports.append({
            "port_name": port_name,
            "direction": direction,
            "width": _visible(candidate.get("width")),
        })

    for source in connection_rows:
        source_module = _visible(source.get("源模块"))
        target_module = _visible(source.get("目标模块"))
        if not source_module or not target_module:
            uncertain.append({
                "record_type": "connection",
                "reason": "missing module endpoint",
                "evidence": {
                    "connector_id": source.get("连接器ID", ""),
                    "source_module": source_module,
                    "target_module": target_module,
                },
            })
            continue
        connections.append({
            "No.": len(connections) + 1,
            "initiator_module_name(发起端实例名/模块名)": source_module,
            "initiator_port": _visible(source.get("源模块口名")),
            "target_module_name": target_module,
            "target_port": _visible(source.get("目标端口名")),
            "bus_protocol": _visible(source.get("总线/信协议")),
            "direction": _visible(source.get("方向")),
        })

    return DiagramOutput(
        hierarchy=tuple(hierarchy), ports=tuple(ports),
        connections=tuple(connections), uncertain=tuple(uncertain),
    )
