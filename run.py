import uvicorn

from predatory_beavers.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "predatory_beavers.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_config=None,
        access_log=False,
    )
