def _strip_fences(text: str) -> str:
    """Remove markdown code fences that models sometimes add despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


# ── Geo lookups ───────────────────────────────────────────────────────────────

CITY_TO_COUNTY: dict[str, str] = {
    "MIAMI":             "Miami-Dade",
    "MIAMI BEACH":       "Miami-Dade",
    "CORAL GABLES":      "Miami-Dade",
    "HIALEAH":           "Miami-Dade",
    "AVENTURA":          "Miami-Dade",
    "DORAL":             "Miami-Dade",
    "SUNNY ISLES":       "Miami-Dade",
    "BAL HARBOUR":       "Miami-Dade",
    "SURFSIDE":          "Miami-Dade",
    "NORTH MIAMI":       "Miami-Dade",
    "NORTH MIAMI BEACH": "Miami-Dade",
    "SWEETWATER":        "Miami-Dade",
    "CUTLER BAY":        "Miami-Dade",
    "PALMETTO BAY":      "Miami-Dade",
    "PINECREST":         "Miami-Dade",
    "SOUTH MIAMI":       "Miami-Dade",
    "KEY BISCAYNE":      "Miami-Dade",
    "HOMESTEAD":         "Miami-Dade",
    "OPA-LOCKA":         "Miami-Dade",
    "FT. LAUDERDALE":    "Broward",
    "MIRAMAR":           "Broward",
    "PEMBROKE PINES":    "Broward",
    "HOLLYWOOD":         "Broward",
    "HALLANDALE":        "Broward",
    "HALLANDALE BEACH":  "Broward",
    "POMPANO BEACH":     "Broward",
    "DEERFIELD BEACH":   "Broward",
    "CORAL SPRINGS":     "Broward",
    "SUNRISE":           "Broward",
    "PLANTATION":        "Broward",
    "DAVIE":             "Broward",
    "WESTON":            "Broward",
    "LAUDERHILL":        "Broward",
    "TAMARAC":           "Broward",
    "MARGATE":           "Broward",
    "COCONUT CREEK":     "Broward",
    "OAKLAND PARK":      "Broward",
    "WILTON MANORS":     "Broward",
    "DANIA BEACH":       "Broward",
    "COOPER CITY":       "Broward",
    "LIGHTHOUSE POINT":  "Broward",
    "WEST PALM BEACH":   "Palm Beach",
    "BOCA RATON":        "Palm Beach",
    "DELRAY BEACH":      "Palm Beach",
    "BOYNTON BEACH":     "Palm Beach",
    "LAKE WORTH":        "Palm Beach",
    "PALM BEACH":        "Palm Beach",
    "PB GARDENS":        "Palm Beach",
    "JUPITER":           "Palm Beach",
    "RIVIERA BEACH":     "Palm Beach",
    "WELLINGTON":        "Palm Beach",
    "TAMPA":             "Hillsborough",
    "ST. PETE":          "Pinellas",
    "CLEARWATER":        "Pinellas",
    "LARGO":             "Pinellas",
    "BRADENTON":         "Manatee",
    "SARASOTA":          "Sarasota",
    "ORLANDO":           "Orange",
    "WINTER PARK":       "Orange",
    "KISSIMMEE":         "Osceola",
    "SANFORD":           "Seminole",
    "JACKSONVILLE":      "Duval",
    "FT. MYERS":         "Lee",
    "CAPE CORAL":        "Lee",
    "NAPLES":            "Collier",
    "GAINESVILLE":       "Alachua",
    "TALLAHASSEE":       "Leon",
    "DAYTONA BEACH":     "Volusia",
    "PORT ST. LUCIE":    "St. Lucie",
    "VERO BEACH":        "Indian River",
    "PENSACOLA":         "Escambia",
}

COUNTY_TO_REGION: dict[str, str] = {
    "Miami-Dade":   "South Florida",
    "Broward":      "South Florida",
    "Palm Beach":   "South Florida",
    "Monroe":       "South Florida",
    "Hillsborough": "Tampa Bay",
    "Pinellas":     "Tampa Bay",
    "Manatee":      "Tampa Bay",
    "Sarasota":     "Tampa Bay",
    "Pasco":        "Tampa Bay",
    "Hernando":     "Tampa Bay",
    "Orange":       "Orlando Metro",
    "Osceola":      "Orlando Metro",
    "Seminole":     "Orlando Metro",
    "Lake":         "Orlando Metro",
    "Volusia":      "Orlando Metro",
}


def get_cities_by_county(county: str) -> list[str]:
    """Return all cities in a given county."""
    return [city for city, c in CITY_TO_COUNTY.items() if c == county]


def get_counties_by_region(region: str) -> list[str]:
    """Return all counties in a given region."""
    return [county for county, r in COUNTY_TO_REGION.items() if r == region]
