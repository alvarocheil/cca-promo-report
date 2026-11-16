"""Brand recognition patterns shared by access checks, discovery and validations."""

import re

from .models import Brand

BRAND_PATTERNS: dict[Brand, re.Pattern[str]] = {
    Brand.SAMSUNG: re.compile(r"\b(samsung|galaxy)\b", re.I),
    Brand.APPLE: re.compile(r"\b(apple|iphone)\b", re.I),
    Brand.HONOR: re.compile(r"\bhonor\b", re.I),
    Brand.MOTOROLA: re.compile(r"\b(motorola|moto\s?g\d*|razr|edge\s?\d+)\b", re.I),
}
