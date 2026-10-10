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
| Error rate, CloudWatch alarms (deployed) | Safety metrics: PII leakage, prompt-injection attempts, toxicity, policy violations |

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
