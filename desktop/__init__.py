"""Desktop shell for the chats web UI via pywebview.

Keeps the existing Flask app untouched. Starts it on a private loopback
port in a background thread and opens a native OS webview window.

Delete this folder to remove the desktop shell; the web UI remains.
"""
