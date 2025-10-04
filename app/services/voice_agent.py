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
# OPTIMIZED SafeLine Agent with Robust Phone Collection
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
        
        # OPTIMIZED: Better state management
        self._is_speaking = False
        self._waiting_for_response = False
        self._pending_user_input = None
        self._current_question = ""
        self._last_question_time = None
        self._timeout_task = None
        self._current_tts_task = None
        
        # OPTIMIZED: Robust phone number collection
        self._phone_digits = []
        self._phone_collection_mode = "full"  # 'full' or 'digits'
        self._phone_attempts = 0
        self._max_phone_attempts = 2
        self._digit_timeout_task = None
        
        # Conversation flow tracking
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
        """OPTIMIZED: Robust speaking with single TTS task management"""
        try:
            print(f"🗣️ Speaking: {text}")
            
            # Cancel any ongoing TTS safely
            if self._current_tts_task and not self._current_tts_task.done():
                try:
                    self._current_tts_task.cancel()
                    await asyncio.sleep(0.05)  # Minimal pause for cleanup
                except Exception as e:
                    print(f"🔄 TTS cancellation note: {e}")
            
            # Set speaking state
            self._is_speaking = True
            self._waiting_for_response = False
            self._current_question = question_type
            
            await self._add_to_transcript("agent", text, self.current_step)
            
            # Brief pause before speaking
            await asyncio.sleep(0.1)
            
            # Single TTS task with robust error handling
            async def execute_tts():
                try:
                    if hasattr(self, '_session_ref') and self._session_ref:
                        await self._session_ref.say(text)
                    elif hasattr(self.tts, 'speak'):
                        await self.tts.speak(text)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"🔇 TTS execution error: {e}")
            
            self._current_tts_task = asyncio.create_task(execute_tts())
            
            # Wait for completion with timeout
            try:
                await asyncio.wait_for(self._current_tts_task, timeout=8.0)
            except asyncio.TimeoutError:
                print("⏰ TTS timeout, continuing...")
            except asyncio.CancelledError:
                print("🔄 TTS cancelled during execution")
                    
        except Exception as e:
            print(f"⚠️ Error in _speak: {e}")
        finally:
            # Always reset states
            self._is_speaking = False
            self._waiting_for_response = True
            self._last_question_time = datetime.datetime.now()
            print(f"⏳ Now waiting for user response for: {question_type}")
            
            # Process any pending input
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
            is_final = getattr(evt, 'is_final', False)
            print(f"🎤 user_input_transcribed: '{evt.transcript}' (final: {is_final})")
            
            # Process only final transcriptions for smoother flow
            if is_final:
                asyncio.create_task(self._handle_user_input(evt.transcript))

        print("✅ Event listeners setup complete")

    async def _handle_user_input(self, transcription: str):
        """OPTIMIZED: Handle user input with phone-aware routing"""
        print(f"🎯 _handle_user_input() called with: '{transcription}'")
        
        if not transcription.strip():
            print("⚠️ Empty transcription, ignoring")
            return
        
        # Special handling for phone number collection
        if self.current_step == "phone":
            await self._handle_phone_input(transcription)
            return
        
        # Standard handling for other steps
        if self._is_speaking:
            print(f"⏸️ Agent speaking, storing input: '{transcription}'")
            self._pending_user_input = transcription
            return
            
        if not self._waiting_for_response:
            print(f"⚠️ Not waiting for response, ignoring: '{transcription}'")
            return
            
        await self._process_user_transcription(transcription)

    async def _handle_phone_input(self, transcription: str):
        """OPTIMIZED: Dedicated phone input handler"""
        text = transcription.strip().lower()
        
        # Check for skip requests
        if any(word in text for word in ['skip', 'later', 'not now', "don't have", 'no phone', 'no']):
            print("📱 User wants to skip phone")
            self.case_data.phone = "Not provided"
            await self._speak("No problem. Let's continue with your email address.", "phone_skip")
            await asyncio.sleep(0.2)
            await self._ask_email()
            return
        
        # Check for digit-by-digit mode
        if self._phone_collection_mode == "digits":
            await self._process_phone_digit(text)
            return
        
        # Try full phone number extraction first
        phone = self._extract_phone_robust(text)
        
        if phone and len(phone) >= 10:
            # Successfully extracted complete phone
            self.case_data.phone = phone
            print(f"✅ Extracted complete phone: {phone}")
            await self._speak(f"Thank you. I have {phone}.", "phone_complete")
            await asyncio.sleep(0.2)
            await self._ask_email()
        elif phone and len(phone) >= 7:
            # Partial but substantial number
            self.case_data.phone = phone
            print(f"✅ Extracted partial phone: {phone}")
            await self._speak(f"I have {phone}. Let's use this number.", "phone_partial")
            await asyncio.sleep(0.2)
            await self._ask_email()
        else:
            # Switch to digit-by-digit collection
            self._phone_attempts += 1
            print(f"❌ Could not extract phone (attempt {self._phone_attempts})")
            
            if self._phone_attempts >= self._max_phone_attempts:
                # After max attempts, use what we have or skip
                if phone:
                    self.case_data.phone = phone
                    print(f"🔄 Using extracted digits: {phone}")
                    await self._speak(f"Let's use {phone} as your phone number. Continuing to email.", "phone_fallback")
                else:
                    self.case_data.phone = "Not provided"
                    await self._speak("Let's continue without a phone number.", "phone_skip")
                await asyncio.sleep(0.2)
                await self._ask_email()
            else:
                # Switch to digit-by-digit mode
                await self._start_digit_collection()

    async def _start_digit_collection(self):
        """Start digit-by-digit phone number collection"""
        print("🔄 Starting digit-by-digit phone collection")
        self._phone_collection_mode = "digits"
        self._phone_digits = []
        
        prompt = (
            "Let me get your phone number one digit at a time. "
            "Please say the first digit of your phone number."
        )
        await self._speak(prompt, "phone_digit_start")
        
        # Start digit timeout monitor
        if self._digit_timeout_task:
            self._digit_timeout_task.cancel()
        self._digit_timeout_task = asyncio.create_task(self._monitor_digit_timeout())

    async def _process_phone_digit(self, text: str):
        """Process individual phone digits"""
        digit = self._text_to_digit(text)
        
        if digit:
            self._phone_digits.append(digit)
            current_length = len(self._phone_digits)
            print(f"📱 Added digit '{digit}', progress: {current_length}/10 - {' '.join(self._phone_digits)}")
            
            # Reset timeout on successful digit
            if self._digit_timeout_task:
                self._digit_timeout_task.cancel()
            self._digit_timeout_task = asyncio.create_task(self._monitor_digit_timeout())
            
            # Provide appropriate feedback
            if current_length == 10:
                # Complete number collected
                phone_number = ''.join(self._phone_digits)
                self.case_data.phone = phone_number
                print(f"✅ Digit collection complete: {phone_number}")
                await self._speak(f"Thank you. Your phone number is {phone_number}.", "phone_digit_complete")
                self._phone_collection_mode = "full"
                self._phone_digits = []
                await asyncio.sleep(0.2)
                await self._ask_email()
            elif current_length in [3, 6]:
                # Milestone feedback
                progress = " ".join(self._phone_digits[-3:])
                await self._speak(f"{progress}... Please continue with the next digits.", "phone_digit_progress")
            else:
                # Simple acknowledgment
                await self._speak("...", "phone_digit_ack")
        else:
            print(f"❌ Could not convert '{text}' to digit")
            await self._speak("I didn't catch that as a digit. Please say a number from 0 to 9.", "phone_digit_retry")

    async def _monitor_digit_timeout(self):
        """Monitor timeout during digit collection"""
        try:
            await asyncio.sleep(10.0)  # 10 second timeout for digits
            if self._phone_collection_mode == "digits" and self._phone_digits:
                print("⏰ Digit collection timeout")
                await self._handle_digit_timeout()
        except asyncio.CancelledError:
            pass  # Normal cancellation when digit is received

    async def _handle_digit_timeout(self):
        """Handle timeout during digit collection"""
        if len(self._phone_digits) >= 7:
            # Use collected digits
            phone_number = ''.join(self._phone_digits)
            self.case_data.phone = phone_number
            print(f"🔄 Using timeout-collected digits: {phone_number}")
            await self._speak(f"I'll use {phone_number} as your phone number. Let's continue.", "phone_digit_timeout")
        else:
            # Not enough digits, fall back
            self.case_data.phone = "Not provided"
            await self._speak("Let's continue without a complete phone number.", "phone_digit_skip")
        
        self._phone_collection_mode = "full"
        self._phone_digits = []
        await asyncio.sleep(0.2)
        await self._ask_email()

    def _extract_phone_robust(self, text: str) -> str:
        """ROBUST phone number extraction with multiple strategies"""
        print(f"🔍 Extracting phone from: '{text}'")
        
        # Strategy 1: Direct digit extraction
        digits_only = re.sub(r'\D', '', text)
        if len(digits_only) >= 10:
            return digits_only[:10]
        
        # Strategy 2: Spoken number conversion
        digit_map = {
            'zero': '0', 'oh': '0', 'o': '0', 'nought': '0',
            'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
            'six': '6', 'seven': '7', 'eight': '8', 'nine': '9'
        }
        
        # Enhanced pattern matching for spoken numbers
        patterns = [
            # Handle "nine eight seven six five four three two one zero" etc.
            r'(?:(?:zero|oh|o|nought|one|two|three|four|five|six|seven|eight|nine|\d)\s*)+',
            # Handle "nine eight seven six" etc.
            r'(?:(?:zero|oh|o|nought|one|two|three|four|five|six|seven|eight|nine|\d)\s+)+',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text.lower())
            if matches:
                digits = []
                for match in matches:
                    words = re.findall(r'[a-z]+|\d+', match)
                    for word in words:
                        if word.isdigit():
                            digits.append(word)
                        elif word in digit_map:
                            digits.append(digit_map[word])
                        elif word == 'double' and digits:
                            digits.append(digits[-1])
                        elif word == 'triple' and digits:
                            digits.extend([digits[-1], digits[-1]])
                
                result = ''.join(digits)
                if len(result) >= 7:
                    return result
        
        # Strategy 3: Look for phone number patterns
        phone_patterns = [
            r'(\d{3}[-\.\s]??\d{3}[-\.\s]??\d{4})',
            r'(\d{10})',
            r'(\d{3}\s\d{3}\s\d{4})',
            r'(\d{3}-\d{3}-\d{4})',
        ]
        
        for pattern in phone_patterns:
            match = re.search(pattern, text)
            if match:
                digits = re.sub(r'\D', '', match.group(1))
                if len(digits) >= 10:
                    return digits[:10]
        
        return ""

    def _text_to_digit(self, text: str) -> str:
        """Convert spoken text to single digit"""
        digit_map = {
            'zero': '0', 'oh': '0', 'o': '0', 'nought': '0',
            'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5',
            'six': '6', 'seven': '7', 'eight': '8', 'nine': '9'
        }
        
        text_lower = text.lower().strip()
        
        # Direct digit
        if text in '0123456789':
            return text
        
        # Single digit word
        if text_lower in digit_map:
            return digit_map[text_lower]
        
        # Try to extract from phrases
        words = text_lower.split()
        for word in words:
            if word in digit_map:
                return digit_map[word]
            elif word.isdigit() and len(word) == 1:
                return word
        
        return ""

    async def _process_user_transcription(self, transcription: str):
        """Main processing with robust error handling"""
        print(f"🎯 _process_user_transcription() called with: '{transcription}'")
        print(f"📋 Current step: {self.current_step}")
        
        # Reset waiting flag
        self._waiting_for_response = False
        
        if not transcription.strip():
            await self._speak("I didn't hear you. Could you please repeat that?", "repeat")
            return

        # Add to transcript
        self.transcript += f"User: {transcription}\n"
        self.case_data.transcript = self.transcript
        await self._add_to_transcript("user", transcription, self.current_step)

        try:
            # Route to appropriate step handler
            if self.current_step == "greeting":
                await self._process_greeting_response(transcription)
            elif self.current_step == "emergency_check":
                await self._process_emergency_check_response(transcription)
            elif self.current_step == "consent":
                await self._process_consent_response(transcription)
            elif self.current_step == "name":
                await self._process_name_response(transcription)
            elif self.current_step == "phone":
                # Already handled by _handle_phone_input
                pass
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
                await self._speak("Let me start over. How can I help you today?", "restart")
                await self._start_conversation()
                
        except Exception as e:
            print(f"⚠️ Error in _process_user_transcription: {e}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            await self._speak("I encountered an issue. Let me ask that again.", "error_recovery")
            await self._recover_from_error()

    async def _recover_from_error(self):
        """Recover from errors gracefully"""
        if self.current_step == "phone":
            await self._ask_phone()
        elif self.current_step == "email":
            await self._ask_email()
        elif self.current_step == "name":
            await self._ask_name()
        else:
            await self._start_conversation()

    # Step processing methods
    async def _process_greeting_response(self, transcription: str):
        """Process response to initial greeting"""
        print("➡️ Processing greeting response")
        
        acknowledgment = "I understand you need help. "
        if "help" in transcription.lower():
            acknowledgment = "I'm here to help. "
        
        await self._speak(acknowledgment + "Is this an ongoing threat or emergency situation?", "emergency_check")
        self.current_step = "emergency_check"

    async def _process_emergency_check_response(self, transcription: str):
        """Process response to emergency check"""
        print("➡️ Processing emergency check response")
        text_clean = transcription.lower().strip()
        
        if any(word in text_clean for word in ['yes', 'yeah', 'yep', 'sure', 'definitely', 'absolutely', 'right now', 'ongoing']):
            print("🚨 User confirmed emergency situation")
            self.case_data.is_emergency = True
            await self._handle_emergency()
        elif any(word in text_clean for word in ['no', 'nope', 'not', "don't", 'no emergency', 'not ongoing']):
            print("✅ No emergency situation")
            await self._speak("Thank you for confirming. Let me ask for your consent to record this conversation.", "no_emergency")
            await asyncio.sleep(0.2)
            await self._ask_consent()
        else:
            print("❓ Unclear emergency response")
            if self._has_emergency_keywords(transcription):
                print("🚨 Emergency keywords detected")
                self.case_data.is_emergency = True
                await self._handle_emergency()
            else:
                await self._speak("Please say 'yes' if this is an ongoing emergency, or 'no' if it's not.", "emergency_clarify")

    def _has_emergency_keywords(self, text: str) -> bool:
        """Check if text contains emergency keywords"""
        emergency_keywords = [
            "emergency", "urgent", "right now", "immediate", "help now",
            "ongoing", "happening now", "currently", "live threat", "active",
            "threatening", "danger", "dangerous", "unsafe", "threat now",
            "bank transfer now", "money transfer now", "transaction ongoing",
            "ransom", "blackmail", "extortion", "threat to life", "someone here"
        ]
        
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in emergency_keywords)

    async def _process_consent_response(self, transcription: str):
        """Process consent response"""
        print("➡️ Processing consent response")
        text_clean = transcription.lower().strip()
        
        if any(word in text_clean for word in ['yes', 'yeah', 'sure', 'okay', 'ok', 'yep', 'yes.']):
            self.case_data.consent_recorded = True
            print("✅ User consented to recording")
            await self._speak("Thank you for your consent. Let's start with your basic information.", "consent_ack")
            await asyncio.sleep(0.2)
            await self._ask_name()
        elif any(word in text_clean for word in ['no', 'not', "don't", 'nope', 'no.']):
            print("❌ User did not consent")
            await self._speak("I understand. I'll only collect basic information without recording. Let's start with your name.", "consent_ack")
            await asyncio.sleep(0.2)
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
            await asyncio.sleep(0.2)
            await self._ask_phone()
        else:
            self._current_field_attempts += 1
            print(f"❌ Could not extract valid name (attempt {self._current_field_attempts})")
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                if transcription.strip():
                    self.case_data.name = transcription.strip()[:50]
                    print(f"🔄 Using provided text as name: {self.case_data.name}")
                    await self._speak("Let's proceed with your phone number.", "name_fallback")
                else:
                    self.case_data.name = "Not provided"
                    await self._speak("Let's proceed with your phone number.", "name_skip")
                
                self._current_field_attempts = 0
                await asyncio.sleep(0.2)
                await self._ask_phone()
            else:
                await self._speak("I didn't catch your name clearly. Could you please tell me your full name?", "name_retry")

    async def _process_email_response(self, transcription: str):
        """Process email response"""
        print("➡️ Processing email response")
        
        text_lower = transcription.lower().strip()
        
        # Check for skip requests
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have", 'no email']):
            print("📧 User wants to skip email")
            self.case_data.email = "Not provided"
            await self._speak("No problem. Please describe what happened in your own words.", "email_skip")
            await asyncio.sleep(0.2)
            await self._ask_description()
            return
        
        email = self._extract_email(transcription)
        if email and self._is_valid_email(email):
            self.case_data.email = email
            print(f"✅ Extracted email: {email}")
            await self._speak("Thank you.", "email_ack")
            self._current_field_attempts = 0
            await asyncio.sleep(0.2)
            await self._ask_description()
        else:
            self._current_field_attempts += 1
            print(f"❌ Could not extract email (attempt {self._current_field_attempts})")
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                if self.case_data.name and self.case_data.name != "Not provided":
                    self.case_data.email = f"{self.case_data.name.lower().replace(' ', '')}@safe-line.example"
                else:
                    self.case_data.email = "user@safe-line.example"
                print(f"🔄 Using default email: {self.case_data.email}")
                await self._speak("Let's proceed. Please describe what happened.", "email_skip")
                self._current_field_attempts = 0
                await asyncio.sleep(0.2)
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
            self.current_step = "name"
            self._current_field_attempts = 0
        else:
            print("❓ Unclear confirmation response")
            await self._speak("Please say 'yes' if the information is correct, or 'no' if you need to make changes.", "confirmation_clarify")

    # Conversation flow methods
    async def on_enter(self):
        """Start the conversation flow"""
        print("🚀 on_enter() called - Agent started")
        self._timeout_task = asyncio.create_task(self._check_for_timeout())
        await self._start_conversation()

    async def _start_conversation(self):
        print("🎯 _start_conversation() called")
        self.current_step = "greeting"
        greeting = self._ctx_obj.get("message_templates", {}).get(
            "greeting",
            "Hello, this is the Safe Line cybercrime helpline assistant. How can I help you today?"
        )
        await self._speak(greeting, "greeting")

    async def _handle_emergency(self):
        """Handle emergency situation"""
        print("🚨 _handle_emergency() called")
        emergency_msg = "This sounds urgent! For immediate assistance with ongoing threats, please call our emergency helpline at 1-800-HELP-NOW. A human operator will assist you right away."
        await self._speak(emergency_msg, "emergency")
        
        # End the conversation for emergencies
        self.case_saved = True
        await self._speak("Thank you for contacting Safe Line. Please call the emergency number for immediate help with ongoing threats.", "emergency_end")

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
        """OPTIMIZED: Improved phone number question"""
        print("🎯 _ask_phone() called")
        self.current_step = "phone"
        self._current_field_attempts = 0
        self._phone_attempts = 0
        self._phone_collection_mode = "full"
        self._phone_digits = []
        
        phone_prompt = (
            "What's your phone number? "
            "You can say all 10 digits together, "
            "or if it's easier, I can collect them one by one. "
            "If you don't have a phone, just say 'skip'."
        )
        await self._speak(phone_prompt, "phone")

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

    # Helper methods
    def _extract_name(self, text: str) -> str:
        print(f"🔍 _extract_name() called with: '{text}'")
        
        text = text.strip()
        
        confirmation_words = {
            'yes', 'yeah', 'yep', 'no', 'nope', 'ok', 'okay', 'sure',
            'thank you', 'thanks', 'done', 'good', 'fine', 'hello', 'hi',
            'skip', 'later'
        }
        
        if text.lower() in confirmation_words:
            return ""
            
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
        
        words = text.split()
        if len(words) >= 2 and len(text) > 3 and text.lower() not in confirmation_words:
            return text
        
        return ""

    def _extract_email(self, text: str) -> str:
        print(f"🔍 _extract_email() called with: '{text}'")
        
        text_lower = text.lower().strip()
        
        text_clean = text_lower.replace(' at ', '@').replace(' dot ', '.').replace(' ', '')
        
        match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text_clean)
        if match:
            return match.group(0)
        
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
                    
        if text_lower not in ['yes', 'no', 'okay', 'ok', 'skip']:
            return text.strip()
            
        return ""

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

    async def _check_for_timeout(self):
        """Check if we've been waiting too long for a response"""
        while not self.case_saved:
            try:
                if (self._waiting_for_response and 
                    self._last_question_time and 
                    (datetime.datetime.now() - self._last_question_time).seconds > 25):
                    
                    print("⏰ No response received, prompting user...")
                    await self._speak("Are you still there? Please respond to continue.", "timeout_prompt")
                    self._last_question_time = datetime.datetime.now()
                
                await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Timeout checker error: {e}")
                await asyncio.sleep(5)

    async def _save_and_send_form(self):
        """Save case and send SMS"""
        try:
            print("💾 _save_and_send_form() called")
            
            case_dict = asdict(self.case_data)
            
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
    
    await agent.setup_event_listeners(session)
    
    async def shutdown_callback():
        print("🛑 Shutdown callback called - finalizing transcript")
        if agent._timeout_task:
            agent._timeout_task.cancel()
        if agent._digit_timeout_task:
            agent._digit_timeout_task.cancel()
        if agent._current_tts_task:
            agent._current_tts_task.cancel()
        await agent._finalize_transcript()
        if agent.case_saved and agent.transcript_file:
            print(f"📋 Transcript saved to file: {agent.transcript_file}")
    
    ctx.add_shutdown_callback(shutdown_callback)
    
    try:
        await session.start(room=ctx.room, agent=agent)
        print("✅ Session started successfully")
        
        print("⏳ Waiting for conversation to complete...")
        for i in range(300):
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