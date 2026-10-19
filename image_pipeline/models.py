"""Plain data containers used across the pipeline.

Everything here is a frozen dataclass (or an Enum) so results can be compared,
logged and serialised without aliasing surprises.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    """Which sub-folder of the audit tree a local file belongs to.

    The audit tree is the evidence that never goes to a shop.  Composited finals are *not*
    a role: they live under ``images/<platform key>/`` and are addressed by that key (see
    ``AcceptedImage.final_paths``), so a fourth platform needs no new member here.
    """

    RAW = "raw"
    CLEAN = "clean"
    ISOLATED = "isolated"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ProductTask:
    """One sheet row: a title plus its ordered candidate source URLs."""

    index: int
    title: str
    source_urls: tuple[str, ...]
    #: Series words that name *other rows* of the same sheet (see
    #: ``model_matching.sibling_series_terms``).  A file whose name carries one of
    #: these belongs to another product in the catalog, whatever the page's status -
    #: the Orient run of 2026-10-10 published the Innova file on the Pulse row because
    #: neither URL carried the ``19C`` key.  Empty for a single-row sheet.
    sibling_terms: frozenset[str] = frozenset()
    #: This row's *own* series words.  A file carrying one of them is anchored to the
    #: row, so a sibling word beside it is decoration, not identity (live 2026-10-11:
    #: ``…Hyper-18X-T3-Super-White-Turbo-Pro.webp`` must not die on the word ``turbo``).
    own_terms: frozenset[str] = frozenset()
    #: How many rows of this sheet share the row's model-code **tail** (see
    #: ``model_matching.annotate_tail_owners``).  The tail-chunk last resort arms only
    #: at exactly ``1``: ``0`` means never computed (tests, single passes) and ``> 1``
    #: is an ambiguous tail the sheet cannot resolve silently - both stay dead and loud.
    tail_owner_count: int = 0
    #: Option 3 (``--images-dir``) only: the stem every platform's file names share,
    #: computed once from the folder title (``csv_io.local_filename_prefix``).  Sheet
    #: rows leave it ``None`` and keep the model-code stem of owner decision D7.
    #: ``csv_io.merge_duplicate_titles`` rebuilds tasks without it, so the Option 3
    #: wiring must set it *after* the merge (``main._build_tasks``).
    name_prefix_override: str | None = None

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
    #: The shade in front of it - ``pearl`` of ``Pearl Red`` ("" when the title states none).
    #: 9140-WB is sold as Pearl, Ruby and Diamond Red, so the head word alone names no product.
    colour_shade: str = ""
    #: Positional "name" words - series lines and unknown colourways - with brand-agnostic
    #: noise (type words, colours, model codes) removed; used by the scored matcher.
    name_words: tuple[str, ...] = ()
    #: Significant title words for rows that carry *no* model code (owner 2026-10-09:
    #: "Dawlance Dual Pro 30 Inverter T3" is the model - keywords, not a token).  Empty
    #: whenever the title has a model-like token; then S1 uses the keys as before.
    keywords: tuple[str, ...] = ()
    #: Colourway letters the sheet wrote **inside** the model code, e.g. ``(OW)`` of
    #: ``HSU-20HFTEX/023WDC(OW)-T3``.  They are part of the model's identity, so a
    #: tolerance may not drop them (Haier sells one body as -OW- Ocean White and -DG-
    #: Dark Grey).  Empty unless the title actually spells a code this way.
    colourway_markers: frozenset[str] = frozenset()

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
    """A fully processed, locally saved image (not yet uploaded).

    ``final_paths`` is an ordered ``(platform key, path)`` tuple: one entry per platform the
    run selected, in the order the platforms were resolved.  Ordered pairs rather than a dict
    so the instance stays frozen and hashable - the same idiom ``ModelKeys.variant_facts``
    already uses.
    """

    source_url: str
    dedup_key: str
    raw_path: Path
    square_path: Path
    final_paths: tuple[tuple[str, Path], ...]
    #: The page that offered this picture (``S22.source_lock`` keeps an album to one source,
    #: and an owner reviewing a mixed gallery needs to see where each file came from).  Empty
    #: only for ``--direct``, where there is no page: the sheet hands over image URLs, so the
    #: accept line records no ``page=`` and the source lock has nothing to lock.
    page_url: str = ""

    def final_path(self, key: str) -> Path:
        """The composed final for one platform.

        Raises instead of returning ``None``: a platform with no final means the row would
        publish half-filled, and S14 requires every selected platform or none at all.
        """
        for platform, path in self.final_paths:
            if platform == key:
                return path
        raise KeyError(f"no final image for platform {key!r}")


#: On-disk shape of a checkpoint line.  Lines written by an older layout are recognised and
#: then **re-processed**, never resumed: a pre-platform-split entry stores two fixed URL
#: lists, so a run that now publishes three platforms could not attribute those URLs to the
#: extra one and would resume a row it cannot actually write.  Bump this whenever a stored
#: entry stops meaning what it used to.
CHECKPOINT_LAYOUT: int = 2


@dataclass(frozen=True)
class ProductResult:
    """Everything the CSV writers and the checkpoint need for one product."""

    task_index: int
    title: str
    status: Status
    brand: str = ""
    #: Published URLs per platform as ordered ``(platform key, urls)`` pairs, in the order
    #: the run resolved its platforms - the same idiom ``AcceptedImage.final_paths`` uses,
    #: so the instance stays frozen and a fourth platform needs no new field.
    platform_urls: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Filename stem shared by every platform's pictures for this product
    #: (``12HRFN1_QC6`` -> ``12HRFN1_QC6_1.jpg``).  Carried on the result so the import
    #: sheets can name the files each URL belongs to; stored in the checkpoint so a
    #: resumed row still fills its ``images`` column.
    name_prefix: str = ""
    reason: str = ""
    pages_tried: int = 0
    pages_rejected: int = 0
    images_accepted: int = 0
    note: str = ""
    source_url_hash: str = ""

    def urls_for(self, key: str) -> tuple[str, ...]:
        """One platform's URL list; ``()`` when the row published nothing there.

        Silent for a missing key, unlike :meth:`AcceptedImage.final_path`: a failed row
        legitimately carries no URLs, and the CSV writers skip those rows anyway.
        """
        for platform, urls in self.platform_urls:
            if platform == key:
                return urls
        return ()

    def to_checkpoint_dict(self) -> dict[str, object]:
        return {
            "layout": CHECKPOINT_LAYOUT,
            "title": self.title,
            "brand": self.brand,
            "source_url_hash": self.source_url_hash,
            "status": self.status.value,
            "platform_urls": [[key, list(urls)] for key, urls in self.platform_urls],
            "name_prefix": self.name_prefix,
            "reason": self.reason,
            "pages_tried": self.pages_tried,
            "pages_rejected": self.pages_rejected,
            "images_accepted": self.images_accepted,
            "note": self.note,
        }


def _platform_urls(raw: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``[["digi", ["url", ...]], ...]`` back into ordered pairs.

    A malformed pair is skipped rather than raising: the checkpoint is append-only
    history, and one damaged line must not cost a resume of everything after it.
    """
    if not isinstance(raw, list):
        return ()
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            continue
        if not isinstance(item[1], list):
            continue
        pairs.append((item[0], tuple(str(url) for url in item[1])))
    return tuple(pairs)


@dataclass(frozen=True)
class CheckpointEntry:
    """A previously recorded product outcome, used to decide resume eligibility."""

    title: str
    source_url_hash: str
    status: Status
    #: The same ordered ``(platform key, urls)`` pairs ``ProductResult`` publishes with.
    platform_urls: tuple[tuple[str, tuple[str, ...]], ...]
    reason: str
    timestamp: str
    #: Brand for the import CSV.  Kept in the checkpoint so a resumed run still writes it
    #: (live bug: every resumed row had an empty Brand column).  Defaults to "" so
    #: checkpoints written before this field existed still load.
    brand: str = ""
    #: Layout this line was written in; see :data:`CHECKPOINT_LAYOUT`.  Absent means 1.
    layout: int = 1
    #: The shared filename stem, so a resumed row still writes its ``images`` column.
    name_prefix: str = ""

    @staticmethod
    def from_dict(payload: dict[str, object]) -> CheckpointEntry:
        raw_status = str(payload.get("status", Status.FAILED.value))
        try:
            status = Status(raw_status)
        except ValueError:          # unknown status -> treat as failed (retry)
            status = Status.FAILED
        raw_layout = payload.get("layout", 1)
        return CheckpointEntry(
            title=str(payload.get("title", "")),
            source_url_hash=str(payload.get("source_url_hash", "")),
            status=status,
            platform_urls=_platform_urls(payload.get("platform_urls")),
            reason=str(payload.get("reason", "")),
            timestamp=str(payload.get("timestamp", "")),
            brand=str(payload.get("brand", "")),
            layout=raw_layout if isinstance(raw_layout, int) else 1,
            name_prefix=str(payload.get("name_prefix", "")),
        )

    def is_resume_done(self, source_url_hash: str, platform_keys: Sequence[str]) -> bool:
        """Done = current layout, ok/partial, a hosted URL on **every** platform this run
        publishes, and unchanged source URLs.

        A ``--dry-run`` entry carries ``file://`` local paths instead of real uploads;
        resuming one into a live run would publish local paths to the platforms, so
        dry entries must always be re-processed (owner run 2026-10-08: he deleted
        ``output/`` and wondered why rows still said "already done" - the resume
        cache lives in ``state/checkpoint.jsonl``, and dry entries poisoned it).

        An older layout never resumes either: its URLs cannot be attributed to the
        platforms this run publishes (see :data:`CHECKPOINT_LAYOUT`).

        Covering the whole plan is what makes "done" mean something to a shop: an entry
        published by a ``--platforms digi`` run has nothing for Alfamall, so a run that
        publishes Alfamall re-processes the row instead of leaving its sheet short.
        """
        if self.layout != CHECKPOINT_LAYOUT:
            return False
        if self.status not in (Status.OK, Status.PARTIAL):
            return False
        if self.source_url_hash != source_url_hash:
            return False
        per_platform = dict(self.platform_urls)
        if not platform_keys or not all(per_platform.get(key) for key in platform_keys):
            return False
        return all(
            url.startswith(("http://", "https://"))
            for key in platform_keys
            for url in per_platform[key]
        )
