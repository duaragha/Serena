import json
import re

import pytest
from flask import Flask

from ui.workspace_app import install_workspace


@pytest.mark.parametrize("query,expected", [("", False), ("?resume=1", True), ("?resume=0", False)])
def test_code_open_explicit_resume_boot_without_launch(tmp_path, query, expected):
    app = Flask(__name__)
    host = install_workspace(app, tmp_path / "workspace.db",
                             describe=lambda sid: {"session_id": sid, "agent": "claude"})
    try:
        page = app.test_client().get("/workspace/exact" + query)
        assert page.status_code == 200
        boot = json.loads(re.search(r'<script id="workspace-boot" type="application/json">(.*?)</script>', page.text).group(1))
        assert boot["autoResume"] is expected
        assert host._loop is None
    finally:
        host.shutdown()
