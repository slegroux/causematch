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

FREQUENCY = [
    ("once", "Once, to try it"),
    ("monthly", "About monthly"),
    ("weekly", "About weekly"),
]

# What a nonprofit sees of a volunteer after a like. The onboarding consent says exactly
# this and org_inbox returns exactly these fields; tests/test_match.py keeps them equal.
SHARED_WITH_NONPROFIT = (
    "Causematch shares your photo, your first name and last initial (your full last "
    "name once they accept you), your pronouns, short bio and interests, the causes you "
    "picked, your general availability, how often you'd like to help and your "
    "neighborhood with a nonprofit only after you like their opening. Never your "
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
