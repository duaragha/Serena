from ui.pty_model_mask import ModelMaskStream, _rewrite


def test_launchers_keep_the_laggy_model_mask_explicitly_opt_in():
    """Every launcher that can turn the relay on must default it off.

    The launchers are found rather than listed: the GTK app moved under
    archive/ and this test spent that whole time failing on a path that no
    longer existed, which is not the same as the rule being upheld.
    """

    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    launchers = [
        path
        for path in repo.rglob("*.py")
        if ".git" not in path.parts
        and "SERENA_MODEL_MASK" in path.read_text(encoding="utf-8", errors="replace")
        and path.name != Path(__file__).name
        and path.name != "pty_model_mask.py"
    ]

    assert launchers, "no launcher reads SERENA_MODEL_MASK any more"
    for launcher in launchers:
        source = launcher.read_text(encoding="utf-8")
        assert 'os.environ.get("SERENA_MODEL_MASK", "off")' in source, launcher
        assert 'os.environ.get("SERENA_MODEL_MASK", "on")' not in source, launcher


def test_rewrites_each_codex_family_from_its_workflow_label():
    source = (
        b"sol5.6-xhigh:watch-me  Sonnet 5 - 21.8k tok\n"
        b"terra5.6-high:review  Opus 4.8 - 8.1k tok\n"
        b"luna5.6-medium:research  Haiku 4.5 - 3.2k tok\n"
    )

    assert _rewrite(source) == (
        b"sol5.6-xhigh:watch-me  Sol 5.6 - 21.8k tok\n"
        b"terra5.6-high:review  Terra 5.6 - 8.1k tok\n"
        b"luna5.6-medium:research  Luna 5.6 - 3.2k tok\n"
    )


def test_defaults_a_family_only_label_to_that_familys_generation():
    assert _rewrite(b"terra-medium:scan  Sonnet 5") == b"terra-medium:scan  Terra 5.6"
    assert _rewrite(b"luna:route  Sonnet 5") == b"luna:route  Luna 5.6"
    # Astra is a 6, not a 5.6, so the generation is per family and not one
    # default shared by all of them.
    assert _rewrite(b"astra:build  Sonnet 5") == b"astra:build  Astra 6"
    assert _rewrite(b"astra6-high:build  Opus 4.8") == b"astra6-high:build  Astra 6"


def test_preserves_real_claude_and_ambiguous_codex_rows():
    source = b"haiku45:control  Haiku 4.5\ncodex:unknown  Sonnet 5\n"
    assert _rewrite(source) == source


def test_rewrites_across_full_screen_cursor_and_colour_sequences():
    source = (
        b"\x1b[38;5;114msol5.6-xhigh:watch-me\x1b[0m"
        b"\x1b[28G\x1b[38;5;245mSonnet 5\x1b[0m - 21.8k tok"
    )
    expected = source.replace(b"Sonnet 5", b"Sol 5.6")
    assert _rewrite(source) == expected


def test_stream_rewrites_every_possible_two_chunk_split():
    source = (
        b"\x1b[2K  sol5.6-xhigh:watch-me\x1b[0m"
        b"\x1b[42G\x1b[1mSonnet 5\x1b[0m - 21.8k tok\r\n"
    )
    expected = source.replace(b"Sonnet 5", b"Sol 5.6")

    for split in range(len(source) + 1):
        stream = ModelMaskStream()
        actual = stream.feed(source[:split]) + stream.feed(source[split:]) + stream.finish()
        assert actual == expected, f"split failed at byte {split}"


def test_stream_rewrites_byte_at_a_time_without_changing_other_bytes():
    source = (
        b"header\x1b[2Kterra5.6-medium:research  Sonnet 5 - 4k\r\n"
        b"haiku45:control  Haiku 4.5 - 2k\r\nfooter"
    )
    expected = source.replace(b"Sonnet 5", b"Terra 5.6")
    stream = ModelMaskStream()
    actual = b"".join(stream.feed(bytes([byte])) for byte in source) + stream.finish()
    assert actual == expected
