"""Regression: a gzipped upstream response must not raise DecodingError."""
import gzip, json
import httpx, pytest

from app.security.safe_http import SafeHttpClient


@pytest.mark.asyncio
async def test_gzipped_upstream_response_decodes():
    payload = {"results": [{"k_number": "K251002"}], "meta": {"n": 1}}
    packed = gzip.compress(json.dumps(payload).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=packed,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/json",
                "Content-Length": str(len(packed)),
            },
        )

    transport = httpx.MockTransport(handler)
    client = SafeHttpClient(client=httpx.AsyncClient(transport=transport))
    try:
        resp = await client._send(
            "GET",
            "https://api.fda.gov/device/510k.json",
            params=None, data=None, headers=None,
            ceiling=8 * 1024 * 1024,
        )
        # Before the fix this raised httpx.DecodingError.
        assert resp.json() == payload
        assert "content-encoding" not in {k.lower() for k in resp.headers}
    finally:
        await client._client.aclose()
