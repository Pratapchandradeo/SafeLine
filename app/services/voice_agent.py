# app/services/voice_agent.py
import os
import re
import datetime
import uuid
import asyncio
import sys
from pathlib import Path
from typing import Optional, Set
from dataclasses import dataclass
from dotenv import load_dotenv

# NOTE: avoid inserting project root at index 0 (it can cause shadowing of packages).
# If you truly need project-root imports, append instead of insert:
# sys.path.append(str(Path(__file__).parent.parent.parent))

from load_context import load_context

load_dotenv()

# Environment variable checks
CEREBRAS_KEY = os.getenv("CEREBRAS_API_KEY")
DEEPGRAM_KEY = os.getenv("DEEPGRAM_API_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
VONAGE_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_SECRET = os.getenv("VONAGE_API_SECRET")

if not CEREBRAS_KEY:
    print("⚠️  Warning: CEREBRAS_API_KEY not found in .env")
if not DEEPGRAM_KEY:
    print("⚠️  Warning: DEEPGRAM_API_KEY not found in .env (will use dummy audio for local tests)")
if not VONAGE_KEY or not VONAGE_SECRET:
    print("⚠️  Warning: VONAGE_API_KEY/SECRET not found (SMS will log to console)")

# LiveKit / plugin imports (matching working SalesAgent)
from livekit import agents
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions
from livekit.plugins import openai, deepgram

from app.services.db_service import DBService
from app.services.sms_service import SMService
from app.services.form_service import FormService

# -------------------------
# Dummy STT / TTS fallbacks
# -------------------------
class DummySTT:
    async def transcribe_stream(self, *args, **kwargs):
        return {"text": "This is a test incident report."}

    async def transcribe_file(self, path):
        return {"text": "Report a scam that happened today."}


class DummyTTS:
    sample_rate = 24000
    channels = 1

    def __init__(self):
        self.capabilities = type("Capabilities", (), {"streaming": True})()

    async def speak(self, text: str):
        print("🤖 (TTS) ->", text)
        return None

    class _StreamContext:
        def __init__(self, tts, text=None, **kwargs):
            self.tts = tts
            self.text = text or ""
            self.sample_rate = tts.sample_rate
            self.channels = tts.channels

        async def __aenter__(self):
            return self._generator()

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def _generator(self):
            chunk_duration_ms = 20
            bytes_per_ms = int(self.sample_rate * self.channels * 2 / 1000)
            chunk_size = bytes_per_ms * chunk_duration_ms
            silent_chunk = b"\x00" * chunk_size
            
            # Try different approaches for AudioEvent
            try:
                # Approach 1: Try importing from livekit.agents
                from livekit.agents import AudioFrame
                for _ in range(50):
                    await asyncio.sleep(0)
                    yield AudioFrame(
                        data=silent_chunk,
                        sample_rate=self.sample_rate,
                        num_channels=self.channels,
                        samples_per_channel=chunk_size // (self.channels * 2)
                    )
            except ImportError:
                try:
                    # Approach 2: Try the voice.audio import with different name
                    from livekit.agents.voice import AudioChunk
                    for _ in range(50):
                        await asyncio.sleep(0)
                        yield AudioChunk(
                            data=silent_chunk,
                            sample_rate=self.sample_rate,
                            channels=self.channels
                        )
                except ImportError:
                    # Approach 3: Use the base class approach
                    for _ in range(50):
                        await asyncio.sleep(0)
                        # Create a simple object with the expected attributes
                        yield type('AudioEvent', (), {
                            'data': silent_chunk,
                            'sample_rate': self.sample_rate,
                            'channels': self.channels
                        })()

    def stream(self, text=None, **kwargs):
        return self._StreamContext(self, text, **kwargs)

    def synthesize_stream(self, text=None, **kwargs):
        return self.stream(text, **kwargs)


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
# SafeLine Agent (Agent-based)
# -------------------------
class SafeLineAgent(Agent):
    def __init__(self):
        context = load_context() if Path("context").exists() else ""
        print(f"📄 Loaded context: {len(context)} characters")

        # Initialize LLM
        try:
            if CEREBRAS_KEY:
                llm = openai.LLM.with_cerebras(model="llama3.1-8b")
            elif OPENAI_KEY:
                llm = openai.LLM(model="gpt-3.5-turbo")
            else:
                llm = None
                print("⚠️  No LLM available; using dummy responses.")
        except Exception as e:
            print("⚠️  LLM initialization failed:", e)
            llm = None

        # Initialize STT and TTS
        try:
            stt = deepgram.STT(model="nova-2", language="en") if DEEPGRAM_KEY else DummySTT()
        except Exception as e:
            print("⚠️  deepgram.STT() raised:", e)
            stt = DummySTT()

        try:
            if OPENAI_KEY:
                tts = openai.TTS(model="tts-1")
            else:
                tts = DummyTTS()
        except Exception as e:
            print("⚠️  TTS initialization failed:", e)
            tts = DummyTTS()

        # Strict instructions / flow
        instructions = f"""
You are a professional, empathetic Safe Line cybercrime helpline assistant. Keep responses concise...
CONTEXT:
{context}
"""

        # Initialize base Agent with the chosen components
        super().__init__(instructions=instructions, stt=stt, llm=llm, tts=tts)

        # conversation state
        self.case_data = CaseData()
        self.transcript = ""
        self.case_saved = False
        self._bg_tasks: Set[asyncio.Task] = set()

        # services
        self.db_service = DBService()
        self.sms_service = SMService()
        self.form_service = FormService()

    async def on_enter(self):
        # called when session starts; greet user
        await self.session.generate_reply(user_input="Hello, this is the Safe Line cybercrime helpline assistant. How can I help you today?")

    async def on_user_transcription(self, transcription: str):
        """Optional hook if framework calls it; update transcript and handle triage."""
        # preserve transcript
        self.transcript += f"User: {transcription}\n"
        self.case_data.transcript = self.transcript

        # Emergency triage (same logic you used)
        emergency_keywords = ["bank", "money", "transfer", "ongoing", "threatening", "ransom", "house"]
        if any(kw in transcription.lower() for kw in emergency_keywords) or "emergency" in transcription.lower():
            self.case_data.is_emergency = True
            await self.session.generate_reply(user_input="This sounds urgent. Routing to a human operator immediately. Stay safe.")
            try:
                case_id = await asyncio.to_thread(self.db_service.create_case, self.case_data.__dict__)
                self.case_saved = True
                print("💥 Emergency case logged:", case_id)
            except Exception as e:
                print("⚠️ Emergency save failed:", e)
            return

        # Ask LLM to craft reply (delegate to session)
        try:
            # you can pass the transcription as user input so the AgentSession uses the llm
            await self.session.generate_reply(user_input=transcription)
        except Exception as e:
            print("⚠️ generate_reply failed:", e)
            await self.session.generate_reply(user_input="Sorry, I had trouble processing that. Can you repeat?")

        # Extract fields locally (same code as before)
        self._extract_fields(transcription)

    def _extract_fields(self, transcription: str):
        # name
        if "name" in transcription.lower() and not self.case_data.name:
            match = re.search(r'name[:\s]*([A-Za-z\s]+)', transcription, re.I)
            if match:
                self.case_data.name = match.group(1).strip()

        # phone
        if not self.case_data.phone:
            match = re.search(r'\b\d{10}\b', transcription)
            if match:
                self.case_data.phone = match.group(0)
                if len(self.case_data.phone) != 10:
                    self.case_data.phone = ""

        # email
        if not self.case_data.email:
            match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', transcription)
            if match:
                self.case_data.email = match.group(0)

        # date
        if not self.case_data.incident_date:
            if "today" in transcription.lower():
                self.case_data.incident_date = datetime.date.today().isoformat()
            elif "yesterday" in transcription.lower():
                self.case_data.incident_date = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            else:
                match = re.search(r'(\d{4}-\d{2}-\d{2})', transcription)
                if match:
                    self.case_data.incident_date = match.group(1)

        # consent
        if "consent" in transcription.lower() and "yes" in transcription.lower():
            self.case_data.consent_recorded = True

        # crime type
        if not self.case_data.crime_type:
            for t in ["scam", "phishing", "harassment", "hacking", "doxxing", "fraud"]:
                if t in transcription.lower():
                    self.case_data.crime_type = t
                    break

        # description fallback
        if not self.case_data.description and len(transcription.strip()) > 20:
            self.case_data.description = transcription.strip()

        # amount extraction
        if self.case_data.amount_lost is None:
            m = re.search(r'([₹$]?\s?\d{1,3}(?:[,.\d]{0,})\b)', transcription)
            if m:
                s = m.group(1)
                digits = re.sub(r'[^\d.]', '', s)
                try:
                    self.case_data.amount_lost = float(digits.replace(',', ''))
                except Exception:
                    pass

        # evidence URL
        if not self.case_data.evidence:
            match = re.search(r'(https?://[^\s]+)', transcription)
            if match:
                self.case_data.evidence = match.group(1)

        # completion check (same background flow)
        required = ["name", "phone", "email", "crime_type", "incident_date", "description"]
        if all(getattr(self.case_data, f) for f in required if f != "crime_type"):
            task = asyncio.create_task(self._complete_flow())
            self._bg_tasks.add(task)
            def _on_done(t: asyncio.Task):
                self._bg_tasks.discard(t)
                try:
                    exc = t.exception()
                    if exc:
                        print("⚠️ Background _complete_flow task failed:", exc)
                except asyncio.CancelledError:
                    print("ℹ️ Background task cancelled.")
            task.add_done_callback(_on_done)

    async def _complete_flow(self):
        try:
            summary = f"I have your details: {self.case_data.name}, {self.case_data.phone}. Correct?"
            await self.session.generate_reply(user_input=summary)

            if not self.case_saved:
                case_id = await asyncio.to_thread(self.db_service.create_case, self.case_data.__dict__)
                self.case_saved = True
            else:
                case_id = "CR-ALREADY-SAVED"

            form_link = self.form_service.get_prefill_link(case_id)

            message = (
                f"Hello {self.case_data.name}, your case number is {case_id}. "
                f"Verify and complete your report: {form_link}. If urgent, reply 'EMERGENCY'."
            )
            # send SMS in thread to avoid blocking
            await asyncio.to_thread(self.sms_service.send, self.case_data.phone, message)

            await self.session.generate_reply(user_input=f"Sent. Your case number is {case_id}. Check your message. Thank you.")
        except Exception as e:
            print("⚠️ _complete_flow failed:", e)

# -------------------------
# Entrypoint for LiveKit Worker
# -------------------------
async def entrypoint(ctx: JobContext):
    await ctx.connect()
    agent = SafeLineAgent()
    session = AgentSession()
    await session.start(room=ctx.room, agent=agent)

if __name__ == "__main__":
    agents.cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))