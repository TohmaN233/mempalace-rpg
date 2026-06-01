"""RPG narrative memory kernel for MemPalace.

The public API is intentionally host-agnostic.  Game packages can treat this as
an external service/library and keep their own state engines untouched.
"""

from .adapter import MempalaceEpisodeAdapter, NullEpisodeAdapter, RecordingEpisodeAdapter
from .budget import RecallBudget, budget_for_tier
from .kernel import RpgMemoryKernel
from .models import MemoryPack, SceneEventInput
from .settings import DEFAULT_MEMO_SETTINGS, load_memo_settings
from .tavern_importer import import_taverndb

__all__ = [
    "DEFAULT_MEMO_SETTINGS",
    "MemoryPack",
    "MempalaceEpisodeAdapter",
    "NullEpisodeAdapter",
    "RecallBudget",
    "RecordingEpisodeAdapter",
    "RpgMemoryKernel",
    "SceneEventInput",
    "budget_for_tier",
    "import_taverndb",
    "load_memo_settings",
]
