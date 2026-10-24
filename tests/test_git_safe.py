"""Integration tests for git-safe's blame and formatter behavior."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import git_safe


class GitSafeTests(unittest.TestCase):
    """Exercise line selection and the command-line interface."""

    def test_only_hunks_overlapping_owned_lines_are_selected(self):
        """Keep formatter hunks near an author-owned range."""
        original = [f"line {number}\n" for number in range(12)]
        formatted = list(original)
        formatted[0] = "LINE 0\n"
        formatted[11] = "LINE 11\n"
        owners = ["alice@example.com"] + ["bob@example.com"] * 11

        result = git_safe.selected_formatted_lines(
            original, formatted, owners, "alice@example.com"
        )

        self.assertEqual(result[0], "LINE 0\n")
        self.assertEqual(result[8:], original[8:])

    def test_hunk_may_include_neighboring_unowned_lines(self):
        """Accept neighboring unowned lines in an overlapping hunk."""
        original = ["old\n", "neighbor\n", "tail\n"]
        formatted = ["new\n", "NEIGHBOR\n", "tail\n"]
        owners = ["alice@example.com", "bob@example.com", "bob@example.com"]

        result = git_safe.selected_formatted_lines(
            original, formatted, owners, "alice@example.com"
        )

        self.assertEqual(result, formatted)

    def test_resolves_unique_display_name_and_rejects_ambiguous_name(self):
        """Resolve emails and reject names associated with multiple emails."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(Path(directory) / "repo")
            self.commit(root, "one.txt", "one\n", "Alice", "alice@example.com")
            self.commit(root, "two.txt", "two\n", "Alice", "other@example.com")

            with self.assertRaisesRegex(git_safe.GitSafeError, "ambiguous"):
                git_safe.resolve_author(root, "Alice")
            self.assertEqual(
                git_safe.resolve_author(root, "alice@example.com"), "alice@example.com"
            )

    def test_author_blame_is_parsed_for_each_source_line(self):
        """Return ownership for each line from Git blame."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(Path(directory) / "repo")
            self.commit(
                root,
                "sample.txt",
                "alice line\nbob line\n",
                "Alice",
                "alice@example.com",
            )
            self.commit(
                root,
                "sample.txt",
                "alice line\nbob changed\n",
                "Bob",
                "bob@example.com",
            )

            self.assertEqual(
                git_safe.blame_emails(root, "sample.txt"),
                ["alice@example.com", "bob@example.com"],
            )

    def test_cli_dry_run_and_apply_only_selected_hunks(self):
        """Preview by default and apply only the selected formatter hunk."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(Path(directory) / "repo")
            contents = "alice edit\n" + "".join(f"bob line {i}\n" for i in range(10))
            self.commit(root, "sample.txt", contents, "Bob", "bob@example.com")
            self.commit(
                root,
                "sample.txt",
                "alice change\n" + contents.split("\n", 1)[1],
                "Alice",
                "alice@example.com",
            )

            formatter = Path(directory) / "uppercase.py"
            formatter_script = (
                "from pathlib import Path\n"
                "import sys\n"
                "path = Path(sys.argv[1])\n"
                "text = path.read_text()\n"
                "path.write_text(\n"
                "    text.replace('alice change', 'ALICE CHANGE')"
                ".replace('bob line 9', 'BOB LINE 9')\n"
                ")\n"
            )
            formatter.write_text(
                formatter_script,
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(Path(git_safe.__file__).resolve()),
                "format",
                "--author",
                "alice@example.com",
                "--",
                sys.executable,
                str(formatter),
            ]
            dry_run = subprocess.run(
                command, cwd=root, text=True, capture_output=True, check=False
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
            self.assertIn("-alice change", dry_run.stdout)
            self.assertNotIn("-bob line 9", dry_run.stdout)
            self.assertIn("Dry run", dry_run.stderr)
            self.assertEqual(
                (root / "sample.txt").read_text(),
                "alice change\n" + contents.split("\n", 1)[1],
            )

            apply_result = subprocess.run(
                command[:5] + ["--apply"] + command[5:],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(apply_result.returncode, 0, apply_result.stderr)
            self.assertEqual(
                (root / "sample.txt").read_text(),
                "ALICE CHANGE\n" + contents.split("\n", 1)[1],
            )

    def test_dirty_author_file_is_refused(self):
        """Refuse formatting when an author-owned file has local changes."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(Path(directory) / "repo")
            self.commit(
                root, "sample.txt", "alice line\n", "Alice", "alice@example.com"
            )
            (root / "new.txt").write_text("new file\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "new.txt"], check=True)
            self.assertEqual(git_safe.list_tracked_files(root), ["sample.txt"])
            (root / "sample.txt").write_text("uncommitted\n", encoding="utf-8")

            self.assertTrue(git_safe.is_dirty(root, "sample.txt"))
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(git_safe.__file__).resolve()),
                    "format",
                    "--author",
                    "alice@example.com",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("uncommitted changes", result.stderr)

    def test_wrapper_is_created_in_git_directory_for_resolved_email(self):
        """Create an executable wrapper with the author's resolved email."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(Path(directory) / "repo")
            self.commit(root, "sample.txt", "line\n", "Alice", "alice@example.com")
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(git_safe.__file__).resolve()),
                    "wrapper",
                    "--author",
                    "Alice",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            wrapper = Path(result.stdout.strip())
            self.assertTrue(wrapper.is_file())
            self.assertTrue(os.access(wrapper, os.X_OK))
            self.assertIn(
                "--author alice@example.com", wrapper.read_text(encoding="utf-8")
            )

    def test_binary_file_is_reported_and_skipped(self):
        """Report and skip files Git cannot blame because they are binary."""
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repo(Path(directory) / "repo")
            (root / "image.bin").write_bytes(b"\x00\x01\x02")
            subprocess.run(["git", "-C", str(root), "add", "image.bin"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Alice",
                    "-c",
                    "user.email=alice@example.com",
                    "commit",
                    "-qm",
                    "binary",
                ],
                check=True,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(git_safe.__file__).resolve()),
                    "format",
                    "--author",
                    "alice@example.com",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Skipping binary file", result.stderr)

    def make_repo(self, root):
        """Initialize an empty test repository.

        Args:
            root: Path at which to create the repository.

        Returns:
            The repository path.
        """
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        return root

    def commit(self, root, name, content, author_name, author_email):
        """Write and commit a file under the supplied author identity.

        Args:
            root: Repository directory.
            name: File name relative to the repository root.
            content: Text to commit.
            author_name: Git author display name.
            author_email: Git author email.
        """
        (root / name).write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", name], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                f"user.name={author_name}",
                "-c",
                f"user.email={author_email}",
                "commit",
                "-qm",
                "test commit",
            ],
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
