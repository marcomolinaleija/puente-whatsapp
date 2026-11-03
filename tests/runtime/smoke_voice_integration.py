"""Import the rebuilt gopy binding without opening a real account or socket."""
import os

from slidge_whatsapp.generated import whatsapp
from slidge_whatsapp.voice import Tokens, secure_url

assert os.environ.get("CAN_VOICE_ENABLE") == "0"
assert secure_url("wss://voice.example.invalid/voice/v1")
assert not Tokens().consume("missing")
gateway = whatsapp.NewGateway()
assert gateway is not None
print("PASS rebuilt v35 native binding + voice import; no account or network")
