"""Local browser discovery and a trusted-input fixture; no live site requests."""

import os
import shutil

CHROME = os.environ.get("CHROME_BIN") or shutil.which("google-chrome") or shutil.which("chromium")
FIXTURE = '<!doctype html><title>Local Vote fixture</title>\n<style>\n  body { margin: 0; }\n  button { margin: 100px; width: 180px; height: 50px; }\n  #cover { position: fixed; inset: 0; z-index: 10; background: white; }\n</style>\n<div id="ad"></div><button id="vote"><span>Vote</span></button>\n<script>\nwindow.events = [];\nwindow.votes = 0;\nwindow.lastPress = null;\nfor (const type of [\'pointerdown\', \'pointerup\', \'click\']) {\n  document.addEventListener(type, event => {\n    const button = event.target.closest(\'#vote\');\n    if (!button) return;\n    events.push({type, trusted: event.isTrusted, time: performance.now()});\n    if (type === \'pointerdown\') lastPress = button;\n    // Model a UI needing the normal input sequence, not just a JS click().\n    if (type === \'click\' && lastPress === button && event.isTrusted) votes++;\n  });\n}\n</script>'
