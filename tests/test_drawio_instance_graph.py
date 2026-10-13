import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zlib
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / 'code'))
from parse_drawio_connections import parse_drawio, write_outputs
from test_wia_output import workbook_values

FIXTURE = HERE.parent / 'fixtures' / 'chip_subsystem.drawio'
PREFIX = 'jtHkyc7mrChYPoDOt1jT-'


def box(i, name, x, y, w, h, parent='1', style='', relative=False):
    return f'<mxCell id="{i}" parent="{parent}" vertex="1" value="{name}" style="{style}"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" relative="{int(relative)}" as="geometry"/></mxCell>'


def document(body):
    return '<mxfile><diagram id="p"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>' + body + '</root></mxGraphModel></diagram></mxfile>'


class NativeGraphTest(unittest.TestCase):
    def graph(self, result):
        self.assertTrue(hasattr(result, 'graph'), 'parser must expose instance graph')
        return result.graph

    def sample(self, body):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sample.drawio'; p.write_text(document(body))
            return self.graph(parse_drawio(p))

    def test_real_shape_ids_pair_each_rts_with_its_own_core(self):
        graph = self.graph(parse_drawio(FIXTURE))
        nodes = {n['module_id']: n for n in graph['hierarchy']}
        parents = {7: 2, 22: 7, 41: 7, 53: 7, 75: 7, 121: 2,
                   36: 22, 37: 22, 47: 41, 49: 41, 59: 53, 61: 53,
                   79: 75, 81: 75, 31: 36, 38: 37, 48: 47, 50: 49,
                   60: 59, 62: 61, 80: 79, 82: 81, 114: 121, 116: 121}
        for child, parent in parents.items():
            self.assertEqual(PREFIX + str(parent), nodes[PREFIX + str(child)]['parent_id'])
        self.assertEqual([PREFIX + str(i) for i in [2, 7, 22, 36, 31]], nodes[PREFIX+'31']['hierarchy_path'])
        self.assertEqual(77, len(nodes))
        self.assertEqual([], graph['ports'])

    def test_real_connections_preserve_native_and_candidate_endpoints(self):
        g = self.graph(parse_drawio(FIXTURE))
        edges = {e['connector_id']: e for e in g['connections']}
        self.assertEqual(16, len(edges))
        e = edges['drawio:'+PREFIX+'125']
        self.assertEqual(PREFIX+'121', e['original_target_id'])
        self.assertEqual(PREFIX+'121', e['target_module_id'])
        self.assertIn(PREFIX+'116', e['candidate_target_id'])
        self.assertEqual('native', e['target_evidence'])
        self.assertEqual('unknown', e['direction'])
        for suffix, source, target in [(123,85,114),(127,7,119)]:
            e = edges['drawio:'+PREFIX+str(suffix)]
            self.assertEqual(PREFIX+str(source), e['source_module_id'])
            self.assertEqual(PREFIX+str(target), e['target_module_id'])
        self.assertEqual('unresolved', edges['drawio:'+PREFIX+'103']['status'])
        self.assertIn('source endpoint unresolved', edges['drawio:'+PREFIX+'103']['reason'])
        self.assertIn('target endpoint unresolved', edges['drawio:'+PREFIX+'103']['reason'])
        self.assertTrue(any(i['connector_id'].endswith('-103') for i in g['connection_issues']))
        self.assertTrue(all(not e['source_port_id'] and not e['target_port_id'] for e in edges.values()))
        self.assertEqual(['CHIE','AXI','CXSB','ACE-Lite','CXSB','ACE-Lite','HND','SBSX','Debug Chain','CXSB','ACE-Lite','CXSB','ACE-Lite','ACE-Lite','AXI'], [e['protocol'] for e in g['connections'] if e['protocol']])

    def test_chi_text_has_explicit_classification_and_reason(self):
        g = self.graph(parse_drawio(FIXTURE)); objects = {o['shape_id']:o for o in g['objects']}
        for suffix in [40,52,64,84]:
            o = objects[PREFIX+str(suffix)]
            self.assertEqual('annotation', o['object_type'])
            self.assertTrue(o['classification_reason'])

    def test_excel_appends_ids_and_preserves_all_edges_in_graph_or_issues(self):
        with tempfile.TemporaryDirectory() as d:
            write_outputs(parse_drawio(FIXTURE), Path(d), 'sample')
            sheets = workbook_values(Path(d)/'connections.xlsx')
            self.assertIn('module_id', sheets['hierarchy'][0])
            self.assertIn('connector_id', sheets['connect'][0])
            self.assertIn('connection_issues', sheets)
            self.assertIn('original_target_id', sheets['connection_issues'][0])
            graph = json.loads((Path(d)/'structured_graph.json').read_text())
            self.assertEqual(16, len(graph['connections']))
            self.assertEqual(['module_name','instance_name','parent module name'], sheets['hierarchy'][0][:3])
            for row in sheets['connect'][1:]:
                self.assertFalse(row[2]); self.assertFalse(row[4])
            ids=set()
            for sheet in ['connect','connection_issues']:
                index=sheets[sheet][0].index('connector_id')
                ids.update(row[index] for row in sheets[sheet][1:])
            self.assertEqual({'drawio:'+PREFIX+str(i) for i in [9,11,102,103,105,110,112,123,125,127,130,132,141,143,148,151]},ids)

    def test_overlapping_containers_do_not_force_a_parent_or_free_endpoint(self):
        body = box('a','A',0,0,100,100)+box('b','B',50,0,100,100)+box('c','C',60,20,10,10)
        body += '<mxCell id="e" edge="1" parent="1" target="c"><mxGeometry relative="1"><mxPoint x="75" y="0" as="sourcePoint"/></mxGeometry></mxCell>'
        g=self.sample(body); c=next(n for n in g['hierarchy'] if n['module_id']=='c')
        self.assertEqual('',c['parent_id']); self.assertEqual(['a','b'],c['candidate_parent_ids'])
        e=g['connections'][0]; self.assertEqual('',e['source_module_id'])
        self.assertEqual(['a','b'],e['candidate_source_id'])

    def test_native_cycles_are_reported_without_recursion(self):
        g=self.sample(box('a','A',0,0,100,100,'b')+box('b','B',0,0,100,100,'a'))
        self.assertTrue(g['hierarchy_issues'])
        self.assertTrue(all(not n['parent_id'] for n in g['hierarchy']))

    def test_relative_coordinates_and_unnamed_group_preserve_identity(self):
        g=self.sample(box('g','',100,200,200,100,style='group;')+box('a','A',0,0,200,100,'g')+box('p','P',.5,.5,10,10,'a',relative=True))
        objects={o['shape_id']:o for o in g['objects']}
        self.assertEqual([200.,250.,10.,10.],objects['p']['geometry'])
        self.assertEqual('group',objects['g']['object_type'])
        nodes={n['module_id']:n for n in g['hierarchy']}
        self.assertEqual('a',nodes['p']['parent_id'])

    def test_explicit_port_is_owned_and_regular_child_box_is_a_module(self):
        body=box('a','A',0,0,100,100)+box('b','B',10,10,30,30)+box('p','m0',1,.5,8,8,'a','shape=port;',True)
        body+= '<mxCell id="e" edge="1" parent="1" source="b" target="p" value="AXI" style="startArrow=classic;endArrow=none;"/>'
        g=self.sample(body); e=g['connections'][0]
        self.assertEqual('b',e['source_module_id']); self.assertEqual('',e['source_port_id'])
        self.assertEqual('a',e['target_module_id']); self.assertEqual('p',e['target_port_id'])
        self.assertEqual('target_to_source',e['direction'])
        self.assertEqual('p',g['ports'][0]['port_id'])

    def test_repeat_and_cli_without_api_key(self):
        self.assertEqual(self.graph(parse_drawio(FIXTURE)),self.graph(parse_drawio(FIXTURE)))
        with tempfile.TemporaryDirectory() as d:
            env={k:v for k,v in os.environ.items() if 'API_KEY' not in k}
            p=subprocess.run([sys.executable,str(HERE.parents[1]/'code'/'parse_drawio_connections.py'),str(FIXTURE),d],env=env,capture_output=True,text=True)
            self.assertEqual(0,p.returncode,p.stderr)
            self.assertTrue((Path(d)/'structured_graph.json').exists())

    def test_compressed_pages_with_duplicate_local_ids_are_namespaced(self):
        model='<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'+box('a','A',0,0,10,10)+'</root></mxGraphModel>'
        compressor=zlib.compressobj(wbits=-15)
        data=base64.b64encode(compressor.compress(urllib.parse.quote(model).encode())+compressor.flush()).decode()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'multi.drawio';p.write_text(f'<mxfile><diagram id="first">{data}</diagram><diagram id="second">{data}</diagram></mxfile>')
            g=self.graph(parse_drawio(p))
        self.assertEqual(['page1:a','page2:a'],[n['module_id'] for n in g['hierarchy']])
        self.assertEqual(['a','a'],[n['original_shape_id'] for n in g['hierarchy']])

    def test_geometry_protocol_association_requires_unique_wire(self):
        body=box('a','A',0,0,20,20)+box('b','B',100,0,20,20)
        wire='<mxCell id="e" edge="1" parent="1" source="a" target="b" style="startArrow=none;endArrow=none;"><mxGeometry relative="1"><mxPoint x="20" y="10" as="sourcePoint"/><mxPoint x="100" y="10" as="targetPoint"/></mxGeometry></mxCell>'
        label=box('l','AXI',50,0,20,20,style='text;')
        g=self.sample(body+wire+label)
        self.assertEqual('AXI',g['connections'][0]['protocol'])
        self.assertEqual('geometric',g['connections'][0]['protocol_evidence'])
        self.assertEqual('unknown',g['connections'][0]['direction'])
        g=self.sample(body+wire+wire.replace('id="e"','id="f"')+label)
        self.assertTrue(all(not e['protocol'] for e in g['connections']))
        self.assertTrue(all(e['candidate_protocol_ids']==['l'] for e in g['connections']))

    def test_native_group_binding_records_group_and_resolved_frame(self):
        body=box('g','',0,0,100,100,style='group;')+box('a','A',0,0,100,100,'g')+box('b','B',200,0,100,100)
        g=self.sample(body+'<mxCell id="e" edge="1" parent="1" source="g" target="b"/>')
        self.assertEqual('g',g['connections'][0]['original_source_id'])
        self.assertEqual('a',g['connections'][0]['source_module_id'])
        self.assertEqual('native_group',g['connections'][0]['source_evidence'])

    def test_named_background_is_not_a_parent_and_cycles_from_geometry_are_removed(self):
        g=self.sample(box('bg','Background',0,0,1000,1000,style='background=1;')+box('a','A',0,0,100,100,'b')+box('b','B',20,20,10,10))
        self.assertNotIn('bg',[n['module_id'] for n in g['hierarchy']])
        self.assertTrue(g['hierarchy_issues'])
        self.assertTrue(all(not n['parent_id'] for n in g['hierarchy']))

    def test_ordinary_plain_child_in_group_is_not_a_port(self):
        g=self.sample(box('g','',0,0,100,100,style='group;')+box('a','controller_top',0,0,100,100,'g')+box('b','Child',10,10,20,20,'g'))
        self.assertEqual([],g['ports'])
        self.assertEqual('a',next(n for n in g['hierarchy'] if n['module_id']=='b')['parent_id'])

    def test_module_label_starting_with_port_like_prefix_is_not_truncated(self):
        g=self.sample(box('a','m12_processor',0,0,10,10))
        self.assertEqual('m12_processor',g['hierarchy'][0]['module_name'])

    def test_stale_native_reference_stays_unresolved_even_near_a_module(self):
        body=box('a','A',0,0,20,20)+box('b','B',100,0,20,20)
        body+='<mxCell id="e" edge="1" parent="1" source="deleted" target="b"><mxGeometry relative="1"><mxPoint x="20" y="10" as="sourcePoint"/></mxGeometry></mxCell>'
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'stale.drawio';p.write_text(document(body));r=parse_drawio(p)
        g=self.graph(r)
        self.assertEqual('deleted',g['connections'][0]['original_source_id'])
        self.assertEqual('',g['connections'][0]['source_module_id'])
        self.assertEqual(1,r.native_missing_source)

    def test_direction_issue_keeps_original_arrow_style(self):
        g=self.sample(box('a','A',0,0,10,10)+box('b','B',50,0,10,10)+'<mxCell id="e" edge="1" parent="1" source="a" target="b" value="AXI" style="endArrow=none;startArrow=none;"/>')
        self.assertEqual('unknown',g['connections'][0]['direction'])
        self.assertEqual('endArrow=none;startArrow=none;',g['connection_issues'][0]['raw_style'])

    def test_deep_native_nesting_retains_full_id_path(self):
        body=''.join(box(f'n{i}', 'Same', 1,1,100,100, '1' if i==0 else f'n{i-1}') for i in range(128))
        g=self.sample(body)
        node=g['hierarchy'][-1]
        self.assertEqual('n126',node['parent_id'])
        self.assertEqual([f'n{i}' for i in range(128)],node['hierarchy_path'])
        self.assertEqual([128.,128.,100.,100.],node['geometry'])


if __name__=='__main__': unittest.main()
