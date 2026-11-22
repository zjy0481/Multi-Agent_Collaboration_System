import subprocess

import pytest

from collaboration_runtime.git_executor import GitActionError, GitExecutor


def git(*args):
    return subprocess.run(['git', *map(str, args)], check=True, capture_output=True)


def test_commit_push_and_update(tmp_path):
    targets = tmp_path / 'targets'
    targets.mkdir()
    git('init', '--bare', targets / 'demo.git')
    executor = GitExecutor(tmp_path / 'state', targets)
    executor.validate_target('demo', 'refs/heads/main')
    first = executor.create_commit('session', 'demo', 'refs/heads/main', {'a.txt': b'one'}, 'First')
    assert executor.observe('demo', 'refs/heads/main', first) == 'pending_verification'
    assert executor.push('session', 'demo', 'refs/heads/main', first) == 'succeeded'
    assert executor.observe('demo', 'refs/heads/main', first) == 'succeeded'
    second = executor.create_commit('session', 'demo', 'refs/heads/main', {'a.txt': b'two', 'dir/b.txt': b'b'}, 'Second')
    assert executor.push('session', 'demo', 'refs/heads/main', second) == 'succeeded'
    assert git('--git-dir', targets / 'demo.git', 'show', f'{second}:a.txt').stdout == b'two'
    assert executor.observe('demo', 'refs/heads/main', first) == 'pending_verification'


def test_invalid_targets_and_paths(tmp_path):
    targets = tmp_path / 'targets'
    targets.mkdir()
    git('init', '--bare', targets / 'demo.git')
    executor = GitExecutor(tmp_path / 'state', targets)
    for name, ref in [('../outside', 'refs/heads/main'), ('demo', 'main'), ('demo', 'refs/heads/../x')]:
        with pytest.raises(GitActionError):
            executor.validate_target(name, ref)
    for path in ('../x', '/absolute', '.git/config', 'src/.gitmodules', 'A\\b', 'x:evil', '.gitattributes'):
        with pytest.raises(GitActionError):
            executor.create_commit('session', 'demo', 'refs/heads/main', {path: b'x'}, 'message')
