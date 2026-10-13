"""Native Draw.io instance graph. Geometry is evidence, never an extra wire."""
from __future__ import annotations

import base64
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib


def read_pages(path):
    root = ET.parse(path).getroot()
    if root.tag == 'mxGraphModel':
        return [root]
    diagrams = root.findall('diagram')
    pages = []
    for diagram in diagrams:
        model = diagram.find('mxGraphModel')
        if model is None and (diagram.text or '').strip():
            text = diagram.text.strip()
            if text.startswith('<'):
                model = ET.fromstring(text)
            else:
                model = ET.fromstring(urllib.parse.unquote(
                    zlib.decompress(base64.b64decode(text), -15).decode()))
        if model is not None:
            pages.append(model)
    if not pages:
        raise ValueError('No mxGraphModel pages found')
    # Draw.io also wraps mxCell in UserObject/object nodes with id/label metadata.
    for model in pages:
        for wrapper in model.iter():
            cell = wrapper.find('mxCell')
            if cell is not None and wrapper.tag not in {'root', 'mxGraphModel'}:
                for key in ('id', 'label'):
                    if wrapper.get(key) is not None:
                        cell.set('value' if key == 'label' else key, wrapper.get(key))
                if wrapper.get('object_type'):
                    cell.set('object_type', wrapper.get('object_type'))
    return pages


def build_graph(pages):
    # Reuse existing XML label/style/containment helpers without coupling export.
    from parse_drawio_connections import (
        _label, _style_map, _contains_bounds, MODULE_RE, PORT_RE,
        PAD_RE, _normalize_pad_label,
    )
    graph = {k: [] for k in ('hierarchy', 'ports', 'connections', 'objects',
                             'hierarchy_issues', 'connection_issues', 'contains')}
    graph['schema_version'] = '2.0'
    multi = len(pages) > 1
    for page_number, model in enumerate(pages, 1):
        cells = list(model.iter('mxCell'))
        by = {}
        for cell in cells:
            i = cell.get('id', '')
            if not i or i in by:
                raise ValueError(f'Page {page_number}: missing or duplicate mxCell id {i!r}')
            by[i] = cell
        def uid(i):
            return f'page{page_number}:{i}' if i and multi else i
        def cid(i):
            return 'drawio:' + uid(i)
        def hissue(i, reason, **extra):
            graph['hierarchy_issues'].append({'shape_id': uid(i), 'page': page_number,
                                             'reason': reason, **extra})

        # Identify cycles up front; iterative traversal avoids Python recursion limits.
        cyclic = set()
        for i in by:
            trail = []
            current = i
            while current in by:
                if current in trail:
                    cyclic.update(trail[trail.index(current):]); break
                trail.append(current); current = by[current].get('parent', '')
        for i in sorted(cyclic):
            hissue(i, 'native parent cycle')

        bounds_cache = {}
        def bounds(i):
            if i in bounds_cache:
                return bounds_cache[i]
            trail = []
            current = i
            while current in by and current not in bounds_cache:
                if current in cyclic:
                    bounds_cache[current] = None; break
                trail.append(current)
                parent = by[current].get('parent', '')
                if parent not in by or by[parent].get('vertex') != '1':
                    break
                current = parent
            for key in reversed(trail):
                cell = by[key]; geometry = cell.find('mxGeometry')
                if geometry is None:
                    bounds_cache[key] = None; continue
                try:
                    x, y, w, h = [float(geometry.get(k, '0')) for k in ('x','y','width','height')]
                    if not all(math.isfinite(v) for v in (x,y,w,h)) or w < 0 or h < 0:
                        raise ValueError('invalid geometry')
                    parent = cell.get('parent', '')
                    if parent in by and by[parent].get('vertex') == '1':
                        pb = bounds_cache.get(parent)
                        if pb is None:
                            bounds_cache[key] = None; continue
                        if geometry.get('relative') == '1':
                            x *= pb[2]; y *= pb[3]
                        x += pb[0]; y += pb[1]
                    offset = geometry.find("mxPoint[@as='offset']")
                    if offset is not None:
                        x += float(offset.get('x',0)); y += float(offset.get('y',0))
                    bounds_cache[key] = [x,y,w,h]
                except ValueError:
                    bounds_cache[key] = None
            return bounds_cache.get(i)

        objects = {}
        for cell in cells:
            i = cell.get('id'); name = _label(cell); style = cell.get('style','')
            sm = _style_map(style); parent = by.get(cell.get('parent',''))
            if cell.get('edge') == '1':
                role, reason = 'connector', 'XML edge=1'
            elif cell.get('vertex') != '1':
                role, reason = 'root', 'non-vertex XML node'
            elif parent is not None and parent.get('edge') == '1':
                role, reason = 'edge_label', 'native child of connector'
            elif 'text' in style.split(';') or 'edgeLabel' in style or sm.get('shape') == 'text':
                role, reason = 'annotation', 'text shape, not a module or physical port'
            elif cell.get('object_type') == 'background' or sm.get('background') == '1':
                role, reason = 'background', 'explicit background metadata'
            elif cell.get('object_type') == 'port' or sm.get('shape') == 'port':
                role, reason = 'port', 'explicit port shape metadata'
            elif 'group' in style.split(';') and not name:
                role, reason = 'group', 'unnamed native layout group'
            elif name:
                role, reason = 'module', 'named non-text structural vertex'
            else:
                role, reason = 'background', 'unnamed non-group frame; no module evidence'
            objects[i] = {'shape_id':uid(i), 'original_shape_id':i, 'page':page_number,
                          'name':name, 'object_type':role, 'classification_reason':reason,
                          'original_parent_id':cell.get('parent',''), 'geometry':bounds(i),
                          'style':style, 'raw_attributes':dict(cell.attrib)}
            geometry = cell.find('mxGeometry')
            objects[i]['geometry_xml'] = ET.tostring(geometry,encoding='unicode') if geometry is not None else ''

        # Preserve the existing documented group-owned signal and PAD convention.
        # Unlike the old endpoint helper, ordinary decorated child boxes stay modules.
        port_owners = {}
        pad_ports = {}
        for i, obj in objects.items():
            if obj['object_type'] != 'module':
                continue
            cell = by[i]; name = obj['name']; pad = PAD_RE.fullmatch(name)
            if pad:
                obj['name'] = _normalize_pad_label(pad.group(1))
                pad_ports[i] = _normalize_pad_label(pad.group(2)); continue
            parent = cell.get('parent',''); g = objects.get(parent)
            if not g or g['object_type'] != 'group' or cell.get('style','').strip() or MODULE_RE.search(name):
                continue
            if not (PORT_RE.fullmatch(name) or re.fullmatch(
                    r'(?:[A-Za-z0-9]+_)*(?:s?clk|clock|reset|rst(?:_n)?|irq|interrupt|[rt]x[dp]?|[ms]iso|mosi|cs(?:_n)?)', name, re.I)):
                continue
            owners = [j for j,o in objects.items() if j != i and by[j].get('parent') == parent
                      and o['object_type'] == 'module' and MODULE_RE.search(o['name'])
                      and o['geometry'] and obj['geometry']
                      and _contains_bounds(o['geometry'],obj['geometry'])]
            if len(owners) == 1:
                obj['object_type'] = 'port'
                obj['classification_reason'] = 'plain signal label in native group, uniquely owned by module frame'
                obj['classification_evidence'] = 'group_signal_convention'
                port_owners[i] = owners[0]

        modules = {i:o for i,o in objects.items() if o['object_type'] == 'module'}
        parent_ids = {}; parent_candidates = {}; parent_evidence = {}
        for i, obj in modules.items():
            parent_ids[i] = ''; parent_candidates[i] = []
            parent_evidence[i] = 'root'
            if not obj['geometry']:
                parent_evidence[i] = 'unresolved'
                hissue(i, 'invalid or cyclic geometry'); continue
            # Nearest named ancestor through arbitrary unnamed group nesting.
            p = by[i].get('parent',''); seen = {i}
            while p in by and p not in seen:
                seen.add(p)
                if p in modules:
                    parent_ids[i] = p; parent_evidence[i] = 'native'; break
                p = by[p].get('parent','')
            if parent_ids[i]:
                continue
            candidates = [j for j,o in modules.items() if j != i and o['geometry']
                          and _contains_bounds(o['geometry'],obj['geometry'])]
            # A unique innermost frame is valid only if all others form its ancestor chain.
            nearest = [j for j in candidates if not any(
                k != j and _contains_bounds(modules[j]['geometry'],modules[k]['geometry'])
                for k in candidates)]
            if len(nearest) == 1:
                parent_ids[i] = nearest[0]; parent_evidence[i] = 'geometric'
            elif candidates:
                parent_candidates[i] = sorted(candidates)
                parent_evidence[i] = 'ambiguous'
                hissue(i, 'overlapping or equal container candidates', candidate_parent_ids=[uid(j) for j in sorted(candidates)])

        # Native and inferred edges can together form a cycle, even with acyclic XML.
        bad = set()
        for i in modules:
            trail = []; p = i
            while p:
                if p in trail:
                    bad.update(trail[trail.index(p):]); break
                trail.append(p); p = parent_ids.get(p,'')
        for i in sorted(bad):
            parent_candidates[i] = [parent_ids[i]] if parent_ids[i] else []
            parent_ids[i] = ''; parent_evidence[i] = 'ambiguous'
            hissue(i, 'combined native/geometric parent cycle')

        for i, obj in modules.items():
            p = parent_ids[i]; path = [i]; current = p
            while current:
                path.insert(0,current); current = parent_ids.get(current,'')
            is_container = any(value == i for value in parent_ids.values())
            obj['object_type'] = 'container' if is_container else 'module'
            row = {'module_name':obj['name'], 'instance_name':obj['name'],
                   'parent module name':modules[p]['name'] if p else '',
                   'module_id':uid(i), 'shape_id':uid(i), 'original_shape_id':i,
                   'parent_id':uid(p), 'parent_name':modules[p]['name'] if p else '',
                   'object_type':obj['object_type'], 'hierarchy_path':[uid(j) for j in path],
                   'hierarchy_name_path':[modules[j]['name'] for j in path],
                   'evidence_type':parent_evidence[i], 'geometry':obj['geometry'],
                   'candidate_parent_ids':[uid(j) for j in parent_candidates[i]],
                   'page':page_number}
            graph['hierarchy'].append(row)
            if p:
                graph['contains'].append({'parent_id':uid(p),'child_id':uid(i),
                                          'evidence_type':parent_evidence[i]})

        for i, obj in objects.items():
            if obj['object_type'] != 'port':
                continue
            if i not in port_owners:
                p = by[i].get('parent',''); seen = set()
                while p in by and p not in seen:
                    if p in modules:
                        port_owners[i] = p; break
                    seen.add(p); p = by[p].get('parent','')
            owner = port_owners.get(i,'')
            graph['ports'].append({'port_id':uid(i),'port_name':obj['name'],
                'module_id':uid(owner),'direction':'unknown','width':'',
                'evidence_type':obj.get('classification_evidence','native') if owner else 'unresolved', 'page':page_number})
        for i, name in pad_ports.items():
            graph['ports'].append({'port_id':uid(i)+':port','port_name':name,
                'module_id':uid(i),'direction':'unknown','width':'',
                'evidence_type':'compound_pad_label','page':page_number})

        def endpoint_point(edge, end):
            # Bound terminal coordinates override stale mxPoint fallbacks.
            sm = _style_map(edge.get('style','')); i = edge.get(end,'')
            b = bounds(i) if i in by else None
            prefix = 'exit' if end == 'source' else 'entry'
            if b and prefix+'X' in sm and prefix+'Y' in sm:
                try:
                    return [b[0]+float(sm[prefix+'X'])*b[2]+float(sm.get(prefix+'Dx',0)),
                            b[1]+float(sm[prefix+'Y'])*b[3]+float(sm.get(prefix+'Dy',0))]
                except ValueError:
                    return None
            geometry = edge.find('mxGeometry')
            point = geometry.find(f"mxPoint[@as='{end}Point']") if geometry is not None else None
            if point is None:
                return None
            try:
                xy = [float(point.get('x',0)),float(point.get('y',0))]
                p = edge.get('parent',''); pb = bounds(p) if p in by and by[p].get('vertex')=='1' else None
                return [xy[0]+pb[0],xy[1]+pb[1]] if pb else xy
            except ValueError:
                return None

        def boundary_distance(point, b):
            x,y,w,h = b; px,py = point
            if x-2<=px<=x+w+2 and y-2<=py<=y+h+2:
                return min(abs(px-x),abs(px-x-w),abs(py-y),abs(py-y-h))
            return float('inf')

        def resolve(edge, end):
            original = edge.get(end,''); point = endpoint_point(edge,end)
            resolved = original if original in modules or original in port_owners or original in pad_ports else ''
            evidence = 'native' if resolved else 'unresolved'; candidates = []
            reason = ''
            if original in objects and objects[original]['object_type'] == 'group':
                group_bounds = objects[original]['geometry']
                frames = [j for j,o in modules.items() if by[j].get('parent') == original
                          and group_bounds and o['geometry'] == group_bounds]
                if len(frames) == 1:
                    resolved = frames[0]; evidence = 'native_group'
                else:
                    candidates = frames
                    reason = 'native group has no unique module frame'
            if not original and point:
                candidates = [i for i,o in objects.items() if o['object_type'] in {'module','container','port'}
                              and o['geometry'] and boundary_distance(point,o['geometry']) <= 2]
                if len(candidates) == 1:
                    resolved = candidates[0]; evidence = 'geometric'; candidates = []
                elif candidates:
                    evidence = 'ambiguous'; reason = 'multiple shapes touch free endpoint'
            if original and not resolved:
                reason = 'native endpoint does not identify a module or owned port'
            if resolved in modules:
                # Alignment candidates are advisory only. Never overwrite native binding.
                children = [j for j,p in parent_ids.items() if p == resolved]
                if children and point:
                    b = modules[resolved]['geometry']
                    vertical = b and min(abs(point[1]-b[1]),abs(point[1]-b[1]-b[3])) <= 2
                    horizontal = b and min(abs(point[0]-b[0]),abs(point[0]-b[0]-b[2])) <= 2
                    candidates = []
                    for j in children:
                        cb = modules[j]['geometry']
                        if not cb:
                            continue
                        limit = max(20., min(b[2],b[3]) * .1)
                        if ((vertical and cb[0]<=point[0]<=cb[0]+cb[2]
                             and min(abs(point[1]-cb[1]),abs(point[1]-cb[1]-cb[3])) <= limit)
                            or (horizontal and cb[1]<=point[1]<=cb[1]+cb[3]
                                and min(abs(point[0]-cb[0]),abs(point[0]-cb[0]-cb[2])) <= limit)):
                            candidates.append(j)
                    if candidates:
                        reason = 'container boundary aligns with child candidates; no native child binding'
            module = port_owners.get(resolved,resolved if resolved in modules else '')
            port = resolved if resolved in port_owners else (resolved+':port' if resolved in pad_ports else '')
            return {'original_id':original, 'resolved_shape_id':uid(resolved),
                    'module_id':uid(module),'module_name':modules[module]['name'] if module else '',
                    'port_id':uid(port) if port else '',
                    'port_name':objects[resolved]['name'] if resolved in port_owners else pad_ports.get(resolved,''),
                    'parent_id':uid(parent_ids.get(module,'')), 'evidence':evidence,
                    'candidates':[uid(j) for j in sorted(candidates)],'point':point,'reason':reason}

        edges = [cell for cell in cells if cell.get('edge')=='1']
        def route(edge):
            start = endpoint_point(edge,'source'); end = endpoint_point(edge,'target')
            geometry = edge.find('mxGeometry')
            waypoints = []
            parent = edge.get('parent',''); pb = bounds(parent) if parent in by and by[parent].get('vertex')=='1' else None
            if geometry is not None:
                try:
                    waypoints = [[float(p.get('x',0))+(pb[0] if pb else 0),
                                  float(p.get('y',0))+(pb[1] if pb else 0)]
                                 for p in geometry.findall("Array[@as='points']/mxPoint")]
                except ValueError:
                    return []
            # Never invent auto-routed orthogonal segments from two terminals.
            if not waypoints and 'orthogonal' in edge.get('style','').lower():
                return []
            return [start,*waypoints,end] if start and end else []

        def distance_to_route(point, points):
            distance = float('inf')
            for a,b in zip(points,points[1:]):
                dx,dy = b[0]-a[0],b[1]-a[1]
                squared = dx*dx+dy*dy
                t = max(0.,min(1.,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/squared)) if squared else 0.
                distance = min(distance, math.hypot(point[0]-a[0]-t*dx,point[1]-a[1]-t*dy))
            return distance

        # Free protocol text is associated only when a visible stored route has
        # a unique nearby label and that label has a unique nearby connector.
        geometry_labels = {edge.get('id'):[] for edge in edges}
        label_edges = {}
        for j,o in objects.items():
            if o['object_type'] != 'annotation' or not o['geometry']:
                continue
            b=o['geometry']; center=[b[0]+b[2]/2,b[1]+b[3]/2]
            matches=[e.get('id') for e in edges if distance_to_route(center,route(e)) <= 6]
            label_edges[j]=matches
            for i in matches:
                geometry_labels[i].append(j)
        for edge in edges:
            i = edge.get('id'); source = resolve(edge,'source'); target = resolve(edge,'target')
            label = _label(edge); labels = [o for j,o in objects.items() if by[j].get('parent')==i and o['name']]
            protocol = label or ' / '.join(o['name'] for o in labels)
            protocol_evidence = 'edge_value' if label else ('edge_label' if labels else 'missing')
            protocol_candidates = geometry_labels[i] if not protocol else []
            if len(protocol_candidates) == 1 and len(label_edges[protocol_candidates[0]]) == 1:
                protocol = objects[protocol_candidates[0]]['name']
                protocol_evidence = 'geometric'
            sm = _style_map(edge.get('style',''))
            start = sm.get('startArrow','none') not in {'','none','0'}
            end = sm.get('endArrow','none') not in {'','none','0'}
            direction = 'bidirectional' if start and end else 'source_to_target' if end else 'target_to_source' if start else 'unknown'
            status = 'confirmed'
            reasons = []
            for endpoint_name, terminal in (('source',source),('target',target)):
                if terminal['reason']:
                    reasons.append(terminal['reason'])
                if not terminal['module_id']:
                    status = 'unresolved'
                    reasons.append(endpoint_name + ' endpoint unresolved')
            if status == 'confirmed' and (source['candidates'] or target['candidates']):
                status = 'needs_review'
            if not protocol:
                reasons.append('missing protocol label')
            if direction == 'unknown':
                reasons.append('arrow style does not establish direction')
            if protocol_evidence == 'geometric':
                reasons.append('protocol associated by geometry; requires review')
                if status == 'confirmed':
                    status = 'needs_review'
            geometry = edge.find('mxGeometry')
            row = {'connector_id':cid(i),'original_connector_id':i,'page':page_number,
                   'protocol':protocol,'protocol_evidence':protocol_evidence,
                   'protocol_label_ids':[o['shape_id'] for o in labels],
                   'candidate_protocol_ids':[uid(j) for j in protocol_candidates],
                   'direction':direction,'raw_style':edge.get('style',''),
                   'status':status,'reason':'; '.join(dict.fromkeys(reasons)),
                   'evidence_type':source['evidence']+' / '+target['evidence'],
                   'geometry_xml':ET.tostring(geometry,encoding='unicode') if geometry is not None else '',
                   'path_points':route(edge),
                   'raw_attributes':dict(edge.attrib)}
            for name, terminal in (('source',source),('target',target)):
                for key in ('module_id','module_name','port_id','port_name','parent_id','evidence','point','resolved_shape_id'):
                    row[name+'_'+key] = terminal[key]
                row['original_'+name+'_id'] = terminal['original_id']
                row['candidate_'+name+'_id'] = terminal['candidates']
            graph['connections'].append(row)
            if status != 'confirmed' or not protocol or direction == 'unknown':
                graph['connection_issues'].append(dict(row))
        graph['objects'].extend(objects.values())
    return graph
