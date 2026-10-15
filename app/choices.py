"""Answer lists shared by the volunteer form and the coordinator inbox."""

AVAILABILITY = [
    ("weekday_morning", "Weekday mornings"),
    ("weekday_afternoon", "Weekday afternoons"),
    ("weekday_evening", "Weekday evenings"),
    ("weekend_morning", "Weekend mornings"),
    ("weekend_afternoon", "Weekend afternoons"),
]

FREQUENCY = [
    ("once", "Once, to try it"),
    ("monthly", "About monthly"),
    ("weekly", "About weekly"),
]

# What a nonprofit sees of a volunteer after a like. The onboarding consent says exactly
# this and org_inbox returns exactly these fields; tests/test_match.py keeps them equal.
SHARED_WITH_NONPROFIT = (
    "Causematch shares your first name, the causes you picked, your general "
    "availability, how often you'd like to help and your neighborhood with a "
    "nonprofit only after you like their opening. Never your phone, email or "
    "exact location."
)
