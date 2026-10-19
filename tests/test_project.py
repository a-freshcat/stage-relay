"""Git preflight diagnostics; never creates or modifies the user's repository."""
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
import test_relay as fixtures
from relaylib.runtime import require_clean_project
from relaylib.contracts import RelayError


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp=fixtures.WorkspaceTemp(prefix="project-check-")
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"project";self.path.mkdir()

    def git(self,*args):
        subprocess.run(["git","-C",str(self.path),*args],check=True,capture_output=True)

    def expect(self,code):
        with self.assertRaises(RelayError) as error:require_clean_project(self.path)
        self.assertEqual(error.exception.code,code)

    def test_deleted_directory_is_not_reported_as_missing_git(self):
        self.path.rmdir();self.expect("project_missing")

    def test_existing_non_repository_is_distinguished(self):
        with patch.dict(os.environ,{"GIT_CEILING_DIRECTORIES":str(self.path.parent)}):self.expect("git_required")

    def test_unborn_repository_needs_first_commit(self):
        self.git("init","-q");self.expect("git_no_commit")

    def test_clean_baseline_is_accepted_and_dirty_files_are_preserved(self):
        self.git("init","-q")
        (self.path/"README.md").write_text("baseline",encoding="utf-8")
        self.git("add","README.md")
        self.git("-c","user.name=Relay test","-c","user.email=relay@example.invalid","commit","-qm","baseline")
        self.assertTrue(require_clean_project(self.path)["head"])
        (self.path/"README.md").write_text("user changes",encoding="utf-8")
        self.expect("dirty_project")
        self.assertEqual((self.path/"README.md").read_text(),"user changes")
