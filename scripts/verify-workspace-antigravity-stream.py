"""Explicit native Gemini coding-view probe; never touches an existing chat."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from core.workspace_antigravity import AntigravityWorkspace


async def main(root):
    root.mkdir(parents=True, exist_ok=False)
    events = []
    async def publish(event):
        events.append(event)
    targets = []
    async def checkpoint(target):
        targets.append(target)
    owner = AntigravityWorkspace(session_id='new:' + str(uuid4()), cwd=root, publish=publish)
    async def ask(text):
        await owner.submit([{'type': 'text', 'text': text}])
        async with asyncio.timeout(120):
            while owner.active_turn:
                if owner.state == 'unavailable':
                    raise RuntimeError(events[-1])
                await asyncio.sleep(.05)
        result = [e for e in events if e['method'] == 'turn/completed'][-1]
        assert result['params']['turn']['status'] == 'completed', result
        return result['params']['turn']['providerOriginal']['response']
    try:
        await owner.create(checkpoint=checkpoint)
        sid = owner.session_id
        await owner.list_models()
        first = await ask('Remember the marker violet-copper-27. Reply with that marker only. Do not use tools or modify files.')
        second = await ask('Repeat the marker from my previous message. Nothing else. Do not use tools.')
        assert 'violet-copper-27' in first and 'violet-copper-27' in second
        await owner.close()
        owner = AntigravityWorkspace(session_id=sid, cwd=root, publish=publish)
        await owner.open()
        third = await ask('Repeat the marker from our earlier messages. Nothing else. Do not use tools.')
        assert 'violet-copper-27' in third
        from core.gemini_scanner import parse_gemini_metadata, resumable_conversation_path
        indexed = parse_gemini_metadata(resumable_conversation_path(sid))
        assert indexed.cwd == str(root) and 'violet-copper-27' in indexed.first_message
        print(json.dumps({'ok': True, 'session_id': sid, 'turns': 3,
                          'native_resume_retains_context': True, 'existing_chats_untouched': True}))
    finally:
        await owner.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.directory.resolve()))
