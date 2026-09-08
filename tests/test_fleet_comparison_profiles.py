import pytest

from fleet.policy import build_policy, builtin_config, policy_models_match_contract


@pytest.mark.parametrize('variant,code,review', [
    ('sol', ('gpt-5.6-sol', 'xhigh'), ('gpt-5.6-sol', 'high')),
    ('astra', ('gpt-6-astra', 'medium'), ('gpt-6-astra', 'medium')),
])
def test_comparison_freezes_exact_pair(variant, code, review):
    policy = build_policy('coding', f'Fleet comparison profile: {variant}\nImplement queue',
                          config=builtin_config(), provider_mode='codex')
    assert [(p.workers[0].model, p.workers[0].effort) for p in policy.phases] == [
        ('gpt-5.6-luna', 'max'), code, review, ('gpt-6-astra', 'high')]
    assert policy_models_match_contract('coding', policy.to_dict())

def test_comparison_cannot_override_provider_restriction():
    with pytest.raises(ValueError, match='codex-only'):
        build_policy('coding', 'Fleet comparison profile: astra',
                     config=builtin_config(), provider_mode='claude')

def test_invalid_comparison_fails_closed():
    with pytest.raises(ValueError, match='must be sol or astra'):
        build_policy('coding', 'Fleet comparison profile: mystery',
                     config=builtin_config(), provider_mode='codex')
