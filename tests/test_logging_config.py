import logging

from app.logging_config import configure_app_logging


def test_configure_app_logging_enables_info_records():
    logger = configure_app_logging()

    assert logger.name == "patent_service"
    assert logger.level == logging.INFO
    assert logger.isEnabledFor(logging.INFO)
