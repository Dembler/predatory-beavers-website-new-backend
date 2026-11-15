import asyncio
import sys

import uvicorn

from predatory_beavers.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    if sys.platform == "win32" and settings.database_url.startswith("postgresql+"):
        # psycopg async requires a selector loop; SQLite keeps Windows' default.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "predatory_beavers.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_ips,
        log_config=None,
        access_log=False,
    )
