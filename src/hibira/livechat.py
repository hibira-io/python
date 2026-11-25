"""Hibira Live Chat helpers for your server.

Verified visitors: sign the signed-in user's ID with the workspace's
identity secret (Live Chat › Settings › Widget › Verified visitors) and put
it on the widget's script tag, so the chat shows who they are and nobody can
pretend to be them::

    from hibira.livechat import user_hash

    <script src="https://chat.hibira.io/w/v1.js" data-workspace="..."
            data-user-id="{{ user.id }}" data-user-hash="{{ user_hash(SECRET, user.id) }}"
            data-user-name="{{ user.name }}" data-user-email="{{ user.email }}" async></script>

Or after load: ``HibiraLiveChat.identify({id, hash, name, email})``.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["user_hash"]


def user_hash(identity_secret: str, user_id: object) -> str:
    """hex(HMAC-SHA256(identity secret, user_id)), the widget's data-user-hash."""
    if not identity_secret:
        raise ValueError("user_hash: identity_secret is required")
    uid = str(user_id)
    if not uid:
        raise ValueError("user_hash: user_id is required")
    return hmac.new(identity_secret.encode(), uid.encode(), hashlib.sha256).hexdigest()
