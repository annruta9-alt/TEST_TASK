"""Status codes of a company (companies.status). Defined once, used everywhere."""
from enum import IntEnum


class Status(IntEnum):
    OK = 0                       # Impressum parsed AND address complete
    NO_IMPRESSUM_LINK_FOUND = 1  # home page has no Impressum/Imprint link
    PARSING_FAILED = 2           # Impressum parsed but no fields detected (empty JSON)
    ADDRESS_INCOMPLETE = 3       # fields parsed but postcode/city/street/house_no not all filled
    ERROR = 4                    # transport failure: unreachable, HTTP 4xx/5xx, DeepSeek API error
    NO_WEBSITE = 5               # web column is empty


# NULL in the database = "pending" (imported, not processed yet)
PENDING_NAME = "PENDING"


def status_name(value):
    """Readable name for a status value coming from the database (None = pending)."""
    if value is None:
        return PENDING_NAME
    try:
        return Status(value).name
    except ValueError:
        return f"UNKNOWN({value})"
