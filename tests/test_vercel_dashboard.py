import unittest
from unittest.mock import patch

from api.index import app


class VercelDashboardTests(unittest.TestCase):
    def _invoke(self, path: str):
        captured: dict[str, object] = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(
            app(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": path,
                },
                start_response,
            )
        ).decode("utf-8")
        return captured["status"], dict(captured["headers"]), body

    def test_root_serves_dashboard_html(self):
        status, headers, body = self._invoke("/")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("TradingBot Monitor", body)

    @patch("api.index.fetch_remote_snapshot")
    @patch.dict("os.environ", {"TRADINGAGENTS_DASHBOARD_PROXY_URL": "https://example.com"}, clear=False)
    def test_api_overview_uses_proxy_when_configured(self, fetch_remote_snapshot):
        fetch_remote_snapshot.return_value = {
            "generated_at": "2026-04-11T22:00:00Z",
            "refresh_seconds": 15,
            "overview": {"daemon_status": {"running": True}},
        }
        status, headers, body = self._invoke("/api/overview")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn('"running": true', body)

    @patch("api.index._build_local_dashboard_data_service", side_effect=RuntimeError("missing runtime"))
    @patch("api.index._read_proxy_url_file", return_value=None)
    @patch.dict("os.environ", {}, clear=True)
    def test_api_overview_returns_placeholder_when_no_runtime(self, _read_proxy_url_file, _build_service):
        status, _, body = self._invoke("/api/overview")
        self.assertEqual(status, "200 OK")
        self.assertIn("No live dashboard runtime is available inside this Vercel deployment", body)

    @patch("api.index.fetch_remote_snapshot")
    @patch("api.index._read_proxy_url_file", return_value="https://example-file-proxy.com")
    @patch.dict("os.environ", {}, clear=True)
    def test_api_overview_uses_repo_proxy_file_when_present(self, _read_proxy_url_file, fetch_remote_snapshot):
        fetch_remote_snapshot.return_value = {
            "generated_at": "2026-04-11T22:00:00Z",
            "refresh_seconds": 15,
            "overview": {"daemon_status": {"running": True}},
        }
        status, headers, body = self._invoke("/api/overview")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn('"running": true', body)
        fetch_remote_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
