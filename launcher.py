"""Launch the bundled local service and open its browser interface."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from backend.app import create_app


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Star Rail story search local launcher")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        import sqlite3
        import sqlite_vec
        bundle = Path(getattr(sys, "_MEIPASS", application_directory()))
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE words USING fts5(text)")
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            connection.execute("CREATE VIRTUAL TABLE vectors USING vec0(embedding float[2])")
        assert (bundle / "frontend" / "dist" / "index.html").is_file()
        from backend.version_metadata import load_metadata
        assert load_metadata()
        print("SELF_TEST_OK: SQLite FTS5, vector extension, web assets, version metadata, bundled backend")
        return
    data_dir = args.data_dir or Path(os.environ.get("HSR_DATA_DIR", application_directory() / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    app = create_app(data_dir=data_dir, mode="local", bootstrap_token=token)

    # Bind first so that another application can never receive the bootstrap token.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", args.port))
    except OSError:
        listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"Story search: {url}")
    print("Close this window or press Ctrl+C to stop the local service.")

    def open_when_ready() -> None:
        for _ in range(120):
            try:
                with urllib.request.urlopen(url + "api/v1/health", timeout=1):
                    if not args.no_browser:
                        webbrowser.open(url + "#bootstrap=" + token)
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)

    threading.Thread(target=open_when_ready, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
