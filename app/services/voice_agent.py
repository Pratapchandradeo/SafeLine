import os
import re
import json
import datetime
import asyncio
import sys
from pathlib import Path
from typing import Optional, Set, Dict, Any, List
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

load_dotenv()

# Environment variable checks (keep existing)
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
# Improved SafeLine Agent
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
        
        # Initialize STT, TTS, LLM (keep existing)
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

        # Improved instructions
        instructions = """
        You are a Safe Line cybercrime helpline assistant.
        
        FLOW:
        1. Warm greeting + emergency check
        2. Brief consent request
        3. Collect: name → phone → email → description → date
        4. Classify crime type
        5. Confirm details
        6. Save and send SMS
        
        GUIDELINES:
        - Be warm, professional, and empathetic
        - Keep responses under 2 sentences
        - Ask ONE question at a time
        - Acknowledge responses naturally
        - Provide clear transitions between questions
        - Confirm what you understood
        - Don't rush the user
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
        
        # IMPROVED: Better state management
        self._is_speaking = False
        self._waiting_for_response = False
        self._pending_user_input = None
        self._current_question = ""
        self._last_question_time = None
        self._timeout_task = None
        
        # IMPROVED: Conversation flow tracking
        self._collected_digits: List[str] = []  # For phone number collection
        self._current_field_attempts = 0
        self._max_attempts_per_field = 2
        
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

    async def _speak(self, text: str, question_type: str = ""):
        """Speak text to user with proper state management"""
        try:
            print(f"🗣️ Speaking: {text}")
            
            # Set speaking flag
            self._is_speaking = True
            self._waiting_for_response = False
            self._current_question = question_type
            
            await self._add_to_transcript("agent", text, self.current_step)
            
            if hasattr(self, '_session_ref') and self._session_ref:
                print(f"🔍 Using session.say to speak: {text}")
                await self._session_ref.say(text)
            else:
                print(f"⚠️ No session reference, using direct TTS: {text}")
                if hasattr(self.tts, 'speak'):
                    await self.tts.speak(text)
                    
        except Exception as e:
            print(f"⚠️ TTS error: {e}")
            if hasattr(self.tts, 'speak'):
                await self.tts.speak(text)
        finally:
            # Reset flags and set waiting state
            self._is_speaking = False
            self._waiting_for_response = True
            self._last_question_time = datetime.datetime.now()
            print(f"⏳ Now waiting for user response for: {question_type}")
            
            # Process any pending input that came while speaking
            if self._pending_user_input:
                pending = self._pending_user_input
                self._pending_user_input = None
                print(f"🔄 Processing pending input: '{pending}'")
                await self._process_user_transcription(pending)

    async def setup_event_listeners(self, session):
        """Setup event listeners for user transcriptions"""
        print("🎯 Setting up event listeners for user transcriptions")
        
        @session.on("user_input_transcribed")
        def on_user_transcribed(evt):
            print(f"🎤 user_input_transcribed: '{evt.transcript}' (final: {getattr(evt, 'is_final', False)})")
            
            # Process only final transcriptions
            if getattr(evt, 'is_final', True):
                asyncio.create_task(self._handle_user_input(evt.transcript))

        print("✅ Event listeners setup complete")

    async def _handle_user_input(self, transcription: str):
        """Handle user input with proper state management"""
        print(f"🎯 _handle_user_input() called with: '{transcription}'")
        
        # Ignore empty transcriptions
        if not transcription.strip():
            print("⚠️ Empty transcription, ignoring")
            return
            
        # If agent is speaking, store for later processing
        if self._is_speaking:
            print(f"⏸️ Agent speaking, storing input: '{transcription}'")
            self._pending_user_input = transcription
            return
            
        # If not waiting for response, ignore
        if not self._waiting_for_response:
            print(f"⚠️ Not waiting for response, ignoring: '{transcription}'")
            return
            
        # Process the input
        await self._process_user_transcription(transcription)

    async def _process_user_transcription(self, transcription: str):
        """Process user input based on current step"""
        print(f"🎯 _process_user_transcription() called with: '{transcription}'")
        print(f"📋 Current step: {self.current_step}")
        print(f"📋 Current question: {self._current_question}")
        
        # Reset waiting flag since we're processing a response
        self._waiting_for_response = False
        
        if not transcription.strip():
            print("⚠️ Empty transcription received, prompting user to repeat")
            await self._speak("I didn't hear you. Could you please repeat that?", "repeat")
            return

        # Add to transcript
        self.transcript += f"User: {transcription}\n"
        self.case_data.transcript = self.transcript
        await self._add_to_transcript("user", transcription, self.current_step)

        try:
            # IMPROVED: Check for emergency in ANY response
            if self._is_emergency(transcription) and not self.case_data.is_emergency:
                self.case_data.is_emergency = True
                await self._handle_emergency()
                return

            # IMPROVED: Better step routing with context awareness
            if self.current_step == "greeting":
                await self._process_greeting_response(transcription)
                        
            elif self.current_step == "consent":
                await self._process_consent_response(transcription)
                
            elif self.current_step == "name":
                await self._process_name_response(transcription)
                
            elif self.current_step == "phone":
                await self._process_phone_response(transcription)
                
            elif self.current_step == "email":
                await self._process_email_response(transcription)
                
            elif self.current_step == "description":
                await self._process_description_response(transcription)
                
            elif self.current_step == "date":
                await self._process_date_response(transcription)
                
            elif self.current_step == "confirmation":
                await self._process_confirmation_response(transcription)
            else:
                print(f"❓ Unknown step: {self.current_step}")
                
        except Exception as e:
            print(f"⚠️ Error in _process_user_transcription: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            await self._speak("Something went wrong. Please try again.", "error")

    # IMPROVED: Separate processing methods for better organization
    async def _process_greeting_response(self, transcription: str):
        """Process response to initial greeting"""
        print("➡️ Processing greeting response")
        
        # Acknowledge the user's response naturally
        acknowledgment = "I understand you need help. "
        if "help" in transcription.lower():
            acknowledgment = "I'm here to help. "
        
        await self._speak(acknowledgment + "First, I need to ask for your consent to record this conversation for your case file.", "consent_transition")
        await asyncio.sleep(0.5)  # Small pause for natural flow
        await self._ask_consent()

    async def _process_consent_response(self, transcription: str):
        """Process consent response"""
        print("➡️ Processing consent response")
        text_clean = transcription.lower().strip()
        
        if any(word in text_clean for word in ['yes', 'yeah', 'sure', 'okay', 'ok', 'yep', 'yes.']):
            self.case_data.consent_recorded = True
            print("✅ User consented to recording")
            await self._speak("Thank you for your consent. Let's start with your basic information.", "consent_ack")
            await asyncio.sleep(0.5)
            await self._ask_name()
        elif any(word in text_clean for word in ['no', 'not', "don't", 'nope', 'no.']):
            print("❌ User did not consent")
            await self._speak("I understand. I'll only collect basic information without recording. Let's start with your name.", "consent_ack")
            await asyncio.sleep(0.5)
            await self._ask_name()
        else:
            print("❓ Unclear consent response")
            await self._speak("Please say 'yes' if you consent to recording, or 'no' if you don't.", "consent_clarify")

    async def _process_name_response(self, transcription: str):
        """Process name response"""
        print("➡️ Processing name response")
        name = self._extract_name(transcription)
        
        if name and len(name) > 2 and self._is_valid_name(name):
            self.case_data.name = name
            print(f"✅ Extracted valid name: {name}")
            await self._speak(f"Thank you, {name}.", "name_ack")
            self._current_field_attempts = 0
            await asyncio.sleep(0.5)
            await self._ask_phone()
        else:
            self._current_field_attempts += 1
            print(f"❌ Could not extract valid name (attempt {self._current_field_attempts})")
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                # After max attempts, move forward
                if transcription.strip():
                    self.case_data.name = transcription.strip()[:50]  # Limit length
                    print(f"🔄 Using provided text as name: {self.case_data.name}")
                    await self._speak("Let's proceed with your phone number.", "name_fallback")
                else:
                    self.case_data.name = "Not provided"
                    await self._speak("Let's proceed with your phone number.", "name_skip")
                
                self._current_field_attempts = 0
                await asyncio.sleep(0.5)
                await self._ask_phone()
            else:
                await self._speak("I didn't catch your name clearly. Could you please tell me your full name?", "name_retry")

    async def _process_phone_response(self, transcription: str):
        """Process phone response with improved digit collection"""
        print("➡️ Processing phone response")
        
        # Check if this might be an attempt to skip
        text_lower = transcription.lower().strip()
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have"]):
            print("📱 User wants to skip phone")
            self.case_data.phone = "Not provided"
            await self._speak("No problem. Let's continue with your email address.", "phone_skip")
            await asyncio.sleep(0.5)
            await self._ask_email()
            return
        
        # Extract digits from current response
        current_digits = self._extract_digits(transcription)
        self._collected_digits.extend(list(current_digits))
        
        print(f"📱 Collected digits so far: {''.join(self._collected_digits)}")
        
        if len(self._collected_digits) >= 10:
            # We have enough digits
            phone_number = ''.join(self._collected_digits)[:10]  # Take first 10 digits
            self.case_data.phone = phone_number
            print(f"✅ Collected complete phone: {phone_number}")
            await self._speak(f"Got it, {phone_number}.", "phone_ack")
            self._collected_digits = []
            self._current_field_attempts = 0
            await asyncio.sleep(0.5)
            await self._ask_email()
        else:
            # Need more digits
            self._current_field_attempts += 1
            digits_needed = 10 - len(self._collected_digits)
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                # After max attempts, use what we have or skip
                if self._collected_digits:
                    self.case_data.phone = ''.join(self._collected_digits)
                    print(f"🔄 Using partial phone: {self.case_data.phone}")
                    await self._speak(f"Using the number {self.case_data.phone}. Let's continue with your email.", "phone_partial_fallback")
                else:
                    self.case_data.phone = "Not provided"
                    await self._speak("Let's skip the phone number for now and continue with your email.", "phone_skip")
                
                self._collected_digits = []
                self._current_field_attempts = 0
                await asyncio.sleep(0.5)
                await self._ask_email()
            else:
                # Ask for more digits
                if self._collected_digits:
                    current = ''.join(self._collected_digits)
                    await self._speak(f"I have {len(self._collected_digits)} digits: {current}. I need {digits_needed} more digits.", "phone_continue")
                else:
                    await self._speak("I didn't get enough digits. Please say the 10-digit phone number.", "phone_retry")

    async def _process_email_response(self, transcription: str):
        """Process email response"""
        print("➡️ Processing email response")
        
        # Check if this might be an attempt to skip
        text_lower = transcription.lower().strip()
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have"]):
            print("📧 User wants to skip email")
            self.case_data.email = "Not provided"
            await self._speak("No problem. Please describe what happened in your own words.", "email_skip")
            await asyncio.sleep(0.5)
            await self._ask_description()
            return
        
        email = self._extract_email(transcription)
        if email and self._is_valid_email(email):
            self.case_data.email = email
            print(f"✅ Extracted email: {email}")
            await self._speak("Thank you.", "email_ack")
            self._current_field_attempts = 0
            await asyncio.sleep(0.5)
            await self._ask_description()
        else:
            self._current_field_attempts += 1
            print(f"❌ Could not extract email (attempt {self._current_field_attempts})")
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                # After max attempts, use default or skip
                if self.case_data.name and self.case_data.name != "Not provided":
                    self.case_data.email = f"{self.case_data.name.lower().replace(' ', '')}@safe-line.example"
                else:
                    self.case_data.email = "user@safe-line.example"
                print(f"🔄 Using default email: {self.case_data.email}")
                await self._speak("Let's proceed. Please describe what happened.", "email_skip")
                self._current_field_attempts = 0
                await asyncio.sleep(0.5)
                await self._ask_description()
            else:
                await self._speak("I didn't catch a valid email address. Could you please say it clearly, like 'john at gmail dot com'?", "email_retry")

    async def _process_description_response(self, transcription: str):
        """Process description response"""
        print("➡️ Processing description response")
        
        # Store the description
        self.case_data.description = transcription.strip()
        print(f"✅ Saved description: {transcription[:50]}...")
        
        # Classify crime type
        crime_type = await self._classify_crime_type(transcription)
        self.case_data.crime_type = crime_type
        print(f"✅ Classified crime type: {crime_type}")
        
        # Acknowledge and transition to date
        await self._speak(f"I understand. This sounds like {crime_type}. When did this happen?", "description_ack")
        self.current_step = "date"

    async def _process_date_response(self, transcription: str):
        """Process date response"""
        print("➡️ Processing date response")
        date = self._extract_date(transcription)
        
        if date and self._is_valid_date(date):
            self.case_data.incident_date = date
            print(f"✅ Extracted valid date: {date}")
            await self._confirm_details()
        else:
            self._current_field_attempts += 1
            print(f"❌ Could not extract valid date (attempt {self._current_field_attempts})")
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                # Use today's date as default
                self.case_data.incident_date = datetime.date.today().isoformat()
                print(f"🔄 Using default date: {self.case_data.incident_date}")
                await self._speak("Let's use today's date. Now let me confirm your details.", "date_fallback")
                await self._confirm_details()
            else:
                await self._speak("I didn't catch a clear date. When did this happen? You can say 'today', 'yesterday', or a specific date.", "date_retry")

    async def _process_confirmation_response(self, transcription: str):
        """Process confirmation response"""
        print("➡️ Processing confirmation response")
        text_lower = transcription.lower().strip()
        
        if any(word in text_lower for word in ['yes', 'correct', 'right', 'yes that\'s correct', 'yeah', 'okay', 'ok', 'good', 'perfect']):
            print("✅ User confirmed details")
            await self._save_and_send_form()
        elif any(word in text_lower for word in ['no', 'wrong', 'incorrect', 'change', 'not correct', 'fix']):
            print("❌ User wants to correct information")
            await self._speak("Let me help you correct the information. What would you like to change?", "correction_start")
            # You could implement a more sophisticated correction flow here
            self.current_step = "name"  # Restart from name for simplicity
            self._current_field_attempts = 0
        else:
            print("❓ Unclear confirmation response")
            await self._speak("Please say 'yes' if the information is correct, or 'no' if you need to make changes.", "confirmation_clarify")

    # IMPROVED: Helper methods
    def _extract_digits(self, text: str) -> str:
        """Extract digits from spoken number words or direct digits"""
        _num_map = {
            "zero": "0", "oh": "0", "o": "0", 
            "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9"
        }

        def words_to_digits(s: str) -> str:
            words = re.findall(r'[a-z]+|\d+', s.lower())
            digits = []
            
            for word in words:
                if word.isdigit():
                    digits.append(word)
                elif word in _num_map:
                    digits.append(_num_map[word])
                elif word == "double" and digits:
                    digits.append(digits[-1])
                elif word == "triple" and digits:
                    digits.extend([digits[-1], digits[-1]])
            
            return ''.join(digits)

        # Try direct digit extraction first
        digits_only = re.sub(r'\D', '', text)
        if digits_only:
            return digits_only

        # Try spoken number conversion
        return words_to_digits(text)

    def _is_valid_name(self, name: str) -> bool:
        """Check if the extracted name is valid"""
        invalid_names = {
            'yes', 'no', 'okay', 'ok', 'thank you', 'thanks', 'done', 
            'done then', 'good', 'fine', 'hello', 'hi', 'skip', 'later'
        }
        
        name_lower = name.lower().strip()
        return (name_lower not in invalid_names and 
                len(name) >= 2 and 
                not all(char.isdigit() for char in name))

    def _is_valid_email(self, email: str) -> bool:
        """Basic email validation"""
        return '@' in email and '.' in email and len(email) > 5

    def _is_valid_date(self, date: str) -> bool:
        """Check if the extracted date is valid"""
        invalid_dates = {'yes', 'no', 'okay', 'ok', 'thank you', 'skip'}
        return date.lower().strip() not in invalid_dates

    # IMPROVED: Timeout checker
    async def _check_for_timeout(self):
        """Check if we've been waiting too long for a response"""
        while not self.case_saved:
            try:
                if (self._waiting_for_response and 
                    self._last_question_time and 
                    (datetime.datetime.now() - self._last_question_time).seconds > 30):  # Reduced to 30 seconds
                    
                    print("⏰ No response received, prompting user...")
                    await self._speak("Are you still there? Please respond to continue.", "timeout_prompt")
                    self._last_question_time = datetime.datetime.now()
                
                await asyncio.sleep(5)  # Check more frequently
            except Exception as e:
                print(f"⚠️ Timeout checker error: {e}")
                await asyncio.sleep(5)

    # IMPROVED: Conversation flow methods (keep existing but with better transitions)
    async def on_enter(self):
        """Start the conversation flow"""
        print("🚀 on_enter() called - Agent started")
        self._timeout_task = asyncio.create_task(self._check_for_timeout())
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
        await self._speak(greeting, "greeting")

    async def _handle_emergency(self):
        print("🚨 _handle_emergency() called")
        emergency_msg = self._ctx_obj.get("message_templates", {}).get("triage",
                        "This sounds urgent! Let me get your basic details quickly to connect you with help.")
        await self._speak(emergency_msg, "emergency")
        self.case_data.is_emergency = True
        # Skip consent in emergency situations
        await self._ask_name()

    async def _ask_consent(self):
        print("🎯 _ask_consent() called")
        self.current_step = "consent"
        self._current_field_attempts = 0
        consent_msg = self._ctx_obj.get("message_templates", {}).get(
            "consent",
            "For your report, do you consent to recording this conversation? Please say yes or no."
        )
        await self._speak(consent_msg, "consent")

    async def _ask_name(self):
        print("🎯 _ask_name() called")
        self.current_step = "name"
        self._current_field_attempts = 0
        await self._speak("Could you please tell me your full name?", "name")

    async def _ask_phone(self):
        print("🎯 _ask_phone() called")
        self.current_step = "phone"
        self._current_field_attempts = 0
        self._collected_digits = []
        await self._speak("What is your 10-digit phone number? You can say the digits one by one.", "phone")

    async def _ask_email(self):
        print("🎯 _ask_email() called")
        self.current_step = "email"
        self._current_field_attempts = 0
        await self._speak("What is your email address? You can say it like 'john at gmail dot com'.", "email")

    async def _ask_description(self):
        print("🎯 _ask_description() called")
        self.current_step = "description"
        await self._speak("Please describe what happened in your own words.", "description")

    async def _ask_date(self):
        print("🎯 _ask_date() called")
        self.current_step = "date"
        self._current_field_attempts = 0
        await self._speak("When did this incident happen? You can say 'today', 'yesterday', or a specific date.", "date")

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
        Incident: {self.case_data.crime_type or 'Not classified'}
        Date: {self.case_data.incident_date or 'Not provided'}
        
        Is this information correct?
        """
        self.current_step = "confirmation"
        await self._speak(summary, "confirmation")

    # Keep existing _extract_name, _extract_email, _extract_date, _save_and_send_form methods
    # (They're already quite good, just ensure they work with the new flow)

    def _extract_name(self, text: str) -> str:
        print(f"🔍 _extract_name() called with: '{text}'")
        
        text = text.strip()
        
        # Common confirmation words to ignore
        confirmation_words = {
            'yes', 'yeah', 'yep', 'no', 'nope', 'ok', 'okay', 'sure',
            'thank you', 'thanks', 'done', 'good', 'fine', 'hello', 'hi',
            'skip', 'later'
        }
        
        if text.lower() in confirmation_words:
            return ""
            
        # Remove common prefixes and extract name
        patterns = [
            r'my name is\s+([A-Za-z\s]{2,})',
            r'i am\s+([A-Za-z\s]{2,})', 
            r'name is\s+([A-Za-z\s]{2,})',
            r'call me\s+([A-Za-z\s]{2,})',
            r'this is\s+([A-Za-z\s]{2,})',
            r'([A-Z][a-z]+ [A-Z][a-z]+)',
            r'([A-Z][a-z]+ [A-Z][a-z]+ [A-Z][a-z]+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                name = match.group(1).strip()
                if len(name) > 2 and name.lower() not in confirmation_words:
                    return name
        
        # If no pattern matched but text looks like a name
        words = text.split()
        if len(words) >= 2 and len(text) > 3 and text.lower() not in confirmation_words:
            return text
        
        return ""

    def _extract_email(self, text: str) -> str:
        print(f"🔍 _extract_email() called with: '{text}'")
        
        text_lower = text.lower().strip()
        
        # Handle "at" and "dot" patterns
        text_clean = text_lower.replace(' at ', '@').replace(' dot ', '.').replace(' ', '')
        
        # Standard email pattern matching
        match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text_clean)
        if match:
            return match.group(0)
        
        # Handle common email providers
        if 'gmail' in text_lower:
            username_match = re.search(r'([a-zA-Z0-9]+)\s*(?:at|@)\s*gmail', text_lower)
            if username_match:
                username = username_match.group(1)
            else:
                words = text_lower.split()
                for i, word in enumerate(words):
                    if 'gmail' in word and i > 0:
                        username = words[i-1]
                        break
                else:
                    username = "user"
            
            username = re.sub(r'[^a-zA-Z0-9]', '', username)
            if not username:
                username = "user"
                
            return f"{username}@gmail.com"
        
        return ""

    def _extract_date(self, text: str) -> str:
        print(f"🔍 _extract_date() called with: '{text}'")
        text_lower = text.lower().strip()
        
        # Don't accept confirmations as dates
        if text_lower in ['yes', 'no', 'okay', 'ok', 'thank you', 'skip']:
            return ""
            
        today = datetime.date.today()
        if "today" in text_lower:
            return today.isoformat()
        elif "yesterday" in text_lower:
            return (today - datetime.timedelta(days=1)).isoformat()
        elif "day before yesterday" in text_lower:
            return (today - datetime.timedelta(days=2)).isoformat()
        else:
            # Try to extract specific date patterns
            patterns = [
                r'(\d{1,2}/\d{1,2}/\d{4})',
                r'(\d{1,2}-\d{1,2}-\d{4})',
                r'(\d{4}-\d{1,2}-\d{1,2})',
                r'(\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4})',
                r'((?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2},? \d{4})'
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.I)
                if match:
                    return match.group(1)
                    
        # If no specific pattern found but it's not a confirmation, return the text
        if text_lower not in ['yes', 'no', 'okay', 'ok', 'skip']:
            return text.strip()
            
        return ""

    async def _save_and_send_form(self):
        """Save case and send SMS (keep existing implementation)"""
        try:
            print("💾 _save_and_send_form() called")
            
            case_dict = asdict(self.case_data)
            
            # Required fields
            required_fields = ["name", "description"]
            missing_fields = [field for field in required_fields if not case_dict.get(field)]
            
            if missing_fields:
                print(f"❌ Missing required fields: {missing_fields}")
                await self._speak("I'm missing some important information. Let's try again.", "missing_info")
                self.current_step = "name"
                self._current_field_attempts = 0
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
                
                if self.case_data.phone and self.case_data.phone != "Not provided":
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
                You'll receive an SMS with your case details and a link to update any information.
                Thank you for calling Safe Line.
                """
                await self._speak(final_msg, "completion")
                print("🎉 Conversation completed successfully!")
            else:
                print("❌ DBService.create_case returned None - case not saved")
                await self._speak("I'm sorry, but I couldn't save your case right now. Please try calling again.", "save_error")
        except Exception as e:
            print(f"⚠️ Error in save and send: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            await self._speak("There was an error processing your case. Please call back.", "error")

# Entrypoint (keep existing)
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
    
    # SETUP EVENT LISTENERS
    await agent.setup_event_listeners(session)
    
    async def shutdown_callback():
        print("🛑 Shutdown callback called - finalizing transcript")
        if agent._timeout_task:
            agent._timeout_task.cancel()
        await agent._finalize_transcript()
        if agent.case_saved and agent.transcript_file:
            print(f"📋 Transcript saved to file: {agent.transcript_file}")
    
    ctx.add_shutdown_callback(shutdown_callback)
    
    try:
        await session.start(room=ctx.room, agent=agent)
        print("✅ Session started successfully")
        
        # Wait for conversation completion
        print("⏳ Waiting for conversation to complete...")
        for i in range(300):  # 5 minute timeout
            if agent.case_saved:
                print("✅ Case saved, completing session")
                break
            await asyncio.sleep(1)
            if i % 30 == 0:
                print(f"⏰ Still waiting... {i} seconds elapsed")
        
        print("🎯 Session completed")
        
    except Exception as e:
        print(f"❌ Failed to start session: {e}")

if __name__ == "__main__":
    print("🚀 Starting LiveKit agent...")
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))