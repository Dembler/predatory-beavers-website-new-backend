import asyncio
import logging

from predatory_beavers.observability import configure_logging
from predatory_beavers.settings import get_settings

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    """Worker composition root; notification jobs are added in a later milestone."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    logger.info("Worker skeleton started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_worker())
