"""Creates a self-contained sample rebuttal workspace for quick exploration."""

from __future__ import annotations

import os
from pathlib import Path

from proc_utils import run_cmd
from todo_manager import TodoManager


def get_demo_workspace_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return base / "RebuttalSidecar" / "demo_workspace"


def ensure_demo_workspace(target_dir: Path | None = None) -> Path:
    """Create a realistic sample Git + LaTeX rebuttal workspace if absent."""
    root = (target_dir or get_demo_workspace_dir()).resolve()
    paper = root / "docs" / "manuscripts" / "demo_paper"
    if (paper / "main.tex").is_file() and (root / ".git").is_dir():
        return root

    manuscripts = root / "docs" / "manuscripts"
    sections = paper / "sections"
    sections.mkdir(parents=True, exist_ok=True)

    (manuscripts / "references.bib").write_text(
        "@article{smith2024,\n"
        "  title={Robust Sidecar Workflows for Peer Review},\n"
        "  author={Smith, Alice and Lee, Bob},\n"
        "  journal={Journal of Reproducible Systems},\n"
        "  year={2024}\n"
        "}\n",
        encoding="utf-8",
    )

    (sections / "intro.tex").write_text(
        "\\section{Introduction}\n"
        "Automated academic writing tools improve manuscript clarity \\cite{smith2024}.\n"
        "In the initial submission, we evaluated our approach on three small benchmarks.\n",
        encoding="utf-8",
    )

    (sections / "method.tex").write_text(
        "\\section{Methodology}\n"
        "Our baseline pipeline executes a single validation pass without incremental diffs.\n",
        encoding="utf-8",
    )

    (paper / "main.tex").write_text(
        "\\documentclass[11pt]{article}\n"
        "\\title{Rebuttal Sidecar Sample Manuscript}\n"
        "\\author{Research Team}\n"
        "\\date{}\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\input{sections/intro}\n"
        "\\input{sections/method}\n"
        "\\bibliographystyle{plain}\n"
        "\\bibliography{../references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    run_cmd(["git", "init"], cwd=root, timeout=15)
    run_cmd(["git", "config", "user.name", "Demo Author"], cwd=root, timeout=15)
    run_cmd(["git", "config", "user.email", "demo@example.com"], cwd=root, timeout=15)
    run_cmd(["git", "add", "."], cwd=root, timeout=15)
    run_cmd(["git", "commit", "-m", "v1 submitted manuscript"], cwd=root, timeout=15)
    run_cmd(["git", "tag", "v1-submitted"], cwd=root, timeout=15)

    # Apply realistic rebuttal edits in the working tree for Diff preview
    (sections / "intro.tex").write_text(
        "\\section{Introduction}\n"
        "Automated academic writing tools improve manuscript clarity and auditability \\cite{smith2024}.\n"
        "In this revised version, we expand our evaluation to six large-scale benchmarks and include ablation studies.\n",
        encoding="utf-8",
    )
    (sections / "method.tex").write_text(
        "\\section{Methodology}\n"
        "Our updated pipeline executes isolated sandbox compilation with debounced incremental diffs and full citation checks.\n",
        encoding="utf-8",
    )

    TodoManager().save(
        paper,
        {
            "manuscript": "demo_paper",
            "active_baseline": {"type": "git", "ref": "v1-submitted"},
            "engine": "pdflatex",
            "items": [
                {
                    "id": "R1-C1",
                    "reviewer": "Reviewer #1",
                    "summary": "Expand empirical evaluation beyond three small benchmarks and clarify auditability.",
                    "target_location": "Section 1, Page 1",
                    "status": "verified",
                    "response_draft": "We thank Reviewer #1 for this suggestion. We expanded our benchmark suite from three to six datasets in Section 1.",
                },
                {
                    "id": "R1-C2",
                    "reviewer": "Reviewer #1",
                    "summary": "Provide ablation results for the incremental diff pipeline.",
                    "target_location": "Section 2, Page 1",
                    "status": "agent_done",
                    "response_draft": "We updated Section 2 to describe the isolated sandbox ablation and verification checks.",
                },
                {
                    "id": "R2-C1",
                    "reviewer": "Reviewer #2",
                    "summary": "Discuss computational overhead when multiple .tex files are saved rapidly.",
                    "target_location": "Section 2",
                    "status": "pending",
                    "response_draft": "",
                },
            ],
        },
    )
    return root
