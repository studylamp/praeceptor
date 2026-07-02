"""Render tutor markdown to safe HTML for display.

Tutor replies are model-generated, so we treat them as untrusted: markdown is
rendered with raw-HTML disabled, then the result is sanitized with a tight tag/attr
allowlist (nh3) before it's marked safe for the template. Math is left as
`<span class="math …">LaTeX</span>` / `<div class="math …">` for client-side KaTeX
to typeset (the dollarmath plugin tokenizes `$…$` before markdown, so LaTeX
underscores/backslashes aren't mangled).

Diagrams: a tutor can draw an inline SVG (graphs, geometry, charts, star maps) by
emitting a fenced code block tagged `svg`. That ONE controlled channel is turned into
raw inline SVG and then sanitized by nh3 against a conservative SVG subset (no script,
foreignObject, external images, or event handlers). All OTHER raw HTML stays escaped.

Student text is NOT rendered through here — it stays plain, escaped text.
"""

import html as _html
import re

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup
from mdit_py_plugins.dollarmath import dollarmath_plugin

# html=False → any literal HTML in the tutor text is escaped, not passed through.
# breaks=True → single newlines become <br> (chat-style line breaks).
_md = MarkdownIt("js-default", {"html": False, "breaks": True}).use(dollarmath_plugin)

_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "del", "s",
    "code", "pre", "ul", "ol", "li", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a", "span", "div",
    "table", "thead", "tbody", "tr", "th", "td",
}

# A conservative inline-SVG subset for tutor diagrams. Deliberately excludes script,
# foreignObject, image, use, and animate* — nh3 strips anything not listed (and event
# handlers / javascript: URLs), so model output can't smuggle executable content.
# Attribute names are case-sensitive in nh3: SVG camelCase attrs (viewBox,
# preserveAspectRatio, gradientUnits, …) must be listed with their exact casing.
_SVG_TAGS = {
    "svg", "g", "defs", "title", "desc", "path", "line", "polyline", "polygon",
    "rect", "circle", "ellipse", "text", "tspan", "linearGradient", "radialGradient",
    "stop", "marker",
}
_PRESENT = {"transform", "opacity", "fill", "stroke", "stroke-width", "stroke-linecap",
            "stroke-linejoin", "stroke-dasharray", "fill-opacity", "stroke-opacity"}
_SVG_ATTRS = {
    "svg": {"viewBox", "xmlns", "width", "height", "preserveAspectRatio", "role", "aria-label"},
    "g": {"transform", "fill", "stroke", "opacity"},
    "path": {"d", *_PRESENT},
    "line": {"x1", "y1", "x2", "y2", *_PRESENT},
    "polyline": {"points", *_PRESENT},
    "polygon": {"points", *_PRESENT},
    "rect": {"x", "y", "width", "height", "rx", "ry", *_PRESENT},
    "circle": {"cx", "cy", "r", *_PRESENT},
    "ellipse": {"cx", "cy", "rx", "ry", *_PRESENT},
    "text": {"x", "y", "dx", "dy", "font-family", "font-size", "font-weight",
             "text-anchor", *_PRESENT},
    "tspan": {"x", "y", "dx", "dy", "font-size", "font-weight", "text-anchor", *_PRESENT},
    "linearGradient": {"id", "x1", "y1", "x2", "y2", "gradientUnits", "gradientTransform",
                       "spreadMethod"},
    "radialGradient": {"id", "cx", "cy", "r", "fx", "fy", "gradientUnits", "gradientTransform",
                       "spreadMethod"},
    "stop": {"offset", "stop-color", "stop-opacity"},
    "marker": {"id", "markerWidth", "markerHeight", "refX", "refY", "orient",
               "markerUnits", "viewBox"},
}

_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "span": {"class"},
    "div": {"class"},
    **_SVG_ATTRS,
}
_ALL_TAGS = _ALLOWED_TAGS | _SVG_TAGS

# --- Trusted figure SVGs (matplotlib, server-generated) ------------------------------
# Plots are produced by our own matplotlib code in the sandbox from numeric data — the
# model never writes them — and arrive via the separate ```svgfig fence. matplotlib's
# output needs a broader element/attr set than the strict model-SVG allowlist (clipPath,
# defs, use, style attrs, …). This profile stays SAFE for any input: no <script>,
# <style> element, <foreignObject>, event handlers, or external references — so even a
# (forged) model-written ```svgfig can only ever produce inert vector graphics, the same
# as ```svg, just with the richer matplotlib shape set.
_FIG_PRESENT = _PRESENT | {"clip-path", "id", "style", "transform"}
_FIG_TAGS = {
    "svg", "g", "defs", "clipPath", "use", "path", "line", "polyline", "polygon",
    "rect", "circle", "ellipse", "text", "tspan", "linearGradient", "radialGradient",
    "stop", "marker", "title", "desc",
}
_FIG_ATTRS = {
    "svg": {"viewBox", "xmlns", "xmlns:xlink", "width", "height", "version",
            "preserveAspectRatio", "role", "aria-label"},
    "g": {"clip-path", "id", "transform", "style", "fill", "stroke", "opacity"},
    "defs": {"id"},
    "clipPath": {"id", "clipPathUnits", "transform"},
    "use": {"x", "y", "width", "height", "xlink:href", "href", "transform", "style", "id"},
    "path": {"d", "id", *_FIG_PRESENT},
    "line": {"x1", "y1", "x2", "y2", *_FIG_PRESENT},
    "polyline": {"points", *_FIG_PRESENT},
    "polygon": {"points", *_FIG_PRESENT},
    "rect": {"x", "y", "width", "height", "rx", "ry", *_FIG_PRESENT},
    "circle": {"cx", "cy", "r", *_FIG_PRESENT},
    "ellipse": {"cx", "cy", "rx", "ry", *_FIG_PRESENT},
    "text": {"x", "y", "dx", "dy", "font-family", "font-size", "font-weight",
             "text-anchor", *_FIG_PRESENT},
    "tspan": {"x", "y", "dx", "dy", "font-size", "font-weight", "text-anchor", *_FIG_PRESENT},
    "linearGradient": {"id", "x1", "y1", "x2", "y2", "gradientUnits", "gradientTransform",
                       "spreadMethod"},
    "radialGradient": {"id", "cx", "cy", "r", "fx", "fy", "gradientUnits", "gradientTransform",
                       "spreadMethod"},
    "stop": {"offset", "stop-color", "stop-opacity", "style"},
    "marker": {"id", "markerWidth", "markerHeight", "refX", "refY", "orient",
               "markerUnits", "viewBox"},
}

# A figure SVG is bounded in size (the sandbox caps each at 400 KB); reject anything
# larger to stop a forged ```svgfig fence from driving the synchronous sanitizer into a
# pathological/expensive pass in the request path.
_FIG_MAX_BYTES = 450_000

# Cosmetic pre-clean BEFORE nh3 — purely linear, no backtracking. Safety is nh3's job
# (it strips <script>/<style> AND their content as clean-content tags, and drops/sanitizes
# anything not in _FIG_TAGS). These passes only remove the XML prolog/doctype and the
# <metadata> RDF block, whose text nh3 would otherwise leave behind as a stray caption.
# The prolog/doctype regex has NO unbounded body scan (bounded char classes only), so it
# can't go quadratic; <metadata> is stripped by a linear scanner (_strip_tag_blocks).
_FIG_STRIP_PROLOG = re.compile(r"<\?xml[^>]{0,400}\?>|<!DOCTYPE[^>]{0,400}>", re.IGNORECASE)


def _strip_tag_blocks(svg: str, tag: str) -> str:
    """Remove every <tag ...>...</tag> block (linear; unclosed → dropped to end). Used
    for cosmetics only — not a security control."""
    low = svg.lower()
    open_lit, close_lit = "<" + tag, "</" + tag + ">"
    out: list[str] = []
    i = 0
    while True:
        j = low.find(open_lit, i)
        if j < 0:
            out.append(svg[i:])
            break
        k = low.find(close_lit, j)
        if k < 0:
            out.append(svg[i:j])  # unclosed — drop the rest
            break
        out.append(svg[i:j])
        i = k + len(close_lit)
    return "".join(out)
# Enforce #fragment-only on href/xlink:href AFTER nh3 (which normalizes attrs to double
# quotes, so this single pattern also covers originally-unquoted values). nh3 does NOT
# scheme-filter href on <use>, so this — not the sanitizer — is the external-ref guard.
_FIG_HREF_FRAGMENT = re.compile(r'\b(xlink:href|href)\s*=\s*"(?!#)[^"]*"', re.IGNORECASE)

# CSS in a `style` attribute is passed through by nh3 VERBATIM (it doesn't parse CSS), so
# we filter it ourselves to an inert allowlist: only these visual properties survive, and
# any value carrying a fetch/active vector is dropped. matplotlib needs `style` for
# colors/strokes, so we can't just drop the attribute. (Also: never add "style" as a
# TAG to _FIG_TAGS — nh3 panics if a tag is both a tag and a clean-content tag.)
_SAFE_CSS_PROPS = frozenset({
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit",
    "stroke-opacity", "opacity", "color", "font", "font-family", "font-size",
    "font-style", "font-weight", "font-variant", "text-anchor", "dominant-baseline",
    "alignment-baseline", "baseline-shift", "paint-order", "letter-spacing",
    "word-spacing", "visibility", "marker-start", "marker-mid", "marker-end",
})
# Reject a declaration whose value carries an active/fetch vector. url(#frag) is allowed
# (matplotlib markers); url( to anything else, and the legacy IE/Mozilla vectors, are not.
_CSS_BAD_VALUE = re.compile(r"""expression|@import|behavior|-moz-binding|javascript:|url\(\s*['"]?(?!#)""", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"""(style\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def _safe_style_value(value: str) -> str:
    keep = []
    for decl in value.split(";"):
        prop, sep, val = decl.partition(":")
        if not sep:
            continue
        prop = prop.strip().lower()
        val = val.strip()
        # Drop any declaration carrying a backslash: CSS escapes (e.g. u\72l(...) →
        # url(...)) would otherwise slip a fetch past the literal denylist below.
        # matplotlib never emits escaped style values, so this costs nothing legitimate.
        if "\\" in val or "\\" in prop:
            continue
        if prop in _SAFE_CSS_PROPS and val and not _CSS_BAD_VALUE.search(val):
            keep.append(f"{prop}: {val}")
    return "; ".join(keep)


def _filter_styles(svg: str) -> str:
    return _STYLE_ATTR.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{_safe_style_value(m.group(3))}{m.group(2)}", svg)

# markdown-it renders ```svg as <pre><code class="language-svg">ESCAPED</code></pre>.
# Convert that single channel back to raw inline SVG (wrapped for styling); nh3 then
# sanitizes it. The fence content is markdown-escaped, so a literal "</code></pre>"
# inside it can't break out of this match.
_SVG_FENCE = re.compile(r'<pre><code class="language-svg">(.*?)</code></pre>', re.DOTALL)
# Trusted figure fence (server-generated matplotlib). Distinct language so it can't be
# confused with the strict model ```svg path.
_FIG_FENCE = re.compile(r'<pre><code class="language-svgfig">(.*?)</code></pre>', re.DOTALL)


def _svg_fence_to_figure(match: "re.Match[str]") -> str:
    svg = _html.unescape(match.group(1))
    return f'<div class="svg-figure">{svg}</div>'


def _fig_fence_to_figure(match: "re.Match[str]") -> str:
    """Turn a ```svgfig fence into a sanitized inline figure using the broader (but still
    inert) matplotlib profile. Pre-cleaned of prolog/metadata/style/script/foreignObject
    and external hrefs, then nh3 with the figure allowlist as the final guarantee."""
    svg = _html.unescape(match.group(1))
    if len(svg) > _FIG_MAX_BYTES:
        return '<div class="svg-figure"><p class="muted">[figure too large to display]</p></div>'
    svg = _FIG_STRIP_PROLOG.sub("", svg)
    svg = _strip_tag_blocks(svg, "metadata")  # cosmetic; nh3 enforces safety
    svg = _filter_styles(svg)  # inert-CSS allowlist (nh3 won't sanitize style contents)
    clean = nh3.clean(svg, tags=_FIG_TAGS, attributes=_FIG_ATTRS,
                      link_rel="noopener noreferrer nofollow")
    # nh3 doesn't scheme-filter href on <use>; force #fragment-only now that quoting is
    # normalized (this also catches originally-unquoted external refs).
    clean = _FIG_HREF_FRAGMENT.sub(r'\1="#"', clean)
    return f'<div class="svg-figure">{clean}</div>'


# Placeholder for a trusted figure during the outer sanitize pass. The figure's richer
# tag set (clipPath/use/…) would be stripped by the outer clean, so we stash the already-
# sanitized figure, leave a plain-text token through the outer clean, then re-insert it.
_FIG_TOKEN = "@@PRAECEPTOR_FIG_{}@@"
_FIG_TOKEN_RE = re.compile(r"@@PRAECEPTOR_FIG_(\d+)@@")


def render_tutor_markdown(text: str | None) -> Markup:
    html = _md.render(text or "")

    # Trusted matplotlib figures: sanitize each with the figure profile up front and
    # swap in a text token so the outer clean (strict allowlist) can't strip the richer
    # figure tags. Re-inserted after the clean.
    figures: list[str] = []

    def _stash(match: "re.Match[str]") -> str:
        figures.append(_fig_fence_to_figure(match))
        return _FIG_TOKEN.format(len(figures) - 1)

    html = _FIG_FENCE.sub(_stash, html)
    # Model-drawn SVG goes through the STRICT allowlist as part of the outer clean.
    html = _SVG_FENCE.sub(_svg_fence_to_figure, html)
    clean = nh3.clean(
        html,
        tags=_ALL_TAGS,
        attributes=_ALLOWED_ATTRS,
        link_rel="noopener noreferrer nofollow",
        # url_schemes defaults to http/https/mailto/tel etc. — drops javascript:
    )

    if figures:
        def _restore(match: "re.Match[str]") -> str:
            i = int(match.group(1))
            return figures[i] if 0 <= i < len(figures) else ""
        clean = _FIG_TOKEN_RE.sub(_restore, clean)

    return Markup(clean)  # sanitized → safe to emit unescaped in the template
