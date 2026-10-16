#!/usr/bin/env python3
"""Compatibility command for the AniGame voting workflow."""

import nodriver as uc


if __name__ == "__main__":
    from anigame_vote import main
    raise SystemExit(uc.loop().run_until_complete(main()))
