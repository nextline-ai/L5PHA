import httpx
import pytest

from veyquant.local_config import config_presence, inspect_telegram, read_local_config


def test_configuration_presence_does_not_show_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text('TOSS_CLIENT_SECRET="private-${TOKEN}-marker"\nTELEGRAM_BOT_TOKEN=123:secret\n')
    values = read_local_config(str(path))
    assert values["TOSS_CLIENT_SECRET"] == "private-${TOKEN}-marker"
    result = config_presence(values)
    assert result["configuration"]["TOSS_CLIENT_SECRET"] is True
    assert "private-" not in str(result) and "123:secret" not in str(result)


async def test_getme_returns_only_verified_public_identity():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": 123,
                    "is_bot": True,
                    "username": "veyquant_bot",
                    "first_name": "Veyquant",
                    "unexpected_private_field": "must-not-return",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await inspect_telegram("123:secret-marker", "veyquant_bot", client=client)
    assert result["status"] == "passed" and result["bot_id"] == 123
    assert result["mini_app_verified"] is False
    assert "secret-marker" not in str(result) and "must-not-return" not in str(result)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"description": "token secret-marker invalid"}),
        httpx.Response(
            200,
            json={"ok": True, "result": {"id": 123, "is_bot": True, "username": "someone_else"}},
        ),
        httpx.Response(
            200,
            json={"ok": True, "result": {"id": True, "is_bot": True, "username": "veyquant_bot"}},
        ),
        httpx.Response(200, json=[]),
        httpx.Response(200, content="not-json"),
    ],
)
async def test_telegram_failures_are_sanitized(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await inspect_telegram("123:secret-marker", "veyquant_bot", client=client)
    assert result["status"] == "blocked"
    assert "secret-marker" not in str(result)


async def test_missing_token_never_makes_request():
    assert (await inspect_telegram("", "veyquant_bot"))["reason"] == "bot_token_missing_or_invalid"
