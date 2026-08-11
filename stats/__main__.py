import uvicorn

from stats.app import create_app
from stats.settings import get_stats_settings


def main() -> None:
    settings = get_stats_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.stats_host,
        port=settings.stats_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
