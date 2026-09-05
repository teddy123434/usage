from __future__ import annotations

import math

import pytest

from panels.panel_scale import PanelViewport, fit_panel_viewport


@pytest.mark.parametrize(
    ("natural_width", "natural_height", "maximum_height", "expected"),
    [
        (380.0, 900.0, 921.0, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 1183.0, 921.0, PanelViewport(380.0, 921.0, 1183.0, True)),
        (380.0, 4000.0, 921.0, PanelViewport(380.0, 921.0, 4000.0, True)),
        (380.0, 900.0, 0, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 900.0, -1, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 900.0, None, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 900.0, True, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 900.0, math.nan, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 900.0, math.inf, PanelViewport(380.0, 900.0, 900.0, False)),
        (380.0, 0, 921.0, PanelViewport(380.0, 0, 0, False)),
        (380.0, True, 921.0, PanelViewport(380.0, True, True, False)),
    ],
)
def test_fit_panel_viewport(
    natural_width: object,
    natural_height: object,
    maximum_height: object,
    expected: PanelViewport,
) -> None:
    assert fit_panel_viewport(natural_width, natural_height, maximum_height) == expected


def test_fit_panel_viewport_preserves_non_finite_natural_values_without_arithmetic() -> None:
    viewport = fit_panel_viewport(math.nan, math.inf, 921.0)

    assert math.isnan(viewport.width)
    assert math.isinf(viewport.height)
    assert math.isinf(viewport.natural_height)
    assert viewport.scrollable is False
