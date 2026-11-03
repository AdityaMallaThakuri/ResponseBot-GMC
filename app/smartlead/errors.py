class SmartleadError(RuntimeError):
    """A Smartlead call failed or was refused."""


class SendDisabledError(SmartleadError):
    """send_reply was called while SEND_ENABLED is false."""


class RecipientNotAllowed(SmartleadError):
    """send_reply was called for an address not in SEND_ALLOWLIST while LIVE_SEND_ALL is off."""


class CategoryWriteRefused(SmartleadError):
    """update_lead_category was refused before any HTTP call."""


class ParseError(SmartleadError):
    """A Smartlead payload did not have the expected shape."""
