import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.locket_archive_sync import (
    CHUNK_SIZE,
    MAX_SESSION_PASSAGES,
    _cap_passages,
    chunk_recall_text,
    sanitize_recall_text,
)
import memory.store as memory_store


class LocketArchiveSanitizerTests(unittest.TestCase):
    def test_redacts_secrets_and_machine_context(self):
        secret = "sk-ant-" + "x" * 40
        text = "\n".join(
            [
                "keep this answer",
                "<system-reminder>drop this setup</system-reminder>",
                f"ANTHROPIC_API_KEY={secret}",
                "postgresql://user:pass@example.test/locket",
                "opaque=FKoBvzMVDf20kAurAgQ10GlioB8lGz3eu7ICev5GQgY",
            ]
        )

        cleaned = sanitize_recall_text(text)

        self.assertIn("keep this answer", cleaned)
        self.assertNotIn("drop this setup", cleaned)
        self.assertNotIn(secret, cleaned)
        self.assertNotIn("user:pass", cleaned)
        self.assertNotIn("FKoBvzMVDf20kAurAgQ10GlioB8lGz3eu7ICev5GQgY", cleaned)
        self.assertIn("[REDACTED", cleaned)

    def test_chunks_are_bounded_without_losing_words(self):
        original = "word " * 1400
        chunks = chunk_recall_text(original)

        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= CHUNK_SIZE for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), original.split())

    def test_oversized_session_keeps_head_and_tail_with_explicit_cap(self):
        passages = [
            {"messagePosition": index, "content": "x"}
            for index in range(MAX_SESSION_PASSAGES + 5)
        ]

        capped, truncated = _cap_passages(passages)

        self.assertTrue(truncated)
        self.assertLessEqual(len(capped), MAX_SESSION_PASSAGES)
        self.assertEqual(capped[0]["messagePosition"], 0)
        self.assertEqual(capped[-1]["messagePosition"], MAX_SESSION_PASSAGES + 4)


class MemoryProvenanceTests(unittest.TestCase):
    def test_capture_and_edit_preserve_source_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = memory_store.MEMORY_DIR
            memory_store.MEMORY_DIR = Path(tmp)
            try:
                with (
                    patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-session-42"}, clear=False),
                    patch("core.indexer.get_session", return_value={"display_title": "Recall work"}),
                    patch("memory.locket_mirror.mirror_add") as mirror_add,
                ):
                    memory_id = memory_store.add_memory("Remember this", "project")
                    memory_store.update_memory(memory_id, content="Remember this exactly")

                saved = memory_store.get_memory(memory_id)
                self.assertIsNotNone(saved)
                self.assertEqual(saved["source_session_id"], "codex-session-42")
                self.assertEqual(saved["source_agent"], "codex")
                self.assertEqual(saved["source_title"], "Recall work")
                mirror_add.assert_called_once()
            finally:
                memory_store.MEMORY_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
