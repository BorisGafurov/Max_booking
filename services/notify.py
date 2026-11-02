"""notify service facade redirecting to core_bot.services.notify."""

import sys
import core_bot.services.notify as _mod

sys.modules[__name__] = _mod
