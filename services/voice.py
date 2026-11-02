"""voice service facade redirecting to core_bot.services.voice."""

import sys
import core_bot.services.voice as _mod

sys.modules[__name__] = _mod
