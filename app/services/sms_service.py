import os
from typing import Optional
from dotenv import load_dotenv

# v4-style import
from vonage import Vonage, Auth
from vonage_sms import SmsMessage

load_dotenv()

class SMService:
    def __init__(self):
        self.client = None
        api_key = os.getenv('VONAGE_API_KEY')
        api_secret = os.getenv('VONAGE_API_SECRET')

        if api_key and api_secret:
            auth = Auth(api_key=api_key, api_secret=api_secret)
            self.client = Vonage(auth=auth)

    def send(self, to_phone: str, message: str, from_num: str = 'SafeLine') -> Optional[str]:
        test_phone = os.getenv('TEST_PHONE', '')
        if not to_phone or len(to_phone) < 10:
            to_phone = test_phone

        if not self.client:
            return "logged"

        try:
            sms_msg = SmsMessage(to=to_phone, from_=from_num, text=message)
            response = self.client.sms.send(sms_msg)

            # response is a pydantic model in v4 — convert to dict
            data = response.model_dump()
            # older-style message id access:
            msg_id = None
            messages = data.get("messages") or []
            if messages and isinstance(messages, list):
                msg_id = messages[0].get("message-id") or messages[0].get("messageId")

            return msg_id
        except Exception:
            return None