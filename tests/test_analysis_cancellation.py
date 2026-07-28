import asyncio

import pytest

from app.analysis.cancellation import AnalysisCancellation, AnalysisCancelled
from app.api.routes import _run_monitored_analysis


class DisconnectedRequest:
    async def is_disconnected(self):
        return True


def test_disconnect_cancels_analysis_and_waits_for_cleanup():
    cancellation = AnalysisCancellation()
    cleaned_up = asyncio.Event()

    async def analysis():
        try:
            while True:
                cancellation.raise_if_cancelled()
                await asyncio.sleep(0.01)
        finally:
            cleaned_up.set()

    async def run():
        with pytest.raises(AnalysisCancelled):
            await _run_monitored_analysis(
                http_request=DisconnectedRequest(),
                cancellation=cancellation,
                analysis=analysis(),
                timeout_seconds=5,
            )
        assert cancellation.cancelled is True
        assert cleaned_up.is_set() is True

    asyncio.run(run())
