from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader

from ..http_client import PublicHttpClient
from ..store import EvidenceStore
from ..util import file_fingerprint, normalize_address, normalize_text, sha256_bytes, stable_id, utcnow

PARSER_VERSION = "property-document-intelligence/1.1.0"

TYPE_RULES = {
    "mortgage_or_security_instrument": ("mortgage", "security instrument", "promissory note", "lender", "borrower"),
    "deed_or_transfer": ("quitclaim deed", "warranty deed", "grantor", "grantee", "consideration"),
    "lease_or_tenant_material": ("lease", "tenant", "rent", "occupancy", "available space"),
    "permit_or_entitlement": ("permit", "planning board", "zoning board", "special permit", "variance", "site plan"),
    "environmental_report": ("phase i", "environmental site assessment", "recognized environmental condition", "release", "remediation"),
    "securities_filing": ("securities and exchange commission", "form 8-k", "exhibit 21", "subsidiaries"),
    "property_report": ("property profile", "parcel", "assessed value", "building area"),
}

MENTION_PATTERNS = {
    "parcel_identifier": re.compile(r"\b(?:APN|PARCEL(?:\s+(?:ID|NO\.?|NUMBER))?)\s*[:#]?\s*([A-Z0-9-]{5,24})", re.I),
    "money": re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?(?:\s*(?:million|mm|m|thousand|k))?", re.I),
    "area": re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*(?:square\s+feet|sq\.?\s*ft\.?|sf|acres?)\b", re.I),
    "date": re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", re.I),
    "organization": re.compile(r"\b[A-Z][A-Za-z0-9&'., -]{2,80}\s+(?:LLC|L\.L\.C\.|INC\.?|CORP(?:ORATION)?\.?|LP|L\.P\.|TRUST|BANK|REIT)\b"),
    "street_address": re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.' -]{2,50}\s+(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Drive|Dr\.?|Lane|Ln\.?|Highway|Hwy\.?)\b", re.I),
}


def fingerprint(config: dict[str, Any]) -> str:
    inputs = []
    for item in config.get("items", []):
        if item.get("path"):
            inputs.append(("file", file_fingerprint(item["path"]), item))
        else:
            inputs.append(("url", item.get("url"), item.get("source_date"), item))
    return stable_id("input", PARSER_VERSION, inputs)


def _extract(content: bytes, content_type: str, title: str) -> tuple[list[str], str]:
    lower = title.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(content))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return pages, "pdf_text"
    text = content.decode("utf-8", errors="replace")
    if "html" in content_type or lower.endswith((".html", ".htm")):
        soup = BeautifulSoup(text, "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        text = soup.get_text("\n", strip=True)
        return [text], "html_text"
    return [text], "plain_text"


def _classify(text: str, declared: str | None) -> tuple[str, dict[str, int]]:
    normalized = text.lower()
    scores = {kind: sum(normalized.count(term) for term in terms) for kind, terms in TYPE_RULES.items()}
    winner = max(scores, key=scores.get) if scores and max(scores.values()) else "unclassified_document"
    return declared or winner, scores


def collect(store: EvidenceStore, target_id: str, target: dict[str, Any],
            config: dict[str, Any]) -> dict[str, Any]:
    client = PublicHttpClient(store, timeout=int(config.get("timeout_seconds", 90)))
    old_ids = [r[0] for r in store.rows("SELECT document_id FROM documents WHERE target_id=?", (target_id,))]
    store.db.execute("DELETE FROM document_mentions WHERE document_id IN (SELECT document_id FROM documents WHERE target_id=?)", (target_id,))
    store.db.execute("DELETE FROM documents WHERE target_id=?", (target_id,))
    if old_ids:
        marks = ",".join("?" for _ in old_ids)
        store.db.execute(f"DELETE FROM facts WHERE subject_id IN ({marks})", tuple(old_ids))
        store.db.execute(f"DELETE FROM relationships WHERE from_entity_id IN ({marks}) OR to_entity_id IN ({marks})",
                         tuple(old_ids) + tuple(old_ids))
        store.db.execute(f"DELETE FROM entities WHERE entity_id IN ({marks})", tuple(old_ids))
    stats: dict[str, Any] = {"documents": 0, "mentions": 0, "classifications": {}, "ocr_required": 0, "errors": []}
    for item in config.get("items", []):
        try:
            path = Path(item["path"]) if item.get("path") else None
            if path:
                content = path.read_bytes()
                suffix = path.suffix.lower()
                content_type = {".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html",
                                ".json": "application/json", ".txt": "text/plain", ".md": "text/markdown"}.get(suffix, "application/octet-stream")
                raw_sha = store.put_raw(content, content_type)
                retrieved = utcnow()
                url = item.get("url")
                access_note = f"Local evidence file retained verbatim: {path.resolve()}"
            else:
                fetched = client.fetch(item["url"], cache_days=int(item.get("refresh_days", 30)))
                content, content_type, raw_sha, retrieved, url = (fetched.content, fetched.content_type,
                                                                  fetched.raw_sha256, fetched.retrieved_at, fetched.url)
                access_note = "Public document fetched without login, CAPTCHA, paywall, or access-control bypass."
            title = item.get("title") or (path.name if path else item["url"].rsplit("/", 1)[-1])
            pages, extraction = _extract(content, content_type, title)
            text = "\n\n".join(pages)
            text_sha = store.put_raw(text, "text/plain")
            doc_type, scores = _classify(text, item.get("document_type"))
            source_id = store.source(name=item.get("source_name", title), url=url,
                                     authority=item.get("authority", "public document"),
                                     parser_version=PARSER_VERSION, raw_sha256=raw_sha,
                                     source_date=item.get("source_date"), retrieved_at=retrieved,
                                     access_note=access_note)
            document_id = store.document(target_id=target_id, title=title, document_type=doc_type,
                                         source_id=source_id, raw_sha256=raw_sha, content_type=content_type,
                                         text_sha256=text_sha, page_count=len(pages),
                                         published_date=item.get("source_date"), parser_version=PARSER_VERSION,
                                         attributes={"classification_scores": scores, "extraction": extraction,
                                                     "scope": item.get("scope", "property_specific")})
            store.entity("document", title, external_id=document_id,
                         attributes={"document_type": doc_type, "content_type": content_type},
                         entity_id=document_id)
            store.fact(subject_id=document_id, category="documents", predicate="document_classification",
                       value={"type": doc_type, "scores": scores}, fact_class="inference", confidence=0.72,
                       source_id=source_id, parser_version=PARSER_VERSION, raw_sha256=raw_sha,
                       evidence_locator="Deterministic keyword classifier; declared manifest type takes precedence")
            if not text.strip():
                stats["ocr_required"] += 1
                store.gap(target_id, "documents", "partial", f"OCR required for document: {title}",
                          reason="No embedded text was available; no OCR engine was configured.", source_url=url)
            for page_number, page in enumerate(pages, start=1):
                for mention_type, pattern in MENTION_PATTERNS.items():
                    for match in pattern.finditer(page):
                        raw_value = match.group(1) if match.lastindex else match.group(0)
                        normalized = normalize_address(raw_value) if mention_type == "street_address" else normalize_text(raw_value)
                        start, end = match.span()
                        store.mention(document_id=document_id, mention_type=mention_type,
                                      raw_value=raw_value, normalized_value=normalized,
                                      page_number=page_number, character_start=start, character_end=end,
                                      context=page[max(0, start - 100):min(len(page), end + 100)].replace("\n", " "),
                                      confidence=0.75, parser_version=PARSER_VERSION)
                        stats["mentions"] += 1
            store.relationship(from_id=document_id, relationship_type="evidence_for", to_id=target_id,
                               fact_class="reported" if item.get("authority", "").lower().startswith(("owner", "broker", "mixed")) else "confirmed_official",
                               confidence=float(item.get("confidence", 0.85)), source_id=source_id,
                               parser_version=PARSER_VERSION, raw_sha256=raw_sha,
                               effective_date=item.get("source_date"),
                               explanation={"document_type": doc_type, "extraction": extraction})
            stats["documents"] += 1
            stats["classifications"][doc_type] = stats["classifications"].get(doc_type, 0) + 1
        except Exception as exc:
            stats["errors"].append({"item": item.get("title") or item.get("path") or item.get("url"), "error": str(exc)})
    if stats["errors"]:
        store.gap(target_id, "documents", "partial", "One or more configured documents failed",
                  reason=str(stats["errors"]))
    else:
        store.resolve_gap(target_id, "One or more configured documents failed", "All configured documents completed.")
    return stats
