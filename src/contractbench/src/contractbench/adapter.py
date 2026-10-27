"""ContractBenchAdapter - Adapter for the ContractBench benchmark.

ContractBench is a suite of 33 dual-axis observation-contract tasks that test
whether an LLM agent can reason about API contracts (validity + integrity).
Each task spins up a local FastAPI server and asks the agent to interact with
it according to a contract described in natural language.

The upstream repository already ships every task in Harbor-native form under
``harbor/cookbook-export/<task>/`` (an environment that auto-starts the FastAPI
server via ENTRYPOINT, a solution/solve.sh and a tests/test.sh that writes
``/logs/verifier/reward.txt``). The adapter clones the public repo at a pinned
ref, copies each task directory, and then applies a fixed set of changes that
leave the instruction, the server's behaviour and the scoring logic untouched:

1. strip the provenance/canary footer from ``instruction.md``;
2. replace the benchmark name in every agent-visible file (and, consistently,
   in tests/ and solution/) with a same-length neutral name;
3. grade from a trusted copy of the grading helper in ``tests/`` instead of the
   ``/app/shared`` package next to the server, and install the verifier
   entrypoint and image from ``task-template/``;
4. run the task server in its own compose service (``task-server``) instead of
   the agent's container, so its source and request log are out of the agent's
   reach; the agent still reaches it at ``localhost:8080``;
5. run the verifier in a separate container (``environment_mode =
   "separate"``) on the agent's ``/root/output`` and the server's
   ``/root/logs``, collected as artifacts;
6. set ``version = "1.0"`` and credit the benchmark authors in ``task.toml``.

Each task is built in a staging directory and moved into place only when every
step succeeded, and the result is checked for leftover canary text and
benchmark names before it is moved.

Source: https://github.com/SecurityLab-UCD/contractbench
Paper:  https://arxiv.org/abs/2605.17281
"""

from __future__ import annotations

import re
import shutil
import tempfile
import tomllib
from pathlib import Path

# Relative path inside the upstream repo holding the Harbor-native tasks.
COOKBOOK_EXPORT_SUBDIR = Path("harbor") / "cookbook-export"

TEMPLATE_DIR = Path(__file__).parent / "task-template"

# Upstream instruction.md files carry a benchmark-provenance footer (an HTML
# comment holding the benchmark name + a contamination-canary identifier). It is
# useful in the source repo for contamination detection, but it must never reach
# the agent: an agent could search the GUID/name and recover the benchmark (and
# its reference solutions). We strip that comment block at generation time so the
# agent-visible instruction.md does not disclose the benchmark identity.
_CANARY_COMMENT_RE = re.compile(
    r"\n*<!--(?:(?!-->).)*?canary\s+GUID(?:(?!-->).)*?-->\s*", re.DOTALL
)

# The benchmark name also appears in served data (e.g. data ids, client ids,
# signature-scheme labels) and in docstrings under /app. It is replaced with a
# neutral name of the same length, so string lengths, offsets and URL sizes in
# the tasks are unchanged. The replacement is applied to tests/ and solution/
# as well, so expected values stay consistent with what the server returns.
_NAME_RE = re.compile("contractbench", re.IGNORECASE)
_NEUTRAL_NAME = "serviceportal"
_SCRUBBED_DIRS = ("environment", "tests", "solution")
# Signing secrets that contain the name must keep their exact bytes: signatures
# derived from them are precomputed into instructions. They are re-encoded as
# hex instead of renamed, which hides the name and leaves the value unchanged.
_NAMED_SECRET_RE = re.compile(
    r'^(?P<lhs>\w*(?:SECRET|KEY)\w* = )b"(?P<value>[^"\\]*contractbench[^"\\]*)"$',
    re.IGNORECASE | re.MULTILINE,
)

_UPSTREAM_IMPORT_RE = re.compile(
    r'^sys\.path\.insert\(0, "/app"\)\n\n?'
    r"from shared\.server_logging import read_log, write_reward\n",
    re.M,
)
_TRUSTED_IMPORT = (
    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
    "from server_logging import read_log, write_reward\n"
)

# Agent-visible text must not identify the benchmark after generation.
_GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_CANARY_RE = re.compile(r"canary", re.IGNORECASE)

# Upstream environment/Dockerfile lines that put the server in the agent's
# container. The task-server image keeps the server lines and runs uvicorn in
# the foreground; the agent image drops them.
_SERVER_COPY_LINES = (
    "COPY shared/ /app/shared/\n",
    "COPY server.py /app/server.py\n",
    "ENV PYTHONPATH=/app\n",
)
_START_LINES = (
    "RUN printf '#!/bin/bash\\nuvicorn server:app --host 0.0.0.0 --port 8080 "
    "--log-level warning &\\nsleep 1\\nexec \"$@\"\\n' > /start.sh && chmod +x /start.sh\n",
    'ENTRYPOINT ["/start.sh"]\n',
    'CMD ["/bin/bash"]\n',
)
_SERVER_CMD = (
    'CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", '
    '"--log-level", "warning"]\n'
)
_MAIN_DOCKERFILE_HEADER = (
    "# Agent container. The task server runs in the `task-server` compose service\n"
    "# (environment/task-server/), reachable at localhost:8080; see\n"
    "# environment/docker-compose.yaml.\n"
)
_SERVER_DOCKERFILE_HEADER = (
    "# Task server, run as the `task-server` compose service. The agent reaches it\n"
    "# at localhost:8080 but cannot read its files.\n"
)

# task.toml edits: contract version key, artifacts handed to the separate
# verifier, and the verifier mode.
_SCHEMA_LINE = 'schema_version = "1.3"\n'
_VERSION_LINE = 'version = "1.0"\n'
_EMPTY_ARTIFACTS = "artifacts = []\n"
_ARTIFACTS = (
    "artifacts = [\n"
    '  { source = "/root/output" },\n'
    '  { source = "/root/logs", service = "task-server" },\n'
    "]\n"
)
_VERIFIER_HEADER = "[verifier]\n"
_SEPARATE_VERIFIER = '[verifier]\nenvironment_mode = "separate"\n'

# A cookbook-export task directory must contain these to be a valid Harbor task.
REQUIRED_TASK_FILES = (
    "task.toml",
    "instruction.md",
    Path("solution") / "solve.sh",
    Path("tests") / "test.sh",
    Path("tests") / "test_outputs.py",
    Path("environment") / "Dockerfile",
    Path("environment") / "shared" / "server_logging.py",
)


def _neutral(match: re.Match[str]) -> str:
    word = match.group(0)
    if word.isupper():
        return _NEUTRAL_NAME.upper()
    if word.islower():
        return _NEUTRAL_NAME
    return "ServicePortal"


class ContractBenchAdapter:
    """Adapter for ContractBench.

    One upstream ``harbor/cookbook-export/<task>/`` directory maps to one Harbor
    task directory; see the module docstring for the changes applied on top of
    the copy.
    """

    NAME = "contractbench"

    @staticmethod
    def make_local_task_id(source_id: str) -> str:
        """Convert a source task name into a Harbor task directory name.

        The upstream task names (e.g. ``basic-oauth-token``) are already valid
        Harbor task ids, so this is an identity-with-normalization mapping.

        Examples:
            >>> ContractBenchAdapter.make_local_task_id("basic-oauth-token")
            'basic-oauth-token'
        """
        return source_id.strip().lower().replace("_", "-").replace(" ", "-")

    def __init__(
        self,
        output_dir: Path,
        benchmark_root: Path,
        task_ids: list[str] | None = None,
        limit: int | None = None,
        overwrite: bool = False,
    ) -> None:
        """Initialize the adapter.

        Args:
            output_dir: Directory where Harbor tasks will be written.
            benchmark_root: Path to a checked-out ContractBench repository root.
            task_ids: Source task names to generate; ``None`` means all tasks.
            limit: Generate at most this many tasks.
            overwrite: Regenerate tasks whose output directory already exists.
        """
        self.output_dir = Path(output_dir)
        self.benchmark_root = Path(benchmark_root)
        self.task_ids = task_ids
        self.limit = limit
        self.overwrite = overwrite
        template_toml = tomllib.loads((TEMPLATE_DIR / "task.toml").read_text())
        self._authors_line = self._authors_toml(template_toml["task"]["authors"])

    @property
    def cookbook_dir(self) -> Path:
        return self.benchmark_root / COOKBOOK_EXPORT_SUBDIR

    def list_source_ids(self) -> list[str]:
        """Return the sorted list of cookbook-export task names in the repo."""
        cookbook = self.cookbook_dir
        if not cookbook.is_dir():
            raise FileNotFoundError(
                f"cookbook-export directory not found at {cookbook}. "
                "Is this a ContractBench checkout?"
            )
        ids = [
            child.name
            for child in cookbook.iterdir()
            if child.is_dir() and (child / "task.toml").exists()
        ]
        return sorted(ids)

    def resolve_source_ids(self) -> list[str]:
        """Return the source ids selected by ``task_ids`` and ``limit``.

        Raises:
            ValueError: if ``task_ids`` names a task that does not exist.
        """
        available = self.list_source_ids()
        if self.task_ids is None:
            selected = available
        else:
            unknown = sorted(set(self.task_ids) - set(available))
            if unknown:
                raise ValueError(f"Unknown task ids: {unknown}")
            selected = list(self.task_ids)
        if self.limit is not None:
            selected = selected[: max(0, self.limit)]
        return selected

    def _validate_source_task(self, src: Path) -> None:
        missing = [str(rel) for rel in REQUIRED_TASK_FILES if not (src / rel).exists()]
        if missing:
            raise FileNotFoundError(
                f"Source task {src.name} is missing required files: {missing}"
            )
        name = tomllib.loads((src / "task.toml").read_text()).get("task", {}).get("name")
        if name != f"{self.NAME}/{src.name}":
            raise ValueError(
                f"Source task {src.name} has [task].name {name!r}; "
                f"expected '{self.NAME}/{src.name}'"
            )

    def generate_task(self, source_id: str, local_task_id: str) -> Path:
        """Generate one Harbor task from a cookbook-export task.

        Args:
            source_id: Upstream task name (e.g. ``basic-oauth-token``).
            local_task_id: Output directory name under ``output_dir``.

        Returns:
            Path to the generated Harbor task directory.
        """
        src = self.cookbook_dir / source_id
        if not src.is_dir():
            raise FileNotFoundError(f"Source task not found: {src}")
        self._validate_source_task(src)

        dst = self.output_dir / local_task_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Build in a staging directory next to the output, and move the task
        # into place only once every step succeeded: a failure never leaves a
        # half-processed task that a later run would skip as existing.
        staging = Path(tempfile.mkdtemp(prefix=f".{local_task_id}.", dir=self.output_dir))
        try:
            work = staging / local_task_id
            # copy2-based copytree preserves the +x bits on solve.sh / test.sh.
            shutil.copytree(src, work)

            self._sanitize_instruction(work / "instruction.md")
            self._scrub_benchmark_name(work)
            self._install_trusted_verifier(work)
            self._isolate_server(work / "environment")
            self._configure_task_toml(work / "task.toml")
            self._check_agent_visible(work)

            if dst.exists():
                shutil.rmtree(dst)
            work.rename(dst)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return dst

    @staticmethod
    def _sanitize_instruction(instruction: Path) -> None:
        """Remove the benchmark-provenance canary comment from an instruction.md."""
        original = instruction.read_text()
        cleaned = _CANARY_COMMENT_RE.sub("\n", original).rstrip() + "\n"
        if cleaned != original:
            instruction.write_text(cleaned)

    @staticmethod
    def _check_agent_visible(task: Path) -> None:
        """Fail if canary text or the benchmark name survived in agent-visible files.

        The agent sees instruction.md and whatever the server returns; the
        server's files are the only source of served data.
        """
        files = [task / "instruction.md"]
        files.extend(p for p in sorted((task / "environment").rglob("*")) if p.is_file())
        for path in files:
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for pattern, what in (
                (_CANARY_RE, "canary text"),
                (_GUID_RE, "a GUID"),
                (_NAME_RE, "the benchmark name"),
            ):
                if pattern.search(text):
                    raise ValueError(f"{path}: {what} survived generation")

    @staticmethod
    def _scrub_benchmark_name(task: Path) -> None:
        """Replace the benchmark name in instruction, environment, tests, solution."""
        files = [task / "instruction.md"]
        for sub in _SCRUBBED_DIRS:
            files.extend(p for p in sorted((task / sub).rglob("*")) if p.is_file())
        for path in files:
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            scrubbed = _NAMED_SECRET_RE.sub(
                lambda m: f'{m["lhs"]}bytes.fromhex("{m["value"].encode().hex()}")',
                text,
            )
            scrubbed = _NAME_RE.sub(_neutral, scrubbed)
            if scrubbed != text:
                path.write_text(scrubbed)

    @staticmethod
    def _install_trusted_verifier(task: Path) -> None:
        """Grade from tests/, never from the /app/shared package next to the server."""
        tests = task / "tests"
        shutil.copy2(
            task / "environment" / "shared" / "server_logging.py",
            tests / "server_logging.py",
        )
        test_outputs = tests / "test_outputs.py"
        source = test_outputs.read_text()
        rewritten, n = _UPSTREAM_IMPORT_RE.subn(_TRUSTED_IMPORT, source)
        if n != 1 or "\nimport os\n" not in source:
            raise ValueError(f"{test_outputs}: unexpected grading-helper import")
        test_outputs.write_text(rewritten)
        for name in ("test.sh", "Dockerfile"):
            shutil.copy2(TEMPLATE_DIR / "tests" / name, tests / name)

    @staticmethod
    def _isolate_server(environment: Path) -> None:
        """Move the task server out of the agent's container into ``task-server``."""
        lines = (environment / "Dockerfile").read_text().splitlines(keepends=True)
        for line in _SERVER_COPY_LINES + _START_LINES:
            if lines.count(line) != 1:
                raise ValueError(f"{environment / 'Dockerfile'}: expected one {line!r}")

        server_dir = environment / "task-server"
        server_dir.mkdir()
        (environment / "server.py").rename(server_dir / "server.py")
        (environment / "shared").rename(server_dir / "shared")

        server = "".join(
            _SERVER_CMD if line == _START_LINES[0] else line
            for line in lines
            if line not in _START_LINES[1:]
        )
        (server_dir / "Dockerfile").write_text(
            _SERVER_DOCKERFILE_HEADER + "\n" + server.rstrip() + "\n"
        )

        removed = _SERVER_COPY_LINES + _START_LINES
        main = "".join(line for line in lines if line not in removed)
        main = re.sub(r"\n{3,}", "\n\n", main)
        (environment / "Dockerfile").write_text(
            _MAIN_DOCKERFILE_HEADER + "\n" + main.rstrip() + "\n"
        )
        shutil.copy2(
            TEMPLATE_DIR / "environment" / "docker-compose.yaml",
            environment / "docker-compose.yaml",
        )

    def _configure_task_toml(self, task_toml: Path) -> None:
        lines = task_toml.read_text().splitlines(keepends=True)
        for old, new in (
            (_SCHEMA_LINE, _VERSION_LINE),
            (_EMPTY_ARTIFACTS, _ARTIFACTS),
            (_VERIFIER_HEADER, _SEPARATE_VERIFIER),
        ):
            if lines.count(old) != 1:
                raise ValueError(f"{task_toml}: expected exactly one {old!r}")
            lines[lines.index(old)] = new
        text = "".join(lines)
        text, n = re.subn(r"(?m)^authors = .*$", self._authors_line, text)
        if n != 1:
            raise ValueError(f"{task_toml}: expected exactly one authors line")
        task_toml.write_text(text)

    @staticmethod
    def _authors_toml(authors: list[dict[str, str]]) -> str:
        entries = []
        for author in authors:
            fields = [f'name = "{author["name"]}"']
            if author.get("email"):
                fields.append(f'email = "{author["email"]}"')
            entries.append("{ " + ", ".join(fields) + " }")
        return "authors = [" + ", ".join(entries) + "]"

    def run(self) -> list[str]:
        """Generate the selected tasks into ``output_dir``.

        Returns:
            Local ids of the tasks written (existing ones are skipped unless
            ``overwrite`` is set).

        Raises:
            RuntimeError: if any selected task failed to generate.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        failed: dict[str, str] = {}
        for source_id in self.resolve_source_ids():
            local_task_id = self.make_local_task_id(source_id)
            if not self.overwrite and (self.output_dir / local_task_id).exists():
                continue
            try:
                self.generate_task(source_id, local_task_id)
            except Exception as e:  # noqa: BLE001 - reported per task below
                failed[source_id] = f"{type(e).__name__}: {e}"
                continue
            written.append(local_task_id)
        if failed:
            raise RuntimeError(f"{len(failed)} task(s) failed to generate: {failed}")
        return written
