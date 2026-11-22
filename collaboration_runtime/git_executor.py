"""Host-controlled Git object creation and local bare-repository delivery.

Targets and state directories must be trusted and controlled by the operator.
No model-supplied remote URL, repository hooks, checkout, or shell is used.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile


class GitActionError(RuntimeError):
    """The requested Git action is invalid or could not be performed."""


_SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SESSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")


class GitExecutor:
    def __init__(self, state_dir: Path, targets_dir: Path | None = None):
        self.state_dir = Path(state_dir).resolve()
        self.targets_dir = Path(targets_dir).resolve() if targets_dir is not None else self.state_dir / 'targets'
        self.repos = self.state_dir / 'git-objects'
        self.template = self.state_dir / 'empty-template'
        self.hooks = self.state_dir / 'disabled-hooks'
        try:
            for path in (self.state_dir, self.targets_dir, self.repos, self.template, self.hooks):
                path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GitActionError('Cannot initialize Git executor') from exc

    def _env(self):
        env = {'PATH': os.environ.get('PATH', ''), 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
               'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
               'GIT_CONFIG_VALUE_0': str(self.hooks), 'GIT_CONFIG_KEY_1': 'protocol.allow',
               'GIT_CONFIG_VALUE_1': 'never', 'GIT_AUTHOR_NAME': 'Collaboration Runtime',
               'GIT_AUTHOR_EMAIL': 'runtime@localhost', 'GIT_COMMITTER_NAME': 'Collaboration Runtime',
               'GIT_COMMITTER_EMAIL': 'runtime@localhost', 'HOME': str(self.state_dir),
               'GIT_ATTR_NOSYSTEM': '1'}
        for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def _git(self, args, *, data=None, index=None):
        env = self._env()
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
        try:
            return subprocess.run(['git', '-c', 'core.longpaths=true', *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitActionError('Git command could not be completed') from exc

    @staticmethod
    def _ref(ref):
        if (not isinstance(ref, str) or len(ref) > 200 or not ref.startswith('refs/heads/')
                or any(ord(c) < 32 or ord(c) > 126 for c in ref)):
            raise GitActionError('Invalid branch ref')
        return ref

    def _target_path(self, repository):
        if not isinstance(repository, str) or not _SLUG.fullmatch(repository):
            raise GitActionError('Invalid repository name')
        target = self.targets_dir / (repository + '.git')
        if target.is_symlink() or not target.is_dir() or target.resolve().parent != self.targets_dir:
            raise GitActionError('Target repository not available')
        return target

    def target_identity(self, repository):
        target = self._target_path(repository)
        stat = target.stat()
        return hashlib.sha256(f'{target}\0{stat.st_dev}\0{stat.st_ino}'.encode('utf-8')).hexdigest()

    def _target(self, repository, ref):
        self._ref(ref)
        if self._git(['check-ref-format', ref]).returncode != 0:
            raise GitActionError('Invalid branch ref')
        target = self._target_path(repository)
        result = self._git(['--git-dir', str(target), 'rev-parse', '--is-bare-repository'])
        if result.returncode != 0 or result.stdout.strip() != b'true':
            raise GitActionError('Target repository not available')
        return target

    def validate_target(self, repository, ref) -> None:
        self._target(repository, ref)

    @staticmethod
    def _file(path):
        if not isinstance(path, str) or not path or len(path) > 1024 or '\\' in path or '\x00' in path:
            raise GitActionError('Invalid file path')
        parts = path.split('/')
        if (PurePosixPath(path).is_absolute() or any(not p or p in ('.', '..') or ':' in p
                or p.rstrip(' .') != p or any(ord(c) < 32 for c in p) for p in parts)
                or any(p.casefold() in ('.git', '.gitmodules', '.gitattributes') for p in parts)
                or any(any(c in p for c in '<>"|?*') or p.split('.')[0].upper() in
                       {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                        *(f'LPT{i}' for i in range(1, 10))} for p in parts)):
            raise GitActionError('Invalid file path')
        return path

    def _local(self, session_id, repository, ref):
        if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
            raise GitActionError('Invalid session identity')
        name = hashlib.sha256((session_id + '\0' + repository + '\0' + ref).encode()).hexdigest()[:24]
        local = self.repos / (name + '.git')
        if local.is_symlink():
            raise GitActionError('Invalid Git state')
        if not local.exists():
            result = self._git(['init', '--bare', '--template=' + str(self.template), str(local)])
            if result.returncode:
                raise GitActionError('Cannot initialize Git state')
        return local

    def create_commit(self, session_id: str, repository: str, ref: str,
                      files: dict[str, bytes], message: str) -> str:
        target = self._target(repository, ref)
        if not isinstance(files, dict) or not files or len(files) > 256:
            raise GitActionError('Invalid file set')
        if not isinstance(message, str) or not message.strip() or len(message) > 12000 or '\x00' in message:
            raise GitActionError('Invalid commit message')
        validated = [(self._file(path), data) for path, data in files.items()]
        names = {PurePosixPath(path.casefold()) for path, _ in validated}
        if len(names) != len(validated) or any(names.intersection(path.parents) for path in names):
            raise GitActionError('Conflicting commit paths')
        if any(not isinstance(data, bytes) or len(data) > 1024 * 1024 for _, data in validated):
            raise GitActionError('Invalid file content')
        local = self._local(session_id, repository, ref)
        gitdir = ['--git-dir', str(local)]
        # Resolve the operator's target branch without checking out any content.
        parent_result = self._git(['--git-dir', str(target), 'rev-parse', '--verify', '--quiet', ref])
        if parent_result.returncode not in (0, 1):
            raise GitActionError('Cannot inspect target branch')
        parent = parent_result.stdout.decode('ascii', 'replace').strip() if parent_result.returncode == 0 else ''
        if parent and not _SHA.fullmatch(parent):
            raise GitActionError('Invalid target object')
        if parent:
            result = self._git([*gitdir, '-c', 'protocol.file.allow=always', 'fetch', '--no-tags',
                                '--no-write-fetch-head', str(target), ref])
            if result.returncode:
                raise GitActionError('Cannot read target history')
        with tempfile.TemporaryDirectory(prefix='index-', dir=self.state_dir) as temp:
            index = Path(temp) / 'index'
            result = self._git([*gitdir, 'read-tree', parent] if parent else [*gitdir, 'read-tree', '--empty'], index=index)
            if result.returncode:
                raise GitActionError('Cannot prepare commit tree')
            for path, data in validated:
                blob = self._git([*gitdir, 'hash-object', '-w', '--stdin'], data=data)
                sha = blob.stdout.decode('ascii', 'replace').strip()
                if blob.returncode or not _SHA.fullmatch(sha):
                    raise GitActionError('Cannot store file content')
                updated = self._git([*gitdir, 'update-index', '--add', '--cacheinfo', '100644', sha, path], index=index)
                if updated.returncode:
                    raise GitActionError('Cannot update commit tree')
            tree_result = self._git([*gitdir, 'write-tree'], index=index)
            tree = tree_result.stdout.decode('ascii', 'replace').strip()
            if tree_result.returncode or not _SHA.fullmatch(tree):
                raise GitActionError('Cannot write commit tree')
            args = [*gitdir, 'commit-tree', tree] + (['-p', parent] if parent else [])
            commit = self._git(args, data=message.encode('utf-8'))
            sha = commit.stdout.decode('ascii', 'replace').strip()
            if commit.returncode or not _SHA.fullmatch(sha):
                raise GitActionError('Cannot create commit')
            return sha

    def push(self, session_id: str, repository: str, ref: str, sha: str) -> str:
        target = self._target(repository, ref)
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitActionError('Invalid commit SHA')
        local = self._local(session_id, repository, ref)
        try:
            result = self._git(['--git-dir', str(local), '-c', 'protocol.file.allow=always',
                                'push', '--no-verify', '--no-follow-tags', '--', str(target), f'{sha}:{ref}'])
        except GitActionError:
            return 'pending_verification'
        if result.returncode == 0:
            return 'succeeded'
        # A rejected non-fast-forward is definitive; other failures can have an unknown outcome.
        if b'[rejected]' in result.stderr and b'non-fast-forward' in result.stderr:
            return 'failed'
        return 'pending_verification'

    def observe(self, repository: str, ref: str, sha: str) -> str:
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitActionError('Invalid commit SHA')
        try:
            target = self._target(repository, ref)
            result = self._git(['--git-dir', str(target), 'rev-parse', '--verify', '--quiet', ref])
            return 'succeeded' if result.returncode == 0 and result.stdout.strip().decode('ascii') == sha else 'pending_verification'
        except (GitActionError, UnicodeError):
            return 'pending_verification'
