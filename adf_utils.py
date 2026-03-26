"""
adf_utils.py – Convert Atlassian Document Format (ADF) to plain text.

Jira Cloud REST API v3 returns 'description' as an ADF JSON object, not a
plain string.  This module walks the node tree and produces a readable plain-
text / Markdown-ish representation that survives a CSV round-trip.

Only the most common node types are handled; unknown types fall back to
recursively rendering their children (so text is never silently dropped).

Reference: https://developer.atlassian.com/cloud/jira/platform/apis/document/
"""

from __future__ import annotations

from typing import Any, Optional


def adf_to_text(node: Any, _indent: int = 0) -> str:
    """
    Recursively convert an ADF node (dict) to a plain-text string.

    Pass the top-level 'description' field value from the Jira issue directly.
    Returns "" if node is None or not an ADF document.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        # Occasionally the API returns a plain string for older issues.
        return node

    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}

    # ---- Leaf nodes --------------------------------------------------------
    if node_type == "text":
        text = node.get("text", "")
        marks = node.get("marks") or []
        mark_types = {m.get("type") for m in marks}
        if "code" in mark_types:
            text = f"`{text}`"
        # strong/em/strike are readable as-is in plain text; skip wrapping.
        return text

    if node_type == "hardBreak":
        return "\n"

    if node_type == "rule":
        return "\n---\n"

    if node_type == "mention":
        # attrs.text is the display name when available.
        return f"@{attrs.get('text') or attrs.get('id') or 'unknown'}"

    if node_type == "emoji":
        return attrs.get("shortName") or attrs.get("text") or ""

    if node_type == "inlineCard":
        return attrs.get("url") or ""

    if node_type in ("media", "mediaSingle", "mediaGroup"):
        # Can't embed binaries in CSV; leave a placeholder.
        alt = attrs.get("alt") or attrs.get("id") or "attachment"
        return f"[{alt}]"

    if node_type == "status":
        return attrs.get("text") or ""

    if node_type == "date":
        return attrs.get("timestamp") or ""

    # ---- Block nodes -------------------------------------------------------
    if node_type == "doc":
        return _join_children(content, separator="\n")

    if node_type == "paragraph":
        inner = _join_children(content)
        return inner + "\n" if inner else "\n"

    if node_type in ("heading",):
        level = attrs.get("level", 1)
        prefix = "#" * level + " "
        return prefix + _join_children(content) + "\n"

    if node_type == "blockquote":
        inner = _join_children(content, separator="\n")
        # Prefix each line with "> "
        lines = inner.splitlines()
        return "\n".join("> " + l for l in lines) + "\n"

    if node_type == "codeBlock":
        lang = attrs.get("language") or ""
        inner = _join_children(content)
        return f"```{lang}\n{inner}\n```\n"

    if node_type == "bulletList":
        parts = []
        for item in content:
            item_text = _join_children(item.get("content") or [], separator="\n").rstrip()
            # Indent continuation lines.
            lines = item_text.splitlines()
            if lines:
                parts.append("- " + lines[0])
                for l in lines[1:]:
                    parts.append("  " + l)
        return "\n".join(parts) + "\n"

    if node_type == "orderedList":
        parts = []
        for i, item in enumerate(content, start=attrs.get("order", 1)):
            item_text = _join_children(item.get("content") or [], separator="\n").rstrip()
            lines = item_text.splitlines()
            if lines:
                parts.append(f"{i}. " + lines[0])
                for l in lines[1:]:
                    parts.append("   " + l)
        return "\n".join(parts) + "\n"

    if node_type == "listItem":
        return _join_children(content, separator="\n")

    # ---- Table -------------------------------------------------------------
    if node_type == "table":
        rows = []
        for row_node in content:
            cells = []
            for cell_node in (row_node.get("content") or []):
                cell_text = _join_children(
                    cell_node.get("content") or [], separator=" "
                ).replace("\n", " ").strip()
                cells.append(cell_text)
            rows.append(" | ".join(cells))
        return "\n".join(rows) + "\n"

    if node_type in ("tableRow", "tableHeader", "tableCell"):
        return _join_children(content, separator=" ")

    # ---- Panel (info/note/warning boxes in Confluence-style content) -------
    if node_type == "panel":
        panel_type = attrs.get("panelType") or "note"
        inner = _join_children(content, separator="\n")
        return f"[{panel_type.upper()}]\n{inner}\n"

    # ---- Expand / details --------------------------------------------------
    if node_type == "expand":
        title = attrs.get("title") or ""
        inner = _join_children(content, separator="\n")
        return f"[{title}]\n{inner}\n"

    # ---- Fallback: render children recursively so no text is lost ----------
    return _join_children(content, separator="\n")


def _join_children(children: list, separator: str = "") -> str:
    return separator.join(adf_to_text(child) for child in children)
