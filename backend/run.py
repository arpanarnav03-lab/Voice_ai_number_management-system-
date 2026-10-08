"""Start the app:  python run.py

Without APP_PASSWORD the app has no login, so it only starts on the local machine."""
import logging
import sys

import uvicorn

from app.config import Settings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.app_password and settings.host not in LOOPBACK:
        sys.exit(
            f"Refusing to start on host {settings.host!r}: APP_PASSWORD is not set, so there is no login "
            "and anyone who can reach the app could place calls. Set APP_PASSWORD, or use HOST=127.0.0.1."
        )
    uvicorn.run("app.main:create_app", factory=True, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
