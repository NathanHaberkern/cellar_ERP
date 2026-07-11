"""
Template helpers for rendering service output whose exact shape isn't pinned
down here (the model/service source wasn't in the handoff package). The report
services return dicts / lists-of-dicts; `_generic.html` uses these to render any
of them as a readable table without me hard-coding column names. Once you know a
report's exact shape you can replace the generic include with a purpose-built
table -- but this works correctly in the meantime.
"""

from django import template

register = template.Library()


@register.filter
def is_mapping(value):
    return isinstance(value, dict)


@register.filter
def is_listy(value):
    return isinstance(value, (list, tuple)) and not isinstance(value, str)


@register.filter
def all_mappings(value):
    """True if a list is entirely dicts -> render as one table with shared columns."""
    return is_listy(value) and len(value) > 0 and all(isinstance(x, dict) for x in value)


@register.filter
def column_keys(value):
    """Union of keys across a list of dicts, preserving first-seen order."""
    keys = []
    for row in value:
        for k in row.keys():
            if k not in keys:
                keys.append(k)
    return keys


@register.filter
def get(mapping, key):
    """dict.get in templates (for pulling a column value out of a row)."""
    if isinstance(mapping, dict):
        return mapping.get(key, "")
    return ""


@register.filter
def humanize_key(value):
    """under_scored / camelCase-ish keys -> Title Case labels."""
    return str(value).replace("_", " ").strip().title()


@register.filter
def jsonify(value):
    """Render a dict/list as compact JSON for an editable textarea (rule params)."""
    import json
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return "{}"
