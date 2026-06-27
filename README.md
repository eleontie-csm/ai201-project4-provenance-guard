# Provenance Guard

Backend service for classifying submitted text as likely AI-generated, likely human-written, or uncertain using a multi-signal pipeline. The system returns a transparent user-facing label, supports creator appeals, enforces submission rate limits, and writes structured audit events.

## Architecture Overview

Submission flow:
1. Client submits text and creator identity to POST /submit.
2. Input validation enforces required fields and minimum text length (150 chars).
3. Two independent signals run:
	 - LLM-based classifier (Groq)
	 - Stylometric heuristics (sentence-length variance, type-token ratio, punctuation density)
4. Signals are normalized to [0,1] and combined into a single confidence score.
5. Confidence score maps to one of three attribution categories and a plain-language transparency label.
6. Decision is appended to structured JSONL audit log.
7. API returns content_id, attribution, confidence, signal scores, label, and status.

Appeal flow:
1. Creator submits POST /appeal with content_id and creator_reasoning.
2. Service verifies requester identity (X-User-Id) and content ownership.
3. Content status is updated to under_review.
4. Appeal event is appended to structured audit log and linked by content_id.
5. API returns confirmation with updated status.

## Detection Signals

### Signal 1: LLM-Based Classification (Groq)
What it captures:
- High-level semantic and stylistic patterns often associated with AI-generated writing.

Why this signal was chosen:
- It captures holistic language behavior that hand-crafted metrics cannot reliably detect.

What it misses:
- Lightly edited AI text can look human.
- Formal human writing can appear AI-like.
- Prompt sensitivity can shift the score distribution.

Output:
- llm_score in [0,1], where higher means more likely AI-generated.

### Signal 2: Stylometric Heuristics
Metrics used:
- Sentence length variance
- Type-token ratio
- Punctuation density

What it captures:
- Structural variability, lexical diversity, and punctuation rhythm patterns.

Why this signal was chosen:
- Independent from semantic model judgment; deterministic and locally computable.

What it misses:
- Repetitive poetry/lyrics and short text can destabilize heuristics.
- Non-native writing style may skew type-token ratio.

Output:
- stylometric_score in [0,1], where higher means more likely AI-generated.

## Confidence Scoring

Combination rule:
- confidence = 0.50 * llm_score + 0.50 * stylometric_score
- confidence is clipped to [0,1]

Thresholds (balanced policy):
- likely_ai: confidence >= 0.75
- uncertain: 0.40 <= confidence < 0.75
- likely_human: confidence < 0.40

Uncertainty design:
- A score around 0.6 is intentionally mapped to uncertain, not likely_ai, to reduce false-positive harm to human creators.

Validation approach:
- I test with 4 fixed inputs: clearly AI-like, clearly human-like, and 2 borderline cases.
- I compare both individual signal scores and final confidence to verify meaningful score spread.

## Transparency Label (Exact Variants)

High-confidence AI:
"This content is likely AI-generated (high confidence). We may be wrong, but both language and writing-pattern signals strongly indicate AI assistance."

Uncertain:
"This content could not be confidently attributed. Signals are mixed, so this result is uncertain and should be interpreted with caution."

High-confidence Human:
"This content is likely human-written (high confidence). We may be wrong, but available signals do not strongly indicate AI generation."

## Appeals Workflow

Who can appeal:
- Original creator only in MVP.

Appeal request:
- content_id
- creator_reasoning (minimum 30 characters)
- header X-User-Id must match submission creator_id

System behavior:
- Validates content existence and ownership
- Updates status to under_review
- Logs appeal event in JSONL audit log
- Returns confirmation payload

## Rate Limiting

Applied on POST /submit:
- 10 requests per minute per IP
- 100 requests per day per IP

Reasoning:
- Supports normal creator behavior (occasional drafts/revisions).
- Limits flood/probing attempts from scripts.
- Demonstrable in API tests via HTTP 429 once threshold is exceeded.

## Audit Log

Format:
- JSON Lines (one JSON object per event)

Classification event fields:
- event_type, timestamp, content_id, creator_id
- attribution, confidence, llm_score, stylometric_score
- label, status

Appeal event fields:
- event_type, timestamp, content_id, creator_id
- appeal_reasoning, status (under_review)

The audit log is append-only and structured for easy demo and inspection.

## API Summary

- POST /submit
- POST /appeal
- GET /log
- GET /content/<id>
- GET /health
- GET /analytics (stretch)
- GET /dashboard (stretch)

## Setup

1. Create and activate a virtual environment.
2. Install dependencies from requirements.txt.
3. Create .env in repo root and set GROQ_API_KEY.

Example .env:

GROQ_API_KEY=your_key_here

## Run

Run the Flask app (entrypoint name depends on your implementation):

python app.py

or

flask --app app run

## Test Examples

Submit content:

curl -s -X POST http://localhost:5000/submit \
	-H "Content-Type: application/json" \
	-H "X-User-Id: test-user-1" \
	-d '{"text": "Your sample text here with at least 150 characters...", "creator_id": "test-user-1"}'

Submit appeal:

curl -s -X POST http://localhost:5000/appeal \
	-H "Content-Type: application/json" \
	-H "X-User-Id: test-user-1" \
	-d '{"content_id": "<content-id>", "creator_reasoning": "I wrote this myself based on personal experience and drafting notes."}'

View latest logs:

curl -s http://localhost:5000/log

View analytics summary (stretch):

curl -s http://localhost:5000/analytics

Open dashboard UI (stretch):

http://localhost:5000/dashboard

Auto-run full demo sequence (submit, content lookup, appeal, log, rate-limit burst):

BASE_URL=http://127.0.0.1:5050 ./scripts/demo_sequence.sh

## Known Limitations

1. Repetitive poetry and lyrics:
Stylometric metrics may overestimate AI-likeness because repetition can reduce lexical diversity and structural variability.

2. Formal academic human prose:
Highly consistent structure and polished style may increase both model and heuristic AI-likeness signals.

3. Lightly edited AI drafts:
Human post-editing can suppress obvious AI markers and move confidence into uncertain range.

## Spec Reflection

How the spec helped:
- Defining thresholds and label text early made implementation decisions concrete and prevented a vague "score-only" API.
- The architecture section and API contract in planning.md made it straightforward to implement and verify the full request lifecycle: submit -> scoring -> label -> log, then appeal -> under_review -> log.

Implementation divergence from plan and why:
- The plan started with M3 placeholder labels, but implementation moved directly to the final 3 production label variants during M5 so endpoint behavior matched the final product language earlier.
- The plan noted optional mock auth during design; implementation enforced creator ownership for appeals using X-User-Id immediately, which reduced abuse risk and made appeal behavior deterministic for testing.
- During validation, port 5000 was occupied on the machine, so end-to-end tests were run on port 5050. This did not change API behavior, only local execution configuration.

Concrete validation outcomes from this run:
- Four calibration submissions produced differentiated confidence scores (0.625, 0.35, 0.65, 0.60), matching intended spread between clear and borderline samples.
- Appeal flow was validated with a real submission: status changed to under_review on GET /content/<id> and an appeal event appeared in GET /log.
- Rate limiting was validated with 12 rapid submit requests: 200 for the first 9 and 429 for the final 3 in that window (one prior request had already consumed part of the per-minute quota).

## AI Usage

### Instance 1
Prompted AI to generate:
- Flask app scaffold with POST /submit, GET /log, request validation, UUID content IDs, and a Groq-based first signal function returning a normalized score.

AI output:
- A working baseline with JSON parsing for model output and initial structured log writes.

What I revised:
- Added explicit minimum length enforcement (150 chars), robust fallback behavior when GROQ_API_KEY is missing or API calls fail, and consistent [0,1] clipping for all score paths.
- Extended audit entries to include normalization metadata for debugging and grading visibility.

### Instance 2
Prompted AI to generate:
- Stylometric feature extraction (sentence-length variance, type-token ratio, punctuation density), confidence combination logic, and threshold-based attribution mapping.

AI output:
- Rule-band based stylometric scoring and 50/50 score combiner with three-category attribution output.

What I revised:
- Implemented exact threshold behavior from planning.md (likely_ai >= 0.75, uncertain 0.40-0.74, likely_human < 0.40).
- Added production-layer endpoints and behavior not present in the initial generated output: POST /appeal, GET /content/<id>, label text variants, creator-only appeal ownership checks, and Flask-Limiter configuration (10/min, 100/day with memory:// storage).

### Instance 3
Prompted AI to generate:
- Milestone 5 production integration: mapping confidence to final transparency labels, persistent content status storage, and appeal logging linked to original decisions.

AI output:
- Candidate route logic for status updates and log appends.

What I revised:
- Persisted content records in a local JSON index, added strict appeal reasoning minimum (30 chars), required X-User-Id for appeals, and ensured audit log events include both classification and appeal entries in structured JSONL format.

## Submission Checklist

- planning.md in repo root
- README with all required sections
- Demo video (3-5 minutes) showing:
	- submit response with attribution, confidence, and label
	- visible confidence difference across inputs
	- appeal submission and under_review status
	- rate limiting behavior (429)
	- audit log with at least 3 structured entries including an appeal