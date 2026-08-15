# Forge Blog Studio — Multi-Agent Blog Writing System

A LangGraph multi-agent pipeline that decides for itself whether a topic needs live web research, dynamically generates its own search queries, plans a variable-length outline, fans out into parallel writer agents, and self-corrects when its own output runs into token limits or rate errors — all streamed live into a Streamlit UI.

Give it a topic. A router agent first classifies it (`closed_book` / `hybrid` / `open_book`) and, if research is warranted, writes 3–6 targeted search queries itself — no hardcoded query templates. An orchestrator agent then plans a variable number of sections based on topic complexity, a pool of worker agents write them concurrently, and a reducer agent merges, cites, and illustrates the result into a single downloadable post.

---

## Architecture

```
                                   START
                                     │
                              ┌──────▼──────┐
                              │   Router    │  Decides: closed_book / hybrid / open_book
                              └──────┬──────┘
                          needs_research?
                       ┌─────────────┴─────────────┐
                     yes                            no
                       │                             │
                ┌──────▼──────┐                      │
                │  Research   │  Tavily search        │
                │             │  + evidence extraction│
                └──────┬──────┘                      │
                       └─────────────┬───────────────┘
                              ┌──────▼──────┐
                              │Orchestrator │  Plans 5–6 sections (tasks)
                              └──────┬──────┘
                              fan-out (Send)
                       ┌─────────────┼─────────────┐
                  ┌────▼───┐    ┌────▼───┐    ┌────▼───┐
                  │Worker 1│    │Worker 2│ ... │Worker N│  Parallel section writers
                  └────┬───┘    └────┬───┘    └────┬───┘
                       └─────────────┼─────────────┘
                              ┌──────▼──────────────┐
                              │  Reducer (subgraph)  │
                              │  ┌────────────────┐  │
                              │  │ merge_content  │  │  Dedupe + join sections
                              │  └───────┬────────┘  │
                              │  ┌───────▼────────┐  │
                              │  │ decide_images  │  │  Plan diagrams (if useful)
                              │  └───────┬────────┘  │
                              │  ┌───────▼────────┐  │
                              │  │ generate_images│  │  Call image API, append
                              │  └────────────────┘  │
                              └──────────┬───────────┘
                                       END
```

**8 nodes total** — 5 in the main graph (router, research, orchestrator, worker, reducer) and 3 inside the reducer subgraph (merge_content, decide_images, generate_and_place_images).

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Orchestration | LangGraph (`StateGraph`) | Conditional branching (research or not) and parallel fan-out (`Send()`) for workers |
| Planning / Router LLM | Groq `openai/gpt-oss-120b` | Strong reasoning for single-shot planning calls (router, research, orchestrator) |
| Section-writing LLM | Groq  `gpt-oss-120b (or qwen model for fallback) | Separate token pool from the planning models, reducing shared rate-limit pressure |
| Web research | Tavily Search API | Real-time evidence retrieval for topics needing current information |
| Image generation | Pollinations.ai (free, no API key) | Diagram generation without billing/quota constraints |
| Frontend | Streamlit | Live streaming progress, tabs for plan/evidence/preview/images/logs |
| Structured output | Pydantic + Groq native tool-calling | Enforces valid `Plan`, `RouterDecision`, `GlobalImagePlan` schemas |

---

## Key Features

- **Adaptive research routing** — the router classifies each topic as `closed_book` (evergreen, no research needed), `hybrid`, or `open_book` (needs current web evidence) before planning begins.
- **Parallel section writing** — the orchestrator's plan fans out into N independent workers via `Send()`, each writing one section concurrently.
- **Grounded citations** — when research is used, every worker is instructed to cite only the retrieved evidence, or explicitly mark a claim as "Not found in provided sources."
- **Automatic diagram generation** — a dedicated node decides if the blog needs visual diagrams, writes image prompts, and calls a free image API to generate them.
- **Downloadable output** — final blog available as Markdown, or as a zip bundle with generated images.

---

## Engineering Notes

A few real problems came up while building this:

- **Rate limits under parallel load** — 5–6 workers writing at the same time hit Groq's free-tier token limits. Fixed with retry logic and splitting workers/planning across separate models so they don't share the same limit.
- **Grounding isn't perfect on every topic** — for well-known, stable topics (pricing pages, established tools), citations and numbers come out accurate. On fast-moving or newer topics, the model can still state specific numbers confidently even when they're not fully backed by the source it was given, despite being told not to. This is a known limitation of prompting an LLM to stay grounded — it helps a lot, but doesn't fully fix it.

Generation takes 2-3 min per post (parallel writing + image calls),because of image generation model 

---

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:
```
GROQ_API_KEY=your_key_here
TAVILY_API_KEY=your_key_here
```

Run the frontend:
```bash
python -m streamlit run frontend.py
```

---

## Project Structure

```
├── blog_writing_agent_backend.py   # LangGraph nodes, schemas, compiled graph
├── frontend.py                     # Streamlit interface
├── requirements.txt
├── images/                         # Generated diagram images (created at runtime)
├──  schemas.py                     # all the schemas necessary for the graph                    
└── *.md                            # Generated blog outputs
```
