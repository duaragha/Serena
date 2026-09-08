import subprocess

from ui import web
from ui.workspace_git import repository_changes


def test_changes_are_read_only_and_preserve_paths(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    (tmp_path / "old name.txt").write_text("original\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    git("mv", "old name.txt", "new name.txt")
    (tmp_path / "untracked.txt").write_text("untracked\n")
    before = (tmp_path / ".git" / "index").read_bytes()
    result = repository_changes(str(tmp_path))
    assert result["is_git"] is True
    assert result["root"] == str(tmp_path)
    assert {"status": "R", "path": "new name.txt", "original_path": "old name.txt"} in result[
        "changes"
    ]
    assert {"status": "??", "path": "untracked.txt"} in result["changes"]
    assert (tmp_path / ".git" / "index").read_bytes() == before


def test_non_git_and_missing_cwd(tmp_path):
    assert repository_changes(str(tmp_path)) == {"is_git": False, "changes": []}
    client = web.app.test_client()
    assert client.get("/api/workspace-changes").status_code == 400
    assert (
        client.get(
            "/api/workspace-changes", query_string={"cwd": str(tmp_path / "missing")}
        ).status_code
        == 404
    )


def test_failure_is_honest(tmp_path, monkeypatch):
    from ui import workspace_git

    monkeypatch.setattr(
        workspace_git,
        "repository_changes",
        lambda cwd: (_ for _ in ()).throw(OSError("missing git")),
    )
    response = web.app.test_client().get(
        "/api/workspace-changes", query_string={"cwd": str(tmp_path)}
    )
    assert response.status_code == 503
    assert "Unable" in response.json["error"]
