"""What every dashboard metric means, how it is computed, and how to act on it.

The Metrics tab shows these next to each number; documents/07-ai-metrics.md teaches the
concepts behind them. One source, so the explanation always matches the computation.
"""

from typing import Literal

from pydantic import BaseModel

Kind = Literal["online", "offline"]
Better = Literal["higher", "lower", "target", "context"]


class MetricInfo(BaseModel):
    name: str
    group: str
    kind: Kind  # online: from real traffic, no answer key; offline: scored against the golden set
    definition: str
    formula: str
    why: str  # what decision it informs
    better: Better
    caveat: str = ""


def _m(  # noqa: PLR0913, PLR0917 - one glossary row
    name: str,
    group: str,
    kind: Kind,
    definition: str,
    formula: str,
    why: str,
    better: Better,
    caveat: str = "",
) -> MetricInfo:
    return MetricInfo(
        name=name,
        group=group,
        kind=kind,
        definition=definition,
        formula=formula,
        why=why,
        better=better,
        caveat=caveat,
    )


GLOSSARY: dict[str, MetricInfo] = {
    # ---------------------------------------------------------------- online: traffic & outcome
    "questions": _m(
        "Questions",
        "Traffic",
        "online",
        "Questions asked in the window, including failed ones.",
        "count(turns)",
        "The denominator for every rate below. Small numbers make every rate noisy.",
        "context",
        "With fewer than ~30 questions, treat percentages as anecdotes.",
    ),
    "answer_rate": _m(
        "Answer rate",
        "Outcome",
        "online",
        "Share of questions that got an answer (not 'not found', not an error).",
        "answered / questions",
        "Coverage: how often the system has something to say. Falling answer rate after a "
        "change usually means retrieval got worse or the prompt got stricter.",
        "higher",
        "Higher is only better if the answers are right: a system that never abstains has a "
        "100% answer rate and hallucinates. Read it next to the offline refusal metrics.",
    ),
    "not_found_rate": _m(
        "Abstention rate ('not found')",
        "Outcome",
        "online",
        "Share of questions answered with 'I couldn't find that in your documents.'",
        "not_found / questions",
        "Abstaining is the correct behaviour for questions the documents cannot answer. A "
        "sudden rise means retrieval is missing things; a fall to zero can mean the model "
        "stopped admitting ignorance.",
        "target",
        "Online you cannot tell correct from wrong abstentions; the offline refusal "
        "accuracy and false-refusal rate can.",
    ),
    "error_rate": _m(
        "Error rate",
        "Outcome",
        "online",
        "Share of questions that failed (model throttled, provider down, timeouts).",
        "failed / questions",
        "Reliability. The first number an on-call engineer checks; every failed question "
        "has a trace showing which step failed.",
        "lower",
    ),
    "helpful_rate": _m(
        "Helpful rate (👍)",
        "Outcome",
        "online",
        "Share of rated answers that got a thumbs up.",
        "👍 / (👍 + 👎)",
        "The only online signal of usefulness, as opposed to speed or behaviour. Each 👎 is a "
        "candidate for the golden set: open its trace, find the cause, add the question.",
        "higher",
        "Biased: people rate when annoyed or delighted. Read it with feedback coverage.",
    ),
    "feedback_coverage": _m(
        "Feedback coverage",
        "Outcome",
        "online",
        "Share of answered questions that were rated at all.",
        "rated / answered",
        "Tells you how much to trust the helpful rate.",
        "higher",
    ),
    # ---------------------------------------------------------------- online: latency
    "latency_p50": _m(
        "Latency P50 (median)",
        "Latency",
        "online",
        "Half of the questions were answered faster than this.",
        "50th percentile of end-to-end time",
        "The typical experience.",
        "lower",
        "Never use the mean for latency: one 30 s outlier moves it a lot and describes nobody.",
    ),
    "latency_p95": _m(
        "Latency P95",
        "Latency",
        "online",
        "95% of questions were faster than this; 1 in 20 was slower.",
        "95th percentile of end-to-end time (nearest rank)",
        "The usual target for SLOs: tail latency is what users remember and what alarms watch.",
        "lower",
        "Needs volume: P95 of 10 questions is just the slowest one.",
    ),
    "latency_p99": _m(
        "Latency P99",
        "Latency",
        "online",
        "99% of questions were faster than this.",
        "99th percentile of end-to-end time",
        "Worst-case planning (timeouts, Lambda limits). Cold starts and retries live here.",
        "lower",
        "Below ~100 samples this is simply the maximum.",
    ),
    "tail_ratio": _m(
        "Tail ratio (P95 / P50)",
        "Latency",
        "online",
        "How much slower the slow questions are than typical ones.",
        "P95 / P50",
        "A ratio near 1 means predictable latency. A large ratio points at variable work: "
        "agent retries, cold starts, long contexts, provider throttling.",
        "lower",
    ),
    "stage_latency": _m(
        "Time per step",
        "Latency",
        "online",
        "P50/P95 of each pipeline step (rewrite, embed, searches, fuse, rerank, plan, grade, "
        "generate, verify), from the traces.",
        "percentile of span duration, per span name, summed within a question",
        "Shows where to optimise. In RAG, generation usually dominates; if retrieval does, "
        "check the index (ANN vs exact) or the network to the vector store.",
        "lower",
    ),
    "latency_share": _m(
        "Share of time",
        "Latency",
        "online",
        "Each step's share of total time across all questions.",
        "sum(step time) / sum(total time)",
        "Amdahl's law: halving a step that takes 5% of the time saves 2.5%.",
        "context",
    ),
    # ---------------------------------------------------------------- online: inference
    "input_tokens": _m(
        "Input tokens per question",
        "Inference",
        "online",
        "Tokens sent to language models per question, all steps together (rewrite, plan, "
        "grade, generate, verify).",
        "sum of *_input tokens / answered",
        "Drives cost and time-to-first-token. Mostly the retrieved context.",
        "lower",
        "Embedding tokens are estimated (characters / 4); model tokens are reported by the model.",
    ),
    "output_tokens": _m(
        "Output tokens per question",
        "Inference",
        "online",
        "Tokens generated per question, all steps together.",
        "sum of *_output tokens / answered",
        "Output tokens are slower and pricier than input tokens (decode is sequential).",
        "lower",
    ),
    "context_tokens": _m(
        "Answer context size",
        "Inference",
        "online",
        "Input tokens of the answer-generation call: instructions + retrieved chunks + question.",
        "mean(generate_input)",
        "Larger contexts cost more, run slower and can bury the answer ('lost in the middle'). "
        "Tune top_k and chunk size against offline quality.",
        "context",
    ),
    "output_tps": _m(
        "Generation speed",
        "Inference",
        "online",
        "Output tokens per second of the answer-generation call (decode throughput).",
        "generate_output / generate seconds",
        "Model and hardware health. A drop with the same model means contention, throttling "
        "or a cold model; locally it depends on what else is using the GPU.",
        "higher",
        "Includes prompt processing time (no streaming), so it understates pure decode speed.",
    ),
    "model_calls": _m(
        "Model calls per question",
        "Inference",
        "online",
        "How many language-model calls a question needed (1 for fixed strategies; 3-6 for the "
        "agent).",
        "count of spans that called a generation model",
        "The main driver of agent latency and cost.",
        "lower",
    ),
    "cost_per_question": _m(
        "Cost per question",
        "Cost",
        "online",
        "Estimated model cost per question from token counts and the price table.",
        "sum(tokens x price) / questions",
        "Unit economics: multiply by expected volume before choosing a model or strategy.",
        "lower",
        "Local models are $0. Infrastructure (Lambda, S3, KMS) is not included.",
    ),
    # ---------------------------------------------------------------- online: retrieval signals
    "top1_similarity": _m(
        "Top-1 similarity",
        "Retrieval (online)",
        "online",
        "Cosine similarity between the question and the best dense match.",
        "1 - cosine distance of the first dense result",
        "A confidence proxy without labels: low similarity often means the answer is not in "
        "the documents, so a drop across many questions suggests an indexing problem or new "
        "kinds of questions.",
        "higher",
        "Scales differ per embedding model; compare like with like. BM25-only questions have none.",
    ),
    "similarity_margin": _m(
        "Top-1 margin",
        "Retrieval (online)",
        "online",
        "How far the best dense match is ahead of the second best.",
        "similarity(1st) - similarity(2nd)",
        "A small margin means near-duplicates competed (the look-alike transcripts); that is "
        "where rerankers and the agent's grading help.",
        "context",
    ),
    "source_diversity": _m(
        "Sources in context",
        "Retrieval (online)",
        "online",
        "Distinct documents among the chunks given to the model.",
        "mean(distinct doc_id in final chunks)",
        "Multi-document questions need more than one; single facts need one. Very high "
        "diversity with low similarity usually means retrieval is guessing.",
        "context",
    ),
    "empty_retrieval_rate": _m(
        "Empty retrieval rate",
        "Retrieval (online)",
        "online",
        "Share of questions where retrieval returned nothing (no model call is made).",
        "questions with 0 chunks / questions",
        "Usually BM25 with no shared words, or an empty index.",
        "lower",
    ),
    "rewrite_rate": _m(
        "Follow-up rewrite rate",
        "Retrieval (online)",
        "online",
        "Share of questions that were follow-ups and were rewritten into standalone questions.",
        "rewritten / questions",
        "How conversational usage is; rewrites add a model call and are a source of errors "
        "(check 'Searched as' in traces).",
        "context",
    ),
    "agent_retry_rate": _m(
        "Agent corrective-search rate",
        "Retrieval (online)",
        "online",
        "Share of agent questions where the grader asked for a second search.",
        "agent questions with 2 retrieve rounds / agent questions",
        "High rates cost latency; if offline quality does not improve with them, the grader "
        "is too strict.",
        "context",
    ),
    "cited_answer_rate": _m(
        "Cited answer rate",
        "Generation (online)",
        "online",
        "Share of answers that cite at least one source.",
        "answers with >= 1 citation / answered",
        "Every factual answer here must cite. An uncited answer is the cheapest online "
        "hallucination signal there is: open those traces first.",
        "higher",
    ),
    "citations_per_answer": _m(
        "Citations per answer",
        "Generation (online)",
        "online",
        "Mean number of distinct sources cited per answer.",
        "mean(len(citations)) over answered",
        "Single facts should cite 1; comparisons 2+.",
        "context",
    ),
    "answer_length": _m(
        "Answer length",
        "Generation (online)",
        "online",
        "Mean answer length in characters.",
        "mean(len(answer))",
        "Drift detector: a prompt or model change that doubles length changes cost, latency "
        "and how users read answers.",
        "context",
    ),
    # ---------------------------------------------------------------- offline: retrieval
    "hit@1": _m(
        "Hit@1",
        "Retrieval (offline)",
        "offline",
        "Share of questions where the very first retrieved chunk contains the evidence.",
        "questions with a relevant chunk at rank 1 / questions",
        "What a user of a 'top answer' experience sees; the strictest ranking metric.",
        "higher",
    ),
    "hit@5": _m(
        "Hit@5",
        "Retrieval (offline)",
        "offline",
        "Share of questions with at least one relevant chunk in the top 5 (the model's context).",
        "questions with a relevant chunk in ranks 1-5 / questions",
        "If the evidence is not in the context, no model can answer correctly: an upper bound "
        "on answer quality.",
        "higher",
    ),
    "recall@5": _m(
        "Recall@5",
        "Retrieval (offline)",
        "offline",
        "Share of the needed evidence items found in the top 5.",
        "evidence items covered / evidence items needed",
        "Differs from hit@5 for multi-document questions, which need every fact.",
        "higher",
    ),
    "precision@5": _m(
        "Precision@5",
        "Retrieval (offline)",
        "offline",
        "Share of the top 5 chunks that are relevant.",
        "relevant chunks in top 5 / 5",
        "Noise in the context: irrelevant chunks cost tokens and can mislead the model.",
        "higher",
        "Capped by the number of relevant chunks: a one-fact question with one relevant chunk "
        "scores at most 0.2. Compare strategies, not against 1.0.",
    ),
    "mrr": _m(
        "MRR (mean reciprocal rank)",
        "Retrieval (offline)",
        "offline",
        "Average of 1 / rank of the first relevant chunk (1 at rank 1, 0.5 at rank 2, ...).",
        "mean(1 / first relevant rank)",
        "One number for 'how high is the first good result', rewarding rank 1 most.",
        "higher",
    ),
    "ndcg@5": _m(
        "nDCG@5",
        "Retrieval (offline)",
        "offline",
        "Normalised discounted cumulative gain: rewards relevant chunks, more when ranked "
        "higher, scaled so a perfect ranking scores 1.",
        "DCG / ideal DCG, DCG = sum(gain_i / log2(i + 1))",
        "The standard ranking metric in search; handles several relevant items.",
        "higher",
    ),
    # ---------------------------------------------------------------- offline: answers
    "correct": _m(
        "Correct",
        "Answers (offline)",
        "offline",
        "Answerable: contains every expected fact and none of the forbidden ones. "
        "Unanswerable: correctly said 'not found'.",
        "correct questions / questions",
        "The headline quality number; deterministic and free to compute.",
        "higher",
        "String matching: a correct answer worded unexpectedly can fail. Spot-check failures.",
    ),
    "refusal_accuracy": _m(
        "Refusal accuracy",
        "Answers (offline)",
        "offline",
        "Share of unanswerable questions correctly answered with 'not found'.",
        "refused unanswerable / unanswerable",
        "Measures hallucination resistance directly.",
        "higher",
    ),
    "false_refusal_rate": _m(
        "False refusal rate",
        "Answers (offline)",
        "offline",
        "Share of answerable questions wrongly answered with 'not found'.",
        "refused answerable / answerable",
        "The cost of caution; trades off against refusal accuracy.",
        "lower",
    ),
    "citation_hit": _m(
        "Citation hit",
        "Answers (offline)",
        "offline",
        "Share of answered questions citing at least one correct document.",
        "answers citing an evidence document / answered",
        "Attribution: can the user verify the answer from its citation?",
        "higher",
    ),
    "citation_precision": _m(
        "Citation precision",
        "Answers (offline)",
        "offline",
        "Share of citations pointing to an evidence document.",
        "correct citations / citations",
        "Wrong citations erode trust even when the answer is right.",
        "higher",
    ),
    "faithfulness": _m(
        "Faithfulness (RAGAS)",
        "Answers (offline, LLM judge)",
        "offline",
        "Share of the answer's claims supported by the retrieved context, as judged by an LLM.",
        "supported claims / claims",
        "The hallucination metric: an answer can be correct by luck and still unfaithful.",
        "higher",
        "LLM judges are biased (verbosity, self-preference) and noisy; calibrate on a few "
        "hand-checked examples before trusting small differences.",
    ),
    "answer_relevancy": _m(
        "Answer relevancy (RAGAS)",
        "Answers (offline, LLM judge)",
        "offline",
        "Does the answer address the question (not just say true things)?",
        "mean similarity between the question and questions generated from the answer",
        "Catches evasive or off-topic answers.",
        "higher",
    ),
    "context_precision": _m(
        "Context precision (RAGAS)",
        "Answers (offline, LLM judge)",
        "offline",
        "Are the useful chunks ranked above the useless ones, as judged by an LLM?",
        "LLM-judged precision@k averaged over ranks",
        "A label-free cousin of nDCG.",
        "higher",
    ),
    "context_recall": _m(
        "Context recall (RAGAS)",
        "Answers (offline, LLM judge)",
        "offline",
        "Does the retrieved context contain everything the reference answer needs?",
        "reference claims supported by context / reference claims",
        "A cousin of recall@k that needs only a reference answer, not evidence labels.",
        "higher",
    ),
    "answer_correctness": _m(
        "Answer correctness (RAGAS)",
        "Answers (offline, LLM judge)",
        "offline",
        "Agreement between the answer and the reference answer (facts + meaning).",
        "weighted F1 of facts + semantic similarity",
        "Like 'correct' but tolerant of wording.",
        "higher",
    ),
}
