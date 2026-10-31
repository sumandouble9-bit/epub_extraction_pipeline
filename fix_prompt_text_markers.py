import re

_MARKER_PAT = re.compile(r"([_^])\(([^)]+)\)|([_^])([^\s()\[\]{}_^])")


def _has_script_runs(item):
    """Check if item has any subscript or superscript in its runs."""
    for run in item.get("runs") or []:
        styles = run.get("char_style") or []
        if "subscript" in styles or "superscript" in styles:
            return True
    return False


def _has_script_table_runs(cell):
    """Check if a table cell has subscript/superscript in table_runs."""
    for run in cell.get("table_runs") or []:
        styles = run.get("char_style") or []
        if "subscript" in styles or "superscript" in styles:
            return True
    return False


def strip_script_markers(text):
    """Remove _() ^() markers, returning just the content inside."""
    def _replace(m):
        if m.group(1):
            return m.group(2)
        else:
            return m.group(4)
    return _MARKER_PAT.sub(_replace, text)
