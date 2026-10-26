import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"skills/parallel-dev-coordination/scripts"))
from goal_guard import Guard, PAUSE, RESUME, sha


@contextmanager
def connect(path):
    c = sqlite3.connect(path)
    try:
        with c:
            yield c
    finally:
        c.close()


class GoalGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root/"goals_1.sqlite"
        with connect(self.db) as c:
            c.execute("CREATE TABLE thread_goals(thread_id TEXT,goal_id TEXT,status TEXT,objective TEXT)")
            c.execute("INSERT INTO thread_goals VALUES('t','g','active','original objective')")
        self.transcript = self.root/"rollout.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.now = 1000
        self.guard = Guard(self.root/"state", self.root, clock=lambda:self.now)

    def event(self, name, **kw):
        e = {"hook_event_name":name,"session_id":"t","turn_id":"turn-1",
             "transcript_path":str(self.transcript)}
        e.update(kw)
        return self.guard.handle(e)

    def pause_call(self, **kw):
        return self.event("PreToolUse",tool_name="update_goal",tool_input={"status":"paused"},tool_use_id="call-1",**kw)

    def assertDenied(self, r):
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"],"deny")

    def append(self, q, record_type="response_item"):
        with self.transcript.open("a",encoding="utf-8") as f:
            f.write(json.dumps({"type":record_type,"payload":q},ensure_ascii=False)+"\n")

    def test_old_pause_without_current_receipt_denied(self):
        self.assertDenied(self.pause_call())

    def test_fresh_pause_permitted_only_once(self):
        self.event("UserPromptSubmit",prompt="先暂停")
        self.append({"type":"message","role":"user","content":[{"text":"先暂停\n"}]})
        self.assertEqual(self.pause_call(),{})
        self.assertDenied(self.pause_call())

    def test_resume_supersedes_pause(self):
        self.event("UserPromptSubmit",prompt="先暂停")
        self.event("UserPromptSubmit",prompt="继续")
        self.assertDenied(self.pause_call())

    def test_automatic_goal_turn_does_not_inherit_pause(self):
        self.event("UserPromptSubmit",prompt="先暂停")
        self.assertDenied(self.pause_call(turn_id="automatic-next-turn"))

    def test_ui_activation_supersedes_pause_in_same_turn(self):
        self.event("UserPromptSubmit",prompt="先暂停")
        self.append({"type":"thread_goal_updated","threadId":"t","goal":{"status":"active"}},"event_msg")
        self.assertDenied(self.pause_call())

    def test_compaction_and_resume_revoke_pause(self):
        for source in ["compact","resume","startup"]:
            self.event("UserPromptSubmit",prompt="暂停")
            self.event("SessionStart",source=source)
            self.assertDenied(self.pause_call())

    def test_interruption_revokes_pending_pause(self):
        self.event("UserPromptSubmit",prompt="暂停")
        self.event("Interrupt")
        self.assertDenied(self.pause_call())

    def test_changed_goal_does_not_reuse_authority(self):
        self.event("UserPromptSubmit",prompt="暂停")
        with connect(self.db) as c:c.execute("UPDATE thread_goals SET goal_id='new'")
        self.assertDenied(self.pause_call())

    def test_budget_limited_is_not_resumed_or_overridden(self):
        with connect(self.db) as c:c.execute("UPDATE thread_goals SET status='budgetLimited'")
        r=self.event("UserPromptSubmit",prompt="继续")
        self.assertIn("budgetLimited",r["hookSpecificOutput"]["additionalContext"])
        with connect(self.db) as c:self.assertEqual(c.execute("SELECT status FROM thread_goals").fetchone()[0],"budgetLimited")

    def test_resume_reports_goal_still_paused(self):
        with connect(self.db) as c:c.execute("UPDATE thread_goals SET status='paused'")
        r=self.event("UserPromptSubmit",prompt="继续")
        self.assertIn("paused",r["hookSpecificOutput"]["additionalContext"])

    def test_readback_after_pause(self):
        self.event("UserPromptSubmit",prompt="暂停")
        self.pause_call()
        with connect(self.db) as c:c.execute("UPDATE thread_goals SET status='paused'")
        self.event("PostToolUse",tool_name="update_goal",tool_input={"status":"paused"},tool_use_id="call-1")
        state=json.loads((self.root/'state/t.json').read_text())
        self.assertEqual(state['pauseReadback'],'paused')

    def test_restart_does_not_reset_consumed_authority(self):
        self.event("UserPromptSubmit",prompt="暂停");self.pause_call()
        self.guard=Guard(self.root/"state",self.root,clock=lambda:self.now)
        self.assertDenied(self.pause_call())

    def test_expiry_and_large_history_fail_closed(self):
        self.event("UserPromptSubmit",prompt="暂停");self.now+=181
        self.assertDenied(self.pause_call())
        self.event("UserPromptSubmit",prompt="暂停")
        self.transcript.write_bytes(b' '*(4*1024*1024+1))
        self.assertDenied(self.pause_call())

    def test_no_prompt_text_or_objective_stored(self):
        self.event("UserPromptSubmit",prompt="private message")
        raw=(self.root/'state/t.json').read_text()
        self.assertNotIn('private message',raw)
        self.assertNotIn('original objective',raw)

    def test_unrelated_goal_operations_unchanged(self):
        for tool,args in [('get_goal',{}),('update_goal',{'status':'complete'}),('update_goal',{'status':'blocked'})]:
            self.assertEqual(self.event('PreToolUse',tool_name=tool,tool_input=args),{})

    def test_quoted_pause_is_not_authority(self):
        for prompt in ['分析“先暂停”是什么意思','以前说过先暂停，现在继续','<codex_internal_context>先暂停</codex_internal_context>']:
            self.event('UserPromptSubmit',prompt=prompt)
            self.assertDenied(self.pause_call())

    def test_other_user_input_supersedes_unconsumed_pause(self):
        self.event('UserPromptSubmit',prompt='暂停')
        self.append({'type':'message','role':'user','content':[{'text':'继续'}]})
        self.assertDenied(self.pause_call())

    def test_does_not_write_codex_goal_database(self):
        before=sha(self.db.read_bytes())
        self.event('UserPromptSubmit',prompt='暂停');self.pause_call();self.event('Interrupt')
        self.assertEqual(before,sha(self.db.read_bytes()))

    def test_non_target_session_is_untouched(self):
        self.guard.targets=frozenset(['different-thread'])
        self.assertEqual(self.pause_call(),{})
        self.assertFalse((self.root/'state').exists())

    def test_repeated_identical_user_message_is_new_control(self):
        self.event('UserPromptSubmit',prompt='暂停')
        for _ in range(2):
            self.append({'type':'message','role':'user','content':[{'text':'暂停'}]})
        self.assertDenied(self.pause_call())

    def test_unrelated_tools_do_not_read_database(self):
        self.db.unlink()
        self.assertEqual(self.event('PreToolUse',tool_name='exec',tool_input={}),{})

    def test_child_and_side_transcripts_are_untouched(self):
        self.guard.transcript=self.transcript.resolve()
        self.assertEqual(self.pause_call(transcript_path=str(self.root/'child.jsonl')),{})
        self.assertEqual(self.pause_call(agent_id='child'),{})
        self.assertFalse((self.root/'state').exists())

    @unittest.skipUnless(os.name=='nt','Windows long path prefix')
    def test_native_extended_transcript_path_matches_parent(self):
        self.guard.transcript=self.transcript.resolve()
        self.assertDenied(self.pause_call(transcript_path='\\\\?\\'+str(self.transcript)))

    def run_cli(self, event, code_hash=None):
        script=Path(__file__).resolve().parents[1]/'skills/parallel-dev-coordination/scripts/goal_guard.py'
        proc=subprocess.run([sys.executable,'-X','utf8','-B',str(script),
             '--state-dir',str(self.root/'state'),'--codex-home',str(self.root),
             '--thread-id','t','--transcript-path',str(self.transcript),
             '--code-sha256',code_hash or sha(script.read_bytes())],
             input=json.dumps(event),text=True,encoding='utf-8',capture_output=True,timeout=5)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return json.loads(proc.stdout)

    def test_cli_integrity_mismatch_returns_valid_pause_denial(self):
        e={'hook_event_name':'PreToolUse','session_id':'t','turn_id':'turn-1',
           'transcript_path':str(self.transcript),'tool_name':'update_goal','tool_input':{'status':'paused'}}
        self.assertDenied(self.run_cli(e,'0'*64))

    def test_cli_corrupt_state_cannot_grant_pause(self):
        (self.root/'state').mkdir();(self.root/'state/t.json').write_text('broken')
        e={'hook_event_name':'PreToolUse','session_id':'t','turn_id':'turn-1',
           'transcript_path':str(self.transcript),'tool_name':'update_goal','tool_input':{'status':'paused'}}
        self.assertDenied(self.run_cli(e))

    def test_cli_ignores_other_session_even_on_integrity_error(self):
        e={'hook_event_name':'PreToolUse','session_id':'unrelated','tool_name':'update_goal','tool_input':{'status':'paused'}}
        self.assertEqual(self.run_cli(e,'0'*64),{})


if __name__=='__main__':unittest.main()
