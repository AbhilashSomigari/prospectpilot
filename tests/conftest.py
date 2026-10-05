from __future__ import annotations

import os

# Unit tests never talk to real providers or collectors.
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("OTEL_ENABLED", "false")
