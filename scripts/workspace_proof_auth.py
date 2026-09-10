"""Keep opt-in native proofs away from working Codex credentials."""

import json
import os
from pathlib import Path


def read_test_auth(auth_home):
    if not auth_home:
        raise ValueError("An explicit separate --auth-home test profile is required")
    source = Path(auth_home).expanduser().resolve() / "auth.json"
    protected = {Path.home() / ".codex"}
    if os.environ.get("CODEX_HOME"):
        protected.add(Path(os.environ["CODEX_HOME"]).expanduser())
    for home in protected:
        auth_file = home.resolve() / "auth.json"
        if source == auth_file or (source.exists() and auth_file.exists() and source.samefile(auth_file)):
            raise ValueError("The proof cannot use the normal or active Codex login; use a separate test profile")
    auth = json.loads(source.read_text())
    if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens"):
        raise RuntimeError("An existing ChatGPT subscription login is required")
    return auth
