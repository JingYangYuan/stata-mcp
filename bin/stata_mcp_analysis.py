#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standard-library Stata analysis engine for stata-mcp.

Provides four self-contained capabilities with zero third-party dependencies
(Python 3.8+ compatible):

1. **Stata Help Engine**: locate ``.sthlp`` / ``.hlp`` files across base,
   updates, site, plus (SSC) and personal ado directories, resolve Stata
   ``*help_alias.maint`` aliases (e.g. ``reg`` -> ``regress``), expand
   ``{INCLUDE ...}`` directives, convert SMCL to clean Markdown-like text with
   section extraction, search help topics, and inspect ``.ado`` files locally.
2. **Exact Error Line Localization**: parse ``.do`` files into logical
   statements (handling ``#delimit ;``, ``///`` continuations, ``/* ... */``
   nested block comments, ``//`` and ``*`` comments, and macro-expanded
   commands), distinguish fatal ``r(NNN);`` errors from ``capture``-swallowed
   ones, and map failing log commands monotonically to exact 1-based line
   numbers and source context snippets.
3. **Anti-Loop Linter & Pitfall Checker**: detect unidiomatic Stata loops
   (``1/_N`` observation loops, ``levelsof`` + ``foreach``, manual dummy loops,
   row accumulation loops, ``preserve``/``restore`` in loops, nested loops,
   lag/rolling loops, bulk rename/recode loops) as well as missing-value ``>``
   traps, deprecated ``xi:``, unbalanced braces/quotes, and ``merge`` checks.
4. **Vectorization Advisor**: 11 concrete side-by-side "bad loop vs. idiomatic
   Stata" recipes (``bysort``, ``egen``, ``collapse``, ``i.var``, ``L./D.``,
   ``frames``, ``mata``), plus automatic pattern matching on code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# caches (keyed by normalized stata_path)
# ---------------------------------------------------------------------------
_ALIAS_CACHE: Dict[str, Dict[str, str]] = {}
_SEARCH_INDEX_CACHE: Dict[str, List[Dict[str, str]]] = {}

# ---------------------------------------------------------------------------
# 1. Stata Help Engine
# ---------------------------------------------------------------------------
_SMCL_CHAR_CODES: Dict[str, str] = {
    "S|": "$",
    "'g": "`",
    "-(": "{",
    ")-": "}",
    "-": "-",
    "|": "|",
    "+": "+",
    "TT": "+",
    "BT": "+",
    "LT": "+",
    "RT": "+",
    "TLC": "+",
    "TRC": "+",
    "BRC": "+",
    "BLC": "+",
    "a'": "á",
    "A'": "Á",
    "e'": "é",
    "E'": "É",
    "i'": "í",
    "I'": "Í",
    "o'": "ó",
    "O'": "Ó",
    "u'": "ú",
    "U'": "Ú",
    "a'g": "à",
    "e'g": "è",
    "i'g": "ì",
    "o'g": "ò",
    "u'g": "ù",
    "a^": "â",
    "e^": "ê",
    "i^": "î",
    "o^": "ô",
    "u^": "û",
    "a~": "ã",
    "n~": "ñ",
    "o~": "õ",
    "a..": "ä",
    "e..": "ë",
    "o..": "ö",
    "u..": "ü",
    "ss": "ß",
    "c,": "ç",
}

_SMCL_PLACEHOLDERS: Dict[str, str] = {
    "ifin": "[if] [in]",
    "if": "[if]",
    "in": "[in]",
    "weight": "[weight]",
    "depvar": "depvar",
    "indepvars": "indepvars",
    "varlist": "varlist",
    "varname": "varname",
    "newvar": "newvar",
    "newvarlist": "newvarlist",
    "dtype": "type",
    "p_end": "",
    "break": "\n",
    "tab": "\t",
    "newpage": "\n",
}

_SMCL_IGNORED_TAGS = {
    "smcl",
    "viewerdialog",
    "viewerjumpto",
    "findalias",
    "p2colset",
    "p2colreset",
    "synoptset",
    "synopthdr",
    "reset",
    "asis",
    "s",
    "c",
    "right",
    "center",
    "centre",
}

_SMCL_INLINE_TEXT_TAGS = {
    "cmd",
    "cmdab",
    "bf",
    "it",
    "ul",
    "inp",
    "input",
    "res",
    "result",
    "txt",
    "text",
    "err",
    "error",
    "hilite",
    "hi",
    "sf",
    "it",
    "com",
    "bind",
    "stata",
    "browse",
}


def find_ado_roots(stata_path: str = "") -> List[Tuple[str, str]]:
    """Return ordered ``(label, directory)`` pairs for existing ado roots.

    Priority matches Stata's search precedence: updates -> plus -> personal ->
    site -> base so user-installed/updated commands shadow base files.
    """
    candidates: List[Tuple[str, Path]] = []
    home = Path.home()

    env_ado = os.environ.get("STATA_ADO_PATH") or os.environ.get("S_ADO") or ""
    if env_ado:
        for chunk in env_ado.split(os.pathsep):
            cleaned = chunk.strip().strip('"').strip("'")
            if cleaned and cleaned.upper() not in ("BASE", "SITE", "PLUS", "PERSONAL", "OLDPLACE", "UPDATES", "."):
                candidates.append(("env", Path(cleaned).expanduser()))

    sp = Path(stata_path).expanduser() if stata_path else None
    if sp and sp.is_dir():
        candidates.append(("updates", sp / "ado" / "updates"))

    if sys.platform == "darwin":
        candidates.extend(
            [
                ("plus", home / "Library" / "Application Support" / "Stata" / "ado" / "plus"),
                ("personal", home / "Library" / "Application Support" / "Stata" / "ado" / "personal"),
                ("plus", home / "ado" / "plus"),
                ("personal", home / "ado" / "personal"),
            ]
        )
    elif os.name == "nt":
        user_profile = Path(os.environ.get("USERPROFILE", str(home)))
        candidates.extend(
            [
                ("plus", Path("C:/ado/plus")),
                ("personal", Path("C:/ado/personal")),
                ("plus", user_profile / "ado" / "plus"),
                ("personal", user_profile / "ado" / "personal"),
            ]
        )
    else:
        candidates.extend(
            [
                ("plus", home / "ado" / "plus"),
                ("personal", home / "ado" / "personal"),
            ]
        )

    if sp and sp.is_dir():
        candidates.extend(
            [
                ("site", sp / "ado" / "site"),
                ("base", sp / "ado" / "base"),
            ]
        )

    seen: Set[str] = set()
    roots: List[Tuple[str, str]] = []
    for label, path in candidates:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen:
            continue
        if path.is_dir():
            seen.add(resolved)
            roots.append((label, resolved))
    return roots


def _load_aliases(stata_path: str = "") -> Dict[str, str]:
    key = stata_path or ""
    if key in _ALIAS_CACHE:
        return _ALIAS_CACHE[key]

    aliases: Dict[str, str] = {}
    for _label, root in find_ado_roots(stata_path):
        root_path = Path(root)
        try:
            subdirs = sorted(root_path.iterdir())
        except OSError:
            continue
        for sub in subdirs:
            if not sub.is_dir():
                continue
            try:
                maint_files = list(sub.glob("*help_alias.maint")) + list(sub.glob("*smcl_alias.maint"))
            except OSError:
                continue
            for maint in maint_files:
                try:
                    text = maint.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for raw_line in text.splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("*"):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        alias_name = parts[0].lower()
                        target = parts[1]
                        aliases.setdefault(alias_name, target)
    _ALIAS_CACHE[key] = aliases
    return aliases


def _find_help_file(topic: str, stata_path: str = "") -> Optional[Tuple[str, str]]:
    """Return ``(source_label, file_path)`` for a help topic or INCLUDE file."""
    if not topic:
        return None
    cleaned = topic.strip()
    if not cleaned:
        return None

    # Allow explicit extension when called from {INCLUDE ...}
    has_ext = cleaned.lower().endswith((".sthlp", ".hlp", ".ihlp"))
    first_char = cleaned[0].lower()
    roots = find_ado_roots(stata_path)

    for label, root in roots:
        root_path = Path(root)
        candidates: List[Path] = []
        if has_ext:
            candidates.extend([root_path / first_char / cleaned, root_path / cleaned])
        else:
            for ext in (".sthlp", ".hlp", ".ihlp"):
                candidates.append(root_path / first_char / (cleaned + ext))
                candidates.append(root_path / (cleaned + ext))
                if cleaned.lower() != cleaned:
                    candidates.append(root_path / first_char / (cleaned.lower() + ext))
                    candidates.append(root_path / (cleaned.lower() + ext))
        for cand in candidates:
            if cand.is_file():
                return label, str(cand)
    return None


def _normalize_topic(topic: str) -> str:
    value = (topic or "").strip().lstrip("#")
    value = value.split(",")[0].strip()
    value = re.sub(r"\s+", "_", value)
    return value


def _find_matching_brace(text: str, start: int) -> int:
    depth = 1
    idx = start + 1
    length = len(text)
    while idx < length:
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
        idx += 1
    return -1


def _split_tag_body(body: str) -> Tuple[str, str, Optional[str]]:
    """Split inside of ``{...}`` into ``(name, args, inner_text)``."""
    first_space = -1
    for idx, ch in enumerate(body):
        if ch.isspace():
            first_space = idx
            break
    first_word = (body[:first_space] if first_space != -1 else body).strip()
    head_name = first_word.split(":", 1)[0].lower()

    # For {opt a:b}, {opth a:b(...)}, {cmdab:a:b}, {help topic:label}, etc.,
    # check whether the colon separates the tag/args from inner text vs sits inside an option spec.
    if head_name in ("opt", "opth") and first_space != -1:
        # e.g. `{opt nocons:tant}` -> tag="opt", args="", inner="nocons:tant"
        #      `{opt :foo}` -> tag="opt", args="", inner="foo"
        rest = body[first_space + 1 :].lstrip()
        if rest.startswith(":"):
            rest = rest[1:]
        return head_name, "", rest

    colon_idx = body.find(":")
    if colon_idx != -1:
        prefix = body[:colon_idx].strip()
        inner = body[colon_idx + 1 :]
        parts = prefix.split(None, 1)
        name = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return name, args, inner
    parts = body.split(None, 1)
    name = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    return name, args, None


def _format_opt(raw: str) -> str:
    """Format SMCL ``{opt a:b}`` or ``{opt a:b(args)}`` cleanly."""
    text = raw.strip()
    # Strip help cross-reference targets inside parentheses like `vce(regress##vcetype:vcetype)`
    if "##" in text:
        text = re.sub(r"\([^)]*##[^:)]*:\s*([^)]+)\)", r"(\1)", text)
        if "##" in text and ":" in text:
            text = text.split(":", 1)[1]
    # Keep minimal abbreviation visible: e.g. nocons:tant -> nocons[tant]
    if ":" in text:
        head, tail = text.split("(", 1) if "(" in text else (text, "")
        if ":" in head:
            left, right = head.split(":", 1)
            m = re.match(r"^([A-Za-z0-9_]+)(.*)$", right)
            if m and m.group(1):
                base = "%s[%s]%s" % (left, m.group(1), m.group(2))
            else:
                base = left + right
        else:
            base = head
        if tail:
            if ":" in tail:
                tail = tail.split(":", 1)[1]
            return "%s(%s" % (base, tail)
        return base
    return text


def _resolve_char_code(code: str) -> str:
    c = code.strip()
    if c in _SMCL_CHAR_CODES:
        return _SMCL_CHAR_CODES[c]
    if c.lower().startswith("0x"):
        try:
            return chr(int(c[2:], 16))
        except ValueError:
            return c
    try:
        num = int(c)
        if 1 <= num <= 0x10FFFF:
            return chr(num)
    except ValueError:
        pass
    return c


def _expand_includes(
    raw_smcl: str,
    include_resolver: Optional[Callable[[str], Optional[str]]],
    depth: int = 0,
    visited: Optional[Set[str]] = None,
) -> str:
    if not include_resolver or depth >= 4:
        return raw_smcl
    seen = set(visited or ())

    def _resolve_target(target: str) -> str:
        t = target.strip().strip('"').strip("'")
        key = t.lower()
        if not t or key in seen:
            return ""
        included = include_resolver(t)
        if not included:
            return ""
        seen.add(key)
        return _expand_includes(included, include_resolver, depth + 1, seen)

    expanded = re.sub(r"\{INCLUDE\s+([^}]+)\}", lambda m: _resolve_target(m.group(1)), raw_smcl, flags=re.IGNORECASE)
    # Stata also uses bare line directive `INCLUDE help <topic>` (e.g. `INCLUDE help fvvarlist`)
    expanded = re.sub(
        r"(?im)^[ \t]*INCLUDE[ \t]+help[ \t]+([A-Za-z0-9_.-]+)[ \t]*$",
        lambda m: _resolve_target(m.group(1)) or ("(see help %s)" % m.group(1)),
        expanded,
    )
    return expanded


def smcl_to_text(
    raw_smcl: str,
    include_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Convert SMCL help source to clean plain text with section indexing."""
    text = (raw_smcl or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _expand_includes(text, include_resolver)
    # Remove SMCL line-continuation markers `{...}` followed by newline
    text = re.sub(r"\{\.\.\.\}[ \t]*\n?", "", text)
    # Remove SMCL comments `{* ...}`
    text = re.sub(r"\{\*[^}]*\}", "", text)

    also_see: List[str] = []
    markers_pending: List[str] = []
    section_events: List[Tuple[int, str, str]] = []  # (char_offset, key, title)
    out: List[str] = []

    def _render_span(span: str, top_level: bool = False) -> str:
        buf: List[str] = []
        i = 0
        n = len(span)
        while i < n:
            ch = span[i]
            if ch != "{":
                buf.append(ch)
                i += 1
                continue
            end = _find_matching_brace(span, i)
            if end == -1:
                buf.append(ch)
                i += 1
                continue
            body = span[i + 1 : end]
            i = end + 1
            if not body.strip():
                continue

            name, args, inner = _split_tag_body(body)

            if name == "vieweralsosee":
                m_see = re.findall(r'"([^"]+)"', args)
                if m_see and m_see[0].strip() and m_see[0].strip() != "--":
                    also_see.append(m_see[0].strip())
                continue
            if name in _SMCL_IGNORED_TAGS:
                if name == "c":
                    buf.append(_resolve_char_code(args))
                continue
            if name in _SMCL_PLACEHOLDERS and inner is None:
                buf.append(_SMCL_PLACEHOLDERS[name])
                continue
            if name == "marker":
                m_key = (inner if inner is not None else args).strip()
                if m_key:
                    markers_pending.append(m_key.lower())
                continue
            if name == "title":
                title_txt = _render_span(inner if inner is not None else args, False).strip()
                if title_txt:
                    offset = sum(len(x) for x in out) + sum(len(x) for x in buf)
                    sec_key = markers_pending[-1] if markers_pending else re.sub(r"\W+", "_", title_txt.lower()).strip("_")
                    markers_pending.clear()
                    section_events.append((offset, sec_key, title_txt))
                    buf.append("\n\n## %s\n" % title_txt)
                continue
            if name == "syntab":
                tab_txt = _render_span(inner if inner is not None else args, False).strip()
                if tab_txt:
                    buf.append("\n### %s\n" % tab_txt)
                continue
            if name == "synoptline":
                buf.append("\n" + "-" * 60 + "\n")
                continue
            if name == "synopt":
                # {synopt :{opt nocons:tant}}description{p_end}
                label_txt = _render_span(inner if inner is not None else args, False).strip()
                buf.append("\n  %s  --  " % label_txt)
                continue
            if name in ("p", "pstd", "pmore", "phang", "phang2", "phang3", "pin", "pin2", "pin3"):
                buf.append("\n")
                if inner is not None:
                    buf.append(_render_span(inner, False))
                continue
            if name == "p2col":
                col_txt = _render_span(inner if inner is not None else args, False).strip()
                if col_txt:
                    buf.append("\n  %-20s " % col_txt)
                continue
            if name == "hline":
                try:
                    cnt = int(args.strip()) if args.strip() else 40
                except ValueError:
                    cnt = 40
                buf.append("-" * min(max(cnt, 2), 60))
                continue
            if name == "space":
                try:
                    cnt = int(args.strip()) if args.strip() else 1
                except ValueError:
                    cnt = 1
                buf.append(" " * min(max(cnt, 1), 20))
                continue
            if name == "col":
                buf.append("  ")
                continue
            if name in ("opt", "opth"):
                raw_opt = inner if inner is not None else args
                # e.g. {opth vce(vcetype)} or {opt nocons:tant} or {opth "vce(regress##vcetype:vcetype)"}
                rendered_opt = _render_span(raw_opt, False).strip()
                if name == "opth" and "(" in rendered_opt:
                    # Clean `vce(regress##vcetype vcetype)` -> `vce(vcetype)`
                    rendered_opt = re.sub(r"\([^)]*##[^)\s]*\s+([^)]+)\)", r"(\1)", rendered_opt)
                buf.append(_format_opt(rendered_opt))
                continue
            if name in ("help", "helpb", "mansection", "browse", "stata", "dialog", "newvar"):
                if inner is not None:
                    buf.append(_render_span(inner, False))
                else:
                    # {help topic##sub:label} or {help topic}
                    cleaned_arg = args.strip().strip('"')
                    if "##" in cleaned_arg:
                        cleaned_arg = cleaned_arg.split("##", 1)[1]
                    buf.append(cleaned_arg)
                continue
            if name in _SMCL_INLINE_TEXT_TAGS or inner is not None:
                content = inner if inner is not None else args
                buf.append(_render_span(content, False))
                continue
            if args:
                buf.append(args)
        return "".join(buf)

    rendered = _render_span(text, top_level=True)

    # Clean whitespace while preserving section offsets reliably by splitting on `## ` headings
    lines = [line.rstrip() for line in rendered.splitlines()]
    cleaned_lines: List[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                cleaned_lines.append("")
        else:
            blank_run = 0
            cleaned_lines.append(line)
    full_text = "\n".join(cleaned_lines).strip()

    # Build section dictionary from headings in full_text
    sections: Dict[str, str] = {}
    section_names: List[str] = []
    heading_matches = list(re.finditer(r"(?m)^## (.+)$", full_text))
    # Map titles to marker keys when available
    title_to_key = {title: key for _off, key, title in section_events}
    for idx, m in enumerate(heading_matches):
        title = m.group(1).strip()
        start = m.start()
        end = heading_matches[idx + 1].start() if idx + 1 < len(heading_matches) else len(full_text)
        sec_key = title_to_key.get(title) or re.sub(r"\W+", "_", title.lower()).strip("_")
        sec_body = full_text[start:end].strip()
        sections[sec_key] = sec_body
        section_names.append(title)

    # Deduplicate also_see preserving order
    dedup_see: List[str] = []
    for item in also_see:
        if item not in dedup_see:
            dedup_see.append(item)

    return {
        "text": full_text,
        "sections": sections,
        "section_names": section_names,
        "also_see": dedup_see[:15],
    }


def resolve_help(
    topic: str,
    stata_path: str = "",
    section: str = "",
    max_chars: int = 16000,
) -> Dict[str, Any]:
    """Resolve and render a Stata help topic from local ado roots."""
    norm = _normalize_topic(topic)
    if not norm:
        return {"found": False, "topic": topic, "error": "empty help topic"}

    aliases = _load_aliases(stata_path)
    candidates = [norm]
    lower_norm = norm.lower()
    if lower_norm != norm:
        candidates.append(lower_norm)
    if lower_norm in aliases:
        candidates.insert(0, aliases[lower_norm])
    if "_" in lower_norm and lower_norm.endswith("_post"):
        candidates.append(lower_norm + "estimation")

    found_file: Optional[Tuple[str, str]] = None
    resolved_topic = norm
    for cand in candidates:
        res = _find_help_file(cand, stata_path)
        if res:
            found_file = res
            resolved_topic = cand
            break

    if not found_file:
        return {
            "found": False,
            "topic": norm,
            "resolved_topic": resolved_topic,
            "source_file": "",
            "sections_available": [],
            "also_see": [],
            "content": "",
            "truncated": False,
        }

    source_label, file_path = found_file
    try:
        raw_smcl = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"found": False, "topic": norm, "error": str(exc)}

    def _include_resolver(inc_name: str) -> Optional[str]:
        res = _find_help_file(inc_name, stata_path)
        if not res:
            return None
        try:
            return Path(res[1]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    parsed = smcl_to_text(raw_smcl, include_resolver=_include_resolver)
    content = parsed["text"]
    sections = parsed["sections"]

    if section and section.strip():
        q = section.strip().lower()
        matched_parts: List[str] = []
        for sec_key, sec_body in sections.items():
            heading = sec_body.splitlines()[0].lstrip("# ").lower() if sec_body else ""
            if q == sec_key or q in sec_key or q in heading:
                matched_parts.append(sec_body)
        if matched_parts:
            content = "\n\n".join(matched_parts)
        else:
            content = (
                "Section %r not found for topic %r. Available sections: %s\n\n%s"
                % (section, resolved_topic, ", ".join(parsed["section_names"]) or "(none)", content)
            )

    limit = max(500, min(int(max_chars or 16000), 60000))
    truncated = False
    if len(content) > limit:
        content = content[:limit].rstrip() + "\n\n… (truncated; pass section='syntax'|'options'|'examples'|'results' to focus)"
        truncated = True

    return {
        "found": True,
        "topic": norm,
        "resolved_topic": resolved_topic,
        "source": source_label,
        "source_file": file_path,
        "sections_available": parsed["section_names"],
        "also_see": parsed["also_see"],
        "content": content,
        "truncated": truncated,
    }


def _extract_sthlp_title(path: Path) -> str:
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            head = "".join(handle.readline() for _ in range(55))
    except OSError:
        return ""
    for line in head.splitlines():
        if "{hline" in line and "{p2col:" in line:
            parts = line.split("{hline", 1)
            if len(parts) == 2:
                after = re.sub(r"^\s*\d*\}+", "", parts[1])
                after = re.sub(r"\{[^}]*\}", " ", after)
                after = re.sub(r"^[}\s]+", "", after)
                after = re.sub(r"\s+", " ", after).strip()
                if after:
                    return after
    m2 = re.search(r"\{title\s*:\s*([^}]+)\}", head)
    if m2:
        t = m2.group(1).strip()
        if t.lower() != "syntax":
            return t
    return ""


def _build_search_index(stata_path: str = "") -> List[Dict[str, str]]:
    key = stata_path or ""
    if key in _SEARCH_INDEX_CACHE:
        return _SEARCH_INDEX_CACHE[key]

    entries: List[Dict[str, str]] = []
    seen_topics: Set[str] = set()

    for label, root in find_ado_roots(stata_path):
        root_path = Path(root)
        try:
            items = list(root_path.rglob("*.sthlp")) + list(root_path.rglob("*.hlp"))
        except OSError:
            continue
        for item in items:
            stem = item.stem
            low = stem.lower()
            if low in seen_topics:
                continue
            seen_topics.add(low)
            entries.append({"topic": stem, "target": stem, "source": label, "path": str(item)})

    for alias_name, target in _load_aliases(stata_path).items():
        if alias_name not in seen_topics:
            entries.append({"topic": alias_name, "target": target, "source": "alias", "path": ""})

    _SEARCH_INDEX_CACHE[key] = entries
    return entries


def search_help(query: str, stata_path: str = "", limit: int = 20) -> Dict[str, Any]:
    """Search installed Stata help topics and aliases."""
    q = (query or "").strip().lower()
    if not q:
        return {"query": query, "count": 0, "results": []}

    tokens = [tok for tok in re.split(r"[\s_]+", q) if tok]
    index = _build_search_index(stata_path)
    scored: List[Tuple[int, Dict[str, str]]] = []
    for item in index:
        topic_l = item["topic"].lower()
        target_l = item["target"].lower()
        score = 0
        if topic_l == q or target_l == q:
            score = 100
        elif topic_l.startswith(q) or target_l.startswith(q):
            score = 80
        elif all(tok in topic_l or tok in target_l for tok in tokens):
            score = 60
        elif any(tok in topic_l for tok in tokens):
            score = 30
        if score > 0:
            # Slightly prefer direct files over aliases when score ties
            if item["source"] != "alias":
                score += 5
            scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], len(pair[1]["topic"]), pair[1]["topic"]))
    max_n = max(1, min(int(limit or 20), 50))
    results: List[Dict[str, Any]] = []
    for score, item in scored[:max_n]:
        title = ""
        if item["path"]:
            title = _extract_sthlp_title(Path(item["path"]))
        results.append(
            {
                "topic": item["topic"],
                "target": item["target"],
                "source": item["source"],
                "title": title,
                "score": score,
            }
        )
    return {"query": query, "count": len(results), "results": results}


def which_local(command: str, stata_path: str = "") -> Dict[str, Any]:
    """Inspect local ado/help files for ``command`` without invoking Stata."""
    raw_cmd = (command or "").strip().split()[0] if command else ""
    if not raw_cmd:
        return {"command": command, "found": False}
    aliases = _load_aliases(stata_path)
    resolved_cmd = aliases.get(raw_cmd.lower(), raw_cmd)
    candidates = [raw_cmd] if resolved_cmd.lower() == raw_cmd.lower() else [raw_cmd, resolved_cmd]

    ado_path = ""
    ado_source = ""
    version_line = ""
    help_path = ""
    matched_cmd = raw_cmd
    for label, root in find_ado_roots(stata_path):
        rp = Path(root)
        for cand_cmd in candidates:
            first = cand_cmd[0].lower()
            if not ado_path:
                for cand in (rp / first / (cand_cmd + ".ado"), rp / (cand_cmd + ".ado")):
                    if cand.is_file():
                        ado_path = str(cand)
                        ado_source = label
                        matched_cmd = cand_cmd
                        try:
                            with open(ado_path, "r", encoding="utf-8", errors="replace") as handle:
                                for _ in range(10):
                                    line = handle.readline()
                                    if line.strip().startswith("*!"):
                                        version_line = line.strip()
                                        break
                        except OSError:
                            pass
                        break
            if not help_path:
                for ext in (".sthlp", ".hlp"):
                    for cand in (rp / first / (cand_cmd + ext), rp / (cand_cmd + ext)):
                        if cand.is_file():
                            help_path = str(cand)
                            if not ado_path:
                                matched_cmd = cand_cmd
                            break
                    if help_path:
                        break
    return {
        "command": raw_cmd,
        "resolved_command": resolved_cmd,
        "alias_of": resolved_cmd if resolved_cmd.lower() != raw_cmd.lower() else "",
        "matched_command": matched_cmd,
        "found": bool(ado_path or help_path),
        "ado_path": ado_path,
        "source": ado_source,
        "version_header": version_line,
        "help_path": help_path,
    }


# ---------------------------------------------------------------------------
# 2. Do-File Logical Statement Parser & Exact Error Line Locator
# ---------------------------------------------------------------------------
ERROR_CATALOG: Dict[int, Dict[str, str]] = {
    100: {
        "title": "varlist required",
        "cause": "Command requires one or more variable names that were omitted.",
        "fix_hint": "Pass the required variable(s) or check syntax with stata_help.",
    },
    101: {
        "title": "varlist not allowed",
        "cause": "A variable list was passed to a command that does not accept one.",
        "fix_hint": "Check command syntax with stata_help and remove the unexpected varlist.",
    },
    102: {
        "title": "too few variables specified",
        "cause": "Fewer variables were provided than the command requires.",
        "fix_hint": "Provide all required variables (e.g. depvar and indepvars).",
    },
    103: {
        "title": "too many variables specified",
        "cause": "More variables were supplied than allowed by the command.",
        "fix_hint": "Verify argument count in stata_help.",
    },
    107: {
        "title": "not possible with weighted data",
        "cause": "The chosen command or option does not allow weights.",
        "fix_hint": "Remove weights or check supported weight types in stata_help.",
    },
    109: {
        "title": "type mismatch",
        "cause": "String vs numeric mismatch (e.g. arithmetic on string var or string literal on numeric var).",
        "fix_hint": "Check variable types with stata_describe; convert via destring/real() or tostring/string().",
    },
    110: {
        "title": "variable already defined",
        "cause": "Tried to generate/rename/create a variable that already exists.",
        "fix_hint": "Use `replace`, `capture drop var`, or choose a new variable name.",
    },
    111: {
        "title": "variable not found",
        "cause": "Referenced variable does not exist in the current dataset (or case typo).",
        "fix_hint": "Run stata_describe to inspect actual variable names and capitalization.",
    },
    119: {
        "title": "statement out of context",
        "cause": "Statement used outside its required block or context (e.g. `else` or `continue` outside loop).",
        "fix_hint": "Check brace matching `{` / `}` and block structure.",
    },
    121: {
        "title": "invalid numlist",
        "cause": "Malformed numeric list in option or `forvalues`.",
        "fix_hint": "Check numlist syntax (e.g. `1/10`, `1(2)9`).",
    },
    131: {
        "title": "not possible with test",
        "cause": "Hypothesis test syntax or parameter name invalid.",
        "fix_hint": "Inspect stored coefficient names via stata_stored_results(kind='e').",
    },
    133: {
        "title": "unknown function",
        "cause": "Called an unrecognized Stata/egen function.",
        "fix_hint": "Check `stata_help(topic='functions')` or `stata_help(topic='egen')`.",
    },
    198: {
        "title": "syntax error / invalid syntax",
        "cause": "Invalid option, unmatched parenthesis/quote, unexpected token, or malformed expression.",
        "fix_hint": "Check exact command syntax and allowed options via stata_help.",
    },
    199: {
        "title": "unrecognized command",
        "cause": "Command name is misspelled or the community package (SSC) is not installed.",
        "fix_hint": "Run `stata_which` to check installation, or `stata_search_help` for similar commands.",
    },
    301: {
        "title": "last estimates not found",
        "cause": "Postestimation command (predict, margins, test, esttab) run with no active estimation results.",
        "fix_hint": "Run the estimation command first or check `stata_stored_results(kind='estimates')`.",
    },
    451: {
        "title": "repeated time values within panel",
        "cause": "`xtset id time` or `tsset` found duplicate `(id, time)` observations.",
        "fix_hint": "Run `duplicates report id time` and resolve duplicates before xtset/tsset.",
    },
    459: {
        "title": "something that should be true of your data is not",
        "cause": "Data integrity assertion failed (e.g. `merge` assert, `isid`, or panel structure requirement).",
        "fix_hint": "Inspect offending rows with `stata_inspect_data` or `duplicates list`.",
    },
    498: {
        "title": "general package/program error",
        "cause": "User-written command exited with a specific validation error.",
        "fix_hint": "Read the preceding error message line and check `stata_help` for that command.",
    },
    601: {
        "title": "file not found",
        "cause": "Specified `.dta`, `.csv`, `.do`, or output path does not exist or working_dir differs.",
        "fix_hint": "Check `c(pwd)` via `stata_stored_results(kind='c')` and verify the file path.",
    },
    602: {
        "title": "file already exists",
        "cause": "`save`, `export`, or `log` tried to overwrite an existing file without `, replace`.",
        "fix_hint": "Add `, replace` to the command.",
    },
    603: {
        "title": "file could not be opened",
        "cause": "Permission denied, directory missing, or file locked by another process.",
        "fix_hint": "Verify parent directory exists and close any open handle (e.g. Excel).",
    },
    612: {
        "title": "unexpected end of file",
        "cause": "Corrupted or truncated data/import file, or unclosed quote in CSV.",
        "fix_hint": "Check file encoding, delimiter, and quote options in `import delimited`.",
    },
    621: {
        "title": "already preserved",
        "cause": "`preserve` called twice without `restore` (or previous run crashed mid-preserve).",
        "fix_hint": "Run `restore` or `capture restore, not`, or prefer `frame` instead of `preserve`.",
    },
    1000: {
        "title": "system limit exceeded",
        "cause": "Too many variables, macros, or matsize limit hit.",
        "fix_hint": "Increase `set maxvar` / `set matsize` or simplify factor/dummy expansion.",
    },
    2000: {
        "title": "no observations",
        "cause": "No observations remain (often due to `if` filter, string-numeric mismatch, or missing values).",
        "fix_hint": "Inspect sample size with `stata_describe` or check `if` condition.",
    },
    2001: {
        "title": "insufficient observations",
        "cause": "Too few non-missing observations to estimate the model.",
        "fix_hint": "Check missingness across model variables via `stata_describe`.",
    },
    3000: {
        "title": "Mata run-time error",
        "cause": "Error inside a Mata block or Mata-backed command (e.g. reghdfe, estout).",
        "fix_hint": "Check preceding Mata traceback lines and verify variable/matrix dimensions.",
    },
    3499: {
        "title": "Mata object/function not found",
        "cause": "Required Mata library (.mlib) not indexed or compiled (common after package install).",
        "fix_hint": "Run `mata: mata mlib index` or reinstall the package.",
    },
}

_PREFIX_TOKENS = {
    "qui",
    "quie",
    "quiet",
    "quietl",
    "quietly",
    "cap",
    "capt",
    "captu",
    "captur",
    "capture",
    "noi",
    "nois",
    "noisi",
    "noisil",
    "noisily",
    "xi",
    "version",
}

_BLOCK_OPEN_KEYWORDS = ("foreach", "forvalues", "forval", "forv", "while", "if", "else", "program")


def _strip_stmt_prefixes(normalized: str) -> str:
    """Strip leading Stata prefixes (`qui`, `cap`, `noi`, `version N`) from a statement."""
    tokens = normalized.strip().split()
    while tokens:
        tok = tokens[0].rstrip(":")
        low = tok.lower()
        if low in _PREFIX_TOKENS:
            tokens.pop(0)
            if low == "version" and tokens:
                tokens.pop(0)
            continue
        break
    return " ".join(tokens)


def _strip_inline_comment(line: str, in_block_comment: int) -> Tuple[str, int, bool]:
    """Remove ``/* ... */`` and ``//`` / ``///`` comments outside string literals.

    Returns ``(code_without_comments, new_block_comment_depth, has_continuation_slash)``.
    """
    out: List[str] = []
    i = 0
    n = len(line)
    in_dq = False
    has_cont = False

    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""

        if in_block_comment > 0:
            if ch == "/" and nxt == "*":
                in_block_comment += 1
                i += 2
                continue
            if ch == "*" and nxt == "/":
                in_block_comment -= 1
                i += 2
                continue
            i += 1
            continue

        if in_dq:
            out.append(ch)
            if ch == '"':
                in_dq = False
            i += 1
            continue

        # Not inside string or block comment
        if ch == '"':
            in_dq = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment += 1
            i += 2
            continue
        if ch == "/" and nxt == "/":
            # Check for `///` continuation (only if preceded by start or whitespace)
            if i + 2 < n and line[i + 2] == "/" and (i == 0 or line[i - 1].isspace()):
                has_cont = True
            break
        out.append(ch)
        i += 1

    return "".join(out), in_block_comment, has_cont


def _extract_verb(normalized: str) -> str:
    """Return the primary Stata command verb after stripping prefixes/by-clauses."""
    s = normalized.strip()
    if not s:
        return ""
    # Strip leading `by ...:` or `bysort ...:`
    m_by = re.match(r"^(?:by|bys|bysort)\b[^:]*:\s*(.+)$", s, flags=re.IGNORECASE)
    if m_by:
        s = m_by.group(1).strip()
    tokens = s.split()
    while tokens:
        tok = tokens[0].rstrip(":")
        low = tok.lower()
        if low in _PREFIX_TOKENS:
            tokens.pop(0)
            if low == "version" and tokens:
                tokens.pop(0)
            continue
        return low
    return ""


def _detect_block_open(normalized: str) -> Optional[str]:
    s = _strip_stmt_prefixes(normalized)
    if not s.endswith("{"):
        return None
    low = s.lower()
    if re.match(r"^forv(?:a(?:l(?:u(?:e(?:s)?)?)?)?)?(?:\b|\s|\{)", low):
        return "forvalues"
    for kw in ("foreach", "while", "else", "if", "program"):
        if re.match(r"^" + kw + r"(?:\b|\s|\{)", low):
            return kw
    if low == "{":
        return "block"
    return "block"


def parse_do_statements(source_text: str) -> List[Dict[str, Any]]:
    """Parse a Stata ``.do`` file into logical statements with 1-based line spans."""
    text = (source_text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = text.split("\n")

    statements: List[Dict[str, Any]] = []
    block_stack: List[Dict[str, Any]] = []
    in_block_comment = 0
    delimit_semi = False

    acc_raw: List[str] = []
    acc_norm: List[str] = []
    acc_start: Optional[int] = None
    in_mata = False

    def _flush(end_line: int) -> None:
        nonlocal acc_raw, acc_norm, acc_start, delimit_semi, in_mata
        if acc_start is None:
            return
        raw_joined = "\n".join(acc_raw)
        norm_joined = re.sub(r"\s+", " ", " ".join(part.strip() for part in acc_norm if part.strip())).strip()
        start_line = acc_start
        acc_raw = []
        acc_norm = []
        acc_start = None

        if not norm_joined:
            return

        # Handle #delimit directives
        m_del = re.match(r"^#delimit\s+(\S+)", norm_joined, flags=re.IGNORECASE)
        if m_del:
            delimit_semi = m_del.group(1).strip() == ";"
            return

        if re.match(r"^mata\s*:\s*$", norm_joined, flags=re.IGNORECASE) or norm_joined.lower() == "mata":
            in_mata = True
        elif in_mata and norm_joined.lower() == "end":
            in_mata = False

        # Handle closing braces before recording enclosing depth for this statement
        starts_with_close = norm_joined.startswith("}")
        if starts_with_close and block_stack:
            block_stack.pop()

        enclosing = [dict(b) for b in block_stack]
        verb = _extract_verb(norm_joined)
        stmt = {
            "index": len(statements),
            "start_line": start_line,
            "end_line": end_line,
            "raw_text": raw_joined,
            "normalized": norm_joined,
            "verb": verb,
            "block_depth": len(enclosing),
            "enclosing_blocks": enclosing,
            "in_mata": in_mata,
        }
        statements.append(stmt)

        # Open new block if line ends with `{` (and not also closed on same line)
        opened = _detect_block_open(norm_joined)
        if opened and not (norm_joined.endswith("}") and norm_joined.count("{") <= norm_joined.count("}")):
            block_stack.append({"kind": opened, "header": norm_joined, "start_line": start_line})
        elif norm_joined.count("}") > norm_joined.count("{") and not starts_with_close:
            pops = norm_joined.count("}") - norm_joined.count("{")
            for _ in range(pops):
                if block_stack:
                    block_stack.pop()

    for idx, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        # Full-line `*` comment (only outside block comments and not continuing a statement)
        if in_block_comment == 0 and acc_start is None and stripped.startswith("*"):
            continue

        code_part, in_block_comment, has_cont = _strip_inline_comment(raw, in_block_comment)
        if acc_start is None and not code_part.strip():
            continue

        # `#delimit cr` / `#delimit ;` always takes effect on its own line in Stata
        if in_block_comment == 0 and acc_start is None and re.match(r"^#delimit\s+\S+", code_part.strip(), flags=re.IGNORECASE):
            m_del = re.match(r"^#delimit\s+(\S+)", code_part.strip(), flags=re.IGNORECASE)
            if m_del:
                mode_tok = m_del.group(1).strip().lower()
                delimit_semi = mode_tok.startswith(";")
            continue

        if acc_start is None:
            acc_start = idx
        acc_raw.append(raw)

        if delimit_semi:
            # In `;` mode, `#delimit cr` still ends the directive immediately even without `;`
            if re.match(r"^#delimit\s+cr\b", code_part.strip(), flags=re.IGNORECASE):
                acc_raw.pop()
                if acc_raw:
                    _flush(idx - 1)
                else:
                    acc_start = None
                    acc_norm = []
                delimit_semi = False
                continue
            # In `;` mode, a semicolon ends the logical statement
            if ";" in code_part:
                before_semi, after_semi = code_part.split(";", 1)
                acc_norm.append(before_semi)
                _flush(idx)
                remainder = after_semi.strip()
                while ";" in remainder:
                    seg, remainder = remainder.split(";", 1)
                    acc_start = idx
                    acc_raw = [raw]
                    acc_norm = [seg]
                    _flush(idx)
                    remainder = remainder.strip()
                if remainder:
                    acc_start = idx
                    acc_raw = [raw]
                    acc_norm = [remainder]
            else:
                acc_norm.append(code_part)
        else:
            acc_norm.append(code_part)
            if not has_cont and in_block_comment == 0:
                _flush(idx)

    _flush(len(raw_lines))
    return statements


def _parse_log_lines_to_records(error_log: str) -> List[Dict[str, Any]]:
    """Convert raw Stata log text into synthetic gateway record dicts."""
    records: List[Dict[str, Any]] = []
    seq = 0
    step = 0
    for raw in (error_log or "").replace("\r\n", "\n").splitlines():
        text = raw.rstrip("\r\n")
        stripped = text.strip()
        if not stripped or stripped in (".", ">"):
            continue
        seq += 1
        if stripped.startswith((". ", "> ")):
            step += 1
            kind = "command"
        elif re.match(r"^r\(\d+\);$", stripped) or stripped.startswith("ERROR:"):
            kind = "error"
        else:
            kind = "output"
        records.append({"seq": seq, "step": step, "t": 0.0, "kind": kind, "text": text})
    return records


def _stitch_executed_commands(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse `. ` + `> ` continuations and block numbered lines `N. `."""
    commands: List[Dict[str, Any]] = []
    for idx, rec in enumerate(records):
        kind = rec.get("kind")
        text = rec.get("text", "")
        stripped = text.strip()
        if kind == "command":
            if stripped.startswith("> ") and commands:
                commands[-1]["text"] += " " + stripped[2:].strip()
                commands[-1]["end_idx"] = idx
            else:
                cmd = stripped[2:].strip() if stripped.startswith(". ") else stripped
                commands.append({"idx": idx, "end_idx": idx, "step": rec.get("step", 0), "text": cmd})
        elif kind == "output":
            # Stata echoes statements inside foreach/forvalues/if blocks as `  2. gen x = 1`
            m_blk = re.match(r"^\s*\d+\.\s+(.+)$", text)
            if m_blk:
                inner = m_blk.group(1).strip()
                # Ignore Stata output like `1. variable ...` if it's obviously a list/table header
                if not re.match(r"^(obs|str|float|double|int|long|byte)\b", inner, flags=re.IGNORECASE):
                    commands.append({"idx": idx, "end_idx": idx, "step": rec.get("step", 0), "text": inner})
    return commands


def detect_stata_error(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Detect a fatal Stata error from job records (ignoring capture-swallowed rc)."""
    if not records:
        return None

    # Find all r(NNN); occurrences
    rc_indices: List[Tuple[int, int]] = []
    for idx, rec in enumerate(records):
        txt = (rec.get("text") or "").strip()
        m = re.match(r"^r\((\d+)\);$", txt)
        if m:
            rc_indices.append((idx, int(m.group(1))))

    fatal_idx: Optional[int] = None
    fatal_rc: Optional[int] = None

    if rc_indices:
        # Check the last r(NNN); first: in Stata, a fatal error stops the do-file
        # (`end of do-file` follows immediately, or no later real command executes).
        last_idx, last_rc = rc_indices[-1]
        tail_records = records[last_idx + 1 :]
        subsequent_cmds = [
            r
            for r in tail_records
            if r.get("kind") == "command"
            and not re.match(r"^\.\s*(?:capture\s+)?log\s+close\b", (r.get("text") or "").strip(), re.I)
        ]
        saw_end_of_do = any("end of do-file" in (r.get("text") or "").lower() for r in tail_records)
        if saw_end_of_do or not subsequent_cmds:
            fatal_idx = last_idx
            fatal_rc = last_rc

    # Also check gateway/engine ERROR: lines if no r(NNN); was found
    error_banner_idx: Optional[int] = None
    error_banner_msg = ""
    for idx, rec in enumerate(records):
        txt = (rec.get("text") or "").strip()
        if rec.get("kind") == "error" and txt.startswith("ERROR:"):
            error_banner_idx = idx
            error_banner_msg = txt

    if fatal_idx is None:
        if error_banner_idx is None:
            return None
        fatal_idx = error_banner_idx
        m_rc = re.search(r"r\((\d+)\)", error_banner_msg)
        fatal_rc = int(m_rc.group(1)) if m_rc else 0

    # Collect 1-3 explanatory lines right before r(NNN);
    msg_lines: List[str] = []
    for back in range(fatal_idx - 1, max(-1, fatal_idx - 6), -1):
        rec = records[back]
        if rec.get("kind") in ("command", "marker"):
            break
        txt = (rec.get("text") or "").strip()
        if not txt or txt in (".", ">") or txt.lower() == "end of do-file":
            continue
        if re.match(r"^\s*\d+\.\s+", rec.get("text", "")):
            break
        msg_lines.insert(0, txt)
        if len(msg_lines) >= 3:
            break

    error_message = " | ".join(msg_lines) if msg_lines else (error_banner_msg or ("r(%d);" % fatal_rc))

    stitched = _stitch_executed_commands(records[:fatal_idx])
    executed_cmd = stitched[-1]["text"] if stitched else ""
    executed_list = [c["text"] for c in stitched if c["text"]]

    return {
        "error_code": ("r(%d)" % fatal_rc) if fatal_rc else "ERROR",
        "rc": fatal_rc or 0,
        "error_message": error_message,
        "executed_command": executed_cmd,
        "executed_commands": executed_list,
        "error_step": records[fatal_idx].get("step", 0),
        "error_seq": records[fatal_idx].get("seq", fatal_idx + 1),
    }


def _macro_regex_for_statement(stmt_norm: str) -> Optional[re.Pattern]:
    """Turn a source statement containing `` `mac' `` or ``$mac`` into a regex."""
    if "`" not in stmt_norm and "$" not in stmt_norm:
        return None
    parts: List[str] = []
    i = 0
    n = len(stmt_norm)
    while i < n:
        ch = stmt_norm[i]
        if ch == "`":
            close = stmt_norm.find("'", i + 1)
            if close != -1:
                parts.append(r".*?")
                i = close + 1
                continue
        if ch == "$":
            m_brace = re.match(r"\$\{[A-Za-z0-9_]+\}", stmt_norm[i:])
            if m_brace:
                parts.append(r".*?")
                i += len(m_brace.group(0))
                continue
            m_var = re.match(r"\$[A-Za-z0-9_]+", stmt_norm[i:])
            if m_var:
                parts.append(r".*?")
                i += len(m_var.group(0))
                continue
        parts.append(re.escape(ch))
        i += 1
    pattern = "^" + "".join(parts) + "$"
    try:
        return re.compile(pattern, flags=re.IGNORECASE)
    except re.error:
        return None


def _match_score(stmt_norm: str, exec_cmd: str) -> float:
    """Score how well a parsed source statement matches an executed log command."""
    s_clean = re.sub(r"\s+", " ", stmt_norm).strip()
    e_clean = re.sub(r"\s+", " ", exec_cmd).strip()
    if not s_clean or not e_clean:
        return 0.0
    if s_clean == e_clean:
        return 1.0
    if s_clean.lower() == e_clean.lower():
        return 0.98

    # Strip leading `capture` / `quietly` / `noisily` if the log echoed inner stmt
    s_verb = _extract_verb(s_clean)
    e_verb = _extract_verb(e_clean)
    macro_re = _macro_regex_for_statement(s_clean)
    if macro_re and macro_re.match(e_clean):
        return 0.92

    # Also compare without leading prefixes on source statement
    s_unprefixed = re.sub(r"^(?:capture|cap|quietly|qui|noisily|noi)\s+", "", s_clean, flags=re.IGNORECASE).strip()
    if s_unprefixed == e_clean:
        return 0.95
    macro_re2 = _macro_regex_for_statement(s_unprefixed)
    if macro_re2 and macro_re2.match(e_clean):
        return 0.90

    # Token Jaccard similarity (with verb match bonus)
    s_toks = set(re.findall(r"[A-Za-z0-9_]+", s_clean.lower()))
    e_toks = set(re.findall(r"[A-Za-z0-9_]+", e_clean.lower()))
    if not s_toks or not e_toks:
        return 0.0
    inter = len(s_toks & e_toks)
    union = len(s_toks | e_toks)
    jaccard = inter / float(union)
    if s_verb and e_verb and (s_verb == e_verb or s_verb.startswith(e_verb) or e_verb.startswith(s_verb)):
        return min(0.85, 0.35 + 0.55 * jaccard)
    return 0.45 * jaccard


def locate_error_in_do(
    source_text: str,
    records: List[Dict[str, Any]],
    file_path: str = "",
    line_offset: int = 0,
    error_log: str = "",
) -> Optional[Dict[str, Any]]:
    """Pinpoint the exact 1-based line in ``source_text`` where a Stata run failed."""
    recs = records if records else (_parse_log_lines_to_records(error_log) if error_log else [])
    err = detect_stata_error(recs)
    if not err:
        return None

    statements = parse_do_statements(source_text or "")
    raw_lines = (source_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    executed = err["executed_commands"]
    failing_cmd = err["executed_command"]

    matched_stmt: Optional[Dict[str, Any]] = None
    confidence = "low"

    if statements and failing_cmd:
        # Walk prior executed commands monotonically to advance cursor & active block
        cursor = 0
        active_block_start: Optional[int] = None
        for cmd in executed[:-1]:
            cmd_norm = re.sub(r"\s+", " ", cmd).strip()
            if not cmd_norm:
                continue
            # Check forward window
            best_idx = None
            best_sc = 0.0
            for idx in range(cursor, len(statements)):
                sc = _match_score(statements[idx]["normalized"], cmd_norm)
                if sc >= 0.85:
                    best_idx = idx
                    best_sc = sc
                    break
                if sc > best_sc and sc >= 0.55:
                    best_sc = sc
                    best_idx = idx
            if best_idx is not None:
                st = statements[best_idx]
                if _detect_block_open(st["normalized"]):
                    active_block_start = st["start_line"]
                # Do not jump cursor past the active loop body while still iterating
                if active_block_start is None or st["start_line"] >= active_block_start:
                    cursor = best_idx

        # 1) If inside an active loop/block, search inside that block first
        if active_block_start is not None:
            block_candidates = [
                st
                for st in statements
                if any(b["start_line"] == active_block_start for b in st["enclosing_blocks"])
            ]
            best_st = None
            best_sc = 0.0
            for st in block_candidates:
                sc = _match_score(st["normalized"], failing_cmd)
                if sc > best_sc:
                    best_sc = sc
                    best_st = st
            if best_st is not None and best_sc >= 0.45:
                matched_stmt = best_st
                confidence = "high" if best_sc >= 0.80 else "medium"

        # 2) Forward search from monotonic cursor
        if matched_stmt is None:
            best_st = None
            best_sc = 0.0
            for st in statements[cursor:]:
                sc = _match_score(st["normalized"], failing_cmd)
                if sc > best_sc:
                    best_sc = sc
                    best_st = st
                if sc >= 0.95:
                    break
            if best_st is not None and best_sc >= 0.50:
                matched_stmt = best_st
                confidence = "high" if best_sc >= 0.80 else "medium"

        # 3) Full-file fallback
        if matched_stmt is None:
            best_st = None
            best_sc = 0.0
            for st in statements:
                sc = _match_score(st["normalized"], failing_cmd)
                if sc > best_sc:
                    best_sc = sc
                    best_st = st
            if best_st is not None and best_sc >= 0.35:
                matched_stmt = best_st
                confidence = "medium" if best_sc >= 0.70 else "low"

    line = (matched_stmt["start_line"] + int(line_offset)) if matched_stmt else None
    end_line = (matched_stmt["end_line"] + int(line_offset)) if matched_stmt else None

    context_lines: List[str] = []
    if matched_stmt and raw_lines:
        s_l = matched_stmt["start_line"]
        e_l = matched_stmt["end_line"]
        win_start = max(1, s_l - 3)
        win_end = min(len(raw_lines), e_l + 3)
        for ln in range(win_start, win_end + 1):
            marker = ">>>" if s_l <= ln <= e_l else "   "
            context_lines.append("%s %4d | %s" % (marker, ln + int(line_offset), raw_lines[ln - 1]))

    rc = err["rc"]
    catalog_entry = ERROR_CATALOG.get(rc, {})
    verb = matched_stmt["verb"] if matched_stmt else _extract_verb(failing_cmd)
    suggested_help = verb if verb and verb not in ("display", "di", "local", "global") else ""
    if rc == 199 and verb:
        fix_hint = "Command %r not recognized. Check `stata_which(command=%r)` or `stata_search_help(query=%r)`." % (
            verb,
            verb,
            verb,
        )
    elif catalog_entry:
        fix_hint = catalog_entry["fix_hint"]
    else:
        fix_hint = "Inspect the failing line and run `stata_help` on the command if needed."

    return {
        "file_path": file_path,
        "line": line,
        "end_line": end_line,
        "confidence": confidence,
        "error_code": err["error_code"],
        "rc": rc,
        "error_title": catalog_entry.get("title", ""),
        "error_message": err["error_message"],
        "executed_command": failing_cmd,
        "source_line_text": matched_stmt["raw_text"] if matched_stmt else "",
        "enclosing_blocks": matched_stmt["enclosing_blocks"] if matched_stmt else [],
        "source_context": "\n".join(context_lines),
        "cause": catalog_entry.get("cause", ""),
        "fix_hint": fix_hint,
        "suggested_help_topic": suggested_help,
    }


def format_error_diagnosis(diag: Dict[str, Any]) -> str:
    """Format an error diagnosis dict into a concise human/LLM-readable block."""
    if not diag:
        return ""
    loc = (
        "%s:%s" % (diag.get("file_path") or "<selection>", diag["line"])
        if diag.get("line")
        else (diag.get("file_path") or "<selection>")
    )
    if diag.get("end_line") and diag.get("line") and diag["end_line"] != diag["line"]:
        loc += "-%s" % diag["end_line"]
    lines = [
        "[ERROR LOCATION]",
        "Location   : %s (confidence: %s)" % (loc, diag.get("confidence", "unknown")),
        "Code       : %s%s"
        % (diag.get("error_code", ""), (" — " + diag["error_title"]) if diag.get("error_title") else ""),
        "Message    : %s" % diag.get("error_message", ""),
    ]
    if diag.get("executed_command"):
        lines.append("Executed   : %s" % diag["executed_command"])
    if diag.get("source_context"):
        lines.append("Source:")
        lines.append(diag["source_context"])
    if diag.get("fix_hint"):
        lines.append("Fix hint   : %s" % diag["fix_hint"])
    if diag.get("suggested_help_topic"):
        lines.append("Help       : call stata_help(topic=%r)" % diag["suggested_help_topic"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Anti-Loop Linter & Vectorization Advisor
# ---------------------------------------------------------------------------
def _extract_loops(statements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair each loop opener with the statements inside its body."""
    loops: List[Dict[str, Any]] = []
    for idx, st in enumerate(statements):
        opened = _detect_block_open(st["normalized"])
        if opened not in ("foreach", "forvalues", "while"):
            continue
        start_line = st["start_line"]
        body_stmts: List[Dict[str, Any]] = []
        end_line = st["end_line"]
        for inner in statements[idx + 1 :]:
            if any(b["start_line"] == start_line for b in inner["enclosing_blocks"]):
                body_stmts.append(inner)
                end_line = max(end_line, inner["end_line"])
            elif inner["block_depth"] <= st["block_depth"]:
                if inner["normalized"].startswith("}"):
                    end_line = max(end_line, inner["end_line"])
                break
        loops.append(
            {
                "kind": opened,
                "header": st["normalized"],
                "start_line": start_line,
                "end_line": end_line,
                "depth": st["block_depth"],
                "body": body_stmts,
            }
        )
    return loops


def _extract_loop_var(header: str) -> str:
    s = _strip_stmt_prefixes(header)
    m = re.match(r"^(?:foreach|forv(?:a(?:l(?:u(?:e(?:s)?)?)?)?)?)\s+([A-Za-z0-9_]+)\b", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m_while = re.match(r"^while\s+`([A-Za-z0-9_]+)'\s*(?:<=|<|>=|>|!=|==)", s, flags=re.IGNORECASE)
    if m_while:
        return m_while.group(1)
    return ""


def _strip_string_literals(text: str) -> str:
    """Replace contents of double-quoted and compound-quoted literals with spaces."""
    cleaned = text
    for _ in range(8):
        nxt = re.sub(r'`"[^"]*"\'', ' "" ', cleaned)
        if nxt == cleaned:
            break
        cleaned = nxt
    return re.sub(r'"[^"]*"', '""', cleaned)


def _unbalanced_quotes(stmt: Dict[str, Any]) -> bool:
    """Check whether a non-Mata statement leaves a standard double quote unclosed."""
    if stmt.get("in_mata"):
        return False
    s = stmt["normalized"]
    # Strip Stata compound quotes `" ... "' iteratively (innermost first)
    cleaned = s
    for _ in range(8):
        nxt = re.sub(r'`"[^"]*"\'', "", cleaned)
        if nxt == cleaned:
            break
        cleaned = nxt
    return (cleaned.count('"') % 2) == 1


def lint_do_code(code: str, file_path: str = "", strict: bool = False) -> Dict[str, Any]:
    """Analyze Stata ``.do`` source for anti-loop patterns, traps, and syntax issues."""
    text = (code or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = text.split("\n")
    statements = parse_do_statements(text)
    loops = _extract_loops(statements)

    issues: List[Dict[str, Any]] = []
    anti_loop_count = 0

    def _add_issue(
        rule_id: str,
        severity: str,
        category: str,
        line: int,
        end_line: int,
        snippet: str,
        message: str,
        suggestion: str,
        example_after: str = "",
        help_topic: str = "",
    ) -> None:
        nonlocal anti_loop_count
        if category == "anti_loop":
            anti_loop_count += 1
        issues.append(
            {
                "rule_id": rule_id,
                "severity": severity,
                "category": category,
                "line": line,
                "end_line": end_line,
                "snippet": snippet,
                "message": message,
                "suggestion": suggestion,
                "example_after": example_after,
                "help_topic": help_topic,
            }
        )

    # Track pre-loop context across the file
    levelsof_locals: Dict[str, Tuple[int, str]] = {}
    has_bare_levelsof = False
    group_id_vars: Set[str] = set()
    obs_count_locals: Set[str] = set()
    has_panel_decl = False
    saw_count_cmd = False

    for st in statements:
        norm = st["normalized"]
        verb = st["verb"]
        if verb in ("xtset", "tsset"):
            has_panel_decl = True
        if verb == "count":
            saw_count_cmd = True
        if verb == "levelsof":
            m_loc = re.search(r"\bloc(?:al)?\(\s*([A-Za-z0-9_]+)\s*\)", norm, flags=re.IGNORECASE)
            m_var = re.match(r"^(?:.*\blevelsof)\s+([A-Za-z0-9_]+)", norm, flags=re.IGNORECASE)
            if m_loc:
                levelsof_locals[m_loc.group(1)] = (st["start_line"], m_var.group(1) if m_var else "group_var")
            else:
                has_bare_levelsof = True
        if verb == "egen":
            m_grp = re.search(r"\begen\b[^=]*\b([A-Za-z0-9_]+)\s*=\s*group\s*\(", norm, flags=re.IGNORECASE)
            if m_grp:
                group_id_vars.add(m_grp.group(1))
        if verb == "local":
            m_obs = re.match(
                r"^(?:qui(?:etly)?\s+)?loc(?:al)?\s+([A-Za-z0-9_]+)\s*=\s*(?:_N|c\(N\)|r\(N\))\b",
                norm,
                flags=re.IGNORECASE,
            )
            if m_obs:
                obs_count_locals.add(m_obs.group(1))
        elif saw_count_cmd and verb not in ("summarize", "su", "sum"):
            saw_count_cmd = False

    for lp in loops:
        header = lp["header"]
        header_core = _strip_stmt_prefixes(header)
        s_line = lp["start_line"]
        e_line = lp["end_line"]
        loop_var = _extract_loop_var(header)
        body = lp["body"]
        body_text = "\n".join(b["normalized"] for b in body)
        body_verbs = [b["verb"] for b in body]

        # 1. Nested loop
        if lp["depth"] >= 1:
            is_model_only = all(
                v in ("regress", "reg", "reghdfe", "xtreg", "logit", "probit", "eststo", "local", "display", "di", "}")
                for v in body_verbs
            )
            _add_issue(
                "ANTI_LOOP_NESTED",
                "medium" if is_model_only else "high",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Nested Stata loop detected (slow O(N*M) interpretation overhead and hard to read).",
                (
                    "Extract a small helper `program` or use factor interactions `i.g1##i.g2` + `margins`."
                    if is_model_only
                    else "Replace nested loops with `bysort g1 g2:`, `collapse`, `joinby`/`merge`, or `mata`."
                ),
                "bysort group1 group2: egen mean_y = mean(y)",
                "by",
            )

        # 2. Observation-level loop (1/_N, 2/_N, local n=_N, while `i'<=_N, or `in `i'' / `[`i']`)
        obs_bounds = [r"_N", r"`=_N'", r"`N'", r"r\(N\)", r"c\(N\)"] + [r"`" + re.escape(x) + r"'" for x in obs_count_locals]
        obs_bound_pat = "(?:" + "|".join(obs_bounds) + ")"
        is_obs_header = bool(
            re.search(r"(?:1|2)\s*/\s*" + obs_bound_pat, header_core, flags=re.IGNORECASE)
            or re.search(r"^while\b.*(?:<=|<)\s*" + obs_bound_pat, header_core, flags=re.IGNORECASE)
        )
        uses_obs_idx = bool(
            loop_var
            and re.search(
                r"(?:\bin\s+`"
                + re.escape(loop_var)
                + r"'|\[`"
                + re.escape(loop_var)
                + r"'(?:\s*[+-]\s*\d+)?\]|\b_n\s*==\s*`"
                + re.escape(loop_var)
                + r"')",
                body_text,
            )
        )
        is_carryforward_obs_loop = bool(
            (is_obs_header or uses_obs_idx)
            and re.search(r"\bmissing\s*\(", body_text, flags=re.IGNORECASE)
            and re.search(r"\[(?:`" + re.escape(loop_var or "i") + r"'|_n)\s*-\s*1\]", body_text)
        )
        if is_carryforward_obs_loop:
            _add_issue(
                "ANTI_LOOP_CARRYFORWARD",
                "high",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Row loop used for forward-fill / carry-forward imputation.",
                "Stata evaluates `replace` sequentially from obs 1 to _N; a single `bysort id (t): replace x = x[_n-1] if missing(x)` cascades across consecutive missing rows in C.",
                "bysort id (year): replace x = x[_n-1] if missing(x)",
                "by",
            )
        elif is_obs_header or uses_obs_idx:
            _add_issue(
                "ANTI_LOOP_OBS_INDEX",
                "high",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Observation-by-observation loop detected; Stata commands are vectorized across all rows.",
                "Operate on whole columns directly using `generate`/`replace`, `bysort`, `_n`/`_N`, or `egen`.",
                "bysort id (year): gen double growth = (y - y[_n-1]) / y[_n-1]",
                "by",
            )

        # 3. levelsof / egen group + loop over groups
        m_loc_loop = re.search(r"\bof\s+loc(?:al)?\s+([A-Za-z0-9_]+)\s*\{", header_core, flags=re.IGNORECASE)
        uses_r_levels = bool(re.search(r"\bin\s+`r\(levels\)'", header_core, flags=re.IGNORECASE))
        is_r_max_group_loop = bool(
            group_id_vars
            and re.search(r"^forv(?:a(?:l(?:u(?:e(?:s)?)?)?)?)?\s+\w+\s*=\s*1\s*/\s*(?:`r\(max\)'|`=r\(max\)')", header_core, flags=re.I)
            and any(re.search(r"\b" + re.escape(gv) + r"\s*==\s*`" + re.escape(loop_var or "") + r"'", body_text) for gv in group_id_vars)
        )
        uses_if_filter = bool(loop_var and re.search(r"\bif\b.*==\s*`?" + re.escape(loop_var) + r"'?", body_text))
        has_group_work = any(
            v in ("summarize", "su", "sum", "egen", "replace", "gen", "generate", "count", "regress", "reg", "reghdfe", "xtreg")
            for v in body_verbs
        )
        is_levelsof_pattern = (
            (m_loc_loop and (m_loc_loop.group(1) in levelsof_locals or (uses_if_filter and has_group_work)))
            or (uses_r_levels and (has_bare_levelsof or has_group_work))
            or is_r_max_group_loop
        )
        if is_levelsof_pattern:
            has_reg = any(v in ("regress", "reg", "reghdfe", "xtreg") for v in body_verbs)
            if "post" in body_verbs:
                _add_issue(
                    "ANTI_LOOP_POSTFILE_SUMMARY",
                    "high",
                    "anti_loop",
                    s_line,
                    e_line,
                    header,
                    "`postfile` + group loop used to tabulate summary statistics.",
                    "Replace `levelsof` + `summarize` + `post` with a single `collapse` (or `table`/`collect`).",
                    "collapse (mean) mean_y=y (sd) sd_y=y (count) n=y, by(group)",
                    "collapse",
                )
            elif has_reg:
                _add_issue(
                    "ANTI_LOOP_LEVELSOF",
                    "high",
                    "anti_loop",
                    s_line,
                    e_line,
                    header,
                    "Group-by-group regression loop (`levelsof`/`egen group` + `foreach`) instead of `statsby` / `rangestat`.",
                    "Use `statsby _b _se, by(group) clear: regress y x` or `rangestat (reg) y x, interval(...) by(group)`.",
                    "statsby _b _se n=e(N), by(group) clear: regress y x",
                    "statsby",
                )
            else:
                _add_issue(
                    "ANTI_LOOP_LEVELSOF",
                    "high",
                    "anti_loop",
                    s_line,
                    e_line,
                    header,
                    "`levelsof` / group-ID loop iterates group-by-group instead of letting Stata vectorize by group.",
                    "Use `bysort group: egen ...`, `collapse`, or `statsby`.",
                    "bysort group: egen double group_mean = mean(x)\n* or aggregate to group level:\ncollapse (mean) group_mean=x, by(group)",
                    "egen",
                )

        # 4. preserve / restore inside a loop
        if any(v in ("preserve", "restore") for v in body_verbs):
            _add_issue(
                "ANTI_LOOP_PRESERVE_RESTORE",
                "high",
                "anti_loop",
                s_line,
                e_line,
                header,
                "`preserve`/`restore` inside a loop repeatedly copies the entire dataset to/from disk.",
                "Filter with `if` qualifiers, aggregate with `collapse`/`bysort`, or use isolated `frame`s (Stata 16+).",
                "frame put group y, into(work_copy)\nframe work_copy: collapse (mean) y_mean=y, by(group)\nfrlink m:1 group, frame(work_copy)\nfrget y_mean, from(work_copy)",
                "frames",
            )

        # 5. Manual dummy creation inside loop
        has_manual_dummy_gen = any(
            re.search(r"^(?:qui(?:etly)?\s+)?(?:gen|generate)\b.*==\s*`?" + re.escape(loop_var or "") + r"'?", b["normalized"], flags=re.I)
            for b in body
        )
        has_manual_dummy_two_step = bool(
            loop_var
            and any(re.search(r"^(?:qui(?:etly)?\s+)?(?:gen|generate)\b.*\b[A-Za-z0-9_]+_`" + re.escape(loop_var) + r"'\s*=\s*0\b", b["normalized"], flags=re.I) for b in body)
            and any(re.search(r"^(?:qui(?:etly)?\s+)?replace\b.*\b[A-Za-z0-9_]+_`" + re.escape(loop_var) + r"'\s*=\s*1\b", b["normalized"], flags=re.I) for b in body)
        )
        if has_manual_dummy_gen or has_manual_dummy_two_step:
            _add_issue(
                "ANTI_LOOP_MANUAL_DUMMIES",
                "high",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Manually generating dummy variables in a loop.",
                "Use native factor-variable notation (`i.var`, `c.x##i.group`) in regressions, or `tabulate var, gen(d_)`.",
                "reghdfe y x i.industry##i.year, absorb(firm_id)\n* or: tabulate industry, gen(ind_)",
                "fvvarlist",
            )

        # 6. Wide year-suffix manipulation loop (`forv y = 2005/2020` operating on `var_`y''`)
        is_year_loop = bool(re.match(r"^forv(?:a(?:l(?:u(?:e(?:s)?)?)?)?)?\s+\w+\s*=\s*(?:19|20)\d{2}\s*/\s*(?:19|20)\d{2}", header_core, flags=re.I))
        uses_wide_year_var = bool(
            is_year_loop
            and loop_var
            and re.search(r"[A-Za-z_][A-Za-z0-9_]*`" + re.escape(loop_var) + r"'", body_text)
            and any(v in ("gen", "generate", "replace", "egen") for v in body_verbs)
        )
        if uses_wide_year_var:
            _add_issue(
                "ANTI_LOOP_WIDE_YEAR",
                "medium",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Looping across wide year-suffixed columns (e.g. `sales_`y'') harms readability and duplicates lag logic.",
                "Convert wide year columns once with `reshape long`, declare `xtset id year`, and use `L.` / `D.` operators.",
                "reshape long sales_, i(id) j(year)\nxtset id year\ngen double growth = (sales_ - L.sales_) / L.sales_",
                "reshape",
            )

        # 7. Rolling / lag / time-slice loop (do not flag clean outcome varlist loops that use `bysort`)
        has_lag_idx = bool(re.search(r"\[_n\s*[+-]\s*\d+\]|\[`[A-Za-z0-9_]+'\s*[+-]\s*\d+\]", body_text))
        all_bysort_body = bool(body) and all(re.match(r"^(?:by|bys|bysort)\b", b["normalized"], flags=re.I) for b in body if b["verb"] not in ("}", ""))
        if ((has_lag_idx and not is_obs_header and not all_bysort_body) or (is_year_loop and not uses_wide_year_var and re.search(r"\bif\b", body_text))):
            _add_issue(
                "ANTI_LOOP_ROLLING_LAG",
                "high",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Time/lag/rolling computation written as an explicit loop.",
                "Declare panel/time structure with `xtset id t` or `tsset t` and use `L.`/`F.`/`D.` operators, `bysort id (t):`, or `rangestat`.",
                "xtset firm_id year\ngen double lag_y = L.y\ngen double d_y = D.y",
                "tsvarlist",
            )

        # 8. Missing-value uniform recode across varlist (`mvencode` / `mvdecode`)
        is_varlist_header = bool(
            re.search(r"\bof\s+var(?:list)?\b", header_core, flags=re.I)
            or (re.search(r"^foreach\s+\w+\s+in\b", header_core, flags=re.I) and "replace" in body_verbs)
        )
        if is_varlist_header and loop_var and all(v in ("replace", "}") for v in body_verbs):
            pat_mv_encode = re.search(
                r"\breplace\s+`" + re.escape(loop_var) + r"'\s*=\s*-?\d+(?:\.\d+)?\s+if\s+(?:missing|mi)\s*\(\s*`" + re.escape(loop_var) + r"'\s*\)",
                body_text,
                flags=re.I,
            )
            pat_mv_decode = re.search(
                r"\breplace\s+`" + re.escape(loop_var) + r"'\s*=\s*\.\s+if\b.*(?:`" + re.escape(loop_var) + r"'|inlist)",
                body_text,
                flags=re.I,
            )
            if pat_mv_encode or pat_mv_decode:
                _add_issue(
                    "ANTI_LOOP_MV_RECODE",
                    "medium",
                    "anti_loop",
                    s_line,
                    e_line,
                    header,
                    "Missing-value encoding/decoding across variables written as a `foreach` loop.",
                    "Use Stata's built-in `mvencode varlist, mv(#)` (missing -> code) or `mvdecode varlist, mv(#)` (code -> missing) in one readable line.",
                    "mvencode q1-q10, mv(0)\n* or convert sentinel codes to missing:\nmvdecode q1-q10, mv(-99 -999)",
                    "mvencode",
                )

        # 9. Row accumulation loop (`replace total = total + \`v'`, row max/min, row count)
        if is_varlist_header and loop_var:
            is_row_sum = bool(re.search(r"\breplace\s+([A-Za-z0-9_]+)\s*=\s*\1\s*[+-]\s*(?:cond\([^)]*`" + re.escape(loop_var) + r"'|`?" + re.escape(loop_var) + r"'?)", body_text, flags=re.I))
            is_row_ext = bool(re.search(r"\breplace\s+([A-Za-z0-9_]+)\s*=\s*(?:max|min)\s*\(\s*\1\s*,\s*`" + re.escape(loop_var) + r"'", body_text, flags=re.I))
            is_row_cnt = bool(re.search(r"\breplace\s+([A-Za-z0-9_]+)\s*=\s*\1\s*\+\s*!?mi(?:ssing)?\s*\(\s*`" + re.escape(loop_var) + r"'", body_text, flags=re.I))
            if is_row_sum or is_row_ext or is_row_cnt:
                _add_issue(
                    "ANTI_LOOP_ROW_ACCUM",
                    "medium",
                    "anti_loop",
                    s_line,
                    e_line,
                    header,
                    "Row-wise aggregation across variables written as a `foreach` loop.",
                    "Use `egen rowtotal(...)`, `rowmean(...)`, `rowmax(...)`, `rowmin(...)`, `rownonmiss(...)`, or `rowmiss(...)`.",
                    "egen double total_score = rowtotal(var1-var10)\negen double max_score = rowmax(var1-var10)\negen int n_valid = rownonmiss(var1-var10)",
                    "egen",
                )

        # 10. Bulk rename loop (ignore `local` / `capture` helper statements inside the loop body)
        rename_core_verbs = [v for v in body_verbs if v not in ("local", "loc", "}", "")]
        if rename_core_verbs and any(v in ("rename", "ren") for v in rename_core_verbs) and all(v in ("rename", "ren") for v in rename_core_verbs):
            _add_issue(
                "ANTI_LOOP_BULK_RENAME",
                "medium",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Variable renaming done inside a loop.",
                "Use Stata's native group `rename` syntax (`rename old_* new_*` or `rename *, lower`).",
                "rename old_* new_*",
                "rename_group",
            )

        # 11. Repetitive value-by-value recode loop
        if (
            body_verbs
            and all(v in ("replace", "}") for v in body_verbs)
            and loop_var
            and re.search(r"==\s*`?" + re.escape(loop_var) + r"'?", body_text)
            and not any(i["line"] == s_line and i["rule_id"] == "ANTI_LOOP_MV_RECODE" for i in issues)
        ):
            _add_issue(
                "ANTI_LOOP_RECODE",
                "medium",
                "anti_loop",
                s_line,
                e_line,
                header,
                "Repetitive conditional `replace` inside a loop.",
                "Use `recode`, `inlist()`, `inrange()`, or `cond()` in a single vectorized statement.",
                "gen byte flag = inlist(code, 101, 102, 105)\nrecode score (0/59=0) (60/100=1), gen(pass)",
                "recode",
            )

        # Smart tolerance note for clean small regression-spec loops (only under strict=True)
        if strict and lp["depth"] == 0 and not any(i["line"] == s_line for i in issues):
            if any(v in ("regress", "reg", "reghdfe", "xtreg", "logit", "probit", "eststo") for v in body_verbs):
                _add_issue(
                    "INFO_MODEL_SPEC_LOOP",
                    "info",
                    "best_practice",
                    s_line,
                    e_line,
                    header,
                    "Small specification/outcome loop detected (acceptable when iterating <= 6 regression specs).",
                    "Keep specification loops short and free of `preserve`/`levelsof`.",
                    "",
                    "eststo",
                )

    # Statement-level traps & readability checks
    for idx, st in enumerate(statements):
        norm = st["normalized"]
        norm_no_str = _strip_string_literals(norm)
        verb = st["verb"]
        s_line = st["start_line"]
        e_line = st["end_line"]

        # Missing value `>` trap (`if x > 0` or `if x > y` matches `.` because `.` > all numbers in Stata!)
        if re.search(
            r"(?:\bif\b|\bcond\s*\().*?>=?\s*(?:-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*|`[A-Za-z0-9_]+')",
            norm_no_str,
            flags=re.IGNORECASE,
        ):
            if not re.search(r"(?:!missing\(|!mi\(|<\s*\.|!=\s*\.)", norm_no_str, flags=re.IGNORECASE):
                _add_issue(
                    "PITFALL_MISSING_GT",
                    "warning",
                    "missing_trap",
                    s_line,
                    e_line,
                    norm,
                    "Stata missing-value trap: `.` (and `.a`..`.z`) is treated as +infinity, so `x > ...` evaluates TRUE when `x` is missing!",
                    "Guard `>` / `>=` comparisons with `& !missing(x)` or `& x < .`.",
                    "keep if price > 5000 & !missing(price)",
                    "missing",
                )

        # Unsorted [_n-1] / [_n+1] subscript without `by` prefix or `xtset`/`tsset`
        if (
            not has_panel_decl
            and re.search(r"\[_n\s*[+-]\s*\d+\]", norm_no_str)
            and not re.match(r"^(?:by|bys|bysort)\b", norm_no_str, flags=re.IGNORECASE)
            and not any(b["kind"] in ("foreach", "forvalues", "while") for b in st["enclosing_blocks"])
        ):
            _add_issue(
                "PITFALL_UNSORTED_SUBSCRIPT",
                "warning",
                "best_practice",
                s_line,
                e_line,
                norm,
                "Row subscript `[_n-1]` / `[_n+1]` used without `bysort id (t):` or `xtset`/`tsset`.",
                "Wrap with `bysort id (time):` or declare `xtset id time` and use `L.` / `F.` so observations never bleed across panel IDs or time gaps.",
                "xtset id year\ngen double lag_y = L.y",
                "by",
            )

        # Deprecated xi: prefix
        if re.match(r"^(?:(?:qui|cap|noi)\w*\s+)*xi\s*:", norm, flags=re.IGNORECASE):
            _add_issue(
                "PITFALL_DEPRECATED_XI",
                "warning",
                "best_practice",
                s_line,
                e_line,
                norm,
                "Legacy `xi:` prefix creates unnecessary temporary `_I*` variables.",
                "Drop `xi:` and use native factor variables (`i.var`, `c.x##i.group`).",
                "regress y c.x##i.industry",
                "fvvarlist",
            )

        # egen sum() ambiguity vs total()
        if strict and verb == "egen" and re.search(r"=\s*sum\s*\(", norm_no_str, flags=re.IGNORECASE):
            _add_issue(
                "PITFALL_GEN_SUM_VS_EGEN",
                "info",
                "best_practice",
                s_line,
                e_line,
                norm,
                "`egen ... = sum()` is a deprecated alias for `egen ... = total()` (and easily confused with `gen sum()` cumulative sum).",
                "Prefer `egen ... = total()` for group totals and `gen ... = sum()` for running cumulative sums.",
                "bysort group: egen double group_total = total(x)",
                "egen",
            )

        # Unclosed double quote on statement
        if _unbalanced_quotes(st):
            _add_issue(
                "SYNTAX_UNBALANCED_QUOTES",
                "error",
                "syntax",
                s_line,
                e_line,
                norm,
                "Unbalanced double quote (`\"`) in logical statement.",
                "Close the double quote or use Stata compound quotes `` `\"...\"' `` when nesting quotes.",
                "",
                "quotes",
            )

        # Merge without assert / nogenerate
        if verb == "merge" and not re.search(r"\b(?:assert|nogen|nogenerate)\b", norm, flags=re.IGNORECASE):
            nearby = " ".join(x["normalized"] for x in statements[idx + 1 : idx + 6])
            if strict or "_merge" not in nearby:
                _add_issue(
                    "PITFALL_MERGE_CHECK",
                    "info",
                    "best_practice",
                    s_line,
                    e_line,
                    norm,
                    "`merge` without `assert(...)` or explicit `_merge` check.",
                    "Add `, assert(match master) nogenerate` or inspect `tab _merge` immediately after merging.",
                    "merge m:1 id using other.dta, assert(match master) nogenerate",
                    "merge",
                )

    # Whole-file brace balance check
    brace_stack: List[int] = []
    in_bc = 0
    for ln_no, raw in enumerate(raw_lines, start=1):
        if in_bc == 0 and raw.strip().startswith("*"):
            continue
        code_part, in_bc, _ = _strip_inline_comment(raw, in_bc)
        in_dq = False
        i = 0
        while i < len(code_part):
            ch = code_part[i]
            if ch == '"':
                in_dq = not in_dq
            elif not in_dq:
                # Ignore macro braces `${...}`
                if ch == "$" and i + 1 < len(code_part) and code_part[i + 1] == "{":
                    close_b = code_part.find("}", i + 2)
                    if close_b != -1:
                        i = close_b + 1
                        continue
                if ch == "{":
                    brace_stack.append(ln_no)
                elif ch == "}":
                    if brace_stack:
                        brace_stack.pop()
                    else:
                        _add_issue(
                            "SYNTAX_UNBALANCED_BRACES",
                            "error",
                            "syntax",
                            ln_no,
                            ln_no,
                            raw.strip(),
                            "Unexpected closing brace `}` with no matching `{`.",
                            "Remove the extra `}` or restore the missing opening `{`.",
                        )
            i += 1
    for unclosed_ln in brace_stack:
        _add_issue(
            "SYNTAX_UNBALANCED_BRACES",
            "error",
            "syntax",
            unclosed_ln,
            unclosed_ln,
            raw_lines[unclosed_ln - 1].strip() if unclosed_ln <= len(raw_lines) else "{",
            "Opening brace `{` is never closed.",
            "Add the matching closing brace `}`.",
        )

    # Idiomatic score (0-100)
    penalty_map = {"error": 25, "high": 20, "medium": 10, "warning": 8, "info": 2}
    penalty = sum(penalty_map.get(item["severity"], 5) for item in issues)
    score = max(0, 100 - penalty)

    summary = (
        "Clean idiomatic Stata code (%d/100, %d loop(s), 0 anti-loops)." % (score, len(loops))
        if not issues
        else "Score %d/100: %d issue(s) found (%d anti-loop pattern(s))."
        % (score, len(issues), anti_loop_count)
    )

    return {
        "file_path": file_path,
        "total_lines": len(raw_lines),
        "statement_count": len(statements),
        "loop_count": len(loops),
        "anti_loop_count": anti_loop_count,
        "idiomatic_score": score,
        "summary": summary,
        "issues": issues,
    }


VECTORIZE_RECIPES: List[Dict[str, Any]] = [
    {
        "id": "obs_loop",
        "title": "Observation Loop (`forvalues i = 1/_N`) -> Vectorized Column / `bysort`",
        "keywords": ["obs", "1/_n", "_n", "row", "forvalues", "逐行", "观测值", "循环"],
        "rule_ids": ["ANTI_LOOP_OBS_INDEX"],
        "when_to_use": "Never loop over 1/_N to assign values, compute ratios, or inspect neighbors.",
        "bad_loop": "forvalues i = 1/`=_N' {\n    replace z = x[`i'] / y[`i'] in `i'\n}",
        "idiomatic_stata": "gen double z = x / y",
        "why_better": "C-level vector execution instead of interpreted macro substitution per row (verified ~11x faster at N=20k in StataNow 19.5 MP, >100x on larger tables).",
        "gotchas": "Missing values propagate cleanly in vectorized expressions; guard divisions with `if y != 0 & !missing(y)`.",
        "verified_note": "StataNow 19.5 MP verified: exact match (< 1e-12); 0.044s loop vs 0.004s vectorized at N=20,000.",
        "help_topics": ["generate", "by", "egen"],
    },
    {
        "id": "group_agg",
        "title": "Group Loop (`levelsof` + `foreach`) -> `bysort ... : egen` / `collapse`",
        "keywords": ["group", "levelsof", "bysort", "egen", "collapse", "分组", "均值", "聚合"],
        "rule_ids": ["ANTI_LOOP_LEVELSOF"],
        "when_to_use": "Computing group means, sums, counts, ranks, or percentiles across categories.",
        "bad_loop": "levelsof industry, local(inds)\nforeach g of local inds {\n    quietly summarize sales if industry == `g'\n    replace ind_mean = r(mean) if industry == `g'\n}",
        "idiomatic_stata": "bysort industry: egen double ind_mean = mean(sales)\n* Leave-one-out mean:\nbysort industry: egen double _tot = total(sales)\nbysort industry: egen long _cnt = count(sales)\ngen double loo_mean = (_tot - sales) / (_cnt - 1) if _cnt > 1",
        "why_better": "One C-level sort + single pass replaces K full dataset scans (verified ~170x faster at N=20k, 200 groups in StataNow 19.5 MP).",
        "gotchas": "`egen total()` treats missing as 0; check `!missing(sales)` when needed.",
        "verified_note": "StataNow 19.5 MP verified: exact match (< 1e-10); 0.341s loop vs 0.002s bysort:egen at N=20,000 / 200 groups.",
        "help_topics": ["by", "egen", "collapse"],
    },
    {
        "id": "lag_lead_diff",
        "title": "Panel Lag/Lead/Diff Loop -> `xtset` + `L.` / `F.` / `D.` Operators",
        "keywords": ["lag", "lead", "diff", "panel", "xtset", "tsset", "滞后", "差分", "面板"],
        "rule_ids": ["ANTI_LOOP_ROLLING_LAG"],
        "when_to_use": "Computing lags, leads, growth rates, or differences within panel IDs over time.",
        "bad_loop": "forvalues i = 2/`=_N' {\n    if id[`i'] == id[`i'-1] replace dy = y[`i'] - y[`i'-1] in `i'\n}",
        "idiomatic_stata": "xtset id year\ngen double dy = D.y\ngen double lag1_y = L.y\ngen double growth = (y - L.y) / L.y",
        "why_better": "`L.` and `D.` respect actual time gaps (missing years won't silently lag across 2-year gaps unlike `[_n-1]`).",
        "gotchas": "Run `duplicates report id year` before `xtset` to avoid `r(451)`.",
        "verified_note": "StataNow 19.5 MP verified: `D.y` correctly returns missing across 2-year panel gaps where `[_n-1]` leaks.",
        "help_topics": ["xtset", "tsvarlist"],
    },
    {
        "id": "rolling_window",
        "title": "Rolling Window Loop -> `rangestat` / `tssmooth` / `bysort id (t):`",
        "keywords": ["rolling", "window", "moving", "rangestat", "asrol", "滚动", "窗口"],
        "rule_ids": ["ANTI_LOOP_ROLLING_LAG"],
        "when_to_use": "Computing rolling means, SDs, or rolling regressions over a trailing time window.",
        "bad_loop": "forvalues t = 2005/2020 {\n    summarize ret if inrange(year, `t'-4, `t')\n    ...\n}",
        "idiomatic_stata": "* Balanced panel 3-period rolling mean via cumulative sum():\nbysort id (year): gen double run_sum = sum(x)\nbysort id (year): gen double ma3 = (run_sum - cond(_n > 3, run_sum[_n-3], 0)) / 3 if _n >= 3\n* Arbitrary windows / regressions (SSC):\n* rangestat (mean) ret (sd) ret, interval(year -4 0) by(id)",
        "why_better": "Avoids O(T * N) subsetting loops and cleanly handles unbalanced panels.",
        "gotchas": "Guard `run_sum[_n-3]` with `cond(_n > 3, run_sum[_n-3], 0)` so row 3 does not evaluate `run_sum[0]` (missing). Also remember `sum(x)` is cumulative while `egen total()` is group-constant.",
        "verified_note": "StataNow 19.5 MP verified: cumulative `sum()` + `cond(_n > 3, ...)` matches explicit window loop (< 1e-12).",
        "help_topics": ["sum", "rangestat", "rolling"],
    },
    {
        "id": "dummies_interactions",
        "title": "Manual Dummy Loop -> Native Factor Variables (`i.var`, `c.x##i.g`)",
        "keywords": ["dummy", "dummies", "factor", "interaction", "xi", "虚拟变量", "交互项"],
        "rule_ids": ["ANTI_LOOP_MANUAL_DUMMIES", "PITFALL_DEPRECATED_XI"],
        "when_to_use": "Including fixed effects, category dummies, or interactions in estimation commands.",
        "bad_loop": "levelsof industry, local(inds)\nforeach g of local inds {\n    gen d_`g' = (industry == `g')\n}\nregress y x d_*",
        "idiomatic_stata": "regress y x i.industry##i.year\n* High-dimensional FE:\nreghdfe y x, absorb(firm_id industry#year) vce(cluster firm_id)\n* Only when explicit dummy columns are required:\ntabulate industry, gen(ind_)",
        "why_better": "Keeps dataset slim, enables `margins` to understand interactions properly, and automatically handles collinearity.",
        "gotchas": "Factor variables require non-negative integer categories; use `egen g = group(str_var)` for strings.",
        "help_topics": ["fvvarlist", "margins", "tabulate"],
    },
    {
        "id": "row_stats",
        "title": "Varlist Accumulation Loop -> `egen rowtotal` / `rowmean` / `rowmiss`",
        "keywords": ["rowtotal", "rowmean", "varlist", "sum", "行求和", "行均值", "缺失"],
        "rule_ids": ["ANTI_LOOP_ROW_ACCUM"],
        "when_to_use": "Aggregating across columns on each row.",
        "bad_loop": "gen total = 0\nforeach v of varlist q1-q10 {\n    replace total = total + `v' if !missing(`v')\n}",
        "idiomatic_stata": "egen double total = rowtotal(q1-q10)\negen double avg = rowmean(q1-q10)\negen int n_miss = rowmiss(q1-q10)",
        "why_better": "Single-line declarative expression that handles missing values explicitly and predictably.",
        "gotchas": "`rowtotal()` returns 0 if all inputs are missing unless `, missing` is specified.",
        "help_topics": ["egen"],
    },
    {
        "id": "group_regression",
        "title": "Subgroup Regression Loop -> `statsby` / `rangestat` / `reghdfe`",
        "keywords": ["statsby", "regression", "subgroup", "by", "分组回归"],
        "rule_ids": ["ANTI_LOOP_LEVELSOF", "ANTI_LOOP_PRESERVE_RESTORE"],
        "when_to_use": "Running the same regression separately for many groups (e.g. firm-year betas) and collecting coefficients.",
        "bad_loop": "levelsof id, local(ids)\nforeach k of local ids {\n    preserve\n    keep if id == `k'\n    capture regress y x\n    ...\n    restore\n}",
        "idiomatic_stata": "statsby _b _se n=e(N), by(id) clear: regress y x\n* Or keep original data intact via rangestat:\n* rangestat (reg) y x, interval(year -4 0) by(id)",
        "why_better": "Eliminates repeated `preserve`/`restore` disk I/O and automatically collects clean coefficient tables.",
        "gotchas": "`statsby ..., clear` replaces the dataset in memory; use `saving(betas.dta, replace)` to preserve the main dataset.",
        "help_topics": ["statsby", "postfile"],
    },
    {
        "id": "recode_inlist",
        "title": "Value Mapping Loop -> `recode` / `inlist()` / `inrange()` / `cond()`",
        "keywords": ["recode", "inlist", "inrange", "cond", "mapping", "映射", "重编码"],
        "rule_ids": ["ANTI_LOOP_RECODE"],
        "when_to_use": "Mapping or bucketing many discrete values or ranges into categories.",
        "bad_loop": "gen byte treated = 0\nforeach c in 11 14 23 45 67 {\n    replace treated = 1 if prov == `c'\n}",
        "idiomatic_stata": "gen byte treated = inlist(prov, 11, 14, 23, 45, 67)\nrecode age (0/17=1) (18/64=2) (65/max=3), gen(age_grp)",
        "why_better": "Cleaner, self-documenting, and executes in a single pass.",
        "gotchas": "`inlist()` supports up to 255 numeric arguments (or 10 string arguments); for large lookup tables use `merge m:1`.",
        "help_topics": ["recode", "inlist", "cond"],
    },
    {
        "id": "bulk_rename",
        "title": "Rename Loop -> Group `rename` Wildcards",
        "keywords": ["rename", "prefix", "suffix", "lower", "重命名", "批量改名"],
        "rule_ids": ["ANTI_LOOP_BULK_RENAME"],
        "when_to_use": "Adding/removing prefixes or suffixes or changing case across many variables.",
        "bad_loop": "foreach v of varlist v_* {\n    local new = subinstr(\"`v'\", \"v_\", \"col_\", 1)\n    rename `v' `new'\n}",
        "idiomatic_stata": "rename v_* col_*\nrename *, lower\nrename (x1 x2 x3) (y1 y2 y3)",
        "why_better": "Atomic, reversible, and 1 line instead of a macro-manipulation loop.",
        "gotchas": "See `stata_help(topic='rename_group')` for full pattern rules.",
        "help_topics": ["rename_group"],
    },
    {
        "id": "carryforward_fill",
        "title": "Forward-Fill Loop (`forvalues i = 2/_N`) -> Sequential `bysort id (t): replace`",
        "keywords": ["carryforward", "locf", "missing", "fill", "forward", "前向填充", "缺失填充"],
        "rule_ids": ["ANTI_LOOP_CARRYFORWARD"],
        "when_to_use": "Carrying forward the last non-missing observation within panel IDs over consecutive missing rows.",
        "bad_loop": "bysort id (year): gen obs = _n\nforvalues i = 2/`=_N' {\n    if id[`i'] == id[`i'-1] & missing(x[`i']) {\n        qui replace x = x[`i'-1] in `i'\n    }\n}",
        "idiomatic_stata": "bysort id (year): replace x = x[_n-1] if missing(x)",
        "why_better": "Stata evaluates `replace` sequentially from row 1 to _N, so consecutive missing rows cascade in a single C pass.",
        "gotchas": "Ensure data is sorted by `id (year)` first so values never carry across panel boundaries.",
        "help_topics": ["by", "missing"],
    },
    {
        "id": "mv_encode_decode",
        "title": "Missing Value Loop (`foreach v of varlist`) -> `mvencode` / `mvdecode`",
        "keywords": ["mvencode", "mvdecode", "missing", "sentinel", "-99", "缺失值编码", "缺失替换"],
        "rule_ids": ["ANTI_LOOP_MV_RECODE"],
        "when_to_use": "Replacing missing `.` with numeric codes (e.g. 0) or converting sentinel codes (-99, -999) to `.` across many variables.",
        "bad_loop": "foreach v of varlist q1-q10 {\n    replace `v' = 0 if missing(`v')\n}",
        "idiomatic_stata": "mvencode q1-q10, mv(0)\n* Or convert sentinel values to Stata missing:\n* mvdecode q1-q10, mv(-99 -999)",
        "why_better": "Single built-in command across an arbitrary varlist with automatic protection against accidental collisions (unless `override` is passed).",
        "gotchas": "`mvencode` refuses (`r(9)`) if the replacement value already exists in any target variable unless `, override` is specified.",
        "verified_note": "StataNow 19.5 MP verified: `mvencode` + `mvdecode` round-trip matches explicit `foreach` loop.",
        "help_topics": ["mvencode"],
    },
    {
        "id": "wide_to_long_panel",
        "title": "Wide Year-Suffix Loop (`sales_2018`..`sales_2022`) -> `reshape long` + `xtset`",
        "keywords": ["reshape", "wide", "long", "year", "panel", "宽转长", "面板重塑"],
        "rule_ids": ["ANTI_LOOP_WIDE_YEAR"],
        "when_to_use": "Computing year-over-year growth, differences, or panel regressions across wide year-suffixed columns.",
        "bad_loop": "forvalues y = 2019/2022 {\n    local prev = `y' - 1\n    gen double g_`y' = (sales_`y' - sales_`prev') / sales_`prev'\n}",
        "idiomatic_stata": "reshape long sales_, i(id) j(year)\nxtset id year\ngen double g = (sales_ - L.sales_) / L.sales_",
        "why_better": "Eliminates duplicated column-name macro arithmetic, unlocks `L.`/`D.`/`xtreg`/`reghdfe`, and keeps code concise and readable.",
        "gotchas": "Ensure `id` uniquely identifies wide rows (`isid id`) before `reshape long`.",
        "verified_note": "StataNow 19.5 MP verified: exact match (< 1e-12) against wide year-suffix loop.",
        "help_topics": ["reshape", "xtset"],
    },
    {
        "id": "collapse_summary",
        "title": "`levelsof` + `summarize` + `postfile` Loop -> `collapse` / `table`",
        "keywords": ["collapse", "postfile", "post", "summary", "table", "分组统计", "汇总表"],
        "rule_ids": ["ANTI_LOOP_POSTFILE_SUMMARY", "ANTI_LOOP_LEVELSOF"],
        "when_to_use": "Building a group-level summary table (means, medians, SDs, counts, totals) without writing boilerplate `postfile` loops.",
        "bad_loop": "tempname pf\npostfile `pf' grp mean_y sd_y n_y using \"`out'\", replace\nlevelsof group, local(gs)\nforeach g of local gs {\n    qui summarize y if group == `g'\n    post `pf' (`g') (r(mean)) (r(sd)) (r(N))\n}\npostclose `pf'",
        "idiomatic_stata": "collapse (mean) mean_y=y (sd) sd_y=y (count) n_y=y, by(group)",
        "why_better": "1 line replaces 8 lines of tempname/postfile/levelsof boilerplate and runs in a single C-level sort pass.",
        "gotchas": "`collapse (mean)` on `int`/`float` inputs defaults to `float` precision (~1e-4 on large values); run `recast double y` first if exact `double` precision is needed, and wrap in `frame put` to keep original data intact.",
        "verified_note": "StataNow 19.5 MP verified: exact match (< 1e-10) after `recast double`.",
        "help_topics": ["collapse", "frames"],
    },
    {
        "id": "frames_not_preserve",
        "title": "`preserve`/`restore` in Loops -> Stata `frames` (`frame put` + `frlink`)",
        "keywords": ["preserve", "restore", "frame", "frames", "frlink", "frget", "数据框", "临时数据"],
        "rule_ids": ["ANTI_LOOP_PRESERVE_RESTORE"],
        "when_to_use": "Computing a derived summary or secondary lookup dataset without mutating or disk-copying the main dataset.",
        "bad_loop": "levelsof group, local(gs)\nforeach g of local gs {\n    preserve\n    keep if group == `g'\n    collapse (mean) y, by(group)\n    ...\n    restore\n}",
        "idiomatic_stata": "frame put group y, into(scratch)\nframe scratch: collapse (mean) y_mean=y, by(group)\nfrlink m:1 group, frame(scratch)\nfrget y_mean, from(scratch)\nframe drop scratch",
        "why_better": "Frames live in memory, support multiple datasets simultaneously, and never leak `r(621) already preserved`.",
        "gotchas": "Requires Stata 16+. Drop temporary frames with `capture frame drop scratch`, and `recast double y` if `y` is integer and double precision is needed after `collapse`.",
        "verified_note": "StataNow 19.5 MP verified: exact match (< 1e-10) against `bysort: egen double`.",
        "help_topics": ["frames", "frlink"],
    },
    {
        "id": "mata_matrix",
        "title": "Nested Matrix/Simulation Loop -> `mata` Vectorized Operations",
        "keywords": ["mata", "matrix", "nested", "simulation", "矩阵", "嵌套循环"],
        "rule_ids": ["ANTI_LOOP_NESTED"],
        "when_to_use": "Complex linear algebra, custom estimators, or unavoidable matrix computations.",
        "bad_loop": "forvalues i = 1/`n' {\n    forvalues j = 1/`m' {\n        ...\n    }\n}",
        "idiomatic_stata": "mata:\n    X = st_data(., (\"x1\", \"x2\"))\n    y = st_data(., \"y\")\n    b = invsym(cross(X, X)) * cross(X, y)\nend",
        "why_better": "Mata compiles to bytecode and uses LAPACK/BLAS vectorized operations.",
        "gotchas": "Use `st_view()` for zero-copy views on large datasets.",
        "help_topics": ["mata", "m2_syntax"],
    },
]


def vectorize_guide(pattern: str = "", code: str = "") -> Dict[str, Any]:
    """Return idiomatic Stata alternatives for loops (optionally analyzing ``code``)."""
    lint_result = lint_do_code(code) if code and code.strip() else None
    matched_recipes: List[Dict[str, Any]] = []

    if lint_result and lint_result["issues"]:
        triggered_rules = {issue["rule_id"] for issue in lint_result["issues"]}
        for rec in VECTORIZE_RECIPES:
            if triggered_rules & set(rec["rule_ids"]):
                matched_recipes.append(rec)

    if pattern and pattern.strip():
        q = pattern.strip().lower()
        if q == "all":
            matched_recipes = list(VECTORIZE_RECIPES)
        else:
            for rec in VECTORIZE_RECIPES:
                if rec in matched_recipes:
                    continue
                if q == rec["id"].lower() or q in rec["title"].lower() or any(q in kw.lower() for kw in rec["keywords"]):
                    matched_recipes.append(rec)

    if not matched_recipes and not (code and code.strip()):
        matched_recipes = VECTORIZE_RECIPES[:5]

    return {
        "available_patterns": [{"id": r["id"], "title": r["title"]} for r in VECTORIZE_RECIPES],
        "lint": lint_result,
        "recipes": matched_recipes,
    }


# ---------------------------------------------------------------------------
# 4. Standalone CLI (for Desktop App & shell usage)
# ---------------------------------------------------------------------------
def _emit_json(payload: Any) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="stata_mcp_analysis", description="Stata help, error diagnosis, and anti-loop analysis.")
    sub = parser.add_subparsers(dest="command")

    p_help = sub.add_parser("help")
    p_help.add_argument("topic")
    p_help.add_argument("--stata-path", default="")
    p_help.add_argument("--section", default="")
    p_help.add_argument("--max-chars", type=int, default=16000)
    p_help.add_argument("--json", action="store_true")

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--stata-path", default="")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true")

    p_lint = sub.add_parser("lint")
    p_lint.add_argument("--file", default="")
    p_lint.add_argument("--stdin", action="store_true")
    p_lint.add_argument("--strict", action="store_true")
    p_lint.add_argument("--json", action="store_true")

    p_diag = sub.add_parser("diagnose")
    p_diag.add_argument("--job-dir", default="")
    p_diag.add_argument("--file", default="")
    p_diag.add_argument("--log-file", default="")
    p_diag.add_argument("--json", action="store_true")

    p_vec = sub.add_parser("vectorize")
    p_vec.add_argument("--pattern", default="")
    p_vec.add_argument("--file", default="")
    p_vec.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "help":
        res = resolve_help(args.topic, stata_path=args.stata_path, section=args.section, max_chars=args.max_chars)
        if args.json:
            return _emit_json(res)
        print(res.get("content") or ("Help not found for %s" % args.topic))
        return 0 if res.get("found") else 1

    if args.command == "search":
        res = search_help(args.query, stata_path=args.stata_path, limit=args.limit)
        if args.json:
            return _emit_json(res)
        for item in res["results"]:
            print("%-24s -> %-24s [%s] %s" % (item["topic"], item["target"], item["source"], item["title"]))
        return 0

    if args.command == "lint":
        if args.file:
            code = Path(args.file).read_text(encoding="utf-8", errors="replace")
        elif args.stdin:
            code = sys.stdin.read()
        else:
            code = ""
        res = lint_do_code(code, file_path=args.file, strict=args.strict)
        if args.json:
            return _emit_json(res)
        print(res["summary"])
        for iss in res["issues"]:
            print("L%d [%s] %s: %s" % (iss["line"], iss["severity"], iss["rule_id"], iss["message"]))
        return 0

    if args.command == "diagnose":
        records: List[Dict[str, Any]] = []
        file_path = args.file
        line_offset = 0
        if args.job_dir:
            jd = Path(args.job_dir)
            meta_p = jd / "meta.json"
            rec_p = jd / "records.jsonl"
            if meta_p.is_file():
                meta = json.loads(meta_p.read_text(encoding="utf-8", errors="replace"))
                file_path = file_path or meta.get("source_file") or meta.get("file_path") or ""
                line_offset = int(meta.get("line_offset") or 0)
            if rec_p.is_file():
                for line in rec_p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip():
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            pass
        src = Path(file_path).read_text(encoding="utf-8", errors="replace") if file_path and Path(file_path).is_file() else ""
        err_log = Path(args.log_file).read_text(encoding="utf-8", errors="replace") if args.log_file else ""
        diag = locate_error_in_do(src, records, file_path=file_path, line_offset=line_offset, error_log=err_log)
        if args.json:
            return _emit_json({"found": bool(diag), "diagnosis": diag})
        print(format_error_diagnosis(diag) if diag else "No Stata error detected.")
        return 0

    if args.command == "vectorize":
        code = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else ""
        res = vectorize_guide(pattern=args.pattern, code=code)
        if args.json:
            return _emit_json(res)
        for rec in res["recipes"]:
            print("## %s\n%s\n" % (rec["title"], rec["idiomatic_stata"]))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
