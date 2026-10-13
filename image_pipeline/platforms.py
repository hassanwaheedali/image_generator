"""Which platforms a run publishes, and what each one does with its closing banner.

The three platforms share every gate, the whole compositing path and the upload
machinery; they differ only in *data* - which template to paste on, which folder
and CSV to write to, and whether the closing banner belongs in the set.  Holding
that data in one table means a fourth platform is a row here plus a folder under
``assets/``, not another pass through a dozen two-branch ``if`` statements.

Everything in this module is pure: no filesystem writes, no network, no logging.
Callers decide what to do with the notices.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from config import ConfigError

if TYPE_CHECKING:
    from config import PipelineConfig

#: File stems inside ``assets/<platform key>/``.  Owner decision 2026-10-13: a folder
#: per platform holding "all relevant files", so the filename never has to carry the
#: platform name and no config field has to remember it.
TEMPLATE_STEM = "template"
BANNER_STEM = "last"

#: Exactly two extensions are tried, in this order.  A glob would let a stray
#: ``template (final) v2.png`` win silently, and "which file did it pick?" must
#: always have one answer.
ASSET_EXTENSIONS: tuple[str, ...] = (".png", ".jpg")


class BannerPolicy(str, Enum):
    """Whether a platform's set of images ends with the closing banner."""

    #: Alfa and Digi publish the banner as their last image.
    KEEP = "keep"
    #: Website drops it by rule - the site has its own closing slot.
    DROP = "drop"


class BannerMode(str, Enum):
    """What this run will actually do about one platform's banner.

    Resolved once at startup and then frozen, so a mid-run decision can never make
    some products carry a banner and others not.
    """

    APPEND = "append"
    SKIP_BY_POLICY = "skip_by_policy"
    SKIP_MISSING_ASSET = "skip_missing_asset"


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's published identity.  Pure data - no paths, no behaviour."""

    #: Folder name under ``assets/``, ``images/`` and the run folder, and the key
    #: every other module uses to address this platform.
    key: str
    #: What the terminal and the CSV README call it.
    display_name: str
    #: Not derivable from ``key``: the ``alfa`` folder holds ``alfamall_...``.
    csv_filename: str
    banner_policy: BannerPolicy
    #: ``None`` inherits the global quotas.  Owner decision 2026-10-13: all three
    #: platforms share ``target_/max_/min_required_images`` for now.
    max_final_images: int | None = None


#: The platform table.  Order is the order the terminal, the CSVs and the notices
#: list them in, so it is part of the contract.
DEFAULT_SPECS: tuple[PlatformSpec, ...] = (
    PlatformSpec("website", "Website", "website_product_images.csv", BannerPolicy.DROP),
    PlatformSpec("alfa", "Alfamall", "alfamall_product_images.csv", BannerPolicy.KEEP),
    PlatformSpec("digi", "Digimall", "digimall_product_images.csv", BannerPolicy.KEEP),
)

PLATFORM_KEYS: tuple[str, ...] = tuple(spec.key for spec in DEFAULT_SPECS)

_NAME_WIDTH = max(len(spec.display_name) for spec in DEFAULT_SPECS)


def build_platform_specs(config: PipelineConfig) -> tuple[PlatformSpec, ...]:
    """The platform table with the owner's Website banner override applied.

    Website dropping the banner is a rule of the platform, but the owner asked for it
    to stay switchable (2026-10-13), so it is the one policy that config can move.
    """
    if not config.website_keep_banner:
        return DEFAULT_SPECS
    return tuple(
        replace(spec, banner_policy=BannerPolicy.KEEP) if spec.key == "website" else spec
        for spec in DEFAULT_SPECS
    )


def parse_platform_keys(raw: str) -> tuple[str, ...]:
    """``"website, alfa"`` -> ``("website", "alfa")``, de-duplicated, order preserved.

    An unknown key is an error rather than a silent no-op: ``--platforms dgii`` must
    not quietly publish every platform.  The display names are accepted as aliases so
    ``--platforms digimall`` does what it looks like.
    """
    keys: list[str] = []
    for part in raw.split(","):
        word = part.strip().lower()
        if not word:
            continue
        key = word if word in PLATFORM_KEYS else _alias_key(word)
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _alias_key(word: str) -> str:
    match = next((s.key for s in DEFAULT_SPECS if s.display_name.lower() == word), None)
    if match is None:
        raise ConfigError(
            f"unknown platform {word!r} - expected one of: {', '.join(PLATFORM_KEYS)}"
        )
    return match


def resolve_asset(directory: Path, stem: str) -> Path | None:
    """The single file an asset slot resolves to, or ``None`` when it is absent."""
    for extension in ASSET_EXTENSIONS:
        candidate = directory / f"{stem}{extension}"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class ResolvedPlatform:
    """A spec paired with the assets that exist on disk for this run."""

    spec: PlatformSpec
    template_path: Path
    banner_path: Path | None
    banner_mode: BannerMode

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def display_name(self) -> str:
        return self.spec.display_name

    @property
    def csv_filename(self) -> str:
        return self.spec.csv_filename

    @property
    def appends_banner(self) -> bool:
        """True when this run writes and uploads a closing banner for the platform."""
        return self.banner_mode is BannerMode.APPEND


@dataclass(frozen=True)
class PlatformPlan:
    """What this run publishes, decided once at startup and never revisited."""

    platforms: tuple[ResolvedPlatform, ...]
    #: One line per platform in the table - including the ones not running - so a
    #: skipped platform is never silent.
    notices: tuple[str, ...]

    def by_key(self, key: str) -> ResolvedPlatform | None:
        return next((p for p in self.platforms if p.key == key), None)


def resolve_platforms(
    config: PipelineConfig, requested: Sequence[str] | None = None
) -> PlatformPlan:
    """Decide which platforms publish and what each does with its banner.

    A missing *template* drops that platform with a notice instead of killing the run:
    Website ships before its artwork exists, and a missing DigiMall template must not
    stop Alfamall publishing.  A missing *banner* on a KEEP platform drops the banner
    for the whole run and no product fails - today ``main.check_assets`` aborts the
    entire run for it, which is not what the owner expects.

    Resolving once here, rather than per product, is what stops 36 workers from
    printing the same sentence 1800 times.
    """
    wanted = (
        set(parse_platform_keys(config.enabled_platforms))
        if requested is None
        else set(requested)
    )
    platforms: list[ResolvedPlatform] = []
    notices: list[str] = []
    for spec in build_platform_specs(config):
        asset_dir = config.assets_dir / spec.key
        if spec.key not in wanted:
            notices.append(_notice(spec.display_name, "not selected for this run"))
            continue
        template = resolve_asset(asset_dir, TEMPLATE_STEM)
        if template is None:
            notices.append(
                _notice(
                    spec.display_name,
                    f"SKIPPED (template not found: {_missing(asset_dir, TEMPLATE_STEM)})",
                )
            )
            continue
        if spec.banner_policy is BannerPolicy.DROP:
            # Not even looked for: ``banner_path`` is set exactly when the banner will be
            # written and uploaded, so no caller can pick it up by accident.
            platforms.append(
                ResolvedPlatform(spec, template, None, BannerMode.SKIP_BY_POLICY)
            )
            notices.append(_notice(spec.display_name, "enabled, closing banner dropped by rule"))
            continue
        banner = resolve_asset(asset_dir, BANNER_STEM)
        if banner is None:
            platforms.append(
                ResolvedPlatform(spec, template, None, BannerMode.SKIP_MISSING_ASSET)
            )
            notices.append(
                _notice(
                    spec.display_name,
                    "enabled, closing banner SKIPPED for this run "
                    f"(not found: {_missing(asset_dir, BANNER_STEM)})",
                )
            )
            continue
        platforms.append(ResolvedPlatform(spec, template, banner, BannerMode.APPEND))
        notices.append(
            _notice(
                spec.display_name, f"enabled, closing banner appended ({_shown(banner)})"
            )
        )
    return PlatformPlan(tuple(platforms), tuple(notices))


def _notice(display_name: str, message: str) -> str:
    return f"platform: {display_name.ljust(_NAME_WIDTH)} -> {message}"


def _shown(path: Path) -> str:
    """Absolute path for a notice: a relative one hides *which* assets folder lost."""
    return str(path.resolve())


def _missing(asset_dir: Path, stem: str) -> str:
    """The path a notice blames - the first candidate that would have been used."""
    return _shown(asset_dir / f"{stem}{ASSET_EXTENSIONS[0]}")
