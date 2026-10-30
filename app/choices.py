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

# How long a shift lasts, as Opening.duration_minutes and Shift.duration_minutes store it.
SHIFT_LENGTHS = [
    ("60", "1 hour"),
    ("90", "1.5 hours"),
    ("120", "2 hours"),
    ("150", "2.5 hours"),
    ("180", "3 hours"),
    ("240", "4 hours"),
    ("300", "5 hours"),
    ("360", "6 hours"),
    ("480", "8 hours"),
]

# The day grid: 7 days x 3 parts, stored as "mon_morning" ... "sun_evening" (21 slots).
# AVAILABILITY above is the weekday/weekend summary the nonprofit sees: the grid summed up
# into it (app/match.py general_buckets).
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
# Last-minute calls (an org's urgent call, app/data.py post_urgent_call), opt-in
URGENT_WINDOWS = [("today", "Same day only"), ("48h", "Today or tomorrow")]
URGENT_PER_WEEK = [(1, "1"), (2, "2"), (3, "3")]  # at most 3 a week, whatever is picked
HOURS = [
    (h, {0: "Midnight", 12: "Noon"}.get(h, f"{h % 12} {'am' if h < 12 else 'pm'}"))
    for h in range(24)
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

# What a nonprofit sees of a volunteer after a like. The privacy page says exactly
# this and org_inbox returns exactly these fields; tests/test_match.py keeps them equal.
SHARED_WITH_NONPROFIT = (
    "Causematch shares your photo, your first name and last initial (your full last "
    "name once they accept you), your pronouns, short bio and interests, the causes you "
    "picked, your general availability, the languages you speak, how often you'd like "
    "to help and your neighborhood with a nonprofit only after you like their opening. "
    "Never your "
    "phone, email or exact location."
)

# A volunteer's profile (/me/profile): what they like doing, up to MAX_INTERESTS of them
INTERESTS = [
    ("cooking", "Cooking"),
    ("gardening", "Gardening"),
    ("hiking", "Hiking & the outdoors"),
    ("beach", "The beach & the ocean"),
    ("animals", "Animals"),
    ("kids", "Kids & families"),
    ("seniors", "Older adults"),
    ("teaching", "Teaching & tutoring"),
    ("reading", "Reading & books"),
    ("writing", "Writing"),
    ("art", "Art & crafts"),
    ("music", "Music"),
    ("photography", "Photography"),
    ("sports", "Sports & fitness"),
    ("diy", "Building & fixing things"),
    ("tech", "Tech & computers"),
    ("languages", "Languages"),
    ("events", "Organizing events"),
    ("faith", "Faith communities"),
    ("neighbors", "Meeting neighbors"),
]
MAX_INTERESTS = 6
MAX_BIO = 280
MAX_PRONOUNS = 30
PRONOUNS = ["she/her", "he/him", "they/them", "she/they", "he/they"]  # suggestions only
