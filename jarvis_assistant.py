import io
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import wave
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import sounddevice as sd
from vosk import Model as VoskModel, KaldiRecognizer
from openwakeword.model import Model as WakeModel

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================
# J.A.R.V.I.S. CONFIGURATION
# ============================================================

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "qwen3:1.7b"

VOICE_URL = "http://127.0.0.1:8765/speak"
STAGE_URL = "http://127.0.0.1:8765/stage"

VOSK_MODEL_PATH = "models/vosk-model-small-en-in-0.4"
SAMPLE_RATE = 16000
BLOCK_SIZE = 1280
MICROPHONE_DEVICE = 0
WAKE_THRESHOLD = 0.5
COMMAND_TIMEOUT = 12

GOOGLE_CREDENTIALS_FILE = "credentials.json"
GOOGLE_TOKEN_FILE = "token.json"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# JARVIS uses your Mac's configured local timezone for calendar dates.
# Change this only if your Google Calendar's primary timezone is different.
LOCAL_TIMEZONE = ZoneInfo("Asia/Kolkata")

# City names and common aliases -> IANA timezone identifiers.
CITY_TIMEZONES = {
    "india": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "london": "Europe/London", "uk": "Europe/London",
    "united kingdom": "Europe/London",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "rome": "Europe/Rome", "italy": "Europe/Rome",
    "madrid": "Europe/Madrid", "spain": "Europe/Madrid",
    "amsterdam": "Europe/Amsterdam", "netherlands": "Europe/Amsterdam",
    "zurich": "Europe/Zurich", "switzerland": "Europe/Zurich",
    "lisbon": "Europe/Lisbon", "portugal": "Europe/Lisbon",
    "new york": "America/New_York", "toronto": "America/Toronto",
    "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "chicago": "America/Chicago", "usa": "America/New_York",
    "united states": "America/New_York",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "seoul": "Asia/Seoul", "south korea": "Asia/Seoul",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "auckland": "Pacific/Auckland", "new zealand": "Pacific/Auckland",
    "cairo": "Africa/Cairo", "south africa": "Africa/Johannesburg",
    "johannesburg": "Africa/Johannesburg", "moscow": "Europe/Moscow",
    "istanbul": "Europe/Istanbul", "bangkok": "Asia/Bangkok",
}

conversation = [{
    "role": "system",
    "content": (
        "You are J.A.R.V.I.S., a sophisticated personal AI assistant. "
        "Speak in polished, natural British English. Be calm, intelligent, "
        "courteous and concise. Address the user as Sir occasionally. "
        "Avoid Markdown and emojis in spoken answers. Never invent appointments, "
        "emails, weather, news or personal information. Live information is "
        "handled by dedicated tools; if a tool is unavailable, say so honestly."
    )
}]

_google_services = None
vosk_model = None
wake_model = None


# ============================================================
# SMALL HTTP HELPERS
# ============================================================

def fetch_json(url, timeout=15):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "JARVIS-Local-Assistant/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url, timeout=15):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "JARVIS-Local-Assistant/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


# ============================================================
# WORLD CLOCK
# ============================================================

def get_world_time(city):
    requested = city.lower().strip(" .,?!")
    timezone_name = CITY_TIMEZONES.get(requested)

    if not timezone_name:
        # Also accept a direct IANA timezone such as Europe/Paris.
        if "/" in requested:
            timezone_name = requested
        else:
            return (
                f"Sir, I don't recognise the city {city}. "
                "Try a city such as London, Paris, Berlin, New York, Tokyo or Chennai."
            )

    try:
        local_now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        return f"Sir, I couldn't find a valid timezone for {city}."

    return (
        f"The current local time in {city.title()} is "
        f"{local_now.strftime('%I:%M %p').lstrip('0')}. "
        f"Today is {local_now.strftime('%A, %d %B %Y')}."
    )


def handle_time_command(text):
    lower = text.lower()
    if not any(word in lower for word in ("time", "clock", "date")):
        return None

    # Match longest aliases first, so "new york" is not reduced to "york".
    for city in sorted(CITY_TIMEZONES, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(city) + r"(?![a-z])", lower):
            return get_world_time(city)

    # Direct IANA timezone request.
    match = re.search(r"\b([A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)\b", text)
    if match:
        return get_world_time(match.group(1))

    if any(x in lower for x in ("my time", "local time", "here")):
        now = datetime.now().astimezone()
        return (
            f"Your local time is {now.strftime('%I:%M %p').lstrip('0')}. "
            f"Today is {now.strftime('%A, %d %B %Y')}."
        )

    return (
        "Certainly, Sir. Which city would you like the time for? "
        "For example, London, Paris, New York, Tokyo or Chennai."
    )


# ============================================================
# LIVE WEATHER - Open-Meteo, no API key required
# ============================================================

WEATHER_CITIES = {
    "chennai": (13.0827, 80.2707),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "bengaluru": (12.9716, 77.5946),
    "bangalore": (12.9716, 77.5946),
    "hyderabad": (17.3850, 78.4867),
    "kolkata": (22.5726, 88.3639),
    "london": (51.5072, -0.1276),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "rome": (41.9028, 12.4964),
    "madrid": (40.4168, -3.7038),
    "amsterdam": (52.3676, 4.9041),
    "zurich": (47.3769, 8.5417),
    "new york": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "toronto": (43.6532, -79.3832),
    "tokyo": (35.6762, 139.6503),
    "dubai": (25.2048, 55.2708),
    "singapore": (1.3521, 103.8198),
    "sydney": (-33.8688, 151.2093),
}

WEATHER_CODES = {
    0: "clear skies", 1: "mostly clear", 2: "partly cloudy",
    3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow",
    80: "rain showers", 81: "moderate rain showers", 82: "heavy rain showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms",
}


def get_weather(city, forecast=False):
    city_key = city.lower().strip(" .,?!")
    coordinates = WEATHER_CITIES.get(city_key)

    if not coordinates:
        # Geocode other city names through Open-Meteo.
        query = urllib.parse.urlencode({"name": city, "count": 1, "language": "en", "format": "json"})
        geo = fetch_json("https://geocoding-api.open-meteo.com/v1/search?" + query)
        results = geo.get("results", [])
        if not results:
            return f"Sir, I couldn't find a location called {city}."
        place = results[0]
        coordinates = (place["latitude"], place["longitude"])
        city = place.get("name", city)

    lat, lon = coordinates
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 3 if forecast else 1,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = fetch_json(url)
    current = data.get("current", {})
    code = current.get("weather_code")
    description = WEATHER_CODES.get(code, "current conditions")
    temp = current.get("temperature_2m", "unknown")
    feels = current.get("apparent_temperature", temp)
    humidity = current.get("relative_humidity_2m", "unknown")
    wind = current.get("wind_speed_10m", "unknown")

    answer = (
        f"Current weather in {city}: {description}. "
        f"Temperature {temp} degrees Celsius, feels like {feels}. "
        f"Humidity {humidity} percent and wind speed {wind} kilometres per hour."
    )

    if forecast:
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        codes = daily.get("weather_code", [])
        rain = daily.get("precipitation_probability_max", [])
        forecast_lines = []
        for i, day in enumerate(dates):
            day_name = datetime.fromisoformat(day).strftime("%A")
            condition = WEATHER_CODES.get(codes[i], "variable conditions")
            chance = rain[i] if i < len(rain) else "unknown"
            forecast_lines.append(
                f"{day_name}: {condition}, high {highs[i]} and low {lows[i]} degrees, "
                f"rain chance {chance} percent"
            )
        answer += " Forecast: " + ". ".join(forecast_lines) + "."
    return answer


def handle_weather_command(text):
    lower = text.lower()
    if not any(x in lower for x in ("weather", "forecast", "temperature", "rain", "raining")):
        return None

    city = None
    for known_city in sorted(WEATHER_CITIES, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(known_city) + r"(?![a-z])", lower):
            city = known_city
            break

    if city is None:
        # If no place is specified, use Chennai as the configured default.
        city = "chennai"

    try:
        return get_weather(city, forecast=any(x in lower for x in ("forecast", "next few days", "next 3 days")))
    except Exception as error:
        print(f"[WEATHER ERROR] {error}", flush=True)
        return "I'm unable to retrieve live weather right now, Sir. Please try again shortly."


# ============================================================
# LIVE INTERNATIONAL NEWS - public RSS feeds, no API key
# ============================================================

NEWS_FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
]


def get_live_news(max_items=6):
    headlines = []
    errors = []

    for source, url in NEWS_FEEDS:
        try:
            root = ET.fromstring(fetch_text(url))
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                if title:
                    headlines.append((source, title))
                if len(headlines) >= max_items:
                    break
        except Exception as error:
            errors.append(f"{source}: {error}")
        if len(headlines) >= max_items:
            break

    if not headlines:
        print("[NEWS ERROR]", "; ".join(errors), flush=True)
        return "I'm unable to retrieve live news headlines at the moment, Sir."

    return "Here are the latest international headlines. " + ". ".join(
        f"From {source}: {title}" for source, title in headlines[:max_items]
    )


def handle_news_command(text):
    lower = text.lower()
    if not any(x in lower for x in ("news", "headlines", "world events", "latest updates")):
        return None
    try:
        return get_live_news()
    except Exception as error:
        print(f"[NEWS ERROR] {error}", flush=True)
        return "I'm unable to retrieve live news at the moment, Sir."


# ============================================================
# GOOGLE AUTHENTICATION
# ============================================================

def get_google_services():
    global _google_services
    if _google_services:
        return _google_services

    import os
    if not os.path.isfile(GOOGLE_CREDENTIALS_FILE):
        raise FileNotFoundError(
            "I cannot find credentials.json in the JARVIS project folder, Sir."
        )

    creds = None
    if os.path.isfile(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN_FILE, GOOGLE_SCOPES
        )

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES
        )
        print(
            "\n[JARVIS] Google sign-in is opening in your browser. "
            "Approve read-only Calendar and Gmail access.",
            flush=True,
        )
        creds = flow.run_local_server(
            host="localhost",
            port=0,
            authorization_prompt_message="Authorize JARVIS in your browser: {url}",
            success_message="JARVIS Google access is connected. You may close this tab.",
            open_browser=True,
        )

    with open(GOOGLE_TOKEN_FILE, "w", encoding="utf-8") as token_file:
        token_file.write(creds.to_json())
    try:
        os.chmod(GOOGLE_TOKEN_FILE, 0o600)
    except OSError:
        pass

    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    _google_services = (calendar, gmail)
    print("[JARVIS] Google Calendar and Gmail connected.", flush=True)
    return _google_services


# ============================================================
# GOOGLE CALENDAR
# ============================================================

def _format_event_time(event):
    start = event.get("start", {})
    value = start.get("dateTime") or start.get("date")
    if not value:
        return "time not specified"
    if "T" not in value:
        return "all day"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(LOCAL_TIMEZONE).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return "time not specified"


def get_calendar_briefing(day_offset=0):
    calendar, _ = get_google_services()
    now = datetime.now(LOCAL_TIMEZONE)
    target = (now + timedelta(days=day_offset)).date()
    start = datetime.combine(target, datetime.min.time(), tzinfo=LOCAL_TIMEZONE)
    end = start + timedelta(days=1)

    result = calendar.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=40,
    ).execute()

    events = result.get("items", [])
    label = "today" if day_offset == 0 else "tomorrow"
    if not events:
        return f"You have no calendar events scheduled for {label}, Sir."

    lines = []
    for event in events:
        line = f"{_format_event_time(event)}: {event.get('summary', 'Untitled event')}"
        if event.get("location"):
            line += f", at {event['location']}"
        lines.append(line)
    return f"You have {len(events)} event{'s' if len(events) != 1 else ''} {label}. " + ". ".join(lines)


def get_week_briefing():
    calendar, _ = get_google_services()
    now = datetime.now(LOCAL_TIMEZONE)
    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=LOCAL_TIMEZONE)
    end = start + timedelta(days=7)

    result = calendar.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=60,
    ).execute()

    events = result.get("items", [])
    if not events:
        return "You have no events scheduled in the next seven days, Sir."

    lines = []
    for event in events:
        value = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date", "")
        try:
            if "T" in value:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TIMEZONE)
                when = dt.strftime("%A, %I:%M %p").replace(" 0", " ")
            else:
                when = datetime.fromisoformat(value).strftime("%A") + ", all day"
        except ValueError:
            when = "time not specified"
        lines.append(f"{when}: {event.get('summary', 'Untitled event')}")
    return f"I found {len(events)} events over the next seven days. " + ". ".join(lines)


# ============================================================
# GMAIL - read-only summaries
# ============================================================

def get_email_briefing(important_only=True, max_results=5):
    _, gmail = get_google_services()
    query = "in:inbox newer_than:7d"
    if important_only:
        query += " is:important"

    response = gmail.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = response.get("messages", [])
    if not messages:
        qualifier = "important " if important_only else ""
        return f"I couldn't find any {qualifier}inbox emails from the last seven days, Sir."

    summaries = []
    for item in messages:
        message = gmail.users().messages().get(
            userId="me",
            id=item["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {
            h["name"].lower(): h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        sender = headers.get("from", "Unknown sender")
        subject = headers.get("subject", "No subject")
        snippet = message.get("snippet", "").strip()
        summaries.append(f"From {sender}. Subject: {subject}. {snippet}")

    intro = "Here are your latest important emails. " if important_only else "Here are your latest emails. "
    return intro + " ".join(summaries)


# ============================================================
# LIVE COMMAND ROUTER
# ============================================================

def handle_live_command(command):
    text = command.lower().strip()

    # Google calendar and email requests must bypass the local LLM.
    calendar_terms = ("calendar", "schedule", "meeting", "meetings", "appointment", "appointments", "events")
    email_terms = ("gmail", "email", "emails", "inbox", "messages")

    try:
        if any(term in text for term in calendar_terms):
            if any(term in text for term in ("this week", "next seven days", "next 7 days", "weekly")):
                return get_week_briefing()
            if "tomorrow" in text:
                return get_calendar_briefing(1)
            if any(term in text for term in ("today", "schedule", "calendar", "meeting", "appointment", "events")):
                return get_calendar_briefing(0)

        if any(term in text for term in email_terms):
            all_mail = any(term in text for term in ("all emails", "all messages", "latest emails", "latest messages"))
            return get_email_briefing(important_only=not all_mail)

        answer = handle_time_command(text)
        if answer is not None:
            return answer

        answer = handle_weather_command(text)
        if answer is not None:
            return answer

        answer = handle_news_command(text)
        if answer is not None:
            return answer

    except FileNotFoundError as error:
        return str(error)
    except HttpError as error:
        print(f"[GOOGLE API ERROR] {error}", flush=True)
        return "I'm having trouble accessing Google, Sir. Please check the Google connection."
    except Exception as error:
        print(f"[LIVE COMMAND ERROR] {error}", flush=True)
        return "I couldn't retrieve that live information just now, Sir. Please try again."

    return None


# ============================================================
# DASHBOARD STATUS
# ============================================================

def set_stage(stage):
    try:
        payload = json.dumps({"stage": stage}).encode("utf-8")
        request = urllib.request.Request(
            STAGE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2):
            pass
        print(f"[DASHBOARD] {stage}", flush=True)
    except Exception as error:
        print(f"[DASHBOARD] Status update unavailable: {error}", flush=True)


# ============================================================
# SPEECH MODELS
# ============================================================

def initialise_models():
    global vosk_model, wake_model
    print("\nLoading JARVIS systems...", flush=True)
    print("Loading speech recognition...", flush=True)
    vosk_model = VoskModel(VOSK_MODEL_PATH)
    print("Loading wake-word detector...", flush=True)
    wake_model = WakeModel(
        wakeword_models=["hey_jarvis"],
        inference_framework="onnx",
    )
    print("JARVIS systems loaded.", flush=True)


def listen_for_command(stream):
    set_stage("HEARING")
    print("\nJARVIS: Listening for your command...", flush=True)
    recognizer = KaldiRecognizer(vosk_model, SAMPLE_RATE)
    start_time = time.monotonic()

    while time.monotonic() - start_time < COMMAND_TIMEOUT:
        audio, _overflowed = stream.read(BLOCK_SIZE)
        audio = np.asarray(audio[:, 0], dtype=np.int16)
        if recognizer.AcceptWaveform(audio.tobytes()):
            text = json.loads(recognizer.Result()).get("text", "").strip()
            if text:
                print(f"\nYOU: {text}", flush=True)
                return text

    text = json.loads(recognizer.FinalResult()).get("text", "").strip()
    if text:
        print(f"\nYOU: {text}", flush=True)
    return text


# ============================================================
# LOCAL QWEN3
# ============================================================

def ask_jarvis(question):
    global conversation
    set_stage("THINKING")
    conversation.append({"role": "user", "content": question})
    payload = {
        "model": OLLAMA_MODEL,
        "messages": conversation,
        "stream": False,
    }
    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read())
        answer = data.get("message", {}).get(
            "content", "I am sorry, Sir. I could not formulate a response."
        ).strip()
        conversation.append({"role": "assistant", "content": answer})
        if len(conversation) > 11:
            conversation = [conversation[0]] + conversation[-10:]
        return answer
    except Exception as error:
        print(f"[QWEN ERROR] {error}", flush=True)
        return "I am having difficulty connecting to my local intelligence system, Sir."


# ============================================================
# DANIEL VOICE OUTPUT
# ============================================================

def speak(text):
    if not text:
        return
    set_stage("SPEAKING")
    print(f"\nJARVIS: {text}", flush=True)
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        VOICE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            audio_bytes = response.read()
        with wave.open(io.BytesIO(audio_bytes), "rb") as audio_file:
            sample_rate = audio_file.getframerate()
            channels = audio_file.getnchannels()
            audio_data = audio_file.readframes(audio_file.getnframes())
        samples = np.frombuffer(audio_data, dtype=np.int16)
        if channels > 1:
            samples = samples.reshape(-1, channels)
        sd.play(samples, sample_rate)
        sd.wait()
    except Exception as error:
        print(f"[VOICE PLAYBACK ERROR] {error}", flush=True)


# ============================================================
# SELF-TEST
# ============================================================

def run_self_test():
    print("\n========== JARVIS SELF-TEST ==========", flush=True)
    checks = []

    checks.append(("World clock", "Europe/London" in CITY_TIMEZONES.values()))
    try:
        london = get_world_time("london")
        checks.append(("World clock response", "London" in london))
    except Exception:
        checks.append(("World clock response", False))

    checks.append(("Weather module", callable(get_weather)))
    checks.append(("News module", callable(get_live_news)))
    checks.append(("Google Calendar module", callable(get_calendar_briefing)))
    checks.append(("Gmail module", callable(get_email_briefing)))

    for name, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name}", flush=True)

    print("Note: live network and Google account checks run when you ask JARVIS.", flush=True)
    print("======================================\n", flush=True)


# ============================================================
# MAIN WAKE-WORD LOOP
# ============================================================

def main():
    initialise_models()
    run_self_test()

    print("\n===================================")
    print("       J.A.R.V.I.S. ONLINE")
    print("===================================")
    print("Say: Hey JARVIS")
    print("Try: time in London")
    print("Try: weather in Chennai")
    print("Try: international news")
    print("Try: what's on my calendar tomorrow")
    print("Try: read my important emails")
    print("Press Control + C to stop.\n", flush=True)

    set_stage("LISTENING")

    while True:
        command = ""
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                device=MICROPHONE_DEVICE,
            ) as stream:
                while True:
                    set_stage("LISTENING")
                    audio, _overflowed = stream.read(BLOCK_SIZE)
                    audio = np.asarray(audio[:, 0], dtype=np.int16)
                    scores = wake_model.predict(audio)
                    score = float(scores.get("hey_jarvis", 0.0))
                    if score > WAKE_THRESHOLD:
                        print("\n[JARVIS] Wake phrase detected!", flush=True)
                        wake_model.reset()
                        command = listen_for_command(stream)
                        break

            # Microphone is closed before network calls and speech playback.
            if command:
                live_answer = handle_live_command(command)
                answer = live_answer if live_answer is not None else ask_jarvis(command)
                speak(answer)
            else:
                print("I didn't catch that, Sir.", flush=True)

            set_stage("LISTENING")

        except KeyboardInterrupt:
            set_stage("STANDBY")
            print("\nJARVIS shutting down. Goodbye, Sir.", flush=True)
            break
        except Exception as error:
            print(f"\n[JARVIS SYSTEM ERROR] {error}", flush=True)
            set_stage("LISTENING")
            time.sleep(2)
            
if __name__ == "__main__":
    main()
