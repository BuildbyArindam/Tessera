"""Parse the timestamp formats Airwallex documents (ISO 8601, e.g. 2021-06-18T16:30:00+0000)."""

import re
from datetime import datetime

_COMPACT_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_timestamp(value: object) -> datetime:
    """Timezone-aware datetime from an ISO 8601 string; ValueError for anything else.

    A timestamp without an offset is refused rather than assumed to be UTC: a wrong guess here
    would make an expired quote look valid.
    """
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value.strip()
    if "T" in text:
        text = _COMPACT_OFFSET.sub(r"\1:\2", text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return parsed
