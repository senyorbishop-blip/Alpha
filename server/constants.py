# D&D Game Constants

# ---------------------------------------------------------------------------
# Grid / distance
# ---------------------------------------------------------------------------

# Physical pixels that represent one 5-ft grid square.
PX_PER_GRID: float = 50.0
# Real-world feet per grid square (D&D standard).
FT_PER_GRID: float = 5.0

# ---------------------------------------------------------------------------
# Player roles
# ---------------------------------------------------------------------------

ROLE_DM: str = "dm"
ROLE_PLAYER: str = "player"
ROLE_VIEWER: str = "viewer"
ROLE_CHAT_BRIDGE: str = "chat_bridge"
ROLES_ALL: frozenset[str] = frozenset({ROLE_DM, ROLE_PLAYER, ROLE_VIEWER})
ROLES_ACTIVE: frozenset[str] = frozenset({ROLE_DM, ROLE_PLAYER})

# ---------------------------------------------------------------------------
# Ambient sound tracks and SFX ids
# ---------------------------------------------------------------------------

VALID_AMBIENT_TRACKS: frozenset[str] = frozenset(
    {"silence", "tavern", "dungeon", "forest", "battle"}
)
VALID_SFX_IDS: frozenset[str] = frozenset(
    {
        "sword_clash", "fireball", "door_creak", "thunder",
        "heal_chime", "trap_click", "crowd_gasp",
    }
)

# ---------------------------------------------------------------------------
# Equipment / inventory slots
# ---------------------------------------------------------------------------

EQUIPMENT_KINDS: frozenset[str] = frozenset({"armor", "shield", "weapon", "gear"})
ARMOR_TYPES: frozenset[str] = frozenset({"light", "medium", "heavy"})
HANDEDNESS_OPTIONS: frozenset[str] = frozenset({"one_handed", "two_handed", "shield"})
EQUIP_SLOTS: frozenset[str] = frozenset({"armor", "shield", "main_hand", "off_hand"})

# ---------------------------------------------------------------------------
# Commerce / shop TTLs (seconds)
# ---------------------------------------------------------------------------

HAGGLE_OFFER_TTL_SECONDS: int = 300
SHOP_ACCESS_TTL_SECONDS: int = 240
SELL_HAGGLE_TTL_SECONDS: int = 300
RESALE_LOCKOUT_SECONDS: int = 300

# ---------------------------------------------------------------------------
# Loot roll mechanics
# ---------------------------------------------------------------------------

LOOT_ROLL_RANGE_FT: float = 30.0
LOOT_ROLL_NEED_GREED_CHOICES: frozenset[str] = frozenset({"need", "greed", "pass"})

# Maximum number of active professions a single player may hold.
PLAYER_MAX_PROFESSIONS: int = 2

# ---------------------------------------------------------------------------
# Skill to Ability Mapping
# ---------------------------------------------------------------------------

# Skill to Ability Mapping
SKILL_ABILITY_MAP = {
    'Acrobatics': 'Dexterity',
    'Animal Handling': 'Wisdom',
    'Arcana': 'Intelligence',
    'Athletics': 'Strength',
    'Deception': 'Charisma',
    'History': 'Intelligence',
    'Insight': 'Wisdom',
    'Intimidation': 'Charisma',
    'Investigation': 'Intelligence',
    'Medicine': 'Wisdom',
    'Nature': 'Intelligence',
    'Perception': 'Wisdom',
    'Performance': 'Charisma',
    'Persuasion': 'Charisma',
    'Religion': 'Intelligence',
    'Sleight of Hand': 'Dexterity',
    'Stealth': 'Dexterity',
    'Survival': 'Wisdom',
}

# Stat Indices
STAT_INDICES = {
    'Strength': 0,
    'Dexterity': 1,
    'Constitution': 2,
    'Intelligence': 3,
    'Wisdom': 4,
    'Charisma': 5,
}

# PDF Field Patterns
PDF_FIELD_PATTERNS = {
    'name': r'Name: (.*)',
    'class': r'Class: (.*)',
    'level': r'Level: (\d+)',
    'race': r'Race: (.*)',
}

# Default Values
DEFAULT_VALUES = {
    'hit_points': 10,
    'level': 1,
    'experience_points': 0,
}

# File Upload Constraints
FILE_UPLOAD_CONSTRAINTS = {
    'max_size_mb': 5,
    'allowed_types': ['image/jpeg', 'image/png', 'application/pdf'],
}
