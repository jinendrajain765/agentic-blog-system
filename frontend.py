from __future__ import annotations

import json
import re
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, List, Iterator, Tuple

import pandas as pd
import streamlit as st
import uuid


# Import your compiled LangGraph app
from blog_writing_agent_backend import app



# Helpers

def safe_slug(title: str) -> str:
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s or "blog"


def bundle_zip(md_text: str, md_filename: str, images_dir: Path) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(md_filename, md_text.encode("utf-8"))
        if images_dir.exists() and images_dir.is_dir():
            for p in images_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(p))
    return buf.getvalue()


def images_zip(images_dir: Path) -> Optional[bytes]:
    if not images_dir.exists() or not images_dir.is_dir():
        return None
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in images_dir.rglob("*"):
            if p.is_file():
                z.write(p, arcname=str(p))
    return buf.getvalue()


def try_stream(graph_app, inputs: Dict[str, Any], config: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    try:
        for step in graph_app.stream(inputs, config=config, stream_mode="updates"):
            yield ("updates", step)
        out = graph_app.invoke(inputs, config=config)
        yield ("final", out)
        return
    except Exception:
        pass

    try:
        for step in graph_app.stream(inputs, config=config, stream_mode="values"):
            yield ("values", step)
        out = graph_app.invoke(inputs, config=config)
        yield ("final", out)
        return
    except Exception:
        pass

    out = graph_app.invoke(inputs, config=config)
    yield ("final", out)
    yield ("final", out)


def extract_latest_state(current_state: Dict[str, Any], step_payload: Any) -> Dict[str, Any]:
    if isinstance(step_payload, dict):
        if len(step_payload) == 1 and isinstance(next(iter(step_payload.values())), dict):
            inner = next(iter(step_payload.values()))
            current_state.update(inner)
        else:
            current_state.update(step_payload)
    return current_state



# Markdown renderer that supports local images

_MD_IMG_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)")
_CAPTION_LINE_RE = re.compile(r"^\*(?P<cap>.+)\*$")


def _resolve_image_path(src: str) -> Path:
    src = src.strip().lstrip("./")
    return Path(src).resolve()


def render_markdown_with_local_images(md: str):
    md=md.replace("$", "\\$")
    matches = list(_MD_IMG_RE.finditer(md))
    if not matches:
        st.markdown(md, unsafe_allow_html=False)
        return

    parts: List[Tuple[str, str]] = []
    last = 0
    for m in matches:
        before = md[last: m.start()]
        if before:
            parts.append(("md", before))
        alt = (m.group("alt") or "").strip()
        src = (m.group("src") or "").strip()
        parts.append(("img", f"{alt}|||{src}"))
        last = m.end()

    tail = md[last:]
    if tail:
        parts.append(("md", tail))

    i = 0
    while i < len(parts):
        kind, payload = parts[i]
        if kind == "md":
            st.markdown(payload, unsafe_allow_html=False)
            i += 1
            continue

        alt, src = payload.split("|||", 1)
        caption = None
        if i + 1 < len(parts) and parts[i + 1][0] == "md":
            nxt = parts[i + 1][1].lstrip()
            if nxt.strip():
                first_line = nxt.splitlines()[0].strip()
                mcap = _CAPTION_LINE_RE.match(first_line)
                if mcap:
                    caption = mcap.group("cap").strip()
                    rest = "\n".join(nxt.splitlines()[1:])
                    parts[i + 1] = ("md", rest)

        if src.startswith("http://") or src.startswith("https://"):
            st.image(src, caption=caption or (alt or None), use_container_width=True)
        else:
            img_path = _resolve_image_path(src)
            if img_path.exists():
                st.image(str(img_path), caption=caption or (alt or None), use_container_width=True)
            else:
                st.warning(f"Image not found: `{src}` (looked for `{img_path}`)")
        i += 1



# Past blogs helpers

def list_past_blogs() -> List[Path]:
    cwd = Path(".")
    files = [p for p in cwd.glob("*.md") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def read_md_file(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def extract_title_from_md(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            t = line[2:].strip()
            return t or fallback
    return fallback



# Page config + custom theme (dark, teal accent)

st.set_page_config(page_title="Forge Blog Studio", layout="wide", page_icon="🖋️")

st.markdown(
    """
    <style>
    .stApp { background-color: #0f1419; }
    section[data-testid="stSidebar"] { background-color: #151b23; border-right: 1px solid #23303d; }
    h1, h2, h3 { color: #e6edf3 !important; font-family: 'Segoe UI', sans-serif; }
    .hero {
        padding: 1.2rem 1.5rem; border-radius: 14px;
        background: linear-gradient(135deg, #0e7c66 0%, #14532d 100%);
        margin-bottom: 1.2rem;
    }
    .hero h1 { margin: 0; color: #f0fff4 !important; font-size: 1.8rem; }
    .hero p { margin: 0.2rem 0 0 0; color: #d7f5e8; font-size: 0.95rem; }
    div.stButton > button {
        background-color: #10b981; color: white; border: none;
        border-radius: 8px; font-weight: 600; padding: 0.5rem 1.2rem;
    }
    div.stButton > button:hover { background-color: #0d9668; }
    .stTabs [data-baseweb="tab"] {
        background-color: #151b23; border-radius: 8px 8px 0 0; color: #9fb3c8;
        padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] { background-color: #1c2733 !important; color: #10b981 !important; }
    .node-pill {
        display: inline-block; padding: 4px 12px; border-radius: 999px;
        background-color: #1c2733; color: #10b981; font-size: 0.85rem;
        margin: 2px 4px 2px 0; border: 1px solid #10b981;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
        <h1>🖋️ Forge Blog Studio</h1>
        <p>Multi-agent LangGraph pipeline — router → research → orchestrator → workers → image generation</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# Sidebar

with st.sidebar:
    st.markdown("###New Blog")
    topic = st.text_area("Topic", height=110, placeholder="e.g. How the TLS handshake secures a connection")
    as_of = st.date_input("As-of date", value=date.today())
    run_btn = st.button("Generate Blog", type="primary", use_container_width=True)

    st.divider()
    st.markdown("### Past Blogs")

    past_files = list_past_blogs()
    if not past_files:
        st.caption("No saved blogs found (*.md in current folder).")
        selected_md_file = None
    else:
        options: List[str] = []
        file_by_label: Dict[str, Path] = {}
        for p in past_files[:50]:
            try:
                md_text = read_md_file(p)
                title = extract_title_from_md(md_text, p.stem)
            except Exception:
                title = p.stem
            label = f"{title[:40]}  ·  {p.name}"
            options.append(label)
            file_by_label[label] = p

        selected_label = st.selectbox("Select a blog to load", options=options, label_visibility="collapsed")
        selected_md_file = file_by_label.get(selected_label)

        if st.button(" Load selected blog", use_container_width=True):
            if selected_md_file:
                md_text = read_md_file(selected_md_file)
                st.session_state["last_out"] = {
                    "plan": None,
                    "evidence": [],
                    "image_specs": [],
                    "final": md_text,
                }
                st.session_state["topic_prefill"] = extract_title_from_md(md_text, selected_md_file.stem)

if "last_out" not in st.session_state:
    st.session_state["last_out"] = None

# -----------------------------
# Tabs — same structure as reference
# -----------------------------
tab_plan, tab_evidence, tab_preview, tab_images, tab_logs = st.tabs(
    [" Plan", " Evidence & Sources", "Blog Preview", " Images", " Live Logs"]
)

logs: List[str] = []


def log(msg: str):
    logs.append(msg)


if run_btn:
    st.session_state["thread_id"] = str(uuid.uuid4())
    if not topic.strip():
        st.warning("Please enter a topic.")
        st.stop()

    inputs: Dict[str, Any] = {
        "topic": topic.strip(),
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "as_of": as_of.isoformat(),
        "recency_days": 7,
        "sections": [],
        "merged_md": "",
        "md_with_placeholders": "",
        "image_specs": [],
        "final": "",
    }

    status = st.status("Running graph…", expanded=True)
    node_track = st.empty()
    progress_area = st.empty()

    current_state: Dict[str, Any] = {}
    seen_nodes: List[str] = []

    config = {"configurable": {"thread_id": st.session_state["thread_id"]}}
    for kind, payload in try_stream(app, inputs, config):
        if kind in ("updates", "values"):
            node_name = None
            if isinstance(payload, dict) and len(payload) == 1 and isinstance(next(iter(payload.values())), dict):
                node_name = next(iter(payload.keys()))
            if node_name and node_name not in seen_nodes:
                seen_nodes.append(node_name)
                status.write(f"➡️ Node reached: **{node_name}**")
                pills = " ".join(f'<span class="node-pill">{n}</span>' for n in seen_nodes)
                node_track.markdown(pills, unsafe_allow_html=True)

            current_state = extract_latest_state(current_state, payload)

            summary = {
                "mode": current_state.get("mode"),
                "needs_research": current_state.get("needs_research"),
                "evidence_count": len(current_state.get("evidence", []) or []),
                "tasks": len((current_state.get("plan") or {}).get("tasks", []))
                if isinstance(current_state.get("plan"), dict) else None,
                "images": len(current_state.get("image_specs", []) or []),
                "sections_done": len(current_state.get("sections", []) or []),
            }
            progress_area.json(summary)
            log(f"[{kind}] {json.dumps(payload, default=str)[:1200]}")

        elif kind == "final":
            out = payload
            st.session_state["last_out"] = out
            status.update(label="Blog generation complete", state="complete", expanded=False)
            log("[final] received final state")


# Render result

out = st.session_state.get("last_out")
if out:
    # --- Plan tab ---
    with tab_plan:
        st.subheader("Generation Plan")
        plan_obj = out.get("plan")
        if not plan_obj:
            st.info("No plan found in output.")
        else:
            if hasattr(plan_obj, "model_dump"):
                plan_dict = plan_obj.model_dump()
            elif isinstance(plan_obj, dict):
                plan_dict = plan_obj
            else:
                plan_dict = json.loads(json.dumps(plan_obj, default=str))

            st.markdown(f"**Title:** {plan_dict.get('blog_title')}")
            c1, c2, c3 = st.columns(3)
            c1.metric("Audience", plan_dict.get("audience", "—"))
            c2.metric("Tone", plan_dict.get("tone", "—"))
            c3.metric("Blog kind", plan_dict.get("blog_kind", "—"))

            tasks = plan_dict.get("tasks", [])
            if tasks:
                df = pd.DataFrame(
                    [
                        {
                            "id": t.get("id"),
                            "title": t.get("title"),
                            "target_words": t.get("target_words"),
                            "research": t.get("requires_research"),
                            "citations": t.get("requires_citations"),
                            "code": t.get("requires_code"),
                            "tags": ", ".join(t.get("tags") or []),
                        }
                        for t in tasks
                    ]
                ).sort_values("id")
                st.dataframe(df, use_container_width=True, hide_index=True)
                with st.expander("Raw task details"):
                    st.json(tasks)

    #  Evidence tab 
    with tab_evidence:
        st.subheader("Evidence & Sources")
        evidence = out.get("evidence") or []
        if not evidence:
            st.info("No evidence returned (closed_book mode, or no Tavily results).")
        else:
            rows = []
            for e in evidence:
                if hasattr(e, "model_dump"):
                    e = e.model_dump()
                rows.append(
                    {
                        "title": e.get("title"),
                        "published_at": e.get("published_at"),
                        "source": e.get("source"),
                        "url": e.get("url"),
                    }
                )
            df = pd.DataFrame(rows)
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={"url": st.column_config.LinkColumn("url")},
            )

    #Preview tab
    with tab_preview:
        st.subheader("Blog Preview")
        final_md = out.get("final") or ""
        if not final_md:
            st.warning("No final markdown found.")
        else:
            render_markdown_with_local_images(final_md)

            plan_obj = out.get("plan")
            if hasattr(plan_obj, "blog_title"):
                blog_title = plan_obj.blog_title
            elif isinstance(plan_obj, dict):
                blog_title = plan_obj.get("blog_title", "blog")
            else:
                blog_title = extract_title_from_md(final_md, "blog")

            md_filename = f"{safe_slug(blog_title)}.md"
            dl1, dl2 = st.columns(2)
            with dl1:
                st.download_button(
                    "⬇️ Download Markdown",
                    data=final_md.encode("utf-8"),
                    file_name=md_filename,
                    mime="text/markdown",
                    use_container_width=True,
                )
            with dl2:
                bundle = bundle_zip(final_md, md_filename, Path("images"))
                st.download_button(
                    "📦 Download Bundle (MD + images)",
                    data=bundle,
                    file_name=f"{safe_slug(blog_title)}_bundle.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

    #Images tab
    with tab_images:
        st.subheader("Images")
        specs = out.get("image_specs") or []
        images_dir = Path("images")

        if not specs and not images_dir.exists():
            st.info("No images generated for this blog.")
        else:
            if specs:
                with st.expander("Image plan (prompts + placement)"):
                    st.json(specs)

            if images_dir.exists():
                files = [p for p in images_dir.iterdir() if p.is_file()]
                if not files:
                    st.warning("images/ exists but is empty.")
                else:
                    cols = st.columns(2)
                    for idx, p in enumerate(sorted(files)):
                        with cols[idx % 2]:
                            st.image(str(p), caption=p.name, use_container_width=True)

                z = images_zip(images_dir)
                if z:
                    st.download_button(
                        "⬇️ Download Images (zip)",
                        data=z,
                        file_name="images.zip",
                        mime="application/zip",
                    )

    #Logs tab
    with tab_logs:
        st.subheader("Live Logs")
        if "logs" not in st.session_state:
            st.session_state["logs"] = []
        if logs:
            st.session_state["logs"].extend(logs)
        st.text_area("Event log", value="\n\n".join(st.session_state["logs"][-80:]), height=520)
else:
    st.info("Enter a topic in the sidebar and click **Generate Blog** to get started.")