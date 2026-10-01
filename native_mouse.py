"""One bounded native press with a finite hold and unconditional release."""

import asyncio
from nodriver import cdp


PRESS_HOLD_SEC = 0.1


async def press_and_release(tab, x, y):
    try:
        await asyncio.wait_for(tab.send(cdp.input_.dispatch_mouse_event(
            "mousePressed", x=x, y=y, button=cdp.input_.MouseButton.LEFT,
            buttons=1, click_count=1,
        )), timeout=2)
        await asyncio.sleep(PRESS_HOLD_SEC)
    finally:
        await asyncio.wait_for(tab.send(cdp.input_.dispatch_mouse_event(
            "mouseReleased", x=x, y=y, button=cdp.input_.MouseButton.LEFT,
            buttons=0, click_count=1,
        )), timeout=2)
