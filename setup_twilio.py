"""
PhoneBuddy — Twilio Setup Script
Automatically configures your Twilio phone number webhook to point at this server.

Usage:
  py setup_twilio.py

Requires: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, PUBLIC_URL in .env
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

def setup():
    try:
        from twilio.rest import Client
    except ImportError:
        print("ERROR: Run 'pip install twilio' first.")
        sys.exit(1)

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
    phone_number = os.environ.get("TWILIO_PHONE_NUMBER")
    public_url  = os.environ.get("PUBLIC_URL", "").rstrip("/")

    if not all([account_sid, auth_token, phone_number, public_url]):
        print("ERROR: Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, and PUBLIC_URL in .env")
        sys.exit(1)

    client = Client(account_sid, auth_token)

    # Find the phone number resource
    numbers = client.incoming_phone_numbers.list(phone_number=phone_number)
    if not numbers:
        print(f"ERROR: Phone number {phone_number} not found in your Twilio account.")
        sys.exit(1)

    number = numbers[0]
    webhook_url = f"{public_url}/call/inbound"

    # Update the webhook
    number.update(
        voice_url=webhook_url,
        voice_method="POST",
    )

    print(f"✓ Twilio webhook configured:")
    print(f"  Number:  {phone_number}")
    print(f"  Webhook: {webhook_url}")
    print(f"")
    print(f"  Test by calling {phone_number} — you should hear silence, then be prompted.")
    print(f"  Watch the dashboard at http://localhost:8000/dashboard")


if __name__ == "__main__":
    setup()
