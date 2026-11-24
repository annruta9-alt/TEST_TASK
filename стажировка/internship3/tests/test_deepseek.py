"""DeepSeek client: request format, JSON mode, retries, error handling (no network)."""
import json

import pytest
import requests

import impressum as imp

KEY = "sk-test-key-123"
GOOD = {"company_name": "Muster GmbH", "postal_address": {"street": "Musterstraße", "house_no": "12",
        "postcode": "10115", "city": "Berlin", "country": "Deutschland"}, "handelsregister": "HRB 1",
        "vat_number": "DE123456789", "phone": "+49 30 1", "email": "a@b.de", "responsible_person": "Max"}


class Resp:
    def __init__(self, status=200, content=None, text=None, usage=None):
        self.status_code = status
        self._body = {"choices": [{"message": {"content": content}}], "usage": usage or {}}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        return json.loads(self.text) if self.text.startswith("{") else self._body


class Session:
    """Programmable fake of requests: each item is a Resp or an exception to raise."""

    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(data=GOOD, usage=None):
    return Resp(200, json.dumps(data), usage=usage)


def call(session, **kw):
    sleeps = []
    result = imp.call_deepseek("Impressum\nMuster GmbH", api_key=KEY, session=session,
                               sleep=sleeps.append, **kw)
    return result, sleeps


# ----------------------------------------------------------------- request format
def test_request_format_follows_the_task(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    s = Session(ok(usage={"prompt_tokens": 120, "completion_tokens": 40}))
    (data, usage), _ = call(s)
    req = s.calls[0]
    assert req["url"] == "https://api.deepseek.com/chat/completions"
    assert req["headers"] == {"Authorization": f"Bearer {KEY}"}
    body = req["json"]
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 1024 and body["temperature"] == 0
    assert req["timeout"] == 60
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    system, user = (m["content"] for m in body["messages"])
    assert "json" in system.lower() and "json" in user.lower()        # JSON mode needs the word
    for key in ("company_name", "postal_address", "street", "house_no", "postcode", "city",
                "country", "handelsregister", "vat_number", "phone", "email", "responsible_person"):
        assert f'"{key}"' in system                                    # the schema example
    assert user.startswith("Extract the Impressum data from the text below and return JSON:")
    assert user.endswith("Impressum\nMuster GmbH")
    assert data == GOOD and usage == {"prompt_tokens": 120, "completion_tokens": 40}


def test_model_and_base_url_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://localhost:9999/")
    s = Session(ok())
    call(s)
    assert s.calls[0]["json"]["model"] == "deepseek-flash"
    assert s.calls[0]["url"] == "http://localhost:9999/chat/completions"


def test_explicit_arguments_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "from-env")
    s = Session(ok())
    imp.call_deepseek("x", api_key=KEY, model="explicit", session=s, sleep=lambda _: None)
    assert s.calls[0]["json"]["model"] == "explicit"


def test_api_key_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    s = Session(ok())
    imp.call_deepseek("x", session=s, sleep=lambda _: None)
    assert s.calls[0]["headers"]["Authorization"] == "Bearer sk-from-env"


def test_missing_key_is_an_error(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(imp.DeepSeekError, match="DEEPSEEK_API_KEY"):
        imp.call_deepseek("x", session=Session(), sleep=lambda _: None)


def test_prompt_matches_the_task_text():
    p = imp.SYSTEM_PROMPT
    assert p.startswith('You extract legally required company information ("Impressum" / "Imprint") from')
    assert "Do not invent or guess values." in p and "email: lowercase." in p
    assert "German VAT ids look like DE followed by 9 digits." in p


# ----------------------------------------------------------------- blank / bad JSON
def test_blank_output_is_retried_with_backoff():
    s = Session(Resp(200, "   "), ok())
    (data, _), sleeps = call(s, backoff=1.0)
    assert data == GOOD and len(s.calls) == 2 and sleeps == [1.0]


def test_blank_three_times_gives_an_empty_object_not_an_exception():
    s = Session(Resp(200, ""), Resp(200, " "), Resp(200, None))
    (data, _), sleeps = call(s, backoff=1.0)
    assert data == {} and len(s.calls) == 3 and sleeps == [1.0, 2.0]   # exponential backoff


def test_non_json_text_is_retried():
    s = Session(Resp(200, "Sorry, I cannot do that"), ok())
    (data, _), _ = call(s)
    assert data == GOOD and len(s.calls) == 2


def test_a_json_array_is_not_an_object():
    s = Session(Resp(200, "[1, 2]"), Resp(200, '"text"'), Resp(200, "[]"))
    assert call(s)[0][0] == {}


def test_markdown_fences_and_surrounding_text_are_tolerated():
    s = Session(Resp(200, "```json\n" + json.dumps(GOOD) + "\n```"))
    assert call(s)[0][0] == GOOD and len(s.calls) == 1
    s = Session(Resp(200, "Here you go: " + json.dumps(GOOD) + " Hope it helps"))
    assert call(s)[0][0] == GOOD


def test_missing_choices_is_treated_as_unparseable():
    bad = Resp(200, None, text=json.dumps({"unexpected": True}))
    assert call(Session(bad, bad, bad))[0][0] == {}


@pytest.mark.parametrize("content,expected", [
    ('{"a": 1}', {"a": 1}), ("  {} ", {}), ("", None), (None, None), ("not json", None),
    ("[1]", None), ("```\n{\"a\": 2}\n```", {"a": 2}), ('xx {"a": 3} yy', {"a": 3}),
])
def test_parse_json_content(content, expected):
    assert imp.parse_json_content(content) == expected


# ----------------------------------------------------------------- HTTP / network errors
def test_http_401_fails_at_once_without_retry():
    s = Session(Resp(401, text='{"error": "Authentication Fails"}'))
    with pytest.raises(imp.DeepSeekError, match="HTTP 401"):
        call(s)
    assert len(s.calls) == 1


def test_http_400_fails_at_once():
    with pytest.raises(imp.DeepSeekError, match="HTTP 400"):
        call(Session(Resp(400, text="bad request")))


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_errors_are_retried(status):
    s = Session(Resp(status, text="busy"), ok())
    (data, _), sleeps = call(s, backoff=0.5)
    assert data == GOOD and len(s.calls) == 2 and sleeps == [0.5]


def test_http_500_three_times_is_an_error():
    s = Session(Resp(500, text="x"), Resp(500, text="x"), Resp(500, text="boom"))
    with pytest.raises(imp.DeepSeekError, match="HTTP 500"):
        call(s)
    assert len(s.calls) == 3


def test_network_error_is_retried_then_reported():
    s = Session(requests.ConnectionError("down"), ok())
    assert call(s)[0][0] == GOOD
    s = Session(*[requests.Timeout("slow")] * 3)
    with pytest.raises(imp.DeepSeekError, match="network error"):
        call(s)
    assert len(s.calls) == 3


def test_error_messages_do_not_contain_the_key():
    with pytest.raises(imp.DeepSeekError) as exc:
        call(Session(Resp(401, text='{"error": "bad key"}')))
    assert KEY not in str(exc.value)


def test_max_attempts_is_respected():
    s = Session(Resp(200, ""))
    assert call(s, max_attempts=1)[0][0] == {} and len(s.calls) == 1


def test_backoff_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_RETRY_BACKOFF", "0.25")
    s = Session(Resp(200, ""), ok())
    _, sleeps = call(s)
    assert sleeps == [0.25]
