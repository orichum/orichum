from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations.common.shell_profiles import ProfilePath, UnsafeProfile

ROOT = Path(__file__).resolve().parents[1]


class ShellProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = Path(temporary.name).resolve()
        self.user_home = self.fixture / "home"
        self.user_home.mkdir(mode=0o700)
        self.dotfiles = self.user_home / ".dotfiles"
        self.dotfiles.mkdir(mode=0o700)
        self.target = self.dotfiles / ".zshrc"
        self.original = b"# user configuration\nexport MY_SETTING=1\n"
        self.target.write_bytes(self.original)
        self.target.chmod(0o640)
        self.profile = self.user_home / ".zshrc"
        self.block = self.fixture / "block"

    def resolve(self) -> ProfilePath:
        return ProfilePath(self.profile, user_home=self.user_home)

    def shell(
        self, commands: str, *, mutation: str = ""
    ) -> subprocess.CompletedProcess:
        script = """
set -euo pipefail
source "$1/lib/workflow.sh"
source "$1/lib/uninstall.sh"
orichum_profile_block zsh "$HOME/.orichum/completions/zsh" "$2"
"""
        if mutation:
            # Exercise the real writer with a concurrent mutation at the claim.
            script += """
workflow_python() {
  shift 3
  python3 -c '
import os
import sys
code = sys.stdin.read()
marker = "    # Claim the path atomically before replacement.\\n"
assert marker in code
code = code.replace(marker, os.environ["PROFILE_TEST_MUTATION"] + marker, 1)
exec(compile(code, "<profile-race>", "exec"))
' "$@"
}
"""
        return subprocess.run(
            [
                "bash",
                "-c",
                script + commands,
                "profile-test",
                str(ROOT),
                str(self.block),
                str(self.profile),
            ],
            env={
                **os.environ,
                "HOME": str(self.user_home),
                "ORICHUM_INSTALL_BOOTSTRAP": "true",
                "PROFILE_TEST_MUTATION": mutation,
            },
            capture_output=True,
            text=True,
            check=True,
        )

    def test_absolute_relative_and_directory_link_chains(self) -> None:
        alias = self.user_home / "dotfiles-link"
        alias.symlink_to(".dotfiles", target_is_directory=True)
        for destination in (
            str(self.target),
            ".dotfiles/.zshrc",
            "dotfiles-link/.zshrc",
        ):
            with self.subTest(destination=destination):
                self.profile.symlink_to(destination)
                resolved = self.resolve()
                self.assertEqual(resolved.path, self.target)
                self.assertTrue(resolved.unchanged())
                self.profile.unlink()

    def test_install_verify_and_uninstall_keep_link_and_user_content(self) -> None:
        self.profile.symlink_to(".dotfiles/.zshrc")
        before = self.profile.lstat()
        result = self.shell("""
reconcile_orichum_profile_block "$3" "$2" zsh manual
reconcile_orichum_profile_block "$3" "$2" zsh manual
orichum_profile_block_matches "$3" "$2"
""")
        self.assertEqual(result.stderr, "")
        installed = self.target.read_bytes()
        self.assertTrue(installed.startswith(self.original))
        self.assertEqual(installed.count(b"# >>> Orichum completion >>>"), 1)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.profile.lstat().st_ino, before.st_ino)
        self.assertEqual(os.readlink(self.profile), ".dotfiles/.zshrc")
        result = self.shell('orichum_uninstall_remove_profile_block "$3" "$2"\n')
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.target.read_bytes(), self.original + b"\n")
        self.assertEqual(self.profile.lstat().st_ino, before.st_ino)

    def test_drifted_managed_block_is_retained(self) -> None:
        self.profile.symlink_to(self.target)
        self.shell('reconcile_orichum_profile_block "$3" "$2" zsh manual\n')
        edited = self.target.read_bytes().replace(
            b"fpath=(", b"fpath=(/my/completions "
        )
        self.target.write_bytes(edited)
        result = self.shell("""
reconcile_orichum_profile_block "$3" "$2" zsh manual
orichum_uninstall_remove_profile_block "$3" "$2"
if orichum_profile_block_matches "$3" "$2"; then exit 1; fi
""")
        self.assertIn("retained", result.stderr)
        self.assertEqual(self.target.read_bytes(), edited)
        self.assertTrue(self.profile.is_symlink())

    def test_broken_cyclic_directory_and_external_links_are_rejected(self) -> None:
        external = self.fixture / "external"
        external.write_bytes(b"untouched")
        for destination in ("missing", ".zshrc", ".dotfiles", str(external)):
            with self.subTest(destination=destination):
                self.profile.symlink_to(destination)
                with self.assertRaises(UnsafeProfile):
                    self.resolve()
                self.profile.unlink()
        self.assertEqual(external.read_bytes(), b"untouched")

    def test_shared_writable_target_and_directory_are_rejected(self) -> None:
        self.profile.symlink_to(self.target)
        for path in (self.target, self.dotfiles):
            with self.subTest(path=path):
                mode = path.stat().st_mode & 0o777
                path.chmod(mode | 0o020)
                with self.assertRaises(UnsafeProfile):
                    self.resolve()
                path.chmod(mode)

    def test_foreign_owned_link_is_rejected(self) -> None:
        self.profile.symlink_to(self.target)
        uid = os.getuid()
        with mock.patch(
            "integrations.common.shell_profiles.os.getuid", return_value=uid + 1
        ):
            with self.assertRaisesRegex(UnsafeProfile, "another user"):
                self.resolve()

    def test_link_retargeting_before_install_does_not_edit_either_target(self) -> None:
        self.profile.symlink_to(self.target)
        alternative = self.dotfiles / "other"
        alternative.write_bytes(b"alternative\n")
        mutation = (
            "    original_link = Path(sys.argv[1])\n"
            "    original_link.unlink()\n"
            '    original_link.symlink_to(".dotfiles/other")\n'
        )
        result = self.shell(
            'reconcile_orichum_profile_block "$3" "$2" zsh manual\n', mutation=mutation
        )
        self.assertIn("retained", result.stderr)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(alternative.read_bytes(), b"alternative\n")
        self.assertEqual(os.readlink(self.profile), ".dotfiles/other")

    def test_concurrent_target_edit_is_preserved_during_install_and_uninstall(
        self,
    ) -> None:
        self.profile.symlink_to(self.target)
        result = self.shell(
            'reconcile_orichum_profile_block "$3" "$2" zsh manual\n',
            mutation='    profile.write_bytes(payload + b"# concurrent edit\\n")\n',
        )
        self.assertIn("retained", result.stderr)
        self.assertEqual(
            self.target.read_bytes(), self.original + b"# concurrent edit\n"
        )
        self.shell('reconcile_orichum_profile_block "$3" "$2" zsh manual\n')
        installed = self.target.read_bytes()
        result = self.shell(
            'orichum_uninstall_remove_profile_block "$3" "$2"\n',
            mutation='    profile.write_bytes(payload + b"# later edit\\n")\n',
        )
        self.assertIn("retained", result.stderr)
        self.assertEqual(self.target.read_bytes(), installed + b"# later edit\n")
        self.assertTrue(self.profile.is_symlink())
