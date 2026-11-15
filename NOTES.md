# Knowledge Hackathon

The evaluator imports `solution.py` and calls `build()`. The returned object implements `ingest(snapshot)`, `answer(question, as_of=None)`, and `conflicts()`. The system runs offline using Python 3.11 and the evaluator's `contract.py`. It uses no third-party packages, paid APIs, API keys, or live website requests.

## Evidence and time

Each full crawl has its own in-memory index. A dated question uses the latest capture on or before the requested day. An undated question uses the newest ingest date. Every answer with evidence includes its URL, capture ID, and a short quote. An older fee schedule can support an answer when the selected capture has no fee schedule and the older document was already effective. Removed offers and loan or deposit rate tiers do not carry forward.

Markdown rate tables are read into claims keyed by product, audience, metric, term, variant, and tier. Effective dates and explicit offer deadlines determine whether a claim is eligible. Product tables and official fee schedules take precedence over copies on calculators and promotion pages. Within a source class, the stated effective date breaks ties. The selected table supplies the published tiers. When a conventional mortgage amount falls within multiple published caps, the smallest applicable cap controls. A question about a specific missing term, tier, branch schedule, or product returns a low-confidence answer.

`conflicts()` compares values within the same scope and retains comparable earlier generations. Vehicle model-year bands can be compared where they overlap. Dividend rate, APY, APR, introductory offers, and standard terms have distinct identifiers. Each conflict reports evidence for the competing values, the selected value, and its rationale. Member-service answers also use the branch directory, dated official announcements, and FAQ headings. When a dated article is the available source for hours, the answer names the branch and directs the member to the locator for later changes.

## Run the supplied evaluator

Keep the original participant kit in a separate directory. From the top of the extracted participant kit, run these commands with the submission clone's absolute path.

```bash
SUBMISSION_DIR=/absolute/path/to/knowledge-hackathon
python3.11 -m pip install -r "$SUBMISSION_DIR/requirements.txt"
python3.11 harness/run_eval.py --submission "$SUBMISSION_DIR" --eval eval/public_eval.jsonl

python3.11 harness/run_eval.py --submission "$SUBMISSION_DIR" \
  --snapshots harness/selftest/snapshots \
  --eval harness/selftest/eval.jsonl \
  --conflicts-gold harness/selftest/conflicts_gold.json
```

The repository contains implementation files only. The evaluator, practice questions, and website data come from the participant kit.

## Evaluation notes

The public evaluator's default scoring gives no automatic credit to a free-text, non-refusal item with an empty `must_include` list. The implementation answers that item with the published certificate table. The volunteer question asks for last year's employee count, and the available evidence supports a count for an earlier year. The answer gives that count with its year and marks the requested year as unconfirmed. Public evaluation omits the organizer's hidden conflict gold file. The supplied self-test exercises that scoring path.
