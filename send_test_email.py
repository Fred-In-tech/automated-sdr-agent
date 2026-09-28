"""Send ONE real sample email of your sequence to yourself. Same as `sdr test-email`.

Usage:
  python3 send_test_email.py                          # the first email, to your own mailbox
  python3 send_test_email.py --to you@example.com     # to another address of yours
  python3 send_test_email.py --step 2                 # follow-up 1, etc.
  python3 send_test_email.py --reply interested       # the "send me the link" welcome reply
  python3 send_test_email.py --signature logo         # force the logo signature (A/B test)

Kept for older instructions and scripts; the logic lives in cli.py (cmd_test_email) and
core/setup_sample.py.
"""

import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main(["test-email", *sys.argv[1:]]))
