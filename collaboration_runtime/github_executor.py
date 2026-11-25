"""GitHub HTTPS delivery for one target selected by the host."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil

from .git_executor import GitActionError, GitExecutor, _SHA, _SLUG


_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
TASK_BRANCH_PREFIX = "refs/heads/macs/task-"
_TASK_BRANCH = re.compile(re.escape(TASK_BRANCH_PREFIX) + r"[0-9a-f]{32}\Z")
_AUTH_REFUSAL = re.compile(
    rb"fatal: (?:"
    rb"Authentication failed for 'https://github\.com/[^']+'|"
    rb"could not read (?:Username|Password) for 'https://github\.com(?:/[^']*)?': terminal prompts disabled|"
    rb"unable to access 'https://github\.com/[^']+': The requested URL returned error: (?:401|403)"
    rb")"
)


class _MissingGitHubCLI(GitActionError):
    """Credential helper lookup failed before any Git command was started."""


class GitHubExecutor(GitExecutor):
    @classmethod
    def from_config(cls, state_dir: Path, raw: str):
        try:
            config = json.loads(raw)
            if not isinstance(config, dict) or config.keys() not in ({"alias", "repository"}, {"alias", "repository", "ref"}):
                raise ValueError
            alias, repository = (config[key] for key in ("alias", "repository"))
            ref = config.get("ref")
            if not all(isinstance(value, str) for value in (alias, repository)):
                raise ValueError
            if not _SLUG.fullmatch(alias) or not _REPOSITORY.fullmatch(repository):
                raise ValueError
            name = repository.split("/")[1]
            if name in {".", ".."} or name.lower().endswith(".git"):
                raise ValueError
            if "ref" in config:
                cls._ref(ref)
        except (ValueError, GitActionError):
            raise ValueError("Invalid COLLABORATION_GITHUB_TARGET: expected alias, owner/repository and optional full branch ref") from None
        executor = cls(state_dir, alias, repository, ref)
        if ref is not None and executor._git(["check-ref-format", ref]).returncode != 0:
            raise ValueError("Invalid branch ref in COLLABORATION_GITHUB_TARGET")
        return executor

    def __init__(self, state_dir, alias, repository, ref):
        super().__init__(state_dir)
        self.alias = alias
        self.repository = repository
        self.ref = ref
        self.task_branch_repository = alias if ref is None else None
        self.url = f"https://github.com/{repository}.git"
        self.identity = hashlib.sha256(f"{self.url.lower()}\0{ref or TASK_BRANCH_PREFIX}".encode()).hexdigest()

    def target_identity(self, repository):
        if repository != self.alias:
            raise GitActionError("GitHub repository alias not configured")
        return self.identity

    def remote_repository(self, repository, target_identity):
        if repository == self.alias and target_identity == self.identity:
            return self.repository
        return None

    def _target(self, repository, ref):
        self.target_identity(repository)
        self._ref(ref)
        if self.ref is not None and ref != self.ref:
            raise GitActionError("GitHub branch ref not configured")
        if self.ref is None and not _TASK_BRANCH.fullmatch(ref):
            raise GitActionError("Expected a platform-managed task branch")
        return self.url

    def _remote_git(self, args, gitdir=()):
        gh = shutil.which("gh")
        if gh is None:
            raise _MissingGitHubCLI("GitHub CLI is required for GitHub delivery")
        helper = "!" + shlex.quote(Path(gh).resolve().as_posix()) + " auth git-credential"
        env = self._env()
        # Keep the host CLI credential lookup while leaving Git's HOME and
        # configuration isolated. Do not inherit Git credential or trace variables.
        for key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HTTPS_PROXY", "NO_PROXY",
                    "https_proxy", "no_proxy"):
            if key in os.environ:
                env[key] = os.environ[key]
        config_dir = os.environ.get("GH_CONFIG_DIR")
        if not config_dir and os.environ.get("XDG_CONFIG_HOME"):
            config_dir = str(Path(os.environ["XDG_CONFIG_HOME"]) / "gh")
        if not config_dir and not (os.name == "nt" and os.environ.get("APPDATA")):
            config_dir = str(Path.home() / ".config" / "gh")
        if config_dir:
            env["GH_CONFIG_DIR"] = str(Path(config_dir).resolve())
        env.update({"GH_PROMPT_DISABLED": "1", "LC_ALL": "C",
                    "GIT_CEILING_DIRECTORIES": str(self.state_dir.parent)})
        return self._git([
            *gitdir, "-c", "protocol.https.allow=always", "-c", "credential.helper=",
            "-c", f"credential.https://github.com.helper={helper}",
            "-c", "http.followRedirects=false", *args,
        ], env=env, cwd=self.state_dir)

    def _read_ref(self, target, ref):
        options = [] if ref == "HEAD" else ["--refs"]
        result = self._remote_git(["ls-remote", "--exit-code", *options, "--", target, ref])
        if result.returncode == 2 and not result.stdout:
            return ""
        if result.returncode == 0:
            try:
                sha, observed_ref = result.stdout.decode("ascii").rstrip("\r\n").split("\t")
            except (UnicodeError, ValueError):
                pass
            else:
                if observed_ref == ref and _SHA.fullmatch(sha):
                    return sha
        raise GitActionError("Cannot verify the GitHub branch ref")

    def _fetch_parent(self, gitdir, target, parent):
        if parent:
            fetch = self._remote_git(["fetch", "--no-tags", "--no-write-fetch-head",
                                      "--no-recurse-submodules", "--", target, parent], gitdir)
            if fetch.returncode:
                raise GitActionError("Cannot read GitHub branch history")

    def prepare_branch(self, session_id, repository, ref):
        target = self._target(repository, ref)
        local = self._local(session_id, repository, ref)
        return self._prepare_parent(["--git-dir", str(local)], target, ref)

    def _prepare_parent(self, gitdir, target, ref):
        if self.task_branch_repository is not None:
            existing = self._git([*gitdir, "rev-parse", "--verify", "--quiet", ref])
            if existing.returncode not in (0, 1):
                raise GitActionError("Cannot read the local task branch")
            if existing.returncode == 0:
                head = existing.stdout.decode("ascii", "replace").strip()
                if not _SHA.fullmatch(head):
                    raise GitActionError("Invalid local task branch SHA")
                return head

        parent = self._read_ref(target, ref)
        if not parent and self.task_branch_repository is not None:
            parent = self._read_ref(target, "HEAD")
        self._fetch_parent(gitdir, target, parent)
        if self.task_branch_repository is not None:
            if self._git([*gitdir, "symbolic-ref", "HEAD", ref]).returncode:
                raise GitActionError("Cannot prepare the local task branch")
            if parent:
                self._update_local_branch(gitdir, ref, parent, None)
        return parent

    def _push_status(self, gitdir, target, ref, sha):
        try:
            result = self._remote_git(["push", "--porcelain", "--no-verify", "--no-follow-tags", "--",
                                       target, f"{sha}:{ref}"], gitdir)
        except _MissingGitHubCLI:
            return "failed"
        except GitActionError:
            return "pending_verification"
        if result.returncode == 0:
            return "succeeded"

        # Git distinguishes ref rejection from [remote failure], which
        # can indicate an interrupted response.
        expected_ref = ref.encode("ascii")
        for line in result.stdout.splitlines():
            fields = line.split(b"\t", 2)
            if len(fields) == 3 and fields[0] == b"!" and fields[1].partition(b":")[2] == expected_ref:
                if fields[2].partition(b" (")[0] in {b"[rejected]", b"[remote rejected]"}:
                    return "failed"
        if any(_AUTH_REFUSAL.fullmatch(line) for line in result.stderr.splitlines()):
            return "failed"
        return "pending_verification"

    def observe(self, repository, ref, sha):
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitActionError("Invalid commit SHA")
        try:
            observed = self._read_ref(self._target(repository, ref), ref)
        except GitActionError:
            return "pending_verification"
        return "succeeded" if observed == sha else "pending_verification"
