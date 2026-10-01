"""Use a free local FlareSolverr session without moving its clearance cookies."""

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from contextlib import suppress
from urllib.parse import urlsplit

import nodriver as uc
import requests


class ServiceError(RuntimeError):
    """Safe service failure; never includes response HTML, cookies or URLs."""


def service_url():
    value = os.environ.get("FLARESOLVERR_URL", "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ServiceError("FlareSolverr must use the local runner")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ServiceError("Invalid local service port") from exc
    if port is None:
        raise ServiceError("Explicit local service port required")
    return f"http://127.0.0.1:{port}"


class Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.session_id = "vote-" + uuid.uuid4().hex

    def _request(self, command, timeout, **fields):
        # A proxy inherited from the runner must not send this local API elsewhere.
        with requests.Session() as session:
            session.trust_env = False
            try:
                response = session.post(self.base_url + "/v1", json={
                    "cmd": command, "session": self.session_id, **fields,
                }, timeout=(2, timeout))
                data = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ServiceError("Local FlareSolverr connection failed") from exc
        if not isinstance(data, dict):
            raise ServiceError("Invalid FlareSolverr response")
        return data

    async def request(self, command, timeout=10, **fields):
        return await asyncio.to_thread(self._request, command, timeout, **fields)

    async def destroy(self):
        await self.request("sessions.destroy", timeout=3)


class SessionBrowser(uc.Browser):
    """CDP client only: the container owns Chrome and its sensitive profile."""

    def __init__(self, config, client):
        super().__init__(config)
        self._flare_client = client
        self._flare_started = False
        self._flare_close_task = None
        self._security_profile_path = config.user_data_dir

    async def start(self):
        if not self._flare_started:
            await super().start()
            self._flare_started = True

    async def _close_session(self):
        try:
            for tab in list(self.tabs):
                with suppress(Exception):
                    await asyncio.wait_for(tab.aclose(), 1)
            with suppress(Exception):
                await asyncio.wait_for(super().aclose(), 1)
        finally:
            try:
                await self._flare_client.destroy()
            finally:
                shutil.rmtree(self._security_profile_path, ignore_errors=True)

    async def aclose(self):
        if self._flare_close_task is None:
            self._flare_close_task = asyncio.create_task(self._close_session())
        # Preserve destruction when the caller's bounded cleanup times out.
        await asyncio.shield(self._flare_close_task)

    def stop(self):
        if self._flare_close_task is None:
            with suppress(RuntimeError):
                self._flare_close_task = asyncio.get_running_loop().create_task(self._close_session())


async def start(initial_url=None):
    base = service_url()
    if base is None:
        raise ServiceError("Local FlareSolverr is not enabled")
    client = Client(base)
    browser = None
    profile = None
    try:
        created = await client.request("sessions.create", timeout=45)
        if created.get("status") != "ok" or created.get("session") != client.session_id:
            raise ServiceError("FlareSolverr session creation failed")
        if initial_url and initial_url != "about:blank":
            result = await client.request("request.get", timeout=75, url=initial_url,
                                          maxTimeout=60000, returnOnlyCookies=True,
                                          disableMedia=False)
            # FlareSolverr hardcodes solution.status=200. Inspect the real page
            # afterwards, including when its challenge solver reports failure.
            outcome = "finished" if result.get("status") == "ok" else "incomplete"
            print("  → Free FlareSolverr navigation " + outcome + "; inspecting the same Chrome")
        connected = await client.request("sessions.connect")
        solution = connected.get("solution")
        solution = solution if isinstance(solution, dict) else {}
        address = solution.get("debuggerAddress", "")
        address = address if isinstance(address, str) else ""
        match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", address)
        if connected.get("status") != "ok" or not match or not 0 < int(match[1]) < 65536:
            raise ServiceError("Existing FlareSolverr Chrome is unavailable")
        profile = tempfile.mkdtemp(prefix="flare-cdp-client-")
        config = uc.Config(host="127.0.0.1", port=int(match[1]), user_data_dir=profile,
                           browser_executable_path="/remote/flaresolverr/chrome", headless=False)
        browser = SessionBrowser(config, client)
        await asyncio.wait_for(browser.start(), 20)
        if not browser.tabs:
            raise ServiceError("FlareSolverr Chrome has no usable tab")
        # Selenium may leave a support tab: retain the page it navigated to.
        pages = list(browser.tabs)
        selected = next((tab for tab in pages if str(tab.target.target_id) == solution.get("targetId")), None)
        if selected is None:
            raise ServiceError("FlareSolverr current page could not be attached")
        browser.targets.remove(selected)
        browser.targets.insert(0, selected)
        await asyncio.wait_for(selected.attach(), 8)
        return browser
    except BaseException:
        if browser is not None:
            with suppress(Exception):
                await browser.aclose()
        else:
            with suppress(Exception):
                await client.destroy()
            if profile:
                shutil.rmtree(profile, ignore_errors=True)
        raise
