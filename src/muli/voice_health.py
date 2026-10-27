"""Fixed background voice failures shared without importing voice configuration."""

VOICE_UNAVAILABLE: str = "voice_unavailable"
VOICE_LISTENER_FAILED: str = "voice_listener_failed"
VOICE_FAILURE_CODES: frozenset[str] = frozenset({VOICE_UNAVAILABLE, VOICE_LISTENER_FAILED})
