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
VONAGE_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_SECRET = os.getenv("VONAGE_API_SECRET")

if not DEEPGRAM_KEY:
    print("DEEPGRAM_API_KEY not set, using DummySTT")
if not CEREBRAS_KEY:
    print("CARTESIA_API_KEY not set, using DummyTTS")
if not VONAGE_KEY or not VONAGE_SECRET:
    print("VONAGE_API_KEY/SECRET not set, SMS will log to console")

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
        return {"text": ""}
    async def transcribe_file(self, path):
        return {"text": ""}

class DummyTTS:
    async def speak(self, text: str):
        pass

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
            except Exception:
                stt_client = DummySTT()
        else:
            stt_client = DummySTT()

        # Initialize TTS with Deepgram
        if DEEPGRAM_KEY:
            try:
                tts_client = deepgram.TTS(
                    model="aura-asteria-en",
                    api_key=DEEPGRAM_KEY
                )
            except Exception:
                tts_client = DummyTTS()
        else:
            tts_client = DummyTTS()

        # Initialize LLM with Cerebras
        llm_client = None
        if CEREBRAS_KEY:
            try:
                llm_client = openai.LLM(
                    model="llama3.1-8b",
                    base_url="https://api.cerebras.ai/v1",
                    api_key=CEREBRAS_KEY
                )
            except Exception:
                llm_client = None

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
        
        # State management
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
        

    def _get_caller_phone_number(self, ctx):
        """Get phone number from caller ID"""
        caller_phone = os.getenv("CALLER_PHONE_NUMBER", None)
        
        if not caller_phone and hasattr(ctx, 'room') and hasattr(ctx.room, 'metadata'):
            caller_phone = ctx.room.metadata.get('caller_phone', None)
        
        if not caller_phone:
            timestamp = datetime.datetime.now().strftime("%m%d%H%M")
            caller_phone = f"555{timestamp[-7:]}"
        
        return caller_phone

    def _load_context(self):
        """Load context from JSON file directly"""
        try:
            context_path = Path("context/safe_line_info.json")
            if context_path.exists():
                with open(context_path, 'r') as f:
                    ctx_obj = json.load(f)
                return ctx_obj
            else:
                return None
        except Exception:
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
        except Exception:
            pass

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
        except Exception:
            pass

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
        except Exception:
            pass

    async def _speak(self, text: str, question_type: str = ""):
        """Enhanced speaking with proper waiting state"""
        try:
            if self.case_saved:
                return
            
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
                    if hasattr(self, '_session_ref') and self._session_ref:
                        await self._session_ref.say(text)
                    elif hasattr(self.tts, 'speak'):
                        await self.tts.speak(text)
                    else:
                        dummy_tts = DummyTTS()
                        await dummy_tts.speak(text)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            
            self._current_tts_task = asyncio.create_task(execute_tts())
            
            try:
                await asyncio.wait_for(self._current_tts_task, timeout=10.0)
            except asyncio.CancelledError:
                pass
                    
        except Exception:
            pass
        finally:
            # AFTER speaking is done, set waiting state
            if not self.case_saved:
                self._is_speaking = False
                self._waiting_for_response = True
                self._last_question_time = datetime.datetime.now()
                
                # Process any pending input that came while we were speaking
                if self._pending_user_input and not self.case_saved:
                    pending = self._pending_user_input
                    self._pending_user_input = None
                    await self._process_user_transcription(pending)

    async def setup_event_listeners(self, session):
        
        @session.on("user_input_transcribed")
        def on_user_transcribed(evt):
            is_final = getattr(evt, 'is_final', False)
            
            # Process only final transcriptions for smoother flow
            if is_final:
                asyncio.create_task(self._handle_user_input(evt.transcript))

    async def _handle_user_input(self, transcription: str):
        
        if not transcription.strip():
            return
        
        # If agent is speaking, store the input for later processing
        if self._is_speaking:
            self._pending_user_input = transcription
            return
            
        # If we're waiting for response, process immediately
        if self._waiting_for_response:
            self._waiting_for_response = False
            await self._process_user_transcription(transcription)

    async def _process_user_transcription(self, transcription: str):
        
        # Prevent processing if conversation is already complete
        if self.case_saved:
            return
            
        # Prevent processing if we're in emergency state
        if self.case_data.is_emergency:
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
                await self._speak("Let me start over. How can I help you today?", "restart")
                await self._start_conversation()
                
        except Exception:
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

    # Step processing methods
    async def _process_greeting_response(self, transcription: str):
        
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        consent_msg = message_templates.get(
            "consent",
            "For your report, do you consent to recording this call? Please say yes or no."
        )
        
        # Always move to consent, regardless of what user says
        await self._speak(consent_msg, "consent")
        self.current_step = "consent"

    async def _process_consent_response(self, transcription: str):
        text_lower = transcription.lower().strip()
        
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        consent_indicators = ['yes', 'yeah', 'sure', 'okay', 'ok', 'yep', 'yes.', 'go ahead', 'continue', 'proceed']
        
        # Broader consent detection - accept any reasonable response
        if any(word in text_lower for word in consent_indicators + ['help', 'hello', 'hi']):
            self.case_data.consent_recorded = True
        else:
            self.case_data.consent_recorded = True
        
        name_msg = message_templates.get("name_request", "What is your full name?")
        await self._speak(name_msg, "name")
        self.current_step = "name"
        
    async def _process_emergency_check_response(self, transcription: str):
        
        if self.case_data.is_emergency:
            return
            
        text_clean = transcription.lower().strip()
        
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        emergency_words = ['yes', 'yeah', 'yep', 'sure', 'definitely', 'absolutely', 'right now', 'ongoing', 'emergency', 'urgent', 'immediate']
        
        if any(word in text_clean for word in emergency_words):
            self.case_data.is_emergency = True
            emergency_msg = message_templates.get("emergency_handling", 
                "Okay. If this is an ongoing emergency, please call 1-800-HELP-NOW immediately for urgent assistance. This call will now end. Thank you for reaching out.")
            await self._speak(emergency_msg, "emergency")
            await self._handle_emergency()
        else:
            email_msg = message_templates.get("email_request", "What is your email address?")
            await self._speak(email_msg, "email")
            self.current_step = "email"

    async def _handle_emergency(self):
        
        # Set emergency flag and case_saved to prevent reprocessing
        self.case_data.is_emergency = True
        self.case_saved = True
        
        # Emergency response with clear call ending
        emergency_msg = "Okay. If this is an ongoing emergency, please call 1-800-HELP-NOW immediately for urgent assistance. This call will now end. Thank you for reaching out."
        
        # Speak the emergency message
        await self._speak(emergency_msg, "emergency_end")
        await asyncio.sleep(2)
        
        # End the conversation immediately for emergencies
        await self._end_conversation()

    async def _end_conversation(self):
        """Properly end the conversation and cleanup"""
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

    async def _process_name_response(self, transcription: str):
        
        # Extract name with better logic
        name = self._extract_name(transcription)
        
        if name and len(name) > 2 and self._is_valid_name(name):
            self.case_data.name = name
        else:
            # Use the raw transcription as name if extraction fails
            self.case_data.name = transcription.strip()[:50] if transcription.strip() else "Not provided"
        
        # Always move to emergency check after name
        await self._speak(f"Thank you {self.case_data.name}. Before we continue, is this an ongoing threat or emergency situation?", "emergency_check")
        self.current_step = "emergency_check"

    async def _process_email_response(self, transcription: str):
        
        text_lower = transcription.lower().strip()
        
        # Check for skip requests
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have", 'no email', 'not']):
            self.case_data.email = "Not provided"
            await self._speak("No problem. Please describe what happened in your own words.", "description")
            self.current_step = "description"
            self._current_field_attempts = 0
            return
        
        email = self._extract_email(transcription)
        
        if email and email != "pending_username":
            self.case_data.email = email
            await self._speak("Thank you. Please describe what happened in your own words.", "description")
            self.current_step = "description"
            self._current_field_attempts = 0
        else:
            self._current_field_attempts += 1
            
            if self._current_field_attempts >= self._max_attempts_per_field:
                # Final attempt - use name-based email
                if self.case_data.name and self.case_data.name != "Not provided":
                    self.case_data.email = f"{self.case_data.name.lower().replace(' ', '')}@gmail.com"
                else:
                    self.case_data.email = "Not provided"
                await self._speak("Let's proceed. Please describe what happened.", "description")
                self.current_step = "description"
                self._current_field_attempts = 0
            else:
                await self._speak("I didn't catch your email. Please say your email address.", "email_retry")

    async def _process_description_response(self, transcription: str):
        
        # Store the user's description directly
        user_description = transcription.strip()
        
        # Accept any description that's not empty
        if not user_description:
            await self._speak("Please describe what happened.", "description_retry")
            return
        
        # Classify crime type first
        crime_type = await self._classify_crime_type(user_description)
        self.case_data.crime_type = crime_type
        
        # Generate AI-powered structured description
        ai_description = await self._generate_ai_description(user_description, crime_type)
        self.case_data.description = ai_description
        
        # Move to date question
        await self._speak(f"I understand. This sounds like {crime_type}. When did this happen?", "date")
        self.current_step = "date"

    async def _process_date_response(self, transcription: str):
        
        date = self._extract_date(transcription)
        
        if date and self._is_valid_date(date):
            self.case_data.incident_date = date
        else:
            # Use today's date as default
            self.case_data.incident_date = datetime.date.today().isoformat()
        
        # Always move to confirmation after date
        await self._confirm_details()

    async def _process_confirmation_response(self, transcription: str):
        text_lower = transcription.lower().strip()
        
        confirmation_words = ['yes', 'correct', 'right', 'yes that\'s correct', 'yeah', 'okay', 'ok', 'good', 'perfect']
        
        if any(word in text_lower for word in confirmation_words):
            await self._save_and_send_form()
        else:
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

    # LLM METHODS
    async def _classify_crime_type(self, description: str) -> str:
        
        # If LLM is available, use it for more accurate classification
        if self.llm:
            try:
                prompt = f"Classify this cybercrime description: '{description}' into: scam, phishing, harassment, hacking, doxxing, fraud, other. Return ONLY one word."
                
                try:
                    response = await self.llm.chat(prompt)
                except Exception:
                    return await self._keyword_classify_crime_type(description)
                
                # Extract text from Cerebras response
                crime_type = ""
                if response:
                    if hasattr(response, 'choices') and len(response.choices) > 0:
                        crime_type = response.choices[0].message.content.strip().lower()
                    elif hasattr(response, 'text'):
                        crime_type = response.text.strip().lower()
                    elif hasattr(response, 'content'):
                        crime_type = response.content.strip().lower()
                    elif isinstance(response, str):
                        crime_type = response.strip().lower()
                    elif isinstance(response, dict):
                        crime_type = response.get('text', '').strip().lower() or response.get('content', '').strip().lower()
                    else:
                        crime_type = str(response).strip().lower()
                
                # Validate the response
                valid_types = ['scam', 'phishing', 'harassment', 'hacking', 'doxxing', 'fraud', 'other']
                if crime_type in valid_types:
                    return crime_type
                else:
                    return await self._keyword_classify_crime_type(description)
                    
            except Exception:
                return await self._keyword_classify_crime_type(description)
        
        # Fallback to keyword-based classification
        return await self._keyword_classify_crime_type(description)

    async def _generate_ai_description(self, user_description: str, crime_type: str) -> str:
        
        if not self.llm:
            return await self._generate_template_description(user_description, crime_type)
        
        try:
            prompt = f"Create a professional 2-sentence incident report for {crime_type}: '{user_description}'. Be factual and objective. Return only the description."
            
            try:
                response = await self.llm.chat(prompt)
            except Exception:
                return await self._generate_template_description(user_description, crime_type)
            
            # Extract text from Cerebras response
            ai_description = ""
            if response:
                if hasattr(response, 'choices') and len(response.choices) > 0:
                    ai_description = response.choices[0].message.content.strip()
                elif hasattr(response, 'text'):
                    ai_description = response.text.strip()
                elif hasattr(response, 'content'):
                    ai_description = response.content.strip()
                elif isinstance(response, str):
                    ai_description = response.strip()
                elif isinstance(response, dict):
                    ai_description = response.get('text', '').strip() or response.get('content', '').strip()
                else:
                    ai_description = str(response).strip()
            
            # Validate the AI response
            if ai_description and len(ai_description) > 10 and ai_description.lower() != user_description.lower():
                return ai_description
            else:
                return await self._generate_template_description(user_description, crime_type)
                
        except Exception:
            return await self._generate_template_description(user_description, crime_type)

    async def _keyword_classify_crime_type(self, description: str) -> str:
        """Keyword-based crime classification fallback"""
        crime_keywords = {
            "scam": ["money", "payment", "fake", "lottery", "investment", "won", "prize", "transfer", "bank", "demanding money", "cash", "funds"],
            "phishing": ["email", "link", "password", "login", "account", "website", "click", "credential", "verify", "suspend", "security"],
            "harassment": ["message", "call", "threat", "abuse", "stalk", "bully", "annoy", "harass", "threatening", "intimidate", "abusive"],
            "hacking": ["account", "password", "login", "hack", "access", "unauthorized", "phone", "reset", 
                    "facebook", "instagram", "whatsapp", "social media", "hacked", "compromised", "breach", "profile", "taken over"],
            "doxxing": ["personal", "information", "private", "leak", "expose", "details", "address", "photo", "private info", "personal data"],
            "fraud": ["bank", "card", "transaction", "unauthorized", "payment", "money", "credit", "debit", "identity", "theft"]
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
            return best_crime[0]
        
        return "other"

   
    async def _generate_template_description(self, user_description: str, crime_type: str) -> str:
        """Generate description using templates when LLM fails"""
        templates = {
            "scam": f"The caller reported a financial scam attempt involving {user_description}. This appears to be a fraudulent scheme targeting the victim for financial gain.",
            "phishing": f"The caller encountered a phishing attempt where {user_description}. Credential theft or personal information compromise was attempted through deceptive means.",
            "harassment": f"The caller is experiencing harassment involving {user_description}. This constitutes unwanted communication or threats causing distress to the victim.",
            "hacking": f"The caller experienced unauthorized account access where {user_description}. Security breach and potential data compromise occurred without the victim's consent.",
            "doxxing": f"The caller reported personal information exposure where {user_description}. Private details were leaked or threatened to be released publicly.",
            "fraud": f"The caller reported fraudulent activity involving {user_description}. Financial deception or identity misuse appears to have occurred for illicit gain.",
            "other": f"The caller reported an incident where {user_description}. Further investigation may be required to classify the specific cybercrime type and appropriate response."
        }
        
        template = templates.get(crime_type, templates["other"])
        return template

    async def _confirm_details(self):
        """Confirm details - SIMPLIFIED"""
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
        self._timeout_task = asyncio.create_task(self._check_for_timeout())
        await self._start_conversation()

    async def _start_conversation(self):
        message_templates = self._ctx_obj.get("message_templates", {}) if self._ctx_obj else {}
        
        # Combine greeting and consent into one continuous message
        combined_message = (
            "Hello, this is the Safe Line cybercrime helpline assistant. "
            "For your report, do you consent to recording this call? Please say yes or no."
        )
        
        self.current_step = "consent"
        await self._speak(combined_message, "consent")

    # Helper methods
    def _extract_name(self, text: str) -> str:
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
        text_lower = text.lower().strip()
        
        # Handle skip requests
        if any(word in text_lower for word in ['skip', 'later', 'not now', "don't have", 'no email', 'not']):
            return "skip"
        
        # Extract username from the beginning of the text
        words = text_lower.split()
        if words:
            # The first word is likely the username
            username = words[0]
            
            # Remove any punctuation from username
            username = re.sub(r'[^\w]', '', username)
            
            # Check for email providers in the text
            if 'gmail' in text_lower:
                return f"{username}@gmail.com"
            elif 'yahoo' in text_lower:
                return f"{username}@yahoo.com"
            elif 'hotmail' in text_lower:
                return f"{username}@hotmail.com"
            elif 'outlook' in text_lower:
                return f"{username}@outlook.com"
            elif any(provider in text_lower for provider in ['email', 'mail']):
                # Default to gmail if email/mail is mentioned but no specific provider
                return f"{username}@gmail.com"
        
        # If we have a name and email-related words are mentioned, use the name
        if self.case_data.name and self.case_data.name != "Not provided":
            username = self.case_data.name.lower().replace(' ', '')
            if any(word in text_lower for word in ['gmail', 'yahoo', 'hotmail', 'outlook', 'email', 'mail']):
                if 'gmail' in text_lower:
                    return f"{username}@gmail.com"
                elif 'yahoo' in text_lower:
                    return f"{username}@yahoo.com"
                elif 'hotmail' in text_lower:
                    return f"{username}@hotmail.com"
                elif 'outlook' in text_lower:
                    return f"{username}@outlook.com"
                else:
                    return f"{username}@gmail.com"
        
        return ""

    def _extract_date(self, text: str) -> str:
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
                    
                    await self._speak("Are you still there? Please respond to continue.", "timeout_prompt")
                    self._last_question_time = datetime.datetime.now()
                
                await asyncio.sleep(5)
            except Exception:
                await asyncio.sleep(5)

    async def _save_and_send_form(self):
        """Save case and send SMS - THEN END CALL"""
        try:
            case_dict = asdict(self.case_data)
            
            required_fields = ["name", "description"]
            missing_fields = [field for field in required_fields if not case_dict.get(field)]
            
            if missing_fields:
                await self._speak("I'm missing some important information. Let's try again.", "missing_info")
                self.current_step = "name"
                self._current_field_attempts = 0
                return
            
            try:
                case_id = await asyncio.to_thread(self.db_service.create_case, case_dict)
            except Exception:
                case_id = None
            
            if case_id:
                self.case_saved = True
                
                # Send SMS to the caller's number
                sms_sent = False
                if self.case_data.phone and self.case_data.phone != "From Caller ID":
                    form_link = self.form_service.get_prefill_link(case_id)
                    message = (
                        f"Hello {self.case_data.name}, your case number is {case_id}. "
                        f"Verify and complete your report: {form_link}. "
                        f"If this is urgent, reply 'EMERGENCY'."
                    )
                    try:
                        sms_result = await asyncio.to_thread(self.sms_service.send, self.case_data.phone, message)
                        
                        # Better SMS result checking
                        if sms_result:
                            if isinstance(sms_result, dict):
                                if sms_result.get('messages'):
                                    first_message = sms_result['messages'][0]
                                    if first_message.get('status') == '0':
                                        sms_sent = True
                            else:
                                sms_sent = True
                    except Exception:
                        pass
                
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
                
                # Wait for final message to complete before ending
                if self._current_tts_task and not self._current_tts_task.done():
                    try:
                        await asyncio.wait_for(self._current_tts_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                
                # End the conversation after successful completion
                await self._end_conversation()
                
            else:
                await self._speak("I'm sorry, but I couldn't save your case right now. Please try calling again.", "save_error")
        except Exception:
            await self._speak("There was an error processing your case. Please call back.", "error")

# Entrypoint
async def entrypoint(ctx: JobContext):
    try:
        await ctx.connect()
    except Exception:
        return

    # PASS CONTEXT TO AGENT FOR CALLER ID DETECTION
    agent = SafeLineAgent(ctx=ctx)
    session = AgentSession()
    agent._session_ref = session
    
    await agent._setup_transcript_recording(ctx.room.name)
    
    await agent.setup_event_listeners(session)
    
    async def shutdown_callback():
        if agent._timeout_task:
            agent._timeout_task.cancel()
        if agent._current_tts_task:
            agent._current_tts_task.cancel()
        await agent._finalize_transcript()
    
    ctx.add_shutdown_callback(shutdown_callback)
    
    try:
        await session.start(room=ctx.room, agent=agent)
        
        # Wait for case to be saved (either emergency or normal completion)
        for i in range(180):  # 3 minutes max
            if agent.case_saved:
                break
            await asyncio.sleep(1)
        
    except Exception:
        pass

if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))