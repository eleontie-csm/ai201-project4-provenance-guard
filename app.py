import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from groq import Groq


load_dotenv()

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

DATA_DIR = Path("data")
LOG_PATH = DATA_DIR / "audit_log.jsonl"
CONTENT_PATH = DATA_DIR / "content_index.json"

MIN_TEXT_LENGTH = 150
MAX_LOG_ENTRIES = 50


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        LOG_PATH.touch()
    if not CONTENT_PATH.exists():
        CONTENT_PATH.write_text("{}", encoding="utf-8")


def clip_01(value: float) -> float:
    return max(0.0, min(1.0, value))


def attribution_from_score(score: float) -> str:
    if score >= 0.75:
        return "likely_ai"
    if score < 0.40:
        return "likely_human"
    return "uncertain"


def label_from_confidence(score: float) -> str:
    if score >= 0.75:
        return (
            "This content is likely AI-generated (high confidence). We may be wrong, "
            "but both language and writing-pattern signals strongly indicate AI assistance."
        )
    if score < 0.40:
        return (
            "This content is likely human-written (high confidence). We may be wrong, "
            "but available signals do not strongly indicate AI generation."
        )
    return (
        "This content could not be confidently attributed. Signals are mixed, "
        "so this result is uncertain and should be interpreted with caution."
    )


def parse_groq_json(raw_text: str) -> Dict[str, Any]:
    try:
        payload = json.loads(raw_text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and start < end:
        maybe_json = raw_text[start : end + 1]
        try:
            payload = json.loads(maybe_json)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

    return {}


def llm_signal_score(text: str) -> Dict[str, Any]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return {
            "llm_score": 0.5,
            "raw_reasoning": "GROQ_API_KEY not configured; used neutral fallback score.",
            "normalization_notes": "fallback_no_api_key",
        }

    client = Groq(api_key=api_key)
    prompt = (
        "You are scoring text attribution risk. Return only JSON with keys: "
        "ai_likelihood (number 0 to 1), reasoning (short string). "
        "Higher ai_likelihood means text is more likely AI-generated."
    )

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
        )

        content = completion.choices[0].message.content or "{}"
        payload = parse_groq_json(content)

        raw_score = payload.get("ai_likelihood", 0.5)
        try:
            score = clip_01(float(raw_score))
        except (TypeError, ValueError):
            score = 0.5

        reasoning = payload.get("reasoning", "No reasoning returned.")
        return {
            "llm_score": score,
            "raw_reasoning": reasoning,
            "normalization_notes": "parsed_groq_json",
        }
    except Exception as exc:
        return {
            "llm_score": 0.5,
            "raw_reasoning": f"Groq request failed: {str(exc)}",
            "normalization_notes": "fallback_api_error",
        }


def split_sentences(text: str) -> List[str]:
    sentences = [chunk.strip() for chunk in re.split(r"(?<=[.!?])\s+", text.strip()) if chunk.strip()]
    if not sentences:
        return [text.strip()]
    return sentences


def stylometric_signal_score(text: str) -> Dict[str, Any]:
    sentences = split_sentences(text)
    words = re.findall(r"[A-Za-z']+", text.lower())
    text_length = max(1, len(text))

    sentence_lengths = [len(re.findall(r"[A-Za-z']+", sentence)) for sentence in sentences]
    mean_len = sum(sentence_lengths) / max(1, len(sentence_lengths))
    variance = sum((value - mean_len) ** 2 for value in sentence_lengths) / max(1, len(sentence_lengths))

    unique_words = len(set(words))
    type_token_ratio = unique_words / max(1, len(words))

    punctuation_count = len(re.findall(r"[,:;!?-]", text))
    punctuation_density = punctuation_count / text_length

    # Lower sentence variance trends AI-like; higher variance trends human-like.
    if variance < 8:
        variance_ai = 0.85
    elif variance < 20:
        variance_ai = 0.65
    elif variance < 45:
        variance_ai = 0.45
    else:
        variance_ai = 0.25

    # Lower lexical diversity can indicate more repetitive/generated text.
    if type_token_ratio < 0.32:
        ttr_ai = 0.8
    elif type_token_ratio < 0.42:
        ttr_ai = 0.6
    elif type_token_ratio < 0.55:
        ttr_ai = 0.45
    else:
        ttr_ai = 0.3

    # Extremely low punctuation density often correlates with flatter generated rhythm.
    if punctuation_density < 0.01:
        punct_ai = 0.75
    elif punctuation_density < 0.02:
        punct_ai = 0.6
    elif punctuation_density < 0.04:
        punct_ai = 0.45
    else:
        punct_ai = 0.35

    score = clip_01((variance_ai + ttr_ai + punct_ai) / 3.0)
    return {
        "stylometric_score": score,
        "features": {
            "sentence_length_variance": round(variance, 4),
            "type_token_ratio": round(type_token_ratio, 4),
            "punctuation_density": round(punctuation_density, 4),
        },
        "normalization_notes": "rule_band_mapping_v1",
    }


def combine_confidence(llm_score: float, stylometric_score: float) -> float:
    return clip_01((0.5 * llm_score) + (0.5 * stylometric_score))


def append_audit_event(event: Dict[str, Any]) -> None:
    ensure_storage()
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def load_content_index() -> Dict[str, Any]:
    ensure_storage()
    try:
        raw = CONTENT_PATH.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_content_index(index: Dict[str, Any]) -> None:
    ensure_storage()
    CONTENT_PATH.write_text(json.dumps(index, ensure_ascii=True, indent=2), encoding="utf-8")


def read_recent_audit_events(limit: int = MAX_LOG_ENTRIES) -> List[Dict[str, Any]]:
    ensure_storage()
    with LOG_PATH.open("r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle.readlines() if line.strip()]

    selected = lines[-limit:]
    events: List[Dict[str, Any]] = []
    for line in selected:
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                events.append(parsed)
        except json.JSONDecodeError:
            continue
    return events


def read_all_audit_events() -> List[Dict[str, Any]]:
    ensure_storage()
    events: List[Dict[str, Any]] = []
    with LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    events.append(parsed)
            except json.JSONDecodeError:
                continue
    return events


def build_analytics_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    classification_events = [e for e in events if e.get("event_type") == "classification"]
    appeal_events = [e for e in events if e.get("event_type") == "appeal"]

    by_attribution = {
        "likely_ai": 0,
        "uncertain": 0,
        "likely_human": 0,
    }
    confidence_values: List[float] = []

    for event in classification_events:
        attribution = event.get("attribution")
        if attribution in by_attribution:
            by_attribution[attribution] += 1

        confidence = event.get("confidence")
        try:
            confidence_values.append(float(confidence))
        except (TypeError, ValueError):
            continue

    total_classifications = len(classification_events)
    total_appeals = len(appeal_events)
    appeal_rate = (total_appeals / total_classifications) if total_classifications else 0.0
    avg_confidence = (
        sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    )

    return {
        "totals": {
            "classifications": total_classifications,
            "appeals": total_appeals,
            "events": len(events),
        },
        "detection_patterns": by_attribution,
        "appeal_rate": {
            "ratio": round(appeal_rate, 4),
            "percent": round(appeal_rate * 100, 2),
        },
        "additional_metric": {
            "name": "average_confidence",
            "value": round(avg_confidence, 4),
        },
        "generated_at": utc_now_iso(),
    }


@app.get("/health")
def health() -> Any:
    return jsonify({
        "status": "ok",
        "service": "provenance-guard",
        "timestamp": utc_now_iso(),
    })


@app.get("/analytics")
def analytics() -> Any:
        events = read_all_audit_events()
        return jsonify(build_analytics_summary(events)), 200


@app.get("/dashboard")
def dashboard() -> Any:
        html = """
<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Provenance Guard Analytics</title>
    <style>
        :root {
            --bg: #f4f3ee;
            --card: #ffffff;
            --ink: #1f1f1f;
            --muted: #5d5d5d;
            --accent: #0b6e4f;
            --line: #dedad2;
        }
        body {
            margin: 0;
            font-family: \"IBM Plex Sans\", \"Segoe UI\", sans-serif;
            background: radial-gradient(circle at 20% 10%, #e8efe7, var(--bg) 45%);
            color: var(--ink);
        }
        .wrap {
            max-width: 960px;
            margin: 2rem auto;
            padding: 0 1rem;
        }
        h1 { margin-bottom: 0.25rem; }
        .sub { color: var(--muted); margin-top: 0; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 0.9rem;
            margin-top: 1rem;
        }
        .card {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 1rem;
            box-shadow: 0 8px 24px rgba(0,0,0,0.04);
        }
        .k { color: var(--muted); font-size: 0.9rem; }
        .v { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
        pre {
            background: #0f172a;
            color: #d1fae5;
            padding: 1rem;
            border-radius: 10px;
            overflow: auto;
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <h1>Provenance Guard Analytics Dashboard</h1>
        <p class=\"sub\">Live summary computed from <code>data/audit_log.jsonl</code></p>

        <div class=\"grid\" id=\"cards\"></div>

        <h2>Raw Analytics JSON</h2>
        <pre id=\"raw\">Loading...</pre>
    </div>

    <script>
        async function load() {
            const response = await fetch('/analytics');
            const data = await response.json();

            const cards = document.getElementById('cards');
            const items = [
                ['Classifications', data.totals.classifications],
                ['Appeals', data.totals.appeals],
                ['Appeal Rate', data.appeal_rate.percent + '%'],
                ['Avg Confidence', data.additional_metric.value],
                ['Likely AI', data.detection_patterns.likely_ai],
                ['Uncertain', data.detection_patterns.uncertain],
                ['Likely Human', data.detection_patterns.likely_human],
            ];

            cards.innerHTML = items.map(([k, v]) => `
                <div class=\"card\">
                    <div class=\"k\">${k}</div>
                    <div class=\"v\">${v}</div>
                </div>
            `).join('');

            document.getElementById('raw').textContent = JSON.stringify(data, null, 2);
        }

        load().catch((err) => {
            document.getElementById('raw').textContent = String(err);
        });
    </script>
</body>
</html>
"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit() -> Any:
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    creator_id = (payload.get("creator_id") or "").strip()

    if not creator_id:
        return jsonify({"error": "creator_id is required"}), 400
    if not text:
        return jsonify({"error": "text is required"}), 400
    if len(text) < MIN_TEXT_LENGTH:
        return jsonify(
            {
                "error": f"text must be at least {MIN_TEXT_LENGTH} characters",
                "received_length": len(text),
            }
        ), 400

    content_id = str(uuid.uuid4())
    created_at = utc_now_iso()

    llm_result = llm_signal_score(text)
    llm_score = llm_result["llm_score"]
    stylometric_result = stylometric_signal_score(text)
    stylometric_score = stylometric_result["stylometric_score"]

    confidence = combine_confidence(llm_score, stylometric_score)
    attribution = attribution_from_score(confidence)
    label = label_from_confidence(confidence)

    content_record = {
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "created_at": created_at,
        "updated_at": created_at,
        "status": "classified",
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "label": label,
        "signal_scores": {
            "llm_score": round(llm_score, 4),
            "stylometric_score": round(stylometric_score, 4),
        },
    }
    content_index = load_content_index()
    content_index[content_id] = content_record
    save_content_index(content_index)

    event = {
        "event_type": "classification",
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": created_at,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "llm_score": round(llm_score, 4),
        "stylometric_score": round(stylometric_score, 4),
        "label": label,
        "status": "classified",
        "normalization_notes": {
            "llm": llm_result.get("normalization_notes", "unknown"),
            "stylometric": stylometric_result.get("normalization_notes", "unknown"),
        },
        "stylometric_features": stylometric_result.get("features", {}),
    }
    append_audit_event(event)

    response = {
        "content_id": content_id,
        "attribution": attribution,
        "confidence": round(confidence, 4),
        "label": label,
        "signal_scores": {
            "llm_score": round(llm_score, 4),
            "stylometric_score": round(stylometric_score, 4),
        },
        "status": "classified",
        "created_at": created_at,
    }
    return jsonify(response), 200


@app.get("/log")
def get_log() -> Any:
    entries = read_recent_audit_events()
    return jsonify({"entries": entries}), 200


@app.get("/content/<content_id>")
def get_content(content_id: str) -> Any:
    content_index = load_content_index()
    record = content_index.get(content_id)
    if not record:
        return jsonify({"error": "content_id not found"}), 404
    return jsonify(record), 200


@app.post("/appeal")
def submit_appeal() -> Any:
    payload = request.get_json(silent=True) or {}
    content_id = (payload.get("content_id") or "").strip()
    creator_reasoning = (payload.get("creator_reasoning") or "").strip()
    requester_id = (request.headers.get("X-User-Id") or "").strip()

    if not content_id:
        return jsonify({"error": "content_id is required"}), 400
    if len(creator_reasoning) < 30:
        return jsonify({"error": "creator_reasoning must be at least 30 characters"}), 400
    if not requester_id:
        return jsonify({"error": "X-User-Id header is required"}), 400

    content_index = load_content_index()
    record = content_index.get(content_id)
    if not record:
        return jsonify({"error": "content_id not found"}), 404

    if requester_id != record.get("creator_id"):
        return jsonify({"error": "only the original creator can appeal this content"}), 403

    timestamp = utc_now_iso()
    record["status"] = "under_review"
    record["updated_at"] = timestamp
    record["appeal_reasoning"] = creator_reasoning
    record["appealed_at"] = timestamp
    content_index[content_id] = record
    save_content_index(content_index)

    appeal_event = {
        "event_type": "appeal",
        "timestamp": timestamp,
        "content_id": content_id,
        "creator_id": requester_id,
        "appeal_reasoning": creator_reasoning,
        "status": "under_review",
    }
    append_audit_event(appeal_event)

    return jsonify(
        {
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received and queued for review.",
        }
    ), 200


if __name__ == "__main__":
    ensure_storage()
    app.run(host="0.0.0.0", port=5000, debug=True)
