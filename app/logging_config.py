import logging


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_app_logging(level: int = logging.INFO) -> logging.Logger:
    """Make patent-service logs visible without replacing server log handlers."""
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=level, format=LOG_FORMAT)

    app_logger = logging.getLogger("patent_service")
    app_logger.setLevel(level)
    return app_logger
