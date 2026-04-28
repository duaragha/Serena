"""pywebview launcher — starts the Flask UI on loopback and opens it in a native window."""

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

from core.indexer import update_index, update_knowledge_index
from ui.web import app as flask_app


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _serve(host: str, port: int) -> None:
    flask_app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def run(width: int = 1400, height: int = 900) -> None:
    print("Updating session index...")
    update_index()
    print("Updating knowledge index...")
    update_knowledge_index()

    host = "127.0.0.1"
    port = _find_free_port()
    url = f"http://{host}:{port}"

    threading.Thread(target=_serve, args=(host, port), daemon=True).start()

    if not _wait_for_server(url):
        print(f"Flask didn't start at {url}")
        return

    print(f"Opening desktop window -> {url}")
    window = webview.create_window(
        title="Chats",
        url=url,
        width=width,
        height=height,
        min_size=(900, 600),
    )

    def on_drop(event):
        print(f"[drop] received: keys={list(event.keys())}", flush=True)
        files = event.get("dataTransfer", {}).get("files") or []
        print(f"[drop] {len(files)} file(s): {[f.get('name') for f in files]}", flush=True)
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        print(f"[drop] resolved paths: {paths}", flush=True)
        if not paths:
            return
        js = f"window.onFileDropped && window.onFileDropped({json.dumps(paths)})"
        try:
            window.evaluate_js(js)
        except Exception as e:
            print(f"[drop] evaluate_js failed: {e}", flush=True)

    def on_dragover(event):
        pass  # handler must exist for prevent_default to fire on dragover

    def bind(_win):
        try:
            window.dom.document.events.drop     += DOMEventHandler(on_drop,     True, True)
            window.dom.document.events.dragover += DOMEventHandler(on_dragover, True, True)
            print("[drop] handlers bound", flush=True)
        except Exception as e:
            print(f"[drop] bind failed: {e}", flush=True)

    webview.start(bind, window)


if __name__ == "__main__":
    run()
