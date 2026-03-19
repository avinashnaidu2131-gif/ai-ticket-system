import os
import json
import joblib
import requests

# ── Try loading the trained ML model ──────────────────────────────────────────
MODEL_PATH = "models/classifier.pkl"
VECTORIZER_PATH = "models/vectorizer.pkl"

_model = None
_vectorizer = None

def _load_model():
    global _model, _vectorizer
    if os.path.exists(MODEL_PATH) and os.path.exists(VECTORIZER_PATH):
        _model = joblib.load(MODEL_PATH)
        _vectorizer = joblib.load(VECTORIZER_PATH)


_load_model()


# ── Priority & tag rules (applied after category is known) ────────────────────
PRIORITY_MAP = {
    "Billing":        ("High",   ["payment", "refund", "invoice", "charge"]),
    "Authentication": ("Medium", ["login", "password", "otp", "2fa", "access"]),
    "Technical":      ("High",   ["crash", "error", "down", "broken", "bug"]),
    "Manual Review":  ("Low",    ["general"]),
}


def _rule_tags(text: str, keyword_hints: list[str]) -> str:
    found = [kw for kw in keyword_hints if kw in text.lower()]
    return ",".join(found) if found else "general"


# ── Claude AI classifier (called when ML confidence is low) ───────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def _claude_classify(text: str) -> tuple:
    """
    Ask Claude to classify the ticket and return structured JSON.
    Falls back to Manual Review if the API call fails.
    """
    if not ANTHROPIC_API_KEY:
        return None

    prompt = f"""You are a support ticket classifier. Given the ticket description below,
return ONLY a JSON object (no markdown, no explanation) with these keys:
  category   : one of ["Billing","Authentication","Technical","Feature Request","General"]
  priority   : one of ["Low","Medium","High","Critical"]
  tags       : comma-separated keywords (max 5)
  explanation: one sentence reason for classification
  confidence : float 0-1

Ticket: {text}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=10,
        )
        raw = resp.json()["content"][0]["text"].strip()
        # Strip possible ```json fences
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return (
            data.get("category", "General"),
            float(data.get("confidence", 0.85)),
            data.get("priority", "Medium"),
            data.get("tags", "general"),
            data.get("explanation", "Classified by Claude AI."),
        )
    except Exception:
        return None


# ── Public predict function ───────────────────────────────────────────────────
def predict_ticket(text: str) -> tuple:
    """
    Returns (category, confidence, priority, tags, explanation).
    Pipeline:
      1. Try ML model  → use if confidence >= 0.60
      2. Try Claude AI → use if API key is set
      3. Fall back to rule-based keyword matching
    """
    category = None
    confidence = 0.0

    # 1. ML model
    if _model and _vectorizer:
        try:
            vec = _vectorizer.transform([text])
            proba = _model.predict_proba(vec)[0]
            max_prob = float(max(proba))
            if max_prob >= 0.60:
                category = _model.predict(vec)[0]
                confidence = max_prob
        except Exception:
            pass

    # 2. Claude AI fallback
    if category is None or confidence < 0.60:
        claude_result = _claude_classify(text)
        if claude_result:
            return claude_result

    # 3. Rule-based keyword fallback
    if category is None:
        lower = text.lower()
        if any(k in lower for k in ["payment", "refund", "invoice", "billing", "charge"]):
            category, confidence = "Billing", 0.78
        elif any(k in lower for k in ["login", "password", "otp", "sign in", "auth"]):
            category, confidence = "Authentication", 0.73
        elif any(k in lower for k in ["crash", "error", "bug", "broken", "down", "fail"]):
            category, confidence = "Technical", 0.70
        elif any(k in lower for k in ["feature", "request", "suggestion", "improve"]):
            category, confidence = "Feature Request", 0.65
        else:
            category, confidence = "Manual Review", 0.30

    priority, hint_keywords = PRIORITY_MAP.get(category, ("Medium", []))
    tags = _rule_tags(text, hint_keywords)
    explanation = f"Classified as {category} by ML model (confidence: {confidence:.0%})."

    return category, round(confidence, 4), priority, tags, explanation