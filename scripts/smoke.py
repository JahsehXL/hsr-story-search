"""Exercise a deployed service without logging credentials or dialogue bodies."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    credentials = json.loads(arguments.credentials_file.read_text(encoding="utf-8"))
    base = arguments.base_url.rstrip("/") + "/api/v1/"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", "HSR-Story-Search/0.1")]
    outcomes = []

    def call(route: str, payload=None, expected=200):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(base + route, data=body, headers={"Content-Type": "application/json"})
        start = time.perf_counter()
        try:
            with opener.open(request, timeout=90) as response:
                code = response.status
                value = json.load(response)
        except urllib.error.HTTPError as error:
            code = error.code
            raw = error.read()
            try:
                value = json.loads(raw)
            except ValueError:
                value = {"detail": "Upstream returned a non-JSON error response"}
        assert code == expected, f"{route}: expected {expected}, received {code}"
        outcomes.append({"route": route, "http_status": code, "elapsed_ms": round((time.perf_counter() - start) * 1000)})
        return value

    call("health")
    status = call("status")
    if status.get("public_readonly"):
        call("search", {"query": "星穹列车", "mode": "text", "limit": 1})
        call("admin/settings", expected=401)
    else:
        call("search", {"query": "星穹列车"}, expected=401)
        call("auth/login", {"code": credentials["access_code"], "admin": False})
        call("admin/settings", expected=403)
    catalog = call("catalog")
    assert catalog["worlds"], "World catalog is empty"
    for query in ("星穹列车", "老杨", "星穹列午", "长日入夜行"):
        result = call("search", {"query": query, "filters": {}, "mode": "fuzzy" if query == "星穹列午" else "text", "limit": 5})
        assert result["results"], "Expected a lexical/alias/fuzzy/name match"
        first = result["results"][0]
        detail = call("missions/" + first["mission_id"] + "?line_id=" + first["line_id"])
        assert detail["mission"] and detail["lines"]
    call("auth/login", {"code": credentials["admin_code"], "admin": True})
    settings = call("admin/settings")
    assert "api_key" not in settings["embedding"], "Settings disclose an API key"
    call("admin/diagnostics")
    output = {"base_url": arguments.base_url, "checks": outcomes, "passed": len(outcomes)}
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
