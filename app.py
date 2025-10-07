from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from openai import OpenAI
from flask_mail import Mail, Message
from dotenv import load_dotenv
import os
import re

# ---------------- Env & App ----------------
load_dotenv()

app = Flask(__name__)
app.secret_key = "super_secret_key"  # TODO: set from env in production

# --- OpenAI Client ---
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Mail ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 465
app.config['MAIL_USE_TLS'] = False
app.config['MAIL_USE_SSL'] = True
app.config['MAIL_USERNAME'] = 'kylehampton949@gmail.com'
app.config['MAIL_PASSWORD'] = 'lkxx nrmp vqpy yydp'  # TODO: move to .env in prod
app.config['MAIL_DEFAULT_SENDER'] = 'kylehampton949@gmail.com'
mail = Mail(app)

# ---------------- Constants ----------------
FINALIZATION_LEAD = "✅ Got it! Here’s your finalized request:"
CONFIRM_PHRASES = {
    "yes","y","yeah","yep","correct","that's right","ready to submit",
    "looks good","that works","submit","confirm","ok","okay","alright",
    "all good","do it","go ahead","please submit","proceed"
}

# ---------------- Helpers ----------------
def is_confirmation(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(p in t for p in CONFIRM_PHRASES)

def wants_preview(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(k in t for k in [
        "see the request","show the request","show me the request",
        "preview","can i see","finalized request","final request"
    ])

def ensure_history_initialized():
    if "messages" not in session:
        session["messages"] = [
            {
                "role": "system",
                "content": (
                    "You are a Senior Data Analyst for the Charlotte Fire Department Planning Division.\n\n"
                    "Your purpose:\n"
                    "- Turn rough/vague requests from staff into a single, clear, professional “finalized request” that the Planning team can act on without guessing.\n"
                    "- Ask at most ONE short clarification at a time, and ONLY when essential details are missing.\n"
                    "- Never re-ask something the user already answered. Never change topics. Never add requirements the user didn’t mention.\n"
                    "- When the user’s request is sufficiently clear, STOP and present the final version for confirmation.\n\n"
                    "Sufficiently clear = we have WHO/WHAT, METRICS/DATA, and TIME RANGE. OPTIONAL if provided: output type (CSV/report/table/chart) and breakdowns (shift/month/department). Do NOT force nonessential details.\n\n"
                    "STRICT rules:\n"
                    "1) One concise follow-up ONLY when truly essential info is missing.\n"
                    "2) Do NOT repeat questions the user answered.\n"
                    "3) Do NOT ask about systems/data sources/approvals unless the user brings them up.\n"
                    "4) Do NOT drift scope or invent categories; reflect the user’s exact entities/timeframes/outputs.\n"
                    "5) If the user is vague (e.g., “the data I asked for”), paraphrase what you have and ask ONE precise confirmation.\n"
                    "6) As soon as the request is clear, finalize.\n\n"
                    "Finalization format (MUST follow EXACTLY so the UI detects it):\n"
                    "- Start with:  ✅ Got it! Here’s your finalized request:\n"
                    "- NEXT line: ONE sentence wrapped in **bold** that includes WHO/WHAT, METRICS, TIME RANGE, and any user-specified OUTPUT/BREAKDOWNS.\n"
                    "- Then:  Please click Confirm & Submit below to finalize your request.\n"
                    "Final sentence: single sentence; 25–40 words preferred; no emojis/quotes/fillers; NEVER say “past year” or generic ranges—always use the user’s explicit timeframe.\n\n"
                    "Confirmation & stop: If the user confirms (e.g., “yes”, “correct”, “ready to submit”), your ONLY response is:\n"
                    "  Perfect! Please click Confirm & Submit below to finalize your request.\n\n"
                    "Loop prevention: If the user provided a timeframe, do NOT ask again; if metrics were given (e.g., response times, shifts), do NOT ask what columns; if user corrects timeframe, use the LATEST correction.\n\n"
                    "Your mission: Guide → Gather missing essentials (at most one question) → Finalize in the exact format → Stop after confirmation."
                )
            }
        ]

def extract_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)
    return (m.group(1).strip() if m else "").strip().strip('"').strip("'")

def collect_required_terms():
    """
    Naive pass: extract obvious terms we want preserved in the final sentence.
    Looks for station, years, CSV, shift mention, 'include date'.
    """
    terms = set()
    history = session.get("messages", [])
    text = " ".join([m.get("content","") for m in history]).lower()

    # Station N
    stn = re.findall(r"\bstation\s+(\d+)\b", text)
    for s in stn:
        terms.add(f"station {s}")

    # Years (four digits 20xx)
    years = re.findall(r"\b(20[0-4]\d)\b", text)  # up to 2049 just to be safe
    for y in years:
        terms.add(y)

    # Quarters and FY
    qtrs = re.findall(r"\bq[1-4]\s*20[0-4]\d\b", text)
    for q in qtrs:
        terms.add(q)

    fyears = re.findall(r"\bfy\s*20[0-4]\d\b", text)
    for fy in fyears:
        terms.add(fy.replace(" ", ""))

    # CSV mention
    if "csv" in text:
        terms.add("csv")

    # Shift(s)
    if "shift" in text or "shifts" in text:
        terms.add("shift")

    # include date
    if "include date" in text or "include the date" in text or "include date as well" in text:
        terms.add("include date")

    # response time(s)
    if "response time" in text or "response times" in text:
        terms.add("response time")

    return terms

def sentence_has_required_terms(sentence: str, terms: set) -> bool:
    s = sentence.lower()
    for t in terms:
        if t not in s:
            return False
    return True

def build_final_sentence_from_history(required_terms: set = None) -> str:
    """Ask the model to produce ONE finalized sentence only, and (optionally) enforce required terms."""
    ensure_history_initialized()
    base_instruction = (
        "From the entire conversation, produce ONE polished final request sentence that includes WHO/WHAT, METRICS/DATA, TIME RANGE, "
        "and any user-specified OUTPUT/BREAKDOWNS. Use the latest corrections. "
        "Never say 'past year'; always use the explicit timeframe. "
        "Output ONLY that sentence between <final> and </final>. No extra words."
    )
    if required_terms:
        # Tell the model to include exact strings
        needed = "; ".join(sorted(required_terms))
        base_instruction += f" The sentence MUST include these exact terms if present in the chat: {needed}."

    msgs = list(session["messages"]) + [{"role": "system", "content": base_instruction}]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=msgs,
        temperature=0.0
    )
    txt = resp.choices[0].message.content or ""
    final_sentence = extract_tag(txt, "final") or txt.strip()
    return final_sentence.strip().strip('"').strip("'")

def build_email_package_from_history():
    """
    Build <final> (single sentence), <subject>, <body> based on ALL details already given.
    """
    ensure_history_initialized()
    msgs = list(session["messages"]) + [
        {
            "role": "system",
            "content": (
                "Finalize a Planning Division request. Review the ENTIRE conversation; use ALL specific details and the latest corrections. "
                "Do NOT generalize or say 'details later'. Produce three outputs:\n"
                "<final> ONE complete, single-sentence request (no extra text). </final>\n"
                "<subject> concise email subject (<=70 chars). </subject>\n"
                "<body> 3–6 short lines summarizing who/what/when/metrics/output, using the explicit timeframe and entities. </body>\n"
                "No commentary outside those tags."
            )
        }
    ]
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=msgs,
        temperature=0.0
    )
    out = resp.choices[0].message.content or ""
    final_request = extract_tag(out, "final") or out.strip()
    subject = extract_tag(out, "subject") or "New Planning Request"
    body = extract_tag(out, "body") or final_request

    # Clean quotes
    final_request = final_request.strip().strip('"').strip("'")
    subject = subject.strip().strip('"').strip("'")
    body = body.strip().strip('"').strip("'")

    session["final_request"] = final_request
    session["email_subject"] = subject
    session["email_body"] = body

def make_preview_message(final_sentence: str) -> str:
    # Ensure bold wrapping for front-end parsing
    sentence = final_sentence.strip()
    if not (sentence.startswith("**") and sentence.endswith("**")):
        sentence = f"**{sentence}**"
    return f"{FINALIZATION_LEAD}\n\n{sentence}\n\nPlease click Confirm & Submit below to finalize your request."

# ---------------- Routes ----------------

@app.route("/")
def index():
    return render_template("chat.html")

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "").strip()
    if not user_message:
        return jsonify({"reply": "Could you share your request?"})

    # Confirmation → build email package and return preview (no more questions)
    if is_confirmation(user_message):
        build_email_package_from_history()
        final_sentence = session.get("final_request")
        if not final_sentence:
            # As fallback, rebuild strictly
            req_terms = collect_required_terms()
            final_sentence = build_final_sentence_from_history(req_terms)
            session["final_request"] = final_sentence
        return jsonify({"reply": make_preview_message(final_sentence)})

    # User explicitly asks to preview → force finalization preview server-side
    if wants_preview(user_message):
        req_terms = collect_required_terms()
        final_sentence = build_final_sentence_from_history(req_terms)
        session["final_request"] = final_sentence
        return jsonify({"reply": make_preview_message(final_sentence)})

    # Normal conversational turn
    ensure_history_initialized()
    session["messages"].append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=session["messages"],
        temperature=0.2  # predictable clarifications
    )
    ai_message = response.choices[0].message.content
    session["messages"].append({"role": "assistant", "content": ai_message})

    # If the model tries to finalize on its own, we override with a validated final sentence
    if FINALIZATION_LEAD.lower() in ai_message.lower():
        # Build/validate server-side final sentence with required terms
        req_terms = collect_required_terms()
        final_sentence = build_final_sentence_from_history(req_terms)
        # If still missing required terms, force again without mercy
        if not sentence_has_required_terms(final_sentence, req_terms):
            # Second attempt: reiterate required terms even harder
            final_sentence = build_final_sentence_from_history(req_terms)
        session["final_request"] = final_sentence
        # Replace model's output with our corrected preview
        ai_message = make_preview_message(final_sentence)

    return jsonify({"reply": ai_message})

@app.route("/reset", methods=["POST"])
def reset():
    session.clear()
    return jsonify({"reply": "Chat history has been reset. You can start a new request."})

@app.route("/submit", methods=["POST"])
def submit():
    requester_email = request.form.get("email")

    # Build package if missing (e.g., user jumps straight to submit)
    if not session.get("final_request") or not session.get("email_subject") or not session.get("email_body"):
        build_email_package_from_history()

    final_request = session.get("final_request", "No finalized request found.")
    email_subject = session.get("email_subject", "New Planning Request")
    email_body = session.get("email_body", final_request)

    # Email to Planning
    msg_to_you = Message(email_subject, recipients=["kylehampton949@gmail.com"])
    msg_to_you.body = f"{email_body}\n\nSubmitted by: {requester_email or 'Unknown'}"
    mail.send(msg_to_you)

    # Email to Requester
    if requester_email:
        msg_to_user = Message("Your Request Was Submitted", recipients=[requester_email])
        msg_to_user.body = (
            "Thank you for your submission. Here’s the finalized request we recorded:\n\n"
            f"{final_request}\n\nWe’ll review it shortly."
        )
        mail.send(msg_to_user)

    session.clear()
    return redirect(url_for("confirmation"))

@app.route("/confirmation")
def confirmation():
    return render_template("confirmation.html")

if __name__ == "__main__":
    app.run(debug=True)
