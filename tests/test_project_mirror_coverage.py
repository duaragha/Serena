"""Every chat done in a project gets a mirror, including new projects.

The canonical nested tree under ~/.claude/projects/Projects/<group>/<project>/
is built from a hardcoded table of project names. A project nobody has added to
that table returns None and is silently skipped -- full_tracker was missing, so
twenty-eight chats had no mirror and nothing said so.

The directory layout already carries the answer, so it is the fallback. The
table stays first and authoritative, because it encodes decisions the layout
does not: a chat sitting directly in the frameworth group belongs to
frameworth/general, not to the group directory, and a renamed project keeps its
old home.
"""

from __future__ import annotations

import pytest

from core.project_mirror import canonical_subpath


class TestTheTableStillWins:
    """Reading the path first re-answered chats that were already right."""

    def test_a_chat_in_the_group_root_is_not_the_group(self) -> None:
        assert canonical_subpath("", "/home/raghav/Documents/Projects/frameworth") == (
            "Projects/frameworth/general"
        )

    def test_a_renamed_project_keeps_its_old_home(self) -> None:
        """konpeki was ai-automation-agency; its old chats stay together."""
        assert canonical_subpath("", "/home/raghav/Documents/Projects/personal_projects/konpeki") == (
            "Projects/personal_projects/konpeki"
        )

    def test_a_sub_app_belongs_to_its_project(self) -> None:
        assert canonical_subpath(
            "", "/home/raghav/Documents/Projects/personal_projects/konpeki/apps/landing"
        ) == "Projects/personal_projects/konpeki"


class TestAProjectNobodyListed:
    """The gap the table could not report."""

    def test_full_tracker_is_mirrored(self) -> None:
        assert canonical_subpath(
            "", "/home/raghav/Documents/Projects/personal_projects/full_tracker"
        ) == "Projects/personal_projects/full_tracker"

    def test_a_project_invented_tomorrow_is_mirrored(self) -> None:
        """The point of deriving: no table edit per new project."""
        assert canonical_subpath(
            "", "/home/raghav/Documents/Projects/personal_projects/brand_new_thing"
        ) == "Projects/personal_projects/brand_new_thing"

    def test_the_windows_checkout_lands_in_the_same_place(self) -> None:
        """Both machines put projects under a directory called Projects."""
        assert canonical_subpath(
            "", "C:\\Users\\ragha\\Projects\\personal_projects\\brand_new_thing"
        ) == "Projects/personal_projects/brand_new_thing"

    def test_a_personal_project_is_spelled_with_underscores(self) -> None:
        """The group's convention, so one project is not two directories."""
        assert canonical_subpath(
            "", "/home/raghav/Documents/Projects/personal_projects/some-tool"
        ) == "Projects/personal_projects/some_tool"

    def test_other_groups_keep_the_name_on_disk(self) -> None:
        assert canonical_subpath(
            "", "/home/raghav/Documents/Projects/frameworth/shopify-free-gift-app"
        ) == "Projects/frameworth/shopify-free-gift-app"


class TestWhatIsNotProjectWork:
    @pytest.mark.parametrize("cwd", ["/home/raghav", "C:\\Users\\ragha", "", None])
    def test_a_home_directory_is_not_a_project(self, cwd) -> None:
        assert canonical_subpath("", cwd) is None

    def test_a_slug_is_not_split_back_into_a_path(self) -> None:
        """Separators are already flattened to '-', and project names contain
        hyphens, so a slug cannot be taken apart again without inventing
        structure. Only real paths are derived from."""
        assert canonical_subpath("-home-raghav-Documents-Projects-some-unlisted-thing", "") is None
