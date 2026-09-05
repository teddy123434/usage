# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lollapalooza <https://github.com/aqua5230>

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class PanelViewport:
    width: float
    height: float
    natural_height: float
    scrollable: bool


def _positive_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value > 0
    )


def fit_panel_viewport(
    natural_width: object, natural_height: object, maximum_height: object
) -> PanelViewport:
    """Cap panel height without scaling its natural width or document content."""
    width = cast(float, natural_width)
    natural = cast(float, natural_height)
    if not _positive_finite_number(natural_height) or not _positive_finite_number(
        maximum_height
    ):
        return PanelViewport(width, natural, natural, False)

    available = float(cast(int | float, maximum_height))
    height = float(cast(int | float, natural_height))
    return PanelViewport(width, min(height, available), height, height > available)
