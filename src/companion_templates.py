"""Companion manuscript file templates (Title Page, Highlights, Cover Letter, Declaration)."""

from __future__ import annotations

import re
from pathlib import Path

from scanner import classify_companion_role

COMPANION_TEMPLATES: dict[str, dict[str, str]] = {
    "title_page": {
        "default_filename": "title_page.tex",
        "label": "Title Page (盲审/作者信息页)",
        "content": (
            "\\documentclass[12pt]{article}\n"
            "\\usepackage[margin=1in]{geometry}\n"
            "\\begin{document}\n"
            "\\begin{center}\n"
            "  {\\LARGE \\textbf{Manuscript Title Here}}\\\\[12pt]\n"
            "  {\\large First Author$^{1}$, Second Author$^{2,*}$}\\\\[8pt]\n"
            "  {\\small $^{1}$Department, University, City, Country\\\\\n"
            "  $^{2}$School, Institute, City, Country\\\\\n"
            "  $^{*}$Corresponding author: email@university.edu}\n"
            "\\end{center}\n\n"
            "\\section*{Acknowledgments}\n"
            "This work was supported by ...\n\n"
            "\\section*{CRediT Authorship Contribution Statement}\n"
            "\\textbf{First Author:} Conceptualization, Methodology, Writing -- Original Draft.\n"
            "\\end{document}\n"
        ),
    },
    "highlights": {
        "default_filename": "highlights.tex",
        "label": "Highlights (投稿亮点，建议每条 ≤ 85 字符)",
        "content": (
            "\\documentclass[12pt]{article}\n"
            "\\usepackage[margin=1in]{geometry}\n"
            "\\begin{document}\n"
            "\\section*{Highlights}\n"
            "\\begin{itemize}\n"
            "  \\item Proposes a lightweight method for the core research problem.\n"
            "  \\item Reduces false positives across benchmark evaluations.\n"
            "  \\item Demonstrates consistent performance gains without inference overhead.\n"
            "\\end{itemize}\n"
            "\\end{document}\n"
        ),
    },
    "cover_letter": {
        "default_filename": "cover_letter.tex",
        "label": "Cover Letter (投稿/返修说明信)",
        "content": (
            "\\documentclass[11pt]{article}\n"
            "\\usepackage[margin=1in]{geometry}\n"
            "\\begin{document}\n"
            "\\noindent Dear Editor,\\\\[10pt]\n"
            "Please find enclosed our revised manuscript entitled ``Manuscript Title''. "
            "We sincerely thank the editorial team and reviewers for their constructive comments.\n\n"
            "\\vspace{12pt}\n"
            "\\noindent Sincerely,\\\\\n"
            "The Authors\n"
            "\\end{document}\n"
        ),
    },
    "declaration": {
        "default_filename": "declaration.tex",
        "label": "Declaration of Competing Interest",
        "content": (
            "\\documentclass[12pt]{article}\n"
            "\\usepackage[margin=1in]{geometry}\n"
            "\\begin{document}\n"
            "\\section*{Declaration of Competing Interest}\n"
            "The authors declare that they have no known competing financial interests "
            "or personal relationships that could have appeared to influence the work reported in this paper.\n"
            "\\end{document}\n"
        ),
    },
}

_ITEM_RE = re.compile(r"^\s*(?:\\item|[-*])\s+(.+)$")


def _safe_child_file(manuscript_dir: Path, filename: str) -> Path:
    name = filename.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Invalid companion filename: {filename}")
    target = (manuscript_dir / name).resolve()
    if target.parent != manuscript_dir.resolve():
        raise ValueError(f"Invalid companion path: {filename}")
    return target


def create_companion_file(manuscript_dir: Path, template_key: str, filename: str = "") -> dict:
    """Create a companion .tex/.md/.txt file in manuscript_dir from a template."""
    tpl = COMPANION_TEMPLATES.get(template_key)
    if not tpl:
        raise ValueError(f"Unknown template: {template_key}")
    target_name = filename.strip() or tpl["default_filename"]
    if not Path(target_name).suffix:
        target_name = f"{target_name}.tex"
    target = _safe_child_file(manuscript_dir, target_name)
    if target.exists():
        raise FileExistsError(f"File already exists: {target.name}")
    target.write_text(tpl["content"], encoding="utf-8")
    return {
        "name": target.name,
        "role": classify_companion_role(target.name),
        "compilable": target.suffix.lower() == ".tex",
        "kind": "tex" if target.suffix.lower() == ".tex" else "text",
    }


def read_companion_content(manuscript_dir: Path, filename: str) -> dict:
    """Read companion file content and analyze bullet character counts."""
    target = _safe_child_file(manuscript_dir, filename)
    if not target.is_file():
        raise FileNotFoundError(f"Companion file not found: {filename}")
    text = target.read_text(encoding="utf-8", errors="replace")
    bullets = []
    for line in text.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            item_text = m.group(1).strip()
            bullets.append({"text": item_text, "chars": len(item_text), "over_85": len(item_text) > 85})
    return {
        "name": target.name,
        "role": classify_companion_role(target.name),
        "content": text,
        "line_count": len(text.splitlines()),
        "char_count": len(text),
        "bullets": bullets,
    }
