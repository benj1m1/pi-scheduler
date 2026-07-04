import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("PI_SCHEDULER_CRON_FILE", "/tmp/pi-agent-jobs-test")

from starlette.requests import Request

from app import config, cron, db, pi_models, retention, runner, run_users
from app import main as web
from app import work_window


def test_slugify():
    assert db.slugify("ServiceNow SOS Check") == "servicenow-sos-check"
    assert db.slugify("!!!") == "job"


def test_form_data_forces_overlap_prevention():
    data = web.form_data(
        "pi-agent",
        "check logs",
        "5",
        "minutes",
        "",
        "summary",
        "no_session",
        "full",
        "",
        "",
        "240",
        "",
        "on",
        None,
        "none",
        "",
    )

    assert data["prevent_overlap"] == 1
    assert data["skills_mode"] == "none"
    assert data["skill_paths"] == ""


def test_form_data_accepts_selected_approved_skill_ids():
    data = web.form_data(
        "pi-agent",
        "check logs",
        "5",
        "minutes",
        "",
        "summary",
        "no_session",
        "full",
        "",
        "",
        "240",
        "",
        "on",
        None,
        "approved",
        ["pdf", "obsidian-markdown"],
    )

    assert data["skills_mode"] == "approved"
    assert data["skill_ids"] == "pdf\nobsidian-markdown"
    assert data["skill_paths"] == ""


def test_validate_job_form_accepts_existing_catalog_skill_ids(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    data = {
        "name": "agent",
        "task_prompt": "check logs",
        "cron_expr": "*/5 * * * *",
        "schedule_error": None,
        "output_mode": "summary",
        "session_mode": "no_session",
        "tool_mode": "full",
        "skills_mode": "approved",
        "skill_ids": "pdf",
        "run_user": "",
        "timeout_seconds": "240",
    }

    assert "Approved skill" not in "\n".join(web.validate_job_form(data))


def test_validate_job_form_rejects_missing_catalog_skill_ids(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    root.mkdir()
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    data = {
        "name": "agent",
        "task_prompt": "check logs",
        "cron_expr": "*/5 * * * *",
        "schedule_error": None,
        "output_mode": "summary",
        "session_mode": "no_session",
        "tool_mode": "full",
        "skills_mode": "approved",
        "skill_ids": "pdf",
        "run_user": "",
        "timeout_seconds": "240",
    }

    assert "Approved skill 'pdf' is not available" in web.validate_job_form(data)


def test_database_persists_skill_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "skills_mode": "approved",
            "skill_ids": "pdf\nobsidian-markdown",
        }
    )

    job = db.get_job(job_id)
    assert job["skill_ids"] == "pdf\nobsidian-markdown"


def test_job_form_data_includes_run_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    data = web.form_data(
        "agent",
        "check logs",
        "5",
        "minutes",
        "",
        "summary",
        "no_session",
        "full",
        "",
        "",
        "240",
        "piagent",
        "on",
        None,
        "none",
        "",
    )

    assert data["run_user"] == "piagent"
    assert web.validate_job_form(data) == []


def test_approved_skills_catalog_lists_valid_directories(tmp_path, monkeypatch):
    from app import approved_skills

    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Work with PDFs\n---\n\n# PDF\n",
        encoding="utf-8",
    )
    (root / "obsidian-markdown").mkdir()
    (root / "obsidian-markdown" / "SKILL.md").write_text(
        "---\nname: obsidian-markdown\ndescription: Work with Obsidian markdown\n---\n",
        encoding="utf-8",
    )
    (root / "missing-skill-md").mkdir()
    (root / "bad id").mkdir()
    (root / "bad id" / "SKILL.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
    (root / "file-skill").write_text("not a directory", encoding="utf-8")

    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    entries = approved_skills.list_skills()

    assert [entry.id for entry in entries] == ["obsidian-markdown", "pdf"]
    assert entries[0].name == "obsidian-markdown"
    assert entries[1].description == "Work with PDFs"


def test_approved_skills_catalog_rejects_traversal_absolute_and_missing(tmp_path, monkeypatch):
    from app import approved_skills

    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    assert approved_skills.resolve_skill_path("pdf") == (root / "pdf").resolve()

    for value in ["../pdf", "/tmp/pdf", "bad id", ".hidden", "missing"]:
        try:
            approved_skills.resolve_skill_path(value)
        except approved_skills.SkillCatalogError as exc:
            assert str(exc)
        else:
            raise AssertionError(f"expected {value!r} to be rejected")


def test_approved_skills_catalog_rejects_symlink_entries(tmp_path, monkeypatch):
    from app import approved_skills

    root = tmp_path / "approved-skills"
    external = tmp_path / "external"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: external\n---\n", encoding="utf-8")
    root.mkdir()
    (root / "external").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    assert approved_skills.list_skills() == []
    try:
        approved_skills.resolve_skill_path("external")
    except approved_skills.SkillCatalogError as exc:
        assert "not an approved skill" in str(exc)
    else:
        raise AssertionError("expected symlinked skill to be rejected")


def test_parse_skill_ids_normalizes_repeated_and_newline_values():
    from app import approved_skills

    assert approved_skills.parse_skill_ids(None) == []
    assert approved_skills.parse_skill_ids("pdf\nobsidian-markdown\n") == ["pdf", "obsidian-markdown"]
    assert approved_skills.parse_skill_ids(["pdf", "", "obsidian-markdown"]) == ["pdf", "obsidian-markdown"]


def test_job_form_context_includes_catalog_and_selected_skill_ids(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\ndescription: PDFs\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    context = web.job_form_context(
        request=None,
        job={
            "name": "agent",
            "task_prompt": "check logs",
            "schedule_every": "5",
            "schedule_unit": "minutes",
            "skills_mode": "approved",
            "skill_ids": "pdf\nmissing",
        },
        errors=[],
        action="/jobs/new",
        title="New Job",
    )

    assert [entry.id for entry in context["approved_skills"]] == ["pdf"]
    assert context["selected_skill_ids"] == {"pdf", "missing"}
    assert context["missing_skill_ids"] == ["missing"]


def test_group_form_data_includes_run_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    data = web.group_form_data(
        "flow",
        "5",
        "minutes",
        "",
        "",
        "piagent",
        "on",
        None,
        ["job-a"],
    )

    assert data["run_user"] == "piagent"
    assert "Run user" not in "\n".join(web.validate_group_form(data))


def test_validate_job_form_rejects_invalid_run_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    data = web.form_data(
        "agent",
        "check logs",
        "5",
        "minutes",
        "",
        "summary",
        "no_session",
        "full",
        "",
        "",
        "240",
        "bad user",
        "on",
        None,
        "none",
        "",
    )

    assert "Run user is invalid" in web.validate_job_form(data)


def test_db_forces_overlap_prevention(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 0,
        }
    )

    assert db.get_job(job_id)["prevent_overlap"] == 1

    db.update_job(
        job_id,
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/10 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 0,
        },
    )
    assert db.get_job(job_id)["prevent_overlap"] == 1

    with db.connect() as conn:
        conn.execute("update jobs set prevent_overlap = 0 where id = ?", (job_id,))
    db.init_db()

    assert db.get_job(job_id)["prevent_overlap"] == 1


def test_db_defaults_new_jobs_to_summary_without_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    job = db.get_job(job_id)

    assert job["output_mode"] == "summary"
    assert job["session_mode"] == "no_session"
    assert job["tool_mode"] == "full"


def test_db_creates_ordered_job_groups_and_blocks_member_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    agent_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    qa_id = db.create_job(
        {
            "name": "qa",
            "skill_name": "general",
            "task_prompt": "run qa",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1},
        [agent_id, qa_id],
    )
    group = db.get_group_with_members(group_id)

    assert group["prevent_overlap"] == 1
    assert [member["job_id"] for member in group["members"]] == [agent_id, qa_id]
    assert [member["position"] for member in group["members"]] == [1, 2]

    try:
        db.create_group(
            {"name": "bad flow", "cron_expr": "*/10 * * * *", "enabled": 1},
            [agent_id, agent_id],
        )
    except ValueError as exc:
        assert "only appear once" in str(exc)
    else:
        raise AssertionError("Expected duplicate member rejection")

    try:
        db.soft_delete_job(agent_id)
    except ValueError as exc:
        assert "review flow" in str(exc)
    else:
        raise AssertionError("Expected referenced job delete rejection")


def test_group_form_starts_without_empty_member_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    request = Request({"type": "http", "method": "GET", "path": "/groups/new", "headers": []})
    context = web.group_form_context(
        request,
        {
            "name": "",
            "schedule_every": "5",
            "schedule_unit": "minutes",
            "work_start": "",
            "work_end": "",
            "enabled": 1,
            "member_job_ids": [],
        },
        [],
        "/groups",
        "New Job Group",
    )

    assert context["member_job_ids"] == []
    assert "member_slots" not in context


def test_db_init_creates_performance_indexes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()

    with db.connect() as conn:
        rows = conn.execute(
            """
            select name, sql
            from sqlite_master
            where type = 'index' and name in (
              'idx_jobs_deleted_created_at',
              'idx_runs_job_active_started_at',
              'idx_runs_running_job',
              'idx_runs_job_source_active_started_at'
            )
            """
        ).fetchall()

    indexes = {row["name"]: row["sql"] for row in rows}

    assert set(indexes) == {
        "idx_jobs_deleted_created_at",
        "idx_runs_job_active_started_at",
        "idx_runs_running_job",
        "idx_runs_job_source_active_started_at",
    }
    assert "where status != 'disabled'" in indexes["idx_runs_job_active_started_at"].lower()
    assert "where status = 'running'" in indexes["idx_runs_running_job"].lower()


def test_db_migrates_existing_jobs_to_events_with_saved_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    with db.connect() as conn:
        conn.execute(
            """
            create table jobs (
              id text primary key,
              name text not null,
              skill_name text not null,
              task_prompt text not null,
              cron_expr text not null,
              enabled integer not null default 1,
              timeout_seconds integer not null default 240,
              prevent_overlap integer not null default 1,
              created_at text not null,
              updated_at text not null,
              deleted_at text
            )
            """
        )
        conn.execute(
            """
            insert into jobs (
              id, name, skill_name, task_prompt, cron_expr, enabled, timeout_seconds,
              prevent_overlap, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "Legacy Job",
                "general",
                "check logs",
                "*/5 * * * *",
                1,
                240,
                1,
                "2026-06-27T14:00:00Z",
                "2026-06-27T14:00:00Z",
            ),
        )

    db.init_db()

    job = db.get_job("legacy-job")
    assert job["output_mode"] == "events"
    assert job["session_mode"] == "save"
    assert job["tool_mode"] == "full"


def test_db_migrates_existing_runs_for_group_context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    with db.connect() as conn:
        conn.executescript(
            """
            create table jobs (
              id text primary key,
              name text not null,
              skill_name text not null,
              task_prompt text not null,
              cron_expr text not null,
              enabled integer not null default 1,
              timeout_seconds integer not null default 240,
              prevent_overlap integer not null default 1,
              output_mode text not null default 'summary',
              session_mode text not null default 'no_session',
              tool_mode text not null default 'full',
              created_at text not null,
              updated_at text not null,
              deleted_at text
            );
            create table runs (
              id text primary key,
              job_id text not null,
              started_at text not null,
              finished_at text,
              status text not null,
              exit_code integer,
              duration_ms integer,
              command text not null,
              stdout_path text,
              stderr_path text,
              jsonl_path text,
              error_summary text,
              foreign key (job_id) references jobs(id)
            );
            """
        )

    db.init_db()

    with db.connect() as conn:
        run_columns = {row[1] for row in conn.execute("pragma table_info(runs)").fetchall()}
        index = conn.execute(
            "select name from sqlite_master where type = 'index' and name = 'idx_runs_group_run_id'"
        ).fetchone()

        assert "source" in run_columns
        assert "group_run_id" in run_columns
    assert index is not None


def test_db_migrates_existing_groups_for_failure_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    with db.connect() as conn:
        conn.executescript(
            """
            create table job_groups (
              id text primary key,
              name text not null,
              cron_expr text not null,
              enabled integer not null default 1,
              prevent_overlap integer not null default 1,
              work_start text,
              work_end text,
              created_at text not null,
              updated_at text not null,
              deleted_at text
            );
            insert into job_groups (
              id, name, cron_expr, enabled, prevent_overlap, created_at, updated_at
            ) values ('legacy-flow', 'Legacy Flow', '*/5 * * * *', 1, 1, '2026-06-27T14:00:00Z', '2026-06-27T14:00:00Z');
            """
        )

    db.init_db()

    group = db.get_group("legacy-flow")
    assert group["continue_on_failure"] == 0


def test_build_command_uses_argv():
    argv, display = runner.build_command(
        {"task_prompt": "Run the servicenow-agent skill"}
    )
    assert argv[0] == "pi"
    assert argv[1:4] == ["--no-skills", "--mode", "json"]
    assert argv[4] == "Run the servicenow-agent skill"
    assert display.startswith("pi --no-skills --mode json")


def test_build_command_supports_summary_without_session():
    argv, display = runner.build_command(
        {
            "name": "pi-agent",
            "task_prompt": "summarize status",
            "output_mode": "summary",
            "session_mode": "no_session",
        }
    )

    assert argv == [
        "pi",
        "--no-skills",
        "--no-session",
        "--name",
        "pi-scheduler: pi-agent",
        "-p",
        "summarize status",
    ]
    assert "--no-session" in display
    assert "-p" in display
    assert "--mode json" not in display


def test_build_command_supports_read_only_tools():
    argv, display = runner.build_command(
        {
            "name": "pi-agent",
            "task_prompt": "review status",
            "output_mode": "summary",
            "session_mode": "no_session",
            "tool_mode": "read_only",
        }
    )

    assert argv == [
        "pi",
        "--no-skills",
        "--no-session",
        "--tools",
        "read,grep,find,ls",
        "--name",
        "pi-scheduler: pi-agent",
        "-p",
        "review status",
    ]
    assert "--tools read,grep,find,ls" in display


def test_build_command_supports_no_tools():
    argv, display = runner.build_command(
        {
            "task_prompt": "summarize status",
            "output_mode": "summary",
            "session_mode": "no_session",
            "tool_mode": "no_tools",
        }
    )

    assert argv == ["pi", "--no-skills", "--no-session", "--no-tools", "-p", "summarize status"]
    assert "--no-tools" in display


def test_validate_job_form_rejects_invalid_tool_mode():
    data = {
        "name": "pi-agent",
        "task_prompt": "check logs",
        "cron_expr": "*/5 * * * *",
        "provider_name": None,
        "model_id": None,
        "work_start": None,
        "work_end": None,
        "output_mode": "summary",
        "session_mode": "no_session",
        "tool_mode": "write_only",
        "skills_mode": "none",
        "skill_paths": "",
        "timeout_seconds": "240",
    }

    assert "Tool access is invalid" in web.validate_job_form(data)


def test_build_command_disables_skills_by_default():
    argv, display = runner.build_command(
        {
            "task_prompt": "summarize status",
            "output_mode": "summary",
            "session_mode": "no_session",
        }
    )

    assert argv == ["pi", "--no-skills", "--no-session", "-p", "summarize status"]
    assert "--no-skills" in display


def test_build_command_loads_only_approved_catalog_skills(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    (root / "pdf").mkdir(parents=True)
    (root / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
    (root / "obsidian-markdown").mkdir()
    (root / "obsidian-markdown" / "SKILL.md").write_text("---\nname: obsidian-markdown\n---\n", encoding="utf-8")
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    argv, display = runner.build_command(
        {
            "task_prompt": "summarize status",
            "output_mode": "summary",
            "session_mode": "no_session",
            "skills_mode": "approved",
            "skill_ids": "pdf\nobsidian-markdown",
        }
    )

    assert argv == [
        "pi",
        "--no-skills",
        "--skill",
        str((root / "pdf").resolve()),
        "--skill",
        str((root / "obsidian-markdown").resolve()),
        "--no-session",
        "-p",
        "summarize status",
    ]
    assert f"--skill {root / 'pdf'}" in display


def test_build_command_rejects_missing_approved_catalog_skill(tmp_path, monkeypatch):
    root = tmp_path / "approved-skills"
    root.mkdir()
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    try:
        runner.build_command(
            {
                "task_prompt": "summarize status",
                "skills_mode": "approved",
                "skill_ids": "missing",
            }
        )
    except runner.RunnerConfigError as exc:
        assert "Approved skill 'missing' is not available" in str(exc)
    else:
        raise AssertionError("expected missing catalog skill to fail")


def test_build_command_can_use_runtime_default_skills():
    argv, _ = runner.build_command(
        {
            "task_prompt": "summarize status",
            "output_mode": "summary",
            "session_mode": "no_session",
            "skills_mode": "runtime",
        }
    )

    assert "--no-skills" not in argv
    assert "--skill" not in argv


def test_list_configured_models_omits_provider_secrets(tmp_path, monkeypatch):
    models_file = tmp_path / "models.json"
    models_file.write_text(
        '{"providers":{"local-llama":{"baseUrl":"https://example.invalid","apiKey":"dummy-api-key",'
        '"models":[{"id":"qwen-local","name":"Qwen Local"}]}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PI_MODELS_FILE", models_file)

    options = pi_models.list_configured_models()

    assert options == [
        {
            "provider": "local-llama",
            "id": "qwen-local",
            "name": "Qwen Local",
            "value": pi_models.encode_selection("local-llama", "qwen-local"),
        }
    ]
    assert "apiKey" not in options[0]
    assert "baseUrl" not in options[0]


def test_build_command_includes_configured_provider_model(tmp_path, monkeypatch):
    models_file = tmp_path / "models.json"
    models_file.write_text(
        '{"providers":{"local-llama":{"models":[{"id":"qwen-local"}]}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PI_MODELS_FILE", models_file)

    argv, display = runner.build_command(
        {
            "task_prompt": "custom prompt",
            "provider_name": "local-llama",
            "model_id": "qwen-local",
        }
    )

    assert argv[1:8] == ["--no-skills", "--mode", "json", "--provider", "local-llama", "--model", "qwen-local"]
    assert argv[8] == "custom prompt"
    assert "--provider local-llama --model qwen-local" in display


def test_build_command_rejects_unconfigured_provider_model(tmp_path, monkeypatch):
    models_file = tmp_path / "models.json"
    models_file.write_text('{"providers":{"local-llama":{"models":[{"id":"qwen-local"}]}}}', encoding="utf-8")
    monkeypatch.setattr(config, "PI_MODELS_FILE", models_file)

    try:
        runner.build_command(
            {
                "task_prompt": "custom prompt",
                "provider_name": "other-provider",
                "model_id": "qwen-local",
            }
        )
    except pi_models.ModelConfigError as exc:
        assert "not configured" in str(exc)
    else:
        raise AssertionError("Expected ModelConfigError")


def test_pi_events_to_transcript_includes_thinking_and_tools():
    events = "\n".join(
        [
            '{"type":"agent_start"}',
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"plan work"},{"type":"text","text":"summary"}]}}',
            '{"type":"tool_execution_end","toolName":"bash","result":{"output":"ok"},"isError":false}',
        ]
    )

    transcript = runner.pi_events_to_transcript(events)

    assert "[thinking]" in transcript
    assert "plan work" in transcript
    assert "summary" in transcript
    assert "[tool end:bash error=False]" in transcript
    assert '"output": "ok"' in transcript


def test_run_user_allowlist_defaults_to_cron_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "", raising=False)

    assert run_users.allowed_run_users() == ["root"]
    assert run_users.effective_run_user(None) == "root"
    assert run_users.effective_run_user("") == "root"
    assert run_users.describe_run_user(None) == "default (root)"


def test_validate_run_user_rejects_unsafe_and_unallowed_users(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    run_users.validate_run_user(None)
    run_users.validate_run_user("piagent")

    for value in ["bad user", "bad;user", "../root"]:
        try:
            run_users.validate_run_user(value)
        except run_users.RunUserError as exc:
            assert "invalid" in str(exc).lower()
        else:
            raise AssertionError(f"Expected invalid username rejection for {value}")

    try:
        run_users.validate_run_user("bjli")
    except run_users.RunUserError as exc:
        assert "not allowed" in str(exc).lower()
    else:
        raise AssertionError("Expected allowlist rejection")


def test_validate_run_user_rejects_missing_system_user(monkeypatch):
    from app import run_users

    def missing_user(name):
        raise KeyError(name)

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,ghost", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", missing_user)

    try:
        run_users.validate_run_user("ghost")
    except run_users.RunUserError as exc:
        assert "does not exist" in str(exc).lower()
    else:
        raise AssertionError("Expected missing system user rejection")


def test_render_cron_file_adds_discovered_pi_node_bin_to_path(tmp_path, monkeypatch):
    pi_bin = tmp_path / "pi-node" / "node-v1" / "bin"
    pi_bin.mkdir(parents=True)
    pi_binary = pi_bin / "pi"
    pi_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    pi_binary.chmod(0o755)

    monkeypatch.setattr(config, "PI_BINARY", "pi")
    monkeypatch.setattr(config, "PI_NODE_ROOT", tmp_path / "pi-node", raising=False)
    monkeypatch.setattr(config, "DEFAULT_CRON_PATH", "/usr/local/bin:/usr/bin:/bin", raising=False)

    content = cron.render_cron_file([], [])

    path_line = next(line for line in content.splitlines() if line.startswith("PATH="))
    assert str(pi_bin) in path_line.split("=")[1].split(":")
    assert path_line.index(str(pi_bin)) < path_line.index("/usr/local/bin")


def test_run_local_script_syntax_and_defaults():
    script = Path("/opt/pi-scheduler/deploy/run-local.sh")
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert "deploy/setup-runtime-user.sh" in content
    assert 'PI_SCHEDULER_CRON_FILE="${PI_SCHEDULER_CRON_FILE:-/etc/cron.d/pi-agent-jobs}"' in content
    assert 'PI_SCHEDULER_CRON_USER="${PI_SCHEDULER_CRON_USER:-pi-scheduler-agent}"' in content
    assert 'PI_SCHEDULER_ALLOWED_RUN_USERS="${PI_SCHEDULER_ALLOWED_RUN_USERS:-root,pi-scheduler-agent}"' in content
    assert 'exec sudo -E "$0" "$@"' in content
    assert "uvicorn" in content
    assert "app.main:app" in content


def test_setup_runtime_user_script_syntax_and_defaults():
    script = Path("/opt/pi-scheduler/deploy/setup-runtime-user.sh")
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert 'RUNTIME_USER="pi-scheduler-agent"' in content
    assert 'RUNTIME_GROUP="pi-scheduler"' in content
    assert 'MODELS_FILE="/root/.pi/agent/models.json"' in content
    assert "useradd" in content
    assert "usermod -aG" in content
    assert "chgrp -R" in content
    assert "chmod -R g+rwX" in content


def test_runtime_setup_config_defaults():
    assert config.RUNTIME_USER == "pi-scheduler-agent"
    assert config.RUNTIME_GROUP == "pi-scheduler"
    assert str(config.MODELS_SOURCE_FILE) == "/root/.pi/agent/models.json"


def test_runtime_setup_reports_missing_runtime_user(monkeypatch):
    from app import runtime_setup

    monkeypatch.setattr(config, "RUNTIME_USER", "pi-scheduler-agent")

    def missing_user(name):
        raise KeyError(name)

    monkeypatch.setattr(runtime_setup.pwd, "getpwnam", missing_user)

    warnings = runtime_setup.check_runtime_setup()

    assert any("Runtime user 'pi-scheduler-agent' does not exist" in warning for warning in warnings)
    assert any("setup-runtime-user.sh" in warning for warning in warnings)


def test_manual_runner_command_direct_when_already_target_user(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "RUNNER_PATH", "/opt/pi-scheduler/bin/pi-job-runner")
    monkeypatch.setattr(config, "CRON_USER", "piagent")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(run_users.getpass, "getuser", lambda: "piagent")
    monkeypatch.setattr(run_users.os, "geteuid", lambda: 1001)

    assert run_users.manual_runner_command("--job-id", "job-a", None) == [
        "/opt/pi-scheduler/bin/pi-job-runner",
        "--job-id",
        "job-a",
        "--source",
        "manual",
    ]


def test_manual_runner_command_uses_sudo_from_root(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "RUNNER_PATH", "/opt/pi-scheduler/bin/pi-job-runner")
    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(run_users.getpass, "getuser", lambda: "root")
    monkeypatch.setattr(run_users.os, "geteuid", lambda: 0)
    monkeypatch.setattr(run_users.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)

    assert run_users.manual_runner_command("--group-id", "flow", "piagent") == [
        "/usr/bin/sudo",
        "-u",
        "piagent",
        "/opt/pi-scheduler/bin/pi-job-runner",
        "--group-id",
        "flow",
        "--source",
        "manual",
    ]


def test_manual_runner_command_rejects_switch_from_non_root(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "RUNNER_PATH", "/opt/pi-scheduler/bin/pi-job-runner")
    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())
    monkeypatch.setattr(run_users.getpass, "getuser", lambda: "bjli")
    monkeypatch.setattr(run_users.os, "geteuid", lambda: 1000)

    try:
        run_users.manual_runner_command("--job-id", "job-a", "piagent")
    except run_users.RunUserError as exc:
        assert "cannot switch" in str(exc).lower()
    else:
        raise AssertionError("Expected non-root switch rejection")


def test_runner_cli_accepts_manual_source(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "run_job", lambda job_id, source="auto": calls.append((job_id, source)) or 0)
    monkeypatch.setattr("sys.argv", ["pi-job-runner", "--job-id", "job-a", "--source", "manual"])

    assert runner.main() == 0
    assert calls == [("job-a", "manual")]


def test_runtime_setup_reports_missing_runtime_directory(tmp_path, monkeypatch):
    from app import runtime_setup

    models = tmp_path / "home" / "pi-scheduler-agent" / ".pi" / "agent" / "models.json"
    models.parent.mkdir(parents=True)
    models.write_text('{"providers": []}', encoding="utf-8")

    class Pw:
        pw_dir = str(tmp_path / "home" / "pi-scheduler-agent")
        pw_uid = 1002
        pw_gid = 1003
        pw_name = "pi-scheduler-agent"

    monkeypatch.setattr(config, "RUNTIME_USER", "pi-scheduler-agent")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,pi-scheduler-agent", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "missing-data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "SCHEDULER_HOME", tmp_path)
    monkeypatch.setattr(runtime_setup.pwd, "getpwnam", lambda name: Pw())
    monkeypatch.setattr(runtime_setup.pwd, "getpwuid", lambda uid: type("Owner", (), {"pw_name": "pi-scheduler-agent"})())

    warnings = runtime_setup.check_runtime_setup()

    assert any("Runtime directory is missing" in warning for warning in warnings)
    assert any(str(tmp_path / "missing-data") in warning for warning in warnings)


def test_runtime_setup_accepts_valid_mocked_setup(tmp_path, monkeypatch):
    from app import runtime_setup

    models = tmp_path / "home" / "pi-scheduler-agent" / ".pi" / "agent" / "models.json"
    models.parent.mkdir(parents=True)
    models.write_text('{"providers": []}', encoding="utf-8")
    for dirname in ["data", "logs", "locks", "tmp"]:
        (tmp_path / dirname).mkdir()

    class Pw:
        pw_dir = str(tmp_path / "home" / "pi-scheduler-agent")
        pw_uid = 1002
        pw_gid = 1003
        pw_name = "pi-scheduler-agent"

    monkeypatch.setattr(config, "RUNTIME_USER", "pi-scheduler-agent")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,pi-scheduler-agent", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "SCHEDULER_HOME", tmp_path)
    monkeypatch.setattr(runtime_setup.pwd, "getpwnam", lambda name: Pw())
    monkeypatch.setattr(runtime_setup.pwd, "getpwuid", lambda uid: type("Owner", (), {"pw_name": "pi-scheduler-agent"})())
    monkeypatch.setattr(runtime_setup, "_can_write_as_runtime_user", lambda user_info, path: True)

    assert runtime_setup.check_runtime_setup() == []


def test_startup_logs_runtime_setup_warnings(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "cron")
    monkeypatch.setattr(web.runtime_setup, "log_runtime_setup_warnings", lambda: calls.append("checked") or [])

    web.startup()

    assert calls == ["checked"]


def test_db_persists_job_run_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "run_user": "piagent",
        }
    )

    assert db.get_job(job_id)["run_user"] == "piagent"

    db.update_job(
        job_id,
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent again",
            "cron_expr": "*/10 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "run_user": "root",
        },
    )

    assert db.get_job(job_id)["run_user"] == "root"
    assert db.list_jobs_for_cron()[0]["run_user"] == "root"


def test_db_persists_group_run_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "flow", "cron_expr": "*/10 * * * *", "enabled": 1, "run_user": "piagent"},
        [job_id],
    )

    assert db.get_group(group_id)["run_user"] == "piagent"

    db.update_group(
        group_id,
        {"name": "flow", "cron_expr": "*/15 * * * *", "enabled": 1, "run_user": "root"},
        [job_id],
    )

    assert db.get_group(group_id)["run_user"] == "root"
    assert db.list_groups_for_cron()[0]["run_user"] == "root"


def test_render_cron_file_uses_job_and_group_run_users(monkeypatch):
    from app import run_users

    monkeypatch.setattr(config, "CRON_USER", "root")
    monkeypatch.setattr(config, "ALLOWED_RUN_USERS", "root,piagent", raising=False)
    monkeypatch.setattr(run_users.pwd, "getpwnam", lambda name: object())

    content = cron.render_cron_file(
        [
            {"id": "default-job", "cron_expr": "*/5 * * * *", "enabled": 1, "deleted_at": None, "run_user": None},
            {"id": "user-job", "cron_expr": "0 * * * *", "enabled": 1, "deleted_at": None, "run_user": "piagent"},
        ],
        [
            {"id": "user-group", "cron_expr": "*/30 * * * *", "enabled": 1, "deleted_at": None, "run_user": "piagent"},
        ],
    )

    assert "*/5 * * * * root " in content
    assert "0 * * * * piagent " in content
    assert "*/30 * * * * piagent " in content


def test_render_cron_file():
    content = cron.render_cron_file(
        [
            {
                "id": "servicenow-sos-check",
                "cron_expr": "*/5 * * * *",
                "enabled": 1,
                "deleted_at": None,
            },
            {
                "id": "disabled",
                "cron_expr": "*/5 * * * *",
                "enabled": 0,
                "deleted_at": None,
            },
        ]
    )
    assert "/bin/pi-job-runner --job-id servicenow-sos-check" in content
    assert "--job-id disabled" not in content


def test_render_cron_file_includes_enabled_groups():
    content = cron.render_cron_file(
        jobs=[],
        groups=[
            {
                "id": "review-flow",
                "cron_expr": "*/10 * * * *",
                "enabled": 1,
                "deleted_at": None,
            },
            {
                "id": "disabled-flow",
                "cron_expr": "*/10 * * * *",
                "enabled": 0,
                "deleted_at": None,
            },
        ],
    )

    assert "/bin/pi-job-runner --group-id review-flow" in content
    assert "--group-id disabled-flow" not in content


def test_render_cron_file_uses_lightweight_job_query(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    enabled_id = db.create_job(
        {
            "name": "enabled job",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    disabled_id = db.create_job(
        {
            "name": "disabled job",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/10 * * * *",
            "enabled": 0,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    deleted_id = db.create_job(
        {
            "name": "deleted job",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/15 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.soft_delete_job(deleted_id)

    def fail_list_jobs():
        raise AssertionError("render_cron_file should not use the homepage list_jobs query")

    monkeypatch.setattr(db, "list_jobs", fail_list_jobs)

    jobs = db.list_jobs_for_cron()
    content = cron.render_cron_file()

    assert jobs
    assert all(set(job) == {"id", "cron_expr", "enabled", "deleted_at", "run_user"} for job in jobs)
    assert f"--job-id {enabled_id}" in content
    assert f"--job-id {disabled_id}" not in content
    assert f"--job-id {deleted_id}" not in content


def test_render_cron_file_uses_lightweight_group_query(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    enabled_group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1}, [job_id]
    )
    disabled_group_id = db.create_group(
        {"name": "disabled flow", "cron_expr": "*/15 * * * *", "enabled": 0}, [job_id]
    )

    def fail_list_groups():
        raise AssertionError("render_cron_file should not use the homepage list_groups query")

    monkeypatch.setattr(db, "list_groups", fail_list_groups)

    groups = db.list_groups_for_cron()
    content = cron.render_cron_file()

    assert groups
    assert all(set(group) == {"id", "cron_expr", "enabled", "deleted_at", "run_user"} for group in groups)
    assert f"--group-id {enabled_group_id}" in content
    assert f"--group-id {disabled_group_id}" not in content


def test_cron_status_preview_only_for_non_system_path(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "tmp" / "pi-agent-jobs"
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: None)

    status = cron_status.inspect("# generated\n")

    assert status["status"] == "preview_only"
    assert status["automatic_status"] == "not_active"
    assert status["headline"] == "Automatic jobs are not active"
    assert "preview-only" in status["summary"]
    assert status["is_system_cron_path"] is False
    assert any(check["label"] == "System cron path" and check["state"] == "fail" for check in status["checks"])
    assert any("outside /etc/cron.d" in warning for warning in status["warnings"])
    assert any("PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs" in item for item in status["recommendations"])


def test_cron_status_active_candidate_for_matching_system_file(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    target.parent.mkdir(parents=True)
    content = "# generated\n* * * * * root echo ok\n"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: True)

    status = cron_status.inspect(content)

    assert status["status"] == "active_candidate"
    assert status["automatic_status"] == "active"
    assert status["headline"] == "Automatic jobs are active"
    assert "cron service is running" in status["summary"]
    assert status["file_exists"] is True
    assert status["content_matches"] is True
    assert status["cron_service_active"] is True
    assert any(check["label"] == "Cron service" and check["state"] == "pass" for check in status["checks"])
    assert status["warnings"] == []


def test_cron_status_out_of_sync_for_different_existing_file(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    target.parent.mkdir(parents=True)
    target.write_text("# old\n", encoding="utf-8")
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: True)

    status = cron_status.inspect("# generated\n")

    assert status["status"] == "out_of_sync"
    assert status["automatic_status"] == "not_active"
    assert status["headline"] == "Automatic jobs need attention"
    assert status["content_matches"] is False
    assert any(check["label"] == "Content matches generated preview" and check["state"] == "fail" for check in status["checks"])
    assert any("does not match" in warning for warning in status["warnings"])


def test_cron_status_missing_for_system_target(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: True)

    status = cron_status.inspect("# generated\n")

    assert status["status"] == "missing"
    assert status["automatic_status"] == "not_active"
    assert status["headline"] == "Automatic jobs are not active"
    assert status["file_exists"] is False
    assert any(check["label"] == "Target file exists" and check["state"] == "fail" for check in status["checks"])
    assert any("does not exist" in warning for warning in status["warnings"])


def test_cron_status_likely_active_when_service_unknown(tmp_path, monkeypatch):
    from app import cron_status

    target = tmp_path / "etc" / "cron.d" / "pi-agent-jobs"
    target.parent.mkdir(parents=True)
    content = "# generated\n* * * * * root echo ok\n"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "CRON_FILE", target)
    monkeypatch.setattr(cron_status, "SYSTEM_CRON_DIR", tmp_path / "etc" / "cron.d")
    monkeypatch.setattr(cron_status, "_cron_service_active", lambda: None)

    status = cron_status.inspect(content)

    assert status["status"] == "active_candidate"
    assert status["automatic_status"] == "likely_active"
    assert status["headline"] == "Automatic jobs are likely active"
    assert any(check["label"] == "Cron service" and check["state"] == "unknown" for check in status["checks"])


def test_validate_cron_expr_rejects_six_fields():
    try:
        cron.validate_cron_expr("* * * * * *")
    except ValueError as exc:
        assert "5 fields" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_interval_schedule_helpers():
    assert cron.interval_to_cron("5", "minutes") == "*/5 * * * *"
    assert cron.interval_to_cron("2", "hours") == "0 */2 * * *"
    assert cron.cron_to_interval("*/5 * * * *") == {"every": "5", "unit": "minutes"}
    assert cron.describe_cron("0 */2 * * *") == "Every 2 hours"


def test_beijing_time_filter_converts_utc_to_beijing():
    assert web.beijing_time("2026-06-27T14:00:13Z") == "2026-06-27 22:00:13 Beijing"


def test_seconds_duration_filter_formats_ms_as_seconds():
    assert web.seconds_duration(18352) == "19"
    assert web.seconds_duration(18000) == "18"
    assert web.seconds_duration(None) == "0"


def test_recent_runs_page_size_is_small_for_ui():
    assert web.RUNS_PER_PAGE == 10


def test_hour_options_are_hourly_12_hour_labels():
    options = web.hour_options()

    assert len(options) == 24
    assert options[0] == {"value": "00:00", "label": "12:00 AM"}
    assert options[1] == {"value": "01:00", "label": "1:00 AM"}
    assert options[12] == {"value": "12:00", "label": "12:00 PM"}
    assert options[23] == {"value": "23:00", "label": "11:00 PM"}


def test_work_window_supports_daytime_and_overnight_windows():
    daytime = datetime(2026, 6, 28, 10, 0, tzinfo=work_window.BEIJING_TZ)
    evening = datetime(2026, 6, 28, 20, 0, tzinfo=work_window.BEIJING_TZ)
    early_morning = datetime(2026, 6, 28, 2, 0, tzinfo=work_window.BEIJING_TZ)

    assert work_window.is_within_window("09:00", "18:00", daytime)
    assert not work_window.is_within_window("09:00", "18:00", evening)
    assert work_window.is_within_window("18:00", "09:00", evening)
    assert work_window.is_within_window("18:00", "09:00", early_morning)
    assert not work_window.is_within_window("18:00", "09:00", daytime)


def test_work_window_describes_overnight_windows():
    assert work_window.describe("09:00", "18:00") == "09:00 - 18:00 Beijing"
    assert work_window.describe("18:00", "09:00") == "18:00 - 09:00 Beijing (overnight)"


def test_work_window_requires_both_times():
    try:
        work_window.validate("09:00", None)
    except ValueError as exc:
        assert "both start and end" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_index_renders_for_authenticated_user(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = web.index(request)

    assert response.status_code == 200


def test_toggle_buttons_use_stateful_action_styles(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "disabled-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 0,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    index_request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    index_response = web.index(index_request)
    index_html = web.templates.env.get_template("index.html").render(index_response.context)

    detail_request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}", "headers": []})
    detail_response = web.job_detail(detail_request, job_id)
    detail_html = web.templates.env.get_template("job_detail.html").render(detail_response.context)

    assert '<button class="toggle-enable">Enable</button>' in index_html
    assert '<button class="toggle-enable">Enable</button>' in detail_html
    assert '<input type="hidden" name="return_to" value="index">' in index_html
    assert '<input type="hidden" name="return_to" value="detail">' in detail_html


def test_toggle_job_redirects_back_to_detail_when_requested(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    response = web.toggle_job(job_id, return_to="detail")

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}"
    assert db.get_job(job_id)["enabled"] == 0


def test_index_shows_running_job_state(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.insert_run(
        {
            "id": "running-run",
            "job_id": job_id,
            "source": "manual",
            "started_at": "2026-06-27T14:55:01Z",
            "status": "running",
            "command": "pi --mode json run",
        }
    )

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = web.index(request)
    html = web.templates.env.get_template("index.html").render(response.context)

    assert response.context["jobs"][0]["has_running_run"] == 1
    assert '<button class="toggle-disable">Disable</button>' in html
    assert '<button class="primary" disabled>Running</button>' in html


def test_index_shows_queued_job_as_running(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = web.index(request, queued=job_id)
    html = web.templates.env.get_template("index.html").render(response.context)

    assert response.context["jobs"][0]["has_running_run"] == 1
    assert '<button class="primary" disabled>Running</button>' in html


def test_index_shows_group_last_run_status_and_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1}, [job_id]
    )
    db.insert_group_run(
        {
            "id": "group-run-1",
            "group_id": group_id,
            "source": "manual",
            "started_at": "2026-06-27T14:30:01Z",
            "finished_at": "2026-06-27T14:30:19Z",
            "status": "failed",
            "duration_ms": 18000,
        }
    )

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    response = web.index(request)
    html = web.templates.env.get_template("index.html").render(response.context)

    assert response.context["groups"][0]["last_status"] == "failed"
    assert response.context["groups"][0]["last_duration_ms"] == 18000
    assert f'href="/groups/{group_id}/runs/group-run-1"' in html
    assert '<span class="badge bad">failed</span>' in html
    assert "18 s" in html


def test_startup_syncs_existing_jobs_to_cron_file(tmp_path, monkeypatch):
    cron_file = tmp_path / "pi-agent-jobs"
    monkeypatch.setattr(web.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(web.config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(web.config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(web.config, "CRON_FILE", cron_file)

    db.init_db()
    db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    web.startup()

    assert cron_file.exists()
    assert "*/5 * * * * root" in cron_file.read_text()
    assert "--job-id pi-agent" in cron_file.read_text()


def test_disabled_runner_does_not_create_run_and_self_heals_cron(tmp_path, monkeypatch):
    cron_file = tmp_path / "pi-agent-jobs"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", cron_file)

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 0,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    cron_file.write_text(
        "# stale cron file\n*/5 * * * * root /root/pi-scheduler/bin/pi-job-runner --job-id pi-agent\n",
        encoding="utf-8",
    )

    exit_code = runner.run_job(job_id)

    assert exit_code == 0
    assert db.list_recent_runs(job_id) == []
    assert "--job-id pi-agent" not in cron_file.read_text(encoding="utf-8")


def test_runner_skips_without_run_record_outside_work_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")
    monkeypatch.setattr(runner.work_window, "is_within_window", lambda start, end: False)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pi should not run")),
    )

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "work_start": "09:00",
            "work_end": "18:00",
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    exit_code = runner.run_job(job_id)

    assert exit_code == 0
    assert db.list_recent_runs(job_id) == []


def test_manual_runner_bypasses_disabled_state_and_work_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")
    monkeypatch.setattr(
        runner.work_window,
        "is_within_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("manual run should bypass window")),
    )

    class Result:
        stdout = '{"type":"agent_start"}\n'
        stderr = ""
        returncode = 0

    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: Result())

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 0,
            "work_start": "09:00",
            "work_end": "18:00",
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    exit_code = runner.run_job(job_id, source="manual")
    runs = db.list_recent_runs(job_id)

    assert exit_code == 0
    assert len(runs) == 1
    assert runs[0]["source"] == "manual"
    assert runs[0]["status"] == "success"


def test_group_runner_executes_members_in_order_and_keeps_job_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")

    prompts = []

    class Result:
        stderr = ""
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(argv, **kwargs):
        prompts.append(argv[-1])
        return Result(f"summary for {argv[-1]}\n")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        runner.work_window,
        "is_within_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("member window should not be checked")),
    )

    db.init_db()
    agent_id = db.create_job(
        {
            "name": "agent",
            "skill_name": "general",
            "task_prompt": "run agent",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "summary",
            "session_mode": "no_session",
        }
    )
    qa_id = db.create_job(
        {
            "name": "qa",
            "skill_name": "general",
            "task_prompt": "run qa",
            "cron_expr": "*/5 * * * *",
            "enabled": 0,
            "work_start": "09:00",
            "work_end": "18:00",
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "summary",
            "session_mode": "no_session",
        }
    )
    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1},
        [agent_id, qa_id],
    )

    exit_code = runner.run_group(group_id, source="manual")
    group_run = db.get_group_run_with_steps(db.list_group_runs(group_id)[0]["id"])

    assert exit_code == 0
    assert prompts == ["run agent", "run qa"]
    assert group_run["status"] == "success"
    assert [step["status"] for step in group_run["steps"]] == ["success", "success"]
    assert [step["run_id"] is not None for step in group_run["steps"]] == [True, True]

    agent_run = db.get_run(group_run["steps"][0]["run_id"])
    qa_run = db.get_run(group_run["steps"][1]["run_id"])
    assert agent_run["group_run_id"] == group_run["id"]
    assert qa_run["group_run_id"] == group_run["id"]
    assert f"/jobs/{agent_id}/runs/" in agent_run["stdout_path"]
    assert f"/jobs/{qa_id}/runs/" in qa_run["stdout_path"]


def test_group_runner_stops_after_failed_member(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")

    prompts = []

    class Result:
        stderr = "failed"

        def __init__(self, prompt):
            self.stdout = ""
            self.returncode = 1 if prompt == "run qa" else 0

    def fake_run(argv, **kwargs):
        prompts.append(argv[-1])
        return Result(argv[-1])

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    db.init_db()
    job_ids = []
    for name in ["agent", "qa", "reviewer"]:
        job_ids.append(
            db.create_job(
                {
                    "name": name,
                    "skill_name": "general",
                    "task_prompt": f"run {name}",
                    "cron_expr": "*/5 * * * *",
                    "enabled": 1,
                    "timeout_seconds": 240,
                    "prevent_overlap": 1,
                    "output_mode": "summary",
                    "session_mode": "no_session",
                }
            )
        )
    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1}, job_ids
    )

    exit_code = runner.run_group(group_id, source="manual")
    group_run = db.get_group_run_with_steps(db.list_group_runs(group_id)[0]["id"])

    assert exit_code == 1
    assert prompts == ["run agent", "run qa"]
    assert group_run["status"] == "failed"
    assert [step["status"] for step in group_run["steps"]] == ["success", "failed", "skipped"]
    assert group_run["steps"][2]["run_id"] is None


def test_group_runner_continues_after_failed_member_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")

    prompts = []

    class Result:
        stderr = "failed"

        def __init__(self, prompt):
            self.stdout = ""
            self.returncode = 1 if prompt == "run qa" else 0

    def fake_run(argv, **kwargs):
        prompts.append(argv[-1])
        return Result(argv[-1])

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    db.init_db()
    job_ids = []
    for name in ["agent", "qa", "reviewer"]:
        job_ids.append(
            db.create_job(
                {
                    "name": name,
                    "skill_name": "general",
                    "task_prompt": f"run {name}",
                    "cron_expr": "*/5 * * * *",
                    "enabled": 1,
                    "timeout_seconds": 240,
                    "prevent_overlap": 1,
                    "output_mode": "summary",
                    "session_mode": "no_session",
                }
            )
        )
    group_id = db.create_group(
        {
            "name": "review flow",
            "cron_expr": "*/10 * * * *",
            "enabled": 1,
            "continue_on_failure": 1,
        },
        job_ids,
    )

    exit_code = runner.run_group(group_id, source="manual")
    group_run = db.get_group_run_with_steps(db.list_group_runs(group_id)[0]["id"])

    assert exit_code == 1
    assert prompts == ["run agent", "run qa", "run reviewer"]
    assert group_run["status"] == "failed"
    assert [step["status"] for step in group_run["steps"]] == ["success", "failed", "success"]
    assert all(step["run_id"] for step in group_run["steps"])


def test_summary_mode_runs_print_and_writes_summary_without_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")

    captured = {}

    class Result:
        stdout = "final summary\n"
        stderr = ""
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "summary",
            "session_mode": "no_session",
        }
    )

    exit_code = runner.run_job(job_id)
    run = db.get_run(db.list_recent_runs(job_id)[0]["id"])

    assert exit_code == 0
    assert captured["argv"] == [
        "pi",
        "--no-skills",
        "--no-session",
        "--name",
        "pi-scheduler: pi-agent",
        "-p",
        "check logs",
    ]
    assert Path(run["stdout_path"]).read_text(encoding="utf-8") == "final summary\n"
    assert run["jsonl_path"] is None


def test_events_mode_runs_json_and_writes_transcript_and_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")

    events = "\n".join(
        [
            '{"type":"agent_start"}',
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"final summary"}]}}',
        ]
    )
    captured = {}

    class Result:
        stdout = events
        stderr = ""
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "events",
            "session_mode": "save",
        }
    )

    exit_code = runner.run_job(job_id)
    run = db.get_run(db.list_recent_runs(job_id)[0]["id"])

    assert exit_code == 0
    assert captured["argv"] == [
        "pi",
        "--no-skills",
        "--name",
        "pi-scheduler: pi-agent",
        "--mode",
        "json",
        "check logs",
    ]
    assert "final summary" in Path(run["stdout_path"]).read_text(encoding="utf-8")
    assert Path(run["jsonl_path"]).read_text(encoding="utf-8") == events


def test_run_job_fails_safely_when_approved_skill_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")
    root = tmp_path / "approved-skills"
    root.mkdir()
    monkeypatch.setattr(config, "APPROVED_SKILLS_DIR", root, raising=False)

    called = {"run": False}

    def fake_run(argv, **kwargs):
        called["run"] = True
        raise AssertionError("pi should not be invoked when skill resolution fails")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "summary",
            "session_mode": "no_session",
            "skills_mode": "approved",
            "skill_ids": "missing",
        }
    )

    exit_code = runner.run_job(job_id)
    run = db.get_run(db.list_recent_runs(job_id)[0]["id"])

    assert exit_code == 1
    assert called["run"] is False
    assert run["status"] == "failed"
    assert "Approved skill 'missing' is not available" in Path(run["stderr_path"]).read_text(encoding="utf-8")


def test_recent_runs_ignore_disabled_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 0,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.insert_run(
        {
            "id": "disabled-run",
            "job_id": job_id,
            "started_at": "2026-06-27T15:20:01Z",
            "finished_at": "2026-06-27T15:20:01Z",
            "status": "disabled",
            "duration_ms": 0,
            "command": "pi --mode json run",
        }
    )
    db.insert_run(
        {
            "id": "success-run",
            "job_id": job_id,
            "started_at": "2026-06-27T14:55:01Z",
            "finished_at": "2026-06-27T14:55:19Z",
            "status": "success",
            "duration_ms": 18000,
            "command": "pi --mode json run",
        }
    )

    jobs = db.list_jobs()
    runs = db.list_recent_runs(job_id)

    assert jobs[0]["last_status"] == "success"
    assert [run["id"] for run in runs] == ["success-run"]


def test_recent_runs_support_limit_and_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    for index in range(4):
        db.insert_run(
            {
                "id": f"run-{index}",
                "job_id": job_id,
                "started_at": f"2026-06-27T14:0{index}:01Z",
                "finished_at": f"2026-06-27T14:0{index}:13Z",
                "status": "success",
                "duration_ms": 12000,
                "command": "pi --mode json run",
            }
        )

    first_page = db.list_recent_runs(job_id, limit=2, offset=0)
    second_page = db.list_recent_runs(job_id, limit=2, offset=2)

    assert [run["id"] for run in first_page] == ["run-3", "run-2"]
    assert [run["id"] for run in second_page] == ["run-1", "run-0"]
    assert "command" not in first_page[0]


def test_recent_runs_filter_by_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    for index, (run_id, source) in enumerate([("auto-run", "auto"), ("manual-run", "manual")]):
        db.insert_run(
            {
                "id": run_id,
                "job_id": job_id,
                "source": source,
                "started_at": f"2026-06-27T14:5{index}:01Z",
                "finished_at": f"2026-06-27T14:5{index}:19Z",
                "status": "success",
                "duration_ms": 18000,
                "command": "pi --mode json run",
            }
        )

    auto_runs = db.list_recent_runs(job_id, source="auto")
    manual_runs = db.list_recent_runs(job_id, source="manual")

    assert [run["id"] for run in auto_runs] == ["auto-run"]
    assert [run["id"] for run in manual_runs] == ["manual-run"]


def test_get_job_runs_status_combines_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    for index, source in enumerate(["manual", "auto", "manual"]):
        db.insert_run(
            {
                "id": f"{source}-run-{index}",
                "job_id": job_id,
                "source": source,
                "started_at": f"2026-06-27T14:0{index}:01Z",
                "finished_at": f"2026-06-27T14:0{index}:19Z",
                "status": "success",
                "duration_ms": 18000,
                "command": "pi --mode json run",
            }
        )
    db.insert_run(
        {
            "id": "running-run",
            "job_id": job_id,
            "source": "manual",
            "started_at": "2026-06-27T14:09:01Z",
            "status": "running",
            "command": "pi --mode json run",
        }
    )

    status = db.get_job_runs_status(job_id, limit=2, offset=0, source="manual")

    assert status["job"]["id"] == job_id
    assert status["has_running_run"] is True
    assert [run["id"] for run in status["runs"]] == ["running-run", "manual-run-2"]

    db.soft_delete_job(job_id)

    assert db.get_job_runs_status(job_id) is None


def test_job_runs_status_reports_running_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.insert_run(
        {
            "id": "running-run",
            "job_id": job_id,
            "source": "manual",
            "started_at": "2026-06-27T14:55:01Z",
            "status": "running",
            "command": "pi --mode json run",
        }
    )

    status = web.job_runs_status(job_id)

    assert status["has_running_run"] is True
    assert status["runs"][0] == {
        "id": "running-run",
        "started_at": "2026-06-27 22:55:01 Beijing",
        "source": "manual",
        "status": "running",
        "status_class": "muted",
        "duration": "0",
        "exit_code": None,
        "url": "/runs/running-run",
    }


def test_job_runs_status_supports_filtered_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.insert_run(
        {
            "id": "auto-newer-run",
            "job_id": job_id,
            "source": "auto",
            "started_at": "2026-06-27T15:30:01Z",
            "finished_at": "2026-06-27T15:30:19Z",
            "status": "success",
            "duration_ms": 18000,
            "command": "pi --mode json run",
        }
    )
    for index in range(12):
        db.insert_run(
            {
                "id": f"manual-run-{index}",
                "job_id": job_id,
                "source": "manual",
                "started_at": f"2026-06-27T14:{index:02d}:01Z",
                "finished_at": f"2026-06-27T14:{index:02d}:19Z",
                "status": "success",
                "duration_ms": 18000,
                "command": "pi --mode json run",
            }
        )

    first_page = web.job_runs_status(job_id, page=1, source="manual")
    second_page = web.job_runs_status(job_id, page=2, source="manual")

    assert first_page["page"] == 1
    assert first_page["has_next_page"] is True
    assert len(first_page["runs"]) == web.RUNS_PER_PAGE
    assert first_page["runs"][0]["id"] == "manual-run-11"
    assert second_page["page"] == 2
    assert second_page["has_next_page"] is False
    assert [run["id"] for run in second_page["runs"]] == ["manual-run-1", "manual-run-0"]


def test_job_detail_shows_only_recent_runs_with_logs_link(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    for index in range(12):
        db.insert_run(
            {
                "id": f"run-{index}",
                "job_id": job_id,
                "started_at": f"2026-06-27T14:{index:02d}:01Z",
                "finished_at": f"2026-06-27T14:{index:02d}:19Z",
                "status": "success",
                "duration_ms": 18000,
                "command": "pi --mode json run",
            }
        )

    request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}", "headers": []})
    response = web.job_detail(request, job_id)
    html = web.templates.env.get_template("job_detail.html").render(response.context)

    assert len(response.context["runs"]) == web.RUNS_PER_PAGE
    assert response.context["runs"][0]["id"] == "run-11"
    assert f'href="/logs?job_id={job_id}"' in html
    assert '<button class="toggle-disable">Disable</button>' in html
    assert '<textarea id="prompt-preview" class="readonly-field collapsible-text expanded" rows="1" readonly' in html
    assert 'aria-controls="prompt-preview"' not in html
    assert 'id="command-preview" class="collapsible-text command-preview' in html
    assert 'id="previous-page"' not in html
    assert 'id="next-page"' not in html


def test_job_detail_collapses_long_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "review-agent",
            "skill_name": "general",
            "task_prompt": "Review this repository carefully. " * 20,
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}", "headers": []})
    response = web.job_detail(request, job_id)
    html = web.templates.env.get_template("job_detail.html").render(response.context)

    assert '<textarea id="prompt-preview" class="readonly-field collapsible-text is-collapsible" rows="5" readonly' in html
    assert 'aria-controls="prompt-preview"' in html
    assert "Show more" in html


def test_logs_page_lists_runs_with_filters_and_cleanup_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1}, [job_id]
    )
    db.insert_group_run(
        {
            "id": "group-run-1",
            "group_id": group_id,
            "source": "manual",
            "started_at": "2026-06-27T14:30:01Z",
            "finished_at": "2026-06-27T14:30:19Z",
            "status": "success",
            "duration_ms": 18000,
        }
    )
    db.insert_run(
        {
            "id": "success-run",
            "job_id": job_id,
            "group_run_id": "group-run-1",
            "source": "manual",
            "started_at": "2026-06-27T14:30:01Z",
            "finished_at": "2026-06-27T14:30:19Z",
            "status": "success",
            "duration_ms": 18000,
            "command": "pi --mode json run",
            "stdout_path": str(config.LOG_DIR / "success.stdout.log"),
        }
    )
    db.insert_run(
        {
            "id": "failed-run",
            "job_id": job_id,
            "source": "auto",
            "started_at": "2026-06-27T15:30:01Z",
            "finished_at": "2026-06-27T15:30:19Z",
            "status": "failed",
            "duration_ms": 18000,
            "command": "pi --mode json run",
        }
    )

    request = Request({"type": "http", "method": "GET", "path": "/logs", "headers": []})
    response = web.logs_page(
        request,
        job_id=job_id,
        group_id=group_id,
        source="manual",
        run_status="success",
    )
    html = web.templates.env.get_template("logs.html").render(response.context)

    assert [run["id"] for run in response.context["runs"]] == ["success-run"]
    assert response.context["filters"]["job_id"] == job_id
    assert response.context["filters"]["group_id"] == group_id
    assert f'<option value="{group_id}" selected>review flow</option>' in html
    assert 'action="/logs/cleanup"' in html
    assert "Delete All Completed Runs" in html
    assert "failed-run" not in html


def test_cron_preview_displays_cron_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CRON_FILE", tmp_path / "pi-agent-jobs")
    monkeypatch.setattr(web.cron_status, "inspect", lambda content: {
        "target_file": str(tmp_path / "pi-agent-jobs"),
        "is_system_cron_path": False,
        "file_exists": False,
        "content_matches": None,
        "file_mode": None,
        "file_owner": None,
        "cron_service_active": None,
        "status": "preview_only",
        "automatic_status": "not_active",
        "headline": "Automatic jobs are not active",
        "summary": "Cron file is only a preview-only file. System cron does not read this path.",
        "checks": [
            {"label": "System cron path", "state": "fail", "detail": "Target is outside /etc/cron.d"},
            {"label": "Target file exists", "state": "fail", "detail": "File is missing"},
            {"label": "Content matches generated preview", "state": "unknown", "detail": "No target file to compare"},
            {"label": "Cron service", "state": "unknown", "detail": "Could not confirm with systemctl"},
        ],
        "warnings": ["Target file is outside /etc/cron.d. System cron will not read this file automatically."],
        "recommendations": ["For active system cron, set PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs before starting the app."],
    })

    response = web.cron_preview(Request({"type": "http", "method": "GET", "path": "/cron", "headers": []}))
    html = response.body.decode()

    assert "Automatic Scheduling" in html
    assert "Automatic jobs are not active" in html
    assert "Cron file is only a preview-only file" in html
    assert "System cron path" in html
    assert "Target file exists" in html
    assert "outside /etc/cron.d" in html
    assert "PI_SCHEDULER_CRON_FILE=/etc/cron.d/pi-agent-jobs" in html


def test_maintenance_logs_redirects_to_logs():
    response = web.maintenance_logs()

    assert response.status_code == 303
    assert response.headers["location"] == "/logs"


def test_manual_run_queues_background_task(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    class Tasks:
        def __init__(self):
            self.calls = []

        def add_task(self, func, *args, **kwargs):
            self.calls.append((func, args, kwargs))

    tasks = Tasks()
    response = web.manual_run(job_id, tasks)

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}?queued=1"
    assert tasks.calls == [(
        run_users.launch_command,
        ([config.RUNNER_PATH, "--job-id", job_id, "--source", "manual"],),
        {},
    )]


def test_manual_run_from_index_returns_to_index(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )

    class Tasks:
        def __init__(self):
            self.calls = []

        def add_task(self, func, *args, **kwargs):
            self.calls.append((func, args, kwargs))

    tasks = Tasks()
    response = web.manual_run(job_id, tasks, return_to="index")

    assert response.status_code == 303
    assert response.headers["location"] == f"/?queued={job_id}"
    assert tasks.calls == [(
        run_users.launch_command,
        ([config.RUNNER_PATH, "--job-id", job_id, "--source", "manual"],),
        {},
    )]


def test_manual_group_run_queues_background_task(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1}, [job_id]
    )

    class Tasks:
        def __init__(self):
            self.calls = []

        def add_task(self, func, *args, **kwargs):
            self.calls.append((func, args, kwargs))

    tasks = Tasks()
    response = web.manual_group_run(group_id, tasks, return_to="index")

    assert response.status_code == 303
    assert response.headers["location"] == f"/?queued={group_id}"
    assert tasks.calls == [(
        run_users.launch_command,
        ([config.RUNNER_PATH, "--group-id", group_id, "--source", "manual"],),
        {},
    )]


def test_running_group_run_detail_auto_refreshes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    group_id = db.create_group(
        {"name": "review flow", "cron_expr": "*/10 * * * *", "enabled": 1}, [job_id]
    )
    db.insert_group_run(
        {
            "id": "running-group-run",
            "group_id": group_id,
            "source": "manual",
            "started_at": "2026-06-27T14:30:01Z",
            "status": "running",
        }
    )

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/groups/{group_id}/runs/running-group-run",
            "headers": [],
        }
    )
    response = web.group_run_detail(request, group_id, "running-group-run")
    html = web.templates.env.get_template("group_run_detail.html").render(response.context)

    assert "refreshes every 5 seconds" in html
    assert "window.location.reload(), 5000" in html


def test_read_log_limits_large_files(tmp_path):
    log_path = tmp_path / "large.log"
    log_path.write_text(("a" * 1024) + "tail", encoding="utf-8")

    content = web.read_log(str(log_path), max_bytes=1024)

    assert content.startswith("[Showing last 1 KiB of 2 KiB log]")
    assert content.endswith("tail")
    assert len(content) < 1100


def test_run_detail_marks_whether_jsonl_is_available(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    stdout_path = tmp_path / "stdout.log"
    jsonl_path = tmp_path / "events.jsonl"
    stdout_path.write_text("summary", encoding="utf-8")
    jsonl_path.write_text('{"type":"agent_start"}', encoding="utf-8")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.insert_run(
        {
            "id": "summary-run",
            "job_id": job_id,
            "started_at": "2026-06-27T14:55:01Z",
            "finished_at": "2026-06-27T14:55:19Z",
            "status": "success",
            "duration_ms": 18000,
            "command": "pi -p run",
            "stdout_path": str(stdout_path),
        }
    )
    db.insert_run(
        {
            "id": "events-run",
            "job_id": job_id,
            "started_at": "2026-06-27T14:56:01Z",
            "finished_at": "2026-06-27T14:56:19Z",
            "status": "success",
            "duration_ms": 18000,
            "command": "pi --mode json run",
            "stdout_path": str(stdout_path),
            "jsonl_path": str(jsonl_path),
        }
    )

    request = Request({"type": "http", "method": "GET", "path": "/runs/summary-run", "headers": []})
    summary_response = web.run_detail(request, "summary-run")
    events_response = web.run_detail(request, "events-run")
    summary_html = web.templates.env.get_template("run_detail.html").render(summary_response.context)

    assert summary_response.context["has_jsonl"] is False
    assert events_response.context["has_jsonl"] is True
    assert 'id="command-preview" class="collapsible-text command-preview expanded"' in summary_html
    assert 'aria-controls="command-preview"' not in summary_html


def test_run_detail_collapses_long_command(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.insert_run(
        {
            "id": "long-command-run",
            "job_id": job_id,
            "started_at": "2026-06-27T14:55:01Z",
            "finished_at": "2026-06-27T14:55:19Z",
            "status": "success",
            "duration_ms": 18000,
            "command": "pi -p " + "review logs " * 40,
        }
    )

    request = Request({"type": "http", "method": "GET", "path": "/runs/long-command-run", "headers": []})
    response = web.run_detail(request, "long-command-run")
    html = web.templates.env.get_template("run_detail.html").render(response.context)

    assert 'id="command-preview" class="collapsible-text command-preview is-collapsible"' in html
    assert 'aria-controls="command-preview"' in html
    assert "Show more" in html


def test_run_detail_displays_current_job_name_after_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-test",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    db.update_job(
        job_id,
        {
            "name": "skill@servicenow-agent - loop queue",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
            "output_mode": "summary",
            "session_mode": "no_session",
            "tool_mode": "full",
        },
    )
    db.insert_run(
        {
            "id": "20260629T143613Z-c128de6a-pi-test",
            "job_id": job_id,
            "started_at": "2026-06-29T14:36:13Z",
            "status": "success",
            "duration_ms": 1000,
            "command": "pi",
        }
    )

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/runs/20260629T143613Z-c128de6a-pi-test",
            "headers": [],
        }
    )
    response = web.run_detail(request, "20260629T143613Z-c128de6a-pi-test")
    html = web.templates.env.get_template("run_detail.html").render(response.context)

    assert '<a href="/jobs/pi-test">skill@servicenow-agent - loop queue</a>' in html
    assert '<a href="/jobs/pi-test">pi-test</a>' not in html


def test_cleanup_old_logs_removes_old_run_files_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")
    monkeypatch.setattr(
        retention,
        "cutoff_for_days",
        lambda days: datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
    )

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    runs_dir = config.LOG_DIR / "jobs" / job_id / "runs"
    old_stdout = runs_dir / "old.stdout.log"
    old_stderr = runs_dir / "old.stderr.log"
    old_jsonl = runs_dir / "old.pi-events.jsonl"
    fresh_stdout = runs_dir / "fresh.stdout.log"
    old_summary = config.LOG_DIR / "jobs" / job_id / "2026-05-30.jsonl"
    fresh_summary = config.LOG_DIR / "jobs" / job_id / "2026-06-01.jsonl"
    for path in [old_stdout, old_stderr, old_jsonl, fresh_stdout, old_summary, fresh_summary]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("log", encoding="utf-8")

    db.insert_run(
        {
            "id": "old-run",
            "job_id": job_id,
            "started_at": "2026-05-31T23:59:59Z",
            "finished_at": "2026-05-31T23:59:59Z",
            "status": "success",
            "duration_ms": 12000,
            "command": "pi --mode json run",
            "stdout_path": str(old_stdout),
            "stderr_path": str(old_stderr),
            "jsonl_path": str(old_jsonl),
        }
    )
    db.insert_run(
        {
            "id": "fresh-run",
            "job_id": job_id,
            "started_at": "2026-06-01T00:00:00Z",
            "finished_at": "2026-06-01T00:00:12Z",
            "status": "success",
            "duration_ms": 12000,
            "command": "pi --mode json run",
            "stdout_path": str(fresh_stdout),
        }
    )

    deleted = retention.cleanup_old_logs(days=30)

    assert deleted == 1
    assert db.get_run("old-run") is None
    assert db.get_run("fresh-run") is not None
    assert not old_stdout.exists()
    assert not old_stderr.exists()
    assert not old_jsonl.exists()
    assert fresh_stdout.exists()
    assert not old_summary.exists()
    assert fresh_summary.exists()


def test_cleanup_all_runs_removes_records_files_and_summaries(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    runs_dir = config.LOG_DIR / "jobs" / job_id / "runs"
    stdout_path = runs_dir / "done.stdout.log"
    stderr_path = runs_dir / "done.stderr.log"
    running_stdout_path = runs_dir / "running.stdout.log"
    summary_path = config.LOG_DIR / "jobs" / job_id / "2026-06-29.jsonl"
    for path in [stdout_path, stderr_path, running_stdout_path, summary_path]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("log", encoding="utf-8")

    db.insert_run(
        {
            "id": "done-run",
            "job_id": job_id,
            "started_at": "2026-06-29T03:19:01Z",
            "finished_at": "2026-06-29T03:19:13Z",
            "status": "success",
            "duration_ms": 11839,
            "command": "pi -p run",
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    )
    db.insert_run(
        {
            "id": "running-run",
            "job_id": job_id,
            "started_at": "2026-06-29T03:20:01Z",
            "status": "running",
            "command": "pi -p run",
            "stdout_path": str(running_stdout_path),
        }
    )

    result = retention.cleanup_all_runs()

    assert result.runs_deleted == 1
    assert db.get_run("done-run") is None
    assert db.get_run("running-run") is not None
    assert not stdout_path.exists()
    assert not stderr_path.exists()
    assert running_stdout_path.exists()
    assert not summary_path.exists()


def test_cleanup_logs_does_not_require_typed_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "pi-scheduler.sqlite3")

    db.init_db()
    job_id = db.create_job(
        {
            "name": "pi-agent",
            "skill_name": "general",
            "task_prompt": "check logs",
            "cron_expr": "*/5 * * * *",
            "enabled": 1,
            "timeout_seconds": 240,
            "prevent_overlap": 1,
        }
    )
    stdout_path = config.LOG_DIR / "jobs" / job_id / "runs" / "run.stdout.log"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("log", encoding="utf-8")
    db.insert_run(
        {
            "id": "run",
            "job_id": job_id,
            "started_at": "2026-06-29T03:19:01Z",
            "finished_at": "2026-06-29T03:19:13Z",
            "status": "success",
            "duration_ms": 11839,
            "command": "pi -p run",
            "stdout_path": str(stdout_path),
        }
    )

    request = Request({"type": "http", "method": "POST", "path": "/logs/cleanup", "headers": []})
    response = web.cleanup_logs(request, mode="all", days=30)

    assert response.context["errors"] == []
    assert response.context["result"].runs_deleted == 1
    assert db.get_run("run") is None
    assert not stdout_path.exists()
