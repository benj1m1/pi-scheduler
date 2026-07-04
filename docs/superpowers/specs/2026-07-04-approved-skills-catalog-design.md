# Approved Skills Catalog Design

## Goal

Replace free-form approved skill paths in job configuration with a scheduler-managed Approved Skills Catalog. Jobs should continue to run with no skills by default, and should only load explicit skills selected from a controlled, read-only catalog.

## Motivation

The current Skills Policy v1 supports `No skills`, `Approved skill paths only`, and `Runtime user default skills`. The free-form path field is functional but creates governance and safety problems:

- Job authors can reference arbitrary absolute paths.
- Skill provenance is hard to audit.
- Symlinks or path changes can make a job load unexpected content.
- Absolute paths are difficult to migrate.
- It is hard to answer which jobs use a given skill.

A catalog-based model keeps skill approval centralized and makes job configuration safer and easier to review.

## Approved Skills Directory

The approved skills root will be:

```text
/opt/pi-scheduler/approved-skills
```

Each approved skill is a direct child directory containing a `SKILL.md` file:

```text
/opt/pi-scheduler/approved-skills/
  pdf/
    SKILL.md
  obsidian-markdown/
    SKILL.md
  defuddle/
    SKILL.md
```

Recommended ownership and permissions:

```text
/opt/pi-scheduler/approved-skills      root:pi-scheduler  750
/opt/pi-scheduler/approved-skills/*    root:pi-scheduler  750
```

`pi-scheduler-agent` should be able to read and traverse the catalog through the `pi-scheduler` group, but should not be able to modify approved skills.

## Skill Identity

Jobs should store stable skill IDs, not absolute paths.

A skill ID is the direct child directory name under the approved skills root. Valid IDs must match:

```text
^[A-Za-z0-9][A-Za-z0-9_-]*$
```

Invalid examples:

```text
../pdf
/path/to/pdf
pdf skill
.hidden
```

A catalog entry is valid only when:

1. The skill ID is syntactically valid.
2. The entry is a direct child of the approved skills root.
3. The entry is a directory.
4. It contains `SKILL.md`.
5. The resolved path remains inside the approved skills root.
6. The entry is not a symlink.

## Job Data Model

Keep the existing `skills_mode` field:

```text
none | approved | runtime
```

Add a new field:

```text
skill_ids text not null default ''
```

`skill_ids` stores newline-separated skill IDs, not paths.

Compatibility behavior:

- New jobs default to `skills_mode = 'none'` and `skill_ids = ''`.
- Existing jobs with `skills_mode = 'none'` remain unchanged.
- Existing jobs with `skills_mode = 'runtime'` remain unchanged.
- Existing jobs with `skills_mode = 'approved'` and old `skill_paths` should not silently translate arbitrary paths. They should be shown as requiring review unless their paths exactly map to catalog entries.

The old `skill_paths` column can remain for compatibility during migration, but the UI should stop exposing a free-form path editor.

## UI Design

The job form should show a Skills Policy section:

```text
Skills policy

(●) No skills
    Safest default. Pi runs with --no-skills.

( ) Approved skills
    [ ] pdf
        Use for reading, extracting, merging, OCR, and creating PDFs.
    [ ] obsidian-markdown
        Create and edit Obsidian-flavored markdown.

( ) Runtime default skills
    Advanced: allows Pi to discover skills from the runtime user's environment.
```

Behavior:

- `No skills` is the default.
- `Approved skills` requires at least one selected catalog skill.
- The selected skills are submitted as skill IDs.
- Users cannot type arbitrary skill paths.
- If the catalog is empty, the Approved skills mode should show a clear empty state and remain invalid unless at least one catalog skill exists and is selected.
- If a job references a missing skill ID, the edit/detail UI should display a warning and running the job should fail safely.

The job detail and index pages should display selected skill names, not absolute paths.

## Runner Behavior

The runner should convert skill IDs to approved catalog paths at command-build time.

Modes:

### No skills

```bash
pi --no-skills ...
```

### Approved skills

```bash
pi --no-skills --skill /opt/pi-scheduler/approved-skills/pdf --skill /opt/pi-scheduler/approved-skills/obsidian-markdown ...
```

Before adding each `--skill` argument, the runner must validate that the selected skill ID still resolves to a valid catalog entry.

If any selected skill is missing or invalid, the run should fail with a clear error in the run log. It must not fall back to runtime default skills.

### Runtime default skills

```bash
pi ...
```

No `--no-skills` or `--skill` arguments are added. This mode remains advanced/unsafe.

## Catalog Reader Component

Add a small, focused module, for example:

```text
app/approved_skills.py
```

Responsibilities:

- Read configured approved skills root.
- List valid catalog entries.
- Parse `SKILL.md` frontmatter for `name` and `description` when available.
- Validate skill IDs.
- Resolve a skill ID to a safe path.
- Return warnings for ignored invalid entries.

This module should not mutate the filesystem.

## Configuration

Add:

```text
PI_SCHEDULER_APPROVED_SKILLS_DIR=/opt/pi-scheduler/approved-skills
```

The default should be `/opt/pi-scheduler/approved-skills`.

Deployment setup should create this directory with safe ownership/permissions, but FastAPI startup should only warn if the catalog is missing or unreadable.

## Error Handling

- Invalid submitted skill IDs: reject form submission with a validation error.
- Approved mode with no selected skills: reject form submission.
- Missing catalog directory: show an empty catalog warning in the form; startup logs a warning.
- Missing skill at run time: fail the run safely and record the error.
- Symlinked or path-escaping entries: ignore from catalog listing and reject if referenced.

## Testing Plan

Tests should cover:

- Catalog listing finds direct child directories with `SKILL.md`.
- Catalog listing ignores invalid IDs, symlinks, files, and directories without `SKILL.md`.
- Skill ID resolution rejects traversal and absolute paths.
- New jobs default to `skills_mode = 'none'`.
- Approved mode stores and displays skill IDs.
- Form validation rejects approved mode without selected skills.
- Form validation rejects IDs not present in the catalog.
- Runner builds `--no-skills` by default.
- Runner builds `--no-skills --skill <catalog path>` for selected approved skills.
- Runner fails safely when a selected skill disappears.
- Runtime mode does not add skills-related arguments.

## Out of Scope for This Change

- Web UI for uploading, editing, or approving skill contents.
- Skill version pinning.
- Risk scores per skill.
- Audit log for skill catalog changes.
- Remote skill repositories.

These can be added later once the controlled catalog exists.

## Open Operational Guidance

Approved skill installation should be an explicit admin action, for example:

```bash
sudo mkdir -p /opt/pi-scheduler/approved-skills
sudo cp -a /source/pdf /opt/pi-scheduler/approved-skills/pdf
sudo chown -R root:pi-scheduler /opt/pi-scheduler/approved-skills
sudo chmod -R u=rwX,g=rX,o= /opt/pi-scheduler/approved-skills
```

The scheduler should treat catalog contents as trusted only because they are controlled by filesystem permissions and admin process, not because job authors can choose arbitrary paths.
