import asyncio

from src.app.analysis_notifications import send_slow_alert_if_needed


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


def test_slow_alert_sends_message_once():
    async def run() -> list[str]:
        message = FakeMessage()
        await send_slow_alert_if_needed(message.answer, delay_seconds=0)
        return message.answers

    answers = asyncio.run(run())
    assert len(answers) == 1
    assert "Анализ все еще выполняется" in answers[0]


def test_slow_alert_is_silent_when_cancelled():
    async def run() -> list[str]:
        message = FakeMessage()
        task = asyncio.create_task(send_slow_alert_if_needed(message.answer, delay_seconds=1))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return message.answers

    answers = asyncio.run(run())
    assert answers == []
