"""Service area: Duhai, Ghaziabad (UP). Reports outside this box are rejected."""
AREA_NAME = "Duhai, Ghaziabad"
CENTER = (28.7285, 77.4740)
# south, west, north, east  (roughly 6.5 km square around Duhai)
BOUNDS = (28.700, 77.440, 28.760, 77.510)


def inside(lat, lng):
    s, w, n, e = BOUNDS
    return s <= lat <= n and w <= lng <= e
