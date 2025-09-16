"""Helpers for working with Linux Lite release series identifiers.

The upstream project publishes releases in numbered "series" (for example
``6.0`` or ``10.2``).  Release automation often needs to reason about these
identifiers to determine which upgrade steps are still pending.  A previous
implementation relied on plain string comparison which broke once a two digit
major version (``10.x``) was introduced because ``"10"`` compares lower than
``"6"`` lexicographically.  The utilities in this module avoid that pitfall by
parsing and comparing the numeric components of the series identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

_VERSION_RE = re.compile(r"\d+")


class SeriesFormatError(ValueError):
    """Raised when a series string does not contain any numeric information."""


def _extract_numeric_parts(series: str) -> Tuple[int, ...]:
    """Return the numeric components of a series identifier as a tuple.

    Parameters
    ----------
    series:
        The textual series descriptor.  Only the digits matter; everything else
        is ignored which allows identifiers like ``"6.2 Final"`` or
        ``"10.0 (LTS)"``.

    Returns
    -------
    tuple[int, ...]
        A tuple containing the numeric components in order.

    Raises
    ------
    SeriesFormatError
        If the identifier does not contain any decimal digits.
    """

    parts = _VERSION_RE.findall(series)
    if not parts:
        raise SeriesFormatError(
            f"Series identifier '{series}' does not contain a numeric version"
        )
    return tuple(int(part) for part in parts)


def _series_sort_key(series: str) -> Tuple[int, ...]:
    """Key function that orders series by their numeric components."""

    return _extract_numeric_parts(series)


@dataclass(frozen=True)
class Series:
    """Represents a Linux Lite release series.

    The dataclass makes it easy for callers to store metadata alongside the
    raw series label if needed.  Only the ``label`` attribute is used by the
    helper functions in this module.
    """

    label: str

    def version_key(self) -> Tuple[int, ...]:
        """Expose the numeric components so callers can reuse the ordering."""

        return _series_sort_key(self.label)


def sort_series(series_list: Iterable[str]) -> List[str]:
    """Return the provided series identifiers sorted by semantic version order.

    ``sorted`` on its own performs lexicographical ordering which places
    ``"10.0"`` before ``"6.2"``.  Using :func:`_series_sort_key` fixes that by
    comparing the numeric components in order.  The input is defensively copied
    so that callers do not see their sequences mutated.
    """

    series = list(series_list)
    series.sort(key=_series_sort_key)
    return series


def _deduplicate_preserving_order(series: Sequence[str]) -> Iterator[str]:
    """Yield unique series while preserving their first occurrence order."""

    seen: set[str] = set()
    for item in series:
        if item not in seen:
            seen.add(item)
            yield item


def compute_upgrade_path(
    current_series: str,
    available_series: Iterable[str],
    target_series: Optional[str] = None,
    *,
    include_current: bool = False,
) -> List[str]:
    """Return the ordered upgrade path starting after ``current_series``.

    Parameters
    ----------
    current_series:
        The series currently installed on the system.
    available_series:
        All known series identifiers.  They do not need to be sorted and may
        contain duplicates or labels with additional text.
    target_series:
        Optional series at which the upgrade should stop.  When omitted, the
        path includes all series newer than ``current_series``.
    include_current:
        When ``True`` the returned list starts with ``current_series`` (if it is
        present in ``available_series``).  This is useful when the caller wants
        to include the current state in a migration plan.  By default the plan
        only contains newer series.

    Returns
    -------
    list[str]
        A monotonically increasing list of series identifiers.

    Raises
    ------
    SeriesFormatError
        If either the current or target series cannot be parsed.
    ValueError
        If ``target_series`` is older than ``current_series`` or not part of
        ``available_series``.
    """

    current_key = _series_sort_key(current_series)
    target_key: Optional[Tuple[int, ...]] = None
    if target_series is not None:
        target_key = _series_sort_key(target_series)
        if target_key < current_key:
            raise ValueError(
                "Target series must not be older than the current series."
            )

    ordered_candidates = list(_deduplicate_preserving_order(sort_series(available_series)))

    path: List[str] = []
    for candidate in ordered_candidates:
        candidate_key = _series_sort_key(candidate)

        if include_current:
            if candidate_key < current_key:
                continue
        else:
            if candidate_key <= current_key:
                continue

        path.append(candidate)

        if target_key is not None:
            if candidate_key == target_key:
                break
            if candidate_key > target_key:
                raise ValueError(
                    f"Target series '{target_series}' was not found in the available list."
                )

    if target_key is not None:
        if not path or _series_sort_key(path[-1]) != target_key:
            raise ValueError(
                f"Target series '{target_series}' was not found in the available list."
            )

    return path

