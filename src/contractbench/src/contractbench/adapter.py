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
leave the scoring logic untouched:

1. strip the provenance/canary footer from ``instruction.md``;
2. replace the benchmark name in every agent-visible file (and, consistently,
   in tests/ and solution/) with a same-length neutral name;
3. grade from a trusted copy of the grading helper in ``tests/`` instead of the
   agent-writable ``/app/shared`` package, and install the hardened
   ``tests/test.sh`` from ``task-template/``;
4. preinstall the verifier's pytest dependencies in the image;
5. credit the benchmark authors in ``task.toml``.

Source: https://github.com/SecurityLab-UCD/contractbench
Paper:  https://arxiv.org/abs/2605.17281
"""

from __future__ import annotations

import re
import shutil
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

_VERIFIER_DEPS = "RUN pip install --no-cache-dir pytest==9.1.1 pytest-json-ctrf==0.6.1\n"
_FASTAPI_INSTALL_RE = re.compile(r"^RUN pip install --no-cache-dir fastapi[^\n]*\n", re.M)

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
        if dst.exists():
            shutil.rmtree(dst)
        # copy2-based copytree preserves the +x bits on solve.sh / test.sh.
        shutil.copytree(src, dst)

        self._sanitize_instruction(dst / "instruction.md")
        self._scrub_benchmark_name(dst)
        self._install_trusted_verifier(dst)
        self._preinstall_verifier_deps(dst / "environment" / "Dockerfile")
        self._set_authors(dst / "task.toml")
        return dst

    @staticmethod
    def _sanitize_instruction(instruction: Path) -> None:
        """Remove the benchmark-provenance canary comment from an instruction.md."""
        original = instruction.read_text()
        cleaned = _CANARY_COMMENT_RE.sub("\n", original).rstrip() + "\n"
        if cleaned != original:
            instruction.write_text(cleaned)

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
        """Grade from tests/, never from the agent-writable /app/shared package."""
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
        shutil.copy2(TEMPLATE_DIR / "tests" / "test.sh", tests / "test.sh")

    @staticmethod
    def _preinstall_verifier_deps(dockerfile: Path) -> None:
        text = dockerfile.read_text()
        if _VERIFIER_DEPS in text:
            return
        match = _FASTAPI_INSTALL_RE.search(text)
        if match is None:
            raise ValueError(f"{dockerfile}: no fastapi install line to extend")
        dockerfile.write_text(text[: match.end()] + _VERIFIER_DEPS + text[match.end() :])

    def _set_authors(self, task_toml: Path) -> None:
        text = task_toml.read_text()
        updated, n = re.subn(r"(?m)^authors = .*$", self._authors_line, text)
        if n != 1:
            raise ValueError(f"{task_toml}: expected exactly one authors line")
        task_toml.write_text(updated)

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
