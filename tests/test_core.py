"""Core tests for the eval framework.

These test the critical paths that, if broken, silently produce wrong results
or confusing errors. No API calls, no CLI invocations — just pure logic.

Run: uv run pytest tests/
"""

import json
from pathlib import Path

import click
import pytest
import yaml

from evals.run import load_tasks, save_result, validate_env_vars

# ---------------------------------------------------------------------------
# 1. Task YAML loading — the foundation of everything
# ---------------------------------------------------------------------------


class TestLoadTasks:
    """If task loading is broken, every eval silently does the wrong thing."""

    def test_loads_real_tasks(self):
        """The committed workdir-smoke.yaml should load without errors."""
        tasks = load_tasks("workdir-smoke")
        assert len(tasks) >= 1
        for t in tasks:
            assert "id" in t
            assert "intent" in t
            assert "ground_truth" in t
            assert "criteria" in t["ground_truth"]

    def test_filter_by_task_id(self, tmp_path):
        """--task flag should return exactly one task."""
        yaml_content = yaml.dump(
            {
                "tasks": [
                    {"id": "task-a", "intent": "do A", "ground_truth": {"criteria": "A"}},
                    {"id": "task-b", "intent": "do B", "ground_truth": {"criteria": "B"}},
                ]
            }
        )
        (tmp_path / "my-skill.yaml").write_text(yaml_content)

        from evals import run

        original = run.TASKS_DIR
        run.TASKS_DIR = tmp_path
        try:
            tasks = load_tasks("my-skill", task_id="task-a")
            assert len(tasks) == 1
            assert tasks[0]["id"] == "task-a"
        finally:
            run.TASKS_DIR = original

    def test_filter_by_category(self, tmp_path):
        """--category should only return tasks in that category."""
        yaml_content = yaml.dump(
            {
                "tasks": [
                    {"id": "t1", "intent": "auth thing", "ground_truth": {"criteria": "x"}, "category": "auth"},
                    {"id": "t2", "intent": "index thing", "ground_truth": {"criteria": "x"}, "category": "index"},
                ]
            }
        )
        (tmp_path / "my-skill.yaml").write_text(yaml_content)

        from evals import run

        original = run.TASKS_DIR
        run.TASKS_DIR = tmp_path
        try:
            tasks = load_tasks("my-skill", category="auth")
            assert all(t.get("category") == "auth" for t in tasks)
            assert len(tasks) == 1
        finally:
            run.TASKS_DIR = original

    def test_filter_nonexistent_task_returns_empty(self, tmp_path):
        """A task ID that doesn't exist should return empty list, not crash."""
        yaml_content = yaml.dump(
            {
                "tasks": [
                    {"id": "real-task", "intent": "do something", "ground_truth": {"criteria": "x"}},
                ]
            }
        )
        (tmp_path / "my-skill.yaml").write_text(yaml_content)

        from evals import run

        original = run.TASKS_DIR
        run.TASKS_DIR = tmp_path
        try:
            tasks = load_tasks("my-skill", task_id="does-not-exist")
            assert tasks == []
        finally:
            run.TASKS_DIR = original

    def test_missing_skill_file_exits(self):
        """Loading a nonexistent skill should exit, not return empty."""
        with pytest.raises(click.exceptions.Exit):
            load_tasks("nonexistent-skill")

    def test_task_has_required_fields(self, tmp_path):
        """A task YAML missing 'id' or 'intent' should fail loudly."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(yaml.dump({"tasks": [{"id": "no-intent"}]}))

        # Temporarily point TASKS_DIR at our temp dir
        from evals import run

        original = run.TASKS_DIR
        run.TASKS_DIR = tmp_path
        try:
            with pytest.raises(click.exceptions.Exit):
                load_tasks("bad")
        finally:
            run.TASKS_DIR = original

    def test_invalid_yaml_structure_exits(self, tmp_path):
        """A YAML file that's a list instead of a dict should fail."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("- just\n- a\n- list\n")

        from evals import run

        original = run.TASKS_DIR
        run.TASKS_DIR = tmp_path
        try:
            with pytest.raises(click.exceptions.Exit):
                load_tasks("bad")
        finally:
            run.TASKS_DIR = original


# ---------------------------------------------------------------------------
# 2. Env var validation — catches missing config before wasting time
# ---------------------------------------------------------------------------


class TestEnvValidation:
    """Env vars should be checked upfront, not mid-run."""

    def test_passes_when_vars_set(self, monkeypatch):
        """No error when all required vars are present."""
        monkeypatch.setenv("PINECONE_API_KEY", "test-key")
        tasks = [{"id": "t1", "intent": "test", "env": ["PINECONE_API_KEY"]}]
        validate_env_vars(tasks)  # should not raise

    def test_fails_when_vars_missing(self, monkeypatch):
        """Should exit when a required var is missing."""
        monkeypatch.delenv("PINECONE_API_KEY", raising=False)
        tasks = [{"id": "t1", "intent": "test", "env": ["PINECONE_API_KEY"]}]
        with pytest.raises(click.exceptions.Exit):
            validate_env_vars(tasks)

    def test_reports_all_missing_vars(self, monkeypatch, capsys):
        """Should list ALL missing vars, not just the first one."""
        monkeypatch.delenv("VAR_A", raising=False)
        monkeypatch.delenv("VAR_B", raising=False)
        tasks = [
            {"id": "t1", "intent": "test", "env": ["VAR_A"]},
            {"id": "t2", "intent": "test", "env": ["VAR_B"]},
        ]
        with pytest.raises(click.exceptions.Exit):
            validate_env_vars(tasks)
        output = capsys.readouterr().out
        assert "VAR_A" in output
        assert "VAR_B" in output

    def test_no_env_field_is_fine(self):
        """Tasks without env requirements should pass validation."""
        tasks = [{"id": "t1", "intent": "test"}]
        validate_env_vars(tasks)  # should not raise


# ---------------------------------------------------------------------------
# 3. Result saving — if files don't get written, grading breaks
# ---------------------------------------------------------------------------


class TestSaveResult:
    """Results must be saved in the exact format the grader expects."""

    def test_saves_json(self, tmp_path):
        """The .json file is what the grader reads — must always exist."""
        result = {"conversation_md": "# test", "some_field": "value"}
        save_result(tmp_path, "task__with-skill", result)

        json_file = tmp_path / "task__with-skill.json"
        assert json_file.exists()
        data = json.loads(json_file.read_text())
        assert data["conversation_md"] == "# test"
        assert data["some_field"] == "value"

    def test_saves_md_when_present(self, tmp_path):
        """Conversation trace should be saved as .md for human reading."""
        result = {"conversation_md": "# with-skill\n\n**User:** hello\n"}
        save_result(tmp_path, "task__with-skill", result)

        md_file = tmp_path / "task__with-skill.md"
        assert md_file.exists()
        assert "**User:** hello" in md_file.read_text()

    def test_saves_jsonl_when_present(self, tmp_path):
        """Raw events should be saved as .jsonl for debugging."""
        result = {"raw_events": ['{"type":"init"}', '{"type":"result"}']}
        save_result(tmp_path, "task__with-skill", result)

        jsonl_file = tmp_path / "task__with-skill.jsonl"
        assert jsonl_file.exists()
        lines = jsonl_file.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_skips_optional_files_when_missing(self, tmp_path):
        """Only .json is mandatory. Missing conversation_md/raw_events = no extra files."""
        result = {"error": "no_output"}
        save_result(tmp_path, "task__with-skill", result)

        assert (tmp_path / "task__with-skill.json").exists()
        assert not (tmp_path / "task__with-skill.md").exists()
        assert not (tmp_path / "task__with-skill.jsonl").exists()

    def test_saves_stderr_log(self, tmp_path):
        """Stderr should be captured for debugging CLI errors."""
        result = {"stderr": "Permission denied"}
        save_result(tmp_path, "task__with-skill", result)

        log = tmp_path / "task__with-skill.stderr.log"
        assert log.exists()
        assert "Permission denied" in log.read_text()


# ---------------------------------------------------------------------------
# 4. Grader prompt construction — garbage in = garbage grades
# ---------------------------------------------------------------------------


class TestGraderPrompt:
    """The grader prompt determines pass/fail. If it's malformed, grades are wrong."""

    @pytest.fixture(autouse=True)
    def _import_grader(self):
        """Import build_grader_prompt, skipping if anthropic SDK isn't installed."""
        try:
            from evals.framework.grader import build_grader_prompt

            self.build = build_grader_prompt
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_includes_criteria(self):
        """The criteria is the most important part — must be in the prompt."""
        task = {"ground_truth": {"criteria": "Agent must run pc index list"}}
        prompt = self.build(task, "some conversation", "")
        assert "Agent must run pc index list" in prompt

    def test_includes_expected_commands(self):
        """Expected commands help the grader check for specific CLI calls."""
        task = {"ground_truth": {"criteria": "test", "commands": ["pc index list", "pc logout"]}}
        prompt = self.build(task, "convo", "")
        assert "pc index list" in prompt
        assert "pc logout" in prompt

    def test_includes_conversation(self):
        """The actual conversation must be in the prompt for grading."""
        task = {"ground_truth": {"criteria": "test"}}
        prompt = self.build(task, "**Bash:** `pc index list --json`", "")
        assert "pc index list --json" in prompt

    def test_includes_calibration_examples(self):
        """Calibration examples should be included when provided."""
        task = {"ground_truth": {"criteria": "test"}}
        examples = "## Calibration Examples\n\n### PASS: used --json flag"
        prompt = self.build(task, "convo", examples)
        assert "Calibration Examples" in prompt
        assert "used --json flag" in prompt

    def test_handles_empty_ground_truth(self):
        """A task with no ground_truth shouldn't crash the grader."""
        task = {}
        prompt = self.build(task, "convo", "")
        assert "no specific criteria provided" in prompt


# ---------------------------------------------------------------------------
# 5. Workdir capture — agent-generated files shown to the grader
# ---------------------------------------------------------------------------


class TestLoadWorkdirFiles:
    """load_workdir_files renders filtered workdir contents for the grader."""

    @pytest.fixture(autouse=True)
    def _import(self):
        try:
            from evals.framework.grader import WORKDIR_CAP_BYTES, load_workdir_files

            self.load = load_workdir_files
            self.cap = WORKDIR_CAP_BYTES
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_missing_dir_returns_empty(self, tmp_path):
        """If the workdir doesn't exist at all, return empty string (grader section omitted)."""
        assert self.load(tmp_path / "nope") == ""

    def test_empty_dir_returns_empty(self, tmp_path):
        """An existing but empty workdir also returns empty string."""
        assert self.load(tmp_path) == ""

    def test_only_filtered_out_files_returns_empty(self, tmp_path):
        """A dir containing only binary/ignored files returns empty — no section."""
        (tmp_path / "image.png").write_bytes(b"\x89PNG...")
        (tmp_path / "lock.lock").write_text("...")
        assert self.load(tmp_path) == ""

    def test_includes_python_file_with_fence(self, tmp_path):
        """.py files should appear under the expected markdown heading with a python fence."""
        (tmp_path / "hello.py").write_text('print("hi")\n')
        out = self.load(tmp_path)
        assert "## Generated Code Files" in out
        assert "### hello.py" in out
        assert "```python" in out
        assert 'print("hi")' in out

    def test_includes_named_file_without_extension(self, tmp_path):
        """requirements.txt / Dockerfile / etc. should be included by name, not extension."""
        (tmp_path / "requirements.txt").write_text("requests\n")
        (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
        out = self.load(tmp_path)
        assert "requirements.txt" in out
        assert "Dockerfile" in out

    def test_skips_noise_directories(self, tmp_path):
        """__pycache__, node_modules, .venv, etc. must not be walked."""
        (tmp_path / "main.py").write_text("x = 1\n")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "main.cpython-311.pyc").write_text("garbage\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = 1\n")
        out = self.load(tmp_path)
        assert "main.py" in out
        assert "__pycache__" not in out
        assert "node_modules" not in out
        assert "garbage" not in out

    def test_recurses_into_subdirs(self, tmp_path):
        """Files in subdirectories should be included with relative paths."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "util.py").write_text("def f(): pass\n")
        out = self.load(tmp_path)
        assert "### src/util.py" in out

    def test_caps_total_bytes_with_note(self, tmp_path):
        """A single oversized file should be truncated, and the truncation note appended."""
        big = "x" * (self.cap + 10_000)
        (tmp_path / "big.py").write_text(big)
        out = self.load(tmp_path)
        assert len(out) <= self.cap + 500  # allow for headers/note
        assert "file truncated" in out
        assert "byte cap" in out


# ---------------------------------------------------------------------------
# 7. Packaged smoke (cultivar hello) — must be locatable via importlib
# ---------------------------------------------------------------------------


class TestPackagedSmoke:
    """`cultivar hello` reads the smoke from package data; if it's missing
    from the wheel, a fresh `uv tool install` can't smoke-test itself."""

    def test_smoke_resources_locatable(self):
        """importlib.resources must find both the task YAML and the SKILL.md."""
        from importlib.resources import files

        pkg = files("evals._resources") / "smoke"
        task_yaml = pkg / "workdir-smoke.yaml"
        skill_md = pkg / "skill" / "SKILL.md"
        assert task_yaml.is_file(), "packaged smoke task YAML missing"
        assert skill_md.is_file(), "packaged smoke SKILL.md missing"

    def test_smoke_task_is_valid(self):
        """The packaged YAML must parse and have the fields run/grade expect."""
        from importlib.resources import files

        pkg = files("evals._resources") / "smoke"
        data = yaml.safe_load((pkg / "workdir-smoke.yaml").read_text())
        assert isinstance(data, dict) and "tasks" in data
        assert len(data["tasks"]) >= 1
        for t in data["tasks"]:
            assert "id" in t and "intent" in t
            assert "ground_truth" in t and "criteria" in t["ground_truth"]

    def test_materialize_smoke_round_trip(self, tmp_path):
        """_materialize_smoke should drop a working skill_dir + return tasks."""
        from evals.hello import _materialize_smoke

        tasks, skill_dir = _materialize_smoke(tmp_path)
        assert len(tasks) >= 1
        assert (skill_dir / "SKILL.md").is_file()
        assert skill_dir.name == "workdir-smoke"


# ---------------------------------------------------------------------------
# 8. Context refs — task-declared reference material in the grader prompt
# ---------------------------------------------------------------------------


class TestContextRefs:
    """load_context_refs reads task.ground_truth.context_refs into the prompt."""

    @pytest.fixture(autouse=True)
    def _import(self):
        try:
            from evals.framework.grader import (
                CONTEXT_REFS_CAP_BYTES,
                build_grader_prompt,
                load_context_refs,
            )

            self.load = load_context_refs
            self.build = build_grader_prompt
            self.cap = CONTEXT_REFS_CAP_BYTES
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_empty_list_returns_empty(self):
        """No refs → empty string → grader prompt omits the section."""
        assert self.load([]) == ""

    def test_missing_file_skipped_not_crashed(self, tmp_path, monkeypatch, capsys):
        """A typo in context_refs should warn + skip, not abort grading."""
        monkeypatch.chdir(tmp_path)
        out = self.load(["definitely-not-a-real-file.md"])
        assert out == ""

    def test_existing_file_included_with_fence(self, tmp_path, monkeypatch):
        """File content appears under '## Reference Material' with a markdown fence."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ref.md").write_text("# n8n best practices\n\nUse the CLI.\n")
        out = self.load(["ref.md"])
        assert "## Reference Material" in out
        assert "### ref.md" in out
        assert "```markdown" in out
        assert "n8n best practices" in out

    def test_caps_total_bytes_with_note(self, tmp_path, monkeypatch):
        """A single oversized ref should be truncated and the cap note appended."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "huge.md").write_text("x" * (self.cap + 5_000))
        out = self.load(["huge.md"])
        assert len(out) <= self.cap + 500  # allow for headers + note
        assert "file truncated" in out
        assert "byte cap" in out

    def test_build_grader_prompt_includes_refs(self):
        """When refs_content is passed, it appears in the final prompt before examples."""
        task = {"ground_truth": {"criteria": "use the CLI"}}
        refs = (
            "## Reference Material\n"
            "These files are authoritative...\n"
            "\n### docs/ref.md\n```markdown\nuse `pc index list`\n```\n"
        )
        prompt = self.build(
            task,
            conversation_json="convo",
            examples_block="## Calibration Examples\n\n### PASS",
            refs_content=refs,
        )
        ref_idx = prompt.index("## Reference Material")
        ex_idx = prompt.index("## Calibration Examples")
        conv_idx = prompt.index("## Agent Conversation")
        assert ref_idx < ex_idx < conv_idx


# ---------------------------------------------------------------------------
# 9. Remediation suggestions — actionable next steps on grader output
# ---------------------------------------------------------------------------


class TestRemediationSuggestions:
    """Grader emits a `suggestions` field with cause+fix entries for failures."""

    @pytest.fixture(autouse=True)
    def _import(self):
        try:
            from evals.framework.grader import _normalize_suggestions, build_grader_prompt

            self.normalize = _normalize_suggestions
            self.build = build_grader_prompt
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_prompt_asks_for_suggestions(self):
        """The grader prompt must request a `suggestions` field with cause+fix shape."""
        task = {"ground_truth": {"criteria": "do thing"}}
        prompt = self.build(task, "convo", "")
        assert "suggestions" in prompt
        assert "cause" in prompt and "fix" in prompt

    def test_normalizes_list_of_dicts(self):
        """Well-formed cause+fix dicts pass through with stripped fields."""
        raw = [
            {"cause": "missing flag  ", "fix": "  add --json"},
            {"cause": "wrong index", "fix": "use the integrated index"},
        ]
        out = self.normalize(raw)
        assert out == [
            {"cause": "missing flag", "fix": "add --json"},
            {"cause": "wrong index", "fix": "use the integrated index"},
        ]

    def test_normalizes_list_of_strings(self):
        """A list of bare strings becomes fix-only entries."""
        out = self.normalize(["add --json flag", "retry with the right index"])
        assert out == [
            {"cause": "", "fix": "add --json flag"},
            {"cause": "", "fix": "retry with the right index"},
        ]

    def test_normalizes_string_input(self):
        """A single string suggestion still produces one entry."""
        out = self.normalize("add the --json flag")
        assert out == [{"cause": "", "fix": "add the --json flag"}]

    def test_empty_inputs_return_empty_list(self):
        """None, empty list, empty string, and weird types all collapse to []."""
        assert self.normalize(None) == []
        assert self.normalize([]) == []
        assert self.normalize("") == []
        assert self.normalize(123) == []
        assert self.normalize({"cause": "x"}) == []

    def test_drops_empty_entries(self):
        """Entries with no cause and no fix get filtered out."""
        raw = [
            {"cause": "", "fix": ""},
            {"cause": "real cause", "fix": ""},
            "",
            {"cause": "", "fix": "real fix"},
        ]
        out = self.normalize(raw)
        assert out == [
            {"cause": "real cause", "fix": ""},
            {"cause": "", "fix": "real fix"},
        ]


# ---------------------------------------------------------------------------
# 10. cultivar hello preflight — doctor-style setup verification
# ---------------------------------------------------------------------------


class TestHelloPreflight:
    """_preflight aggregates setup checks; no I/O beyond fs/env so it's testable."""

    @pytest.fixture(autouse=True)
    def _import(self, monkeypatch, tmp_path):
        from evals import hello

        self.hello = hello
        # Pin home so .modal.toml presence is controllable per test.
        self._home = tmp_path / "home"
        self._home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: self._home)
        # Default: pretend the runner binaries exist on PATH so unrelated tests
        # don't fail just because gemini/copilot aren't installed.
        monkeypatch.setattr(hello.shutil, "which", lambda b: f"/fake/bin/{b}")

    def _row(self, rows, name_fragment):
        for r in rows:
            if name_fragment in r["name"]:
                return r
        raise AssertionError(f"no row matching {name_fragment!r} in {[r['name'] for r in rows]}")

    def test_minimal_local_no_grade(self, monkeypatch):
        """Local + --no-grade: only the runner-CLI check."""
        rows = self.hello._preflight("claude", grade=False, remote=False)
        assert len(rows) == 1
        assert self._row(rows, "claude CLI on PATH")["ok"] is True

    def test_runner_binary_missing(self, monkeypatch):
        """Missing runner binary fails the check and supplies an install hint."""
        monkeypatch.setattr(self.hello.shutil, "which", lambda b: None)
        rows = self.hello._preflight("gemini", grade=False, remote=False)
        r = self._row(rows, "gemini CLI on PATH")
        assert r["ok"] is False
        assert "npm install" in r["hint"]

    def test_grade_requires_anthropic_key(self, monkeypatch):
        """With grading on, missing ANTHROPIC_API_KEY is a blocking row."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        rows = self.hello._preflight("claude", grade=True, remote=False)
        r = self._row(rows, "ANTHROPIC_API_KEY")
        assert r["ok"] is False
        assert "--no-grade" in r["hint"]

    def test_grade_passes_when_key_present(self, monkeypatch):
        """Key set → grader row ok."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        rows = self.hello._preflight("claude", grade=True, remote=False)
        assert self._row(rows, "ANTHROPIC_API_KEY")["ok"] is True

    def test_remote_modal_auth_missing(self, monkeypatch):
        """No ~/.modal.toml and no MODAL_TOKEN_ID → Modal auth row fails."""
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        # Home is empty (set up in fixture), so .modal.toml doesn't exist.
        rows = self.hello._preflight("claude", grade=False, remote=True)
        auth = self._row(rows, "Modal auth")
        assert auth["ok"] is False
        assert "modal token new" in auth["hint"]

    def test_remote_modal_auth_via_token_env(self, monkeypatch):
        """MODAL_TOKEN_ID set is sufficient even without ~/.modal.toml."""
        monkeypatch.setenv("MODAL_TOKEN_ID", "tk-test")
        rows = self.hello._preflight("claude", grade=False, remote=True)
        assert self._row(rows, "Modal auth")["ok"] is True

    def test_remote_modal_auth_via_toml(self, monkeypatch):
        """~/.modal.toml present is sufficient."""
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        (self._home / ".modal.toml").write_text("[default]\n")
        rows = self.hello._preflight("claude", grade=False, remote=True)
        assert self._row(rows, "Modal auth")["ok"] is True


# ---------------------------------------------------------------------------
# 10. cultivar show — selector parsing + run discovery
# ---------------------------------------------------------------------------


class TestShowSelectors:
    """show parses base filenames and filters runs without touching the CLI layer."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from evals.show import _parse_base, discover_runs, filter_runs

        self.parse = _parse_base
        self.discover = discover_runs
        self.filter = filter_runs

    def test_parse_simple_base(self):
        """{task}__{variant} parses cleanly."""
        assert self.parse("hello-py__with-skill") == ("hello-py", "with-skill", 1)

    def test_parse_with_run_num(self):
        """{task}__{variant}__{N} extracts the run number when N is digit-only."""
        assert self.parse("hello-py__with-skill__3") == ("hello-py", "with-skill", 3)

    def test_parse_task_id_with_double_underscore(self):
        """A task id that itself contains '__' must not be split apart."""
        assert self.parse("foo__bar__without-skill") == ("foo__bar", "without-skill", 1)
        assert self.parse("foo__bar__without-skill__2") == ("foo__bar", "without-skill", 2)

    def _make_run(self, tmp_path, runner, base):
        """Helper: drop a fake .json/.md so discover_runs picks it up."""
        rdir = tmp_path / runner
        rdir.mkdir(exist_ok=True)
        (rdir / f"{base}.json").write_text("{}")
        (rdir / f"{base}.md").write_text(f"# {base}")
        return rdir

    def test_discover_skips_run_level_files(self, tmp_path):
        """tasks.json and grades.json sit at run root, not under a runner dir — should never appear in discover output."""
        (tmp_path / "tasks.json").write_text("{}")
        (tmp_path / "grades.json").write_text("[]")
        self._make_run(tmp_path, "claude", "hello-py__with-skill")
        runs = self.discover(tmp_path)
        names = [(r["runner"], r["task_id"]) for r in runs]
        assert names == [("claude", "hello-py")]

    def test_discover_recognizes_repeats(self, tmp_path):
        """Repeated runs (with __N suffix) come through with run_num populated."""
        self._make_run(tmp_path, "claude", "t__with-skill__1")
        self._make_run(tmp_path, "claude", "t__with-skill__2")
        runs = self.discover(tmp_path)
        assert {r["run_num"] for r in runs} == {1, 2}

    def test_filter_by_runner_task_variant(self, tmp_path):
        """Selectors compose: -r/-t/-v narrow to a single match."""
        self._make_run(tmp_path, "claude", "a__with-skill")
        self._make_run(tmp_path, "claude", "a__without-skill")
        self._make_run(tmp_path, "gemini", "a__with-skill")
        self._make_run(tmp_path, "claude", "b__with-skill")
        all_runs = self.discover(tmp_path)
        assert len(all_runs) == 4

        # narrow to claude/a/with-skill
        out = self.filter(all_runs, runner="claude", task="a", variant="with-skill")
        assert len(out) == 1
        assert out[0]["runner"] == "claude"
        assert out[0]["task_id"] == "a"
        assert out[0]["variant"] == "with-skill"

    def test_filter_by_run_num(self, tmp_path):
        """--num picks one specific repeat."""
        self._make_run(tmp_path, "claude", "t__with-skill__1")
        self._make_run(tmp_path, "claude", "t__with-skill__2")
        self._make_run(tmp_path, "claude", "t__with-skill__3")
        runs = self.discover(tmp_path)
        out = self.filter(runs, run_num=2)
        assert len(out) == 1
        assert out[0]["run_num"] == 2


class TestShowInlineMarkup:
    """_styled handles inline **bold** and `code` without breaking on stray chars."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from evals.show import _styled

        self.styled = _styled

    def test_plain_text_passes_through(self):
        out = self.styled("just words")
        assert out.plain == "  just words"

    def test_bold_marker_styled(self):
        """**X** segments become bold; the asterisks are stripped."""
        out = self.styled("you have **1 index**")
        assert out.plain == "  you have 1 index"
        # Find a span styled bold over the right characters.
        spans = [s for s in out.spans if "bold" in str(s.style)]
        assert any(out.plain[s.start : s.end] == "1 index" for s in spans)

    def test_backtick_code_styled(self):
        out = self.styled("run `pc index list` first")
        assert out.plain == "  run pc index list first"
        assert any(out.plain[s.start : s.end] == "pc index list" for s in out.spans if "cyan" in str(s.style))

    def test_mixed_bold_and_code(self):
        out = self.styled("**Name** is `ai-interviews`")
        assert out.plain == "  Name is ai-interviews"

    def test_unmatched_asterisks_left_alone(self):
        """A stray ** without a closing pair stays literal — don't break content."""
        out = self.styled("a ** stray marker")
        assert out.plain == "  a ** stray marker"


# ---------------------------------------------------------------------------
# 11. with-docs variant — third comparison axis using context_refs
# ---------------------------------------------------------------------------


class TestWithDocsVariantFilter:
    """variants_for_task filters with-docs out for tasks that have no context_refs."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from evals.run import variants_for_task

        self.filter = variants_for_task

    def test_drops_with_docs_when_refs_empty(self):
        """A task with no context_refs has nothing to feed the with-docs variant."""
        task = {"id": "t", "ground_truth": {"criteria": "x"}}
        out = self.filter(task, ["with-skill", "without-skill", "with-docs"])
        assert out == ["with-skill", "without-skill"]

    def test_keeps_with_docs_when_refs_present(self):
        """When context_refs is non-empty, with-docs is in scope."""
        task = {"id": "t", "ground_truth": {"criteria": "x", "context_refs": ["docs/foo.md"]}}
        out = self.filter(task, ["with-skill", "without-skill", "with-docs"])
        assert out == ["with-skill", "without-skill", "with-docs"]

    def test_explicit_with_docs_only_skipped_when_no_refs(self):
        """`-v with-docs` against a no-refs task yields no variants."""
        task = {"id": "t", "ground_truth": {"criteria": "x"}}
        out = self.filter(task, ["with-docs"])
        assert out == []

    def test_treats_empty_list_as_no_refs(self):
        """An explicit empty list shouldn't accidentally enable with-docs."""
        task = {"id": "t", "ground_truth": {"criteria": "x", "context_refs": []}}
        out = self.filter(task, ["with-skill", "with-docs"])
        assert out == ["with-skill"]


class TestRunnerRefsFraming:
    """load_runner_refs renders refs as an agent-prompt prefix, not grader framing."""

    @pytest.fixture(autouse=True)
    def _import(self):
        try:
            from evals.framework.grader import load_context_refs, load_runner_refs

            self.runner_refs = load_runner_refs
            self.grader_refs = load_context_refs
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_empty_refs_returns_empty(self):
        """No refs → empty string → caller skips with-docs."""
        assert self.runner_refs([]) == ""

    def test_runner_framing_distinct_from_grader(self, tmp_path, monkeypatch):
        """Runner prefix shouldn't say 'Reference Material' (that's the grader heading)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ref.md").write_text("# Docs\nuse `foo`\n")
        runner_out = self.runner_refs(["ref.md"])
        grader_out = self.grader_refs(["ref.md"])
        # Both contain the file content, but distinct framing.
        assert "use `foo`" in runner_out
        assert "use `foo`" in grader_out
        assert "## Reference Material" in grader_out
        assert "## Reference Material" not in runner_out
        # Runner prefix ends with a divider separating docs from the task intent.
        assert runner_out.endswith("---\n\n")

    def test_runner_prefix_includes_file_content(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ref.md").write_text("authoritative thing here")
        out = self.runner_refs(["ref.md"])
        assert "authoritative thing here" in out
        assert "### ref.md" in out


class TestRunnerWithDocsPrompt:
    """Each runner's build_command prepends docs_context for the with-docs variant."""

    def test_claude_with_docs_prepends_and_omits_bare(self):
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, prompt = r.build_command(
            "do the thing",
            "with-docs",
            max_turns=5,
            docs_context="DOCS HERE\n---\n\n",
        )
        assert prompt.startswith("DOCS HERE")
        assert prompt.endswith("do the thing")
        # --bare is intentionally NOT passed: it strips Write from the tool set
        # regardless of --allowedTools, which broke code-gen runs.
        assert "--bare" not in cmd
        # --allowedTools is a single comma-joined value (not multiple argv
        # tokens — the CLI silently drops all but the first in that form).
        allow_idx = cmd.index("--allowedTools")
        allowed = cmd[allow_idx + 1]
        assert isinstance(allowed, str) and "," in allowed, "--allowedTools must be one comma-joined arg"
        tools = allowed.split(",")
        assert "Write" in tools, f"Write missing from {tools}"
        assert "Edit" in tools
        assert "Skill" not in tools  # bare-flavor variants don't invoke skills

    def test_claude_without_skill_omits_bare(self):
        """Same --bare-omission story for without-skill."""
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command("do the thing", "without-skill", max_turns=5)
        assert "--bare" not in cmd
        tools = cmd[cmd.index("--allowedTools") + 1].split(",")
        assert "Write" in tools

    def test_claude_with_skill_allowed_tools_comma_joined(self):
        """with-skill also takes the comma-joined form and includes Skill+ToolSearch."""
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command("do the thing", "with-skill", max_turns=5)
        allow_idx = cmd.index("--allowedTools")
        tools = cmd[allow_idx + 1].split(",")
        assert {"Bash", "Read", "Write", "Edit", "Skill", "ToolSearch"}.issubset(set(tools))

    def test_copilot_with_docs_prepends_and_excludes_skill(self):
        from evals.runners.copilot import CopilotRunner

        r = CopilotRunner(skill_dir="/tmp/fake-skill")
        cmd, prompt = r.build_command(
            "do the thing",
            "with-docs",
            max_turns=5,
            docs_context="DOCS HERE\n---\n\n",
        )
        assert prompt.startswith("DOCS HERE")
        assert "--no-custom-instructions" in cmd
        # skill tool is excluded
        idx = cmd.index("--excluded-tools")
        assert cmd[idx + 1] == "skill"

    def test_gemini_with_docs_prepends(self):
        from evals.runners.gemini import GeminiRunner

        r = GeminiRunner(skill_dir="/tmp/fake-skill")
        cmd, prompt = r.build_command(
            "do the thing",
            "with-docs",
            max_turns=5,
            docs_context="DOCS HERE\n---\n\n",
        )
        assert prompt.startswith("DOCS HERE")
        # Prompt must round-trip into the cmd via -p
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == prompt

    def test_with_docs_without_context_falls_back_to_intent(self):
        """If docs_context is empty (e.g. orchestrator skipped resolution), don't crash."""
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, prompt = r.build_command("just do it", "with-docs", docs_context="")
        assert prompt == "just do it"

    def test_all_runners_advertise_with_docs(self):
        """variants() includes with-docs on each runner."""
        from evals.runners.claude import ClaudeRunner
        from evals.runners.copilot import CopilotRunner
        from evals.runners.gemini import GeminiRunner

        for cls in (ClaudeRunner, CopilotRunner, GeminiRunner):
            assert "with-docs" in cls(skill_dir=None).variants()


class TestExtraTools:
    """extra_tools (task YAML opt-in) unions into the variant's --allowedTools."""

    def test_claude_without_skill_unions_extra_tools(self):
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command(
            "do the thing", "without-skill", max_turns=5, extra_tools=["WebSearch", "WebFetch"]
        )
        tools = cmd[cmd.index("--allowedTools") + 1].split(",")
        assert {"Bash", "Read", "Write", "Edit", "WebSearch", "WebFetch"}.issubset(set(tools))

    def test_claude_extra_tools_none_is_unchanged(self):
        """Omitting extra_tools must not change the existing allow-list."""
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command("do the thing", "without-skill", max_turns=5)
        tools = set(cmd[cmd.index("--allowedTools") + 1].split(","))
        assert tools == {"Bash", "Read", "Write", "Edit"}

    def test_claude_extra_tools_no_duplicates(self):
        """Requesting a tool that's already in the base list doesn't duplicate it."""
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command("do the thing", "without-skill", max_turns=5, extra_tools=["Bash", "WebSearch"])
        tools = cmd[cmd.index("--allowedTools") + 1].split(",")
        assert tools.count("Bash") == 1
        assert "WebSearch" in tools

    def test_copilot_and_gemini_accept_and_ignore_extra_tools(self):
        """No matching mechanism yet on these runners — must not raise."""
        from evals.runners.copilot import CopilotRunner
        from evals.runners.gemini import GeminiRunner

        for cls in (CopilotRunner, GeminiRunner):
            r = cls(skill_dir="/tmp/fake-skill")
            cmd, _ = r.build_command("do the thing", "without-skill", max_turns=5, extra_tools=["WebSearch"])
            assert isinstance(cmd, list)


class TestModelOverride:
    """model (cultivar run --model, orchestration-level) appends --model to the Claude command."""

    def test_claude_model_appends_flag(self):
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command("do the thing", "without-skill", max_turns=5, model="claude-opus-5")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-opus-5"

    def test_claude_model_none_omits_flag(self):
        """Omitting model must not add --model at all (uses the CLI's own default)."""
        from evals.runners.claude import ClaudeRunner

        r = ClaudeRunner(skill_dir="/tmp/fake-skill")
        cmd, _ = r.build_command("do the thing", "without-skill", max_turns=5)
        assert "--model" not in cmd


class TestEmptyTraceAutofail:
    """_has_agent_signal: traces with no agent activity get autofailed before grading."""

    @pytest.fixture(autouse=True)
    def _import(self):
        try:
            from evals.framework.grader import _has_agent_signal

            self.has = _has_agent_signal
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_empty_string_no_signal(self):
        assert self.has("") is False

    def test_user_only_no_signal(self):
        """Just the User echo line — agent never spoke."""
        assert self.has("# with-skill\n\n**User:** do the thing\n") is False

    def test_assistant_text_is_signal(self):
        md = "# with-skill\n\n**User:** ...\n**Assistant:** I'll help\n"
        assert self.has(md) is True

    def test_tool_call_only_is_signal(self):
        """Agent wrote a file with no narration — still has signal via the tool call."""
        md = "# with-skill\n\n**User:** ...\n**Write:** `hello.py`\n"
        assert self.has(md) is True

    def test_final_result_only_is_signal(self):
        md = "# with-skill\n\n**User:** ...\n**Final result:** done\n"
        assert self.has(md) is True


class TestCodeGenEmptyWorkdirAutofail:
    """grade_one autofails code-gen tasks whose workdir is empty — no file =
    no deliverable, regardless of how good the conversation looked."""

    @pytest.fixture(autouse=True)
    def _import(self):
        try:
            from evals.framework.grader import grade_one

            self.grade = grade_one
        except ImportError:
            pytest.skip("anthropic SDK not installed")

    def test_code_gen_empty_workdir_autofails(self):
        task = {"id": "t", "category": "code-gen", "ground_truth": {"criteria": "x"}}
        # Conversation has signal (so the no-signal autofail doesn't fire), but
        # workdir_content is empty — no file written.
        conv = {"conversation_md": "**User:** ...\n**Bash:** `ls`\n**Output:**\n```\n\n```\n"}
        result = self.grade(
            client=None,  # ty: ignore[invalid-argument-type] -- intentional: triggers AttributeError as a proxy for "didn't short-circuit"
            model="x",
            task=task,
            conversation=conv,
            examples_block="",
            skill_content="",
            workdir_content="",
        )
        assert result["pass"] is False
        assert "no files" in result["reasoning"].lower() or "code-gen" in result["reasoning"].lower()
        assert result["suggestions"]
        assert "Write" in result["suggestions"][0]["fix"]

    def test_code_gen_empty_workdir_surfaces_run_error(self):
        """When the workdir is empty because the agent RUN errored (e.g. a bad
        API key), the autofail surfaces that error rather than the generic
        'check Write/Edit' suggestion."""
        task = {"id": "t", "category": "code-gen", "ground_truth": {"criteria": "x"}}
        conv = {"conversation_md": "**User:** write greeting.txt\n\n**Assistant:** Invalid API key · Fix external API key\n"}
        result = self.grade(
            client=None,  # ty: ignore[invalid-argument-type] -- intentional: autofail returns before using client
            model="x",
            task=task,
            conversation=conv,
            examples_block="",
            skill_content="",
            workdir_content="",
        )
        assert result["pass"] is False
        blob = (result["reasoning"] + result["suggestions"][0]["cause"] + result["suggestions"][0]["fix"]).lower()
        assert "invalid api key" in blob
        assert "write/edit" not in result["suggestions"][0]["fix"].lower()

    def test_code_gen_with_workdir_proceeds_to_grader(self, monkeypatch):
        """When workdir has content, we don't short-circuit — verified by the
        function trying to call client.messages.create and raising AttributeError
        on our None client. Don't actually grade; just confirm we didn't autofail."""
        task = {"id": "t", "category": "code-gen", "ground_truth": {"criteria": "x"}}
        conv = {"conversation_md": "**User:** ...\n**Write:** `hello.py`\n"}
        with pytest.raises(AttributeError):
            self.grade(
                client=None,  # ty: ignore[invalid-argument-type] -- intentional: triggers AttributeError as a proxy for "didn't short-circuit"
                model="x",
                task=task,
                conversation=conv,
                examples_block="",
                skill_content="",
                workdir_content="## Generated Code Files\n### hello.py\n```python\nprint('hi')\n```\n",
            )

    def test_non_code_gen_empty_workdir_proceeds(self):
        """A non-code-gen task with an empty workdir is fine — CLI tasks don't
        write files."""
        task = {"id": "t", "category": "cli", "ground_truth": {"criteria": "x"}}
        conv = {"conversation_md": "**User:** ...\n**Bash:** `pc index list`\n"}
        with pytest.raises(AttributeError):
            self.grade(
                client=None,  # ty: ignore[invalid-argument-type] -- intentional: triggers AttributeError as a proxy for "didn't short-circuit"
                model="x",
                task=task,
                conversation=conv,
                examples_block="",
                skill_content="",
                workdir_content="",
            )


# ---------------------------------------------------------------------------
# Orchestrator call-surface guards — catch the class of bug where a second
# caller (e.g. hello.py) doesn't pass a newly-required argument to
# run_local / run_remote. The actual incident: a `timeout` param was added
# without a default and hello.py kept calling without it; the next run blew
# up with TypeError. Both guards below would have caught it.
# ---------------------------------------------------------------------------


class TestOrchestratorCallSurface:
    def test_hello_calls_run_helpers_with_compatible_args(self):
        """hello.py's calls to run_local / run_remote must match the signatures.

        Walks hello.py's AST, finds calls to those two functions, and uses
        inspect.Signature.bind to confirm the args are valid — i.e. no unknown
        kwargs and no missing required-without-default params. This is the
        direct guard for the bug that hit us: a new param was added to
        run_remote without a default and hello.py didn't get the update.
        """
        import ast
        import inspect
        from pathlib import Path

        from evals.run import run_local, run_remote

        targets = {"run_local": run_local, "run_remote": run_remote}
        hello_src = Path(__file__).parent.parent / "evals" / "hello.py"
        tree = ast.parse(hello_src.read_text())

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id not in targets:
                continue
            fn = targets[node.func.id]
            # Use a sentinel for each positional arg; we only care whether the
            # arity + kwarg names match the signature, not the actual values.
            posargs = [object()] * len(node.args)
            kwargs = {kw.arg: object() for kw in node.keywords if kw.arg}
            try:
                inspect.signature(fn).bind(*posargs, **kwargs)
            except TypeError as e:
                raise AssertionError(
                    f"hello.py call to {fn.__name__}() doesn't match its signature: {e}. "
                    f"Signature: {inspect.signature(fn)}"
                ) from None

    def test_dry_run_smoke(self, monkeypatch, tmp_path):
        """`cultivar run -s workdir-smoke --dry-run` exits 0 and prints the prompt.

        Exercises the Typer wiring + load_tasks + build_command path without
        touching any CLI subprocess. Run from the package root so the bundled
        smoke task and skill are discoverable.
        """
        from typer.testing import CliRunner

        from evals.cli import app

        # `cultivar run` resolves tasks/<skill>.yaml and .claude/skills/<skill>/
        # relative to cwd. The committed workdir-smoke fixtures live at the repo
        # root, so chdir there for the duration of the test.
        repo_root = Path(__file__).parent.parent
        monkeypatch.chdir(repo_root)

        result = CliRunner().invoke(app, ["run", "-s", "workdir-smoke", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Agent Prompt" in result.output
        assert "CLI Command" in result.output

    def test_run_local_mounts_skill_into_agent_cwd(self, tmp_path, monkeypatch):
        """For with-skill, the skill must end up at <agent-cwd>/.claude/skills/<name>/.

        Direct cause of an onboarding-time incident: the local path mounted
        nothing, so Claude Code couldn't resolve the `Use the /<skill>` prompt
        prefix, the agent improvised, and code-gen smokes silently failed
        with empty workdirs. The remote path (modal_runner) already mounts the
        skill into /workspace/.claude/skills/; this test locks the local path
        to the same contract.
        """
        from unittest.mock import patch

        from evals import run as run_module
        from evals.run import run_local

        # Fake skill on disk
        skill_src = tmp_path / "skills" / "fakeskill"
        skill_src.mkdir(parents=True)
        (skill_src / "SKILL.md").write_text("# fakeskill\n")

        seen_cwds: list[str] = []

        class _Runner:
            name = "fake"

            def __init__(self, skill_dir: str):
                self.skill_dir = skill_dir

            def variants(self):
                return ["with-skill", "without-skill"]

            def run(
                self, intent, variant, max_turns=10, cwd=None, docs_context="", timeout=60, extra_tools=None, model=None
            ):
                # Capture the cwd as it exists at agent-invocation time, before
                # the orchestrator cleans up the tempdir.
                assert cwd is not None, "run_local must pass a cwd to the runner"
                seen_cwds.append(cwd)
                # Snapshot whether the skill is mounted at the expected path
                mount = Path(cwd) / ".claude" / "skills" / "fakeskill" / "SKILL.md"
                return {"conversation_md": f"mount_exists={mount.exists()}"}

        # Point run_local at a real run_dir so save_result works
        run_dir = tmp_path / "results"
        run_dir.mkdir()
        tasks = [{"id": "t", "intent": "do thing", "ground_truth": {"criteria": "x"}}]

        with patch.object(run_module, "docs_context_for_task", return_value=""):
            run_local(
                tasks=tasks,
                runner_cls=_Runner,
                variants=["with-skill", "without-skill"],
                skill_dir=str(skill_src),
                max_turns=1,
                repeat=1,
                run_dir=run_dir,
            )

        # Both variants called .run() once each
        assert len(seen_cwds) == 2

        # with-skill conversation should record mount_exists=True
        with_skill_json = (run_dir / "fake" / "t__with-skill.json").read_text()
        assert "mount_exists=True" in with_skill_json, (
            "with-skill ran but the skill was not mounted at <cwd>/.claude/skills/<name>/. "
            "This is the local mirror of modal_runner's image.add_local_dir; without it, "
            "Claude Code can't discover the skill and the /<skill> prompt prefix dangles."
        )

        # without-skill must NOT see the skill (would contaminate the baseline)
        without_skill_json = (run_dir / "fake" / "t__without-skill.json").read_text()
        assert "mount_exists=False" in without_skill_json, (
            "without-skill saw the skill mounted in its cwd. The baseline variant must run "
            "with no skill discoverable; otherwise the with/without delta isn't honest."
        )


class TestResolveSkillsBase:
    """resolve_skills_base() precedence: --skills-dir flag > CULTIVAR_SKILLS_DIR env > ./.claude/skills."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from evals.framework.reporting import resolve_skills_base

        self.resolve = resolve_skills_base

    def test_default_is_dot_claude_skills(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CULTIVAR_SKILLS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert self.resolve("") == tmp_path / ".claude" / "skills"

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CULTIVAR_SKILLS_DIR", "skills")
        monkeypatch.chdir(tmp_path)
        assert self.resolve("") == tmp_path / "skills"

    def test_flag_overrides_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CULTIVAR_SKILLS_DIR", "skills")
        monkeypatch.chdir(tmp_path)
        assert self.resolve("from-flag") == tmp_path / "from-flag"

    def test_absolute_value_preserved(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CULTIVAR_SKILLS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        abs_dir = tmp_path / "elsewhere" / "skills"
        assert self.resolve(str(abs_dir)) == abs_dir


# ---------------------------------------------------------------------------
# CI gate — results dir resolution, pass-rate math, and the threshold verdict
# ---------------------------------------------------------------------------


class TestResolveResultsBase:
    """CI points results at $RUNNER_TEMP; getting this wrong writes into the checkout."""

    @staticmethod
    def resolve():
        from evals.framework.reporting import resolve_results_base

        return resolve_results_base()

    def test_defaults_to_cwd_results(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CULTIVAR_RESULTS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert self.resolve() == tmp_path / "results"

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CULTIVAR_RESULTS_DIR", "out")
        monkeypatch.chdir(tmp_path)
        assert self.resolve() == tmp_path / "out"

    def test_absolute_env_value_preserved(self, tmp_path, monkeypatch):
        elsewhere = tmp_path / "elsewhere" / "out"
        monkeypatch.setenv("CULTIVAR_RESULTS_DIR", str(elsewhere))
        monkeypatch.chdir(tmp_path)
        assert self.resolve() == elsewhere


def _grades(*specs):
    """Build minimal grade rows: each spec is (variant, passed)."""
    return [
        {"pass": ok, "variant": v, "task_id": f"t{i}", "runner": "claude", "run_num": 1}
        for i, (v, ok) in enumerate(specs)
    ]


class TestGatePassRate:
    """The baseline variant is expected to fail; letting it into the denominator
    would make every threshold meaningless."""

    @staticmethod
    def rate(grades, variant="with-skill"):
        from evals.framework.reporting import gate_pass_rate

        return gate_pass_rate(grades, variant)

    def test_counts_only_the_named_variant(self):
        g = _grades(
            ("with-skill", True),
            ("with-skill", True),
            ("with-skill", False),
            ("without-skill", False),
            ("without-skill", False),
        )
        rate, passed, total = self.rate(g)
        assert (passed, total) == (2, 3)
        assert rate == pytest.approx(66.67, abs=0.01)

    def test_naive_overall_rate_would_differ(self):
        """Guards the whole point of the variant filter: 2/5 != 2/3."""
        g = _grades(
            ("with-skill", True),
            ("with-skill", True),
            ("with-skill", False),
            ("without-skill", False),
            ("without-skill", False),
        )
        assert self.rate(g)[0] != pytest.approx(40.0)

    def test_all_passing_is_one_hundred(self):
        assert self.rate(_grades(("with-skill", True), ("with-skill", True)))[0] == 100.0

    def test_empty_variant_yields_zero_not_crash(self):
        assert self.rate(_grades(("without-skill", True))) == (0.0, 0, 0)

    def test_other_variant_selectable(self):
        g = _grades(("with-docs", True), ("with-docs", False))
        assert self.rate(g, "with-docs") == (50.0, 1, 2)


class TestGateVerdict:
    """Exit-code decision. Before this existed, a failing eval still exited 0."""

    @staticmethod
    def verdict(grades, threshold, variant="with-skill"):
        from evals.framework.reporting import gate_verdict

        return gate_verdict(grades, threshold, variant)

    def test_above_threshold_passes(self):
        ok, msg = self.verdict(_grades(("with-skill", True), ("with-skill", True)), 80.0)
        assert ok
        assert "100.0%" in msg

    def test_below_threshold_fails(self):
        ok, msg = self.verdict(_grades(("with-skill", True), ("with-skill", False)), 80.0)
        assert not ok
        assert "50.0%" in msg and "80.0%" in msg

    def test_exactly_at_threshold_passes(self):
        """>= not >, so --fail-under 50 on a 50% run is green."""
        ok, _ = self.verdict(_grades(("with-skill", True), ("with-skill", False)), 50.0)
        assert ok

    def test_no_grades_fails_rather_than_vacuously_passing(self):
        ok, msg = self.verdict([], 0.0)
        assert not ok
        assert "no with-skill grades" in msg

    def test_baseline_only_run_fails(self):
        """A run where the with-skill variant never executed must not read as green."""
        ok, _ = self.verdict(_grades(("without-skill", True)), 0.0)
        assert not ok


class TestReportFormats:
    """md/json are consumed by CI, so their shape is a contract."""

    @staticmethod
    def summarize(grades):
        from evals.framework.report import _summarize

        return _summarize(grades)

    def test_json_summary_shape(self):
        s = self.summarize(_grades(("with-skill", True), ("with-skill", False)))
        assert s["total_conversations"] == 2
        assert s["variants"]["with-skill"] == {"pass_rate": 50.0, "passed": 1, "total": 2}
        assert len(s["failures"]) == 1

    def test_variants_ordered_predictably(self):
        s = self.summarize(_grades(("without-skill", False), ("with-skill", True)))
        assert list(s["variants"]) == ["with-skill", "without-skill"]

    def test_unknown_variant_still_reported(self):
        s = self.summarize(_grades(("with-skill", True), ("custom", False)))
        assert "custom" in s["variants"]

    def test_md_renders_table_and_failures(self):
        from evals.framework.report import _render_md

        md = _render_md(_grades(("with-skill", False)), "run-x")
        assert "run-x" in md
        assert "| `with-skill` | 0.0% | 0 | 1 |" in md
        assert "1 failure(s)" in md

    def test_md_says_no_failures_when_clean(self):
        from evals.framework.report import _render_md

        assert "No failures." in _render_md(_grades(("with-skill", True)), "run-y")
