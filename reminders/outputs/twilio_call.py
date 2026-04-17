import logging
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, MY_PHONE_NUMBER

log = logging.getLogger(__name__)


def call_with_reminder(message: str):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, MY_PHONE_NUMBER]):
        log.warning("Twilio not configured, skipping call")
        return

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    response = VoiceResponse()
    response.say(f"Hey Raghav, quick reminder: {message}", voice="Polly.Matthew", language="en-US")
    response.pause(length=1)
    response.say(f"Again: {message}", voice="Polly.Matthew", language="en-US")
    response.pause(length=2)

    try:
        call = client.calls.create(
            twiml=str(response),
            to=MY_PHONE_NUMBER,
            from_=TWILIO_FROM_NUMBER,
        )
        log.info(f"Twilio call initiated: {call.sid}")
    except Exception as e:
        log.error(f"Twilio call failed: {e}")
