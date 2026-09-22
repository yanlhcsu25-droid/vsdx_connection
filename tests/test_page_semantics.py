import sys
import unittest
import xml.etree.ElementTree as ET
import base64
import io
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'code'))
import parse_native_vsdx_connections as native


def page():
    return ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
    <Shape ID="1"><Cell N="Width" V="2"/><Cell N="Height" V="2"/></Shape>
    <Shape ID="2"><Text>engine</Text></Shape>
    <Shape ID="3"><Text>M_AXI</Text></Shape>
    <Shape ID="4"><Text>sink</Text></Shape>
    <Shape ID="5"><Text>AHB</Text></Shape>
    <Shape ID="9"><Cell N="BeginX" V="0"/><Cell N="EndArrow" V="1"/><Cell N="LineColor" V="#FF0000"/></Shape>
    </Shapes><Connects><Connect FromSheet="9" FromCell="BeginX" ToSheet="3"/><Connect FromSheet="9" FromCell="EndX" ToSheet="4"/></Connects></PageContents>''')


def answer():
    return {'shapes': [{'shape_id': '1', 'role': 'module_container', 'name_shape_id': '2'},
                       {'shape_id': '2', 'role': 'module_title', 'owner_shape_id': '1'},
                       {'shape_id': '3', 'role': 'port', 'owner_shape_id': '1'},
                       {'shape_id': '4', 'role': 'module'},
                       {'shape_id': '5', 'role': 'other'}],
            'connections': [{'connector_id': '9', 'protocol': 'AHB', 'protocol_shape_ids': ['5'],
                             'visual_direction': 'bidirectional'}]}


def png_bytes(width=100, height=80):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack(
            '>I', zlib.crc32(kind + payload) & 0xffffffff
        )
    pixels = b''.join(b'\x00' + b'\xff\xff\xff' * width for _ in range(height))
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(pixels))
        + chunk(b'IEND', b'')
    )


class SemanticsTest(unittest.TestCase):
    def test_nested_containers_accept_only_the_direct_port_owner(self):
        from page_semantics import registry, validate, endpoint
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="3"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="6"/><Cell N="Height" V="6"/><Text>subsystem</Text></Shape>
        <Shape ID="2"><Cell N="PinX" V="3"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="3"/><Cell N="Height" V="3"/><Text>matrix</Text></Shape>
        <Shape ID="3"><Cell N="PinX" V="3"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="0.4"/><Cell N="Height" V="0.2"/><Text>M0</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/><Cell N="EndX" V="3"/>
          <Cell N="EndY" V="3"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="EndX" ToSheet="3"/>
        </Connects></PageContents>''')
        context = registry(p, 1)
        native._add_resolved_endpoints(p, context)
        response = {'shapes': [
            {'shape_id': '1', 'role': 'module_container'},
            {'shape_id': '2', 'role': 'module_container'},
            {'shape_id': '3', 'role': 'port', 'owner_shape_id': '2'},
        ], 'connections': []}
        warnings = []

        shapes, _ = validate(response, context, warnings=warnings)

        self.assertEqual(('matrix', 'M0'), endpoint('3', shapes, context))
        self.assertEqual([], warnings)

        response['shapes'][2]['owner_shape_id'] = '1'
        warnings = []
        shapes, _ = validate(response, context, warnings=warnings)
        self.assertEqual(('/', 'M0'), endpoint('3', shapes, context))
        self.assertTrue(any(w.get('shape_id') == '3' and
                            w.get('reason') == 'port owner is not a direct candidate'
                            for w in warnings))

    def test_overlapping_direct_owners_leave_port_unresolved(self):
        from page_semantics import registry, validate, endpoint
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="3"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="3"/><Cell N="Height" V="3"/><Text>left</Text></Shape>
        <Shape ID="2"><Cell N="PinX" V="3.3"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="3"/><Cell N="Height" V="3"/><Text>right</Text></Shape>
        <Shape ID="3"><Cell N="PinX" V="3"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="0.4"/><Cell N="Height" V="0.2"/><Text>M0</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/><Cell N="EndX" V="3"/>
          <Cell N="EndY" V="3"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="EndX" ToSheet="3"/>
        </Connects></PageContents>''')
        context = registry(p, 1)
        native._add_resolved_endpoints(p, context)
        response = {'shapes': [
            {'shape_id': '1', 'role': 'module_container'},
            {'shape_id': '2', 'role': 'module_container'},
            {'shape_id': '3', 'role': 'port', 'owner_shape_id': '1'},
        ], 'connections': []}
        warnings = []

        shapes, _ = validate(response, context, warnings=warnings)

        self.assertEqual(('/', 'M0'), endpoint('3', shapes, context))
        self.assertTrue(any(w.get('shape_id') == '3' and
                            w.get('reason') == 'multiple direct owner candidates' and
                            w.get('candidate_owner_ids') == ['1', '2']
                            for w in warnings))
        entities, _, _ = native._semantic_output_candidates(
            response, context, validation_warnings=warnings
        )
        port = next(item for item in entities if item['shape_id'] == '3')
        self.assertEqual('unresolved', port['semantic_status'])
        self.assertEqual('', port['owner_shape_id'])
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            result = native.ParseResult(
                source='sample', rows=(), connector_count=0,
                complete_connections=0, missing_source=0, missing_target=0,
                unresolved_shapes=(), semantic_warnings=tuple(warnings),
                vision_calls=(), vision_total_seconds=0,
                semantic_entities=tuple(entities),
            )
            paths = native.write_outputs(result, Path(directory), 'sample')
            questions = json.loads(paths.uncertain.read_text(encoding='utf-8'))
        self.assertTrue(any(
            item['record_type'] == 'semantic'
            and item['evidence']['shape_id'] == '3'
            and item['reason'] == 'multiple direct owner candidates'
            for item in questions
        ))

    def test_port_owner_outside_unique_visible_container_is_unresolved(self):
        from page_semantics import registry, validate, endpoint, endpoint_role_request_context
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="2"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="2"/><Text>matrix</Text></Shape>
        <Shape ID="2"><Cell N="PinX" V="6"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="2"/><Text>core</Text></Shape>
        <Shape ID="3"><Cell N="PinX" V="2"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="0.4"/><Cell N="Height" V="0.2"/><Text>M0</Text></Shape>
        <Shape ID="4"><Cell N="PinX" V="2"/><Cell N="PinY" V="2.8"/>
          <Cell N="Width" V="1"/><Cell N="Height" V="0.2"/><Text>matrix title</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="EndX" ToSheet="3"/>
        </Connects></PageContents>''')
        context = registry(p, 1)
        native._add_resolved_endpoints(p, context)
        request = endpoint_role_request_context(context)
        self.assertEqual(sorted(request['shapes']), list(request['shapes']))
        self.assertEqual(['1'], request['shapes']['3']['candidate_owner_ids'])
        self.assertEqual(['4'], request['shapes']['1']['candidate_title_ids'])
        warnings = []
        shapes, _ = validate({'shapes': [
            {'shape_id': '1', 'role': 'module'},
            {'shape_id': '2', 'role': 'module'},
            {'shape_id': '3', 'role': 'port', 'owner_shape_id': '2'},
        ], 'connections': []}, context, warnings=warnings)

        self.assertEqual(('/', 'M0'), endpoint('3', shapes, context))
        self.assertNotIn('owner_shape_id', shapes['3'])
        self.assertTrue(any(w.get('shape_id') == '3' and
                            w.get('reason') == 'port owner outside candidate set'
                            for w in warnings))

    def test_semantic_endpoint_and_native_direction(self):
        self.assertTrue(hasattr(native, 'semantic_page_rows'), 'visual path is missing')
        rows, _ = native.semantic_page_rows(page(), 1, answer())
        self.assertEqual(('engine', 'M_AXI', 'sink'), tuple(rows[0][k] for k in ('源模块', '源模块口名', '目标模块')))
        self.assertEqual('源→目标', rows[0]['方向'])
        self.assertEqual((255, 0, 0), rows[0]['箭头颜色'])
        self.assertEqual('AHB', rows[0]['总线/信协议'])
        self.assertEqual('/', rows[0]['数据宽度'])

    def test_rejects_unknown_ids_and_unproven_text(self):
        self.assertTrue(hasattr(native, 'semantic_page_rows'), 'visual path is missing')
        data = answer()
        data['shapes'][0]['name_shape_id'] = '999'
        with self.assertRaises(ValueError):
            native.semantic_page_rows(page(), 1, data)

    def test_verified_xml_text_echo_does_not_change_semantics(self):
        from page_semantics import registry
        data = answer()
        xml_text = registry(page(), 1)['shapes']
        for item in data['shapes']:
            item['text'] = xml_text[item['shape_id']]['text']

        rows, _ = native.semantic_page_rows(page(), 1, data)

        self.assertEqual(('engine', 'M_AXI', 'sink'), (
            rows[0]['源模块'], rows[0]['源模块口名'], rows[0]['目标模块']
        ))
        self.assertEqual('engine', data['shapes'][1]['text'])

    def test_rejects_model_text_that_differs_from_xml(self):
        data = answer()
        data['shapes'][1]['text'] = 'invented module'

        with self.assertRaisesRegex(ValueError, 'text does not match XML'):
            native.semantic_page_rows(page(), 1, data)

    def test_drops_owner_reference_to_unclassified_existing_shape(self):
        data = answer()
        data['shapes'] = [shape for shape in data['shapes'] if shape['shape_id'] != '5']
        data['shapes'][3]['owner_shape_id'] = '5'
        warnings = []

        rows, _ = native.semantic_page_rows(page(), 1, data, warnings=warnings)

        self.assertEqual('sink', rows[0]['目标模块'])
        self.assertIn({
            'page': 1,
            'shape_id': '4',
            'field': 'owner_shape_id',
            'referenced_shape_id': '5',
            'reason': 'referenced shape was not classified in this response',
        }, warnings)

    def test_does_not_synthesize_unclassified_geometric_owner(self):
        from page_semantics import registry, validate
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="2"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="4"/><Cell N="Height" V="4"/></Shape>
        <Shape ID="2"><Cell N="PinX" V="2"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="1"/><Cell N="Height" V="1"/><Text>M0</Text></Shape>
        </Shapes></PageContents>''')
        context = registry(p, 1)
        warnings = []

        shapes, _ = validate({
            'shapes': [{'shape_id': '2', 'role': 'port', 'owner_shape_id': '1'}],
            'connections': [],
        }, context, warnings=warnings)

        self.assertNotIn('1', shapes)
        self.assertNotIn('owner_shape_id', shapes['2'])
        self.assertIn({
            'page': 1,
            'shape_id': '2',
            'field': 'owner_shape_id',
            'referenced_shape_id': '1',
            'reason': 'referenced shape was not classified in this response',
        }, warnings)
        data = answer()
        data['connections'][0]['protocol'] = 'AXI'
        with self.assertRaises(ValueError):
            native.semantic_page_rows(page(), 1, data)

    def test_missing_glue_never_inferred(self):
        self.assertTrue(hasattr(native, 'semantic_page_rows'), 'visual path is missing')
        p = page()
        p.remove(p.find(f'{native.V}Connects'))
        rows, _ = native.semantic_page_rows(p, 1, answer())
        self.assertEqual('/', rows[0]['源模块'])

    def test_model_omission_does_not_guess_semantics_from_glued_text(self):
        data = answer()
        data['shapes'] = [shape for shape in data['shapes'] if shape['shape_id'] != '4']
        warnings = []

        rows, _ = native.semantic_page_rows(page(), 1, data, warnings=warnings)

        self.assertEqual(('/', '/'), (rows[0]['目标模块'], rows[0]['目标端口名']))
        self.assertIn({
            'page': 1,
            'shape_id': '4',
            'field': 'terminal_classification',
            'reason': 'terminal Shape was not classified by the vision model',
        }, warnings)

    def test_non_endpoint_visual_role_does_not_fall_back_to_text_heuristics(self):
        data = answer()
        data['shapes'][3]['role'] = 'signal_label'

        rows, _ = native.semantic_page_rows(page(), 1, data)

        self.assertEqual(('/', '/'), (rows[0]['目标模块'], rows[0]['目标端口名']))

    def test_port_like_text_classified_as_annotation_stays_unresolved(self):
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Text>M0</Text></Shape>
        <Shape ID="2"><Text>sink</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="BeginX" ToSheet="1"/>
        <Connect FromSheet="9" FromCell="EndX" ToSheet="2"/></Connects></PageContents>''')
        response = {'shapes': [
            {'shape_id': '1', 'role': 'annotation'},
            {'shape_id': '2', 'role': 'module'},
        ], 'connections': []}

        rows, _ = native.semantic_page_rows(p, 1, response)

        self.assertEqual(('/', '/'), (rows[0]['源模块'], rows[0]['源模块口名']))

    def test_pad_like_text_classified_as_annotation_stays_unresolved(self):
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Text>PAD_DIG_IO(PAD_CLK)</Text></Shape>
        <Shape ID="2"><Text>sink</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="BeginX" ToSheet="1"/>
        <Connect FromSheet="9" FromCell="EndX" ToSheet="2"/></Connects></PageContents>''')
        response = {'shapes': [
            {'shape_id': '1', 'role': 'annotation'},
            {'shape_id': '2', 'role': 'module'},
        ], 'connections': []}

        rows, _ = native.semantic_page_rows(p, 1, response)

        self.assertEqual(('/', '/'), (rows[0]['源模块'], rows[0]['源模块口名']))

    def test_ignores_redundant_module_self_name_reference(self):
        data = answer()
        data['shapes'][3]['name_shape_id'] = '4'
        warnings = []

        rows, _ = native.semantic_page_rows(page(), 1, data, warnings=warnings)

        self.assertEqual('sink', rows[0]['目标模块'])
        self.assertEqual([{
            'page': 1,
            'shape_id': '4',
            'field': 'name_shape_id',
            'reason': 'module name references itself',
        }], warnings)

    def test_ignores_redundant_port_self_name_reference(self):
        data = answer()
        data['shapes'][2]['name_shape_id'] = '3'
        warnings = []

        rows, _ = native.semantic_page_rows(page(), 1, data, warnings=warnings)

        self.assertEqual('M_AXI', rows[0]['源模块口名'])
        self.assertEqual([{
            'page': 1,
            'shape_id': '3',
            'field': 'name_shape_id',
            'reason': 'shape name references itself',
        }], warnings)

    def test_visual_path_resolves_external_glue_from_connector_coordinates(self):
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="20"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="1"/>
          <Text>PAD_DIG_IO(PAD_USI0_SCLK)</Text></Shape>
        <Shape ID="2"><Cell N="PinX" V="2"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="2"/><Text>usi0_sec_top</Text></Shape>
        <Shape ID="3"><Cell N="PinX" V="10"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="1"/>
          <Text>PAD_DIG_IO(PAD_USI0_SD0)</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="2"/><Cell N="BeginY" V="1"/>
          <Cell N="EndX" V="10"/><Cell N="EndY" V="1"/></Shape>
        </Shapes><Connects>
          <Connect FromSheet="9" FromCell="BeginX" ToSheet="1"/>
          <Connect FromSheet="9" FromCell="EndX" ToSheet="1"/>
        </Connects></PageContents>''')
        response = {'shapes': [
            {'shape_id': '1', 'role': 'port'},
            {'shape_id': '2', 'role': 'module'},
            {'shape_id': '3', 'role': 'module'},
        ], 'connections': [{'connector_id': '9', 'visual_direction': 'unknown'}]}

        from page_semantics import registry
        context = registry(p, 1)
        native._add_resolved_endpoints(p, context)

        self.assertEqual(
            {'BeginX': '2', 'EndX': '3'},
            context['connectors']['9']['resolved_endpoints'],
        )

        rows, _ = native.semantic_page_rows(p, 1, response, context=context)

        self.assertEqual(
            ('usi0_sec_top', '/', 'PAD_DIG_IO(PAD_USI0_SD0)', '/'),
            tuple(rows[0][field] for field in (
                '源模块', '源模块口名', '目标模块', '目标端口名'
            )),
        )

    def test_ambiguous_geometry_keeps_native_glue_target(self):
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="20"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="1"/><Text>remote</Text></Shape>
        <Shape ID="2"><Cell N="PinX" V="2"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="2"/><Text>A</Text></Shape>
        <Shape ID="3"><Cell N="PinX" V="2"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="2"/><Cell N="Height" V="2"/><Text>B</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="2"/><Cell N="BeginY" V="1"/></Shape>
        </Shapes></PageContents>''')
        shapes = p.find(f'{native.V}Shapes').findall(f'{native.V}Shape')

        resolved = native._endpoint_shape(shapes[0], shapes[3], 'Begin', shapes)

        self.assertEqual('1', resolved.get('ID'))

    def test_unowned_compound_text_is_not_split_into_module_and_port(self):
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Text>source</Text></Shape>
        <Shape ID="2"><Text>PAD DIG IO(PAD_PWM CH0)</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="BeginX" ToSheet="1"/>
        <Connect FromSheet="9" FromCell="EndX" ToSheet="2"/></Connects></PageContents>''')
        response = {'shapes': [
            {'shape_id': '1', 'role': 'module'},
            {'shape_id': '2', 'role': 'port'},
        ], 'connections': [{'connector_id': '9', 'visual_direction': 'unknown'}]}

        rows, _ = native.semantic_page_rows(p, 1, response)

        self.assertEqual('/', rows[0]['目标模块'])
        self.assertEqual('PAD DIG IO(PAD_PWM CH0)', rows[0]['目标端口名'])
        from page_semantics import registry
        _, _, top_ports = native._semantic_output_candidates(response, registry(p, 1))
        self.assertEqual('PAD DIG IO(PAD_PWM CH0)', top_ports[0]['port_name'])

        response['shapes'][1]['role'] = 'module'
        rows, _ = native.semantic_page_rows(p, 1, response)
        self.assertEqual('PAD DIG IO(PAD_PWM CH0)', rows[0]['目标模块'])
        self.assertEqual('/', rows[0]['目标端口名'])

    def test_connection_can_reference_visible_port_label_owned_by_native_module(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        shapes.insert(4, ET.fromstring(
            f'<Shape xmlns="{native.VISIO_NS}" ID="6"><Text>OUT</Text></Shape>'
        ))
        data = answer()
        data['shapes'].append({'shape_id': '6', 'role': 'port', 'owner_shape_id': '4'})
        data['connections'][0]['end_port_shape_id'] = '6'

        rows, _ = native.semantic_page_rows(p, 1, data)

        self.assertEqual(('sink', 'OUT'), (rows[0]['目标模块'], rows[0]['目标端口名']))

    def test_ignores_optional_port_reference_without_owner_and_records_warning(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        shapes.insert(4, ET.fromstring(
            f'<Shape xmlns="{native.VISIO_NS}" ID="6"><Text>loose_label</Text></Shape>'
        ))
        data = answer()
        data['shapes'].append({'shape_id': '6', 'role': 'port'})
        data['connections'][0]['begin_port_shape_id'] = '6'
        warnings = []

        rows, _ = native.semantic_page_rows(p, 1, data, warnings=warnings)

        self.assertEqual('M_AXI', rows[0]['源模块口名'])
        self.assertEqual([{
            'page': 1,
            'connector_id': '9',
            'field': 'begin_port_shape_id',
            'shape_id': '6',
            'reason': 'port has no owner',
        }], warnings)

    def test_ignores_port_label_owned_by_a_different_native_module(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        shapes.insert(4, ET.fromstring(
            f'<Shape xmlns="{native.VISIO_NS}" ID="6"><Text>OUT</Text></Shape>'
        ))
        data = answer()
        data['shapes'].append({'shape_id': '6', 'role': 'port', 'owner_shape_id': '4'})
        data['connections'][0]['begin_port_shape_id'] = '6'
        warnings = []

        rows, _ = native.semantic_page_rows(p, 1, data, warnings=warnings)

        self.assertEqual('M_AXI', rows[0]['源模块口名'])
        self.assertEqual('port owner does not match native Glue endpoint', warnings[0]['reason'])

    def test_special_visual_group_uses_page_level_visual_direction(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        shapes.append(ET.fromstring(
            f'''<Shape xmlns="{native.VISIO_NS}" ID="10" Type="Group">
            <Cell N="PinX" V="0" F="Sheet.9!PinX"/></Shape>'''
        ))

        rows, _ = native.semantic_page_rows(p, 1, answer())

        self.assertEqual('双向', rows[0]['方向'])

    def test_special_visual_group_geometry_supplies_bidirectional_direction(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        connector = next(shape for shape in shapes if shape.get('ID') == '9')
        next(cell for cell in connector if cell.get('N') == 'EndArrow').set('V', '0')
        shapes.append(ET.fromstring(
            f'''<Shape xmlns="{native.VISIO_NS}" ID="10" Type="Group">
            <Cell N="PinX" V="2" F="Sheet.9!PinX"/>
            <Cell N="Width" V="4"/>
            <Shapes><Shape ID="11">
              <Cell N="Width" V="4"/>
              <Section N="Geometry" IX="0">
                <Row T="MoveTo" IX="1"><Cell N="X" V="1"/><Cell N="Y" V="1"/></Row>
                <Row T="LineTo" IX="2"><Cell N="X" V="0"/><Cell N="Y" V="2"/></Row>
                <Row T="LineTo" IX="3"><Cell N="X" V="1"/><Cell N="Y" V="3"/></Row>
                <Row T="LineTo" IX="4"><Cell N="X" V="3"/><Cell N="Y" V="3"/></Row>
                <Row T="LineTo" IX="5"><Cell N="X" V="4"/><Cell N="Y" V="2"/></Row>
                <Row T="LineTo" IX="6"><Cell N="X" V="3"/><Cell N="Y" V="1"/></Row>
              </Section>
            </Shape></Shapes></Shape>'''
        ))
        data = answer()
        data['connections'][0]['visual_direction'] = 'unknown'

        rows, _ = native.semantic_page_rows(p, 1, data)

        self.assertEqual('双向', rows[0]['方向'])

    def test_special_visual_group_geometry_preserves_one_sided_direction(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        connector = next(shape for shape in shapes if shape.get('ID') == '9')
        next(cell for cell in connector if cell.get('N') == 'EndArrow').set('V', '0')
        shapes.append(ET.fromstring(
            f'''<Shape xmlns="{native.VISIO_NS}" ID="10" Type="Group">
            <Cell N="PinX" V="2" F="Sheet.9!PinX"/>
            <Cell N="Width" V="4"/>
            <Shapes><Shape ID="11">
              <Cell N="Width" V="4"/>
              <Section N="Geometry" IX="0">
                <Row T="MoveTo" IX="1"><Cell N="X" V="0"/><Cell N="Y" V="1"/></Row>
                <Row T="LineTo" IX="2"><Cell N="X" V="3"/><Cell N="Y" V="1"/></Row>
                <Row T="LineTo" IX="3"><Cell N="X" V="4"/><Cell N="Y" V="2"/></Row>
                <Row T="LineTo" IX="4"><Cell N="X" V="3"/><Cell N="Y" V="3"/></Row>
              </Section>
            </Shape></Shapes></Shape>'''
        ))
        data = answer()
        data['connections'][0]['visual_direction'] = 'unknown'

        rows, _ = native.semantic_page_rows(p, 1, data)

        self.assertEqual('源→目标', rows[0]['方向'])

    def test_linked_group_text_classified_as_protocol_is_assigned_to_connector(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        shapes.append(ET.fromstring(
            f'''<Shape xmlns="{native.VISIO_NS}" ID="10" Type="Group">
            <Cell N="PinX" V="2" F="Sheet.9!PinX"/>
            <Shapes><Shape ID="11"><Text>AXI4-Lite</Text></Shape></Shapes>
            </Shape>'''
        ))
        data = answer()
        data['shapes'].append({'shape_id': '11', 'role': 'protocol_label'})
        data['connections'] = []

        rows, _ = native.semantic_page_rows(p, 1, data)

        self.assertEqual('AXI4-Lite', rows[0]['总线/信协议'])

    def test_linked_group_text_with_other_role_is_not_assigned_as_protocol(self):
        p = page()
        shapes = p.find(f'{native.V}Shapes')
        shapes.append(ET.fromstring(
            f'''<Shape xmlns="{native.VISIO_NS}" ID="10" Type="Group">
            <Cell N="PinX" V="2" F="Sheet.9!PinX"/>
            <Shapes><Shape ID="11"><Text>request</Text></Shape></Shapes>
            </Shape>'''
        ))
        data = answer()
        data['shapes'].append({'shape_id': '11', 'role': 'signal_label'})
        data['connections'] = []

        rows, _ = native.semantic_page_rows(p, 1, data)

        self.assertEqual('/', rows[0]['总线/信协议'])

if __name__ == '__main__':
    unittest.main()

class PipelineTest(unittest.TestCase):
    def test_pipeline_always_ignores_vlm_connection_fields(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                context = None
                def resolve_page(self, _image, context):
                    self.context = context
                    response = answer()
                    response['connections'][0]['visual_direction'] = 'bidirectional'
                    return response

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx, resolver=resolver, image_provider=lambda _: image,
                vision_fallback_only=False,
            )

        self.assertEqual('classify_endpoint_roles', resolver.context['task'])
        self.assertEqual({}, resolver.context['connectors'])
        self.assertEqual('源→目标', result.rows[0]['方向'])
        self.assertEqual('/', result.rows[0]['总线/信协议'])

    def test_role_request_marks_resolved_terminal_shapes(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                context = None
                def resolve_page(self, _image, context):
                    self.context = context
                    return answer()

            resolver = Resolver()
            native.parse_vsdx(vsdx, resolver=resolver, image_provider=lambda _: image)

        self.assertEqual({'3', '4'}, set(resolver.context['terminal_shape_ids']))

    def test_missing_terminal_marks_call_incomplete_and_becomes_a_question(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                calls = 0
                def resolve_page(self, _image, _context):
                    self.calls += 1
                    response = answer()
                    response['shapes'] = [
                        shape for shape in response['shapes'] if shape['shape_id'] != '4'
                    ]
                    return response

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx, resolver=resolver, image_provider=lambda _: image
            )
            paths = native.write_outputs(result, directory, 'sample')
            report = json.loads(paths.report.read_text(encoding='utf-8'))
            questions = json.loads(paths.uncertain.read_text(encoding='utf-8'))

        self.assertEqual(3, resolver.calls)
        self.assertEqual('incomplete', result.vision_calls[0]['status'])
        self.assertEqual(2, result.vision_calls[0]['semantic_retry_attempts'])
        self.assertEqual(['4'], result.vision_calls[0]['missing_shape_ids_after_retry'])
        self.assertEqual({
            'required': 2, 'classified': 1, 'missing': 1, 'ratio': 0.5,
        }, report['terminal_classification_coverage'])
        self.assertTrue(any(
            item['record_type'] == 'semantic'
            and item['evidence'].get('shape_id') == '4'
            and item['evidence'].get('field') == 'terminal_classification'
            for item in questions
        ))

    def test_missing_shape_is_merged_from_targeted_retry(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                contexts = []
                def resolve_page(self, _image, context):
                    self.contexts.append(context)
                    if len(self.contexts) == 1:
                        response = answer()
                        response['shapes'] = [
                            shape for shape in response['shapes']
                            if shape['shape_id'] != '4'
                        ]
                        return response
                    return {
                        'shapes': [{'shape_id': '4', 'role': 'module'}],
                        'connections': [],
                    }

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx, resolver=resolver, image_provider=lambda _: image
            )

        self.assertEqual(2, len(resolver.contexts))
        self.assertEqual(['4'], resolver.contexts[1]['retry_shape_ids'])
        self.assertEqual({'4'}, set(resolver.contexts[1]['shapes']))
        self.assertEqual('resolved', result.vision_calls[0]['status'])
        self.assertEqual(1, result.vision_calls[0]['semantic_retry_attempts'])
        self.assertEqual([], result.vision_calls[0]['missing_shape_ids_after_retry'])
        self.assertEqual(1, result.complete_connections)

    def test_page_without_connectors_skips_vision(self):
        import tempfile
        import zipfile
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Text>cover notes</Text></Shape>
        </Shapes></PageContents>''')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(p))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                calls = 0
                def resolve_page(self, _image, _context):
                    self.calls += 1
                    return {'shapes': [], 'connections': []}

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx, resolver=resolver, image_provider=lambda _: image
            )

        self.assertEqual(0, resolver.calls)
        self.assertEqual(0, result.connector_count)

    def test_role_request_includes_text_from_visual_group_linked_to_connector(self):
        import tempfile
        import zipfile

        p = page()
        p.find(f'{native.V}Shapes').append(ET.fromstring(
            f'''<Shape xmlns="{native.VISIO_NS}" ID="10" Type="Group">
            <Cell N="PinX" V="2" F="Sheet.9!PinX"/>
            <Shapes><Shape ID="11"><Text>custom-bus</Text></Shape></Shapes>
            </Shape>'''
        ))
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(p))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                contexts = []
                def resolve_page(self, _image, context):
                    self.contexts.append(context)
                    result = answer()
                    result['shapes'].append({'shape_id': '11', 'role': 'protocol_label'})
                    return result

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx, resolver=resolver, image_provider=lambda _: image,
                vision_fallback_only=True,
                entity_catalog={
                    'schema_version': '1.0',
                    'source': {'type': 'mock_document_extraction', 'is_simulated': True},
                    'entities': [{
                        'canonical_name': 'CUSTOM-BUS',
                        'entity_type': 'protocol',
                        'aliases': ['custom-bus'],
                        'evidence': [],
                    }],
                },
            )

        self.assertIn('11', resolver.contexts[0]['shapes'])
        self.assertEqual({}, resolver.contexts[0]['connectors'])
        self.assertEqual(
            [{'canonical_name': 'CUSTOM-BUS', 'entity_type': 'protocol',
              'match': 'alias', 'source_type': 'mock_document_extraction',
              'is_simulated': True}],
            resolver.contexts[0]['entity_candidates']['11'],
        )
        self.assertEqual('CUSTOM-BUS', result.rows[0]['总线/信协议'])

    def test_native_fallback_mode_calls_vision_when_flat_endpoint_roles_are_uncertain(self):
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                calls = 0
                contexts = []
                def resolve_page(self, _image, _context):
                    self.calls += 1
                    self.contexts.append(_context)
                    return answer()

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx,
                resolver=resolver,
                image_provider=lambda _: image,
                vision_fallback_only=True,
            )

        self.assertEqual(1, resolver.calls)
        self.assertEqual(1, len(result.vision_calls))
        self.assertEqual(1, result.complete_connections)
        self.assertEqual('classify_endpoint_roles', resolver.contexts[0]['task'])
        self.assertEqual({}, resolver.contexts[0]['connectors'])
        self.assertNotIn('9', resolver.contexts[0]['shapes'])

    def test_every_relevant_page_calls_vision_even_for_grouped_endpoints(self):
        import tempfile
        import zipfile

        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1" Type="Group"><Cell N="PinX" V="1"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="1"/><Cell N="Height" V="1"/><Text>source</Text></Shape>
        <Shape ID="2" Type="Group"><Cell N="PinX" V="4"/><Cell N="PinY" V="1"/>
          <Cell N="Width" V="1"/><Cell N="Height" V="1"/><Text>sink</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="1"/><Cell N="BeginY" V="1"/>
          <Cell N="EndX" V="4"/><Cell N="EndY" V="1"/><Cell N="EndArrow" V="1"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="BeginX" ToSheet="1"/>
        <Connect FromSheet="9" FromCell="EndX" ToSheet="2"/></Connects></PageContents>''')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(p))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                calls = 0
                def resolve_page(self, _image, _context):
                    self.calls += 1
                    return {'shapes': [
                        {'shape_id': '1', 'role': 'module'},
                        {'shape_id': '2', 'role': 'module'},
                    ], 'connections': []}

            resolver = Resolver()
            result = native.parse_vsdx(
                vsdx, resolver=resolver, image_provider=lambda _: image,
                vision_fallback_only=True,
            )

        self.assertEqual(1, resolver.calls)
        self.assertEqual(1, len(result.vision_calls))
        self.assertEqual(('source', 'sink'),
                         (result.rows[0]['源模块'], result.rows[0]['目标模块']))

    def test_h_number_port_is_resolved_inside_bus_container(self):
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Cell N="PinX" V="2"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="3"/><Cell N="Height" V="3"/></Shape>
        <Shape ID="2"><Cell N="PinX" V="2"/><Cell N="PinY" V="3"/>
          <Cell N="Width" V="1"/><Cell N="Height" V="0.2"/><Text>APB1</Text></Shape>
        <Shape ID="3"><Cell N="PinX" V="1"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="0.4"/><Cell N="Height" V="0.2"/><Text>H0</Text></Shape>
        <Shape ID="4"><Cell N="PinX" V="3"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="0.4"/><Cell N="Height" V="0.2"/><Text>P0</Text></Shape>
        <Shape ID="5"><Cell N="PinX" V="3"/><Cell N="PinY" V="1.5"/>
          <Cell N="Width" V="0.4"/><Cell N="Height" V="0.2"/><Text>P1</Text></Shape>
        <Shape ID="6"><Cell N="PinX" V="5"/><Cell N="PinY" V="2"/>
          <Cell N="Width" V="1"/><Cell N="Height" V="0.5"/><Text>sink</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="1"/><Cell N="BeginY" V="2"/>
          <Cell N="EndX" V="5"/><Cell N="EndY" V="2"/><Cell N="EndArrow" V="1"/></Shape>
        </Shapes><Connects><Connect FromSheet="9" FromCell="BeginX" ToSheet="3"/>
        <Connect FromSheet="9" FromCell="EndX" ToSheet="6"/></Connects></PageContents>''')

        rows, unresolved = native._page_rows(p, 1)

        self.assertEqual([], unresolved)
        self.assertEqual(('APB1', 'H0'), (rows[0]['源模块'], rows[0]['源模块口名']))

    def test_once_per_page_and_eleven_columns(self):
        import tempfile
        import zipfile
        from page_semantics import JsonResolver
        import json
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            f = d/'sample.vsdx'
            with zipfile.ZipFile(f,'w') as z:
                for n in [1,2]:
                    z.writestr(f'visio/pages/page{n}.xml',ET.tostring(page()))
            (d/'image.png').write_bytes(png_bytes())
            class Resolver:
                calls = 0
                def resolve_page(self,image,context):
                    self.calls += 1
                    return answer()
            r = Resolver()
            ticks = iter((10.0, 10.25, 20.0, 20.5))
            debug_dir = d/'debug'
            result = native.parse_vsdx(
                f, resolver=r, image_provider=lambda n:d/'image.png',
                debug_dir=debug_dir,
                _clock=lambda: next(ticks),
            )
            self.assertEqual(2,r.calls)
            self.assertEqual(2,len(result.rows))
            self.assertEqual(set(native.FIELDS),set(result.rows[0]))
            self.assertEqual(
                ({'page': 1, 'seconds': 0.25, 'status': 'resolved'},
                 {'page': 2, 'seconds': 0.5, 'status': 'resolved'}),
                result.vision_calls,
            )
            self.assertEqual(0.75, result.vision_total_seconds)
            paths = native.write_outputs(result,d,'out')
            self.assertTrue(paths.wia_workbook.exists())
            self.assertTrue(paths.uncertain.exists())
            self.assertTrue(paths.semantic_entities.exists())
            entities = json.loads(paths.semantic_entities.read_text(encoding='utf-8'))
            self.assertEqual(10, len(entities))
            uncertain = json.loads(paths.uncertain.read_text(encoding='utf-8'))
            self.assertEqual(4, len(uncertain))
            self.assertEqual({'engine', 'sink'}, {
                item['evidence']['visible_name'] for item in uncertain
            })
            report = json.loads(paths.report.read_text(encoding='utf-8'))
            self.assertEqual(0.75, report['vision_total_seconds'])
            self.assertEqual(0.5, report['vision_calls'][1]['seconds'])
            self.assertEqual('connection_diagram', report['observed_diagram_profile'])
            self.assertEqual(10, report['visible_information_coverage']['semantic_entity_count'])
            self.assertEqual(8, report['visible_information_coverage']['nonempty_text_entity_count'])
            self.assertEqual(2, report['visible_information_coverage']['port_shape_count'])
            page_debug = json.loads((debug_dir/'page1.json').read_text(encoding='utf-8'))
            self.assertEqual(0.25, page_debug['vision_call_seconds'])

    def test_parse_result_preserves_document_enriched_semantic_entities(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                def resolve_page(self, image_path, context):
                    return answer()

            catalog = {
                'source': {'type': 'mock_document_extraction', 'is_simulated': True},
                'entities': [
                    {
                        'canonical_name': 'engine_type', 'entity_type': 'module',
                        'aliases': ['engine'], 'module_name': 'engine_type',
                        'instance_name': 'u_engine', 'parent_instance_name': 'soc_top',
                    },
                    {
                        'canonical_name': 'sink_type', 'entity_type': 'module',
                        'aliases': ['sink'], 'module_name': 'sink_type',
                        'instance_name': 'u_sink', 'parent_instance_name': 'soc_top',
                    },
                ],
            }

            result = native.parse_vsdx(
                vsdx, resolver=Resolver(), image_provider=lambda page_number: image,
                entity_catalog=catalog,
            )

        self.assertEqual(
            {('engine_type', 'u_engine'), ('sink_type', 'u_sink')},
            {(item['module_name'], item['instance_name']) for item in result.module_candidates},
        )
        self.assertEqual({'1', '4'}, {item['shape_id'] for item in result.module_candidates})
        self.assertEqual(
            ('3', 'port', 'M_AXI', '1'),
            next(
                (item['shape_id'], item['role'], item['visible_text'], item['owner_shape_id'])
                for item in result.semantic_entities if item['shape_id'] == '3'
            ),
        )
        port_entity = next(
            item for item in result.semantic_entities if item['shape_id'] == '3'
        )
        self.assertEqual('resolved', port_entity['semantic_status'])
        self.assertEqual('vlm', port_entity['role_source'])
        self.assertEqual('visible_text', port_entity['normalization_source'])

    def test_final_vision_failure_preserves_topology_and_marks_semantics_unresolved(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())

            class Resolver:
                def resolve_page(self, _image, _context):
                    raise RuntimeError('provider unavailable')

            result = native.parse_vsdx(
                vsdx, resolver=Resolver(), image_provider=lambda _: image
            )

        self.assertEqual(1, result.connector_count)
        self.assertEqual(0, result.complete_connections)
        self.assertEqual(('/', '/'), (
            result.rows[0]['源模块'], result.rows[0]['目标模块']
        ))
        self.assertEqual('vision_failure', result.semantic_warnings[0]['field'])

    def test_document_can_mark_an_owned_visual_port_as_top_level(self):
        from page_semantics import registry
        context = registry(page(), 1)
        catalog = {
            'source': {'type': 'document_extraction', 'is_simulated': False},
            'entities': [{
                'canonical_name': 'M_AXI', 'entity_type': 'port', 'aliases': [],
                'is_top_level': True, 'direction': 'output', 'width': '64',
            }],
        }

        _, _, top_ports = native._semantic_output_candidates(
            answer(), context, entity_catalog=catalog
        )

        self.assertEqual(1, len(top_ports))
        self.assertEqual(
            ('M_AXI', 'output', '64'),
            tuple(top_ports[0][key] for key in ('port_name', 'direction', 'width')),
        )

    def test_protocol_output_uses_document_canonical_name(self):
        catalog = {
            'source': {'type': 'document_extraction', 'is_simulated': False},
            'entities': [{
                'canonical_name': 'AHB', 'entity_type': 'protocol',
                'aliases': ['ahb'], 'evidence': [],
            }],
        }

        self.assertEqual(
            'AHB', native._canonical_catalog_name(catalog, 'ahb', 'protocol')
        )

    def test_many_ports_in_a_container_have_bus_architecture_profile(self):
        entities = [
            {'role': 'module_container', 'visible_text': 'Matrix'},
            *({'role': 'port', 'visible_text': f'S{i}'} for i in range(3)),
        ]

        self.assertEqual('bus_architecture', native._observed_diagram_profile(entities))

    def test_fatal_semantic_validation_error_is_saved_to_debug_file(self):
        import json
        import tempfile
        import zipfile

        invalid = answer()
        invalid['shapes'][0]['unsupported_field'] = 'not allowed'
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory/'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(page()))
            image = directory/'page.png'
            image.write_bytes(png_bytes())
            debug_dir = directory/'debug'

            class Resolver:
                def resolve_page(self, _image, _context):
                    return invalid

            with self.assertRaisesRegex(ValueError, 'Invalid shapes fields'):
                native.parse_vsdx(
                    vsdx,
                    resolver=Resolver(),
                    image_provider=lambda _: image,
                    debug_dir=debug_dir,
                )

            saved = json.loads((debug_dir/'page1.json').read_text(encoding='utf-8'))
            self.assertEqual(invalid['shapes'], saved['semantics']['shapes'])
            self.assertEqual([], saved['semantics']['connections'])
            self.assertIn('Invalid shapes fields', saved['validation_error'])

    def test_port_text_without_module_owner_is_an_incomplete_endpoint(self):
        import tempfile
        import zipfile
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes>
        <Shape ID="1"><Text>source</Text></Shape><Shape ID="2"><Text>loose_port</Text></Shape>
        <Shape ID="9"><Cell N="BeginX" V="0"/></Shape></Shapes><Connects>
        <Connect FromSheet="9" FromCell="BeginX" ToSheet="1"/>
        <Connect FromSheet="9" FromCell="EndX" ToSheet="2"/></Connects></PageContents>''')
        response = {'shapes': [
            {'shape_id': '1', 'role': 'module'}, {'shape_id': '2', 'role': 'port'},
        ], 'connections': [{
            'connector_id': '9',
            'visual_direction': 'unknown',
            'end_port_shape_id': '2',
        }]}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            vsdx = directory / 'sample.vsdx'
            with zipfile.ZipFile(vsdx, 'w') as archive:
                archive.writestr('visio/pages/page1.xml', ET.tostring(p))
            image = directory / 'page.png'
            image.write_bytes(png_bytes())
            class Resolver:
                def resolve_page(self, image, context):
                    return response
            result = native.parse_vsdx(
                vsdx, resolver=Resolver(), image_provider=lambda _: image
            )

        self.assertEqual(1, result.missing_target)
        self.assertEqual(0, result.complete_connections)
        self.assertIn('page1:shape2', result.unresolved_shapes)

    def test_nested_group_coordinates_and_cycle_rejection(self):
        from page_semantics import registry,validate
        p = ET.fromstring(f'''<PageContents xmlns="{native.VISIO_NS}"><Shapes><Shape ID="1" Type="Group">
        <Cell N="PinX" V="10"/><Cell N="PinY" V="20"/><Shapes><Shape ID="2">
        <Cell N="PinX" V="2"/><Cell N="PinY" V="3"/></Shape></Shapes></Shape></Shapes></PageContents>''')
        context = registry(p,1)
        self.assertEqual([12,23,0,0],context['shapes']['2']['bbox'])
        self.assertEqual('1',context['shapes']['2']['parent_group_id'])
        with self.assertRaises(ValueError):
            validate({'shapes':[{'shape_id':'1','role':'module','owner_shape_id':'2'},
                                {'shape_id':'2','role':'module','owner_shape_id':'1'}], 'connections':[]},context)

    def test_vision_request_contains_original_page_and_shape_id_map(self):
        from page_semantics import registry, vision_image_blocks
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'page.png'
            image.write_bytes(png_bytes())
            blocks = vision_image_blocks(image, registry(page(), 1))

        self.assertEqual(2, len(blocks))
        self.assertTrue(blocks[0]['image_url']['url'].startswith('data:image/png;base64,'))
        id_map = base64.b64decode(blocks[1]['image_url']['url'].split(',', 1)[1])
        self.assertEqual(b'\x89PNG\r\n\x1a\n', id_map[:8])


class VisionConfigurationTest(unittest.TestCase):
    def test_role_only_request_does_not_ask_model_for_connections(self):
        from page_semantics import HttpVisionResolver
        import tempfile

        payload = {'choices': [{'message': {'content': '{"shapes": [], "connections": []}'},
                                'finish_reason': 'stop'}]}
        response = io.BytesIO(json.dumps(payload).encode())
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        context = {
            'task': 'classify_endpoint_roles', 'page_number': 1,
            'coordinate_system': 'page inches; origin bottom-left',
            'page_width': 1, 'page_height': 1, 'shapes': {}, 'connectors': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'page.png'
            image.write_bytes(png_bytes())
            resolver = HttpVisionResolver({
                'url': 'https://example.invalid/v1/chat/completions',
                'model': 'vision-model',
            })
            with patch('urllib.request.urlopen', return_value=response) as request:
                resolver.resolve_page(image, context)

        body = json.loads(request.call_args.args[0].data)
        prompt = body['messages'][0]['content'][0]['text']
        self.assertIn('Do not return connection items', prompt)
        self.assertNotIn('connections items:', prompt)

    def test_accepts_json_markdown_fence_and_requests_json_response(self):
        from page_semantics import HttpVisionResolver
        import tempfile

        payload = {'choices': [{'message': {'content': '```json\n{"shapes": [], "connections": []}\n```'},
                                'finish_reason': 'stop'}]}
        response = io.BytesIO(json.dumps(payload).encode())
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'page.png'
            image.write_bytes(png_bytes())
            resolver = HttpVisionResolver({
                'url': 'https://example.invalid/v1/chat/completions',
                'model': 'vision-model',
            })
            with patch('urllib.request.urlopen', return_value=response) as request:
                result = resolver.resolve_page(image, {'page_number': 1, 'shapes': {}, 'connectors': {}})

        self.assertEqual({'shapes': [], 'connections': []}, result)
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual({'type': 'json_object'}, body['response_format'])

    def test_retries_one_malformed_model_response(self):
        from page_semantics import HttpVisionResolver
        import tempfile

        def response(content):
            payload = {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}]}
            stream = io.BytesIO(json.dumps(payload).encode())
            stream.__enter__ = lambda value: value
            stream.__exit__ = lambda *args: None
            return stream

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'page.png'
            image.write_bytes(png_bytes())
            resolver = HttpVisionResolver({
                'url': 'https://example.invalid/v1/chat/completions',
                'model': 'vision-model',
            })
            with patch('urllib.request.urlopen', side_effect=[
                response('not json'), response('{"shapes": [], "connections": []}')
            ]) as request:
                result = resolver.resolve_page(image, {'page_number': 1, 'shapes': {}, 'connectors': {}})

        self.assertEqual({'shapes': [], 'connections': []}, result)
        self.assertEqual(2, request.call_count)

    def test_empty_model_response_reports_finish_reason_and_attempts(self):
        from page_semantics import HttpVisionResolver
        import tempfile

        payload = {'choices': [{'message': {'content': ''}, 'finish_reason': 'length'}]}
        responses = []
        for _ in range(2):
            stream = io.BytesIO(json.dumps(payload).encode())
            stream.__enter__ = lambda value: value
            stream.__exit__ = lambda *args: None
            responses.append(stream)
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'page.png'
            image.write_bytes(png_bytes())
            resolver = HttpVisionResolver({
                'url': 'https://example.invalid/v1/chat/completions',
                'model': 'vision-model',
            })
            with patch('urllib.request.urlopen', side_effect=responses):
                with self.assertRaisesRegex(ValueError, r'empty.*finish_reason=length.*attempts=2'):
                    resolver.resolve_page(image, {'page_number': 1, 'shapes': {}, 'connectors': {}})

    def test_rejects_api_key_value_used_as_environment_variable_name(self):
        from page_semantics import HttpVisionResolver
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'page.png'
            image.write_bytes(b'png fixture')
            resolver = HttpVisionResolver({
                'url': 'https://example.invalid/v1/chat/completions',
                'model': 'vision-model',
                'api_key_env': 'not-an-environment-variable-name',
            })

            with self.assertRaisesRegex(ValueError, 'environment variable name') as caught:
                resolver.resolve_page(image, {})

        self.assertNotIn('not-an-environment-variable-name', str(caught.exception))

    def test_reports_missing_api_key_environment_variable_without_keyerror(self):
        from page_semantics import HttpVisionResolver
        import os
        import tempfile

        variable = 'VSDX_TEST_MISSING_API_KEY'
        previous = os.environ.pop(variable, None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                image = Path(directory) / 'page.png'
                image.write_bytes(b'png fixture')
                resolver = HttpVisionResolver({
                    'url': 'https://example.invalid/v1/chat/completions',
                    'model': 'vision-model',
                    'api_key_env': variable,
                })

                with self.assertRaisesRegex(RuntimeError, variable):
                    resolver.resolve_page(image, {})
        finally:
            if previous is not None:
                os.environ[variable] = previous
