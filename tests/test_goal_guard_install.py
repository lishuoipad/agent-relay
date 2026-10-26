from pathlib import Path
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills/parallel-dev-coordination/scripts'))
from install_goal_guard import build_handlers, install, merge, safe_path


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.helper=self.root/'goal_guard.py';self.helper.write_text('# test')
        self.transcript=self.root/'rollout.jsonl';self.transcript.write_text('')
        self.groups=build_handlers(sys.executable,self.helper,self.root,self.root/'state','t',self.transcript)

    def test_merge_preserves_unrelated_handlers_and_metadata(self):
        old={'description':'keep','hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'original'}]}]}}
        result=merge(old,self.groups)
        self.assertEqual(result['description'],'keep')
        self.assertEqual(result['hooks']['SessionStart'][0],old['hooks']['SessionStart'][0])
        self.assertEqual(len(old['hooks']['SessionStart']),1)

    def test_install_is_idempotent_and_backs_up_exact_bytes(self):
        raw=b'{"description": "original", "hooks": {}}\n'
        (self.root/'hooks.json').write_bytes(raw)
        first=install(self.root,self.root/'backups',self.groups)
        self.assertEqual(Path(first['backup']).read_bytes(),raw)
        second=install(self.root,self.root/'backups',self.groups)
        self.assertFalse(second['changed'])
        self.assertFalse((self.root/'config.toml').exists())
        self.assertNotIn('trusted_hash',(self.root/'hooks.json').read_text())

    def test_code_change_replaces_only_own_target_definition(self):
        other=build_handlers(sys.executable,self.helper,self.root,self.root/'state','other',self.transcript)
        old=merge(merge({},other),self.groups)
        self.helper.write_text('# changed')
        updated=build_handlers(sys.executable,self.helper,self.root,self.root/'state','t',self.transcript)
        result=merge(old,updated)
        self.assertEqual(len(result['hooks']['PreToolUse']),2)
        self.assertEqual(result['hooks']['PreToolUse'][0],other['PreToolUse'][0])
        self.assertEqual(result['hooks']['PreToolUse'][1],updated['PreToolUse'][0])

    def test_unsafe_command_paths_rejected(self):
        for path in ['bad path','bad&command','bad;command','bad$(command)']:
            with self.assertRaises(ValueError):safe_path(self.root/path)

    def test_invalid_existing_json_is_not_overwritten(self):
        p=self.root/'hooks.json';p.write_bytes(b'broken')
        with self.assertRaises(ValueError):install(self.root,self.root/'backup',self.groups)
        self.assertEqual(p.read_bytes(),b'broken')

    @unittest.skipUnless(os.name=='nt','Windows long path prefix')
    def test_native_windows_extended_path_normalizes(self):
        self.assertEqual(safe_path('\\\\?\\'+str(self.helper)),safe_path(self.helper))


if __name__=='__main__':unittest.main()
