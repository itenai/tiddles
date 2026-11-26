"""Compatibility entry point. Provider implementation lives in providers/api.py."""
import os
from contextlib import asynccontextmanager

from providers.api import app

# ZeroGPU is Hugging Face-only. Disable it by default; opt in via env var.
ZEROGPU_ENABLED = os.getenv("FID_ENABLE_ZEROGPU", "0") == "1"

if ZEROGPU_ENABLED:
    try:
        import spaces

        @spaces.GPU(duration=5)
        def _zerogpu_startup_check():
            return True
    except ImportError:
        ZEROGPU_ENABLED = False

if not ZEROGPU_ENABLED:
    def _zerogpu_startup_check():
        return True

_original_lifespan_context = app.router.lifespan_context

@asynccontextmanager
async def _lifespan(application):
    print("LIFESPAN: begin", flush=True)
    if ZEROGPU_ENABLED:
        import asyncio
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_zerogpu_startup_check), timeout=10
            )
        except asyncio.TimeoutError:
            print("LIFESPAN: zerogpu TIMEOUT, continuing", flush=True)
    print("LIFESPAN: entering provider lifespan", flush=True)
    async with _original_lifespan_context(application):
        print("LIFESPAN: provider startup complete", flush=True)
        yield
    print("LIFESPAN: exit", flush=True)

app.router.lifespan_context = _lifespan

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
