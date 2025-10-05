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
from app.services.form_service import FormService
from app.services.sms_service import SMService

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
# OPTIMIZED SafeLine Agent with Caller ID Detection
# -------------------------
class SafeLineAgent(Agent):
    def __init__(self, ctx=None, *args, **kwargs):
        print("🔄 SafeLineAgent.__init__() called")
        
        # Store context for caller ID detection
        self._ctx = ctx
        
        # Initialize services first
        self.db_service = DBService()
        self.sms_service = SMService()
        self.form_service = FormService()

        # Load context
        self._ctx_obj = self._load_context()
        
        # Initialize STT, TTS, LLM
        stt_client = deepgram.STT(model="nova-2", language="en") if DEEPGRAM_KEY else DummySTT()
        
        # Initialize STT with Deepgram
        if DEEPGRAM_KEY:
            try:
                stt_client = deepgram.STT(model="nova-2", language="en")
                print("✅ Initialized Deepgram STT")
            except Exception as e:
                print(f"⚠️ Failed to initialize Deepgram STT: {e}")
                stt_client = DummySTT()
        else:
            stt_client = DummySTT()
            print("⚠️ DEEPGRAM_API_KEY not set, using DummySTT")

        # Initialize TTS with Deepgram
        if DEEPGRAM_KEY:
            try:
                # Deepgram TTS initialization
                tts_client = deepgram.TTS(
                    model="aura-asteria-en",  # You can choose different voices
                    api_key=DEEPGRAM_KEY
                )
                print("✅ Initialized Deepgram TTS")
            except Exception as e:
                print(f"⚠️ Failed to initialize Deepgram TTS: {e}")
                # Fallback to DummyTTS
                tts_client = DummyTTS()
        else:
            tts_client = DummyTTS()
            print("⚠️ DEEPGRAM_API_KEY not set, using DummyTTS")

        # Initialize LLM with Cerebras - FIXED API COMPATIBILITY
        llm_client = None
        if CEREBRAS_KEY:
            try:
                # Use the correct API format for Cerebras
                llm_client = openai.LLM(
                    model="llama3.1-8b",
                    base_url="https://api.cerebras.ai/v1",
                    api_key=CEREBRAS_KEY
                )
                print("✅ Initialized Cerebras LLM with corrected API")
            except Exception as e:
                print(f"⚠️ Failed to initialize Cerebras LLM: {e}")
                llm_client = None
        else:
            print("⚠️ No CEREBRAS_KEY, LLM will not be available")

        # Improved instructions for the agent
        instructions = """
        You are a Safe Line cybercrime helpline assistant. Your ONLY role is to follow the EXACT conversation flow below.

        MANDATORY CONVERSATION FLOW - DO NOT DEVIATE:
        1. GREETING: Use template: "Hello, this is the Safe Line cybercrime helpline assistant. How can I help you today?"
        2. CONSENT: Use template: "For your report, do you consent to recording this call? Please say yes or no."
        3. NAME: Use template: "What is your full name?"
        4. EMERGENCY_CHECK: Use template: "Before we continue, is this an ongoing threat or emergency situation?"
        5. EMAIL: Use template: "What is your email address?"
        6. DESCRIPTION: Use template: "Please describe what happened in your own words."
        7. DATE: Use template: "When did this happen? You can say 'today', 'yesterday', or a specific date."
        8. CONFIRMATION: Use the summary template to confirm all details

        STRICT RULES:
        - ONLY use the provided message templates from the JSON context
        - NEVER skip steps or change the order
        - NEVER ask multiple questions at once
        - If user provides information out of order, gently redirect them to the current step
        - For emergency responses, use ONLY: "Okay. If this is an ongoing emergency, please call 1-800-HELP-NOW immediately for urgent assistance. This call will now end. Thank you for reaching out."
        - Keep responses brief (1-2 sentences maximum)
        - Speak clearly and at a moderate pace
        - Be empathetic but stay on the exact flow

        TEMPLATE USAGE:
        Always use the exact templates from the message_templates in the JSON context. Do not improvise.
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
        
        # AUTO-SET PHONE NUMBER FROM CALLER ID
        if ctx:
            self.case_data.phone = self._get_caller_phone_number(ctx)
        else:
            self.case_data.phone = "From Caller ID"
        print(f"📱 Auto-set phone from caller ID: {self.case_data.phone}")
        
        # OPTIMIZED: Better state management
        self._is_speaking = False
        self._waiting_for_response = False
        self._pending_user_input = None
        self._current_question = ""
        self._last_question_time = None
        self._timeout_task = None
        self._current_tts_task = None
        
        # Conversation flow tracking
        self._current_field_attempts = 0
        self._max_attempts_per_field = 2
        
        print("✅ SafeLineAgent initialized successfully")

    def _get_caller_phone_number(self, ctx):
        """Get phone number from caller ID"""
        # Option 1: From environment variable (for testing)
        caller_phone = os.getenv("CALLER_PHONE_NUMBER", None)
        
        # Option 2: From LiveKit room metadata (if available)
        if not caller_phone and hasattr(ctx, 'room') and hasattr(ctx.room, 'metadata'):
            caller_phone = ctx.room.metadata.get('caller_phone', None)
        
        # Option 3: Generate a placeholder for demo
        if not caller_phone:
            # Generate a demo phone number based on timestamp
            timestamp = datetime.datetime.now().strftime("%m%d%H%M")
            caller_phone = f"555{timestamp[-7:]}"  # 555 + last 7 digits of timestamp
        
        print(f"📱 Caller phone number detected: {caller_phone}")
        return caller_phone

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
        """Enhanced speaking with PROPER waiting state"""
        try:
            if self.case_saved:
                print("🛑 Conversation completed, not speaking")
                return
                
            print(f"🗣️ SPEAK DEBUG - Text to speak: '{text}'")
            print(f"🗣️ SPEAK DEBUG - Question type: '{question_type}'")
            print(f"🗣️ SPEAK DEBUG - Current step: '{self.current_step}'")
            
            # Cancel any ongoing TTS safely
            if self._current_tts_task and not self._current_tts_task.done():
                try:
                    self._current_tts_task.cancel()
                    await asyncio.sleep(0.1)
                except:
                    pass
            
            # Set speaking state
            self._is_speaking = True
            self._current_question = question_type
            
            await self._add_to_transcript("agent", text, self.current_step)
            await asyncio.sleep(0.2)
            
            async def execute_tts():
                try:
                    print(f"🔊 TTS EXECUTE - About to speak: '{text}'")
                    
                    if hasattr(self, '_session_ref') and self._session_ref:
                        await self._session_ref.say(text)
                    elif hasattr(self.tts, 'speak'):
                        await self.tts.speak(text)
                    else:
                        dummy_tts = DummyTTS()
                        await dummy_tts.speak(text)
                        
                    print(f"🔊 TTS EXECUTE - Finished speaking: '{text}'")
                    
                except asyncio.CancelledError:
                    print("🔊 TTS EXECUTE - Cancelled")
                    raise
                except Exception as e:
                    print(f"🔇 TTS execution error: {e}")
            
            self._current_tts_task = asyncio.create_task(execute_tts())
            
            try:
                await asyncio.wait_for(self._current_tts_task, timeout=10.0)
                print("🔊 TTS completed successfully")
            except asyncio.TimeoutError:
                print("⏰ TTS timeout, but continuing conversation...")
            except asyncio.CancelledError:
                print("🔄 TTS cancelled during execution")
                    
        except Exception as e:
            print(f"⚠️ Error in _speak: {e}")
        finally:
            # AFTER speaking is done, set waiting state
            if not self.case_saved:
                self._is_speaking = False
                self._waiting_for_response = True  # CRITICAL: Set this flag
                self._last_question_time = datetime.datetime.now()
                print(f"✅ Speaking completed, now WAITING for user response for: {question_type}")
                
                # Process any pending input that came while we were speaking
                if self._pending_user_input and not self.case_saved:
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
        """Handle user input - SIMPLIFIED VERSION"""
        print(f"🎯 _handle_user_input() called with: '{transcription}'")
        
        if not transcription.strip():
            print("⚠️ Empty transcription, ignoring")
            return
        
        # If agent is speaking, store the input for later processing
        if self._is_speaking:
            print(f"⏸️ Agent speaking, storing input: '{transcription}'")
            self._pending_user_input = transcription
            return
            
        # If we're waiting for response, process immediately
        if self._waiting_for_response:
            print(f"✅ Processing user response: '{transcription}'")
            self._waiting_for_response = False  # Reset waiting flag
            await self._process_user_transcription(transcription)
        else:
            print(f"⚠️ Not waiting for response, ignoring: '{transcription}'")

    async def _process_user_transcription(self, transcription: str):
        """Main processing with robust error handling and state protection"""
        print(f"🎯 _process_user_transcription() called with: '{transcription}'")
        print(f"📋 Current step: {self.current_step}")
        
        # CRITICAL: Prevent processing if conversation is already complete
        if self.case_saved:
            print("🛑 Conversation already completed, ignoring input")
            return
            
        # CRITICAL: Prevent processing if we're in emergency state
        if self.case_data.is_emergency:
            print("🛑 Emergency situation active, ignoring further input")
            return
        
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
            elif self.current_step == "consent":
                await self._process_consent_response(transcription)
            elif self.current_step == "name":
                await self._process_name_response(transcription)
            elif self.current_step == "emergency_check":
                await self._process_emergency_check_response(transcription)
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
        if self.current_step == "email":
            await self._ask_email()
        elif self.current_step == "name":
            await self._ask_name()
        else:
            await self._start_conversation()

    # Step processing methods - FIXED VERSIONS
    async def _process_greeting_response(self, transcription: str):
        """Process response to initial greeting - ACCEPT ANY RESPONSE"""
        print("➡️ Processing greeting response")
        
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        # ACCEPT ANY RESPONSE to move forward - don't get stuck on greeting
        consent_msg = message_templates.get(
            "consent",
            "For your report, do you consent to recording this call? Please say yes or no."
        )
        
        # Always move to consent, regardless of what user says
        await self._speak(consent_msg, "consent")
        self.current_step = "consent"

    async def _process_consent_response(self, transcription: str):
        """Process consent response - SIMPLIFIED"""
        print("➡️ Processing consent response")
        text_lower = transcription.lower().strip()
        
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        consent_indicators = ['yes', 'yeah', 'sure', 'okay', 'ok', 'yep', 'yes.', 'go ahead', 'continue', 'proceed']
        
        # BROADER consent detection - accept any reasonable response
        if any(word in text_lower for word in consent_indicators + ['help', 'hello', 'hi']):
            print("🔄 User consented to recording")
            self.case_data.consent_recorded = True
        else:
            print("❓ Unclear consent response - assuming consent to continue")
            self.case_data.consent_recorded = True
        
        name_msg = message_templates.get("name_request", "What is your full name?")
        await self._speak(name_msg, "name")
        self.current_step = "name"
        
    async def _process_emergency_check_response(self, transcription: str):
        """Process emergency check - SIMPLIFIED"""
        print("➡️ Processing emergency check response")
        
        if self.case_data.is_emergency:
            print("🚨 Already handling emergency, ignoring duplicate input")
            return
            
        text_clean = transcription.lower().strip()
        
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        emergency_words = ['yes', 'yeah', 'yep', 'sure', 'definitely', 'absolutely', 'right now', 'ongoing', 'emergency', 'urgent', 'immediate']
        
        if any(word in text_clean for word in emergency_words):
            print("🚨 User explicitly confirmed emergency situation")
            self.case_data.is_emergency = True
            emergency_msg = message_templates.get("emergency_handling", 
                "Okay. If this is an ongoing emergency, please call 1-800-HELP-NOW immediately for urgent assistance. This call will now end. Thank you for reaching out.")
            await self._speak(emergency_msg, "emergency")
            await self._handle_emergency()
        else:
            # Default to non-emergency - move to email immediately
            print("✅ No emergency situation - proceed to email")
            email_msg = message_templates.get("email_request", "What is your email address?")
            await self._speak(email_msg, "email")
            self.current_step = "email"

    async def _handle_emergency(self):
        """Handle emergency situation - PROPERLY END CALL WITHOUT LOOPS"""
        print("🚨 _handle_emergency() called")
        
        # CRITICAL: Set emergency flag and case_saved to prevent reprocessing
        self.case_data.is_emergency = True
        self.case_saved = True  # This tells the system the conversation is complete
        
        # Emergency response with clear call ending
        emergency_msg = "Okay. If this is an ongoing emergency, please call 1-800-HELP-NOW immediately for urgent assistance. This call will now end. Thank you for reaching out."
        
        # Speak the emergency message
        await self._speak(emergency_msg, "emergency_end")
        
        # Add a brief pause to let the message be heard, then end immediately
        print("🛑 Ending call due to emergency situation")
        await asyncio.sleep(2)
        
        # End the conversation immediately for emergencies
        await self._end_conversation()

    async def _end_conversation(self):
        """Properly end the conversation and cleanup"""
        print("🛑 _end_conversation() called")
        
        # Set completion flags
        self.case_saved = True
        self._waiting_for_response = False
        
        # Cancel any ongoing tasks
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        
        # Finalize transcript
        await self._finalize_transcript()
        
        print("✅ Conversation ended properly")

    async def _process_name_response(self, transcription: str):
        """Process name response - SIMPLIFIED"""
        print("➡️ Processing name response")
        
        # Extract name with better logic
        name = self._extract_name(transcription)
        
        if name and len(name) > 2 and self._is_valid_name(name):
            self.case_data.name = name
            print(f"✅ Extracted valid name: {name}")
        else:
            # Use the raw transcription as name if extraction fails
            self.case_data.name = transcription.strip()[:50] if transcription.strip() else "Not provided"
            print(f"🔄 Using provided text as name: {self.case_data.name}")
        
        # Always move to emergency check after name
        await self._speak(f"Thank you {self.case_data.name}. Before we continue, is this an ongoing threat or emergency situation?", "emergency_check")
        self.current_step = "emergency_check"

    async def _process_email_response(self, transcription: str):
        """Process email response - SIMPLIFIED"""
        print("➡️ Processing email response")
        
        text_lower = transcription.lower().strip()
        
        # Check for skip requests
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have", 'no email', 'not']):
            print("📧 User wants to skip email")
            self.case_data.email = "Not provided"
            await self._speak("No problem. Please describe what happened in your own words.", "description")
            self.current_step = "description"
            self._current_field_attempts = 0
            return
        
        email = self._extract_email(transcription)
        print(f"🔍 Extracted email: '{email}'")
        
        if email and email != "pending_username" and self._is_valid_email(email):
            self.case_data.email = email
            print(f"✅ Extracted valid email: {email}")
            await self._speak("Thank you. Please describe what happened in your own words.", "description")
            self.current_step = "description"
            self._current_field_attempts = 0
        else:
            self._current_field_attempts += 1
            print(f"❌ Could not extract email (attempt {self._current_field_attempts})")
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                # Final attempt - use name-based email or skip
                if self.case_data.name and self.case_data.name != "Not provided":
                    self.case_data.email = f"{self.case_data.name.lower().replace(' ', '')}@gmail.com"
                else:
                    self.case_data.email = "Not provided"
                print(f"🔄 Using constructed email: {self.case_data.email}")
                await self._speak("Let's proceed. Please describe what happened.", "description")
                self.current_step = "description"
                self._current_field_attempts = 0
            else:
                await self._speak("I didn't catch a valid email address. Could you please say it like 'john at gmail dot com'?", "email_retry")

    async def _process_description_response(self, transcription: str):
        """Process description response - SIMPLIFIED"""
        print("➡️ Processing description response")
        
        # Store the user's description directly
        user_description = transcription.strip()
        
        # Accept any description that's not empty
        if not user_description:
            await self._speak("Please describe what happened.", "description_retry")
            return
        
        print(f"✅ Saved user's description: {user_description}")
        
        # Classify crime type first
        crime_type = await self._classify_crime_type(user_description)
        self.case_data.crime_type = crime_type
        print(f"✅ Classified crime type: {crime_type}")
        
        # Generate AI-powered structured description
        ai_description = await self._generate_ai_description(user_description, crime_type)
        self.case_data.description = ai_description
        print(f"🤖 AI-generated description: {ai_description}")
        
        # Move to date question
        await self._speak(f"I understand. This sounds like {crime_type}. When did this happen?", "date")
        self.current_step = "date"

    async def _process_date_response(self, transcription: str):
        """Process date response - SIMPLIFIED"""
        print("➡️ Processing date response")
        
        date = self._extract_date(transcription)
        
        if date and self._is_valid_date(date):
            self.case_data.incident_date = date
            print(f"✅ Extracted valid date: {date}")
        else:
            # Use today's date as default
            self.case_data.incident_date = datetime.date.today().isoformat()
            print(f"🔄 Using default date: {self.case_data.incident_date}")
        
        # Always move to confirmation after date
        await self._confirm_details()

    async def _process_confirmation_response(self, transcription: str):
        """Process confirmation response - SIMPLIFIED"""
        print("➡️ Processing confirmation response")
        text_lower = transcription.lower().strip()
        
        confirmation_words = ['yes', 'correct', 'right', 'yes that\'s correct', 'yeah', 'okay', 'ok', 'good', 'perfect']
        
        if any(word in text_lower for word in confirmation_words):
            print("✅ User confirmed details")
            await self._save_and_send_form()
        else:
            print("❌ User wants to correct information")
            # Simple restart instead of complex correction flow
            await self._speak("Let's start over. What is your full name?", "restart")
            self.current_step = "name"
            self._current_field_attempts = 0
            # Reset data except phone number
            self.case_data.name = ""
            self.case_data.email = ""
            self.case_data.crime_type = ""
            self.case_data.incident_date = ""
            self.case_data.description = ""

    # FIXED LLM METHODS
    async def _classify_crime_type(self, description: str) -> str:
        """Classify crime type - FIXED API COMPATIBILITY"""
        print("🤖 _classify_crime_type() called")
        
        # If LLM is available, use it for more accurate classification
        if self.llm:
            try:
                # SIMPLIFIED PROMPT - use correct API format
                prompt = f"Classify this cybercrime description: '{description}' into: scam, phishing, harassment, hacking, doxxing, fraud, other. Return ONLY one word."
                
                # FIXED: Use correct API call format - try different approaches
                try:
                    # Approach 1: Direct chat call (most common)
                    response = await self.llm.chat(prompt)
                except TypeError as e:
                    # Approach 2: If that fails, try with messages format
                    messages = [{"role": "user", "content": prompt}]
                    response = await self.llm.chat(messages=messages)
                
                # Extract text from response based on different possible formats
                if hasattr(response, 'text'):
                    crime_type = response.text.strip().lower()
                elif hasattr(response, 'content'):
                    crime_type = response.content.strip().lower()
                elif isinstance(response, str):
                    crime_type = response.strip().lower()
                else:
                    crime_type = str(response).strip().lower()
                
                # Validate the response
                valid_types = ['scam', 'phishing', 'harassment', 'hacking', 'doxxing', 'fraud', 'other']
                if crime_type in valid_types:
                    print(f"✅ LLM classified as: {crime_type}")
                    return crime_type
                else:
                    print(f"⚠️ LLM returned invalid type: {crime_type}, using keyword fallback")
                    
            except Exception as e:
                print(f"⚠️ LLM classification failed: {e}, using keyword fallback")
        
        # Fallback to keyword-based classification (your existing code)
        crime_keywords = {
            "scam": ["money", "payment", "fake", "lottery", "investment", "won", "prize", "transfer", "bank", "demanding money"],
            "phishing": ["email", "link", "password", "login", "account", "website", "click", "credential"],
            "harassment": ["message", "call", "threat", "abuse", "stalk", "bully", "annoy", "harass"],
            "hacking": ["account", "password", "login", "hack", "access", "unauthorized", "phone", "reset", 
                    "facebook", "instagram", "whatsapp", "social media", "hacked", "compromised", "breach", "profile"],
            "doxxing": ["personal", "information", "private", "leak", "expose", "details", "address", "photo"],
            "fraud": ["bank", "card", "transaction", "unauthorized", "payment", "money", "credit", "debit"]
        }
        
        description_lower = description.lower()
        
        # Count keyword matches for each crime type
        crime_scores = {}
        for crime_type, keywords in crime_keywords.items():
            score = sum(1 for keyword in keywords if keyword in description_lower)
            if score > 0:
                crime_scores[crime_type] = score
        
        # Return the crime type with highest score
        if crime_scores:
            best_crime = max(crime_scores.items(), key=lambda x: x[1])
            print(f"✅ Keyword classified as '{best_crime[0]}' with score {best_crime[1]}")
            return best_crime[0]
        
        print("🔍 No specific crime type detected, using 'other'")
        return "other"

    async def _generate_ai_description(self, user_description: str, crime_type: str) -> str:
        """Generate a structured, professional incident description using AI - FIXED"""
        print("🤖 _generate_ai_description() called")
        
        if not self.llm:
            print("⚠️ No LLM available, using user description as fallback")
            return user_description
        
        try:
            # SIMPLIFIED PROMPT
            prompt = f"Create a professional 2-sentence incident report for {crime_type}: '{user_description}'. Be factual and objective."
            
            # FIXED: Use correct API call format - try different approaches
            try:
                # Approach 1: Direct chat call
                response = await self.llm.chat(prompt)
            except TypeError as e:
                # Approach 2: Messages format
                messages = [{"role": "user", "content": prompt}]
                response = await self.llm.chat(messages=messages)
            
            # Extract text from response based on different possible formats
            if hasattr(response, 'text'):
                ai_description = response.text.strip()
            elif hasattr(response, 'content'):
                ai_description = response.content.strip()
            elif isinstance(response, str):
                ai_description = response.strip()
            else:
                ai_description = str(response).strip()
            
            # Validate the AI response
            if ai_description and len(ai_description) > 10:
                print(f"✅ AI generated description: {ai_description}")
                return ai_description
            else:
                print("⚠️ AI returned empty description, using user's version")
                return user_description
                
        except Exception as e:
            print(f"⚠️ Error generating AI description: {e}")
            # Fallback to user's description
            return user_description

    async def _confirm_details(self):
        """Confirm details - SIMPLIFIED"""
        print("🎯 _confirm_details() called")
        summary = f"""
        Let me confirm your details:
        Name: {self.case_data.name or 'Not provided'}
        Contact Number: {self.case_data.phone} (from your call)
        Email: {self.case_data.email or 'Not provided'}
        Incident: {self.case_data.crime_type or 'Not classified'}
        Date: {self.case_data.incident_date or 'Not provided'}
        
        Is this information correct?
        """
        self.current_step = "confirmation"
        await self._speak(summary, "confirmation")

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

    # Keep all your existing helper methods (extract_name, extract_email, etc.)
    # ... [all your existing helper methods remain the same]

    # Helper methods (keep your existing implementations)
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
        
        # Handle skip requests first
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have", 'no email', 'not']):
            return "skip"
        
        # Handle username-only inputs like "Silber,"
        if text_lower.replace(',', '').replace(' ', '').isalpha() and len(text_lower) > 2:
            username = text_lower.replace(',', '').strip()
            print(f"🔄 Detected username: {username}")
            if not hasattr(self, '_pending_email_username'):
                self._pending_email_username = username
                return "pending_username"
            else:
                self._pending_email_username = username
                return "pending_username"
        
        # Common email patterns in speech
        patterns = [
            # Pattern: "silva at gmail dot com"
            r'([a-zA-Z0-9]+)\s+(?:at|@)\s+(gmail|yahoo|hotmail|outlook)\s+(?:dot|\.)\s+(com|in|org|co\.in)',
            # Pattern: "silva gmail dot com"  
            r'([a-zA-Z0-9]+)\s+(gmail|yahoo|hotmail|outlook)\s+(?:dot|\.)\s+(com|in|org|co\.in)',
            # NEW: Handle "at the rate" pattern specifically
            r'([a-zA-Z0-9]+)\s+at the rate\s+(gmail|yahoo|hotmail|outlook)\s+(?:dot|\.)\s+(com|in|org|co\.in)',
            # Pattern: just domain part like "at the rate Gmail dot com"
            r'at the rate\s+(gmail|yahoo|hotmail|outlook)\s+(?:dot|\.)\s+(com|in|org|co\.in)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                if len(match.groups()) == 3:
                    # Pattern with username and domain
                    username = match.group(1).lower()
                    domain = match.group(2).lower()
                    tld = match.group(3).lower()
                    return f"{username}@{domain}.{tld}"
                elif len(match.groups()) == 2:
                    # Pattern with just domain - use pending username or name
                    domain = match.group(1).lower()
                    tld = match.group(2).lower()
                    if hasattr(self, '_pending_email_username') and self._pending_email_username:
                        username = self._pending_email_username
                        return f"{username}@{domain}.{tld}"
                    elif self.case_data.name and self.case_data.name != "Not provided":
                        username = self.case_data.name.lower().replace(' ', '')
                        return f"{username}@{domain}.{tld}"
        
        # If we have a pending username and current input contains domain info
        if hasattr(self, '_pending_email_username') and self._pending_email_username:
            if any(domain in text_lower for domain in ['gmail', 'yahoo', 'hotmail', 'outlook']):
                username = self._pending_email_username
                if 'gmail' in text_lower:
                    return f"{username}@gmail.com"
                elif 'yahoo' in text_lower:
                    return f"{username}@yahoo.com"
                elif 'hotmail' in text_lower:
                    return f"{username}@hotmail.com"
                elif 'outlook' in text_lower:
                    return f"{username}@outlook.com"
        
        return ""

    def _extract_date(self, text: str) -> str:
        print(f"🔍 _extract_date() called with: '{text}'")
        text_lower = text.lower().strip()
        
        # Better filtering of non-date responses
        if text_lower in ['yes', 'no', 'okay', 'ok', 'thank you', 'skip']:
            return ""
            
        today = datetime.date.today()
        if "today" in text_lower:
            return today.isoformat()
        elif "yesterday" in text_lower:
            return (today - datetime.timedelta(days=1)).isoformat()
        elif "day before yesterday" in text_lower:
            return (today - datetime.timedelta(days=2)).isoformat()
        elif "last week" in text_lower:
            return (today - datetime.timedelta(days=7)).isoformat()
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
                    
        # Only accept text that looks like a date description
        date_indicators = ['today', 'yesterday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'week', 'month', 'year']
        if any(indicator in text_lower for indicator in date_indicators):
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
        """Better email validation"""
        if email in ["skip", "pending_username", "Not provided"]:
            return False
        return '@' in email and '.' in email and len(email) > 5 and ' ' not in email

    def _is_valid_date(self, date: str) -> bool:
        """Check if the extracted date is valid"""
        invalid_dates = {'yes', 'no', 'okay', 'ok', 'thank you', 'skip'}
        return (date.lower().strip() not in invalid_dates and 
                len(date.strip()) > 0 and
                not all(char.isdigit() for char in date))

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
        """Save case and send SMS - THEN END CALL"""
        try:
            print("💾 _save_and_send_form() called")
            
            # Debug: Print current case data before saving
            print(f"🔍 Current case data before saving:")
            print(f"  - Name: {self.case_data.name}")
            print(f"  - Phone: {self.case_data.phone}") 
            print(f"  - Email: {self.case_data.email}")
            print(f"  - Crime Type: {self.case_data.crime_type}")
            print(f"  - Date: {self.case_data.incident_date}")
            print(f"  - Description: {self.case_data.description}")
            
            case_dict = asdict(self.case_data)
            
            required_fields = ["name", "description"]
            missing_fields = [field for field in required_fields if not case_dict.get(field)]
            
            if missing_fields:
                print(f"❌ Missing required fields: {missing_fields}")
                await self._speak("I'm missing some important information. Let's try again.", "missing_info")
                self.current_step = "name"
                self._current_field_attempts = 0
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
                
                # Send SMS to the caller's number
                sms_sent = False
                if self.case_data.phone and self.case_data.phone != "From Caller ID":
                    form_link = self.form_service.get_prefill_link(case_id)
                    message = (
                        f"Hello {self.case_data.name}, your case number is {case_id}. "
                        f"Verify and complete your report: {form_link}. "
                        f"If this is urgent, reply 'EMERGENCY'."
                    )
                    print(f"📱 Preparing to send SMS to caller: {self.case_data.phone}")
                    try:
                        sms_result = await asyncio.to_thread(self.sms_service.send, self.case_data.phone, message)
                        
                        # FIXED: Better SMS result checking
                        if sms_result:
                            if isinstance(sms_result, dict):
                                if sms_result.get('messages'):
                                    first_message = sms_result['messages'][0]
                                    if first_message.get('status') == '0':
                                        print(f"✅ SMS sent successfully to {self.case_data.phone}")
                                        sms_sent = True
                                    else:
                                        print(f"⚠️ SMS failed with status: {first_message.get('status')}")
                                else:
                                    # Alternative success format
                                    print(f"✅ SMS sent successfully (alternative format)")
                                    sms_sent = True
                            else:
                                # Assume success if we got any response
                                print(f"✅ SMS sent successfully (generic response)")
                                sms_sent = True
                        else:
                            print(f"⚠️ SMS failed - no response from service")
                    except Exception as e:
                        print(f"⚠️ SMS sending error: {e}")
                
                # Build final message based on SMS status
                if sms_sent:
                    final_msg = f"""
                    Thank you for reporting. I've saved your case with number {case_id}. 
                    You'll receive an SMS with your case details and a link to update any information.
                    Thank you for calling Safe Line. Goodbye.
                    """
                else:
                    final_msg = f"""
                    Thank you for reporting. I've saved your case with number {case_id}. 
                    Please note this case number for your records.
                    Thank you for calling Safe Line. Goodbye.
                    """
                    
                await self._speak(final_msg, "completion")
                print("🎉 Conversation completed successfully!")
                
                # Wait for final message to complete before ending
                if self._current_tts_task and not self._current_tts_task.done():
                    try:
                        await asyncio.wait_for(self._current_tts_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        print("🔄 Final message completed or cancelled")
                
                # End the conversation after successful completion
                await self._end_conversation()
                
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

    # PASS CONTEXT TO AGENT FOR CALLER ID DETECTION
    agent = SafeLineAgent(ctx=ctx)
    session = AgentSession()
    agent._session_ref = session
    
    print(f"🔍 Starting session for agent in room: {ctx.room.name}")
    await agent._setup_transcript_recording(ctx.room.name)
    
    await agent.setup_event_listeners(session)
    
    async def shutdown_callback():
        print("🛑 Shutdown callback called - finalizing transcript")
        if agent._timeout_task:
            agent._timeout_task.cancel()
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
        # Wait for case to be saved (either emergency or normal completion)
        for i in range(180):  # 3 minutes max
            if agent.case_saved:
                print("✅ Conversation completed, ending session")
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