"""`python -m m97`, which is what the systemd unit runs."""

import sys

from .cli import main

sys.exit(main())
