# git-safe-format

`git-safe` runs an existing formatter on a temporary copy of each tracked file,
then keeps only formatter diff hunks that touch contiguous lines attributed to a
chosen author by `git blame` at `HEAD`. This lets a repository adopt formatting
incrementally without rewriting every contributor's lines at once.

The tool requires Python 3 and Git. It invokes formatter commands directly
without a shell. The formatter must edit the provided file in place. If its
arguments include `{file}`, that token is replaced with the temporary file path;
otherwise the path is appended to the command.

## Format an author's lines

From inside a Git worktree, pass an author email or a unique Git display name:

```sh
./git-safe format --author alice@example.com -- black
./git-safe format --author "Alice Example" -- prettier --write
```

Use `--apply` before the separator to write changes. Runs are dry-run by default
and print a unified diff:

```sh
./git-safe format --author alice@example.com --apply -- black
```

The formatter runs on a temporary copy of each regular file tracked at `HEAD`
containing at least one line blamed on the selected author. Only complete
unified-diff hunks (including their context) that overlap an author-owned block
are applied. A hunk can therefore change nearby lines attributed to other
authors. Blocks are contiguous blame-line runs, not syntax-aware constructs.
Files with uncommitted changes are refused before formatting begins. Binary
files that Git cannot blame are reported and skipped; attributed non-UTF-8
files are reported as errors rather than silently skipped.

## Generate a per-author wrapper

```sh
./git-safe wrapper --author alice@example.com
```

The command creates an executable wrapper under the repository's Git directory
(`.git/git-safe/authors/`, or the equivalent worktree Git directory). That
directory is already excluded from normal repository status. Pass normal
`format` arguments to the wrapper:

```sh
.git/git-safe/authors/alice-example-com-*.sh --apply -- black
```

The generated wrapper fixes the resolved author email; it does not fix a
formatter command.

## Limitations

- Blame is evaluated at the current `HEAD`; staged or working-tree edits do not
  affect ownership.
- Formatter support is file-based and in-place. Configure the formatter to
  modify the supplied temporary path (for example, `prettier --write`).
- Hunk selection is line-based. It does not parse source syntax or guarantee
  that the resulting partial formatting is syntactically valid.
