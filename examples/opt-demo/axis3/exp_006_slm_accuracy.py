"""
exp_006_slm_accuracy.py — Axis 3 SLM accuracy benchmark

Runs 100 tasks (34 translate + 33 summarize + 33 search) through:
  A. Axis 3 SLMs  — NLLB-200 / BART-large-cnn / Qwen3-0.6B (direct HF inference)
  B. gpt-4o-mini  — OpenAI API (baseline)

Metrics per task:
  output_similarity   word-overlap F1 (SLM output vs GPT-4o-mini output)
  slm_confidence      confidence.py score (0-1)
  slm_latency_s       wall-clock seconds for SLM
  gpt_latency_s       wall-clock seconds for GPT-4o-mini
  slm_in_tokens       input tokens consumed by SLM
  slm_out_tokens      output tokens produced by SLM
  escalated           1 if confidence < threshold

Results written to ClickHouse table: axis3_benchmark
Summary printed to stdout.

Usage (from opt-demo root):
    venv/bin/python3.14 axis3/exp_006_slm_accuracy.py
    venv/bin/python3.14 axis3/exp_006_slm_accuracy.py --tasks 20 --dry-run
    venv/bin/python3.14 axis3/exp_006_slm_accuracy.py --type translate
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
import json
import urllib.request
import urllib.parse
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

# ── fixtures ──────────────────────────────────────────────────────────────────

_TRANSLATE_BASE = [
    (
        "The global effort to combat climate change requires unprecedented cooperation "
        "between governments, industries, and citizens. Carbon emissions must be reduced "
        "dramatically over the next decade to limit warming to 1.5 degrees Celsius.",
        "French",
    ),
    (
        "Artificial intelligence is transforming industries from healthcare to finance. "
        "Machine learning models can now diagnose diseases, predict market movements, "
        "and generate creative content with remarkable accuracy.",
        "Spanish",
    ),
    (
        "The ocean covers more than 70 percent of Earth's surface and plays a critical "
        "role in regulating the global climate. Marine ecosystems support billions of "
        "people through food, oxygen production, and economic activity.",
        "German",
    ),
    (
        "Space exploration has entered a new era with private companies launching "
        "satellites and planning missions to the Moon and Mars. The commercialization "
        "of space could open entirely new industries within this century.",
        "Italian",
    ),
    (
        "Quantum computing leverages the principles of quantum mechanics to perform "
        "calculations that classical computers cannot solve efficiently. The technology "
        "promises breakthroughs in drug discovery, cryptography, and optimization.",
        "Portuguese",
    ),
    (
        "Renewable energy sources such as solar and wind power are growing rapidly "
        "worldwide. Battery storage technology improvements are making intermittent "
        "renewables more reliable for grid-scale electricity generation.",
        "French",
    ),
    (
        "The human brain contains approximately 86 billion neurons connected by "
        "trillions of synapses. Understanding neural circuits is key to treating "
        "neurological disorders and building more capable artificial intelligence.",
        "Spanish",
    ),
    (
        "Digital currencies and blockchain technology are reshaping the financial "
        "system. Central banks around the world are exploring digital versions of "
        "their national currencies to modernize payment infrastructure.",
        "German",
    ),
    (
        "Biodiversity loss is occurring at an alarming rate due to habitat destruction, "
        "pollution, and climate change. Scientists warn that the sixth mass extinction "
        "event is already underway, threatening ecosystem stability.",
        "Italian",
    ),
    (
        "The rapid development of large language models has raised important questions "
        "about safety, bias, and the future of work. Researchers are developing "
        "alignment techniques to ensure AI systems act in accordance with human values.",
        "Portuguese",
    ),
    (
        "Urbanization continues at an unprecedented pace, with more than half of the "
        "world's population now living in cities. Smart city technologies use sensors "
        "and data analytics to improve transportation, energy, and public services.",
        "French",
    ),
    (
        "Genomic medicine is revolutionizing how diseases are diagnosed and treated. "
        "Whole-genome sequencing allows clinicians to identify genetic mutations that "
        "drive cancer and tailor therapies accordingly.",
        "Spanish",
    ),
    (
        "The global semiconductor shortage highlighted the fragility of modern supply "
        "chains. Governments are investing heavily in domestic chip manufacturing "
        "capacity to reduce dependence on a handful of overseas producers.",
        "German",
    ),
    (
        "Electric vehicles are becoming increasingly cost-competitive with combustion "
        "engine cars. Expanding charging infrastructure and improving battery range "
        "are the remaining barriers to mass market adoption.",
        "Italian",
    ),
    (
        "Clean water access remains a challenge for hundreds of millions of people. "
        "New membrane filtration and solar desalination technologies offer affordable "
        "solutions for communities in water-stressed regions.",
        "Portuguese",
    ),
    (
        "The rise of remote work has fundamentally changed how companies operate. "
        "Distributed teams now collaborate across time zones using video conferencing, "
        "project management tools, and asynchronous communication platforms.",
        "French",
    ),
    (
        "Precision agriculture uses satellite imagery, soil sensors, and machine "
        "learning to optimize crop yields while reducing water and fertilizer use. "
        "The technology is vital for feeding a growing global population sustainably.",
        "Spanish",
    ),
]

_SUMMARIZE_TASKS = [
    {
        "text": (
            "The Amazon rainforest, often referred to as the 'lungs of the Earth,' covers more "
            "than 5.5 million square kilometres across nine South American countries. It produces "
            "about 20 percent of the world's oxygen and is home to an estimated 10 percent of all "
            "species on Earth, including thousands of plants, mammals, birds, reptiles, and insects "
            "that have not yet been scientifically described. The forest also plays a critical role "
            "in regulating the global water cycle, with trees releasing vast quantities of water "
            "vapour into the atmosphere through transpiration. Despite its importance, the Amazon "
            "is under severe threat from deforestation driven by agricultural expansion, logging, "
            "and infrastructure projects. Scientists warn that if deforestation exceeds 20 to 25 "
            "percent of the original forest cover, the ecosystem could tip into a drier savanna "
            "state, releasing enormous amounts of stored carbon into the atmosphere."
        ),
        "word_count": 30,
    },
    {
        "text": (
            "Quantum computing is an emerging technology that uses quantum mechanical phenomena "
            "such as superposition and entanglement to perform calculations. Unlike classical bits, "
            "which are either 0 or 1, quantum bits (qubits) can exist in multiple states "
            "simultaneously. This property allows quantum computers to explore many possible "
            "solutions to a problem at the same time, making them potentially much faster than "
            "classical computers for certain tasks. Applications include breaking existing "
            "encryption schemes, simulating molecular interactions for drug discovery, and "
            "optimizing complex logistics networks. However, quantum systems are extremely "
            "sensitive to environmental disturbances, leading to errors that must be corrected "
            "through sophisticated techniques. Major technology companies and governments are "
            "investing billions of dollars to develop fault-tolerant quantum computers."
        ),
        "word_count": 25,
    },
    {
        "text": (
            "The history of the internet traces back to ARPANET, a US Department of Defense "
            "project in the late 1960s designed to create a resilient communication network. "
            "The development of TCP/IP protocols in the 1970s established the foundation for "
            "data exchange. The World Wide Web, invented by Tim Berners-Lee in 1989, made the "
            "internet accessible to the general public through a graphical interface of linked "
            "pages. Commercial adoption exploded during the 1990s, leading to the dot-com boom "
            "and bust. Today, the internet connects more than five billion people worldwide, "
            "enabling e-commerce, social media, cloud computing, streaming entertainment, and "
            "instant global communication. The proliferation of smartphones has made internet "
            "access ubiquitous even in developing regions, transforming commerce, education, "
            "and civic participation."
        ),
        "word_count": 20,
    },
    {
        "text": (
            "Neural networks are computational systems loosely inspired by the structure of "
            "biological brains. They consist of layers of interconnected nodes (neurons) that "
            "transform input data through learned weights. Deep learning, which uses many "
            "hidden layers, has achieved remarkable results in image recognition, natural "
            "language processing, and game playing. Convolutional neural networks (CNNs) excel "
            "at processing visual data, while transformer architectures have become dominant "
            "for language tasks. Training deep networks requires large datasets and substantial "
            "computational resources. Techniques like dropout, batch normalization, and "
            "attention mechanisms improve generalization and performance. The field continues "
            "to evolve rapidly, with new architectures achieving state-of-the-art results "
            "across a growing range of applications."
        ),
        "word_count": 25,
    },
    {
        "text": (
            "The Mediterranean diet is characterized by high consumption of vegetables, fruits, "
            "whole grains, legumes, nuts, and olive oil, moderate fish and poultry intake, and "
            "low consumption of red meat and processed foods. Decades of epidemiological research "
            "associate it with lower rates of cardiovascular disease, type 2 diabetes, cognitive "
            "decline, and certain cancers. The diet's health benefits are attributed to its "
            "anti-inflammatory properties, high fiber content, healthy fats from olive oil and "
            "fish, and rich supply of antioxidants. The World Health Organization has recognized "
            "it as a healthy dietary pattern. Beyond individual health, the traditional food "
            "practices associated with the diet have been inscribed on the UNESCO Intangible "
            "Cultural Heritage list."
        ),
        "word_count": 20,
    },
    {
        "text": (
            "The James Webb Space Telescope, launched in December 2021, is the most powerful "
            "space observatory ever built. It operates primarily in the infrared spectrum, "
            "allowing it to peer through dust clouds and observe the earliest galaxies that "
            "formed after the Big Bang. Webb's primary mirror measures 6.5 meters in diameter, "
            "compared to Hubble's 2.4 meters, giving it vastly greater light-gathering capability. "
            "The telescope is positioned at the second Lagrange point, 1.5 million kilometres "
            "from Earth, where it is shielded from sunlight by a five-layer sunshield the size "
            "of a tennis court. Early results have already challenged existing models of galaxy "
            "formation and revealed detailed atmospheric compositions of exoplanets."
        ),
        "word_count": 30,
    },
    {
        "text": (
            "CRISPR-Cas9 is a revolutionary gene-editing technology derived from a natural "
            "bacterial immune system. Scientists Jennifer Doudna and Emmanuelle Charpentier "
            "adapted the mechanism to precisely cut DNA at specific sequences, earning them "
            "the 2020 Nobel Prize in Chemistry. The technology enables researchers to disable, "
            "correct, or insert genes with unprecedented precision and ease. Medical applications "
            "include potential cures for genetic diseases like sickle cell anemia, beta-thalassemia, "
            "and certain forms of blindness. Agricultural applications include developing crops "
            "with improved disease resistance, drought tolerance, and nutritional profiles. "
            "Ethical debates surround germline editing, which could produce heritable genetic "
            "changes in future generations."
        ),
        "word_count": 25,
    },
    {
        "text": (
            "Blockchain technology is a distributed ledger system in which transactions are "
            "recorded in blocks that are cryptographically linked to form an immutable chain. "
            "Each node in the network holds a copy of the entire ledger, making it highly "
            "resistant to tampering. Bitcoin, the first major application, demonstrated that "
            "peer-to-peer financial transactions could occur without trusted intermediaries. "
            "Ethereum expanded the concept with smart contracts, self-executing programs that "
            "run on the blockchain. Industries from supply chain management to healthcare "
            "recordkeeping are exploring blockchain for transparency and auditability. "
            "However, scalability, energy consumption, and regulatory uncertainty remain "
            "significant challenges."
        ),
        "word_count": 20,
    },
    {
        "text": (
            "The human microbiome refers to the trillions of microorganisms — bacteria, viruses, "
            "fungi, and archaea — that inhabit the human body, particularly the gut. Research "
            "over the past two decades has revealed that the microbiome plays a crucial role in "
            "digestion, immune function, mental health, and protection against pathogens. The "
            "composition of the gut microbiome is influenced by diet, antibiotic use, birth mode, "
            "and environmental exposures. Dysbiosis, or imbalance in the microbial community, "
            "has been linked to conditions including inflammatory bowel disease, obesity, "
            "depression, and autoimmune disorders. Probiotics, prebiotics, and fecal microbiota "
            "transplants are emerging therapeutic approaches for restoring healthy microbial "
            "balance."
        ),
        "word_count": 25,
    },
    {
        "text": (
            "Autonomous vehicles use a combination of cameras, lidar, radar, and artificial "
            "intelligence to navigate roads without human input. The technology has advanced "
            "significantly, with companies operating commercial robotaxi services in several "
            "cities. Safety remains the paramount concern; while autonomous vehicles generally "
            "outperform humans in certain controlled conditions, edge cases involving unusual "
            "weather, road damage, or unexpected obstacles continue to challenge systems. "
            "Regulatory frameworks are still evolving to determine liability standards and "
            "testing requirements. Full autonomy across all road conditions represents a "
            "significant engineering challenge that the industry is actively working to solve "
            "through ongoing development and testing."
        ),
        "word_count": 20,
    },
    {
        "text": (
            "Fusion energy, the process that powers the Sun, involves combining light atomic "
            "nuclei to release enormous amounts of energy. Unlike fission, fusion produces no "
            "long-lived radioactive waste and uses abundant fuels like deuterium and lithium. "
            "In December 2022, scientists at the National Ignition Facility achieved fusion "
            "ignition for the first time, producing more energy from a fusion reaction than "
            "the laser energy used to trigger it. Commercial fusion power plants remain "
            "decades away, but private investment has surged, with dozens of startups pursuing "
            "different confinement approaches including tokamaks, stellarators, and inertial "
            "confinement methods."
        ),
        "word_count": 30,
    },
    {
        "text": (
            "The global semiconductor industry produces chips that power everything from "
            "smartphones to supercomputers. Manufacturing cutting-edge chips requires "
            "photolithography machines capable of etching features just a few nanometres wide "
            "onto silicon wafers. TSMC, Samsung, and Intel are the only companies capable of "
            "producing the most advanced chips. The 2020-2022 chip shortage, triggered by "
            "pandemic disruptions and surging demand, exposed the vulnerability of global "
            "supply chains. Governments in the United States, Europe, and Asia have since "
            "committed hundreds of billions of dollars to build domestic semiconductor "
            "manufacturing capacity and reduce geopolitical concentration risk."
        ),
        "word_count": 25,
    },
    {
        "text": (
            "Machine learning is a branch of artificial intelligence in which systems learn "
            "patterns from data rather than being explicitly programmed. Supervised learning "
            "trains models on labeled examples to make predictions. Unsupervised learning "
            "discovers structure in unlabeled data through clustering and dimensionality "
            "reduction. Reinforcement learning trains agents to maximize rewards through "
            "trial and error interaction with an environment. The explosion of available data, "
            "increased computing power, and algorithmic innovations have driven rapid progress. "
            "Applications span medical diagnosis, fraud detection, recommendation systems, "
            "autonomous navigation, and scientific discovery across virtually every domain."
        ),
        "word_count": 20,
    },
]

_SEARCH_QUERIES = [
    "latest advances in quantum computing 2025",
    "current state of large language model research",
    "recent breakthroughs in cancer immunotherapy",
    "progress on nuclear fusion energy reactors",
    "state of electric vehicle battery technology 2025",
    "recent developments in CRISPR gene therapy",
    "global renewable energy capacity growth",
    "advances in autonomous vehicle safety systems",
    "latest findings on microbiome and mental health",
    "current semiconductor manufacturing technology",
    "recent AI safety research developments",
    "status of Mars mission programs 2025",
    "latest climate change scientific findings",
    "advances in protein structure prediction",
    "recent quantum cryptography breakthroughs",
    "current state of digital currency adoption",
    "latest developments in brain-computer interfaces",
    "advances in carbon capture technology 2025",
    "recent findings in exoplanet research",
    "status of COVID-19 long-term effects research",
    "latest advances in solid-state battery technology",
    "recent developments in mRNA vaccine platform",
    "current state of 6G wireless technology",
    "latest findings on dark matter research",
    "advances in room-temperature superconductors",
    "recent breakthroughs in material science",
    "latest developments in drone delivery systems",
    "current state of augmented reality technology",
    "advances in precision medicine genomics",
    "recent findings on ocean plastic pollution",
    "latest AI hardware accelerator developments",
    "progress on vertical farming technology",
    "recent advances in hydrogen fuel cells",
]


def _word_overlap_f1(text_a: str, text_b: str) -> float:
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    precision = len(intersection) / len(words_b)
    recall    = len(intersection) / len(words_a)
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 4)


def _gpt_translate(text: str, target_language: str, client) -> tuple[str, float]:
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": f"Translate the following text to {target_language}. Output only the translation."},
            {"role": "user",   "content": text},
        ],
        max_tokens=400,
    )
    latency = time.perf_counter() - t0
    return resp.choices[0].message.content.strip(), latency


def _gpt_summarize(text: str, word_count: int, client) -> tuple[str, float]:
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": f"Summarize the following text in approximately {word_count} words. Be concise and factual."},
            {"role": "user",   "content": text},
        ],
        max_tokens=150,
    )
    latency = time.perf_counter() - t0
    return resp.choices[0].message.content.strip(), latency


def _gpt_synthesize(query: str, raw_results: str, client) -> tuple[str, float]:
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Based on the following web search results, write a clear and accurate "
                    "answer to the user's query. Be concise and cite 2-3 sources."
                ),
            },
            {
                "role": "user",
                "content": f"Query: {query}\n\nSearch results:\n{raw_results[:3000]}",
            },
        ],
        max_tokens=300,
    )
    latency = time.perf_counter() - t0
    return resp.choices[0].message.content.strip(), latency


def _web_search(query: str) -> str:
    """Minimal DuckDuckGo instant-answer scrape (no extra deps)."""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        return "\n\n".join(
            f"{r.get('title','')}\n{r.get('href','')}\n{r.get('body','')}"
            for r in results
        )
    except Exception as exc:
        return f"[search unavailable: {exc}]"


def _ch_ensure_table(ch_url: str) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS axis3_benchmark (
        run_id           String,
        task_type        LowCardinality(String),
        task_index       UInt32,
        slm_model        String,
        slm_confidence   Float32,
        slm_latency_s    Float32,
        gpt_latency_s    Float32,
        output_similarity Float32,
        slm_in_tokens    UInt32,
        slm_out_tokens   UInt32,
        escalated        UInt8,
        task_input       String,
        slm_output       String,
        gpt_output       String,
        created_at       DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (run_id, task_type, task_index)
    """
    req = urllib.request.Request(ch_url, data=ddl.encode(), method="POST")
    with urllib.request.urlopen(req) as resp:
        resp.read()


def _ch_insert(ch_url: str, rows: list[dict]) -> None:
    if not rows:
        return
    lines = []
    for r in rows:
        lines.append(json.dumps({
            "run_id":           r["run_id"],
            "task_type":        r["task_type"],
            "task_index":       r["task_index"],
            "slm_model":        r["slm_model"],
            "slm_confidence":   r["slm_confidence"],
            "slm_latency_s":    r["slm_latency_s"],
            "gpt_latency_s":    r["gpt_latency_s"],
            "output_similarity": r["output_similarity"],
            "slm_in_tokens":    r["slm_in_tokens"],
            "slm_out_tokens":   r["slm_out_tokens"],
            "escalated":        int(r["escalated"]),
            "task_input":       r["task_input"][:500],
            "slm_output":       r["slm_output"][:500],
            "gpt_output":       r["gpt_output"][:500],
        }))
    body = "\n".join(lines).encode()
    url  = ch_url + "?query=" + urllib.parse.quote("INSERT INTO axis3_benchmark FORMAT JSONEachRow")
    req  = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-ndjson")
    with urllib.request.urlopen(req) as resp:
        resp.read()


def run_benchmark(task_limit: int, task_types: list[str], dry_run: bool, ch_url: str) -> None:
    import openai
    from axis3 import confidence, translator_slm, summarizer_slm, search_slm

    openai_client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    run_id = str(uuid.uuid4())[:8]

    if not dry_run:
        _ch_ensure_table(ch_url)

    rows: list[dict] = []
    task_idx = 0
    totals: dict[str, list] = {t: [] for t in task_types}

    # ── translate ────────────────────────────────────────────────────────────
    if "translate" in task_types:
        tasks = _TRANSLATE_BASE[:task_limit] if task_limit else _TRANSLATE_BASE
        print(f"\n[translate] {len(tasks)} tasks — loading NLLB-200...")
        for i, (text, lang) in enumerate(tasks):
            t0 = time.perf_counter()
            try:
                slm_out, in_tok, out_tok = translator_slm.translate(text, lang)
            except Exception as exc:
                print(f"  [{i+1}] SLM ERROR: {exc}")
                slm_out, in_tok, out_tok = "", 0, 0
            slm_lat = time.perf_counter() - t0

            conf = confidence.score_translation(slm_out, text, lang)
            escalated = conf < translator_slm.THRESHOLD

            gpt_out, gpt_lat = _gpt_translate(text, lang, openai_client)
            sim = _word_overlap_f1(slm_out, gpt_out)

            row = {
                "run_id":           run_id,
                "task_type":        "translate",
                "task_index":       task_idx,
                "slm_model":        translator_slm.MODEL_ID,
                "slm_confidence":   conf,
                "slm_latency_s":    round(slm_lat, 3),
                "gpt_latency_s":    round(gpt_lat, 3),
                "output_similarity": sim,
                "slm_in_tokens":    in_tok,
                "slm_out_tokens":   out_tok,
                "escalated":        escalated,
                "task_input":       f"{text[:100]} → {lang}",
                "slm_output":       slm_out,
                "gpt_output":       gpt_out,
            }
            rows.append(row)
            totals["translate"].append(row)
            task_idx += 1
            print(
                f"  [{i+1:2d}/{len(tasks)}] conf={conf:.2f} sim={sim:.2f} "
                f"slm={slm_lat:.1f}s gpt={gpt_lat:.1f}s"
                f"{' [ESC]' if escalated else ''}"
            )

    # ── summarize ────────────────────────────────────────────────────────────
    if "summarize" in task_types:
        tasks = _SUMMARIZE_TASKS[:task_limit] if task_limit else _SUMMARIZE_TASKS
        print(f"\n[summarize] {len(tasks)} tasks — loading BART-large-cnn...")
        for i, task in enumerate(tasks):
            text       = task["text"]
            word_count = task["word_count"]
            t0 = time.perf_counter()
            try:
                slm_out, in_tok, out_tok = summarizer_slm.summarize(text, word_count)
            except Exception as exc:
                print(f"  [{i+1}] SLM ERROR: {exc}")
                slm_out, in_tok, out_tok = "", 0, 0
            slm_lat = time.perf_counter() - t0

            conf = confidence.score_summary(slm_out, word_count)
            escalated = conf < summarizer_slm.THRESHOLD

            gpt_out, gpt_lat = _gpt_summarize(text, word_count, openai_client)
            sim = _word_overlap_f1(slm_out, gpt_out)

            row = {
                "run_id":           run_id,
                "task_type":        "summarize",
                "task_index":       task_idx,
                "slm_model":        summarizer_slm.MODEL_ID,
                "slm_confidence":   conf,
                "slm_latency_s":    round(slm_lat, 3),
                "gpt_latency_s":    round(gpt_lat, 3),
                "output_similarity": sim,
                "slm_in_tokens":    in_tok,
                "slm_out_tokens":   out_tok,
                "escalated":        escalated,
                "task_input":       f"{text[:100]}... ({word_count}w)",
                "slm_output":       slm_out,
                "gpt_output":       gpt_out,
            }
            rows.append(row)
            totals["summarize"].append(row)
            task_idx += 1
            print(
                f"  [{i+1:2d}/{len(tasks)}] conf={conf:.2f} sim={sim:.2f} "
                f"slm={slm_lat:.1f}s gpt={gpt_lat:.1f}s"
                f"{' [ESC]' if escalated else ''}"
            )

    # ── search ───────────────────────────────────────────────────────────────
    if "search" in task_types:
        tasks = _SEARCH_QUERIES[:task_limit] if task_limit else _SEARCH_QUERIES
        print(f"\n[search] {len(tasks)} tasks — loading Qwen3-0.6B...")
        for i, query in enumerate(tasks):
            raw = _web_search(query)

            t0 = time.perf_counter()
            try:
                slm_out, in_tok, out_tok = search_slm.synthesize(query, raw)
            except Exception as exc:
                print(f"  [{i+1}] SLM ERROR: {exc}")
                slm_out, in_tok, out_tok = "", 0, 0
            slm_lat = time.perf_counter() - t0

            conf = confidence.score_search_synthesis(slm_out, raw)
            escalated = conf < search_slm.THRESHOLD

            gpt_out, gpt_lat = _gpt_synthesize(query, raw, openai_client)
            sim = _word_overlap_f1(slm_out, gpt_out)

            row = {
                "run_id":           run_id,
                "task_type":        "search",
                "task_index":       task_idx,
                "slm_model":        search_slm.MODEL_ID,
                "slm_confidence":   conf,
                "slm_latency_s":    round(slm_lat, 3),
                "gpt_latency_s":    round(gpt_lat, 3),
                "output_similarity": sim,
                "slm_in_tokens":    in_tok,
                "slm_out_tokens":   out_tok,
                "escalated":        escalated,
                "task_input":       query,
                "slm_output":       slm_out,
                "gpt_output":       gpt_out,
            }
            rows.append(row)
            totals["search"].append(row)
            task_idx += 1
            print(
                f"  [{i+1:2d}/{len(tasks)}] conf={conf:.2f} sim={sim:.2f} "
                f"slm={slm_lat:.1f}s gpt={gpt_lat:.1f}s"
                f"{' [ESC]' if escalated else ''}"
            )

    # ── write to ClickHouse ──────────────────────────────────────────────────
    if not dry_run and rows:
        print(f"\nWriting {len(rows)} rows to ClickHouse (run_id={run_id})...")
        _ch_insert(ch_url, rows)
        print("Done.")
    elif dry_run:
        print(f"\n[dry-run] would write {len(rows)} rows (run_id={run_id})")

    # ── summary table ────────────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  exp_006 Axis 3 SLM Accuracy — run_id: {run_id}")
    print(f"{'='*72}")
    print(f"  {'Type':<12} {'Model':<38} {'N':>3} {'Conf':>6} {'Sim':>6} {'Esc%':>5} {'SLM/s':>7} {'GPT/s':>7}")
    print(f"  {'-'*78}")

    for ttype, trows in totals.items():
        if not trows:
            continue
        n        = len(trows)
        avg_conf = sum(r["slm_confidence"]   for r in trows) / n
        avg_sim  = sum(r["output_similarity"] for r in trows) / n
        esc_pct  = sum(1 for r in trows if r["escalated"]) / n * 100
        avg_slm  = sum(r["slm_latency_s"]   for r in trows) / n
        avg_gpt  = sum(r["gpt_latency_s"]   for r in trows) / n
        model    = trows[0]["slm_model"]
        print(
            f"  {ttype:<12} {model:<38} {n:>3} {avg_conf:>6.2f} {avg_sim:>6.2f} "
            f"{esc_pct:>5.1f}% {avg_slm:>6.1f}s {avg_gpt:>6.1f}s"
        )

    print(f"{'='*72}\n")

    if not dry_run:
        print("Query results in ClickHouse:")
        print(f"  SELECT task_type, avg(slm_confidence), avg(output_similarity),")
        print(f"         countIf(escalated=1)/count() AS escalation_rate")
        print(f"  FROM axis3_benchmark WHERE run_id='{run_id}'")
        print(f"  GROUP BY task_type")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Axis 3 SLM accuracy benchmark (exp_006)"
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=0,
        help="Max tasks per type (0 = all; useful for quick smoke tests)",
    )
    parser.add_argument(
        "--type",
        dest="task_types",
        choices=["translate", "summarize", "search", "all"],
        default="all",
        help="Which task type to benchmark (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip ClickHouse writes; print results only",
    )
    parser.add_argument(
        "--clickhouse-url",
        default=os.environ.get("CLICKHOUSE_URL", "http://localhost:8123"),
        help="ClickHouse HTTP URL (default: http://localhost:8123)",
    )
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    task_types = (
        ["translate", "summarize", "search"]
        if args.task_types == "all"
        else [args.task_types]
    )

    run_benchmark(
        task_limit=args.tasks,
        task_types=task_types,
        dry_run=args.dry_run,
        ch_url=args.clickhouse_url,
    )


if __name__ == "__main__":
    main()
