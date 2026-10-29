"""Model-mode tests exercise mock provider responses and safe local tools only."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from assistant import NonAutonomousAssistant
from model_modes import MAX_CONTEXT_CHARS, ModelModes, build_local_diff
from tools import ToolError, check_patch

OLD='if value >= upper:  # deliberate off-by-one bug\n        return upper - 1'
NEW='if value > upper:\n        return upper'


class FakeProvider:
    def __init__(self,text): self.text=text; self.calls=[]
    def complete(self,messages,*,max_tokens):
        self.calls.append((messages,max_tokens))
        return {"text":self.text,"model":"mock","input_tokens":50,"output_tokens":10,
                "estimated_cost_usd":.00001,"elapsed_seconds":.001}


def suggestion(path='tinycalc.py',old_text=OLD,new_text=NEW,reason='Fix the inclusive upper bound.'):
    return json.dumps({"path":path,"old_text":old_text,"new_text":new_text,"reason":reason})


def make_test_repo(root):
    files={
        'README.md':'Tiny clamp fixture; upper bounds are inclusive.\n',
        'tinycalc.py':'def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError("lower must not exceed upper")\n    if value < lower:\n        return lower\n    if value >= upper:  # deliberate off-by-one bug\n        return upper - 1\n    return value\n',
        'test_tinycalc.py':'import unittest\nfrom tinycalc import clamp\n\nclass ClampTests(unittest.TestCase):\n    def test_upper_bound_is_inclusive(self):\n        self.assertEqual(clamp(5, 0, 5), 5)\n\nif __name__ == "__main__": unittest.main()\n',
    }
    subprocess.run(['/usr/bin/git','init','-q'],cwd=root,check=True)
    for name,content in files.items(): (root/name).write_text(content)
    subprocess.run(['/usr/bin/git','add','README.md','tinycalc.py','test_tinycalc.py'],cwd=root,check=True)
    subprocess.run(['/usr/bin/git','-c','user.name=GuardedCoder Tests','-c','user.email=tests@example.invalid',
                    'commit','-qm','disposable test baseline'],cwd=root,check=True)


class ModelModeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='guardedcoder-model-test-')
        self.repo=Path(self.temp.name).resolve()
        make_test_repo(self.repo)
        self.app=NonAutonomousAssistant(self.repo)
        self.baseline=(self.repo/'tinycalc.py').read_bytes()
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')

    def tearDown(self):
        self.assertEqual((self.repo/'tinycalc.py').read_bytes(),self.baseline)
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')
        self.temp.cleanup()

    def test_question_uses_bounded_read_only_context(self):
        provider=FakeProvider('The upper-bound branch returns upper - 1; it should return upper.')
        result=ModelModes(self.app,provider).question('Why does the upper-bound clamp test fail?')
        self.assertIn('upper - 1',result['answer'])
        self.assertIn('tinycalc.py',result['files_read'])
        self.assertLessEqual(len(provider.calls[0][0][1]['content']),MAX_CONTEXT_CHARS+1200)
        self.assertTrue(all(c['tool'] in {'list_files','search_text','read_file'} for c in result['tool_calls']))
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(result['usage']['input_tokens'],50)

    def test_structured_suggestion_generates_valid_diff_without_writing(self):
        provider=FakeProvider(suggestion())
        modes=ModelModes(self.app,provider)
        result=modes.suggest_fix('Propose the smallest fix for the failing upper-bound test.')
        diff=result['proposal']['diff']
        self.assertIn('diff --git a/tinycalc.py b/tinycalc.py',diff)
        self.assertIn('-        return upper - 1',diff)
        self.assertIn('+        return upper',diff)
        self.assertEqual(diff,build_local_diff(self.repo,{"path":"tinycalc.py","old_text":OLD,
            "new_text":NEW,"reason":"Fix the inclusive upper bound."}))
        check_patch(self.repo,diff)
        self.assertEqual(result['reason'],'Fix the inclusive upper bound.')
        self.assertEqual((self.repo/'tinycalc.py').read_bytes(),self.baseline)
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')
        with self.assertRaises(ToolError): modes.apply(result['proposal']['approval_token'],approved=True)

    def test_old_text_not_found_is_rejected(self):
        raw=suggestion(old_text='this exact text is absent')
        with self.assertRaisesRegex(ToolError,'not found'):
            ModelModes(self.app,FakeProvider(raw)).suggest_fix('Try a nonexistent replacement.')

    def test_old_text_occurring_multiple_times_is_rejected(self):
        raw=suggestion(old_text='return')
        with self.assertRaisesRegex(ToolError,'exactly once'):
            ModelModes(self.app,FakeProvider(raw)).suggest_fix('Replace a repeated token.')

    def test_malformed_json_and_raw_diff_are_rejected(self):
        for raw in ('{not json', 'diff --git a/tinycalc.py b/tinycalc.py\n',
                    '{"path":"a","path":"b","old_text":"x","new_text":"y"}'):
            with self.subTest(raw=raw), self.assertRaises(ToolError):
                ModelModes(self.app,FakeProvider(raw)).suggest_fix('Propose a fix.')

    def test_sensitive_traversal_and_symlink_targets_are_rejected(self):
        for target in ('.env', '../outside.py', '.git/config'):
            with self.subTest(target=target), self.assertRaises(ToolError):
                ModelModes(self.app,FakeProvider(suggestion(path=target))).suggest_fix('Edit a file.')
        link=self.repo/'_structured_suggest_link'
        try:
            link.symlink_to(self.repo/'tinycalc.py')
            with self.assertRaisesRegex(ToolError,'symlink'):
                ModelModes(self.app,FakeProvider(suggestion(path=link.name))).suggest_fix('Edit a linked file.')
        finally:
            if link.is_symlink(): link.unlink()

    def test_binary_target_is_rejected(self):
        target=self.repo/'_structured_suggest_binary.bin'
        try:
            target.write_bytes(b'header\x00payload')
            raw=suggestion(path=target.name,old_text='header',new_text='changed')
            with self.assertRaisesRegex(ToolError,'binary'):
                ModelModes(self.app,FakeProvider(raw)).suggest_fix('Edit the temporary target.')
        finally:
            if target.exists(): target.unlink()

    def test_model_can_decline_without_producing_a_patch(self):
        result=ModelModes(self.app,FakeProvider('null')).suggest_fix('Make a change.')
        self.assertIsNone(result['proposal'])
        self.assertIn('declined',result['reason'])
        self.assertEqual((self.repo/'tinycalc.py').read_bytes(),self.baseline)

    def test_local_diff_preserves_newline_and_eof_semantics(self):
        cases=(
            ('old_text_without_newline','alpha old omega\n','old','new','alpha new omega\n'),
            ('new_text_without_newline_at_eof','before\nold\n','old\n','new','before\nnew'),
            ('replace_eof_line_without_original_newline','before\nold','old','new','before\nnew'),
            ('preserve_original_missing_eof_newline','first\nold\nlast','old\n','new\n','first\nnew\nlast'),
        )
        for label, original, old_text, new_text, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(prefix='guardedcoder-eof-test-') as temp:
                root=Path(temp).resolve()
                subprocess.run(['/usr/bin/git','init','-q'],cwd=root,check=True)
                target=root/'case.txt'
                target.write_bytes(original.encode('utf-8'))
                patch=build_local_diff(root,{'path':'case.txt','old_text':old_text,'new_text':new_text})
                check_patch(root,patch)
                self.assertEqual(target.read_bytes(),original.encode('utf-8'))
                subprocess.run(['/usr/bin/git','apply','--whitespace=error','-'],cwd=root,
                               input=patch,text=True,check=True)
                self.assertEqual(target.read_bytes(),expected.encode('utf-8'))


if __name__=='__main__': unittest.main(verbosity=2)
