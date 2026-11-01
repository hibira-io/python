import base64
import hashlib
import hmac
import json

import pytest

import hibira
from hibira import _signing

KEY = "278d425bdf160c739803"
SECRET = "7ad3773142a6692b25b8"


def test_sign_query_matches_pusher_reference():
    body = b'{"name":"foo","channels":["project-3"],"data":"{\\"some\\":\\"data\\"}"}'
    q = _signing.sign_query(KEY, SECRET, "POST", "/apps/3/events", body=body, now=1353088179)
    assert q["body_md5"] == "ec365a775a4cd0599faeb73354201b6f"
    assert q["auth_signature"] == "da454824c97ba181a32ccc17a72625ba02771f50b50e1e7430e47a1f3f457e6c"


def test_private_channel_auth_matches_pusher_reference():
    c = hibira.Client("3", KEY, SECRET)
    assert c.authenticate_channel("private-foobar", "1234.1234") == {
        "auth": "278d425bdf160c739803:58df8b0c36d6982b82c3ecf6b4662e34fe8c25bba48f5369f135bf843651c3a4"
    }


def test_presence_channel_auth():
    c = hibira.Client("3", KEY, SECRET)
    out = c.authenticate_channel("presence-foobar", "1234.1234", {"user_id": 10, "user_info": {"name": "Mr. Channels"}})
    assert out["channel_data"] == '{"user_id":10,"user_info":{"name":"Mr. Channels"}}'
    expected = hmac.new(
        SECRET.encode(), f"1234.1234:presence-foobar:{out['channel_data']}".encode(), hashlib.sha256
    ).hexdigest()
    assert out["auth"] == f"{KEY}:{expected}"


@pytest.mark.parametrize(
    ("channel", "socket_id", "user_data"),
    [
        ("public", "1.1", None),
        ("private-a", "nope", None),
        ("private-encrypted-a", "1.1", None),
        ("presence-a", "1.1", None),
        ("presence-a", "1.1", {"user_info": {}}),
        ("private-a b", "1.1", None),
    ],
)
def test_channel_auth_rejects(channel, socket_id, user_data):
    with pytest.raises(ValueError):
        hibira.Client("3", KEY, SECRET).authenticate_channel(channel, socket_id, user_data)


def test_user_auth():
    out = hibira.Client("3", KEY, SECRET).authenticate_user("1.2", {"id": "u1", "name": "Ann"})
    assert out["user_data"] == '{"id":"u1","name":"Ann"}'
    sig = hmac.new(SECRET.encode(), b'1.2::user::{"id":"u1","name":"Ann"}', hashlib.sha256).hexdigest()
    assert out["auth"] == f"{KEY}:{sig}"
    with pytest.raises(ValueError):
        hibira.Client("3", KEY, SECRET).authenticate_user("1.2", {"name": "x"})


def test_verify_webhook():
    c = hibira.Client("3", KEY, SECRET)
    body = b'{"time_ms":1,"events":[{"name":"channel_occupied","channel":"a"}]}'
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert c.verify_webhook(KEY, sig, body)["events"][0]["name"] == "channel_occupied"
    assert c.verify_webhook(KEY, sig.upper(), body.decode())["time_ms"] == 1
    for key, s, b in [("other", sig, body), (KEY, "0" * 64, body), (KEY, None, body), (KEY, sig, body + b" ")]:
        with pytest.raises(hibira.InvalidWebhook):
            c.verify_webhook(key, s, b)


def test_push_token():
    c = hibira.Client("3", KEY, SECRET, push_instance_id="inst-1", push_secret_key="push-secret")
    token = c.push_token("alice", ttl=60)["token"]
    header, payload, sig = token.split(".")

    def dec(s):
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    assert json.loads(dec(header)) == {"alg": "HS256", "typ": "JWT"}
    claims = json.loads(dec(payload))
    assert claims["sub"] == "alice"
    assert claims["iss"] == "https://inst-1.pushnotifications.pusher.com"
    assert claims["exp"] - claims["iat"] == 60
    assert dec(sig) == hmac.new(b"push-secret", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    with pytest.raises(hibira.ConfigError):
        hibira.Client("3", KEY, SECRET).push_token("alice")
