"""Entry Point für den HA Self-Healing Agent."""

import uvicorn

from config.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "api.app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
