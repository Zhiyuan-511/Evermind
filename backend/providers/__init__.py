"""Evermind provider plugins.

Per-vendor call adapters that encapsulate the differences between
Kimi / Qwen / DeepSeek / Zhipu / Doubao / MiniMax. Each provider lives
in its own module and registers itself with the `registry`.

The `ai_bridge.py` orchestrator looks up the right provider via
`providers.registry.resolve_provider(model_name)` and delegates the
request shaping + stream decoding + retry policy to that provider.

This keeps the v5.8.x tangle of `if _provider == "kimi" / elif "deepseek"`
out of the hot path and makes it safe to add a new vendor without
touching the orchestrator.
"""

from .base import (  # noqa: F401
    BaseProvider,
    ChatChunk,
    ChatRequest,
    OpenAICompatProvider,
    ProviderError,
    ProviderRetryHint,
)
from .registry import (  # noqa: F401
    known_providers,
    register_provider,
    reset_registry_for_tests,
    resolve_provider,
)

# Importing the concrete providers triggers @register_provider side effects.
# Order determines match precedence when multiple providers claim a prefix.
# Chinese vendors first (most Evermind users today), then western.
from . import deepseek  # noqa: F401,E402
from . import kimi  # noqa: F401,E402
from . import qwen  # noqa: F401,E402
from . import zhipu  # noqa: F401,E402
from . import doubao  # noqa: F401,E402
from . import minimax  # noqa: F401,E402

from . import openai  # noqa: F401,E402
from . import anthropic  # noqa: F401,E402
from . import gemini  # noqa: F401,E402
from . import xai  # noqa: F401,E402
from . import mistral  # noqa: F401,E402
from . import meta_llama  # noqa: F401,E402
