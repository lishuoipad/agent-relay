"""Synthetic read-only panel tests; never inspect the operator's Codex data."""
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[1] / 'tools/development-status/development_status.py'
spec = importlib.util.spec_from_file_location('relay_status_panel', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def make_fixture(root):
    checkpoints = root / 'checkpoints'; checkpoints.mkdir()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    log = root / 'main.jsonl'
    log.write_text(json.dumps({'type':'event_msg','timestamp':stamp,'payload':{'type':'task_started'}})+'\n', encoding='utf-8')
    child_log = root / 'child.jsonl'
    child_log.write_text('', encoding='utf-8')
    db = root / 'state.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, agent_path TEXT, agent_nickname TEXT, updated_at INTEGER, archived INTEGER)')
        conn.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
            ('sample-main',str(log),'','/root',None,0,0),
            ('sample-child',str(child_log),json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'sample-main'}}}),'/root/ui','layout',0,0)])
    conn.close()
    (checkpoints/'sample-main.json').write_text(json.dumps({'task':'UI-01 窗口检查，已交付待接收','updated_at':stamp,'next_actions':['核对受影响项']}),encoding='utf-8')
    (root/'plan.json').write_text(json.dumps({'tasks':[{'id':'UI-01','title':'界面检查','status':'in-progress','acceptance':'未测范围保留'}]}),encoding='utf-8')
    settings = {'state_db':'state.sqlite','checkpoints':'checkpoints','plan':'plan.json','title':'示例项目 · 开发状态','threads':[{'id':'sample-main','name':'主开发对话'}], 'timezone_offset_hours':8,
                'rules':[{'pattern':'窗口','summary':'检查界面','plan_ids':['UI-01'],'validation':'只读检查；未代表验收通过'}],
                'plan_id_pattern':r'\b(?:UI|BUILD)-\d+\b','text_replacements':{'Root':'主协调对话'}}
    config = root/'project.local.json'; config.write_text(json.dumps(settings,ensure_ascii=False),encoding='utf-8')
    return config


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.config = make_fixture(self.root)
        m.configure(self.config)

    def test_paths_are_relative_to_profile(self):
        self.assertEqual(m.DB,self.root/'state.sqlite')
        self.assertEqual(m.CHECKPOINTS,self.root/'checkpoints')

    def test_rules_are_association_not_acceptance(self):
        self.assertEqual(m.describe('检查窗口')[1],['UI-01'])
        self.assertIn('未代表验收',m.describe('检查窗口')[2])

    def test_explicit_number_without_rule(self):
        self.assertEqual(m.describe('BUILD-02 prepare package')[1],['BUILD-02'])

    def test_unknown_work_has_no_invented_number(self):
        self.assertEqual(m.describe('unclassified work')[1],[])

    def test_first_matching_rule_wins(self):
        x=json.loads(self.config.read_text(encoding='utf-8'))
        x['rules'].append({'pattern':'窗口','summary':'other','plan_ids':['OTHER-01'],'validation':'other'})
        self.config.write_text(json.dumps(x),encoding='utf-8');m.configure(self.config)
        self.assertEqual(m.describe('窗口')[1],['UI-01'])

    def test_duplicate_thread_rejected(self):
        x=json.loads(self.config.read_text(encoding='utf-8'));x['threads']*=2
        self.config.write_text(json.dumps(x),encoding='utf-8')
        with self.assertRaises(ValueError):m.configure(self.config)

    def test_bad_regex_rejected(self):
        x=json.loads(self.config.read_text(encoding='utf-8'));x['rules'][0]['pattern']='['
        self.config.write_text(json.dumps(x),encoding='utf-8')
        with self.assertRaises(m.re.error):m.configure(self.config)

    def test_pause_delivery_and_unknown_are_distinct(self):
        now=dt.datetime.now(m.TZ)
        self.assertEqual(m.status_from({'task':'原Goal仍paused'}, {}, {}, now)[1],'paused')
        self.assertEqual(m.status_from({'task':'已交付待接收'}, {}, {}, now)[1],'delivered')
        self.assertEqual(m.status_from({}, {}, {}, now)[1],'unknown')

    def test_ended_and_aborted_events(self):
        now=dt.datetime.now(m.TZ)
        self.assertEqual(m.status_from({}, {'kind':'task_complete'}, {}, now)[1],'ended')
        self.assertEqual(m.status_from({}, {'kind':'turn_aborted'}, {}, now)[1],'paused')

    def test_snapshot_preserves_input_bytes(self):
        sources=[p for p in self.root.rglob('*') if p.is_file()]
        before={p:hashlib.sha256(p.read_bytes()).digest() for p in sources}
        result=m.Collector(self.config).snapshot()
        after={p:hashlib.sha256(p.read_bytes()).digest() for p in sources}
        self.assertEqual(before,after)
        self.assertEqual(result['known_count'],1)
        all_rows=result['rows']+result['history']
        self.assertEqual({x['id'] for x in all_rows},{'sample-main','sample-child'})
        main=next(x for x in all_rows if x['id']=='sample-main')
        self.assertEqual(main['state'],'delivered')
        self.assertEqual(main['plan_entries'][0]['id'],'UI-01')

    def test_missing_plan_is_warning_not_completion(self):
        (self.root/'plan.json').unlink()
        result=m.Collector(self.config).snapshot()
        self.assertTrue(result['warnings'])
        self.assertEqual(result['rows'][0]['plan_entries'],[])

    def test_tail_only_exposes_public_assistant_messages(self):
        path=self.root/'filter.jsonl'
        rows=[{'type':'response_item','payload':{'type':'reasoning','text':'private-test-sentinel'}},
              {'type':'response_item','payload':{'type':'message','role':'user','phase':'commentary','content':[{'type':'text','text':'user-test-sentinel'}]}},
              {'type':'response_item','payload':{'type':'function_call_output','output':'tool-test-sentinel'}},
              {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'commentary','content':[{'type':'output_text','text':'public update'}]}}]
        path.write_text('\n'.join(json.dumps(x) for x in rows)+'\n',encoding='utf-8')
        tail=m.Tail(path,4096);tail.read();tail.read()
        self.assertEqual([x['text'] for x in tail.messages],['public update'])
        self.assertNotIn('sentinel',json.dumps(list(tail.messages)))

    def test_tail_appends_incrementally(self):
        path=self.root/'incremental.jsonl';path.write_text('',encoding='utf-8');tail=m.Tail(path,4096);tail.read()
        row={'type':'event_msg','timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),'payload':{'type':'task_complete'}}
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
        tail.read();self.assertEqual(tail.event['kind'],'task_complete')

    def test_incompatible_schema_warns_and_releases_database(self):
        with sqlite3.connect(self.root/'state.sqlite') as conn:
            conn.execute('DROP TABLE threads')
        conn.close()
        result=m.Collector(self.config).snapshot()
        self.assertTrue(result['warnings'])
        (self.root/'state.sqlite').unlink()

    def test_catalog_limit_stays_bounded(self):
        conn=sqlite3.connect(self.root/'state.sqlite')
        try:
            for i in range(130):
                conn.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',(f'child-{i}',str(self.root/'child.jsonl'),json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'sample-main'}}}),'/root/child',str(i),i,0))
            conn.commit()
        finally:conn.close()
        result=m.Collector(self.config).snapshot()
        self.assertEqual(len(result['rows'])+len(result['history']),120)
        self.assertTrue(result['warnings'])

    def test_cli_requires_explicit_config(self):
        x=subprocess.run([sys.executable,'-B',str(MODULE),'--once'],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(x.returncode,0)
        self.assertIn('--config',x.stderr)

    def test_cli_snapshot_and_clean_quit(self):
        x=subprocess.run([sys.executable,'-B','-X','utf8',str(MODULE),'--config',str(self.config)],input='snapshot\nquit\n',capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(x.returncode,0,x.stderr)
        self.assertEqual(len(x.stdout.splitlines()),1)
        self.assertEqual(json.loads(x.stdout)['known_count'],1)


if __name__ == '__main__':unittest.main()
