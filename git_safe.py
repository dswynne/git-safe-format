#!/usr/bin/env python3
"""Apply formatter changes only to lines attributed to a selected Git author."""

import argparse
import difflib
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile


class GitSafeError(Exception):
    """Raised when git-safe cannot safely complete an operation."""


def run_git(root, *args, check=True):
    """Run a Git command in the specified repository.

    Args:
        root: Repository directory.
        *args: Arguments passed to Git.
        check: Whether to raise an error for a nonzero exit status.

    Returns:
        The completed subprocess result.

    Raises:
        GitSafeError: If the command fails and ``check`` is true.
    """
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise GitSafeError(message or f"git command failed: {' '.join(args)}")
    return result


def repository_root():
    """Return the absolute root directory of the current Git repository.

    Raises:
        GitSafeError: If the current directory is not in a Git repository.
    """
    result = run_git(Path.cwd(), "rev-parse", "--show-toplevel")
    return Path(os.fsdecode(result.stdout.rstrip(b"\n"))).resolve()


def list_tracked_files(root):
    """List regular files tracked in ``HEAD``.

    Args:
        root: Repository directory.

    Returns:
        Repository-relative paths for regular files in ``HEAD``.

    Raises:
        GitSafeError: If Git fails or its tree output cannot be parsed.
    """
    result = run_git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    paths = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        if not separator:
            raise GitSafeError("Could not parse git ls-tree output")
        mode = metadata.split(b" ", 1)[0]
        if mode not in (b"100644", b"100755"):
            print(f"Skipping non-regular file {os.fsdecode(path)!r}", file=sys.stderr)
            continue
        paths.append(os.fsdecode(path))
    return paths


def author_records(root):
    """Return distinct author names and emails found in ``HEAD`` history.

    Args:
        root: Repository directory.

    Returns:
        A set of ``(name, email)`` tuples.
    """
    result = run_git(root, "log", "--format=%aN%x00%aE", "HEAD")
    records = set()
    for record in result.stdout.decode(errors="surrogateescape").splitlines():
        name, separator, email = record.partition("\0")
        if separator:
            records.add((name, email))
    return records


def resolve_author(root, value):
    """Resolve an author's email, using an unambiguous name as a convenience.

    Args:
        root: Repository directory.
        value: Author email or exact display name.

    Returns:
        The selected author email.

    Raises:
        GitSafeError: If the name is absent or maps to multiple emails.
    """
    records = author_records(root)
    emails = {email for _, email in records}
    if value in emails or "@" in value:
        return value

    matches = {email for name, email in records if name == value}
    if len(matches) == 1:
        return matches.pop()
    if len(matches) > 1:
        choices = ", ".join(sorted(matches))
        raise GitSafeError(
            f"Author name {value!r} is ambiguous; use an email: {choices}"
        )
    raise GitSafeError(f"No Git author named {value!r} was found in HEAD history")


def blame_emails(root, path):
    """Return the author email responsible for each line in a ``HEAD`` file.

    Args:
        root: Repository directory.
        path: Repository-relative file path.

    Returns:
        A list of author emails, one per source line.

    Raises:
        GitSafeError: If blame fails, encounters binary data, or is malformed.
    """
    result = run_git(root, "blame", "--line-porcelain", "-e", "HEAD", "--", path)
    if b"\0" in result.stdout:
        raise GitSafeError(f"git blame cannot process binary file {path!r}")
    owners = []
    current_email = None
    for line in result.stdout.splitlines():
        if line.startswith(b"author-mail "):
            current_email = (
                line[len(b"author-mail ") :]
                .strip()
                .strip(b"<>")
                .decode(errors="surrogateescape")
            )
        elif line.startswith(b"\t"):
            if current_email is None:
                raise GitSafeError(f"Could not parse git blame output for {path!r}")
            owners.append(current_email)
    return owners


def is_dirty(root, path):
    """Check whether a file differs from ``HEAD`` in the working tree or index.

    Args:
        root: Repository directory.
        path: Repository-relative file path.

    Returns:
        True if the file has uncommitted changes; otherwise, False.

    Raises:
        GitSafeError: If Git cannot determine the file's status.
    """
    result = run_git(root, "diff", "--quiet", "HEAD", "--", path, check=False)
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    message = result.stderr.decode(errors="replace").strip()
    raise GitSafeError(message or f"Could not check working tree status for {path!r}")


def hunk_touches_ranges(start, end, ranges):
    """Return whether a diff source range intersects an owned line range.

    Args:
        start: Zero-based, inclusive start line of the diff range.
        end: Zero-based, exclusive end line of the diff range.
        ranges: Zero-based, half-open ranges of author-owned lines.

    Returns:
        True when the diff range overlaps an owned range.
    """
    if start == end:
        return any(
            block_start <= start <= block_end for block_start, block_end in ranges
        )
    return any(
        start < block_end and block_start < end for block_start, block_end in ranges
    )


def owned_line_ranges(owners, author_email):
    """Find contiguous line ranges owned by the selected author.

    Args:
        owners: Author email for each original source line.
        author_email: Email whose line blocks may be formatted.

    Returns:
        Zero-based, half-open ranges for the author's contiguous line blocks.
    """
    ranges = []
    run_start = None
    for index, owner in enumerate(owners):
        if owner == author_email and run_start is None:
            run_start = index
        elif owner != author_email and run_start is not None:
            ranges.append((run_start, index))
            run_start = None
    if run_start is not None:
        ranges.append((run_start, len(owners)))
    return ranges


def selected_hunks(original, formatted, ranges):
    """Return formatter hunks intersecting any selected source range.

    Args:
        original: Original source lines.
        formatted: Formatter output lines.
        ranges: Zero-based, half-open source line ranges.

    Returns:
        Selected hunks as source and formatter slice boundaries.
    """
    matcher = difflib.SequenceMatcher(a=original, b=formatted, autojunk=False)
    selected = []
    for group in matcher.get_grouped_opcodes(3):
        source_start = group[0][1]
        source_end = group[-1][2]
        if hunk_touches_ranges(source_start, source_end, ranges):
            selected.append((group[0][1], group[-1][2], group[0][3], group[-1][4]))
    return selected


def selected_formatted_lines(original, formatted, owners, author_email):
    """Select formatter hunks that overlap contiguous author-owned blocks.

    Args:
        original: Original source lines, including line endings.
        formatted: Formatter output lines, including line endings.
        owners: Author email for each original source line.
        author_email: Email whose line blocks may be formatted.

    Returns:
        Source lines with selected formatter hunks applied.
    """
    ranges = owned_line_ranges(owners, author_email)
    selected = selected_hunks(original, formatted, ranges)
    result = list(original)
    for source_start, source_end, formatted_start, formatted_end in reversed(selected):
        result[source_start:source_end] = formatted[formatted_start:formatted_end]
    return result


def format_file(root, path, formatter):
    """Run an in-place formatter on a temporary copy of a tracked file.

    The formatter command may contain ``{file}``; otherwise the temporary file
    path is appended to the command.

    Args:
        root: Repository directory.
        path: Repository-relative file path.
        formatter: Formatter executable and arguments.

    Returns:
        A pair containing original and formatted lines.

    Raises:
        GitSafeError: If the file, formatter, or formatter output is invalid.
    """
    file_path = root / path
    try:
        original_bytes = file_path.read_bytes()
    except OSError as error:
        raise GitSafeError(f"Could not read {path!r}: {error}") from error
    if b"\0" in original_bytes:
        raise GitSafeError(f"Refusing to format binary file {path!r}")
    try:
        original_text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitSafeError(f"File {path!r} is not UTF-8 text") from error

    suffix = file_path.suffix
    try:
        with tempfile.TemporaryDirectory(prefix="git-safe-") as temporary_directory:
            temporary_file = Path(temporary_directory) / f"source{suffix}"
            temporary_file.write_bytes(original_bytes)
            command = [
                argument.replace("{file}", str(temporary_file))
                for argument in formatter
            ]
            if not any("{file}" in argument for argument in formatter):
                command.append(str(temporary_file))
            try:
                result = subprocess.run(command, cwd=root, check=False)
            except OSError as error:
                raise GitSafeError(
                    f"Could not run formatter {command[0]!r}: {error}"
                ) from error
            if result.returncode:
                raise GitSafeError(
                    f"Formatter failed for {path!r} with exit status {result.returncode}"
                )
            formatted_bytes = temporary_file.read_bytes()
    except OSError as error:
        raise GitSafeError(f"Could not process {path!r}: {error}") from error

    try:
        formatted_text = formatted_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitSafeError(
            f"Formatter produced non-UTF-8 output for {path!r}"
        ) from error
    return original_text.splitlines(keepends=True), formatted_text.splitlines(
        keepends=True
    )


def diff_text(path, original, formatted):
    """Create a unified diff for a repository-relative file.

    Args:
        path: Repository-relative file path.
        original: Original source lines.
        formatted: Selected formatted lines.

    Returns:
        The unified diff as text.
    """
    return "".join(
        difflib.unified_diff(
            original,
            formatted,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def author_files(root, author_email):
    """Find clean, blameable files containing lines owned by an author.

    Args:
        root: Repository directory.
        author_email: Email of the selected author.

    Returns:
        Paths and per-line author emails for matching files.

    Raises:
        GitSafeError: If a matching file has uncommitted changes.
    """
    candidates = []
    for path in list_tracked_files(root):
        try:
            owners = blame_emails(root, path)
        except GitSafeError as error:
            message = str(error)
            if "binary" in message.lower() and "blam" in message.lower():
                print(f"Skipping binary file {path!r}: {message}", file=sys.stderr)
                continue
            raise
        if author_email in owners:
            candidates.append((path, owners))

    dirty = [path for path, _ in candidates if is_dirty(root, path)]
    if dirty:
        raise GitSafeError(
            "Refusing to format files with uncommitted changes:\n"
            + "\n".join(f"  {path}" for path in dirty)
        )
    return candidates


def emit_changes(root, changes, apply):
    """Print formatter diffs or write the selected changes.

    Args:
        root: Repository directory.
        changes: Tuples of file path, original lines, and selected lines.
        apply: Whether to write files instead of printing diffs.
    """
    for path, original, formatted in changes:
        patch = diff_text(path, original, formatted)
        if apply:
            destination = root / path
            with destination.open("w", encoding="utf-8", newline="") as output:
                output.write("".join(formatted))
            print(f"Formatted {path}")
        else:
            sys.stdout.write(patch)
    if not apply:
        print(
            f"Dry run: {len(changes)} file(s) would change. "
            "Re-run with --apply to write changes.",
            file=sys.stderr,
        )


def format_author(args):
    """Preview or apply formatter changes for the selected author's lines.

    Args:
        args: Parsed command-line arguments for the ``format`` command.

    Returns:
        Zero when formatting completed successfully.

    Raises:
        GitSafeError: If any target file is dirty or processing fails.
    """
    root = repository_root()
    author_email = resolve_author(root, args.author)
    candidates = author_files(root, author_email)

    if not candidates:
        print(f"No tracked lines in HEAD are attributed to {author_email}.")
        return 0

    changes = []
    for path, owners in candidates:
        original, formatted = format_file(root, path, args.formatter)
        selected = selected_formatted_lines(original, formatted, owners, author_email)
        if selected != original:
            changes.append((path, original, selected))

    if not changes:
        print(f"No formatter changes for {author_email}.")
        return 0

    emit_changes(root, changes, args.apply)
    return 0


def wrapper_path(root, email):
    """Build a stable wrapper path inside the repository's Git directory.

    Args:
        root: Repository directory.
        email: Resolved author email.

    Returns:
        The generated wrapper path.
    """
    git_directory = Path(
        os.fsdecode(
            run_git(root, "rev-parse", "--git-path", "git-safe/authors").stdout.rstrip(
                b"\n"
            )
        )
    )
    if not git_directory.is_absolute():
        git_directory = root / git_directory
    slug = re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-") or "author"
    suffix = hashlib.sha256(email.encode()).hexdigest()[:8]
    return git_directory / f"{slug}-{suffix}.sh"


def create_wrapper(args):
    """Create an executable local wrapper for a resolved author.

    Args:
        args: Parsed command-line arguments for the ``wrapper`` command.

    Returns:
        Zero when the wrapper is created successfully.

    Raises:
        GitSafeError: If the repository or author cannot be resolved.
    """
    root = repository_root()
    email = resolve_author(root, args.author)
    destination = wrapper_path(root, email)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tool_path = Path(__file__).resolve()
    content = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(tool_path))} "
        f'format --author {shlex.quote(email)} "$@"\n'
    )
    destination.write_text(content, encoding="utf-8")
    destination.chmod(0o755)
    print(destination)
    return 0


def build_parser():
    """Construct the command-line argument parser.

    Returns:
        The configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Apply formatter changes only in diff hunks touching an author's "
            "HEAD-blamed lines."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    format_parser = commands.add_parser(
        "format", help="format lines attributed to an author"
    )
    format_parser.add_argument(
        "--author", required=True, help="Git author email or unique display name"
    )
    format_parser.add_argument(
        "--apply", action="store_true", help="write the selected formatter changes"
    )
    format_parser.add_argument(
        "formatter",
        nargs=argparse.REMAINDER,
        help=(
            "formatter command and arguments (use {file} or omit it to append "
            "a temporary file path)"
        ),
    )

    wrapper_parser = commands.add_parser(
        "wrapper", help="generate a local wrapper for an author"
    )
    wrapper_parser.add_argument(
        "--author", required=True, help="Git author email or unique display name"
    )
    return parser


def main(argv=None):
    """Run the command-line application.

    Args:
        argv: Optional argument list, excluding the executable name.

    Returns:
        The process exit status.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "format":
        if args.formatter and args.formatter[0] == "--":
            args.formatter = args.formatter[1:]
        if not args.formatter:
            parser.error("format requires a formatter command after --")
        try:
            return format_author(args)
        except GitSafeError as error:
            print(f"git-safe: {error}", file=sys.stderr)
            return 1
    try:
        return create_wrapper(args)
    except GitSafeError as error:
        print(f"git-safe: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
