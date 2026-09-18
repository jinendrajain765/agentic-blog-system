from __future__ import annotations

import operator
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict, List, Optional, Literal, Annotated
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send # for worker nodes 
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage
from dotenv import load_dotenv
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
#from langgraph.checkpoint.postgres import PostgresSaver thought of using postgres but the problem was the worker although the section was told 5-6 section or task but the worker was writing 10 sections
load_dotenv()
from schemas import (
    Task,
    Plan,
    EvidenceItem,
    RouterDecision,
    EvidencePack,
    ImageSpec,
    GlobalImagePlan,
)

# Blog Writer (Router → (Research?) → Orchestrator → Workers → ReducerWithImages)
# Patches image capability using your 3-node reducer flow:
#   merge_content -> decide_images -> generate_and_place_images

llm=ChatGroq(model="openai/gpt-oss-120b",temperature=0)# temp =0 because this model was failing to call the tool from the router node means it was failing to call the internal its internal tool it was kind of hallucinating 

class State(TypedDict):
    topic: str

    # routing / research
    mode: str # comes from souter 
    needs_research: bool #comes from router 
    queries: List[str] #comes from router 
    evidence: List[EvidenceItem] #the result from the router for evey ans it gives key from the evidenceitem schema if their are 5 q and the max_resuts=2 so 10 results would be there and each result is a evidenceitem the ans should follow this evidenceitem schema so 10 evidenceitem would be there and this is converted into evidencepack pack of all evidenceitem object stored in a list 
    plan: Optional[Plan] #orchestrator 

    # recency
    as_of: str
    recency_days: int

    # workers
    sections: Annotated[List[tuple[int, str]], operator.add]  # (task_id, section_md)

    # reducer/image
    merged_md: str # merged section i.e a blog 
    md_with_placeholders: str # comes from the llm markdown with placeholders and that placehoder will be replaced by the image 
    image_specs: List[dict]

    final: str #final blog 

# Router
ROUTER_SYSTEM = """You are a routing module for a technical blog planner.

Decide whether web research is needed BEFORE planning.

Modes:
- closed_book (needs_research=false): evergreen concepts.
- hybrid (needs_research=true): evergreen + needs up-to-date examples/tools/models.
- open_book (needs_research=true): volatile weekly/news/"latest"/pricing/policy.

If needs_research=true:
- Output 3–6 high-signal, scoped queries.
- For open_book weekly roundup, include queries reflecting last 7 days.
Only choose hybrid if the user explicitly asks for:
- latest
- recent
- 2026
- current
- comparison of current tools
- pricing
- benchmarks
- new models
"""

def router_node(state: State) -> dict: # decides if needs research for the topic or not if needed it gives us 5-6 queries related to queries 
    decider = llm.with_structured_output(RouterDecision)
    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {state['topic']}\nAs-of date: {state['as_of']}"),
        ]
    ) 

    if decision.mode == "open_book":
        recency_days = 7
    elif decision.mode == "hybrid":
        recency_days = 45
    else:
        recency_days = 3650

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
        "recency_days": recency_days,
    }

def route_next(state: State) -> str: # conditioanl edge router->orchestrator or router->research->orchestrator 
    return "research" if state["needs_research"] else "orchestrator"

#Research (Tavily) 
def _tavily_search(query: str, max_results: int = 5) -> List[dict]: # takes every queries from the state and give the info against it 
    if not os.getenv("TAVILY_API_KEY"):
        return []
    try:
        from langchain_community.tools.tavily_search import TavilySearchResults  # type: ignore
        tool = TavilySearchResults(max_results=max_results)
        results = tool.invoke({"query": query})
        out: List[dict] = []
        for r in results or []:
            out.append(
                {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "snippet": r.get("content") or r.get("snippet") or "",
                    "published_at": r.get("published_date") or r.get("published_at"),
                    "source": r.get("source"),
                }
            )
        return out
    except Exception:
        return []

def _iso_to_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None

RESEARCH_SYSTEM = """You are a research synthesizer.

Given raw web search results, produce EvidenceItem objects.

Rules:
- Only include items with a non-empty url.
- Prefer relevant + authoritative sources.
- Normalize published_at to ISO YYYY-MM-DD if reliably inferable; else null (do NOT guess).
- Keep snippets short.
- Deduplicate by URL.
"""

def research_node(state: State) -> dict:
    queries = (state.get("queries") or [])[:3]
    print("Queries sent to Tavily:", queries)
    raw: List[dict] = []
    for q in queries:
        raw.extend(_tavily_search(q, max_results=2))

    if not raw:
        return {"evidence": []}

    print("Raw results from Tavily:", len(raw))

    extractor = llm.with_structured_output(EvidencePack)
    compact = "\n\n".join(
    f"Title: {r['title']}\n"
    f"URL: {r['url']}\n"
    f"Snippet: {r['snippet'][:200]}\n"
    f"Published: {r.get('published_at', '')}"
    for r in raw
)

    pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(
                content=(
                    f"As-of date: {state['as_of']}\n"
                    f"Recency days: {state['recency_days']}\n\n"
                    f"Raw results:\n{compact}"
                )
            ),
        ]
    )
         

    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e
    evidence = list(dedup.values())

    if state.get("mode") == "open_book":
        as_of = date.fromisoformat(state["as_of"])
        cutoff = as_of - timedelta(days=int(state["recency_days"]))
        evidence = [e for e in evidence if not e.published_at or (d := _iso_to_date(e.published_at)) and d >= cutoff]

    return {"evidence": evidence}


#5)Orchestrator (Plan)

# so the problem occured witn this system is ain tpm(token per limit) for this in the decide image did changes means the node was getting full markdown because of this this token problem was comming to gave only first 6k tokens bur then also while don research the problem came because of workers every task needs one worler to writ 1task means one section and earch worker was taking 2.5 tokens so for 5-6 worker the tokens is 12k and the limit is 8k to reduce this reducing this task for 5-6 to 3-4 so now 3-4 workers will work write th section

ORCH_SYSTEM = """You are a senior technical writer and developer advocate.
Produce a highly actionable outline for a technical blog post.

Requirements:
- 5-6 tasks, each with goal + 3–6 bullets + target_words(aim for 250-400 words each section). 
- Tags are flexible; do not force a fixed taxonomy.

Grounding:
- closed_book: evergreen, no evidence dependence.
- hybrid: use evidence for up-to-date examples; mark those tasks requires_research=True and requires_citations=True.
- open_book: weekly/news roundup: 
  - Set blog_kind="news_roundup"
  - No tutorial content unless requested
  - If evidence is weak, plan should explicitly reflect that (don’t invent events).

Output must match Plan schema.
"""

def orchestrator_node(state: State) -> dict: # the planner node 
    planner = llm.with_structured_output(Plan) # plan schema 
    mode = state.get("mode", "closed_book") # need reseach or not 
    evidence = state.get("evidence", []) # the ans 

    forced_kind = "news_roundup" if mode == "open_book" else None

    plan = planner.invoke(
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n"
                    f"As-of: {state['as_of']} (recency_days={state['recency_days']})\n"
                    f"{'Force blog_kind=news_roundup' if forced_kind else ''}\n\n"
                    f"Evidence:\n{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ]
    )
    if forced_kind:
        plan.blog_kind = "news_roundup"

    return {"plan": plan}


# Fanout
def fanout(state: State): # it will automatically fanput means if there are 5 sections so this func will assign 5 workers for 5 sections 
    assert state["plan"] is not None
    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "as_of": state["as_of"],
                "recency_days": state["recency_days"],
                "plan": state["plan"].model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in state["plan"].tasks
    ]


#Worker -> will write the sections and if the need info related the topic they can see the evidence in the state if there are 5 sections so 5 workers willwrite the section

WORKER_SYSTEM = """You are a senior technical writer and developer advocate.
Write ONE section of a technical blog post in Markdown.

Constraints:
- Cover ALL bullets in order.
- Target words ±15%.
- Output only section markdown starting with "## <Section Title>".

Scope guard:
- If blog_kind=="news_roundup", do NOT drift into tutorials (scraping/RSS/how to fetch).
  Focus on events + implications.

Grounding:
- If mode=="open_book": do not introduce any specific event/company/model/funding/policy claim unless supported by provided Evidence URLs.
  For each supported claim, attach a Markdown link ([Source](URL)).
  If unsupported, write "Not found in provided sources."
- If requires_citations==true (hybrid tasks): cite Evidence URLs for external claims.
- Never invent specific named entities (drug names, product names, trial names, model names, company names, statistics, or dates) that do not appear verbatim in the provided Evidence. If a specific detail isn't in the evidence, write generally about the topic instead of fabricating a plausible-sounding specific.

Code:
- If requires_code==true, include at least one minimal snippet.
"""
#worker_llm = ChatGroq(
#    model="qwen/qwen3.6-27b",
#    model_kwargs={"reasoning_format": "hidden"})
worker_llm=ChatGroq(model="openai/gpt-oss-20b",temperature=0)
def worker_node(payload: dict) -> dict:
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]

    bullets_text = "\n- " + "\n- ".join(task.bullets)
    evidence_text = "\n".join(
        f"- {e.title} | {e.url} | {e.published_at or 'date:unknown'}\n  Content: {e.snippet or 'no snippet available'}"
        for e in evidence[:20]
    )
    messages = [
        SystemMessage(content=WORKER_SYSTEM),
        HumanMessage(
            content=(
                f"Blog title: {plan.blog_title}\n"
                f"Audience: {plan.audience}\n"
                f"Tone: {plan.tone}\n"
                f"Blog kind: {plan.blog_kind}\n"
                f"Constraints: {plan.constraints}\n"
                f"Topic: {payload['topic']}\n"
                f"Mode: {payload.get('mode')}\n"
                f"As-of: {payload.get('as_of')} (recency_days={payload.get('recency_days')})\n\n"
                f"Section title: {task.title}\n"
                f"Goal: {task.goal}\n"
                f"Target words: {task.target_words}\n"
                f"Tags: {task.tags}\n"
                f"requires_research: {task.requires_research}\n"
                f"requires_citations: {task.requires_citations}\n"
                f"requires_code: {task.requires_code}\n"
                f"Bullets:{bullets_text}\n\n"
                f"Evidence (ONLY cite these URLs):\n{evidence_text}\n"
            )
        ),
    ]

    try: # had ti apply this logic because of this tpm(token per limit) it was hitting the rate so trying  if the tryfailed to it will wait 22 sec and then try again this is the last thing i can do to precent this token problem 
        section_md = worker_llm.invoke(messages).content.strip()
    except Exception:
        import time
        time.sleep(22)
        section_md = worker_llm.invoke(messages).content.strip()

    return {"sections": [(task.id, section_md)]}

#ReducerWithImages (subgraph)
#erge_content -> decide_images -> generate_and_place_images

def merge_content(state: State) -> dict: # merge the sections 
    plan = state["plan"]
    if plan is None:
        raise ValueError("merge_content called without plan.")
    # Dedupe: keep only thre first section per task_id , we have to do this because of doubling bug 5 task-> 10 sections sql cant handle multiple parallel workers writing checkpoints at the same point 
    seen={}#each key will represent 1 section and and we cannot add two same keys in dict if we do it will replace the 1st key with the 2nd dup key 
    for task_id, md in state['sections']: # in our case task id is the key and md is the section 
        if task_id not in seen:
            seen[task_id] = md

    print("Unique task_ids:", sorted(seen.keys()))
    print("Section lengths:", {k: len(v) for k, v in seen.items()})
    ordered_sections = [seen[tid] for tid in sorted(seen.keys())]  # ← use `seen`, not state["sections"]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md}
DECIDE_IMAGES_SYSTEM = """You are an expert technical editor.
Decide if images/diagrams are needed for THIS blog.

Rules:
- Max 3 images total.
- Each image must materially improve understanding (diagram/flow/table-like visual).
- Insert placeholders exactly: [[IMAGE_1]], [[IMAGE_2]], [[IMAGE_3]].
- You MUST propose at least 1 real image in the `images` list for this blog — do not substitute it with ASCII art or text-based diagrams. Choose the single most visually useful moment (e.g., a flow, sequence, or architecture) and generate a proper image prompt for it.
- If no images needed: md_with_placeholders must equal input and images=[].
- Avoid decorative images; prefer technical diagrams with short labels.
- The "size" field MUST be exactly one of: "1024x1024", "1024x1536", "1536x1024" — no other values are valid.
Return strictly GlobalImagePlan.
- Image prompts must explicitly request "simple flat vector diagram, minimal colors, clean geometric shapes, labeled boxes and arrows, white background, no artistic style, no abstract art" to ensure technical clarity.
- For each image, also set related_section to the exact heading text of the section it illustrates.
"""
#image_llm = ChatGroq(model="llama-3.1-8b-instant")
image_llm = ChatGroq(
    model="openai/gpt-oss-120b",temperature=0
)

# this decide image node will get the full markdown so the token per limit was occusring so for this we will not give the full markdown include will do silicing and guve first 6k tokens 
 # decides if there is a need to upload images if yes then it will create placeholders in the md file or in the blog and for each placeholdersthe llm will give the prompt
  
def decide_images(state: State) -> dict:  # decides if there is a need to upload images if yes then it will create placeholders in the md file or in the blog and for each placeholdersthe llm will give the prompt
  
    planner = image_llm.with_structured_output(GlobalImagePlan)
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None

    messages = [
        SystemMessage(content=DECIDE_IMAGES_SYSTEM),
        HumanMessage(
            content=(
                f"Blog kind: {plan.blog_kind}\n"
                f"Topic: {state['topic']}\n\n"
                "Insert placeholders + propose image prompts.\n\n"
                f"{merged_md[:6000]}"
            )
        ),
    ]

    try: # same retry we are doin becasue of the token limit problem so we will ivoke the messages if the limit reach we will wait 10 sec and retry again
        image_plan = planner.invoke(messages)
    except Exception:
        import time
        time.sleep(10)
        image_plan = planner.invoke(messages)

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }
def _gemini_generate_image_bytes(prompt: str) -> bytes:
    import requests
    import urllib.parse

    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
    
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content

   
def _safe_slug(title: str) -> str:
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s or "blog"

def generate_and_place_images(state: State) -> dict:
    plan = state["plan"]
    assert plan is not None

    md = state["merged_md"]
    image_specs = state.get("image_specs", []) or []
    print("Image specs count:", len(image_specs))

    if not image_specs:
        filename = f"{_safe_slug(plan.blog_title)}.md"
        Path(filename).write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = Path("images")
    images_dir.mkdir(exist_ok=True)

    image_blocks = []
    for spec in image_specs:
        filename = spec["filename"]
        out_path = images_dir / filename

        if not out_path.exists():
            try:
                img_bytes = _gemini_generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
                image_blocks.append(f"**Illustrates: {spec.get('related_section', 'General')}**\n\n![{spec['alt']}](images/{filename})\n*{spec['caption']}*")
            except Exception as e:
                print(f"Image generation failed for {filename}: {e}")
                continue
        else:
          image_blocks.append(f"**Illustrates: {spec.get('related_section', 'General')}**\n\n![{spec['alt']}](images/{filename})\n*{spec['caption']}*") # in the lasst when we put the image before it the section should come 

    if image_blocks:
        md = md + "\n\n---\n\n## Images\n\n" + "\n\n".join(image_blocks)

    filename = f"{_safe_slug(plan.blog_title)}.md"
    Path(filename).write_text(md, encoding="utf-8")
    return {"final": md}

#reducer subgraph
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", END)
reducer_subgraph = reducer_graph.compile()


#main graph

g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

g.add_edge(START, "router")
g.add_conditional_edges("router", route_next, {"research": "research", "orchestrator": "orchestrator"})
g.add_edge("research", "orchestrator")

g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)
conn = sqlite3.connect(database="blog_checkpoints.db", check_same_thread=False)
memory=SqliteSaver(conn)
app = g.compile(checkpointer=memory)
#app=g.compile()
app

