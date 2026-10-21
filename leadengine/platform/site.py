"""Server-rendered EN/AR marketing site. No template dependency: `{{key}}` i18n (escaped), `{{$x}}` context (escaped), `{{!x}}` trusted raw
context, `{{>partial}}` includes and `{{#each prefix N}}…{{.k}}…{{/each}}` which expands to the keys `prefix{i}.k`."""
from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

SITE_DIR = Path(__file__).resolve().parent.parent.parent / "site"
LANGS = ("en", "ar")
# page name -> (public path, template). Only these are public, indexable pages.
PAGES: dict[str, tuple[str, str]] = {
    "home": ("/", "home"), "features": ("/features", "features"), "how": ("/how-it-works", "how"), "security": ("/security", "security"),
    "compliance": ("/compliance", "compliance"), "pricing": ("/pricing", "pricing"), "faq": ("/faq", "faq"), "contact": ("/contact", "contact"),
    "privacy": ("/privacy", "privacy"), "terms": ("/terms", "terms"),
}
_TOKEN = re.compile(r"\{\{([>$!.#/]?)\s*([^}]*?)\s*\}\}")
_EACH = re.compile(r"\{\{#each ([\w.]+) (\d+)\}\}(.*?)\{\{/each\}\}", re.S)


@lru_cache(maxsize=None)
def _template(name: str) -> str:
    if not re.fullmatch(r"[a-z_]{1,30}", name):
        raise KeyError(name)
    return (SITE_DIR / "templates" / f"{name}.html").read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def strings(lang: str) -> dict[str, str]:
    if lang not in LANGS:
        raise KeyError(lang)
    return json.loads((SITE_DIR / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def _expand(text: str) -> str:
    """Resolve includes and each-blocks into plain `{{key}}` tokens."""
    text = re.sub(r"\{\{>(\w+)\}\}", lambda m: _expand(_template(m.group(1))), text)

    def each(m):
        prefix, n, body = m.group(1), int(m.group(2)), m.group(3)
        return "".join(re.sub(r"\{\{\.([\w.]+)\}\}", lambda t: "{{" + f"{prefix}{i}.{t.group(1)}" + "}}", body) for i in range(1, n + 1))
    return _EACH.sub(each, text)


def template_keys(name: str) -> set[str]:
    """Every i18n key a template (with its includes) needs. Used by the parity test."""
    return {m.group(2) for m in _TOKEN.finditer(_expand(_template(name))) if m.group(1) == ""}


def pricing_text(lang: str, path: Path | None = None) -> str:
    """Price text from site/pricing.json; anything missing or malformed falls back to the 'contact us' default."""
    try:
        data = json.loads((path or SITE_DIR / "pricing.json").read_text(encoding="utf-8"))
        text = data["price_text"][lang]
        if isinstance(text, str) and text.strip():
            return text.strip()[:200]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return strings(lang)["pricing.default_price"]


def _render(text: str, lang: str, ctx: dict) -> str:
    st = strings(lang)

    def sub(m):
        kind, key = m.group(1), m.group(2)
        if kind == "$":
            return html.escape(str(ctx.get(key, "")), quote=True)
        if kind == "!":
            return str(ctx.get(key, ""))
        if kind == "":
            return html.escape(st[key], quote=True)                 # KeyError on a missing key: fail loudly, never render a blank
        raise ValueError(f"unexpected token {m.group(0)!r}")
    return _TOKEN.sub(sub, text)


def jsonld(name: str, lang: str, base_url: str) -> str:
    st = strings(lang)
    graph: list[dict] = [
        {"@type": "Organization", "name": st["brand"], "url": base_url, "logo": f"{base_url}/static/brand/favicon-512.png"},
        {"@type": "WebSite", "name": st["brand"], "url": base_url, "inLanguage": lang},
        {"@type": "SoftwareApplication", "name": st["brand"], "applicationCategory": "BusinessApplication", "operatingSystem": "Web", "url": base_url},
    ]
    if name == "faq":
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": st[f"faq.i{i}.t"], "acceptedAnswer": {"@type": "Answer", "text": st[f"faq.i{i}.b"]}} for i in range(1, 9)]})
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")


def render_page(name: str, lang: str, ctx: dict | None = None) -> str:
    """Render a public or auth page. `ctx` values are escaped unless the template asks for raw (`{{!x}}`, trusted values only)."""
    ctx = dict(ctx or {})
    lang = lang if lang in LANGS else "en"
    st = strings(lang)
    base = (ctx.get("base_url") or "").rstrip("/")
    path = PAGES[name][0] if name in PAGES else ctx.get("path", "/")
    title = st[f"{name.replace('auth_', 'auth.')}.title"] if (name in PAGES or name.startswith("auth_")) else st["brand"]
    title = title if name == "home" else f"{title} | {st['brand']}"
    if len(title) > 70:
        title = title[:67].rstrip() + "…"
    key = name.replace("auth_", "auth.")
    ctx.update({
        "lang": lang, "dir": "rtl" if lang == "ar" else "ltr", "title": title, "description": st[f"{key}.desc"], "base_url": base,
        "canonical": f"{base}{path}?lang={lang}", "alt_en": f"{base}{path}?lang=en", "alt_ar": f"{base}{path}?lang=ar",
        "og_locale": "ar_AE" if lang == "ar" else "en_AE", "other_lang": "en" if lang == "ar" else "ar", "path_q": quote(path, safe=""),
    })
    ctx["path_q"] = ctx.get("lang_next_q") or ctx["path_q"]
    ctx.setdefault("price", pricing_text(lang))
    ctx.setdefault("jsonld", jsonld(name, lang, base))
    body = _render(_expand(_template(name)), lang, ctx)
    ctx["content"] = body
    return _render(_expand(_template("base")), lang, ctx)


def render_with_base(template: str, base: str, lang: str, ctx: dict) -> str:
    """Render `template` into `base` for the lean public (per-workspace) pages. `{{$x}}` values are escaped, `{{!x}}` are trusted
    server-built HTML only. Needs ctx `title`, `description`; `canonical_path`, `prefix` and `base_url` build canonical/hreflang URLs."""
    lang = lang if lang in LANGS else "en"
    c = dict(ctx)
    origin, prefix, path = (c.get("base_url") or "").rstrip("/"), c.get("prefix", ""), c.get("canonical_path", "/p")
    other = "en" if lang == "ar" else "ar"
    c.update({"lang": lang, "dir": "rtl" if lang == "ar" else "ltr", "other_lang": other,
              "canonical": f"{origin}{prefix}{path}?lang={lang}", "alt_en": f"{origin}{prefix}{path}?lang=en", "alt_ar": f"{origin}{prefix}{path}?lang=ar",
              "lang_href": f"{prefix}{path}?lang={other}"})
    c.setdefault("robots", "")
    c.setdefault("agent_line", "")
    c.setdefault("home_href", f"{prefix}/p")
    c["content"] = _render(_expand(_template(template)), lang, c)
    return _render(_expand(_template(base)), lang, c)


def detect_lang(accept_language: str | None, cookie: str | None, query: str | None = None) -> str:
    """Explicit choice (query, then cookie) wins; otherwise the best Accept-Language match; default English."""
    for choice in (query, cookie):
        if choice in LANGS:
            return choice
    best, best_q = "en", -1.0
    for part in (accept_language or "").split(","):
        tag, _, q = part.strip().partition(";q=")
        base = tag.strip().lower().split("-")[0]
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            continue
        if base in LANGS and weight > best_q:
            best, best_q = base, weight
    return best


def sitemap_xml(base_url: str) -> str:
    base = base_url.rstrip("/")
    urls = "".join(f"<url><loc>{html.escape(base + p + q, quote=True)}</loc></url>" for p, _ in PAGES.values() for q in ("", "?lang=en", "?lang=ar"))
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
