class SmartleadError(RuntimeError):
    """A Smartlead call failed or was refused."""


class SendDisabledError(SmartleadError):
    """send_reply was called while SEND_ENABLED is false."""


class ParseError(SmartleadError):
    """A Smartlead payload did not have the expected shape."""
