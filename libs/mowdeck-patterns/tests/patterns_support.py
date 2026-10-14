"""Sample shapes shared by the tests of mowdeck-patterns (imported, not a conftest)."""

from mowdeck_patterns import Area, MowerSpec

FRONT_BOUNDARY = [(0.0, 0.0), (12.0, 0.0), (12.0, 8.0), (0.0, 8.0)]
"""The simulator's ``front`` area (``mowdeck_control.simulator.default_map``)."""
FLOWERBED = [(9.0, 5.5), (12.0, 5.5), (12.0, 8.0), (9.0, 8.0)]
"""The simulator's ``flowerbed`` no-go zone in the corner of ``front``."""


def front_area() -> Area:
    """``front`` with its flower bed."""
    return Area.of(FRONT_BOUNDARY, no_go_zones=[FLOWERBED])


def l_shape() -> Area:
    """An L-shaped lawn (10 x 10 m with a 6 x 6 m corner missing)."""
    return Area.of([(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)])


def holed() -> Area:
    """A 14 x 10 m lawn with a 4 x 4 m bed in the middle."""
    return Area.of([(0, 0), (14, 0), (14, 10), (0, 10)], holes=[[(5, 3), (9, 3), (9, 7), (5, 7)]])


SHAPES = {"front": front_area, "l_shape": l_shape, "holed": holed}

MOWER = MowerSpec(cutting_width_m=0.4, overlap_m=0.05)
