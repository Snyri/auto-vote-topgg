"""Run the complete suite; CI must actually execute the browser fixtures."""

import os
import unittest


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover("."))
    if os.environ.get("REQUIRE_BROWSER_TESTS") == "1" and result.skipped:
        print("CI skipped required fixtures:")
        for test, reason in result.skipped:
            print(f"{test}: {reason}")
        raise SystemExit(1)
    raise SystemExit(0 if result.wasSuccessful() else 1)
