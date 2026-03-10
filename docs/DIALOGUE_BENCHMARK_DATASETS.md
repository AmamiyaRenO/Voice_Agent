# Dialogue Benchmark Datasets

This note shortlists public dialogue and assistant-style datasets that are worth using to test and improve the current Voice Agent conversation stack.

The goal here is not to download everything. The goal is to pick a small set of benchmarks that match the agent's real failure modes:

- unnatural acknowledgements
- memory contamination and persona drift
- weak clarification behavior
- poor task/intent robustness
- hallucinated grounded answers
- spoken-command paraphrase failures

## Recommended First Wave

These are the highest-value datasets for the current repo.

| Dataset | Best for | Why it fits this agent | Primary source |
|---|---|---|---|
| DailyDialog | casual multi-turn conversation, concise human phrasing, emotion/intent labels | Good baseline for whether replies sound like everyday conversation instead of robotic paraphrase | [DailyDialog](https://aclanthology.org/I17-1099/) |
| Persona-Chat | persona consistency, remembering self/user facts across turns | Directly useful for testing memory consistency and avoiding contradictory or awkward self-introductions | [Persona-Chat paper](https://aclanthology.org/P18-1205/) |
| EmpatheticDialogues | emotionally aware replies, short acknowledgements, better follow-up behavior | Useful for fixing flat or unnatural responses after the user shares a personal fact or frustration | [EmpatheticDialogues](https://aclanthology.org/P19-1534/) |
| Schema-Guided Dialogue (SGD) | task-oriented multi-domain assistant turns, clarification, slot carry-over | Closest to real virtual-assistant behavior among public benchmarks; good for command + follow-up turns | [Google Research SGD](https://research.google/pubs/towards-scalable-multi-domain-conversational-agents-the-schema-guided-dialogue-dataset/) |
| doc2dial | document-grounded answers, information-seeking turns, reduced hallucination | Good for grounded explain/help flows where the agent should answer from known material instead of inventing facts | [doc2dial](https://aclanthology.org/2020.emnlp-main.652/) |
| SLURP | spoken assistant commands, intents, slots, English audio/text pairs | Useful for spoken command robustness, especially if we want to test ASR + routing together | [SLURP](https://aclanthology.org/2020.emnlp-main.588/) |
| CLINC150 / oos-eval | intent classification and out-of-scope detection | Strong fit for "do not confidently answer garbage" and for deciding when to clarify instead of guessing | [CLINC150 paper](https://aclanthology.org/D19-1131/) and [official repo](https://github.com/clinc/oos-eval) |

## Recommended Second Wave

These are worth adding after the first wave is wired into the eval harness.

| Dataset | Best for | Why it fits this agent | Primary source |
|---|---|---|---|
| MultiWOZ 2.2 / 2.4 | task-oriented dialogue state tracking, slot carry-over, corrections | Useful if we want deeper multi-turn task memory and stricter follow-up testing | [MultiWOZ 2.2](https://aclanthology.org/2020.nlp4convai-1.13/) and [MultiWOZ 2.4](https://aclanthology.org/2022.sigdial-1.34/) |
| Wizard of Wikipedia | knowledge-grounded open-domain conversation | Useful for "explain X" style conversations that should stay grounded and not improvise | [Wizard of Wikipedia](https://aclanthology.org/D18-1255/) |
| MASSIVE | large-scale assistant NLU, intents + slots, multilingual assistant utterances | Good for broader intent coverage and paraphrase stress tests; less urgent if we stay English-only for now | [Amazon MASSIVE](https://www.amazon.science/code-and-datasets/massive) |
| SGD-X | robustness to schema and phrasing variation | Strong follow-up after SGD if we want harder paraphrase and generalization tests | [SGD-X](https://research.google/pubs/sgd-x-a-benchmark-for-robust-generalization-in-schema-guided-dialogue-systems/) |
| PRESTO | realistic task-oriented utterances with speech-like phenomena | Useful for assistant requests with more natural messiness than clean benchmark text | [PRESTO](https://research.google/blog/presto-a-multilingual-dataset-for-parsing-realistic-task-oriented-dialogues/) |
| SaFeRDialogues | recovery after user correction, feedback, or failure | Good for "that is wrong", "you misheard me", and graceful repair behavior | [SaFeRDialogues](https://aclanthology.org/2022.acl-long.447/) |

## Mapping To Current Repo

Use the datasets by failure mode, not by paper popularity.

### 1. Human-sounding short replies

Use:

- DailyDialog
- EmpatheticDialogues

Convert to our eval format:

- Keep the dialogue history as context.
- Use the next user turn as `text`.
- Score the agent on:
  - brevity
  - whether it directly answers instead of paraphrasing the user
  - whether it asks a follow-up only when needed

What to improve from failures:

- system prompt rules for acknowledgement style
- short-turn templates
- clarification policy

### 2. Memory consistency and fact handling

Use:

- Persona-Chat
- EmpatheticDialogues
- DailyDialog self-disclosure turns

Convert to our eval format:

- Feed profile statements as user turns.
- Ask follow-ups such as:
  - `What do you know about me?`
  - `What is my name?`
  - `What did I say about ...?`
- Check:
  - no assistant persona leakage into user memory
  - no impossible names/origins
  - no fact drift across turns

What to improve from failures:

- fact extraction gates
- invalid-memory cleanup
- memory summary formatting

### 3. Clarification instead of guessing

Use:

- SGD
- CLINC150
- SaFeRDialogues

Convert to our eval format:

- For near-intent but ambiguous utterances, verify the agent asks one short clarification question.
- For out-of-scope utterances, verify the agent does not hallucinate a confident answer.

What to improve from failures:

- low-confidence ASR guard
- OOS / unsupported-intent routing
- clarification templates

### 4. Grounded explanations

Use:

- doc2dial
- Wizard of Wikipedia

Convert to our eval format:

- Provide the grounding text as the allowed knowledge source.
- Ask explanatory questions.
- Check:
  - answer stays within source facts
  - no invented benefits or fabricated domain details

What to improve from failures:

- game/document grounding path
- answer compression after grounding
- hallucination guardrails

### 5. Spoken assistant commands and paraphrases

Use:

- SLURP
- CLINC150
- MASSIVE
- PRESTO

Convert to our eval format:

- Use the text side first for routing tests.
- Use the audio side of SLURP later for ASR + routing end-to-end tests.
- Measure:
  - route accuracy
  - slot extraction
  - OOS detection
  - paraphrase robustness

What to improve from failures:

- command grammar
- ASR hotwords
- route confidence thresholds

## Suggested Rollout For This Repo

### Phase 1

Wire in small curated subsets first.

- DailyDialog: 50 cases for natural short replies
- Persona-Chat: 50 cases for memory consistency
- CLINC150: 100 cases for supported vs unsupported intent
- SLURP: 50 text cases for spoken-style commands
- doc2dial: 30 grounded QA cases

This phase should plug directly into the current `scripts/conversation_eval.py` and `scripts/memory_eval_scenarios.sample.json` style workflows.

### Phase 2

Add harder multi-turn benchmarks.

- SGD curated subset
- MultiWOZ curated subset
- SaFeRDialogues repair subset

This phase is where we measure clarification quality and multi-turn slot carry-over.

### Phase 3

Add audio and robustness stress tests.

- SLURP audio
- PRESTO realistic task phrasing
- selected local recordings from `Sound Recordings`

This phase is where we compare:

- `live-captions`
- `moonshine`
- backend `api`

against the same command/intent set.

## What I Would Start With Here

For this codebase, the best first batch is:

1. DailyDialog
2. Persona-Chat
3. CLINC150
4. SLURP
5. doc2dial
6. SaFeRDialogues

Reason:

- they map directly to the current problems we have actually seen in testing
- they do not require us to redesign the full stack before getting signal
- they cover both "sound more human" and "stop making brittle assistant mistakes"

## Practical Note

Do not use exact-string response matching for most of these datasets.

For open-ended dialogue, use rubric-based checks such as:

- directness
- acknowledgment quality
- contradiction / persona consistency
- groundedness
- clarification when uncertain
- unsupported-intent refusal or redirection

Keep exact matching mainly for:

- route labels
- game/intent execution
- memory fact recall
- slot/value extraction
