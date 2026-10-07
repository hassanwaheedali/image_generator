"""Plain data containers used across the pipeline.

Everything here is a frozen dataclass (or an Enum) so results can be compared,
logged and serialised without aliasing surprises.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    """Per-product outcome.  ``ok``/``partial`` reach the CSVs, ``failed`` does not."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class Decision(str, Enum):
    """Outcome of a single image (or page) check, written to decisions.jsonl."""

    KEEP = "keep"
    REJECT = "reject"


class ImageRole(str, Enum):
    """Which sub-folder of the audit tree a local file belongs to."""

    RAW = "raw"
    ISOLATED = "isolated"
    FINAL_DIGIMALL = "final_digimall"
    FINAL_ALFAMALL = "final_alfamall"


@dataclass(frozen=True)
class ProductTask:
    """One sheet row: a title plus its ordered candidate source URLs."""

    index: int
    title: str
    source_urls: tuple[str, ...]

@dataclass(frozen=True)
class ModelKeys:
    """Result of parsing a title (see model_matching.build_model_keys).

    ``tokens``   - model-like tokens, in order ("HRF-538")
    ``identities`` - glued identity cores, aligned with ``tokens`` ("hrf538ifga")
    ``colours``  - colour families named in the title, e.g. ("white",)
    ``variant_facts`` - ("axis", "value") pairs, e.g. (("capacity_kg", "10"),)
    ``colour_word`` - the colour the title is selling ("burgundy"), "" when it names none
    ``name_words`` - series / colourway name words, noise removed (scored matcher)
    """

    cleaned_title: str
    tokens: tuple[str, ...]
    title_key: str
    identities: tuple[str, ...] = ()
    colours: tuple[str, ...] = ()
    variant_facts: tuple[tuple[str, str], ...] = ()
    #: The title's own colour word, for variant separation ("" when it names no colour).
    colour_word: str = ""
    #: Positional "name" words - series lines and unknown colourways - with brand-agnostic
    #: noise (type words, colours, model codes) removed; used by the scored matcher.
    name_words: tuple[str, ...] = ()

    @property
    def all_keys(self) -> tuple[str, ...]:
        """Model-like tokens first, then the whole-title key."""
        if self.title_key:
            return (*self.tokens, self.title_key)
        return self.tokens

    def variant_dict(self) -> dict[str, str]:
        """Variant facts as a plain dict (axes -> values)."""
        return dict(self.variant_facts)

    def __bool__(self) -> bool:
        return bool(self.all_keys)


@dataclass(frozen=True)
class PageMatchResult:
    """Stage-2 verdict for one page.

    ``strength`` 0 means: extract nothing (S1).  ``match_rule`` records *how* the
    page was accepted (``model_key`` / ``brand_code`` / ``body_model_key`` /
    ``body_brand_code`` / ``none``) so the audit trail shows it, and it also names the
    one rejection that is not about a missing code: ``colour_conflict``.
    ``variant_notes`` carries the size/type flags - they annotate the row, they never
    reject a photo (customer decision).
    """

    url: str
    strength: int
    matched_keys: tuple[str, ...]
    detail: str
    match_rule: str = "none"
    variant_notes: tuple[str, ...] = ()
    page_name: str = ""
    #: True when the page matched only through the dropped-trailing-letter tolerance -
    #: a flagged last resort, never proof (see ``model_matching.match_product``).
    tolerated: bool = False

    @property
    def accepted(self) -> bool:
        return self.strength > 0


@dataclass(frozen=True)
class SourceSearchReport:
    """What the fallback chain did for one row (goes into the run report Note)."""

    tried: tuple[str, ...] = ()
    used: str = ""
    note: str = ""


@dataclass(frozen=True)
class PageCandidate:
    """A *candidate* page found by the fallback chain (plan.md section 11).

    It is never trusted: ``pipeline`` re-runs ``verify_page`` on it with the sheet's
    own model (safeguard S15) before a single image URL is considered.
    """

    url: str
    origin: str
    method: str
    title_hint: str = ""


@dataclass(frozen=True)
class ImageCandidate:
    """A candidate image URL produced by one of the four allowed gallery sources.

    ``dom_flags`` carries the DOM-context safeguards that can only be evaluated
    while the tag is still attached to the tree (S2 description ancestor, S3 link to
    another page); the pipeline turns them into rejections so no extractor can
    bypass a safeguard.
    """

    url: str
    page_url: str
    source: str          # e.g. "registry:magento", "json_ld", "gallery_container"
    order: int           # gallery order (stable sort key)
    matches_model: bool  # candidate URL itself contains a model key
    dom_flags: tuple[str, ...] = ()   # e.g. ("S2",) or ("S3",)


@dataclass(frozen=True)
class ImageDecision:
    """One line of decisions.jsonl."""

    title: str
    url: str
    decision: str
    rule_id: str
    detail: str


@dataclass(frozen=True)
class AcceptedImage:
    """A fully processd, locally saved image (not yet uploaded)."""

    source_url: str
    dedup_key: str
    raw_path: Path
    square_path: Path
    digi_path: Path
    alfa_path: Path


@dataclass(frozen=True)
class ProductResult:
    """Everything the CSV writers and the checkpoint need for one product."""

    task_index: int
    title: str
    status: Status
    brand: str = ""
    digi_urls: tuple[str, ...] = ()
    alfa_urls: tuple[str, ...] = ()
    reason: str = ""
    pages_tried: int = 0
    pages_rejected: int = 0
    images_accepted: int = 0
    note: str = ""
    source_url_hash: str = ""

    def to_checkpoint_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "brand": self.brand,
            "source_url_hash": self.source_url_hash,
            "status": self.status.value,
            "digi_urls": list(self.digi_urls),
            "alfa_urls": list(self.alfa_urls),
            "reason": self.reason,
            "pages_tried": self.pages_tried,
            "pages_rejected": self.pages_rejected,
            "images_accepted": self.images_accepted,
            "note": self.note,
        }


@dataclass(frozen=True)
class CheckpointEntry:
    """A previously recorded product outcome, used to decide resume eligibility."""

    title: str
    source_url_hash: str
    status: Status
    digi_urls: tuple[str, ...]
    alfa_urls: tuple[str, ...]
    reason: str
    timestamp: str
    #: Brand for the import CSV.  Kept in the checkpoint so a resumed run still writes it
    #: (live bug: every resumed row had an empty Brand column).  Defaults to "" so
    #: checkpoints written before this field existed still load.
    brand: str = ""

    @staticmethod
    def from_dict(payload: dict[str, object]) -> CheckpointEntry:
        raw_status = str(payload.get("status", Status.FAILED.value))
        try:
            status = Status(raw_status)
        except ValueError:          # unknown status -> treat as failed (retry)
            status = Status.FAILED
        digi_raw = payload.get("digi_urls")
        alfa_raw = payload.get("alfa_urls")
        digi = (
            tuple(str(item) for item in digi_raw)
            if isinstance(digi_raw, list)
            else ()
        )
        alfa = (
            tuple(str(item) for item in alfa_raw)
            if isinstance(alfa_raw, list)
            else ()
        )
        return CheckpointEntry(
            title=str(payload.get("title", "")),
            source_url_hash=str(payload.get("source_url_hash", "")),
            status=status,
            digi_urls=digi,
            alfa_urls=alfa,
            reason=str(payload.get("reason", "")),
            timestamp=str(payload.get("timestamp", "")),
            brand=str(payload.get("brand", "")),
        )

    def is_resume_done(self, source_url_hash: str) -> bool:
        """Done = ok/partial, non-empty URL list, and unchanged source URLs."""
        if self.status not in (Status.OK, Status.PARTIAL):
            return False
        if not self.digi_urls and not self.alfa_urls:
            return False
        return self.source_url_hash == source_url_hash
