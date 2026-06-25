# Provenance Guard — Planning and Specification

A backend system a creative-sharing platform can plug into to classify submitted text as human-written or AI-generated, score its confidence in that classification, surface a plain-language transparency label, and let creators appeal a decision they believe is wrong.

This document is written before any implementation code. It is also the primary prompting tool for Milestones 3–5: each implementation milestone pulls specific sections from here as context for AI code generation.

---

# Milestone 1: Understand the System and Define the Architecture

## Architecture

In the submission flow, raw text enters `POST /submit`, passes the rate limiter and input validation, then fans out to two independent detection signals (a Groq LLM and pure-Python stylometrics). Their two scores are blended into a single calibrated confidence score that maps to an attribution result, that result drives the transparency label, the full decision is written to the audit log, and the `content_id`, attribution result, confidence score, and label are returned. In the appeal flow, a creator sends a `content_id` and their reasoning to `POST /appeal`; the system flips that content's status to `under_review`, logs the appeal next to the original decision, and returns a confirmation, leaving the final judgment to a human reviewer.

### Architecture narrative (component by component)

When a creator submits a piece of text, the system receives it through the `POST /submit` endpoint. The request must include the text content and a `creator_id`. Before any analysis runs, the request passes through a **rate limiter**, which rejects the submission with `429` if the client has exceeded its allowed number of requests, and **input validation**, which confirms the required fields are present and the text meets the minimum length to analyze — **at least 10 words** — rejecting anything shorter with `400`. (Submissions that clear 10 words but are still under 3 sentences are accepted but flagged low-reliability; see the edge cases below.) This protects the system from flooding and protects the downstream Groq API quota.

Once accepted, the raw text is passed to the **detection pipeline**, which runs two independent signals. **Signal 1** sends the text to a Groq-hosted LLM, which returns a score reflecting how AI-generated the writing appears. **Signal 2** runs pure-Python stylometric calculations on the same text and returns a structural score. The two signals measure genuinely different properties — one semantic, one statistical — so combining them is more informative than either alone.

The two signal scores are passed to the **confidence scoring** component, which blends them into a single calibrated confidence score and maps it to one of three attribution results: `likely_ai`, `uncertain`, or `likely_human`. The thresholds are intentionally asymmetric, because falsely accusing a human writer of using AI is the most damaging error the system can make.

The attribution result and confidence score are passed to the **transparency label generator**, which produces the plain-language text a reader sees. The wording changes with the result and confidence level so the label communicates genuine certainty or uncertainty rather than a bare number.

Before responding, the system writes a structured entry to the **audit log** (SQLite): a generated `content_id`, the `creator_id`, a timestamp, the attribution result, the combined confidence, both individual signal scores, an explicit `signals_used` list naming which signals contributed (e.g. `["llm", "stylometric"]`), and a status of `classified`. The audit log is the canonical record of every decision and is updated later if the creator appeals. Finally, the endpoint returns a JSON response containing the `content_id`, attribution result, confidence score, transparency label, and individual signal scores. The `content_id` is the handle the creator uses if they later appeal.

The **appeal flow** is a separate path. When a creator disputes a decision, the platform calls `POST /appeal` with the `content_id` and the creator's written reasoning. The system looks up the original decision, updates that content's status to `under_review`, writes a new audit log entry capturing the appeal reasoning alongside the original classification, and returns a confirmation. No automated re-classification occurs; the appeal is queued for a human reviewer.

### Submission flow

```text
Creator submits text
        |
        v  raw text + creator_id (JSON)
POST /submit
  - Rate limiter      -> 429 if over limit
  - Input validation  -> 400 if invalid
        |
        v  raw text
   +----+----------------------+
   |                           |
   v  raw text                 v  raw text
Signal 1: Groq LLM       Signal 2: Stylometric heuristics
Output: llm_score [0-1]  Output: stylometric_score [0-1]
   |                           |
   +----------+----------------+
              v  two signal scores
Confidence scoring (weighted blend + disagreement penalty + asymmetric thresholds)
Output: combined confidence_score + attribution_result
              |
              v  attribution_result + confidence_score
Transparency label generator
Output: plain-language label text
              |
              v  full decision record
Audit log (SQLite)  ---- writes entry: content_id, scores, status=classified
              |
              v  content_id + attribution_result + confidence_score + label
API response (JSON)
```

### Appeal flow

```text
Creator appeals
        |
        v  content_id + creator_reasoning (JSON)
POST /appeal
  - look up content_id  -> 404 if not found
        |
        v  status change: classified -> under_review
Audit log (SQLite)  ---- updates status + appends appeal_reasoning
                         beside the original decision
        |
        v  confirmation (status: under_review)
API response (JSON)
```

## Detection Signals

The system uses two signals chosen to be genuinely independent. One measures meaning and one measures structure. Pairing a semantic detector with a statistical detector is more informative than running two versions of the same idea, because the two signals fail in different ways.

### Signal 1: LLM-Based Classification (Groq, `llama-3.3-70b-versatile`)

**What it measures:** holistic, semantic plausibility. The text is sent to the model with a prompt asking it to judge whether the writing reads as human-written or AI-generated and to return a structured JSON score. The model assesses coherence, idiom, specificity of detail, hedging patterns, and the averaged-out, generic voice that LLM output tends to have.

**Output shape:** a JSON object `{ "ai_likelihood": <float 0–1>, "reasoning": "<short string>" }`, where `ai_likelihood` is 0 for "clearly human" and 1 for "clearly AI." The prompt pins the output format and asks for a calibrated probability, not a yes/no. We parse `ai_likelihood` into `llm_score`. If the call fails or returns unparseable output, `llm_score` falls back to `0.5` (maximally uncertain) and the failure is logged.

**Why this differs between human and AI writing:** AI text is usually fluent but generic — balanced structure, even tone, safe phrasing. Human writing more often contains specific lived detail, uneven emphasis, opinion, and small imperfections. A capable LLM picks up on these gestalt qualities that are hard to reduce to a formula.

**What it cannot capture (blind spot):** the model has no ground truth and is itself a probabilistic guess, so it can be confidently wrong. It is biased against certain human writing it associates with AI — formal academic prose, writing by non-native English speakers, and clean professional copy can all read as "too polished" and be flagged. It can be gamed by a person who prompts an LLM to write deliberately messy text. It is also non-deterministic, so the same input can yield slightly different scores across calls.

### Signal 2: Stylometric Heuristics (pure Python)

**What it measures:** the statistical regularity of the writing at the surface level. AI text tends to be uniform; human writing tends to be variable. The signal computes three metrics, maps each to an AI-likelihood sub-score in [0, 1], and combines them.

| Metric | How it is computed | Direction (toward AI) | Mapping to sub-score |
|---|---|---|---|
| Sentence-length variance | Standard deviation of words-per-sentence | Low variance = uniform = AI | `v = clamp(1 − SD/8, 0, 1)` |
| Type-token ratio (TTR) | unique tokens ÷ total tokens (lowercased) | High, evenly-spread vocabulary = AI rarely repeats itself | `t = clamp((TTR − 0.5)/0.3, 0, 1)` |
| Punctuation variety | count of distinct punctuation types used | Few distinct types = regular/clean = AI | `p = clamp(1 − distinct/5, 0, 1)` |

**Combined stylometric score:** `stylometric_score = 0.50·v + 0.25·t + 0.25·p`. Sentence-length variance is weighted highest because it is the most defensible of the three; TTR and punctuation variety are corroborators.

**Output shape:** a float `stylometric_score` in [0, 1] (higher = more AI-like), plus the three raw metrics retained for debugging. On text shorter than 3 sentences the statistics are too noisy, so the signal returns a neutral `0.5` and flags low reliability.

**Why this differs between human and AI writing:** AI sentences cluster around similar lengths, vocabulary is broad but evenly distributed, and punctuation is regular. Human writing mixes short and long sentences, reuses favorite words for emphasis, and punctuates irregularly. Low variability is a structural fingerprint of generated text.

**What it cannot capture (blind spot):** this signal is meaning-blind. It can be fooled in both directions. A human writing in a uniform, formal register (a legal brief, a textbook paragraph) looks AI-like; AI text that has been lightly edited or prompted for varied rhythm looks human-like. It is unreliable on short text, and it is genre-sensitive — formal poetry with regular line lengths trips the variance metric toward AI. TTR is the weakest of the three metrics, because short human text and rich human prose also have high TTR.

### Why the pair works

The two signals fail in different ways. The LLM is strong on meaning but biased against polished human prose. Stylometry is blind to meaning but catches mechanical uniformity. When the two signals agree, confidence is high. When they disagree, that disagreement is exactly what should push the result toward `uncertain` rather than a confident binary call — and the scoring formula in Milestone 2 does exactly that.

## The False Positive Problem

A false positive is the system labeling a real human's work as AI-generated. On a creative-sharing platform this is the worst error the system can make: it effectively accuses a creator of passing off generated work as their own, damaging their reputation and their trust in the platform. A false negative (missing some AI text) is comparatively mild. The whole system is tuned around making this error harder to commit and easy to contest.

Tracing the scenario: a non-native English speaker submits a heartfelt but formally worded personal essay.

1. The stylometric signal sees low sentence-length variance and a formal, even register, and scores the text fairly AI-like.
2. The LLM signal sees clean, polished prose and also leans AI-ish, though it may notice the personal content and hedge.
3. Confidence scoring combines them. Because both signals roughly agree on "somewhat AI," the combined score lands in a mid-to-high range — but the asymmetric thresholds require ≥0.70 to cross into `likely_ai`, so a borderline case like this lands in `uncertain` instead.
4. The transparency label therefore shows the uncertain variant, which says the system is not sure, avoids accusing the creator, and tells the reader to use their own judgment. It does not present "AI-generated" as a fact.
5. If the result still feels wrong, the creator appeals through `POST /appeal`, the status becomes `under_review`, the appeal is logged alongside the original decision, and a human reviewer gets the full context.

This scenario drives three design decisions: thresholds are asymmetric and biased toward not accusing; the uncertain label reads as genuinely neutral rather than a soft accusation; and the appeal path is simple and always available, because automated detection will never be perfect.

## API Surface

### POST /submit
Submit text for attribution analysis. Rate-limited.

Request: `{ "text": "<content>", "creator_id": "<id>" }`

Response: `{ content_id, creator_id, attribution_result, confidence_score, transparency_label, status, signals: { llm_score, stylometric_score } }`

Errors: `400` (missing/invalid fields, or text shorter than 10 words), `429` (rate limit exceeded).

### POST /appeal
Contest a previous classification.

Request: `{ "content_id": "<id>", "creator_reasoning": "<text>" }`

Response: `{ content_id, status: "under_review", message }`

Errors: `400` (missing fields), `404` (unknown content_id).

### GET /log
Return the most recent audit log entries as JSON (for documentation/grading; would require auth in production). Optional `?limit=N`.

Response: `{ "entries": [ { content_id, creator_id, timestamp, attribution_result, confidence_score, llm_score, stylometric_score, signals_used, status, action, appeal_reasoning? } ] }`

---

# Milestone 2: Write the Spec Before Any Code

## Uncertainty Representation

### What the confidence score means

`confidence_score` is a single directional number in [0, 1] representing the system's estimated probability that the content is **AI-generated**:

- `0.0` = the system is confident the text is human-written.
- `0.5` = maximally uncertain; the signals give no clear lean either way.
- `1.0` = the system is confident the text is AI-generated.

So a confidence score of **0.6** means "leaning AI, but not by enough to commit" — it falls inside the uncertain band and produces the uncertain label, not an AI verdict. A score of **0.51** and a score of **0.95** therefore produce *different labels*: 0.51 is uncertain, 0.95 is a high-confidence AI verdict. The number is never shown raw to readers; the label translates it into plain language and an approximate percentage.

### How raw signal outputs map to the calibrated score

Both signals output an AI-likelihood in [0, 1]. They are combined in two steps:

```text
base       = 0.65 * llm_score + 0.35 * stylometric_score
disagreement = abs(llm_score - stylometric_score)          # 0 = agree, 1 = max conflict
confidence_score = base - (base - 0.5) * (0.5 * disagreement)
```

1. **Weighted blend.** The LLM is the stronger detector, so it gets 0.65; stylometrics gets 0.35 as a structural corroborator.
2. **Disagreement penalty.** When the two signals disagree, the system should be less sure, so the score is pulled toward the uncertain midpoint (0.5) in proportion to the disagreement. If the signals agree, the score is unchanged; if they fully disagree, the score moves halfway from `base` to 0.5. This directly implements "conflict → uncertainty," which supports the false-positive philosophy from Milestone 1.

### Thresholds (asymmetric, biased against false-accusing humans)

| Combined `confidence_score` | `attribution_result` | Label variant |
|---|---|---|
| `>= 0.70` | `likely_ai` | High-confidence AI |
| `0.40 – 0.70` (exclusive of 0.70) | `uncertain` | Uncertain |
| `< 0.40` | `likely_human` | High-confidence human |

The thresholds are deliberately **not** symmetric around 0.5. The uncertain band (0.40–0.70) sits *above* the midpoint, so it takes strong combined evidence (≥0.70) to reach an AI verdict, while borderline-AI scores (0.50–0.69) fall into "uncertain" rather than accusing a creator. This is the single most important calibration decision in the system and is justified by the false-positive analysis in Milestone 1.

### How meaningfulness will be tested

During Milestone 4, the scoring is validated against at least four hand-picked inputs spanning the range: a clearly AI-generated paragraph (expected ≥0.70), a clearly human casual piece (expected <0.40), and two borderline cases — formal human prose and lightly-edited AI output (both expected in the 0.40–0.70 uncertain band). If any input lands in the wrong band, both individual signal scores are printed to find which signal is misbehaving before the scoring is accepted.

## Transparency Label Design

Three variants, selected by `attribution_result`. The label is plain language, never shows the raw score, and translates confidence into an approximate percentage plus a verbal hedge so it is meaningful to a non-technical reader. In the text below, `{pct}` = `round(confidence_score * 100)` and `{human_pct}` = `round((1 − confidence_score) * 100)`.

**High-confidence AI** (`attribution_result == "likely_ai"`, score ≥ 0.70):

> 🤖 **Likely AI-generated.** Our analysis suggests this text was probably created with the help of AI tools (roughly {pct}% likely AI-generated). This is an automated estimate, not a certainty. If you're the creator and believe this is wrong, you can appeal this decision.

**High-confidence human** (`attribution_result == "likely_human"`, score < 0.40):

> ✍️ **Likely human-written.** Our analysis suggests this text was most likely written by a person (roughly {human_pct}% likely human-written). No strong AI-generation signals stood out. This is an automated estimate, not a guarantee.

**Uncertain** (`attribution_result == "uncertain"`, 0.40 ≤ score < 0.70):

> ❓ **Uncertain.** Our analysis couldn't confidently determine whether this text was written by a person or generated with AI — the signals were mixed, so we are not making a call. Please use your own judgment. If you're the creator, you're welcome to add context or appeal.

Note the asymmetry in tone: the uncertain variant never implies wrongdoing, and even the AI variant explicitly offers the appeal path rather than stating guilt as fact.

## Appeals Workflow

**Who can submit an appeal:** the creator of the content (or the platform acting on their behalf). An appeal is tied to a specific decision by its `content_id`, which was returned in the original `/submit` response.

**What they provide:** a JSON body with `content_id` (the decision being contested) and `creator_reasoning` (free-text explanation of why they believe the classification is wrong).

**What the system does on receipt:**
1. Look up the `content_id` in the audit log. If it does not exist, return `404`.
2. Update that content's status from `classified` to `under_review`.
3. Write a new audit log entry recording the appeal: the same `content_id`, a new timestamp, an action of `appeal`, the `creator_reasoning`, and a snapshot of the original decision (attribution result, confidence, both signal scores) so the appeal sits beside the decision it contests.
4. Return a confirmation: `{ content_id, status: "under_review", message: "Your appeal has been received and queued for human review." }`.

No automated re-classification occurs — that is explicitly out of scope.

**What a human reviewer sees when they open the appeal queue:** for each content with status `under_review`, the original submitted text, the `creator_id`, the original timestamp, the `attribution_result`, the `confidence_score`, both individual signal scores (`llm_score` and `stylometric_score`), the transparency label that was shown, and the creator's `creator_reasoning` — all together, so the reviewer can weigh the system's evidence against the creator's explanation and make the final call.

## Anticipated Edge Cases

The system will handle these specific cases poorly. Each is named with the signal property that causes the failure.

1. **Formal poetry with regular line structure.** A poem with short, evenly-sized lines and sparse punctuation produces low sentence-length variance and low punctuation variety, both of which push the stylometric signal toward AI. The LLM may also misread compressed poetic language as "generic." A human-written poem can therefore drift toward the uncertain or AI band. The wide uncertain band and the appeal path are the mitigations.

2. **Non-native English speakers and formal academic prose.** Clean, even, formally-worded human writing reads as "too polished" to the LLM and as uniform to stylometrics — a false-positive risk. This is the primary scenario the asymmetric thresholds are designed to absorb (it lands in `uncertain`, not `likely_ai`).

3. **Very short submissions.** Text shorter than 10 words is rejected outright with `400`, because neither signal is meaningful on a fragment. Text that clears 10 words but is still under 3 sentences is accepted, but the stylometric statistics are dominated by noise (variance and TTR are unstable), so the stylometric signal returns a neutral 0.5 and flags low reliability. In that range the verdict leans on the LLM alone — which is itself unreliable on so little text — so such results should be treated with low trust.

4. **Lightly-edited AI output (human–AI collaboration).** Text that was AI-drafted and then revised by a human is genuinely ambiguous — it is neither cleanly human nor cleanly AI. By design this lands in the `uncertain` band rather than being forced into a binary call. This is a feature, not a failure, but it means the system cannot resolve the most common real-world case definitively.

## Rate Limiting (planned values)

Applied to `POST /submit`: **10 requests per minute** and **100 requests per day** per client IP.

Reasoning: a real creator submits their own work occasionally — a handful of pieces in a sitting, not dozens per minute — so 10/minute never obstructs legitimate use while stopping a script from hammering the endpoint and burning the Groq quota. The 100/day ceiling caps sustained abuse from a single source over a longer window. Final values and the `429` evidence are documented in the README.

## AI Tool Plan

For each implementation milestone: which spec sections feed the AI tool, what it is asked to generate, and how the output is verified.

### M3 — Submission endpoint + first signal
- **Spec provided:** the *Detection Signals* section (Signal 1) + the *Architecture* diagram + the *API Surface* (`/submit`, `/log`).
- **Ask it to generate:** the Flask app skeleton with the `POST /submit` route stub returning a hardcoded response, the Signal 1 (Groq) function, the SQLite audit-log helper, and the `GET /log` route.
- **How to verify:** call the Signal 1 function directly with two or three test inputs and inspect that it returns a parseable `llm_score` in [0, 1] before wiring it into the endpoint. Confirm the route returns `content_id`, attribution, placeholder confidence, and placeholder label, and that each call writes a structured row to the log.

### M4 — Second signal + confidence scoring
- **Spec provided:** the *Detection Signals* section (Signal 2) + the *Uncertainty Representation* section (formula + thresholds) + the diagram.
- **Ask it to generate:** the Signal 2 stylometric function (three metrics + combined score) and the confidence scoring function implementing the weighted blend, disagreement penalty, and the three-way threshold mapping.
- **How to verify:** confirm the generated thresholds match this spec exactly (≥0.70 / 0.40–0.70 / <0.40) — AI tools sometimes implement reasonable-looking but divergent ranges. Run the four calibration inputs (clearly AI, clearly human, formal human, lightly-edited AI) and check scores land in the expected bands; if not, print both signal scores to isolate the misbehaving one. Update the audit log to record both signal scores.

### M5 — Production layer
- **Spec provided:** the *Transparency Label Design* section (three variants) + the *Appeals Workflow* section + the *Rate Limiting* section + the diagram.
- **Ask it to generate:** the label-generation function mapping `attribution_result` + `confidence_score` to the correct variant text, the `POST /appeal` endpoint, and the Flask-Limiter configuration.
- **How to verify:** ask the tool to print all three label variants and confirm the text matches this spec verbatim. Submit inputs that produce each of the three confidence bands to confirm all variants are reachable. File an appeal with a real `content_id` and confirm via `GET /log` that the status flips to `under_review` and `appeal_reasoning` is populated. Send 12 rapid requests and confirm the first 10 return `200` and the rest `429`.

---

# Stretch Features (Planned)

The four required milestones above cover the 25 required points. The stretch features below are optional (4 points). Per the grading instructions, planning.md is updated *before* starting each one. **All four stretch features are committed and will be built** after the required pipeline is working; each has its design, integration point, blind spot, and verification specified below so it slots in without reworking the core.

## Committed: Ensemble Detection (3rd signal)

The core scoring already uses a documented **weighted blend** of two signals, so adding a third is a natural extension rather than a rewrite. Planned third signal: a **lightweight lexical "AI-tell" heuristic** — a pure-Python check for phrases and patterns over-represented in LLM output (e.g. "it is important to note," "in conclusion," "delve into," "furthermore," "navigate the complexities of," em-dash and tricolon density). It outputs an `ai_tell_score` in [0, 1] from the rate of matched markers.

- **Why it is distinct:** Signal 1 captures *semantics*, Signal 2 captures *structure*, and this captures *surface lexical fingerprints* — a third, independent property of the text.
- **Weighting / voting approach (documented):** move from the current two-way blend to a three-way weighted vote. Proposed weights — LLM `0.55`, stylometric `0.25`, lexical-tell `0.20` — keeping the LLM dominant. The disagreement penalty generalizes from a pairwise difference to the spread (max − min) across the three signal scores, so a lone dissenting signal still pulls the result toward `uncertain`.
- **What it can't capture (blind spot):** trivially gamed by find-and-replace of stock phrases, and prone to false positives on formal human writing that legitimately uses transitional language — which is exactly why it is the lowest-weighted of the three.
- **Verification:** re-run the four M4 calibration inputs and confirm the three-signal blend still lands each in its expected band, and that adding the signal does not push any clearly-human input into `likely_ai`.

## Committed: Provenance Certificate ("verified human" credential)

A creator can earn a `verified_human` credential through an extra verification step, and content they submit afterward displays that credential to readers.

- **How it is earned:** a new `POST /verify` endpoint issues a one-time writing challenge (a random prompt the creator must respond to in their own words, in real time). The response is checked for plausibility (length, and a low `ai_likelihood` from the existing detection pipeline run against the challenge response). On success, the creator's `creator_id` is marked verified in a new `creator_status` store (SQLite table: `creator_id`, `verified` flag, `verified_at` timestamp).
- **How it is displayed:** when a verified creator submits content, the `/submit` response includes a `provenance` block (`{ "verified_human": true, "verified_at": "..." }`), and the transparency label is prefixed with a credential line: **"✅ Verified human creator — this account completed identity-style writing verification."** The credential is about the *account*, shown alongside (not replacing) the per-submission attribution label, so the two pieces of information stay distinct.
- **Why it helps:** detection alone can never be certain; a verified-human credential gives trustworthy creators a way to carry provenance that does not depend on the classifier guessing right.
- **Blind spot:** verification proves the account passed a writing challenge once, not that every later submission is unassisted — so the credential is framed as account-level reputation, never as proof that a specific piece is human.
- **Verification:** call `/verify` for a creator, confirm `creator_status` flips to verified, then submit content as that creator and confirm the `provenance` block and credential line appear; submit as an unverified creator and confirm they do not.

## Committed: Analytics Dashboard

A read-only view summarizing system behavior, computed entirely from the audit log.

- **Endpoint:** `GET /analytics`, returning JSON (and optionally a minimal HTML page rendering the same numbers).
- **Metrics:**
  1. **Detection-result distribution** — counts and percentages of `likely_ai` / `uncertain` / `likely_human` across all classifications.
  2. **Appeal rate** — number of appeals ÷ number of classifications.
  3. **Average confidence per attribution result** (the third, self-chosen metric) — mean `confidence_score` within each result bucket, to show how decisive the system is in each category.
- **Why it helps:** surfaces calibration problems at a glance — e.g. if almost everything lands in `likely_ai`, or if the appeal rate is high, the thresholds are probably miscalibrated.
- **Blind spot:** the numbers describe the system's *own outputs*, not ground-truth accuracy; a low appeal rate could mean good calibration or simply that creators don't know they can appeal.
- **Verification:** generate several classifications across all three bands plus at least one appeal, call `/analytics`, and confirm the distribution sums to the total, the appeal rate matches by hand, and per-result averages are within their bands.

## Committed: Multi-Modal Support

Extend the pipeline to accept a second content type alongside text.

- **Second type:** **structured metadata about an image** (a creator-supplied image description plus fields like title, medium, and tags) — chosen because it stays within a text-analysis pipeline while exercising a genuinely different input shape.
- **Integration point:** add an optional `content_type` field to `POST /submit` (`"text"` default, or `"image_metadata"`). For `image_metadata`, the request carries a `metadata` object; the system serializes its describable fields into an analysis string and routes it to a parallel signal set (the LLM signal with an image-metadata-specific prompt; stylometrics applies only to the free-text description). The confidence-scoring, label, audit-log, and appeal layers are unchanged — they operate on the resulting scores regardless of input type.
- **Why it helps:** shows the architecture generalizes — the production layer (scoring, labels, appeals, audit) is content-type-agnostic and only the front of the pipeline branches.
- **Blind spot:** metadata is shorter and more formulaic than prose, so stylometric signal reliability drops further; the system leans even harder on the LLM for this type and flags the result accordingly.
- **Verification:** submit an `image_metadata` payload and confirm it produces a `content_id`, an attribution result, a label, and a normal audit-log entry tagged with its `content_type`; confirm a text submission still works unchanged.

## Stretch Implementation Order

These are built only after the required M3–M5 pipeline is verified end-to-end, in this order (lowest-risk first): (1) Ensemble detection — extends existing scoring; (2) Analytics dashboard — read-only over the audit log, no pipeline change; (3) Provenance certificate — new store + endpoint, additive to the response; (4) Multi-modal support — branches the front of the pipeline. Each is documented in the README (what was built and how it works) as it lands.
