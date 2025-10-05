# Safe Line - AI-Powered Cybercrime Reporting System

<div align="center">

![Safe Line](https://img.shields.io/badge/Safe-Line-blue?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python)
![Flask](https://img.shields.io/badge/Flask-2.3%2B-lightgrey?style=for-the-badge&logo=flask)

**Revolutionizing Cybercrime Reporting with AI-Powered Voice Assistance**

</div>

## 🚨 Problem Statement

Cybercrime reporting in India currently faces significant challenges:
- **Long wait times** - Callers queue for extended periods
- **Manual processes** - Officers manually collect and transcribe information
- **Inefficient workflows** - Multiple steps required for case registration
- **Delayed response** - Critical emergencies may not get immediate attention

## 💡 Our Solution

Safe Line transforms cybercrime reporting by integrating an AI agent into the initial call handling process. Our system provides:

- **Instant response** - AI agent immediately answers calls
- **Structured data collection** - Guided conversation captures all essential information
- **Emergency prioritization** - Immediate human escalation for urgent cases
- **Automated case management** - Seamless form completion and tracking

## 🏗️ Architecture Overview

### Dual-Path Processing

```mermaid
graph TD
    A[Incoming Call] --> B{AI Agent Triage}
    B -->|Emergency| C[Human Operator]
    B -->|Non-Emergency| D[Structured Data Collection]
    C --> E[Immediate Assistance]
    D --> F[Case Generation]
    F --> G[SMS with Form Link]
    G --> H[Web Form Completion]
```

### Emergency Handling
For urgent situations (ongoing threats, financial theft in progress, imminent harm), the system:
- **Immediately routes** to human operators
- **Skips form process** for faster response
- **Provides direct emergency number**: `1-800-HELP-NOW`
- **Logs as high priority** for follow-up

## 🛠️ Tech Stack

### Core Technologies
- **Python 3.8+** - Backend and AI logic
- **Flask** - Web framework for form handling
- **LiveKit** - Real-time voice communication
- **Deepgram** - Speech-to-text transcription
- **Cerebras** - LLM for crime classification
- **PostgreSQL** - Case data storage
- **Vonage** - SMS notifications

### Windows 11 Compatibility
- All components tested and optimized for Windows 11
- Environment variable configuration support
- Service integration with Windows ecosystem

## 📋 Key Features

### 🤖 AI Agent Capabilities
- **Natural conversation** - Human-like interaction
- **Crime classification** - Automatic incident categorization
- **Data validation** - Real-time field verification
- **Multi-language support** - English with regional language expansion capability

### 🗄️ Data Management
- **Structured storage** - PostgreSQL with optimized schema
- **Transcript recording** - Full conversation logging
- **Case tracking** - Unique registration IDs (CR-YYYYMMDD-XXXX)
- **Audit trails** - Complete activity logging

### 📱 User Experience
- **SMS integration** - Instant case number delivery
- **Web forms** - Pre-filled for easy completion
- **Progress tracking** - Real-time case status
- **Mobile-friendly** - Responsive design

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- PostgreSQL
- Windows 11
- API keys for Deepgram, Cerebras, Vonage

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/your-org/safe-line.git
cd safe-line
```

2. **Set up environment**
```bash
cp .env .env
# Configure your API keys in .env
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Initialize database**
```bash
python -c "from app.services.database import init_db; init_db()"
```

5. **Run the application**
```bash
# Start the voice agent
python -m app.services.voice_agent console

# Start the web server (in separate terminal)
python -m app.main
```

## 📞 Usage Flow

### Standard Case Handling
1. **Call initiation** - User dials helpline number
2. **AI greeting** - Immediate response with consent request  
3. **Data collection** - Structured conversation captures:
   - Personal details (name, contact)
   - Incident description
   - Crime classification
   - Timestamp and context
4. **Case generation** - Unique ID created automatically
5. **SMS delivery** - Case number and form link sent to user
6. **Form completion** - User verifies and submits details

### Emergency Protocol
1. **Emergency detection** - Keywords trigger immediate escalation
2. **Human connection** - Direct transfer to operator
3. **Priority logging** - Case marked as high urgency
4. **Follow-up assurance** - Continuous monitoring

### Caller ID Integration
- **Development/Demo**: Phone number set via `CALLER_PHONE_NUMBER` environment variable
- **Production**: Automatic caller ID detection from telephony provider metadata
- **Fallback**: System-generated unique identifiers when caller ID unavailable

## 🔧 Configuration

### Environment Variables
```env
# Database
POSTGRES_URI=postgresql://user:pass@localhost/safeline

# SMS Service
VONAGE_API_KEY=your_vonage_key
VONAGE_API_SECRET=your_vonage_secret
TEST_PHONE=your_test_number  # For development/demo

# Form & Application
BASE_URL=http://localhost:5000
SECRET_KEY=your_secret_key

# LiveKit Voice Platform
LIVEKIT_URL=wss://ai-voice-kfmwtoc8.livekit.cloud
LIVEKIT_API_KEY=your_livekit_key
LIVEKIT_API_SECRET=your_livekit_secret

# AI Services
CEREBRAS_API_KEY=your_cerebras_key
DEEPGRAM_API_KEY=your_deepgram_key

# Caller Identification
CALLER_PHONE_NUMBER=optional_caller_number  # Development only - Production uses auto-detection
```

### Phone Number Handling
- **Development**: Use `TEST_PHONE` and `CALLER_PHONE_NUMBER` for testing
- **Production**: Automatic detection from telephony provider metadata
- **Validation**: 10-digit Indian format with country code normalization
- **Fallback**: System-generated IDs ensure no case is lost

## 📊 Analytics & Monitoring

### Key Metrics
- Call volume and response times
- Emergency case frequency
- Form completion rates
- Crime type distribution
- User satisfaction indicators

### Dashboard Features
- Real-time case tracking
- Performance analytics
- Emergency response monitoring
- System health status


## 🎯 Impact

### Expected Outcomes
- **80% reduction** in initial response time
- **60% decrease** in manual data entry
- **Improved accuracy** in case classification
- **Enhanced user satisfaction** with streamlined process

### Future Roadmap
- Multi-language support
- Mobile application
- Advanced analytics dashboard
- Integration with law enforcement systems

---

<div align="center">

**Making cybercrime reporting faster, smarter, and more efficient**

[Report Issue](https://github.com/your-org/safe-line/issues) • [Request Feature](https://github.com/your-org/safe-line/issues)

</div>
