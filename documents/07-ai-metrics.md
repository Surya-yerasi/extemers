# Measuring an AI application: a field guide

This guide explains the metrics behind docqa's **Metrics** tab, and the reasoning a senior
engineer applies to them. Each metric's exact definition and formula also appear in the app
(the ⓘ next to every number, and the *Metric guide* view), from
`services/docqa/src/docqa/domain/metrics_glossary.py`.

## 1. Four layers, two loops

An LLM application can fail at any of four layers. Each layer needs its own metrics:

| Layer | Question it answers | docqa examples |
|---|---|---|
| **System** | Is it up, fast and affordable? | error rate, latency P50/P95/P99, cost per question |
| **Inference** | How hard are the models working? | tokens in/out, context size, generation speed, model calls |
| **Retrieval** | Did the right evidence reach the model? | hit@k, recall@k, precision@k, MRR, nDCG (offline); top-1 similarity, margin (online) |
| **Generation** | Is the answer correct, grounded and useful? | correctness, refusal accuracy, citations, faithfulness (offline); abstention, cited-answer rate, 👍 (online) |

Three more views sit on top of these four:
- **Agent:** does the agent finish its task, how directly, and does its retry loop pay off? (§10)
- **Conversation:** did the user leave with what they came for, and were they satisfied? (§11)
- **Safety:** were attacks blocked and identifiers kept out of answers? (§12)

The Metrics tab opens with a **scorecard** that puts one row per metric from every layer
against a starting target, so you can see where the application stands at a glance.

The layers multiply. If retrieval misses the evidence, no model can answer correctly: hit@5
is an **upper bound** on answer quality. That is why RAG teams debug bottom-up.

They are measured in **two loops**:

| | Offline (evaluation) | Online (production) |
|---|---|---|
| Data | A golden set: questions with known answers and evidence | Real traffic: no answer key |
| Measures | **Quality**: is it right? | **Behaviour**: is it fast, cheap, stable, liked? |
| When | Before a change ships (CI gates, A/B decisions) | After it ships (dashboards, alarms, drift) |
| docqa | `make docqa-local-eval` → *Offline evaluation* | every question → *Live traffic*, CloudWatch |

The loops feed each other. A 👎 or an odd trace online becomes a new golden question offline,
so the same failure can never come back unnoticed. Most mature AI teams treat **"every
production failure becomes a test case"** as a rule.

## 2. Latency: percentiles, not averages

**Never report mean latency.** Response times are skewed: one 30 s agent retry next to
twenty 2 s answers puts the mean at 3.3 s, a time nobody experienced. Use percentiles:

| | Meaning | Use |
|---|---|---|
| **P50** (median) | half the questions were faster | the typical experience |
| **P95** | 1 in 20 was slower | SLO targets, alarms (docqa alarms on P95 > 20 s) |
| **P99** | 1 in 100 was slower | timeouts, capacity; cold starts and retries live here |

docqa uses **nearest-rank** percentiles, so the value is always one that actually happened.
P99 of 30 questions is simply the slowest one; percentiles need volume to mean anything.

**Tail ratio (P95 / P50)** shows how predictable the system is. In the local test traffic it
was 2.8, driven by the agent (4 model calls) and by model loading (embed P95 1.6 s against a
P50 of 38 ms: bge-m3 being swapped back into memory).

**Per-step latency** says where to optimise. In that traffic, `generate` was 62% of all time
and `grade` (agent only) 23%; retrieval was under 1%. Speeding up search would change
nothing (Amdahl's law). A smaller answer model, or fewer agent steps, would.

**What production LLM teams add (docqa doesn't stream yet):**
- **TTFT** (time to first token): what users perceive as speed in a streaming UI.
- **TPOT / ITL** (time per output token): reading speed once text starts.
- **Throughput**: requests or tokens per second per replica, which drives capacity planning.

## 3. Inference: tokens are the unit of cost and time

An LLM call has two phases:
- **Prefill:** reading the prompt. This is parallel and fast per token, but it grows with context.
- **Decode:** writing the answer, one token at a time. This is sequential and slow per token.

| Metric | Why it matters |
|---|---|
| Input tokens per question (about 1,450 locally) | Cost and prefill time. Mostly retrieved context: tune `top_k` and chunk size against offline quality |
| Output tokens per question (about 41) | Decode time and the pricier side of the bill |
| Context size (generate input, about 1,000) | Longer contexts are slower and costlier, and can bury the answer ("lost in the middle") |
| Generation speed (tokens/s) | Model and hardware health. docqa's figure includes prefill, so it understates pure decode speed |
| Model calls per question (1 to 1.5 fixed; 4.25 agent) | The main lever on agent latency and cost |
| Cost per question | Unit economics: multiply by expected volume before choosing a model |

## 4. Retrieval quality (offline)

Every golden question lists its **evidence**: a document plus a phrase that must be in the
chunk. A retrieved chunk is *relevant* if it contains that evidence. Take one ranked list
where the right chunk is at rank 2, with one evidence item needed:

| Metric | Value | How |
|---|---|---|
| hit@1 | 0 | no relevant chunk at rank 1 |
| hit@5 | 1 | a relevant chunk is in the top 5 |
| recall@5 | 1.0 | 1 of 1 evidence items found |
| precision@5 | 0.2 | 1 relevant of 5 slots (capped by how many relevant chunks exist) |
| MRR | 0.5 | 1 / rank of the first relevant chunk |
| nDCG@5 | 0.63 | 1/log2(3) divided by the ideal 1/log2(2) |

- **hit@k / recall@k:** did the evidence reach the model at all? Recall differs for
  multi-document questions, which need *every* fact.
- **precision@k:** how much noise came with it. Compare strategies with it, not against 1.0.
- **MRR / nDCG:** how *high* it ranked. They matter because models attend unevenly to their
  context, and because the agent and rerankers try to improve exactly this.

docqa baseline: hybrid hit@5 is 1.00 but hit@1 only 0.86. The evidence is always in the
context, but not always first. That gap is what the agent (0.93) and the BGE reranker test
(0.93 at rank 1) close.

## 5. Generation quality (offline)

| Metric | Catches |
|---|---|
| **Correct** (deterministic: expected facts present, forbidden ones absent) | wrong answers |
| **Refusal accuracy** | hallucination on unanswerable questions (passport number, GRE score) |
| **False refusal rate** | over-caution: "not found" when the answer was there |
| **Citation hit / precision** | answers the user cannot verify, or wrong sources |
| **Faithfulness** (RAGAS, LLM judge) | claims not supported by the retrieved context, even if correct by luck |
| **Answer relevancy / correctness** (judge) | evasive answers; correct answers in unexpected wording |
| **Context precision / recall** (judge) | label-free cousins of the retrieval metrics |

Refusal accuracy and false refusals trade off: a system can always score 100% on one by
failing the other. Watch both.

**LLM-as-judge has known biases:**
- It prefers longer answers, and answers from its own model family.
- It is non-deterministic, and its scores drift between judge versions.

Calibrate it on a handful of hand-checked answers, use a different (ideally stronger) model
than the one being judged, and trust only large differences. docqa's deterministic metrics
run on every eval; the judge is opt-in (`JUDGE=local`).

## 6. Quality signals without an answer key (online)

In production nobody tells you the right answer, so teams watch **proxies** and treat shifts
as investigation triggers, not verdicts:

| Signal | Healthy | Investigate when |
|---|---|---|
| Abstention rate | stable, non-zero | rises sharply (retrieval or index broke) or hits 0% (model stopped admitting ignorance) |
| Cited answer rate | ~100% | any uncited answer: the cheapest hallucination signal there is |
| Top-1 similarity | stable per embedding model | drops (new kinds of questions, broken index) |
| Top-1 margin | context | small margins mean near-duplicates competed (docqa: 0.038, the look-alike transcripts) |
| Empty retrieval rate | ~0% | BM25 with no shared words, or an empty index |
| Helpful rate (👍) | high, with enough coverage | 👎 clusters: open those traces first |
| Error rate | ~0% | any sustained rate; every failed question has a trace |

Feedback is biased (people rate when annoyed or delighted) and sparse. Read **feedback
coverage** before trusting the helpful rate.

## 7. Statistics: how big is a real difference?

With *n* questions and a correct rate *p*, the standard error is about sqrt(p(1-p)/n). For
docqa's golden set (n = 49, p ≈ 0.96) that is about 2.8 points, so a 95% interval is about
**±5.6 points**. The agent's 98.0% vs hybrid's 95.9% (one question) is inside that noise. Its
retrieval gain (hit@1 0.86 → 0.93, three questions of 43) is closer to real, but not proof.

How teams get trustworthy comparisons:
- **Paired comparisons:** the same questions through both variants, counting wins and losses
  per question, are far more sensitive than comparing two averages.
- **More questions where it matters:** a private golden set of real questions, and more of
  the hard categories.
- **Repeat runs** to measure run-to-run variance (docqa saw 1-2 questions of drift).
- **Goodhart's law:** once a metric becomes a target it stops measuring well. Rotate in new
  golden questions; never tune prompts on the exact questions you report.

## 8. What this dashboard has, and what production would add

| Have (local and deployed) | Production AI teams also track |
|---|---|
| Latency P50-P99, per step, per day | TTFT and TPOT (needs streaming); per-endpoint SLOs and error budgets |
| Tokens, model calls, cost per question | Prompt-cache hit rate; cost per tenant/feature; GPU utilisation for self-hosted models |
| Abstention, citations, similarity signals | Drift monitors on these signals with alarms; topic clustering of questions |
| 👍/👎 per answer, linked to traces | A/B tests with guardrail metrics; human review queues sampled from traffic |
| Offline retrieval and answer metrics, LLM judge | Larger, versioned golden sets; per-category release gates in CI |
| Error rate, CloudWatch alarms (deployed) | Model-based safety classifiers (toxicity, jailbreak detection, PII), policy violation reviews |
| Rule-based guardrails with violation and block rates | Red-team test sets run in CI; per-rule false-positive review |
| A sampled LLM judge on live answers | Judge calibration sets; per-segment (topic, user type) quality dashboards |

**Online metrics are computed from stored traces.** That is ideal for learning, and fine for
one user. At scale you would aggregate as you write (CloudWatch EMF metrics, which docqa
already emits, or a metrics pipeline), and keep traces for drill-down only.

## 9. A daily routine with this dashboard

1. **Live traffic → KPI row:** anything off (error rate, P95, abstention)? Filter by strategy.
2. **Time per step:** if latency moved, which step moved?
3. **👎 and uncited answers:** open their traces (Traces tab), find the failing step, and add the
   question to the golden set if it is a real gap.
4. **Before changing anything** (model, prompt, chunking, strategy): run
   `make docqa-local-eval` and compare the runs in *Offline evaluation → Across runs*. Ship only
   changes that beat the noise.

## 10. Agent metrics

An agent is judged on the **task**, not the single answer. docqa's agent runs
plan → search → grade → (search → grade) → answer → (verify), and every step is a span in the
trace, so these metrics are read straight from traces.

| Metric | docqa definition | Watch for |
|---|---|---|
| **Task completion rate** | Online: answered and not rated 👎. Offline: answered correctly | The bottom line; read it next to cost |
| **Tool call accuracy** | The agent has one tool (search), so tool *choice* is always right. What can go wrong is the query, so this is **search accuracy**: the share of searches its grader accepted | Low values mean poor planner queries, or documents that lack the answer |
| **Step count** (mean, P95) | Top-level agent steps per task | A rising P95 means some tasks loop |
| **Trajectory efficiency** | Fewest possible steps (4, or 5 with verify) ÷ steps taken | 1.0 means the first search was enough |
| **Error recovery rate** | Of tasks whose first search failed, the share the retry rescued | Whether the retry loop earns its cost |
| **Cost per task** (mean, P95) | Model cost of all steps | The price side of the agent trade-off |

Read efficiency and recovery together: a retry lowers efficiency but may be what saved the
answer. A good agent has high recovery and high efficiency (few retries needed).

## 11. Chatbot metrics

| Metric | Definition | Notes |
|---|---|---|
| **Response relevance** | Same as answer relevance (§5) | Counted once |
| **Coherence** | A judge scores clarity and consistency 1-5 against a rubric (shown as 0-1) | Low signal for short cited answers; judges also confuse "unhelpful" with "incoherent" |
| **Conversation completion** | Share of conversations whose last question was answered and not rated 👎 | A proxy: users also leave satisfied after "not found", or give up mid-way |
| **Hallucination rate** | Share of answers with at least one unsupported claim (faithfulness < 1); offline it also counts answering an unanswerable question | Strict by design; read the traces behind it |
| **CSAT** | Share of 👍 among rated answers (a two-point CSAT) | Classic CSAT asks 1-5 and counts 4-5; same idea |

## 12. Guardrails and safety

Rule-based checks run on every question and answer, each recorded as a span
(`guard_input`, `guard_output`) with what fired:

| Rule | Stage | Action |
|---|---|---|
| `prompt_injection` | question | **blocked**: no retrieval, no model call ("ignore previous instructions", "show your system prompt") |
| `sensitive_id` | answer | **redacted**: SSN-shaped and Luhn-valid card numbers become `[redacted]` |
| `uncited_answer` | answer | **flagged**: an answer with no citation |

**Guardrail violation rate** = questions with any finding ÷ questions; **block rate** = blocked
÷ questions. Rules are cheap and explainable but miss rephrased attacks and can misfire; that
is why production adds model-based classifiers (e.g. Bedrock Guardrails) and reviews findings.

## 13. Judging live traffic

The LLM judge needs no answer key for **faithfulness, answer relevance, context precision**
(judged against the answer instead of a reference) and **coherence**, so it can score real
answers. **Context recall** and **answer correctness** need a reference and stay offline.

Judging an answer costs several model calls (about 35 s per answer locally), so teams **sample
and score asynchronously** instead of judging inline:

```bash
make docqa-local-judge                 # the newest 20 unjudged answers from the last 7 days
make docqa-local-judge WINDOW=all LIMIT=50 RESCORE=1
```

Scores are stored on each question next to its trace; the Metrics tab averages them and shows
**judge coverage** (how much of the traffic the sample covers). With fewer than ~30 judged
answers, treat the averages as direction, not measurement.

**A real example from the first run (2026-10-12):** "What is GPA?" was answered from the
model's general knowledge ("Grade Point Average is…") while citing three transcripts that do not
define it. The judge scored context precision 0 and faithfulness 0.67: a hallucination with
real-looking citations, invisible to every deterministic metric.
