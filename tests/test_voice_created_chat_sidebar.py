from __future__ import annotations

from ui import web


def test_voice_chats_placeholder_stays_empty_directly_below_fleet() -> None:
    html = web.HTML

    assert "const voiceChats = [];" in html
    assert "_isVoiceCreatedSession" not in html
    assert "session.resident_work === true" not in html
    assert "voiceSet" not in html
    assert 'data-testid="voice-chats-header"' in html
    assert 'data-testid="voice-chats-section"' in html
    assert "Voice Chats (' + voiceChats.length + ')" in html
    assert "fleetChats: true, voiceChats: true" in html
    assert "typeof c.voiceChats === 'boolean'" in html
    assert "function toggleVoiceChatsCollapsed()" in html

    fleet_section = html.index("if (fleetChats.length)")
    voice_section = html.index('data-testid="voice-chats-header"')
    starred_section = html.index("if (starred.length)")
    assert fleet_section < voice_section < starred_section
