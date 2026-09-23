"""Refs, notes and config are read without a Git process, exactly as Git reads them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_core.ledger import git as git_module
from cruxible_core.ledger.git import _ASK_GIT, _files_backend_ref


def _git(repository: Path, *arguments: str, input_bytes: bytes | None = None) -> str:
    return (
        subprocess.run(
            ["git", f"--git-dir={repository}", *arguments],
            input=input_bytes,
            check=True,
            capture_output=True,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            },
        )
        .stdout.decode()
        .strip()
    )


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repo.git"
    subprocess.run(["git", "init", "--bare", "-q", str(repository)], check=True)
    blob = _git(repository, "hash-object", "-w", "--stdin", input_bytes=b"one\n")
    tree = _git(repository, "mktree", input_bytes=f"100644 blob {blob}\tone\n".encode())
    commit = (
        subprocess.run(
            ["git", f"--git-dir={repository}", "commit-tree", tree, "-m", "one"],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "a",
                "GIT_AUTHOR_EMAIL": "a@a",
                "GIT_COMMITTER_NAME": "a",
                "GIT_COMMITTER_EMAIL": "a@a",
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            },
        )
        .stdout.decode()
        .strip()
    )
    _git(repository, "update-ref", "refs/heads/main", commit)
    return repository, commit


def test_loose_packed_and_absent_refs_match_git(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    assert _files_backend_ref(repository, "refs/heads/main") == commit
    assert _files_backend_ref(repository, "refs/heads/absent") is None
    _git(repository, "update-ref", "refs/proposals/owner/one", commit)
    _git(repository, "pack-refs", "--all")
    assert not (repository / "refs/proposals/owner/one").exists()
    assert _files_backend_ref(repository, "refs/proposals/owner/one") == commit
    # A packed ref deleted by Git disappears from the remembered table too.
    _git(repository, "update-ref", "-d", "refs/proposals/owner/one")
    assert _files_backend_ref(repository, "refs/proposals/owner/one") is None


@pytest.mark.parametrize("ref", ["refs/heads/../main", "HEAD", "refs/heads/main.lock"])
def test_unmodeled_names_are_left_to_git(tmp_path: Path, ref: str) -> None:
    repository, _commit = _repository(tmp_path)
    assert _files_backend_ref(repository, ref) is _ASK_GIT


def test_symbolic_refs_are_left_to_git(tmp_path: Path) -> None:
    repository, _commit = _repository(tmp_path)
    _git(repository, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    assert _files_backend_ref(repository, "refs/heads/alias") is _ASK_GIT


def test_resident_notes_match_git_notes_show_across_fanout(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)
    ledger = git_module.GitLedger.__new__(git_module.GitLedger)
    ledger.path = repository
    ref = "refs/notes/playbill-eval"
    ledger._note_ref = lambda kind: ref  # type: ignore[method-assign]
    ledger._object_format_cache = None
    assert ledger._resident_note("evaluation", commit) is None
    # Enough notes that Git fans the notes tree out into directories.
    targets = [commit]
    for index in range(300):
        targets.append(
            _git(repository, "hash-object", "-w", "--stdin", input_bytes=f"{index}\n".encode())
        )
    for index, target in enumerate(targets):
        _git(repository, "notes", f"--ref={ref}", "add", "-m", f"note {index}", target)
    root = _git(repository, "rev-parse", f"{ref}^{{tree}}")
    assert any(len(name) == 2 for name in _git(repository, "ls-tree", "--name-only", root).split())
    for target in (targets[0], targets[150], targets[-1]):
        expected = subprocess.run(
            ["git", f"--git-dir={repository}", "notes", f"--ref={ref}", "show", target],
            check=True,
            capture_output=True,
        ).stdout
        assert ledger._resident_note("evaluation", target) == expected
    unnoted = _git(repository, "hash-object", "-w", "--stdin", input_bytes=b"unnoted\n")
    assert ledger._resident_note("evaluation", unnoted) is None


def test_config_reads_are_remembered_only_while_the_config_file_is_unchanged(
    tmp_path: Path,
) -> None:
    repository, _commit = _repository(tmp_path)
    ledger = git_module.GitLedger.__new__(git_module.GitLedger)
    ledger.path = repository
    calls: list[tuple[str, ...]] = []
    real = git_module.GitLedger._git

    def counted(self, arguments, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(tuple(arguments))
        return real(self, arguments, **kwargs)

    query = ["config", "--default", "UTF-8", "--get", "i18n.commitencoding"]
    try:
        git_module.GitLedger._git = counted  # type: ignore[method-assign]
        assert ledger._config_read(query) == b"UTF-8\n"
        assert ledger._config_read(query) == b"UTF-8\n"
        assert len(calls) == 1
        _git(repository, "config", "i18n.commitencoding", "ISO-8859-1")
        assert ledger._config_read(query) == b"ISO-8859-1\n"
        assert len(calls) == 2  # one fresh read after the change
    finally:
        git_module.GitLedger._git = real  # type: ignore[method-assign]
