"""Inspect AniGame authentication and voting availability without submitting a vote."""

import nodriver as uc


if __name__ == "__main__":
    from anigame_vote import main
    raise SystemExit(uc.loop().run_until_complete(main(inspect_only=True)))
