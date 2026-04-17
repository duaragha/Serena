#!/bin/bash
# Dream hook: runs on SessionStart, consolidates memories if 24h+ since last run.
# Configured as an async SessionStart hook in ~/.claude/settings.json

CHATS_BIN="chats"
PY_BIN="python3"
DREAM_STAMP="$HOME/.local/share/chats/last_dream"
DREAM_LOG="$HOME/.local/share/chats/dream.log"

mkdir -p "$(dirname "$DREAM_STAMP")"

# Check if 24h+ since last dream
if [ -f "$DREAM_STAMP" ]; then
    LAST=$(cat "$DREAM_STAMP")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST))
    # 86400 = 24 hours
    if [ "$DIFF" -lt 86400 ]; then
        exit 0
    fi
fi

# Update timestamp immediately to prevent double-runs
date +%s > "$DREAM_STAMP"

# Run dream consolidation
{
    echo "=== Dream started at $(date) ==="

    # Get all memories as JSON-ish
    MEMORIES=$("$CHATS_BIN" memory 2>/dev/null)

    if [ -z "$MEMORIES" ]; then
        echo "No memories to consolidate."
        exit 0
    fi

    # Use Python to do the actual consolidation
    "$PY_BIN" -c "
from memory.store import list_memories, delete_memory, update_memory
from datetime import datetime, timedelta
import re

memories = list_memories()
if not memories:
    print('No memories found.')
    exit()

print(f'Processing {len(memories)} memories...')

# 1. Find and remove superseded memories
#    Pattern: 'Memory #X is superseded by #Y'
superseded_ids = set()
for m in memories:
    match = re.search(r'[Mm]emory #?(\d+) is superseded', m['content'])
    if match:
        superseded_ids.add(int(match.group(1)))
        # Also remove the pointer memory itself
        superseded_ids.add(m['id'])

for mid in superseded_ids:
    if delete_memory(mid):
        print(f'  Removed superseded memory #{mid}')

# 2. Convert relative dates to absolute
#    (memories with 'yesterday', 'today', 'last week' etc)
now = datetime.now()
date_words = {
    'yesterday': (now - timedelta(days=1)).strftime('%Y-%m-%d'),
    'today': now.strftime('%Y-%m-%d'),
    'tomorrow': (now + timedelta(days=1)).strftime('%Y-%m-%d'),
    'last week': (now - timedelta(weeks=1)).strftime('week of %Y-%m-%d'),
    'next week': (now + timedelta(weeks=1)).strftime('week of %Y-%m-%d'),
}

# Reload after deletions
memories = list_memories()
for m in memories:
    new_content = m['content']
    changed = False
    for word, replacement in date_words.items():
        if word.lower() in new_content.lower():
            new_content = re.sub(
                re.escape(word), replacement, new_content, flags=re.IGNORECASE
            )
            changed = True
    if changed and new_content != m['content']:
        update_memory(m['id'], content=new_content)
        print(f'  Fixed dates in memory #{m[\"id\"]}')

# 3. Find near-duplicate memories (same type, very similar content)
memories = list_memories()
seen_content = {}
for m in memories:
    # Normalize for comparison
    key = re.sub(r'\s+', ' ', m['content'].lower().strip())[:100]
    if key in seen_content:
        # Keep the newer one (higher ID)
        old_id = seen_content[key]
        if m['id'] > old_id:
            delete_memory(old_id)
            seen_content[key] = m['id']
            print(f'  Merged duplicate: kept #{m[\"id\"]}, removed #{old_id}')
        else:
            delete_memory(m['id'])
            print(f'  Merged duplicate: kept #{old_id}, removed #{m[\"id\"]}')
    else:
        seen_content[key] = m['id']

# 4. Flag very old general memories (>90 days)
memories = list_memories()
old_count = 0
for m in memories:
    if m['type'] == 'general' and m.get('updated_at'):
        try:
            updated = datetime.fromisoformat(m['updated_at'])
            if (now - updated).days > 90:
                old_count += 1
        except: pass
if old_count:
    print(f'  Note: {old_count} general memories are >90 days old')

final = list_memories()
print(f'Done. {len(final)} memories after consolidation.')
" 2>&1

    echo "=== Dream finished at $(date) ==="
} >> "$DREAM_LOG" 2>&1
