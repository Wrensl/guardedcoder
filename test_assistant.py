"""Approval-gate tests use isolated temporary Git repositories."""
import difflib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from assistant import NonAutonomousAssistant
from tools import ToolError


def make_patch(path, old, new):
    body=''.join(difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),
        fromfile='a/'+path,tofile='b/'+path,n=3))
    return f'diff --git a/{path} b/{path}\n'+body


def make_test_repo(root):
    files={
        'README.md':'A tiny Python repository for testing guarded edits.\nExpected: clamp uses inclusive bounds.\n',
        'tinycalc.py':'def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError("lower must not exceed upper")\n    if value < lower:\n        return lower\n    if value >= upper:  # deliberate off-by-one bug\n        return upper - 1\n    return value\n',
        'test_tinycalc.py':'import unittest\nfrom tinycalc import clamp\n\nclass ClampTests(unittest.TestCase):\n    def test_upper_bound_is_inclusive(self):\n        self.assertEqual(clamp(5, 0, 5), 5)\n\nif __name__ == "__main__": unittest.main()\n',
    }
    subprocess.run(['/usr/bin/git','init','-q'],cwd=root,check=True)
    for name,content in files.items(): (root/name).write_text(content)
    subprocess.run(['/usr/bin/git','add','README.md','tinycalc.py','test_tinycalc.py'],cwd=root,check=True)
    subprocess.run(['/usr/bin/git','-c','user.name=GuardedCoder Tests','-c','user.email=tests@example.invalid',
                    'commit','-qm','disposable test baseline'],cwd=root,check=True)


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='guardedcoder-assistant-test-')
        self.repo=Path(self.temp.name).resolve()
        make_test_repo(self.repo)
        self.app=NonAutonomousAssistant(self.repo)
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')
        self.original=(self.repo/'README.md').read_bytes()

    def tearDown(self):
        (self.repo/'README.md').write_bytes(self.original)
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')
        self.temp.cleanup()

    def test_question_cannot_write(self):
        self.assertIn('inclusive bounds',self.app.question('read_file',{'path':'README.md'})['output'])
        for tool in ('apply_patch','git_status','run_tests','shell'):
            with self.subTest(tool=tool), self.assertRaises(ToolError):self.app.question(tool,{})
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')

    def test_suggest_validates_and_never_writes(self):
        old=self.original.decode();new=old.replace('A tiny Python repository','A tiny Python repository (suggestion)',1)
        patch=make_patch('README.md',old,new)
        proposed=self.app.suggest_fix(patch)
        self.assertIn('README.md',proposed['files']);self.assertEqual(proposed['diff'],patch)
        self.assertEqual((self.repo/'README.md').read_bytes(),self.original)
        self.assertEqual(subprocess.check_output(['/usr/bin/git','status','--porcelain'],cwd=self.repo,text=True),'')

    def test_escape_symlink_forbidden_and_oversized_patches_rejected(self):
        old=self.original.decode()
        escaping=make_patch('../outside.txt','old\n','new\n')
        with self.assertRaises(ToolError):self.app.suggest_fix(escaping)
        link=self.repo/'_assistant_symlink_test'
        try:
            link.symlink_to('/etc/hosts')
            with self.assertRaises(ToolError):self.app.suggest_fix(make_patch(link.name,'a','b'))
        finally:
            if link.is_symlink():link.unlink()
        forbidden=make_patch('.env','A=1\n','A=2\n')
        with self.assertRaises(ToolError):self.app.suggest_fix(forbidden)
        with self.assertRaises(ToolError):self.app.suggest_fix('x'* (32*1024+1))
        self.assertEqual((self.repo/'README.md').read_bytes(),self.original)

    def test_unapproved_patch_never_applies_then_approved_patch_applies(self):
        old=self.original.decode();new=old.replace('A tiny Python repository','A tiny Python repository (approved)',1)
        proposed=self.app.suggest_fix(make_patch('README.md',old,new));token=proposed['approval_token']
        with self.assertRaises(ToolError):self.app.apply_approved(token,approved=False)
        self.assertEqual((self.repo/'README.md').read_bytes(),self.original)
        result=self.app.apply_approved(token,approved=True)
        self.assertIn('(approved)',(self.repo/'README.md').read_text())
        self.assertEqual(result['applied'],{'applied':True})
        self.assertNotEqual(result['tests']['returncode'],0)
        self.assertIn('test_upper_bound_is_inclusive',result['tests']['output'])
        self.assertIn('README.md',result['diff']['output'])

    def test_single_use_approval_token(self):
        old=self.original.decode();patch=make_patch('README.md',old,old+'\nreview\n')
        token=self.app.suggest_fix(patch)['approval_token']
        self.app.apply_approved(token,approved=True)
        with self.assertRaises(ToolError):self.app.apply_approved(token,approved=True)


if __name__=='__main__':unittest.main(verbosity=2)
