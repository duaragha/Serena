import asyncio
import base64
import io
import threading
from types import SimpleNamespace

from PIL import Image, ImageDraw

from core.computer_watch import WatchFrames


def frame(width, title="AWS"):
    image = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(image).rectangle((0, 0, width, 179), fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {
        "data": base64.b64encode(buffer.getvalue()).decode(),
        "rect": {"x": 0, "y": 0, "width": 320, "height": 180},
        "context": {"id": "1", "title": title},
        "captured_at": 1,
    }


def test_churn_preserves_draft_but_cumulative_change_invalidates_it():
    async def run():
        session = SimpleNamespace(
            id="test", cancelled=threading.Event(), observation="prior guidance",
            observation_preview="current draft", observation_state="thinking",
        )
        pending = [frame(4), frame(8), frame(12), frame(20)]
        states = []

        def observe(_sid):
            return pending.pop(0)

        def event(*args, **kwargs):
            states.append((watch.superseded, session.observation_preview))
            if not pending:
                session.cancelled.set()

        watch = WatchFrames(SimpleNamespace(observe=observe, event=event), session)
        watch.begin_inspection(frame(0))
        await watch.run()
        assert states[:3] == [(False, "current draft")] * 3
        assert states[-1] == (True, "")
        await watch.close()

    asyncio.run(run())


def test_title_change_invalidates_even_identical_pixels():
    from core.computer_watch import changed, sample

    old, new = frame(0), frame(0, "Different tab")
    with sample(old) as a, sample(new) as b:
        assert changed(old, new, a, b, threshold=0.05)
