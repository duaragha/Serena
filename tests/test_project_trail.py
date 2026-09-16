from pathlib import Path

from ui.web import _project_trail


def test_trail_adds_one_parent_level():
    home = str(Path.home())
    assert _project_trail("", f"{home}/Documents/Projects/frameworth/it") == "frameworth/it"
    assert _project_trail("", f"{home}/Documents/Projects/serena") == "Projects/serena"


def test_trail_keeps_home_readable():
    home = str(Path.home())
    assert _project_trail("", home) == "Home"
    assert _project_trail("", f"{home}/gemini") == "~/gemini"


def test_trail_decodes_slugged_project_dirs():
    slug = "-" + str(Path.home()).strip("/").replace("/", "-") + "-Documents-Projects-frameworth-it"
    assert _project_trail(slug) == "frameworth/it"
