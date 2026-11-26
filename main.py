"""Compatibility entry point. Provider implementation lives in providers/api.py."""
import os
from contextlib import asynccontextmanager

from providers.api import app

# Hugging Face ZeroGPU detects decorated functions in the entry module.
try:
    import spaces

    @spaces.GPU(duration=5)
    def _zerogpu_startup_check():
        """Executed at application startup to register/check ZeroGPU."""
        return True

    ZEROGPU_AVAILABLE = True
except ImportError:
    ZEROGPU_AVAILABLE = False

    class spaces:
        @staticmethod
        def GPU(func=None, duration=None):
            if func is None:
                return lambda wrapped: wrapped
            return func

    def _zerogpu_startup_check():
        """Local fallback when the Hugging Face spaces package is unavailable."""
        return True


# Preserve the provider API's existing lifespan and run the ZeroGPU check
# before its normal startup initialization.
_original_lifespan_context = app.router.lifespan_context

@asynccontextmanager
async def _zerogpu_lifespan(application):
    _zerogpu_startup_check()
    async with _original_lifespan_context(application):
        yield

app.router.lifespan_context = _zerogpu_lifespan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
