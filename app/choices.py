"""Answer lists shared by the volunteer form, the opening form and the coordinator inbox."""

# The radius "Any distance" stores. It scales the distance score but gates nothing.
ANY_DISTANCE_KM = 60

AVAILABILITY = [
    ("weekday_morning", "Weekday mornings"),
    ("weekday_afternoon", "Weekday afternoons"),
    ("weekday_evening", "Weekday evenings"),
    ("weekend_morning", "Weekend mornings"),
    ("weekend_afternoon", "Weekend afternoons"),
    ("weekend_evening", "Weekend evenings"),
]

# The day grid: 7 days x 3 parts, stored as "mon_morning" ... "sun_evening" (21 slots).
# AVAILABILITY above is the older weekday/weekend summary: onboarding still asks it, and the
# nonprofit sees it (app/match.py general_buckets).
DAYS = [
    ("mon", "Mon"),
    ("tue", "Tue"),
    ("wed", "Wed"),
    ("thu", "Thu"),
    ("fri", "Fri"),
    ("sat", "Sat"),
    ("sun", "Sun"),
]
PARTS = [("morning", "Morning"), ("afternoon", "Afternoon"), ("evening", "Evening")]
MAX_FREE_DATES = 10
HORIZON = [(7, "This week"), (14, "2 weeks"), (31, "A month"), (0, "Any time")]  # days
COMMITMENT = [
    ("one_time", "One-time"),
    ("recurring", "Recurring"),
    ("either", "Either"),
]

# Kind of work: optional on an opening ("" is not said, which scores neutral) and a
# preference for a volunteer, who can also say "either" / "any".
SETTING = [("indoor", "Indoors"), ("outdoor", "Outdoors")]
EFFORT = [("light", "Light"), ("moderate", "Moderate"), ("active", "Active")]
GROUP = [("solo", "On my own"), ("small", "Small group"), ("big", "Big group")]
LANGUAGES = [
    ("es", "Spanish"),
    ("ko", "Korean"),
    ("tl", "Tagalog"),
    ("hy", "Armenian"),
    ("zh", "Chinese"),
    ("vi", "Vietnamese"),
    ("fa", "Farsi"),
    ("ru", "Russian"),
    ("ja", "Japanese"),
    ("asl", "ASL"),
    ("other", "Another language"),
]

# What filters instead of only ranking ("Only show" vs "Prefer"). "Not for me" causes
# always filter. Distance filters unless the volunteer turns it off, as the radius did.
DEALBREAKERS = [
    ("causes", "Causes I love or am open to"),
    ("distance", "Within my travel time"),
    ("when", "When I'm free"),
    ("work", "The kind of work I picked"),
]
DEFAULT_DEALBREAKERS = ["distance"]

SKILLS = [
    ("physical", "Physical work / lifting"),
    ("car", "A car, happy to drive"),
    ("food_handling", "Cooking or food handling"),
    ("animals", "Experience with animals"),
    ("language", "A language other than English"),
    ("admin", "Admin, data or spreadsheets"),
    ("media", "Photos, video or social media"),
]
MAX_OPENING_SKILLS = 3

# How a volunteer gets to a place (app/match.py MODE_KMH) and how long they'd travel.
MODES = [
    ("car", "Car"),
    ("transit", "Transit"),
    ("bike", "Bike"),
    ("walk", "Walk"),
]
MAX_MINUTES = [  # (minutes, label), 0: any distance
    (15, "15"),
    (30, "30"),
    (45, "45"),
    (60, "60"),
    (0, "Any"),
]
MAX_PLACES = 2

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
