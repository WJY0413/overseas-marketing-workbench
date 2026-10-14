"""Historical native-reply metadata; the Workbench delivery channel is retired."""
import json

NATIVE_REPLY_MODE = "feishu_native_reply"
NATIVE_REPLY_RETIRED = (
    "Feishu native reply has been retired in this edition. "
    "Handle this conversation in Feishu; this draft will not be sent through SMTP."
)


def native_reply_metadata(template_snapshot: str | None) -> dict:
    try:
        value = json.loads(template_snapshot or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def is_native_reply_draft(template_snapshot: str | None) -> bool:
    return native_reply_metadata(template_snapshot).get("delivery_mode") == NATIVE_REPLY_MODE


def send_queued_native_reply(draft, sender, contact) -> dict:
    # Explicit failure protects callers outside the normal audit/queue path too.
    raise RuntimeError(NATIVE_REPLY_RETIRED)
