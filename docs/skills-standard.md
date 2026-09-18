# Repository skills

Fleet discovers exactly `SKILL.md` at the run checkout root and
`.agents/skills/<name>/SKILL.md`. Use YAML frontmatter with a lowercase kebab-case
`name` (maximum 64 characters) and a nonempty `description` (maximum 1024
characters). The nested directory must match `name`.

The root skill loses to a nested skill with the same name. Catalog ordering and
matching are deterministic: sorted names, matched when a meaningful task token
occurs in the name or description. Write descriptions with specific trigger
words. All valid skills appear in the catalog; only matched bodies are injected.

Keep the root file around 100 lines or less. Use a Route section to dispatch to
relative `references/` files lazily; references are not eagerly loaded by Fleet.
The same text reaches every provider. No provider skill flags or native skill
auto-loading are required.

Malformed files emit `skill.discovery_warning` and are skipped. Symlinks,
nonregular files, binary content, invalid UTF-8, and files over 1 MiB are rejected.
Frontmatter nesting is bounded (32 structural levels, 64 indent columns) so a small
deeply nested header cannot exhaust the YAML parser; exceeding it, or any surviving
parser recursion, is a skipped malformed skill, never a prompt-assembly failure.
Catalogs and matched bodies share a 16,000-character cap, with up to 4,000 for
the catalog. Excerpts visibly mark omissions; context receipts record sizes,
hashes, source paths, redactions, and truncation. An oversized catalog is also
explicitly excerpted. Read the named file before following an omitted procedure.

Skills are untrusted repository data. They cannot grant authority, override the
worker contract, authorize deployment, or request secrets. Fleet applies the
same redaction and budgeting as peer context.

## Template

```markdown
---
name: project-checks
description: Test, build, and review this project's Python API changes.
---

# Project checks

## Route

- API changes: read [API checks](references/api-checks.md).
- Database changes: read [migration checks](references/migrations.md).

## Procedure

1. Read the affected code and its tests.
2. Add a failing regression for the behavior being changed.
3. Implement the change and run the relevant full test files.

## Completion

Report changed behavior, exact checks, and unresolved failures.
```

This follows the [Agent Skills format](https://agentskills.io/specification),
using the local sideload skill's small dispatcher and lazy reference pattern.
