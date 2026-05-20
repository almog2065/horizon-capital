import urllib.request

from app.workers.metrics_server import start_metrics_server


def test_worker_metrics_endpoint():
    start_metrics_server(host="127.0.0.1", port=19091)
    with urllib.request.urlopen("http://127.0.0.1:19091/healthz", timeout=2) as r:
        assert r.status == 200
    with urllib.request.urlopen("http://127.0.0.1:19091/metrics", timeout=2) as r:
        body = r.read().decode()
    assert "horizon_alerts_open" in body or "horizon_" in body
