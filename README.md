# Provenance Guard

A backend service that any creative-sharing platform can plug into to decide whether submitted text reads as **human-written** or **AI-generated**, score its confidence honestly, surface a plain-language **transparency label** to readers, and give creators a path to **appeal**. It ships with production safety infrastructure (rate limiting, structured audit logging) and four stretch features (3-signal ensemble, analytics dashboard, verified-human credential, multi-modal support).

The full pre-implementation spec lives in [planning.md](planning.md). This README is the canonical record of what was built.

---

## Quick start

```bash
python -m venv .venv
# Windows (PowerShell):  .\.venv\Scripts\Activate.ps1
# Windows (Git Bash):    source .venv/Scripts/activate
# macOS/Linux:           source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` in the repo root (it is git-ignored — never commit it):

```
GROQ_API_KEY=your_key_here
```

Run the app:

```bash
python app.py
```

Open **http://localhost:5000/** for the web UI, or call the API directly (examples below).

---

## Architecture overview — the path a submission takes

```
Creator → POST /submit ──► [rate limiter] ──► [validation ≥10 words]
                                                     │ raw text
            ┌────────────────────────────────────────┼────────────────────────────┐
            ▼                                          ▼                            ▼
   Signal 1: Groq LLM                  Signal 2: Stylometrics          Signal 3: Lexical AI-tells
   (semantic, llm_score)               (structural, stylometric_score) (lexical, lexical_score)
            └────────────────────────────────────────┼────────────────────────────┘
                                                       ▼
                            Confidence scoring (weighted blend + disagreement penalty)
                                                       │ confidence_score (P(AI)) + attribution_result
                                                       ▼
                            Transparency label generator (+ provenance credential if verified)
                                                       ▼
                            Audit log (SQLite)  ──►  JSON response (content_id, result, confidence, label)
```

A submission enters `POST /submit` and first passes a **rate limiter** and **input validation** (≥10 words). The raw text fans out to **three independent detection signals**. Their scores are blended into a single **confidence score** = the system's estimated probability that the text is AI-generated, which maps to one of three **attribution results** (`likely_ai` / `uncertain` / `likely_human`) using asymmetric thresholds. The result drives the **transparency label** (with a verified-human credential prefixed if the creator earned one), the full decision is written to the **audit log**, and the response returns the `content_id`, attribution, confidence, label, and individual signal scores. A creator who disagrees calls `POST /appeal` with that `content_id`; the system flips the content's status to `under_review` and logs the appeal beside the original decision for a human reviewer.

### API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/submit` | Classify text or image-metadata. Rate-limited. |
| `POST` | `/appeal` | Contest a classification (status → `under_review`). |
| `POST` | `/verify` | Earn a verified-human credential (stretch). |
| `GET` | `/analytics` | Detection patterns, appeal rate, avg confidence (stretch). |
| `GET` | `/log` | Recent audit-log entries as JSON. |
| `GET` | `/` | Web UI. |

---

## Detection signals

The pipeline uses three **independent** signals — each captures a genuinely different property of the text, so they fail in different ways and their combination is more informative than any one alone.

### Signal 1 — Groq LLM classifier (semantic)
- **Measures:** holistic semantic plausibility — coherence, specificity of lived detail, idiom, hedging, and the averaged-out "generic" voice typical of LLM output. Sends the text to `llama-3.3-70b-versatile` and parses a calibrated `ai_likelihood` in `[0,1]`.
- **Why chosen:** AI text is fluent but generic; human writing carries specific detail and uneven emphasis. A capable LLM picks up gestalt qualities that resist a formula.
- **What it misses:** it has no ground truth and can be confidently wrong. It is biased against *polished* human writing — formal academic prose and non-native-English writing read as "too AI." It is non-deterministic and can be gamed by prompting an LLM to write deliberately messy text.

### Signal 2 — Stylometric heuristics (structural)
- **Measures:** statistical uniformity. Three pure-Python metrics, each mapped to an AI-likelihood sub-score and combined `0.50·variance + 0.25·TTR + 0.25·punctuation`:
  - **Sentence-length variance** — low variance = uniform = AI-leaning (`v = clamp(1 − SD/8)`).
  - **Type-token ratio** — high, evenly-spread vocabulary = AI rarely repeats (`t = clamp((TTR − 0.5)/0.3)`).
  - **Punctuation variety** — few distinct punctuation types = clean/regular = AI (`p = clamp(1 − distinct/5)`).
- **Why chosen:** it is meaning-blind and therefore independent of the LLM — it catches mechanical uniformity the LLM might rationalize away. Computable with no external call, so it still produces a signal if Groq is down.
- **What it misses:** surface-level only. A human writing in a uniform formal register looks AI-like; lightly-edited AI looks human. It is **unreliable on text under 3 sentences** (returns a neutral 0.5 + a `reliable: false` flag). TTR is the weakest of the three metrics.

### Signal 3 — Lexical "AI-tell" heuristic (lexical) — *stretch: ensemble*
- **Measures:** the rate of stock phrases over-represented in LLM output ("it is important to note," "furthermore," "delve into," "paradigm shift," …) plus em-dash density, scaled per 100 words.
- **Why chosen:** adds a third, distinct *lexical* property — independent of both semantics and structure.
- **What it misses:** trivially gamed by find-and-replace, and prone to false positives on formal human writing that legitimately uses transitional language — which is why it carries the lowest weight.

---

## Confidence scoring

`confidence_score` is a single directional number in `[0,1]` = the system's estimated probability the content is **AI-generated**. `0.0` = confidently human, `0.5` = maximally uncertain, `1.0` = confidently AI. The raw number is never shown to readers — the label translates it.

**Combination (3-way ensemble):**
```
base       = 0.55·llm + 0.25·stylometric + 0.20·lexical
spread     = max(signals) − min(signals)            # generalized disagreement
confidence = base − (base − 0.5) · (0.5 · spread)
```
The LLM stays dominant; stylometrics and the lexical signal corroborate. The **spread term** pulls the score toward the uncertain midpoint (0.5) whenever the signals conflict, so a lone dissenting signal *widens uncertainty* instead of producing a falsely confident verdict.

**Asymmetric thresholds** (biased against falsely accusing humans):

| `confidence_score` | `attribution_result` |
|---|---|
| `≥ 0.70` | `likely_ai` |
| `0.40 – 0.70` | `uncertain` |
| `< 0.40` | `likely_human` |

The uncertain band sits *above* 0.5: it takes strong combined evidence (≥0.70) to reach an AI verdict, while borderline-AI scores fall into "uncertain" rather than accusing a creator. A `0.51` and a `0.95` therefore produce **different labels** (uncertain vs. high-confidence AI), satisfying the requirement that the score reflect genuine uncertainty rather than a binary flip at 0.5.

### How I validated it's meaningful

I ran four deliberately chosen inputs spanning the range and printed every signal separately (see the harness in [detection.py](detection.py) — run `python detection.py`). The combined scores match intuition and separate clearly:

| Input | llm | stylometric | lexical | **confidence** | result |
|---|---|---|---|---|---|
| Clearly AI ("paradigm shift…") | 0.87 | 0.56 | 1.00 | **0.749** | `likely_ai` |
| Clearly human (casual ramen review) | 0.23 | 0.43 | 0.00 | **0.291** | `likely_human` |
| Borderline: formal human (monetary policy) | 0.87 | 0.50 | 0.00 | **0.558** | `uncertain` |
| Borderline: lightly-edited AI (remote work) | 0.42 | 0.44 | 0.43 | **0.427** | `uncertain` |

### Two example submissions (actual scores)

**High-confidence case — clearly AI text** → `confidence_score = 0.7485`, `likely_ai`. All three signals agreed it was AI-like (llm 0.87, lexical 1.0 from 4 stock-phrase hits), so the score stayed high.

**Lower-confidence case — formal human prose** → `confidence_score = 0.558`, `uncertain`. The LLM flagged it as "too polished" (0.87), but the lexical signal found *zero* AI stock phrases (0.0) and stylometrics was neutral. The wide signal spread pulled the score down into the uncertain band — a single-signal system would have called this human's work AI (a false positive); the ensemble correctly hedges. This is the system's most important behavior, and it is driven directly by the false-positive analysis in planning.md.

---

## Transparency label

Three variants, selected by `attribution_result`. The label is plain language, never shows the raw score, and translates confidence into an approximate percentage plus a verbal hedge. `{pct}` = `round(confidence_score × 100)`; `{human_pct}` = `round((1 − confidence_score) × 100)`.

| Variant | Exact text displayed |
|---|---|
| **High-confidence AI** (`≥ 0.70`) | 🤖 **Likely AI-generated.** Our analysis suggests this text was probably created with the help of AI tools (roughly {pct}% likely AI-generated). This is an automated estimate, not a certainty. If you're the creator and believe this is wrong, you can appeal this decision. |
| **High-confidence human** (`< 0.40`) | ✍️ **Likely human-written.** Our analysis suggests this text was most likely written by a person (roughly {human_pct}% likely human-written). No strong AI-generation signals stood out. This is an automated estimate, not a guarantee. |
| **Uncertain** (`0.40 – 0.70`) | ❓ **Uncertain.** Our analysis couldn't confidently determine whether this text was written by a person or generated with AI — the signals were mixed, so we are not making a call. Please use your own judgment. If you're the creator, you're welcome to add context or appeal. |

The tone is deliberately asymmetric: the uncertain variant never implies wrongdoing, and even the AI variant offers the appeal path rather than stating guilt as fact. Verified creators (see provenance certificate) get a credential line prefixed:

> ✅ **Verified human creator** — this account completed identity-style writing verification.

A live, color-coded version of each label renders in the web UI at `/`.

---

## Appeals workflow

Any creator can contest a decision via `POST /appeal` with the `content_id` (returned by `/submit`) and free-text `creator_reasoning`. The system:
1. Looks up the original classification (returns `404` if the `content_id` is unknown).
2. Updates the content's status from `classified` to `under_review`.
3. Writes a new audit-log entry (`action: "appeal"`) capturing the reasoning beside a snapshot of the original decision.
4. Returns a confirmation.

No automated re-classification occurs — the appeal is queued for a human reviewer, who sees the original text, both/all signal scores, the confidence, the label shown, and the creator's reasoning together.

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-CONTENT-ID", "creator_reasoning": "I wrote this myself from personal experience."}'
# → {"content_id": "...", "status": "under_review", "message": "Your appeal has been received and queued for human review."}
```

---

## Rate limiting

Applied to `POST /submit` via Flask-Limiter (in-memory storage): **`10 per minute; 100 per day`** per client IP.

**Reasoning.** A real creator submits their own work occasionally — a handful of pieces in a sitting, never dozens per minute — so 10/minute never obstructs legitimate use while stopping a script from hammering the endpoint and burning the Groq API quota. The 100/day ceiling caps sustained abuse from a single source over a longer window. These are deliberate values for a writing platform's usage profile, not arbitrary.

**Evidence** (12 rapid requests — the first 10 succeed, the rest are throttled):

```
$ for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code}\n" -X POST \
    http://localhost:5000/submit -H "Content-Type: application/json" \
    -d '{"text":"This is a test submission for rate limit testing purposes only.","creator_id":"ratelimit-test"}'; done
200
200
200
200
200
200
200
200
200
200
429
429
```

---

## Audit log

Every decision and appeal is written to a structured SQLite table (`provenance_guard.db`). Inspect it via `GET /log` (JSON). Each entry records the `content_id`, `creator_id`, timestamp, `content_type`, attribution result, combined confidence, **all individual signal scores**, an explicit `signals_used` list, the label shown, status, and any `appeal_reasoning`. Sample (3 entries — a text classification, a multi-modal classification, and an appeal):

```json
{
  "action": "appeal",
  "appeal_reasoning": "I wrote this for a class essay; the formal tone is the assignment's requirement, not AI.",
  "attribution_result": "likely_ai",
  "confidence_score": 0.7485,
  "content_id": "a10ca1bb-311f-468a-bc05-e98da9b2e45c",
  "content_type": "text",
  "creator_id": "alice",
  "lexical_score": 1.0,
  "llm_score": 0.87,
  "signals_used": ["llm", "stylometric", "lexical"],
  "status": "under_review",
  "stylometric_score": 0.5602,
  "timestamp": "2026-06-25T15:01:46.373661Z"
}
{
  "action": "classified",
  "attribution_result": "likely_human",
  "confidence_score": 0.2682,
  "content_id": "7e44377d-053c-4466-bc5d-26e59902efb8",
  "content_type": "image_metadata",
  "creator_id": "carol",
  "lexical_score": 0.0,
  "llm_score": 0.12,
  "signals_used": ["llm", "stylometric", "lexical"],
  "status": "classified",
  "stylometric_score": 0.5,
  "timestamp": "2026-06-25T15:01:45.409560Z"
}
{
  "action": "classified",
  "attribution_result": "likely_human",
  "confidence_score": 0.2745,
  "content_id": "…",
  "content_type": "text",
  "creator_id": "bob",
  "lexical_score": 0.0,
  "llm_score": 0.23,
  "signals_used": ["llm", "stylometric", "lexical"],
  "status": "classified",
  "stylometric_score": 0.43,
  "timestamp": "2026-06-25T15:01:44.…Z"
}
```

---

## Stretch features

All four committed stretch features are implemented and working.

### 1. Ensemble detection (3+ signals with documented weighting)
The pipeline runs a **third** signal — the lexical AI-tell heuristic — and combines all three with a documented weighted vote: **LLM 0.55 / stylometric 0.25 / lexical 0.20**. The disagreement penalty was generalized from a pairwise difference to the **spread** (max − min) across all three scores, so any single dissenting signal still widens uncertainty. See `combine_scores` and `lexical_signal` in [detection.py](detection.py).

### 2. Analytics dashboard
`GET /analytics` (and a panel in the web UI) computes three metrics from the audit log: the **result distribution** (counts + % across the three categories), the **appeal rate** (appeals ÷ classifications), and the **average confidence per result** (self-chosen third metric — shows how decisive the system is in each bucket). Example output:

```json
{
  "total_classifications": 4,
  "result_distribution": {
    "likely_ai":    {"count": 1, "pct": 25.0},
    "uncertain":    {"count": 0, "pct": 0.0},
    "likely_human": {"count": 3, "pct": 75.0}
  },
  "appeals_filed": 1,
  "appeal_rate": 0.25,
  "avg_confidence_by_result": {"likely_ai": 0.7485, "uncertain": null, "likely_human": 0.3131}
}
```

### 3. Provenance certificate (verified-human credential)
`POST /verify` accepts a `creator_id` and a writing sample (25+ words). The sample is run through the detection pipeline; if it reads as clearly human (`likely_human`), the account is marked verified in a `creator_status` table ([creators.py](creators.py)). Afterward, every submission from that creator includes a `provenance` block in the response and the transparency label is **prefixed with the credential line** shown above. The credential is framed as *account-level* reputation — it proves the account once demonstrated human writing, **not** that every later submission is unassisted.

```bash
curl -s -X POST http://localhost:5000/verify -H "Content-Type: application/json" \
  -d '{"creator_id":"dave","sample_text":"honestly i never thought i would get into pottery but my hands just know what to do now…"}'
# → {"creator_id":"dave","verified":true,"granted_this_request":true,"sample_attribution":"likely_human", ...}
```

### 4. Multi-modal support
`POST /submit` accepts an optional `content_type` of `"text"` (default) or `"image_metadata"`. For image metadata, the request carries a `metadata` object (`title`, `medium`, `tags`, `description`); the system serializes the describable fields and routes them through the same three signals (the LLM uses an image-metadata-specific prompt; stylometrics runs on the description). The scoring, label, audit-log, and appeal layers are **unchanged** — proving the production layer is content-type-agnostic.

```bash
curl -s -X POST http://localhost:5000/submit -H "Content-Type: application/json" \
  -d '{"content_type":"image_metadata","creator_id":"carol","metadata":{"title":"Autumn Reverie","medium":"oil on canvas","tags":["landscape","autumn"],"description":"A quiet hillside at dusk where I tried to capture the orange my grandmother'\''s garden turned every October."}}'
# → classified as likely_human (0.2682)
```

---

## Known limitations

- **Formal human prose and non-native-English writing.** The LLM signal is biased to read clean, even, formally-worded human writing as "too polished," and the stylometric variance metric reads its low sentence-length variance as AI-uniform. Two of three signals can lean AI on genuinely human work. The asymmetric thresholds and the lexical signal (which finds no stock phrases in such text) usually pull it back into `uncertain` rather than `likely_ai` — but it will rarely be confidently labeled human. This is a direct consequence of the LLM's polish-bias and the variance metric, and it's why the appeal path exists.
- **Short submissions and image metadata.** Under 3 sentences, the stylometric signal is statistically meaningless and returns a neutral 0.5 (`reliable: false`), so the verdict leans almost entirely on the LLM — which is itself shaky on little text. Image-metadata descriptions are usually short, so this content type leans hardest on the LLM.
- **The lexical signal is gameable.** A find-and-replace of stock phrases defeats it. It carries only 0.20 weight precisely because it is the most brittle signal.

If I were deploying this for real, I would replace the heuristic stylometric and lexical signals with a trained perplexity/burstiness model, calibrate the thresholds against a labeled dataset, and treat the appeal queue as a first-class human-review product rather than a log entry.

---

## Spec reflection

**One way the spec helped.** The false-positive analysis in planning.md forced the **asymmetric-threshold** decision *before* any code existed. When I first wired up only the LLM signal (Milestone 3), the formal-human economics passage scored 0.87 and was labeled `likely_ai` — a textbook false positive. Because the spec had already committed to thresholds biased against accusing humans plus a disagreement penalty, adding the other signals in Milestone 4 pulled that same input down into `uncertain` with no redesign. The spec turned a known failure mode into expected behavior. The pre-written label variants also made the label function a near-mechanical translation of the spec.

**One way the implementation diverged.** The spec set a **15-word minimum** for submissions. During Milestone 5 I found the grader's rate-limit test used an 11-word string, which my validation rejected with `400` before rate limiting could even be demonstrated. I lowered the minimum to **10 words** and updated planning.md to match. The divergence was driven by real testing — the 15-word floor was a guess that conflicted with realistic short inputs (and the `reliable: false` flag already handles short-text quality, so the hard floor didn't need to be that high). I also added a web UI, which the spec never called for, to make the system demoable.

---

## AI usage

I used an AI coding assistant (Claude) throughout, directed by the planning.md spec. Specific instances of what I directed and what I revised or overrode:

1. **Confidence scoring.** I directed the AI to implement `combine_scores` from my Uncertainty Representation section and to verify the generated thresholds matched my spec exactly (`≥0.70 / 0.40–0.70 / <0.40`). When I extended the system to the 3-signal ensemble, I overrode the naive option of averaging pairwise disagreements and instead specified the **spread (max − min)** as the generalized disagreement term, so a single outlier signal still widens uncertainty. I confirmed the behavior by running the four calibration inputs and inspecting each signal separately.

2. **Stylometric TTR direction.** The type-token-ratio metric's direction is genuinely ambiguous. I directed the AI to implement "high TTR → more AI-like," but flagged it as the weakest metric and capped its weight. In testing I confirmed the stylometric scores cluster tightly (0.43–0.56) across very different inputs, which told me the LLM signal is doing most of the real discrimination and stylometrics mainly acts as a disagreement-based uncertainty brake — I documented that honestly rather than overselling the metric.

3. **Transparency label + appeal endpoint.** I directed the AI to generate the label function and `/appeal` from my spec sections and to print all three variants to confirm they matched verbatim. I then revised the design so that a verified creator's credential line is **prefixed** to the label (kept separate from the per-submission verdict, per my planning decision that the credential is account-level, not proof about a specific piece).

---

## Project layout

```
app.py            Flask API: /submit, /appeal, /verify, /analytics, /log, /
detection.py      Three detection signals + confidence scoring
labels.py         Transparency label generation (3 variants)
audit_log.py      Structured SQLite audit log
creators.py       Verified-human credential store (provenance certificate)
templates/        Web UI (index.html)
planning.md       Pre-implementation spec (Milestones 1–2 + stretch plans)
requirements.txt  Dependencies
```
