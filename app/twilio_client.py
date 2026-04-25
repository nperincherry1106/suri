import os

from twilio.rest import Client

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
    return _client


def send(body: str):
    _get_client().messages.create(
        body=body,
        from_=os.environ["TWILIO_FROM"],
        to=os.environ["USER_PHONE"],
    )
