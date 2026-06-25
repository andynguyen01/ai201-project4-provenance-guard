"""Provenance Guard — Flask API.

A multi-signal AI-content attribution service. Submissions run through a
three-signal ensemble (Groq LLM, stylometrics, lexical AI-tells), are scored
into a single calibrated confidence, labeled, and audit-logged. Creators can
appeal, earn a verified-human credential, and the system exposes analytics.

Endpoints:
  POST /submit     classify text or image-metadata; returns label + scores
  POST /appeal     contest a classification (status -> under_review)
  POST /verify     earn a verified-human credential (stretch: provenance cert)
  GET  /analytics  detection patterns, appeal rate, avg confidence (stretch)
  GET  /log        recent audit-log entries as JSON
  GET  /           web UI
"""

import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import audit_log
import creators
from detection import (
    attribution_from_score,
    combine_scores,
    lexical_signal,
    llm_signal,
    stylometric_signal,
)
from labels import make_label

load_dotenv()

app = Flask(__name__)
audit_log.init_db()
creators.init_db()

# Rate limiting (see planning.md > Rate Limiting). In-memory storage is fine for
# local/dev; a production deployment would use Redis via storage_uri.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# Minimum length to analyze — below this neither signal is meaningful
# (see planning.md > Anticipated Edge Cases).
MIN_WORDS = 10
# A verification sample must be longer, so the human-writing check is meaningful.
VERIFY_MIN_WORDS = 25

# Credential line prefixed to the label for verified creators (stretch feature).
CREDENTIAL_LINE = (
    "✅ Verified human creator — this account completed identity-style writing "
    "verification."
)


def _utc_now():
    """ISO-8601 UTC timestamp, e.g. 2026-06-25T14:32:10.123456Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_pipeline(analysis_text, mode="text", stylo_text=None):
    """Run all three signals + scoring. Returns (signals_dict, confidence, attribution)."""
    stylo_text = analysis_text if stylo_text is None else stylo_text
    s1 = llm_signal(analysis_text, mode=mode)
    s2 = stylometric_signal(stylo_text)
    s3 = lexical_signal(analysis_text)
    confidence = combine_scores(
        s1["llm_score"], s2["stylometric_score"], s3["lexical_score"]
    )
    attribution = attribution_from_score(confidence)
    return {"llm": s1, "stylometric": s2, "lexical": s3}, confidence, attribution


def _serialize_metadata(metadata):
    """Flatten image metadata into a single analysis string (stretch: multi-modal)."""
    parts = []
    for key in ("title", "medium", "tags", "description"):
        value = metadata.get(key)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


@app.errorhandler(429)
def ratelimit_handler(error):
    return jsonify({
        "error": "Rate limit exceeded. Please slow down and try again later.",
        "limit": str(error.description),
    }), 429


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    creator_id = data.get("creator_id")
    content_type = data.get("content_type", "text")

    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "'creator_id' is a required string field."}), 400

    # Build the analysis text depending on content type (stretch: multi-modal).
    if content_type == "image_metadata":
        metadata = data.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("description"):
            return jsonify(
                {"error": "image_metadata requires a 'metadata' object with a 'description'."}
            ), 400
        analysis_text = _serialize_metadata(metadata)
        stylo_text = str(metadata.get("description", ""))
    elif content_type == "text":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "'text' is a required string field."}), 400
        analysis_text = text
        stylo_text = text
    else:
        return jsonify(
            {"error": "content_type must be 'text' or 'image_metadata'."}
        ), 400

    if len(analysis_text.split()) < MIN_WORDS:
        return jsonify(
            {"error": f"Content must be at least {MIN_WORDS} words to analyze."}
        ), 400

    signals, confidence_score, attribution_result = _run_pipeline(
        analysis_text, mode=content_type, stylo_text=stylo_text
    )
    llm_score = signals["llm"]["llm_score"]
    stylometric_score = signals["stylometric"]["stylometric_score"]
    lexical_score = signals["lexical"]["lexical_score"]

    transparency_label = make_label(attribution_result, confidence_score)

    # Provenance certificate (stretch): verified creators get a credential line
    # prefixed to the label, shown alongside the per-submission verdict.
    provenance = creators.get_status(creator_id)
    if provenance["verified"]:
        transparency_label = f"{CREDENTIAL_LINE}\n\n{transparency_label}"

    content_id = str(uuid.uuid4())
    timestamp = _utc_now()

    audit_log.record_entry({
        "content_id": content_id,
        "creator_id": creator_id,
        "text": analysis_text,
        "timestamp": timestamp,
        "action": "classified",
        "content_type": content_type,
        "attribution_result": attribution_result,
        "confidence_score": round(confidence_score, 4),
        "llm_score": round(llm_score, 4),
        "stylometric_score": round(stylometric_score, 4),
        "lexical_score": round(lexical_score, 4),
        "signals_used": ["llm", "stylometric", "lexical"],
        "transparency_label": transparency_label,
        "status": "classified",
        "appeal_reasoning": None,
    })

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "content_type": content_type,
        "attribution_result": attribution_result,
        "confidence_score": round(confidence_score, 4),
        "transparency_label": transparency_label,
        "status": "classified",
        "provenance": provenance,
        "signals": {
            "llm_score": round(llm_score, 4),
            "llm_reasoning": signals["llm"].get("reasoning", ""),
            "stylometric_score": round(stylometric_score, 4),
            "stylometric_metrics": signals["stylometric"].get("metrics"),
            "stylometric_reliable": signals["stylometric"].get("reliable"),
            "lexical_score": round(lexical_score, 4),
            "lexical_markers": signals["lexical"].get("markers"),
        },
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    creator_reasoning = data.get("creator_reasoning")

    if not isinstance(content_id, str) or not content_id.strip() or \
       not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return jsonify(
            {"error": "Both 'content_id' and 'creator_reasoning' are required string fields."}
        ), 400

    original = audit_log.get_classification(content_id)
    if original is None:
        return jsonify(
            {"error": f"No classification found for content_id '{content_id}'."}
        ), 404

    # Flip the original decision to under_review and log the appeal beside it.
    audit_log.update_status(content_id, "under_review")
    audit_log.record_entry({
        "content_id": content_id,
        "creator_id": original.get("creator_id"),
        "text": original.get("text"),
        "timestamp": _utc_now(),
        "action": "appeal",
        "content_type": original.get("content_type"),
        "attribution_result": original.get("attribution_result"),
        "confidence_score": original.get("confidence_score"),
        "llm_score": original.get("llm_score"),
        "stylometric_score": original.get("stylometric_score"),
        "lexical_score": original.get("lexical_score"),
        "signals_used": original.get("signals_used"),
        "transparency_label": original.get("transparency_label"),
        "status": "under_review",
        "appeal_reasoning": creator_reasoning,
    })

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal has been received and queued for human review.",
    })


@app.route("/verify", methods=["POST"])
def verify():
    """Earn a verified-human credential by submitting a writing sample that the
    detection pipeline judges to be human-written (stretch: provenance cert)."""
    data = request.get_json(silent=True) or {}
    creator_id = data.get("creator_id")
    sample_text = data.get("sample_text")

    if not isinstance(creator_id, str) or not creator_id.strip() or \
       not isinstance(sample_text, str) or not sample_text.strip():
        return jsonify(
            {"error": "Both 'creator_id' and 'sample_text' are required string fields."}
        ), 400
    if len(sample_text.split()) < VERIFY_MIN_WORDS:
        return jsonify(
            {"error": f"Verification sample must be at least {VERIFY_MIN_WORDS} words."}
        ), 400

    _signals, confidence_score, attribution_result = _run_pipeline(sample_text)
    granted = attribution_result == "likely_human"

    if granted:
        creators.set_verified(creator_id, _utc_now())

    status = creators.get_status(creator_id)
    return jsonify({
        "creator_id": creator_id,
        "verified": status["verified"],
        "verified_at": status["verified_at"],
        "granted_this_request": granted,
        "sample_confidence_score": round(confidence_score, 4),
        "sample_attribution": attribution_result,
        "message": (
            "Verification granted — your writing sample read as human-written."
            if granted else
            "Verification not granted — the sample did not read as clearly "
            "human. Please try a longer, more personal writing sample."
        ),
    })


@app.route("/analytics", methods=["GET"])
def analytics():
    """Detection patterns, appeal rate, and avg confidence per result (stretch)."""
    entries = audit_log.get_log(limit=1000000)
    classifieds = [e for e in entries if e.get("action") == "classified"]
    appeals = [e for e in entries if e.get("action") == "appeal"]
    total = len(classifieds)

    buckets = ["likely_ai", "uncertain", "likely_human"]
    counts = {b: 0 for b in buckets}
    conf_sums = {b: 0.0 for b in buckets}
    for entry in classifieds:
        result = entry.get("attribution_result")
        if result in counts:
            counts[result] += 1
            conf_sums[result] += entry.get("confidence_score") or 0.0

    distribution = {
        b: {
            "count": counts[b],
            "pct": round(counts[b] / total * 100, 1) if total else 0.0,
        }
        for b in buckets
    }
    avg_confidence = {
        b: (round(conf_sums[b] / counts[b], 4) if counts[b] else None)
        for b in buckets
    }

    return jsonify({
        "total_classifications": total,
        "result_distribution": distribution,
        "appeals_filed": len(appeals),
        "appeal_rate": round(len(appeals) / total, 4) if total else 0.0,
        "avg_confidence_by_result": avg_confidence,
    })


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"entries": audit_log.get_log(limit=limit)})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
