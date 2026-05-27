from __future__ import annotations

import asyncio
import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.captcha_solver import (
    CaptchaChallenge,
    CaptchaSolver,
    CaptchaSolvingError,
    CaptchaTimeoutError,
    CaptchaSolution,
    extract_captcha_challenge,
    solve_and_retry_pdf,
    solve_and_retry_pdf_sync,
)


# ---------------------------------------------------------------------------
# extract_captcha_challenge
# ---------------------------------------------------------------------------


class TestExtractCaptchaChallenge:
    def test_detects_recaptcha_v2(self) -> None:
        html = (
            '<html><body>'
            '<div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI">'
            '</div>'
            '<script src="https://www.google.com/recaptcha/api.js"></script>'
            '</body></html>'
        )
        challenge = extract_captcha_challenge(html, "https://example.com/page")
        assert challenge is not None
        assert challenge.captcha_type == "recaptcha_v2"
        assert challenge.sitekey == "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
        assert challenge.page_url == "https://example.com/page"

    def test_detects_hcaptcha(self) -> None:
        html = (
            '<html><body>'
            '<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001">'
            '</div>'
            '</body></html>'
        )
        challenge = extract_captcha_challenge(html, "https://example.com/page")
        assert challenge is not None
        assert challenge.captcha_type == "hcaptcha"
        assert challenge.sitekey == "10000000-ffff-ffff-ffff-000000000001"

    def test_detects_pravocaptcha(self) -> None:
        html = (
            '<html><body>'
            '<script>pravocaptcha.execute(onSubmit);</script>'
            '<form id="tokenFrom"><input name="RecaptchaToken" /></form>'
            '</body></html>'
        )
        challenge = extract_captcha_challenge(html, "https://example.com/doc.pdf")
        assert challenge is not None
        assert challenge.captcha_type == "pravocaptcha"
        assert challenge.page_url == "https://example.com/doc.pdf"

    def test_detects_pravocaptcha_with_sitekey(self) -> None:
        html = (
            '<html><body>'
            '<script src="/captcha/pravocaptcha.js?k=AbCdEfGhIjKlMnOp"></script>'
            '</body></html>'
        )
        challenge = extract_captcha_challenge(html, "https://example.com/doc.pdf")
        assert challenge is not None
        assert challenge.captcha_type == "pravocaptcha"

    def test_no_captcha_returns_none(self) -> None:
        html = "<html><body><p>No captcha here</p></body></html>"
        challenge = extract_captcha_challenge(html, "https://example.com/page")
        assert challenge is None

    def test_pdf_content_returns_none(self) -> None:
        html = "%PDF-1.4 some binary content"
        challenge = extract_captcha_challenge(html, "https://example.com/doc.pdf")
        assert challenge is None

    def test_empty_html_returns_none(self) -> None:
        challenge = extract_captcha_challenge("", "https://example.com/page")
        assert challenge is None

    def test_recaptcha_in_iframe_src(self) -> None:
        html = (
            '<html><body>'
            '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI">'
            '</iframe>'
            '</body></html>'
        )
        challenge = extract_captcha_challenge(html, "https://example.com/page")
        assert challenge is not None
        assert challenge.captcha_type == "recaptcha_v2"
        assert challenge.sitekey == "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"


# ---------------------------------------------------------------------------
# CaptchaSolver.solve (async)
# ---------------------------------------------------------------------------


class TestCaptchaSolverAsync:
    @pytest.fixture
    def solver(self) -> CaptchaSolver:
        return CaptchaSolver(
            api_key="test_api_key",
            timeout_seconds=30,
            poll_interval_seconds=0.01,
        )

    @pytest.mark.asyncio
    async def test_solve_success(self, solver: CaptchaSolver) -> None:
        challenge = CaptchaChallenge(
            captcha_type="recaptcha_v2",
            sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",
            page_url="https://example.com/page",
        )

        mock_client = AsyncMock()

        submit_response = MagicMock()
        submit_response.json.return_value = {"status": 1, "request": "12345"}
        submit_response.raise_for_status = MagicMock()

        poll_response = MagicMock()
        poll_response.json.return_value = {
            "status": 1,
            "request": "03AGdBq24_token_here",
        }
        poll_response.raise_for_status = MagicMock()

        mock_client.post.return_value = submit_response
        mock_client.get.return_value = poll_response

        solver._http_client = mock_client
        solution = await solver.solve(challenge)

        assert isinstance(solution, CaptchaSolution)
        assert solution.token == "03AGdBq24_token_here"
        assert solution.captcha_type == "recaptcha_v2"
        mock_client.post.assert_called_once()
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_solve_polls_multiple_times(self, solver: CaptchaSolver) -> None:
        challenge = CaptchaChallenge(
            captcha_type="recaptcha_v2",
            sitekey="test_key",
            page_url="https://example.com/page",
        )

        mock_client = AsyncMock()

        submit_response = MagicMock()
        submit_response.json.return_value = {"status": 1, "request": "12345"}
        submit_response.raise_for_status = MagicMock()

        not_ready = MagicMock()
        not_ready.json.return_value = {"status": 0, "request": "CAPCHA_NOT_READY"}
        not_ready.raise_for_status = MagicMock()

        ready = MagicMock()
        ready.json.return_value = {"status": 1, "request": "solved_token"}
        ready.raise_for_status = MagicMock()

        mock_client.post.return_value = submit_response
        mock_client.get.side_effect = [not_ready, not_ready, ready]

        solver._http_client = mock_client
        solution = await solver.solve(challenge)

        assert solution.token == "solved_token"
        assert mock_client.get.call_count == 3

    @pytest.mark.asyncio
    async def test_solve_unsolvable_raises(self, solver: CaptchaSolver) -> None:
        challenge = CaptchaChallenge(
            captcha_type="recaptcha_v2",
            sitekey="test_key",
            page_url="https://example.com/page",
        )

        mock_client = AsyncMock()

        submit_response = MagicMock()
        submit_response.json.return_value = {"status": 1, "request": "12345"}
        submit_response.raise_for_status = MagicMock()

        poll_response = MagicMock()
        poll_response.json.return_value = {
            "status": 0,
            "request": "ERROR_CAPTCHA_UNSOLVABLE",
        }
        poll_response.raise_for_status = MagicMock()

        mock_client.post.return_value = submit_response
        mock_client.get.return_value = poll_response

        solver._http_client = mock_client
        with pytest.raises(CaptchaSolvingError, match="unsolvable"):
            await solver.solve(challenge)

    @pytest.mark.asyncio
    async def test_solve_timeout_raises(self) -> None:
        solver = CaptchaSolver(
            api_key="test_api_key",
            timeout_seconds=0,
            poll_interval_seconds=0.01,
        )
        challenge = CaptchaChallenge(
            captcha_type="recaptcha_v2",
            sitekey="test_key",
            page_url="https://example.com/page",
        )

        mock_client = AsyncMock()

        submit_response = MagicMock()
        submit_response.json.return_value = {"status": 1, "request": "12345"}
        submit_response.raise_for_status = MagicMock()

        not_ready = MagicMock()
        not_ready.json.return_value = {"status": 0, "request": "CAPCHA_NOT_READY"}
        not_ready.raise_for_status = MagicMock()

        mock_client.post.return_value = submit_response
        mock_client.get.return_value = not_ready

        solver._http_client = mock_client
        with pytest.raises(CaptchaTimeoutError):
            await solver.solve(challenge)

    @pytest.mark.asyncio
    async def test_submit_failure_raises(self, solver: CaptchaSolver) -> None:
        challenge = CaptchaChallenge(
            captcha_type="recaptcha_v2",
            sitekey="test_key",
            page_url="https://example.com/page",
        )

        mock_client = AsyncMock()
        submit_response = MagicMock()
        submit_response.json.return_value = {
            "status": 0,
            "request": "ERROR_WRONG_USER_KEY",
        }
        submit_response.raise_for_status = MagicMock()
        mock_client.post.return_value = submit_response

        solver._http_client = mock_client
        with pytest.raises(CaptchaSolvingError, match="submission failed"):
            await solver.solve(challenge)


# ---------------------------------------------------------------------------
# solve_and_retry_pdf (async)
# ---------------------------------------------------------------------------


class TestSolveAndRetryPdf:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_captcha(self) -> None:
        solver = CaptchaSolver(api_key="key")
        mock_client = AsyncMock()
        result = await solve_and_retry_pdf(
            solver,
            mock_client,
            "https://example.com/doc.pdf",
            "<html><p>No captcha</p></html>",
            30.0,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_pdf_after_solving(self) -> None:
        solver = CaptchaSolver(
            api_key="key",
            timeout_seconds=10,
            poll_interval_seconds=0.01,
        )

        captcha_html = (
            '<html><body>'
            '<div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI">'
            '</div>'
            '</body></html>'
        )

        mock_client = AsyncMock()

        submit_resp = MagicMock()
        submit_resp.json.return_value = {"status": 1, "request": "999"}
        submit_resp.raise_for_status = MagicMock()

        poll_resp = MagicMock()
        poll_resp.json.return_value = {"status": 1, "request": "solved_token"}
        poll_resp.raise_for_status = MagicMock()

        pdf_resp = MagicMock()
        pdf_resp.status_code = 200
        pdf_resp.headers = {"content-type": "application/pdf"}
        pdf_resp.content = b"%PDF-1.4 fake pdf content"

        mock_client.post.return_value = submit_resp
        mock_client.get.return_value = poll_resp
        mock_client.get.return_value = poll_resp

        # The retry GET call needs to return PDF
        async def mock_get(*args, **kwargs):
            cookies = kwargs.get("cookies", {})
            if cookies:
                return pdf_resp
            return poll_resp

        mock_client.get.side_effect = None
        mock_client.get.return_value = poll_resp

        # We need to properly mock the flow:
        # 1. submit -> 2. poll -> 3. retry GET
        call_count = [0]
        original_get = mock_client.get

        async def tracked_get(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return poll_resp
            return pdf_resp

        mock_client.get = tracked_get
        mock_client.post.return_value = submit_resp

        solver._http_client = mock_client
        result = await solve_and_retry_pdf(
            solver,
            mock_client,
            "https://example.com/doc.pdf",
            captcha_html,
            30.0,
        )

        assert result == b"%PDF-1.4 fake pdf content"

    @pytest.mark.asyncio
    async def test_returns_none_on_solve_failure(self) -> None:
        solver = CaptchaSolver(
            api_key="key",
            timeout_seconds=10,
            poll_interval_seconds=0.01,
        )

        captcha_html = (
            '<html><body>'
            '<div class="g-recaptcha" data-sitekey="testkey123456789012345">'
            '</div>'
            '</body></html>'
        )

        mock_client = AsyncMock()
        submit_resp = MagicMock()
        submit_resp.json.return_value = {"status": 0, "request": "ERROR"}
        submit_resp.raise_for_status = MagicMock()
        mock_client.post.return_value = submit_resp

        solver._http_client = mock_client
        result = await solve_and_retry_pdf(
            solver,
            mock_client,
            "https://example.com/doc.pdf",
            captcha_html,
            30.0,
        )
        assert result is None


# ---------------------------------------------------------------------------
# solve_and_retry_pdf_sync
# ---------------------------------------------------------------------------


class TestSolveAndRetryPdfSync:
    def test_returns_none_when_no_captcha(self) -> None:
        mock_client = MagicMock()
        result = solve_and_retry_pdf_sync(
            "key", None, mock_client, "https://example.com/doc.pdf",
            "<html><p>No captcha</p></html>", 30.0,
        )
        assert result is None

    def test_returns_pdf_after_solving(self) -> None:
        captcha_html = (
            '<html><body>'
            '<div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI">'
            '</div>'
            '</body></html>'
        )

        mock_client = MagicMock()

        submit_resp = MagicMock()
        submit_resp.json.return_value = {"status": 1, "request": "777"}
        submit_resp.raise_for_status = MagicMock()

        poll_resp = MagicMock()
        poll_resp.json.return_value = {"status": 1, "request": "sync_solved_token"}
        poll_resp.raise_for_status = MagicMock()

        pdf_resp = MagicMock()
        pdf_resp.status_code = 200
        pdf_resp.headers = {"content-type": "application/pdf"}
        pdf_resp.content = b"%PDF-1.4 synced pdf"

        mock_client.post.return_value = submit_resp

        get_calls = [0]

        def mock_get(*args, **kwargs):
            get_calls[0] += 1
            if kwargs.get("cookies"):
                return pdf_resp
            return poll_resp

        mock_client.get = mock_get

        with patch("src.services.captcha_solver.time.sleep"):
            result = solve_and_retry_pdf_sync(
                "test_key", None, mock_client,
                "https://example.com/doc.pdf", captcha_html, 30.0,
            )

        assert result == b"%PDF-1.4 synced pdf"

    def test_returns_none_on_submit_failure(self) -> None:
        captcha_html = (
            '<html><body>'
            '<div class="g-recaptcha" data-sitekey="testkey123456789012345">'
            '</div>'
            '</body></html>'
        )

        mock_client = MagicMock()
        submit_resp = MagicMock()
        submit_resp.json.return_value = {"status": 0, "request": "ERROR"}
        submit_resp.raise_for_status = MagicMock()
        mock_client.post.return_value = submit_resp

        result = solve_and_retry_pdf_sync(
            "key", None, mock_client,
            "https://example.com/doc.pdf", captcha_html, 30.0,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Integration with kad_client
# ---------------------------------------------------------------------------


class TestKadClientCaptchaIntegration:
    def test_captcha_solver_stored_in_constructor(self) -> None:
        from src.services.kad_client import ParserApiKadClient

        solver = CaptchaSolver(api_key="test_key")
        client = ParserApiKadClient(
            base_url="https://kad.arbitr.ru",
            api_key="kad_key",
            captcha_solver=solver,
        )
        assert client._captcha_solver is solver

    def test_captcha_solver_none_by_default(self) -> None:
        from src.services.kad_client import ParserApiKadClient

        client = ParserApiKadClient(
            base_url="https://kad.arbitr.ru",
            api_key="kad_key",
        )
        assert client._captcha_solver is None

    def test_sync_extraction_with_solver_skips_captcha(self) -> None:
        from src.domain.entities import CaseDecision, CaseOutcome
        from src.domain.settings import default_settings
        from src.services.kad_client import ParserApiKadClient

        solver = CaptchaSolver(api_key="test_key")
        client = ParserApiKadClient.__new__(ParserApiKadClient)
        client._logger = logging.getLogger("test.kad_client")
        client._base_url = "https://kad.arbitr.ru"
        client._captcha_solver = solver
        client._current_article = "61.2"

        captcha_html = (
            b"<!DOCTYPE html><html><body>"
            b"<form id='tokenFrom'><input name='RecaptchaToken' /></form>"
            b"<script>pravocaptcha.execute(onSubmit);</script>"
            b"</body></html>"
        )

        mock_sync_client = MagicMock()
        captcha_resp = SimpleNamespace(
            status_code=200,
            headers={"content-type": "text/html"},
            content=captcha_html,
            text=captcha_html.decode(),
        )
        mock_sync_client.get = MagicMock(return_value=captcha_resp)
        client._sync_http_client = mock_sync_client

        decision = CaseDecision(
            case_number="A40-1/2024",
            decision_date=date(2024, 1, 1),
            outcome=CaseOutcome.UNKNOWN,
            reasons=(),
            document_links=(
                {
                    "name": "Определение",
                    "url": "https://kad.arbitr.ru/Kad/PdfDocument/test.pdf",
                },
            ),
        )
        settings = default_settings(date(2026, 3, 28))

        with patch("src.services.captcha_solver.time.sleep"):
            text = client._extract_pdf_text_sync(decision, settings)

        assert text is None
