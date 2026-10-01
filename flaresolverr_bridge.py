"""Expose an existing FlareSolverr Chrome to the local vote process.

Mounted into the pinned upstream container. No credentials, cookie transfer,
new Chrome, navigation or challenge-success assertion belongs in this bridge.
"""

import os
import re
import runpy
import sys


def browser_connection(storage, session_id):
    session = storage.sessions.get(session_id)
    if session is None:
        raise ValueError("Existing session required")
    address = session.driver.capabilities.get("goog:chromeOptions", {}).get("debuggerAddress", "")
    match = re.fullmatch(r"(?:127\.0\.0\.1|localhost):([0-9]{1,5})", address)
    if not match or not 0 < int(match[1]) < 65536:
        raise ValueError("Local Chrome debugger unavailable")
    handle = session.driver.current_window_handle.removeprefix("CDwindow-")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", handle):
        raise ValueError("Current Chrome page unavailable")
    return {"debuggerAddress": "127.0.0.1:" + match[1], "targetId": handle}


def install(service, response_type):
    original = service._controller_v1_handler

    def handle(req):
        if req.cmd != "sessions.connect":
            return original(req)
        connection = browser_connection(service.SESSIONS_STORAGE, req.session)
        return response_type({"status": "ok", "session": req.session,
                              "solution": connection})

    service._controller_v1_handler = handle


if __name__ == "__main__":
    sys.path.insert(0, "/app")
    import flaresolverr_service
    from dtos import V1ResponseBase

    install(flaresolverr_service, V1ResponseBase)
    os.environ["HOST"] = "127.0.0.1"
    os.environ["LOG_HTML"] = "false"
    os.environ["LOG_LEVEL"] = "warn"
    runpy.run_path("/app/flaresolverr.py", run_name="__main__")
