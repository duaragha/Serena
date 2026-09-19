def put(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discovery_precedence_and_malformed_warning(tmp_path):
    from fleet.skills import discover_skills, match_skills
    put(tmp_path, 'SKILL.md', '---\nname: deploy\ndescription: deploy software\n---\nroot')
    put(tmp_path, '.agents/skills/deploy/SKILL.md', '---\nname: deploy\ndescription: deploy release\n---\nnested')
    put(tmp_path, '.agents/skills/broken/SKILL.md', '---\nname: [broken]\n---\ninvalid')
    put(tmp_path, '.agents/skills/ignored/skill.md', '# wrong case')
    warnings = []
    skills = discover_skills(tmp_path, warn=warnings.append)
    assert [s.name for s in skills] == ['deploy']
    assert skills[0].text.endswith('nested')
    assert len(warnings) == 1 and 'broken' in warnings[0]['path']
    assert match_skills(skills, 'deploy this release') == skills
    assert match_skills(skills, 'calculate bananas') == []


def test_injection_catalog_budget_redaction_and_determinism(tmp_path):
    from fleet.skills import prompt_context
    put(tmp_path, 'SKILL.md', '---\nname: deploy\ndescription: release deployment\n---\n'
        + 'password=never-expose\n' + 'release procedure line\n' * 3000)
    text, receipts = prompt_context(tmp_path, 'deploy', budget_chars=2500)
    assert 'deploy: release deployment' in text
    assert 'never-expose' not in text
    assert len(text) <= 2500
    assert any(r['omitted_chars'] > 0 for r in receipts)
    assert 'omitted' in text
    assert prompt_context(tmp_path, 'deploy', budget_chars=2500) == (text, receipts)
    unmatched, _ = prompt_context(tmp_path, 'bananas')
    assert 'deploy: release deployment' in unmatched
    assert 'release procedure line' not in unmatched


def test_deeply_nested_frontmatter_is_skipped_with_a_warning(tmp_path):
    """Parser recursion is a malformed skill, not a prompt-assembly failure."""
    from fleet.skills import discover_skills, prompt_context
    put(tmp_path, 'SKILL.md', '---\nname: deploy\ndescription: deploy software\n---\nroot')
    # Both headers stay far under the 16 KB frontmatter cap while nesting 1200
    # and 600 levels deep, which is what exhausts the YAML parser's stack.
    put(tmp_path, '.agents/skills/nested/SKILL.md', '---\n' + '- ' * 1200 + 'x\n---\nbody')
    put(tmp_path, '.agents/skills/flow/SKILL.md',
        '---\nname: flow\nvalue: ' + '[' * 600 + '1' + ']' * 600 + '\n---\nbody')
    warnings = []
    skills = discover_skills(tmp_path, warn=warnings.append)
    assert [s.name for s in skills] == ['deploy']
    assert {w['path'] for w in warnings} == {'.agents/skills/nested/SKILL.md', '.agents/skills/flow/SKILL.md'}
    text, receipts = prompt_context(tmp_path, 'deploy software')
    assert 'deploy: deploy software' in text and receipts


def test_symlink_skill_does_not_escape_checkout(tmp_path):
    from fleet.skills import discover_skills
    outside = tmp_path / 'outside.md'
    outside.write_text('---\nname: danger\ndescription: private\n---\nprivate', encoding="utf-8")
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    (checkout / 'SKILL.md').symlink_to(outside)
    warnings = []
    assert discover_skills(checkout, warn=warnings.append) == []
    assert warnings
