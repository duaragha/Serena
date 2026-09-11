import os
import subprocess

import pytest

from core.workspace_diff import read_project_diff


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(root), *args], env=env, check=True, capture_output=True).stdout


def test_diff_reads_staged_unstaged_untracked_without_mutation(tmp_path):
    git(tmp_path, 'init')
    file = tmp_path / 'tracked.txt'
    file.write_text('staged\n')
    git(tmp_path, 'add', '--', 'tracked.txt')
    file.write_text('unstaged\n')
    (tmp_path / 'new name.txt').write_text('<b>new</b>\n')
    (tmp_path / '.gitignore').write_text('ignored\n')
    (tmp_path / 'ignored').write_text('secret')
    before = (tmp_path / '.git/index').read_bytes()
    result = read_project_diff(tmp_path)
    assert '+staged' in result['staged'] and '+unstaged' in result['unstaged']
    assert '+<b>new</b>' in result['untracked'] and 'secret' not in str(result)
    assert (tmp_path / '.git/index').read_bytes() == before
    assert file.read_text() == 'unstaged\n'
    assert result['omitted'] == []
    with pytest.raises(RuntimeError, match='display limit'):
        read_project_diff(tmp_path, max_bytes=5)
    with pytest.raises(RuntimeError, match='Too many'):
        read_project_diff(tmp_path, max_files=0)


def test_non_repository_fails_honestly(tmp_path):
    with pytest.raises(RuntimeError, match='could not read'):
        read_project_diff(tmp_path)


@pytest.mark.skipif(os.name == 'nt', reason='Symlink creation requires Windows privileges')
def test_untracked_symlink_is_reported_without_reading_target(tmp_path):
    git(tmp_path, 'init')
    (tmp_path / 'outside').symlink_to('/etc/passwd')
    result = read_project_diff(tmp_path)
    assert result['omitted'] == ['outside']
    assert result['untracked'] == ''
