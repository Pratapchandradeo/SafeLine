import os
import re
import json
import datetime
import asyncio
import sys
from pathlib import Path
from typing import Optional, Set, Dict, Any
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

from load_context import load_context

load_dotenv()

# Environment variable checks
CEREBRAS_KEY = os.getenv("CEREBRAS_API_KEY")
DEEPGRAM_KEY = os.getenv("DEEPGRAM_API_KEY")
CARTESIA_KEY = os.getenv("CARTESIA_API_KEY")
VONAGE_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_SECRET = os.getenv("VONAGE_API_SECRET")

if not DEEPGRAM_KEY:
    print("⚠️ DEEPGRAM_API_KEY not set, using DummySTT")
if not CARTESIA_KEY:
    print("⚠️ CARTESIA_API_KEY not set, using DummyTTS")
if not VONAGE_KEY or not VONAGE_SECRET:
    print("⚠️ VONAGE_API_KEY/SECRET not set, SMS will log to console")

# LiveKit / plugin imports
from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.plugins import deepgram, cartesia, openai

from app.services.db_service import DBService
from app.services.sms_service import SMService
from app.services.form_service import FormService

# -------------------------
# Dummy STT / TTS fallbacks
# -------------------------
class DummySTT:
    async def transcribe_stream(self, *args, **kwargs):
        print("🔍 DummySTT: Returning empty transcription")
        return {"text": ""}
    async def transcribe_file(self, path):
        print(f"🔍 DummySTT: Transcribing file {path} (returning empty)")
        return {"text": ""}

class DummyTTS:
    async def speak(self, text: str):
        print(f"🤖 (DummyTTS): {text}")

# -------------------------
# Case data structure
# -------------------------
@dataclass
class CaseData:
    name: str = ""
    phone: str = ""
    email: str = ""
    crime_type: str = ""
    incident_date: str = ""
    description: str = ""
    amount_lost: Optional[float] = None
    evidence: str = ""
    is_emergency: bool = False
    consent_recorded: bool = False
    transcript: str = ""

# -------------------------
# SafeLine Agent
# -------------------------
class SafeLineAgent(Agent):
    def __init__(self, *args, **kwargs):
        print("🔄 SafeLineAgent.__init__() called")
        
        # Initialize services first
        self.db_service = DBService()
        self.sms_service = SMService()
        self.form_service = FormService()

        # Load context
        self._ctx_obj = self._load_context()
        
        # Initialize STT, TTS, LLM
        stt_client = deepgram.STT(model="nova-2", language="en") if DEEPGRAM_KEY else DummySTT()
        
        # Initialize TTS with Cartesia
        if CARTESIA_KEY:
            try:
                tts_client = cartesia.TTS(api_key=CARTESIA_KEY)
                print("✅ Initialized Cartesia TTS")
            except Exception as e:
                print(f"⚠️ Failed to initialize Cartesia TTS: {e}, falling back to DummyTTS")
                tts_client = DummyTTS()
        else:
            tts_client = DummyTTS()
            print("🔍 No CARTESIA_KEY, using DummyTTS")

        # Initialize LLM with Cerebras
        if CEREBRAS_KEY:
            try:
                llm_client = openai.LLM.with_cerebras(model="llama3.1-8b", api_key=CEREBRAS_KEY)
                print("✅ Initialized Cerebras LLM")
            except Exception as e:
                print(f"⚠️ Failed to initialize Cerebras LLM: {e}")
                llm_client = None
        else:
            llm_client = None
            print("⚠️ No CEREBRAS_KEY, LLM will not be available")

        # Strict instructions
        instructions = """
        You are a Safe Line cybercrime helpline assistant.
        Follow this EXACT process:
        1. Greet and ask if it's an emergency
        2. Ask for consent to record
        3. Collect information ONE FIELD AT A TIME: name, phone, email, incident description
        4. After getting description, classify the crime type using keyword-based logic
        5. Ask for incident date
        6. Confirm all details
        7. Save case and send SMS
        
        Be professional, empathetic, and concise.
        DO NOT generate fake data or scenarios.
        Wait for user input before responding.
        Keep responses under 2 sentences.
        Ask ONE question at a time and wait for response.
        """

        super().__init__(instructions=instructions, stt=stt_client, llm=llm_client, tts=tts_client, *args, **kwargs)

        # Conversation state
        self.case_data = CaseData()
        self.transcript = ""
        self.case_saved = False
        self.current_step = "greeting"
        self.transcript_file = None
        self._room_name = "unknown"
        self._session_ref = None
        
        print("✅ SafeLineAgent initialized successfully")

    def _load_context(self):
        """Load context from JSON file directly"""
        try:
            context_path = Path("context/safe_line_info.json")
            if context_path.exists():
                with open(context_path, 'r') as f:
                    ctx_obj = json.load(f)
                print("✅ Loaded context from safe_line_info.json")
                return ctx_obj
            else:
                print("⚠️ Context file not found, using defaults")
                return None
        except Exception as e:
            print(f"⚠️ Error loading context: {e}")
            return None

    def _log(self, message: str):
        """Use both print and logging to ensure visibility"""
        print(f"🔍 {message}")
        import logging
        logger = logging.getLogger("safeline.agent")
        logger.info(message)

    async def _setup_transcript_recording(self, room_name: str):
        """Setup transcript recording file"""
        try:
            self._room_name = room_name
            transcripts_dir = Path("transcripts")
            transcripts_dir.mkdir(exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.transcript_file = transcripts_dir / f"transcript_{room_name}_{timestamp}.json"
            transcript_data = {
                "session_start": datetime.datetime.now().isoformat(),
                "room_name": room_name,
                "case_data": {},
                "conversation": []
            }
            with open(self.transcript_file, 'w') as f:
                json.dump(transcript_data, f, indent=2)
            print(f"📝 Transcript recording started: {self.transcript_file}")
        except Exception as e:
            print(f"⚠️ Failed to setup transcript recording: {e}")

    async def _add_to_transcript(self, speaker: str, text: str, step: str = None):
        """Add an entry to the transcript"""
        try:
            if self.transcript_file and self.transcript_file.exists():
                with open(self.transcript_file, 'r') as f:
                    transcript_data = json.load(f)
                entry = {
                    "timestamp": datetime.datetime.now().isoformat(),
                    "speaker": speaker,
                    "text": text,
                    "step": step or self.current_step
                }
                transcript_data["conversation"].append(entry)
                transcript_data["case_data"] = asdict(self.case_data)
                with open(self.transcript_file, 'w') as f:
                    json.dump(transcript_data, f, indent=2)
        except Exception as e:
            print(f"⚠️ Failed to add to transcript: {e}")

    async def _finalize_transcript(self):
        """Finalize transcript when conversation ends"""
        try:
            if self.transcript_file and self.transcript_file.exists():
                with open(self.transcript_file, 'r') as f:
                    transcript_data = json.load(f)
                transcript_data["session_end"] = datetime.datetime.now().isoformat()
                transcript_data["case_data"] = asdict(self.case_data)
                transcript_data["case_saved"] = self.case_saved
                transcript_data["final_step"] = self.current_step
                with open(self.transcript_file, 'w') as f:
                    json.dump(transcript_data, f, indent=2)
                print(f"📝 Transcript finalized: {self.transcript_file}")
        except Exception as e:
            print(f"⚠️ Failed to finalize transcript: {e}")

    async def _speak(self, text: str):
        """Speak text to user"""
        try:
            print(f"🗣️ Speaking: {text}")
            await self._add_to_transcript("agent", text, self.current_step)
            if hasattr(self, '_session_ref') and self._session_ref:
                print(f"🔍 Using session.say to speak: {text}")
                await self._session_ref.say(text)
            else:
                print(f"⚠️ No session reference, using direct TTS: {text}")
                await self.tts.speak(text)
        except Exception as e:
            print(f"⚠️ TTS error: {e}")
            await self.tts.speak(text)

    def _log_user_response(self, transcription: str, extracted_field: str = None, field_name: str = None):
        """Log detailed user response information"""
        print("📋 === USER RESPONSE DETAILS ===")
        print(f"📋 Step: {self.current_step}")
        print(f"📋 Transcription: '{transcription}'")
        if field_name and extracted_field:
            print(f"📋 Extracted {field_name}: '{extracted_field}'")
        print(f"📋 Current Case Data: {asdict(self.case_data)}")
        print("📋 ===========================")

    async def on_user_transcription(self, transcription: str):
        """Process user input based on current step"""
        print(f"🎯 on_user_transcription() called with: '{transcription}'")
        print(f"📋 Current step: {self.current_step}")
        
        if not transcription.strip():
            print("⚠️ Empty transcription received, prompting user to repeat")
            await self._speak("I didn't hear you. Could you please repeat?")
            self._log_user_response(transcription)
            return

        self.transcript += f"User: {transcription}\n"
        self.case_data.transcript = self.transcript
        await self._add_to_transcript("user", transcription, self.current_step)

        try:
            # Check for emergency in ANY response
            if self._is_emergency(transcription) and not self.case_data.is_emergency:
                self.case_data.is_emergency = True
                await self._handle_emergency()
                self._log_user_response(transcription, "emergency detected", "Status")
                return

            if self.current_step == "greeting":
                print("➡️ Processing greeting step response")
                self._log_user_response(transcription)
                # User responded to greeting, now ask for consent
                await self._ask_consent()
                        
            elif self.current_step == "consent":
                print("➡️ Processing consent step response")
                text_clean = transcription.lower().strip()
                if any(word in text_clean for word in ['yes', 'yeah', 'sure', 'okay', 'ok', 'yep', 'yes.']):
                    self.case_data.consent_recorded = True
                    print("✅ User consented to recording")
                    await self._speak("Thank you for your consent.")
                    await self._ask_name()
                    self._log_user_response(transcription, "True", "Consent")
                elif any(word in text_clean for word in ['no', 'not', "don't", 'nope']):
                    print("❌ User did not consent")
                    await self._speak("I understand. I'll only collect basic information without recording.")
                    await self._ask_name()
                    self._log_user_response(transcription, "False", "Consent")
                else:
                    print("❓ Unclear consent response")
                    await self._speak("Please say 'yes' if you consent to recording, or 'no' if you don't.")
                    self._log_user_response(transcription)
                
            elif self.current_step == "name":
                print("➡️ Processing name step response")
                name = self._extract_name(transcription)
                if name:
                    self.case_data.name = name
                    print(f"✅ Extracted name: {name}")
                    await self._speak(f"Thank you {name}.")
                    await self._ask_phone()
                    self._log_user_response(transcription, name, "Name")
                else:
                    print("❌ Could not extract name")
                    await self._speak("I didn't catch your name. Could you please tell me your full name?")
                    self._log_user_response(transcription)
                
            elif self.current_step == "phone":
                print("➡️ Processing phone step response")
                phone = self._extract_phone(transcription)
                if phone:
                    self.case_data.phone = phone
                    print(f"✅ Extracted phone: {phone}")
                    await self._speak("Thank you.")
                    await self._ask_email()
                    self._log_user_response(transcription, phone, "Phone")
                else:
                    print("❌ Could not extract phone")
                    await self._speak("I didn't get a valid phone number. Could you please share your 10-digit phone number?")
                    self._log_user_response(transcription)
                
            elif self.current_step == "email":
                print("➡️ Processing email step response")
                email = self._extract_email(transcription)
                if email:
                    self.case_data.email = email
                    print(f"✅ Extracted email: {email}")
                    await self._speak("Thank you.")
                    await self._ask_description()
                    self._log_user_response(transcription, email, "Email")
                else:
                    print("❌ Could not extract email")
                    await self._speak("I didn't catch a valid email address. Could you please share your email?")
                    self._log_user_response(transcription)
                
            elif self.current_step == "description":
                print("➡️ Processing description step response")
                self.case_data.description = transcription
                print(f"✅ Saved description: {transcription[:50]}...")
                crime_type = await self._classify_crime_type(transcription)
                self.case_data.crime_type = crime_type
                print(f"✅ Classified crime type: {crime_type}")
                await self._speak(f"Understood. This sounds like {crime_type}.")
                await self._ask_date()
                self._log_user_response(transcription, crime_type, "Crime Type")
                
            elif self.current_step == "date":
                print("➡️ Processing date step response")
                date = self._extract_date(transcription)
                if date:
                    self.case_data.incident_date = date
                    print(f"✅ Extracted date: {date}")
                    await self._confirm_details()
                    self._log_user_response(transcription, date, "Incident Date")
                else:
                    print("❌ Could not extract date")
                    await self._speak("I didn't catch the date. When did this happen? You can say 'today', 'yesterday', or a specific date.")
                    self._log_user_response(transcription)
                
            elif self.current_step == "confirmation":
                print("➡️ Processing confirmation step response")
                print(f"🔍 User confirmation response: '{transcription}'")
                if any(word in transcription.lower() for word in ['yes', 'correct', 'right', 'yes that\'s correct', 'yeah']):
                    print("✅ User confirmed details - proceeding to save case")
                    await self._save_and_send_form()
                    self._log_user_response(transcription, "Confirmed", "Confirmation")
                elif any(word in transcription.lower() for word in ['no', 'wrong', 'incorrect', 'change']):
                    print("❌ User wants to correct information")
                    await self._speak("Let's correct the information. What's your name?")
                    self.current_step = "name"
                    self._log_user_response(transcription, "Not Confirmed", "Confirmation")
                else:
                    print("❓ Unclear confirmation response")
                    await self._speak("Please say 'yes' if the information is correct, or 'no' to make changes.")
                    self._log_user_response(transcription)
            else:
                print(f"❓ Unknown step: {self.current_step}")
                self._log_user_response(transcription)
        except Exception as e:
            print(f"⚠️ Error in on_user_transcription: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            await self._speak("Something went wrong. Please try again.")

    async def on_enter(self):
        """Start the conversation flow"""
        print("🚀 on_enter() called - Agent started")
        await self._start_conversation()

    def _is_emergency(self, text: str) -> bool:
        keywords = self._ctx_obj.get("urgency_keywords", []) if self._ctx_obj else [
            "bank", "money", "transfer", "ongoing", "threatening", "ransom", 
            "house", "kill", "threat", "danger", "emergency", "help now", "immediate"
        ]
        return any(kw in text.lower() for kw in keywords)

    async def _start_conversation(self):
        print("🎯 _start_conversation() called")
        self.current_step = "greeting"
        greeting = self._ctx_obj.get("message_templates", {}).get(
            "greeting",
            "Hello, this is the Safe Line cybercrime helpline assistant. How can I help you today?"
        )
        await self._speak(greeting)

    async def _handle_emergency(self):
        print("🚨 _handle_emergency() called")
        emergency_msg = self._ctx_obj.get("message_templates", {}).get("triage",
                        "This sounds urgent! Let me get your basic details quickly to connect you with a human operator.")
        await self._speak(emergency_msg)
        self.case_data.is_emergency = True
        await self._ask_name()

    async def _ask_consent(self):
        print("🎯 _ask_consent() called")
        self.current_step = "consent"
        consent_msg = self._ctx_obj.get("message_templates", {}).get(
            "consent",
            "Before we proceed, do you consent to recording this call for your case file? Please say yes or no."
        )
        await self._speak(consent_msg)

    async def _ask_name(self):
        self.current_step = "name"
        await self._speak("Could you please tell me your full name?")

    async def _ask_phone(self):
        print("🎯 _ask_phone() called")
        await self._speak("What is your 10-digit phone number?")
        self.current_step = "phone"

    async def _ask_email(self):
        print("🎯 _ask_email() called")
        await self._speak("What is your email address?")
        self.current_step = "email"

    async def _ask_description(self):
        print("🎯 _ask_description() called")
        await self._speak("Please describe what happened in your own words. Tell me about the incident.")
        self.current_step = "description"

    async def _ask_date(self):
        print("🎯 _ask_date() called")
        await self._speak("When did this incident happen? You can say 'today', 'yesterday', or a specific date.")
        self.current_step = "date"

    async def _classify_crime_type(self, description: str) -> str:
        print("🤖 _classify_crime_type() called")
        crime_keywords = {
            "scam": ["money", "payment", "fake", "lottery", "investment", "won", "prize"],
            "phishing": ["email", "link", "password", "login", "account", "website", "click"],
            "harassment": ["message", "call", "threat", "abuse", "stalk", "bully", "annoy"],
            "hacking": ["account", "password", "login", "hack", "access", "unauthorized", "phone", "reset", "facebook"],
            "doxxing": ["personal", "information", "private", "leak", "expose", "details"],
            "fraud": ["bank", "card", "transaction", "unauthorized", "payment", "money"]
        }
        description_lower = description.lower()
        for crime_type, keywords in crime_keywords.items():
            if any(keyword in description_lower for keyword in keywords):
                return crime_type
        return "other"

    async def _confirm_details(self):
        print("🎯 _confirm_details() called")
        summary = f"""
        Let me confirm your details:
        Name: {self.case_data.name or 'Not provided'}
        Phone: {self.case_data.phone or 'Not provided'}
        Email: {self.case_data.email or 'Not provided'}
        Crime Type: {self.case_data.crime_type or 'Not classified'}
        Date: {self.case_data.incident_date or 'Not provided'}
        Is this information correct?
        """
        await self._speak(summary)
        self.current_step = "confirmation"

    async def _save_and_send_form(self):
        try:
            print("💾 _save_and_send_form() called")
            print("🔍 === DEBUG CASE DATA ===")
            print(f"🔍 Name: '{self.case_data.name}'")
            print(f"🔍 Phone: '{self.case_data.phone}'")
            print(f"🔍 Email: '{self.case_data.email}'")
            print(f"🔍 Crime Type: '{self.case_data.crime_type}'")
            print(f"🔍 Incident Date: '{self.case_data.incident_date}'")
            print(f"🔍 Description: '{self.case_data.description}'")
            print(f"🔍 Consent: {self.case_data.consent_recorded}")
            print(f"🔍 Emergency: {self.case_data.is_emergency}")
            print(f"🔍 Transcript length: {len(self.case_data.transcript)}")
            print("🔍 ========================")
            
            case_dict = asdict(self.case_data)
            print(f"📦 Converted case dict: {case_dict}")
            
            required_fields = ["name", "phone", "email", "crime_type", "incident_date", "description"]
            missing_fields = [field for field in required_fields if not case_dict.get(field)]
            
            if missing_fields:
                print(f"❌ Missing required fields: {missing_fields}")
                await self._speak("I'm missing some important information. Let's try again.")
                self.current_step = "name"
                await self._ask_name()
                return
            
            print("🔄 Calling DBService.create_case...")
            try:
                case_id = await asyncio.to_thread(self.db_service.create_case, case_dict)
                print(f"🔍 DBService.create_case returned: {case_id!r}")
            except Exception as e:
                import traceback
                print("❌ Exception when saving case to DB!", e)
                print(traceback.format_exc())
                case_id = None
            
            if case_id:
                self.case_saved = True
                print(f"✅ Case saved successfully: {case_id}")
                if self.case_data.phone:
                    form_link = self.form_service.get_prefill_link(case_id)
                    message = (
                        f"Hello {self.case_data.name}, your case number is {case_id}. "
                        f"Verify and complete your report: {form_link}. "
                        f"If this is urgent, reply 'EMERGENCY'."
                    )
                    print(f"📱 Preparing to send SMS to {self.case_data.phone}")
                    sms_result = await asyncio.to_thread(self.sms_service.send, self.case_data.phone, message)
                    if sms_result:
                        print(f"✅ SMS sent successfully to {self.case_data.phone}")
                    else:
                        print(f"⚠️ SMS failed to send to {self.case_data.phone}")
                final_msg = f"""
                Thank you for reporting. I've saved your case with number {case_id}. 
                I'm sending you an SMS with your case number and a link to the form where you can update any information.
                Thank you for calling Safe Line.
                """
                await self._speak(final_msg)
                print("🎉 Conversation completed successfully!")
            else:
                print("❌ DBService.create_case returned None - case not saved")
                await self._speak("I'm sorry, but I couldn't save your case right now. Please try calling again.")
        except Exception as e:
            print(f"⚠️ Error in save and send: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            await self._speak("There was an error processing your case. Please call back.")

    def _extract_name(self, text: str) -> str:
        print(f"🔍 _extract_name() called with: '{text}'")
        if len(text.strip().split()) <= 3:
            return text.strip()
        patterns = [
            r'my name is ([A-Za-z\s]{2,})',
            r'i am ([A-Za-z\s]{2,})', 
            r'name is ([A-Za-z\s]{2,})',
            r'call me ([A-Za-z\s]{2,})',
            r'this is ([A-Za-z\s]{2,})',
            r'([A-Z][a-z]+ [A-Z][a-z]+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                name = match.group(1).strip()
                if len(name) > 2:
                    return name
        return text.strip() if len(text.strip()) > 2 else ""

    def _extract_phone(self, text: str) -> str:
        print(f"🔍 _extract_phone() called with: '{text}'")
        match = re.search(r'(\d{10})', text)
        if match:
            return match.group(1)
        match = re.search(r'(\d{3}[-\.\s]??\d{3}[-\.\s]??\d{4})', text)
        if match:
            digits = re.sub(r'\D', '', match.group(1))
            if len(digits) == 10:
                return digits
        digits = re.sub(r'\D', '', text)
        if len(digits) == 10:
            return digits
        return ""

    def _extract_email(self, text: str) -> str:
        print(f"🔍 _extract_email() called with: '{text}'")
        text = text.lower().replace(' at ', '@').replace(' dot ', '.').replace(' ', '')
        match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
        return match.group(0) if match else ""

    def _extract_date(self, text: str) -> str:
        print(f"🔍 _extract_date() called with: '{text}'")
        text_lower = text.lower()
        today = datetime.date.today()
        if "today" in text_lower:
            return today.isoformat()
        elif "yesterday" in text_lower:
            return (today - datetime.timedelta(days=1)).isoformat()
        elif "day before yesterday" in text_lower:
            return (today - datetime.timedelta(days=2)).isoformat()
        else:
            patterns = [
                r'(\d{1,2}/\d{1,2}/\d{4})',
                r'(\d{1,2}-\d{1,2}-\d{4})',
                r'(\d{4}-\d{1,2}-\d{1,2})',
                r'(\d{1,2} [A-Za-z]+ \d{4})'
            ]
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    return match.group(1)
        return text.strip()

# Entrypoint
async def entrypoint(ctx: JobContext):
    print("🚀 entrypoint() called")
    print(f"🔍 Connecting to room: {ctx.room.name}")
    
    try:
        await ctx.connect()
        print("✅ Successfully connected to LiveKit room")
    except Exception as e:
        print(f"❌ Failed to connect to LiveKit room: {e}")
        return

    agent = SafeLineAgent()
    session = AgentSession()
    agent._session_ref = session
    
    print(f"🔍 Starting session for agent in room: {ctx.room.name}")
    await agent._setup_transcript_recording(ctx.room.name)
    
    async def shutdown_callback():
        print("🛑 Shutdown callback called - finalizing transcript")
        await agent._finalize_transcript()
        if agent.case_saved and agent.transcript_file:
            print(f"📋 Transcript saved to file: {agent.transcript_file}")
    
    ctx.add_shutdown_callback(shutdown_callback)
    
    try:
        await session.start(room=ctx.room, agent=agent)
        print("✅ Session started successfully")
    except Exception as e:
        print(f"❌ Failed to start session: {e}")

if __name__ == "__main__":
    print("🚀 Starting LiveKit agent...")
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))