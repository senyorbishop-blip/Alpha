"""
server/handlers/inventory.py — Player inventory and gold management helpers and handlers.
"""
import time
import secrets
import random
import re
import logging

logger = logging.getLogger(__name__)
from server.encumbrance import (
    auto_tag_extradimensional,
    build_encumbrance_payload,
    get_str_size_for_user,
    get_total_carried_weight,
    get_encumbrance_state,
    get_speed_penalty,
    is_inventory_container,
    ENC_NONE,
)
from server.session import (
    Session, User,
    normalize_profile_owner_key,
    get_player_inventory_for_user,
    get_player_gold_for_user,
    set_player_gold_for_user,
    get_party_stash_inventory,
    PARTY_LOOT_LOG_SYNC_LIMIT,
    PARTY_STASH_KEY,
    _user_bucket_key,
    _inventory_owner_key,
    _legacy_inventory_keys,
    build_quick_actions_sync_payload,
)
from server.handlers.common import (
    manager,
    save_campaign_async,
    _safe_int,
    _token_center,
    PX_PER_GRID,
    _broadcast_token_state_sync,
    bump_inventory_revision,
    _send_action_ack,
    build_live_state_debug_summary,
)
from server.item_schema import (
    normalize_item_record,
    normalize_shop_item_row,
    normalize_crafted_result_row,
    to_inventory_entry,
)
from server.item_compendium import merge_compendium_metadata


_LOOT_ROLL_RANGE_FT = 30.0
_LOOT_ROLL_NEED_GREED_CHOICES = {"need", "greed", "pass"}
_LOOT_ROLL_TIMEOUT_SECONDS = 8
_HAGGLE_OFFER_TTL_SECONDS = 300
_SHOP_ACCESS_TTL_SECONDS = 240
_PLAYER_MAX_PROFESSIONS = 2
_SELL_HAGGLE_TTL_SECONDS = 300
_RESALE_LOCKOUT_SECONDS = 300

_DEFAULT_ACCEPTED_TYPES = frozenset(
    {"weapon", "armour", "consumable", "tool", "material", "trinket", "magic", "misc"}
)

# Base sell reference values by item_type (copper units), used when no purchase price is known.
_CATEGORY_SELL_DEFAULTS: dict[str, int] = {
    "weapon":     1500,
    "armour":     2000,
    "armor":      2000,
    "consumable":  500,
    "tool":        300,
    "material":    100,
    "trinket":     200,
    "magic":      3000,
    "misc":         50,
}


_CREATURE_NAME_HINTS = (
    "creature", "pet", "familiar", "companion", "animal", "beast",
    "wolf", "dog", "cat", "horse", "pony", "rat", "spider", "snake",
    "hawk", "owl", "imp", "sprite", "homunculus",
)

_EQUIPMENT_KINDS = {"armor", "shield", "weapon", "gear", "ring", "accessory", "wondrous"}
_ARMOR_TYPES = {"light", "medium", "heavy"}
_HANDEDNESS = {"one_handed", "two_handed", "shield"}
_EQUIP_SLOTS = {"armor", "shield", "main_hand", "off_hand"}
_WEAPON_NAME_HINTS = (
    "sword", "axe", "bow", "crossbow", "dagger", "mace", "maul",
    "hammer", "spear", "javelin", "halberd", "glaive", "rapier",
    "scimitar", "club", "staff", "trident", "whip", "flail",
)
_ARMOR_NAME_HINTS = (
    "armor", "armour", "mail", "leather", "plate", "chain shirt",
    "chainmail", "scale mail", "breastplate", "splint", "hide armor",
)
_CLASS_DEFAULT_TRAINING = {
    "artificer": {"light": True, "medium": True, "heavy": False, "shield": True},
    "barbarian": {"light": True, "medium": True, "heavy": False, "shield": True},
    "bard": {"light": True, "medium": False, "heavy": False, "shield": False},
    "cleric": {"light": True, "medium": True, "heavy": False, "shield": True},
    "druid": {"light": True, "medium": True, "heavy": False, "shield": True},
    "fighter": {"light": True, "medium": True, "heavy": True, "shield": True},
    "monk": {"light": False, "medium": False, "heavy": False, "shield": False},
    "paladin": {"light": True, "medium": True, "heavy": True, "shield": True},
    "ranger": {"light": True, "medium": True, "heavy": False, "shield": True},
    "rogue": {"light": True, "medium": False, "heavy": False, "shield": False},
    "sorcerer": {"light": False, "medium": False, "heavy": False, "shield": False},
    "warlock": {"light": True, "medium": False, "heavy": False, "shield": False},
    "wizard": {"light": False, "medium": False, "heavy": False, "shield": False},
}


def _infer_equipment_kind(entry: dict) -> str:
    explicit = str(entry.get("equipment_kind") or "").strip().lower()
    if explicit in _EQUIPMENT_KINDS:
        return explicit

    item_type = str(entry.get("item_type") or "").strip().lower()
    if item_type in {"armor", "shield", "weapon"}:
        return item_type

    category = str(entry.get("category") or "").strip().lower()
    if category in {"armor", "shield", "weapon"}:
        return category

    tags = str(entry.get("tags") or "").strip().lower()
    if tags in {"armor", "shield", "weapon"}:
        return tags

    name = str(entry.get("name") or "").strip().lower()
    if "shield" in name:
        return "shield"
    if any(hint in name for hint in _WEAPON_NAME_HINTS):
        return "weapon"
    if any(hint in name for hint in _ARMOR_NAME_HINTS):
        return "armor"

    return ""
_EQUIPMENT_META_KEYS = (
    "equipment_kind", "armor_type", "base_ac", "dex_cap", "ac_bonus",
    "handedness", "weapon_properties", "damage_dice", "damage_type",
    "versatile_damage", "strength_requirement", "stealth_disadvantage",
)
_INVENTORY_META_KEYS = (
    "id", "category", "icon", "rarity", "is_magic", "is_identified", "magic_item_id", "item_type",
    "attunement_required", "effect", "unidentified_description",
    "attuned", "charges_current", "charges_max", "recharge_type", "recharge_formula",
    "consumable", "consumed_on_use", "remove_when_empty", "action_type", "activation_type",
    "usage_cost", "range", "target_type", "save_dc", "attack_bonus", "damage_formula",
    "healing_formula", "effect_text", "grants_action", "passive_effects", "granted_spells",
    "granted_ability", "image_key", "itemSpells", "item_spells", "spellsGranted", "spellGrants",
    "item_schema", "source_id", "source_type", "source_revision", "slug", "subtype", "stack_limit", "image_url", "image_path",
    "category_icon_key", "subtype_icon_key",
    "scroll_data", "bonuses", "resistances", "immunities", "senses_modifiers", "movement_modifiers",
    "stat_overrides", "stat_minimums", "equippable", "weapon_type", "ammo_type", "uses_current", "uses_max",
    "material_type", "recipe_tags", "profession_tags", "item_family", "named_item_flag", "legendary_flag",
    "weight_lbs", "extradimensional", "is_container", "own_weight_lbs", "capacity_lbs", "volume_ft3",
    "is_devouring", "bag_contents",
    "item_spell_attack_bonus", "item_spell_save_dc", "item_schema_version",
    "modifiers", "charges", "recharge", "grantedSpells", "grantedActions", "requiresAttunement", "requires_attunement", "requirements",
    "equipped", "equip_slot", *_EQUIPMENT_META_KEYS,
)


_ITEM_ACTION_ACTIVATION_TYPES = {"action", "bonus_action", "reaction", "free", "special"}
_ITEM_RECHARGE_TYPES = {"short_rest", "long_rest", "dawn", "daily", "custom", "none"}


def _normalize_item_runtime_fields(entry: dict, out: dict) -> None:
    for key, minimum, maximum in (
        ("charges_current", 0, 99),
        ("charges_max", 0, 99),
        ("usage_cost", 0, 20),
        ("save_dc", 0, 40),
        ("attack_bonus", -20, 30),
    ):
        normalized = _safe_inventory_int(entry.get(key), minimum, maximum)
        if normalized is not None:
            out[key] = normalized

    for key, limit in (
        ("recharge_type", 24),
        ("recharge_formula", 24),
        ("action_type", 24),
        ("activation_type", 24),
        ("range", 40),
        ("target_type", 40),
        ("damage_formula", 60),
        ("healing_formula", 60),
        ("effect_text", 400),
        ("granted_ability", 80),
        ("image_key", 120),
        ("image_url", 500),
        ("image_path", 500),
        ("category_icon_key", 120),
        ("subtype_icon_key", 120),
    ):
        value = str(entry.get(key) or "").strip()[:limit]
        if value:
            out[key] = value

    for key in ("attuned", "consumable", "consumed_on_use", "remove_when_empty", "grants_action"):
        if key in entry:
            out[key] = bool(entry.get(key))

    for key in ("passive_effects", "granted_spells", "modifiers", "grantedSpells", "grantedActions", "itemSpells", "item_spells", "spellsGranted", "spellGrants"):
        raw = entry.get(key)
        if isinstance(raw, list):
            cleaned: list[dict] = []
            for row in raw[:12]:
                if isinstance(row, dict):
                    cleaned.append({k: row.get(k) for k in list(row.keys())[:12]})
                else:
                    label = str(row or "").strip()[:120]
                    if label:
                        cleaned.append({"type": "note", "value": label})
            if cleaned:
                out[key] = cleaned


def _item_is_attuned(item: dict) -> bool:
    if not bool(item.get("attunement_required") or item.get("requires_attunement") or item.get("requiresAttunement")):
        return True
    return bool(item.get("attuned"))


def _item_requires_attunement(item: dict) -> bool:
    return bool(item.get("attunement_required") or item.get("requires_attunement") or item.get("requiresAttunement"))


def _attuned_item_count(items: list[dict]) -> int:
    return sum(1 for item in list(items or []) if isinstance(item, dict) and _item_requires_attunement(item) and bool(item.get("attuned")))


def _extract_known_healing_formula(name: str, effect: str) -> str:
    mapping = {
        "potion of healing": "2d4+2",
        "potion of greater healing": "4d4+4",
        "potion of superior healing": "8d4+8",
        "potion of supreme healing": "10d4+20",
    }
    lower_name = name.lower()
    if lower_name in mapping:
        return mapping[lower_name]
    matched = re.search(r"(\d+d\d+(?:\s*[+-]\s*\d+)?)\s*hit points", effect, re.IGNORECASE)
    return str(matched.group(1)).replace(" ", "") if matched else ""


def _extract_recharge_formula(effect_text: str) -> tuple[str, str]:
    effect = str(effect_text or "").lower()
    if "regains" not in effect:
        return ("none", "")
    if "at dawn" in effect:
        recharge_type = "dawn"
    elif "long rest" in effect:
        recharge_type = "long_rest"
    elif "once per day" in effect or "once/day" in effect:
        recharge_type = "daily"
    else:
        recharge_type = "none"
    formula_match = re.search(r"regains?\s+([0-9d+\- ]+)\s+charges?", effect, re.IGNORECASE)
    formula = str(formula_match.group(1) or "").replace(" ", "") if formula_match else ""
    return recharge_type, formula


def _build_known_item_runtime(item: dict) -> dict:
    name = str(item.get("name") or "").strip()
    effect = str(item.get("effect") or "").strip()
    lower_name = name.lower()
    item_type = str(item.get("item_type") or item.get("category") or "").strip().lower()
    runtime: dict = {}

    if item_type == "potion" or "potion" in lower_name:
        healing_formula = _extract_known_healing_formula(name, effect)
        runtime.update({
            "consumable": True,
            "consumed_on_use": True,
            "remove_when_empty": True,
            "grants_action": True,
            "action_type": "consumable",
            "activation_type": "action",
            "usage_cost": 1,
            "target_type": "self_or_touch",
            "range": "touch",
            "healing_formula": healing_formula,
            "effect_text": effect or "Use potion effect.",
        })
        if "resistance" in lower_name and "resistance" in effect.lower():
            damage_type = ""
            type_match = re.search(r"resistance\s*\(([^)]+)\)", name, re.IGNORECASE)
            if type_match:
                damage_type = str(type_match.group(1)).strip().lower()
            runtime.setdefault("passive_effects", []).append({
                "type": "resistance",
                "damage_type": damage_type,
                "duration": "1 hour",
                "summary": effect or "Gain damage resistance.",
            })
        return runtime

    if item_type == "scroll" or "scroll" in lower_name:
        runtime.update({
            "consumable": True,
            "consumed_on_use": True,
            "remove_when_empty": True,
            "grants_action": True,
            "action_type": "consumable",
            "activation_type": "action",
            "usage_cost": 1,
            "range": "special",
            "target_type": "special",
            "effect_text": effect or "Use scroll effect.",
        })
        level_match = re.search(r"spell scroll\s*\((\d+)(?:st|nd|rd|th)? level\)", lower_name)
        if level_match:
            runtime["granted_ability"] = f"spell_scroll_level_{level_match.group(1)}"
        return runtime

    if item_type == "wand" or "wand" in lower_name:
        charges_max = 7
        if "wand of secrets" in lower_name:
            charges_max = 3
        recharge_type, recharge_formula = _extract_recharge_formula(effect)
        usage_cost = 1
        if "1–3 charges" in effect or "1-3 charges" in effect:
            usage_cost = 1
        runtime.update({
            "grants_action": True,
            "action_type": "item_power",
            "activation_type": "action",
            "usage_cost": usage_cost,
            "charges_max": charges_max,
            "recharge_type": recharge_type,
            "recharge_formula": recharge_formula,
            "effect_text": effect or "Use wand power.",
            "range": "special",
            "target_type": "spell_target",
        })
        if "save dc" in effect.lower():
            save_match = re.search(r"save dc\s*(\d+)", effect, re.IGNORECASE)
            if save_match:
                runtime["save_dc"] = _safe_int(save_match.group(1), 10, minimum=0, maximum=40)
        return runtime

    if lower_name in {"ring of protection", "cloak of protection"}:
        runtime["passive_effects"] = [
            {"type": "ac_bonus", "value": 1, "summary": "+1 AC while equipped/attuned."},
            {"type": "save_bonus", "value": 1, "summary": "+1 saving throws while equipped/attuned."},
        ]
    elif lower_name == "headband of intellect":
        runtime["passive_effects"] = [{"type": "ability_set", "ability": "int", "value": 19, "summary": "INT becomes 19."}]
    elif lower_name == "amulet of health":
        runtime["passive_effects"] = [{"type": "ability_set", "ability": "con", "value": 19, "summary": "CON becomes 19."}]
    elif lower_name == "goggles of night":
        runtime["passive_effects"] = [{"type": "sense_bonus", "sense": "darkvision", "value": 60, "summary": "Darkvision +60 ft (or gain 60 ft)."}]
    elif lower_name == "boots of speed":
        runtime.update({
            "grants_action": True,
            "action_type": "item_power",
            "activation_type": "bonus_action",
            "usage_cost": 1,
            "effect_text": effect or "Activate boots effect.",
            "passive_effects": [{"type": "speed_multiplier", "value": 2, "summary": "Speed doubled while active."}],
        })
    return runtime


def _build_item_runtime_action(item: dict, item_index: int) -> tuple[dict | None, dict | None]:
    if not isinstance(item, dict):
        return None, None
    merged = dict(item)
    known = _build_known_item_runtime(item)
    for key, value in known.items():
        if key not in merged or merged.get(key) in (None, "", [], {}):
            merged[key] = value

    if "charges_max" in merged and "charges_current" not in merged:
        merged["charges_current"] = _safe_int(merged.get("charges_max"), 0, minimum=0, maximum=99)
    if "recharge_type" in merged:
        recharge_type = str(merged.get("recharge_type") or "").strip().lower()
        if recharge_type not in _ITEM_RECHARGE_TYPES:
            merged["recharge_type"] = "none"
    if merged.get("activation_type"):
        activation = str(merged.get("activation_type") or "").strip().lower()
        if activation not in _ITEM_ACTION_ACTIVATION_TYPES:
            merged["activation_type"] = "action"

    passive_effects = list(merged.get("passive_effects") or [])
    passive_card = None
    if passive_effects and bool(merged.get("equipped")) and _item_is_attuned(merged):
        passive_card = {
            "item_index": item_index,
            "item_id": str(merged.get("id") or merged.get("magic_item_id") or f"item_{item_index}"),
            "item_name": str(merged.get("name") or "Item"),
            "effects": passive_effects,
        }

    grants_action = bool(merged.get("grants_action")) or bool(merged.get("consumable")) or (
        _safe_int(merged.get("charges_max"), 0, minimum=0, maximum=99) > 0
    )
    if not grants_action:
        return None, passive_card

    quantity = _safe_int(merged.get("qty"), 1, minimum=1, maximum=9999)
    charges_max = _safe_int(merged.get("charges_max"), 0, minimum=0, maximum=99)
    charges_current = _safe_int(merged.get("charges_current"), -1, minimum=-1, maximum=99)
    if charges_max <= 0:
        charges_current = -1
    usage_cost = max(1, _safe_int(merged.get("usage_cost"), 1, minimum=1, maximum=20))

    disabled_reason = ""
    if bool(merged.get("attunement_required")) and not _item_is_attuned(merged):
        disabled_reason = "Requires attunement."
    elif bool(merged.get("consumable")) and quantity <= 0:
        disabled_reason = "No quantity left."
    elif charges_max > 0 and charges_current >= 0 and charges_current < usage_cost:
        disabled_reason = "No charges left."

    action_payload = {
        "action_id": f"item::{item_index}::{str(merged.get('id') or merged.get('magic_item_id') or merged.get('name') or 'item')}",
        "source": "item_action",
        "item_index": item_index,
        "item_id": str(merged.get("id") or merged.get("magic_item_id") or ""),
        "item_name": str(merged.get("name") or "Item"),
        "action_name": f"Use {str(merged.get('name') or 'Item')}",
        "action_type": str(merged.get("action_type") or "item_power"),
        "activation_type": str(merged.get("activation_type") or "action"),
        "usage_cost": usage_cost,
        "quantity": quantity,
        "charges_current": charges_current if charges_current >= 0 else None,
        "charges_max": _safe_int(merged.get("charges_max"), 0, minimum=0, maximum=99),
        "consumable": bool(merged.get("consumable")),
        "consumed_on_use": bool(merged.get("consumed_on_use")),
        "remove_when_empty": bool(merged.get("remove_when_empty", True)),
        "recharge_type": str(merged.get("recharge_type") or "none"),
        "recharge_formula": str(merged.get("recharge_formula") or ""),
        "range": str(merged.get("range") or ""),
        "target_type": str(merged.get("target_type") or ""),
        "save_dc": _safe_int(merged.get("save_dc"), 0, minimum=0, maximum=40),
        "attack_bonus": _safe_int(merged.get("attack_bonus"), 0, minimum=-20, maximum=30),
        "damage_formula": str(merged.get("damage_formula") or ""),
        "healing_formula": str(merged.get("healing_formula") or ""),
        "effect_text": str(merged.get("effect_text") or merged.get("effect") or ""),
        "granted_spells": list(merged.get("granted_spells") or []),
        "granted_ability": str(merged.get("granted_ability") or ""),
        "image_key": str(merged.get("image_key") or ""),
        "disabled": bool(disabled_reason),
        "disabled_reason": disabled_reason,
    }
    return action_payload, passive_card


def _safe_inventory_int(raw, minimum: int, maximum: int) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return _safe_int(raw, minimum, minimum=minimum, maximum=maximum)
    except Exception:
        return None


def _normalize_equipment_fields(entry: dict, out: dict) -> None:
    equipment_kind = _infer_equipment_kind(entry)
    if equipment_kind:
        out["equipment_kind"] = equipment_kind

    armor_type = str(entry.get("armor_type") or "").strip().lower()
    if armor_type in _ARMOR_TYPES:
        out["armor_type"] = armor_type

    handedness = str(entry.get("handedness") or "").strip().lower()
    if handedness in _HANDEDNESS:
        out["handedness"] = handedness
    elif equipment_kind == "shield":
        out["handedness"] = "shield"

    equip_slot = str(entry.get("equip_slot") or "").strip().lower()
    if equip_slot in _EQUIP_SLOTS:
        out["equip_slot"] = equip_slot

    if "equipped" in entry:
        out["equipped"] = bool(entry.get("equipped"))

    for key, minimum, maximum in (
        ("base_ac", 0, 99),
        ("dex_cap", -5, 20),
        ("ac_bonus", -20, 20),
        ("strength_requirement", 0, 30),
    ):
        normalized = _safe_inventory_int(entry.get(key), minimum, maximum)
        if normalized is not None:
            out[key] = normalized

    for key, limit in (("damage_dice", 20), ("damage_type", 24), ("versatile_damage", 20)):
        value = str(entry.get(key) or "").strip()[:limit]
        if value:
            out[key] = value

    if "stealth_disadvantage" in entry:
        out["stealth_disadvantage"] = bool(entry.get("stealth_disadvantage"))

    raw_props = entry.get("weapon_properties")
    if isinstance(raw_props, list):
        cleaned_props = []
        for prop in raw_props:
            text = str(prop or "").strip()[:32]
            if text and text not in cleaned_props:
                cleaned_props.append(text)
        if cleaned_props:
            out["weapon_properties"] = cleaned_props


def _looks_like_creature_inventory_entry(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    text_bits = [
        str(entry.get("name") or ""),
        str(entry.get("notes") or ""),
        str(entry.get("category") or ""),
        str(entry.get("item_type") or ""),
    ]
    hay = " ".join(text_bits).strip().lower()
    if not hay:
        return False
    if "cage" in hay or "crate" in hay:
        return True
    return any(hint in hay for hint in _CREATURE_NAME_HINTS)


def _normalize_player_inventory_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    entry = merge_compendium_metadata(entry)
    name = str(entry.get("name") or "").strip()[:80]
    if not name:
        return None
    qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
    notes = str(entry.get("notes") or "").strip()[:160]
    price = str(entry.get("price") or "").strip()[:32]
    source = str(entry.get("source") or "").strip()[:60]
    out = {"name": name, "qty": qty, "notes": notes}
    if price:
        out["price"] = price
    if source:
        out["source"] = source
    entry_id = str(entry.get("id") or "").strip()[:64]
    if entry_id:
        out["id"] = entry_id
    for key, limit in (
        ("category", 40),
        ("icon", 300),
        ("rarity", 32),
        ("magic_item_id", 64),
        ("item_type", 32),
        ("effect", 400),
        ("unidentified_description", 400),
    ):
        value = str(entry.get(key) or "").strip()[:limit]
        if value:
            out[key] = value
    if "is_magic" in entry:
        out["is_magic"] = bool(entry.get("is_magic"))
    if "is_identified" in entry:
        out["is_identified"] = bool(entry.get("is_identified"))
    if "attunement_required" in entry:
        out["attunement_required"] = bool(entry.get("attunement_required"))
    _normalize_item_runtime_fields(entry, out)
    # ── Encumbrance fields ──
    if "weight_lbs" in entry:
        try:
            out["weight_lbs"] = max(0.0, float(entry["weight_lbs"]))
        except Exception:
            pass
    if entry.get("extradimensional") or entry.get("is_container") or entry.get("capacity_lbs") is not None or isinstance(entry.get("bag_contents"), list):
        if entry.get("extradimensional"):
            out["extradimensional"] = True
        if entry.get("is_container"):
            out["is_container"] = True
        for k in ("own_weight_lbs", "capacity_lbs", "volume_ft3"):
            if entry.get(k) is not None:
                try:
                    out[k] = float(entry[k])
                except Exception:
                    pass
        if "is_devouring" in entry:
            out["is_devouring"] = bool(entry["is_devouring"])
        # Preserve bag contents (nested inventory list)
        if isinstance(entry.get("bag_contents"), list):
            cleaned = []
            for sub in entry["bag_contents"]:
                norm = _normalize_player_inventory_entry(sub)
                if norm:
                    cleaned.append(norm)
            out["bag_contents"] = cleaned

    _normalize_equipment_fields(entry, out)

    # Structured canonical schema (backward-compatible additive field).
    canonical = normalize_item_record(
        {**entry, **out, "quantity": out.get("qty", qty)},
        source_type=str(entry.get("source_type") or "inventory"),
        source_id=str(entry.get("source_id") or entry.get("id") or entry.get("magic_item_id") or ""),
    )
    structured = to_inventory_entry(
        canonical,
        notes=out.get("notes", ""),
        source_label=out.get("source", ""),
        price_label=out.get("price", ""),
    )
    out["item_schema"] = canonical
    for key in (
        "slug", "source_type", "source_id", "source_revision", "stack_limit", "image_url", "scroll_data",
        "bonuses", "resistances", "immunities", "senses_modifiers", "movement_modifiers",
        "stat_overrides", "stat_minimums", "equippable", "weapon_type", "ammo_type",
        "uses_current", "uses_max", "material_type", "recipe_tags", "profession_tags",
        "item_family", "named_item_flag", "legendary_flag",
    ):
        value = structured.get(key)
        if value not in ("", None, [], {}):
            out[key] = value
    if not out.get("item_type"):
        out["item_type"] = str(canonical.get("identity", {}).get("subtype") or canonical.get("identity", {}).get("category") or "misc")
    if not out.get("category"):
        out["category"] = str(canonical.get("identity", {}).get("category") or "misc")
    if not out.get("rarity"):
        out["rarity"] = str(canonical.get("identity", {}).get("rarity") or "common")
    if not out.get("image_key"):
        out["image_key"] = str(canonical.get("display", {}).get("image_key") or "")
    return out


def _get_player_inventory_store(session: Session, user: User):
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    owner_key = _inventory_owner_key(session, user)
    mine = list(inventories.get(owner_key, []) or [])
    if not mine:
        for legacy_key in _legacy_inventory_keys(user, str(getattr(user, "id", "") or "")):
            if not legacy_key or legacy_key == owner_key:
                continue
            legacy_items = list(inventories.get(legacy_key, []) or [])
            if not legacy_items:
                continue
            mine = legacy_items
            inventories[owner_key] = mine
            inventories.pop(legacy_key, None)
            break
    mine = [auto_tag_extradimensional(entry) for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    inventories[owner_key] = mine
    session.player_inventories = inventories
    return inventories, owner_key, mine


def _add_item_to_player_inventory(session: Session, user: User, raw_entry: dict, qty: int, *, source_name: str = "", price: str = "") -> tuple[dict, int]:
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    name = str(raw_entry.get("name") or "").strip()[:80]
    if not name:
        return inventories, 0
    qty = _safe_int(qty, 1, minimum=1, maximum=9999)
    notes = str(raw_entry.get("notes") or "").strip()[:160]
    price = str(price or raw_entry.get("price") or "").strip()[:32]
    source = str(source_name or "").strip()[:60]
    target_idx = -1
    for idx, existing in enumerate(mine):
        if not isinstance(existing, dict):
            continue
        if str(existing.get("name") or "").strip().lower() == name.lower() and str(existing.get("notes") or "").strip() == notes and str(existing.get("price") or "").strip() == price:
            target_idx = idx
            break
    if target_idx >= 0:
        mine[target_idx]["qty"] = _safe_int(mine[target_idx].get("qty"), 1, minimum=1, maximum=9999) + qty
        if source:
            mine[target_idx]["source"] = source
        for meta_key in _INVENTORY_META_KEYS:
            if meta_key in raw_entry:
                mine[target_idx][meta_key] = raw_entry.get(meta_key)
    else:
        new_entry = {"name": name, "qty": qty, "notes": notes}
        if price:
            new_entry["price"] = price
        if source:
            new_entry["source"] = source
        for meta_key in _INVENTORY_META_KEYS:
            if meta_key in raw_entry:
                new_entry[meta_key] = raw_entry.get(meta_key)
        # Auto-tag extradimensional containers by name
        new_entry = auto_tag_extradimensional(new_entry)
        mine.append(new_entry)
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    session.player_inventories = inventories
    return inventories, qty


def _send_inventory_action_result(session: Session, user_id: str, message: str):
    return manager.send_to(session.id, user_id, {
        "type": "inventory_action_result",
        "payload": {"message": message}
    })


def _update_encumbrance_cache(session: Session, user_id: str) -> None:
    """Recompute and cache the encumbrance speed penalty for a single user."""
    if not hasattr(session, "_encumbrance_cache"):
        session._encumbrance_cache = {}
    inventory = list(get_player_inventory_for_user(session, user_id) or [])
    gold_units = int(get_player_gold_for_user(session, user_id) or 0)
    strength, size = get_str_size_for_user(session, user_id)
    enc_settings = dict(getattr(session, "encumbrance_settings", {}) or {})
    total_weight = get_total_carried_weight(inventory, gold_units)
    if enc_settings.get("use_encumbrance", True):
        state = get_encumbrance_state(strength, size, total_weight)
        penalty = get_speed_penalty(state)
    else:
        state = ENC_NONE
        penalty = 0
    session._encumbrance_cache[user_id] = {
        "state": state,
        "speed_penalty": penalty,
        "total_weight": total_weight,
        "strength": strength,
        "size": size,
    }
def _inventory_target_user(session: Session, user: User, target_user_id_raw) -> User:
    target_user = user
    if user.role == "dm":
        target_user_id = str(target_user_id_raw or user.id).strip()[:64]
        maybe = (getattr(session, "users", {}) or {}).get(target_user_id)
        if maybe and getattr(maybe, "role", "viewer") in {"dm", "player"}:
            target_user = maybe
    return target_user


async def _broadcast_inventory_state(session: Session):
    # Bumped once per call (not per-recipient) — every call site here represents
    # one real inventory-affecting mutation, never a read-only query or preview.
    bump_inventory_revision(session)
    active_user_ids = set(manager.get_session_connections(session.id).keys())
    for uid in active_user_ids:
        await _send_inventory_state(session, uid)
    await _broadcast_token_state_sync(session)


def _derive_item_actions_and_passives(items: list[dict]) -> tuple[list[dict], list[dict]]:
    actions: list[dict] = []
    passives: list[dict] = []
    for idx, item in enumerate(list(items or [])):
        action_payload, passive_card = _build_item_runtime_action(item, idx)
        if action_payload:
            actions.append(action_payload)
        if passive_card:
            passives.append(passive_card)
    return actions, passives


async def _send_inventory_state(session: Session, user_id: str):
    from server.session import build_player_inventory_payload_for_dm
    user = session.users.get(user_id)
    if not user:
        return
    payload = {
        "party_loot_log": list(getattr(session, "party_loot_log", []) or [])[-PARTY_LOOT_LOG_SYNC_LIMIT:],
        "party_stash": get_party_stash_inventory(session),
        "encumbrance_settings": dict(getattr(session, "encumbrance_settings", {}) or {}),
        "inventory_revision": int(getattr(session, "inventory_revision", 0) or 0),
    }
    if user.role == "dm":
        dm_buckets = build_player_inventory_payload_for_dm(session)
        for bucket in dm_buckets:
            bucket_user_id = str(bucket.get("user_id") or "").strip()
            bucket_user = (getattr(session, "users", {}) or {}).get(bucket_user_id)
            if bucket_user and getattr(bucket_user, "role", "") in {"player", "dm"}:
                bucket["current_ac"] = _calculate_ac_for_user(
                    session,
                    bucket_user,
                    list(bucket.get("items") or []),
                )
        payload["player_inventories"] = dm_buckets
        # DM view: include encumbrance summary for each player
        dm_enc = {}
        for uid, u in (getattr(session, "users", {}) or {}).items():
            if getattr(u, "role", "") != "player":
                continue
            _update_encumbrance_cache(session, uid)
            dm_enc[uid] = dict((getattr(session, "_encumbrance_cache", {}) or {}).get(uid) or {})
        payload["player_encumbrance"] = dm_enc
    elif user.role == "player":
        inv = get_player_inventory_for_user(session, user_id)
        gold = get_player_gold_for_user(session, user_id)
        item_actions, item_passives = _derive_item_actions_and_passives(inv)
        item_spell_cards = _build_item_spell_cards(inv)
        payload["player_inventory"] = inv
        payload["player_gold"] = gold
        payload["item_actions"] = item_actions
        payload["item_passives"] = item_passives
        payload["item_spell_cards"] = item_spell_cards
        payload["quick_actions_revision"] = int(getattr(session, "quick_actions_revision", 0) or 0)
        payload["character_runtime_revision"] = int(getattr(session, "character_runtime_revision", 0) or 0)
        payload["spell_manifest_revision"] = int(getattr(session, "spell_manifest_revision", 0) or 0)
        payload["current_ac"] = _calculate_ac_for_user(session, user, inv)
        # Build encumbrance payload for this player
        _update_encumbrance_cache(session, user_id)
        strength, size = get_str_size_for_user(session, user_id)
        enc_settings = dict(getattr(session, "encumbrance_settings", {}) or {})
        payload["encumbrance"] = build_encumbrance_payload(inv, gold, strength, size, enc_settings)
    else:
        payload["player_inventory"] = []
        payload["player_gold"] = 0
        payload["party_loot_log"] = []
    logger.info("[live_state] player_inventory_sync %s", build_live_state_debug_summary(session, user_id, user.role, payload))
    await manager.send_to(session.id, user_id, {"type": "player_inventory_sync", "payload": payload})
    if user.role == "player":
        await manager.send_to(session.id, user_id, {"type": "quick_actions_sync", "payload": build_quick_actions_sync_payload(session, user_id)})


def _append_party_loot_log(session: Session, entry: dict):
    logs = list(getattr(session, "party_loot_log", []) or [])
    logs.append({
        "id": secrets.token_hex(4),
        "timestamp": time.time(),
        **dict(entry or {}),
    })
    session.party_loot_log = logs[-120:]


async def handle_party_loot_log_fetch(payload: dict, session: Session, user: User):
    """On-demand fetch of loot-log entries older than the synced tail.

    Sync payloads carry only the newest PARTY_LOOT_LOG_SYNC_LIMIT entries;
    ``have`` is how many newest entries the client already holds. Mirrors
    today's sync visibility: only DM and players see the loot log at all.
    """
    if str(getattr(user, "role", "") or "").strip().lower() not in {"dm", "player"}:
        return
    logs = list(getattr(session, "party_loot_log", []) or [])
    have = _safe_int((payload or {}).get("have"), PARTY_LOOT_LOG_SYNC_LIMIT, minimum=0, maximum=500)
    limit = _safe_int((payload or {}).get("limit"), PARTY_LOOT_LOG_SYNC_LIMIT, minimum=1, maximum=120)
    older = logs[:-have] if have else logs
    await manager.send_to(session.id, user.id, {"type": "party_loot_log_response", "payload": {
        "entries": older[-limit:],
        "total": len(logs),
        "have": have,
    }})


def _set_player_inventory_items(session: Session, user: User, items: list[dict]):
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    owner_key = _inventory_owner_key(session, user)
    clean_items = [entry for entry in (_normalize_player_inventory_entry(x) for x in list(items or [])) if entry]
    inventories[owner_key] = clean_items
    session.player_inventories = inventories
    return inventories, clean_items


def _owner_matches_user(owner_id, user: User) -> bool:
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    if owner == str(user.id):
        return True
    owner_key = normalize_profile_owner_key(owner)
    if not owner_key:
        return False
    return owner_key == normalize_profile_owner_key(getattr(user, "name", ""))


def _find_active_player_token_for_user(session: Session, user: User, map_ctx: str):
    for token in (getattr(session, "tokens", {}) or {}).values():
        if bool(getattr(token, "staged", False)):
            continue
        if str(getattr(token, "map_context", "world") or "world") != map_ctx:
            continue
        if _owner_matches_user(getattr(token, "owner_id", ""), user):
            return token
    return None


def _editor_prop_center_px(prop: dict) -> tuple[float, float]:
    x = float(prop.get("x") or 0.0)
    y = float(prop.get("y") or 0.0)
    w_cells = max(1.0, float(prop.get("w") or 1.0))
    h_cells = max(1.0, float(prop.get("h") or 1.0))
    return (x + (w_cells * PX_PER_GRID / 2.0), y + (h_cells * PX_PER_GRID / 2.0))


def _nearby_loot_roll_players(session: Session, map_ctx: str, prop: dict) -> list[tuple[User, object]]:
    center_x, center_y = _editor_prop_center_px(prop)
    max_px = (_LOOT_ROLL_RANGE_FT / 5.0) * PX_PER_GRID
    eligible: list[tuple[User, object]] = []
    for uid, candidate in (getattr(session, "users", {}) or {}).items():
        if getattr(candidate, "role", "") != "player":
            continue
        if not bool(getattr(candidate, "connected", False)):
            continue
        token = _find_active_player_token_for_user(session, candidate, map_ctx)
        if not token:
            continue
        token_x, token_y = _token_center(token)
        if ((token_x - center_x) ** 2 + (token_y - center_y) ** 2) ** 0.5 <= max_px:
            eligible.append((candidate, token))
    return eligible


def _connected_loot_roll_players(session: Session) -> list[tuple[User, object | None]]:
    """Fallback participant list for loot rolls when token proximity cannot be resolved.

    Some sessions do not have active player tokens bound for every connected player,
    but players should still be able to participate in Need/Greed/Pass rolls.
    """
    out: list[tuple[User, object | None]] = []
    for candidate in (getattr(session, "users", {}) or {}).values():
        if getattr(candidate, "role", "") != "player":
            continue
        if not bool(getattr(candidate, "connected", False)):
            continue
        out.append((candidate, None))
    return out


def _init_loot_roll_state(session: Session) -> dict:
    state = getattr(session, "loot_roll_state", None)
    if not isinstance(state, dict):
        state = {}
        session.loot_roll_state = state
    return state


def _cleanup_loot_roll_state(session: Session, roll_id: str):
    state = _init_loot_roll_state(session)
    state.pop(roll_id, None)
    session.loot_roll_state = state


async def _finalize_loot_roll_resolution(session: Session, roll_id: str):
    state = _init_loot_roll_state(session).get(roll_id)
    if not isinstance(state, dict):
        return
    responder_ids = list(state.get("responder_ids") or [])
    choices = dict(state.get("choices") or {})
    if not responder_ids:
        _cleanup_loot_roll_state(session, roll_id)
        return
    for uid in responder_ids:
        if uid not in choices:
            return
    needers = [uid for uid in responder_ids if choices.get(uid, {}).get("choice") == "need"]
    greeders = [uid for uid in responder_ids if choices.get(uid, {}).get("choice") == "greed"]
    pool = needers if needers else greeders
    winner_id = ""
    winner_roll = 0
    winner_choice = "pass"
    if pool:
        ranked = sorted(
            [
                (
                    int(choices.get(uid, {}).get("roll") or 0),
                    str((session.users.get(uid).name if session.users.get(uid) else "") or ""),
                    uid,
                )
                for uid in pool
            ],
            key=lambda row: (row[0], row[1].lower(), row[2]),
            reverse=True,
        )
        winner_roll, _, winner_id = ranked[0]
        winner_choice = str(choices.get(winner_id, {}).get("choice") or "")
    payload = {
        "roll_id": roll_id,
        "resolved": True,
        "winner_user_id": winner_id,
        "winner_name": (session.users.get(winner_id).name if winner_id and session.users.get(winner_id) else ""),
        "winner_roll": winner_roll,
        "winner_choice": winner_choice,
        "choices": [
            {
                "user_id": uid,
                "name": (session.users.get(uid).name if session.users.get(uid) else "Player"),
                "choice": str((choices.get(uid) or {}).get("choice") or "pass"),
                "roll": int((choices.get(uid) or {}).get("roll") or 0),
            }
            for uid in responder_ids
        ],
    }
    await manager.broadcast(session.id, {"type": "chest_loot_roll_resolved", "payload": payload})
    if not winner_id:
        _cleanup_loot_roll_state(session, roll_id)
        return
    winner = (getattr(session, "users", {}) or {}).get(winner_id)
    if not winner:
        _cleanup_loot_roll_state(session, roll_id)
        return
    await _execute_prop_take_item(state.get("take_payload") or {}, session, winner, bypass_need_greed=True)
    _cleanup_loot_roll_state(session, roll_id)


async def _execute_prop_take_item(payload: dict, session: Session, user: User, *, bypass_need_greed: bool = False):
    from server.handlers.map_editor import (
        _get_editor_prop, _send_prop_action_error,
        _broadcast_prop_action_log, _send_prop_action_result,
        _broadcast_editor_props_state,
    )
    if user.role not in {"dm", "player"}:
        return
    map_ctx = str(payload.get("map_context") or "world")[:80]
    prop_id = str(payload.get("prop_id") or "").strip()[:48]
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=999)
    request_qty = _safe_int(payload.get("qty"), 1, minimum=1, maximum=999)
    if not prop_id or item_index < 0:
        return await _send_prop_action_error(session, user, "Invalid chest item selection.")
    props_all, items, prop_idx, prop = _get_editor_prop(session, map_ctx, prop_id)
    if prop_idx < 0 or not prop:
        fallback_ctx, fallback_prop = _find_prop_context_and_data(session, prop_id)
        if fallback_ctx and fallback_prop:
            map_ctx = fallback_ctx
            props_all, items, prop_idx, prop = _get_editor_prop(session, map_ctx, prop_id)
    if prop_idx < 0 or not prop:
        return await _send_prop_action_error(session, user, "That chest is no longer available on this map.")
    if str(prop.get("kind") or "") != "chest":
        return await _send_prop_action_error(session, user, "Only chest props support taking loot right now.")
    if user.role != "dm" and bool(prop.get("hidden")):
        return await _send_prop_action_error(session, user, "That chest is still hidden from players.")
    if user.role == "player" and not bypass_need_greed:
        eligible = _nearby_loot_roll_players(session, map_ctx, prop)
        if len(eligible) <= 1:
            # Fallback for campaigns where not every connected player has a
            # bound/placed token near the prop but group loot rolls are still
            # expected by table flow.
            eligible = _connected_loot_roll_players(session)
        if len(eligible) > 1:
            state = _init_loot_roll_state(session)
            roll_id = secrets.token_hex(6)
            responder_ids = [candidate.id for candidate, _token in eligible]
            state[roll_id] = {
                "roll_id": roll_id,
                "map_context": map_ctx,
                "prop_id": prop_id,
                "item_index": item_index,
                "qty": request_qty,
                "item_name": "",
                "responder_ids": responder_ids,
                "choices": {},
                "created_at": time.time(),
                "deadline": time.time() + _LOOT_ROLL_TIMEOUT_SECONDS,
                "take_payload": {
                    "map_context": map_ctx,
                    "prop_id": prop_id,
                    "item_index": item_index,
                    "qty": request_qty,
                },
            }
            session.loot_roll_state = state
            inventory_preview = list(prop.get("inventory") or [])
            preview = dict(inventory_preview[item_index] or {}) if item_index < len(inventory_preview) else {}
            preview_name = str(preview.get("name") or "Treasure").strip()[:80] or "Treasure"
            state[roll_id]["item_name"] = preview_name
            for candidate, _token in eligible:
                await manager.send_to(session.id, candidate.id, {
                    "type": "chest_loot_roll_prompt",
                    "payload": {
                        "roll_id": roll_id,
                        "prop_id": prop_id,
                        "prop_name": str(prop.get("name") or "Chest").strip()[:60] or "Chest",
                        "item_index": item_index,
                        "item_name": preview_name,
                        "qty": request_qty,
                        "range_ft": int(_LOOT_ROLL_RANGE_FT),
                        "eligible_player_ids": responder_ids,
                        "deadline_at": state[roll_id]["deadline"],
                        "timeout_seconds": _LOOT_ROLL_TIMEOUT_SECONDS,
                    },
                })
            await _send_prop_action_result(session, user.id, f"Loot roll started for {preview_name}. Waiting for Need/Greed/Pass choices.")
            # Schedule server-side auto-pass for non-responders after deadline
            async def _auto_pass_loot_roll(sid: str, rid: str, dl: float):
                import asyncio as _asyncio
                await _asyncio.sleep(max(0.0, dl - time.time()))
                loot_state = _init_loot_roll_state(
                    (getattr(session, '_session_ref', None) or session)
                )
                roll_entry = loot_state.get(rid)
                if not isinstance(roll_entry, dict):
                    return
                responders = list(roll_entry.get("responder_ids") or [])
                choices = dict(roll_entry.get("choices") or {})
                changed = False
                for uid in responders:
                    if uid not in choices:
                        choices[uid] = {"choice": "pass", "roll": 0, "user_name": "", "chosen_at": time.time(), "auto": True}
                        changed = True
                if changed:
                    roll_entry["choices"] = choices
                    loot_state[rid] = roll_entry
                    session.loot_roll_state = loot_state
                    await _finalize_loot_roll_resolution(session, rid)
            import asyncio as _asyncio_mod
            _asyncio_mod.ensure_future(_auto_pass_loot_roll(session.id, roll_id, state[roll_id]["deadline"]))
            return
    inventory = list(prop.get("inventory") or [])
    if item_index >= len(inventory):
        return await _send_prop_action_error(session, user, "That loot slot is no longer available.")
    entry = dict(inventory[item_index] or {})
    entry_name = str(entry.get("name") or "").strip()[:80]
    if not entry_name:
        return await _send_prop_action_error(session, user, "That loot slot is empty.")
    current_qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=999)
    take_qty = min(current_qty, request_qty)
    remaining = current_qty - take_qty
    if remaining > 0:
        entry["qty"] = remaining
        inventory[item_index] = entry
    else:
        inventory.pop(item_index)
    prop["inventory"] = inventory
    items[prop_idx] = prop
    props_all[map_ctx] = items
    session.editor_props = props_all
    from server.handlers.common import _refresh_map_documents
    _refresh_map_documents(session, map_ctx)
    prop_name = str(prop.get("name") or "Chest").strip()[:60] or "Chest"
    gold_units_taken = _gold_units_from_chest_entry(entry, take_qty)
    if gold_units_taken > 0:
        new_gold_total = _add_gold_to_player(session, user, gold_units_taken)
    else:
        _add_item_to_player_inventory(session, user, entry, take_qty, source_name=prop_name)
        _recompute_equipment_effects(session, user)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "take",
        "item_name": "Gold" if gold_units_taken > 0 else entry_name,
        "qty": take_qty,
        "price": _format_gold_units(gold_units_taken) if gold_units_taken > 0 else "",
        "source_name": prop_name,
        "source_kind": "chest",
    })
    await _broadcast_editor_props_state(session)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await manager.send_to(session.id, user.id, {
        "type": "loot_received",
        "payload": {
            "items": [] if gold_units_taken > 0 else [{
                "id": str(entry.get("id") or ""),
                "name": entry_name,
                "qty": take_qty,
                "notes": str(entry.get("notes") or ""),
                "price": str(entry.get("price") or ""),
                "source": prop_name,
                "rarity": str(entry.get("rarity") or ""),
                "category": str(entry.get("category") or ""),
                "item_type": str(entry.get("item_type") or ""),
                "is_magic": bool(entry.get("is_magic")),
                "is_identified": bool(entry.get("is_identified", not bool(entry.get("is_magic")))),
                "unidentified_description": str(entry.get("unidentified_description") or ""),
                "attunement_required": bool(entry.get("attunement_required")),
            }],
            "coins": _coin_breakdown_from_units(gold_units_taken),
            "source": prop_name,
            "source_kind": "chest",
            "event": "chest_take",
            "player_gold_units": new_gold_total if gold_units_taken > 0 else get_player_gold_for_user(session, user.id),
            "player_gold_label": _format_gold_units(new_gold_total if gold_units_taken > 0 else get_player_gold_for_user(session, user.id)),
        },
    })
    if gold_units_taken > 0:
        action_text = f"{user.name} took {_format_gold_units(gold_units_taken)} from {prop_name}."
    else:
        action_text = f"{user.name} took {take_qty}× {entry_name} from {prop_name}." if take_qty != 1 else f"{user.name} took {entry_name} from {prop_name}."
    await _broadcast_prop_action_log(session, action_text)
    if gold_units_taken > 0:
        await _send_prop_action_result(session, user.id, f"Took {_format_gold_units(gold_units_taken)}.")
    else:
        await _send_prop_action_result(session, user.id, f"Took {take_qty}× {entry_name}." if take_qty != 1 else f"Took {entry_name}.")


def _get_party_stash_store(session: Session) -> list[dict]:
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    stash = list(inventories.get(PARTY_STASH_KEY, []) or [])
    stash = [entry for entry in (_normalize_player_inventory_entry(x) for x in stash) if entry]
    inventories[PARTY_STASH_KEY] = stash
    session.player_inventories = inventories
    return stash


def _set_party_stash_items(session: Session, items: list[dict]) -> list[dict]:
    inventories = dict(getattr(session, "player_inventories", {}) or {})
    clean_items = [entry for entry in (_normalize_player_inventory_entry(x) for x in list(items or [])) if entry]
    inventories[PARTY_STASH_KEY] = clean_items
    session.player_inventories = inventories
    return clean_items


def _add_item_to_party_stash(session: Session, raw_entry: dict, qty: int, *, source_name: str = "") -> tuple[list[dict], int]:
    stash = _get_party_stash_store(session)
    name = str(raw_entry.get("name") or "").strip()[:80]
    if not name:
        return stash, 0
    qty = _safe_int(qty, 1, minimum=1, maximum=9999)
    notes = str(raw_entry.get("notes") or "").strip()[:160]
    price = str(raw_entry.get("price") or "").strip()[:32]
    source = str(source_name or raw_entry.get("source") or "").strip()[:60]
    target_idx = -1
    for idx, existing in enumerate(stash):
        if str(existing.get("name") or "").strip().lower() == name.lower() and str(existing.get("notes") or "").strip() == notes and str(existing.get("price") or "").strip() == price:
            target_idx = idx
            break
    if target_idx >= 0:
        stash[target_idx]["qty"] = _safe_int(stash[target_idx].get("qty"), 1, minimum=1, maximum=9999) + qty
        if source:
            stash[target_idx]["source"] = source
        for meta_key in _INVENTORY_META_KEYS:
            if meta_key in raw_entry:
                stash[target_idx][meta_key] = raw_entry.get(meta_key)
    else:
        new_entry = {"name": name, "qty": qty, "notes": notes}
        if price:
            new_entry["price"] = price
        if source:
            new_entry["source"] = source
        for meta_key in _INVENTORY_META_KEYS:
            if meta_key in raw_entry:
                new_entry[meta_key] = raw_entry.get(meta_key)
        stash.append(new_entry)
    return _set_party_stash_items(session, stash), qty


def _remove_item_from_party_stash(session: Session, item_index: int, qty: int) -> tuple[dict | None, int]:
    stash = _get_party_stash_store(session)
    if item_index < 0 or item_index >= len(stash):
        return None, 0
    entry = dict(stash[item_index] or {})
    current_qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
    remove_qty = min(current_qty, _safe_int(qty, 1, minimum=1, maximum=9999))
    remaining = current_qty - remove_qty
    if remaining > 0:
        entry["qty"] = remaining
        stash[item_index] = entry
    else:
        stash.pop(item_index)
    _set_party_stash_items(session, stash)
    entry["qty"] = remove_qty
    return entry, remove_qty


def _remove_item_from_player_inventory(session: Session, user: User, item_index: int, qty: int) -> tuple[dict | None, int]:
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    if item_index < 0 or item_index >= len(mine):
        return None, 0
    entry = dict(mine[item_index] or {})
    current_qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
    remove_qty = min(current_qty, _safe_int(qty, 1, minimum=1, maximum=9999))
    remaining = current_qty - remove_qty
    if remaining > 0:
        entry["qty"] = remaining
        mine[item_index] = entry
    else:
        mine.pop(item_index)
    _set_player_inventory_items(session, user, mine)
    entry["qty"] = remove_qty
    return entry, remove_qty


def _get_connected_transfer_targets(session: Session, user: User) -> list[User]:
    targets = []
    for other in (getattr(session, "users", {}) or {}).values():
        if not other or other.id == user.id:
            continue
        if getattr(other, "role", "viewer") not in {"dm", "player"}:
            continue
        targets.append(other)
    return targets


_GOLD_ITEM_NAMES = {"gold", "gp", "gold piece", "gold pieces"}
_CURRENCY_ONLY_PATTERN = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(pp|gp|sp|cp|gold|silver|copper|platinum)\s*$",
    re.IGNORECASE,
)


def _is_gold_item_name(name: str) -> bool:
    return str(name or "").strip().lower() in _GOLD_ITEM_NAMES


def _format_gold_units(units: int) -> str:
    total = max(0, int(round(float(units or 0))))
    gp = total // 100
    rem = total % 100
    sp = rem // 10
    cp = rem % 10
    parts = []
    if gp or not parts:
        parts.append(f"{gp} gp")
    if sp:
        parts.append(f"{sp} sp")
    if cp:
        parts.append(f"{cp} cp")
    return " ".join(parts)


def _parse_gold_to_units(raw) -> int | None:
    text = str(raw or "").strip().lower()
    if not text:
        return 0
    text = text.replace(',', '')
    matches = list(re.finditer(r'([0-9]+(?:\.[0-9]+)?)\s*(pp|gp|sp|cp|gold|silver|copper|platinum)?', text))
    total = 0
    found = False
    for m in matches:
        num_txt = str(m.group(1) or '').strip()
        if not num_txt:
            continue
        unit = str(m.group(2) or 'gp').strip().lower()
        try:
            value = float(num_txt)
        except Exception:
            continue
        found = True
        if unit in {'pp', 'platinum'}:
            total += int(round(value * 1000))
        elif unit in {'gp', 'gold'}:
            total += int(round(value * 100))
        elif unit in {'sp', 'silver'}:
            total += int(round(value * 10))
        elif unit in {'cp', 'copper'}:
            total += int(round(value))
        else:
            total += int(round(value * 100))
    if found:
        return max(0, min(999999999, total))
    return None


def _coin_breakdown_from_units(units: int) -> dict[str, int]:
    total = max(0, int(units or 0))
    gp = total // 100
    rem = total % 100
    sp = rem // 10
    cp = rem % 10
    out: dict[str, int] = {}
    if gp > 0:
        out["gp"] = gp
    if sp > 0:
        out["sp"] = sp
    if cp > 0:
        out["cp"] = cp
    return out


def _gold_units_from_chest_entry(entry: dict, qty_taken: int) -> int:
    """Return coin units represented by a chest item entry, if it is currency."""
    if not isinstance(entry, dict):
        return 0
    qty_taken = max(0, int(qty_taken or 0))
    if qty_taken <= 0:
        return 0
    name = str(entry.get("name") or "").strip()
    lowered_name = name.lower()
    item_type = str(entry.get("item_type") or "").strip().lower()
    category = str(entry.get("category") or "").strip().lower()
    price = str(entry.get("price") or "").strip()

    # Most common chest currency shape: "37 gp"
    if _CURRENCY_ONLY_PATTERN.fullmatch(name):
        parsed = _parse_gold_to_units(name)
        return max(0, int(parsed or 0)) * qty_taken

    # Explicit "Gold"/"GP" items can optionally carry currency value in price.
    if _is_gold_item_name(lowered_name):
        per_item_units = _parse_gold_to_units(price)
        if per_item_units is None:
            per_item_units = 100  # 1 gp fallback for plain "Gold" entries
        return max(0, int(per_item_units or 0)) * qty_taken

    # Currency-typed entries can encode value in either name or price.
    if item_type in {"currency", "coin", "coins"} or category in {"currency", "coin", "coins"}:
        parsed = _parse_gold_to_units(name)
        if parsed is None:
            parsed = _parse_gold_to_units(price)
        return max(0, int(parsed or 0)) * qty_taken

    return 0


def _add_gold_to_player(session: Session, user: User, amount_units: int) -> int:
    current = get_player_gold_for_user(session, user.id)
    return set_player_gold_for_user(session, user.id, current + max(0, int(amount_units or 0)))


def _try_spend_gold(session: Session, user: User, amount_units: int) -> tuple[bool, int, int]:
    needed = max(0, int(amount_units or 0))
    current = get_player_gold_for_user(session, user.id)
    if needed <= 0:
        return True, current, current
    if current < needed:
        return False, current, current
    new_total = set_player_gold_for_user(session, user.id, current - needed)
    return True, current, new_total


def _charbook_dex_score(char_book: dict) -> int:
    if not isinstance(char_book, dict):
        return 10
    candidates = []
    abilities = char_book.get("abilities")
    if isinstance(abilities, dict):
        dex_block = abilities.get("dex")
        if isinstance(dex_block, dict):
            candidates.extend([dex_block.get("score"), dex_block.get("value"), dex_block.get("base")])
        candidates.append(abilities.get("dex"))
    candidates.extend([
        char_book.get("dex"),
        char_book.get("dexterity"),
        char_book.get("ability_dex"),
        char_book.get("abilityDex"),
    ])
    for raw in candidates:
        try:
            value = int(raw)
            return max(1, min(30, value))
        except Exception:
            continue
    return 10


def _dex_mod_for_user(session: Session, user: User) -> int:
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    owner_key = normalize_profile_owner_key(getattr(user, "name", "")) or user.id
    mine = list(profiles.get(owner_key, []) or [])
    if not mine and user.id in profiles:
        mine = list(profiles.get(user.id, []) or [])
    if not mine:
        return 0
    latest = max(mine, key=lambda p: float((p or {}).get("updated_at") or 0.0))
    dex_score = _charbook_dex_score(dict(latest or {}).get("charBook") or {})
    return (dex_score - 10) // 2


def _profile_ac_for_user(session: Session, user: User) -> int | None:
    """Return the latest profile AC override when present/valid.

    Prefer resolved native AC first so DM inventory and equipment math do not
    fall back to stale legacy sheet values. This protects players who use the
    current native/runtime sheet as their baseline when they do not currently
    have equipped armor metadata in inventory.
    """
    profile = _latest_profile_for_user(session, user)
    if not isinstance(profile, dict):
        return None
    if bool(profile.get("ac_from_equipment")):
        return None
    native_runtime = dict(profile.get("nativeRuntime") or {})
    native_character = dict(profile.get("nativeCharacter") or {})
    native_runtime_from_doc = dict(native_character.get("runtime") or {})
    candidates = [
        native_runtime.get("ac"),
        native_runtime_from_doc.get("ac"),
        profile.get("ac"),
        (dict(profile.get("charSheet") or {})).get("ac"),
        (dict(profile.get("charBook") or {})).get("ac"),
    ]
    for raw in candidates:
        try:
            value = int(raw)
        except Exception:
            continue
        return max(1, min(99, value))
    return None


def _latest_profile_for_user(session: Session, user: User) -> dict:
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    owner_key = normalize_profile_owner_key(getattr(user, "name", "")) or user.id
    mine = list(profiles.get(owner_key, []) or [])
    if not mine and user.id in profiles:
        mine = list(profiles.get(user.id, []) or [])
    if not mine:
        return {}
    return dict(max(mine, key=lambda p: float((p or {}).get("updated_at") or 0.0)) or {})


def _text_implies_training(text: str, target: str) -> bool:
    normalized = f" {str(text or '').strip().lower()} "
    if not normalized.strip():
        return False
    if target == "shield":
        return " shield " in normalized or " shields " in normalized
    if "all armor" in normalized or "all armour" in normalized:
        return True
    if target == "light":
        return "light armor" in normalized or "light armour" in normalized
    if target == "medium":
        return "medium armor" in normalized or "medium armour" in normalized
    if target == "heavy":
        return "heavy armor" in normalized or "heavy armour" in normalized
    return False


def _extract_class_training_defaults(profile: dict) -> dict[str, bool]:
    defaults = {"light": False, "medium": False, "heavy": False, "shield": False}
    if not isinstance(profile, dict):
        return defaults
    char_book = dict(profile.get("charBook") or {})
    char_sheet = dict(profile.get("charSheet") or {})
    classes = []
    for source in (char_book, char_sheet):
        raw_classes = source.get("classes")
        if isinstance(raw_classes, list):
            classes.extend(raw_classes)
        class_name = str(source.get("className") or "").strip()
        if class_name:
            classes.extend([{"name": part.strip()} for part in class_name.split("/") if part.strip()])
    for cls in classes:
        if isinstance(cls, dict):
            cls_name = str(cls.get("name") or "").strip().lower()
        else:
            cls_name = str(cls or "").strip().lower()
        if not cls_name:
            continue
        base = _CLASS_DEFAULT_TRAINING.get(cls_name)
        if not base:
            continue
        defaults["light"] = defaults["light"] or bool(base.get("light"))
        defaults["medium"] = defaults["medium"] or bool(base.get("medium"))
        defaults["heavy"] = defaults["heavy"] or bool(base.get("heavy"))
        defaults["shield"] = defaults["shield"] or bool(base.get("shield"))
    return defaults


def _has_explicit_training_context(profile: dict) -> bool:
    """Return True when a profile contains enough class/proficiency data to enforce training gates."""
    if not isinstance(profile, dict):
        return False

    char_book = dict(profile.get("charBook") or {})
    char_sheet = dict(profile.get("charSheet") or {})
    for source in (char_book, char_sheet):
        if not isinstance(source, dict):
            continue
        raw_classes = source.get("classes")
        if isinstance(raw_classes, list):
            for cls in raw_classes:
                cls_name = str((cls or {}).get("name") if isinstance(cls, dict) else cls or "").strip().lower()
                if cls_name in _CLASS_DEFAULT_TRAINING:
                    return True
        class_name = str(source.get("className") or "").strip().lower()
        if class_name:
            for part in class_name.split("/"):
                if part.strip().lower() in _CLASS_DEFAULT_TRAINING:
                    return True

        if isinstance(source.get("armor_training"), dict):
            return True
        for field in ("armor_training_light", "armor_training_medium", "armor_training_heavy", "shield_training"):
            if field in source:
                return True
        prof_text = str(source.get("proficiencies") or "")[:4000]
        if any(_text_implies_training(prof_text, target) for target in ("light", "medium", "heavy", "shield")):
            return True
    return False


def _resolve_equipment_training(session: Session, user: User) -> dict[str, bool]:
    profile = _latest_profile_for_user(session, user)
    training = _extract_class_training_defaults(profile)
    char_book = dict(profile.get("charBook") or {})
    char_sheet = dict(profile.get("charSheet") or {})
    for source in (char_book, char_sheet):
        if not isinstance(source, dict):
            continue
        armor_training = source.get("armor_training")
        if isinstance(armor_training, dict):
            for key, target in (("light", "light"), ("medium", "medium"), ("heavy", "heavy"), ("shield", "shield")):
                if key in armor_training:
                    training[target] = bool(armor_training.get(key))
        for field, target in (
            ("armor_training_light", "light"),
            ("armor_training_medium", "medium"),
            ("armor_training_heavy", "heavy"),
            ("shield_training", "shield"),
        ):
            if field in source:
                training[target] = bool(source.get(field))
        prof_text = str(source.get("proficiencies") or "")[:4000]
        for target in ("light", "medium", "heavy", "shield"):
            if _text_implies_training(prof_text, target):
                training[target] = True
    return training


def _equipment_training_error(item: dict, training: dict[str, bool]) -> str | None:
    equipment_kind = str(item.get("equipment_kind") or "").strip().lower()
    armor_type = str(item.get("armor_type") or "").strip().lower()
    item_name = str(item.get("name") or "item").strip() or "item"
    if equipment_kind == "shield":
        if not bool(training.get("shield")):
            return "You are not trained to use shields."
        return None
    if equipment_kind != "armor":
        return None
    required = "light"
    if armor_type in {"light", "medium", "heavy"}:
        required = armor_type
    if bool(training.get(required)):
        return None
    if required == "heavy":
        if item_name.lower() == "chain mail":
            return "Wizards cannot equip Chain Mail unless they gain Heavy Armor training."
        return "You are not trained in Heavy Armor."
    if required == "medium":
        return "You are not trained in Medium Armor."
    return "You are not trained in Light Armor."


def _iter_equipped_items(items: list[dict], equipment_kind: str) -> list[tuple[int, dict]]:
    out = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if not bool(item.get("equipped")):
            continue
        if str(item.get("equipment_kind") or "").strip().lower() != equipment_kind:
            continue
        out.append((idx, item))
    return out


def _calculate_ac_for_user(session: Session, user: User, items: list[dict]) -> int:
    def _owned_token_ac_baseline() -> int | None:
        for token in (getattr(session, "tokens", {}) or {}).values():
            if not _owner_matches_user(getattr(token, "owner_id", ""), user):
                continue
            try:
                value = int(getattr(token, "ac", None))
            except Exception:
                continue
            return max(1, min(99, value))
        return None

    dex_mod = _dex_mod_for_user(session, user)
    armor_entries = _iter_equipped_items(items, "armor")
    shield_entries = _iter_equipped_items(items, "shield")
    armor = armor_entries[0][1] if armor_entries else None

    ac = _profile_ac_for_user(session, user)
    if ac is None:
        ac = _owned_token_ac_baseline()
    if ac is None:
        ac = 10 + dex_mod
    if armor:
        base_ac = _safe_inventory_int(armor.get("base_ac"), 0, 99)
        if base_ac is not None:
            armor_type = str(armor.get("armor_type") or "").strip().lower()
            if armor_type == "heavy":
                ac = base_ac
            elif armor_type == "medium":
                dex_cap = armor.get("dex_cap")
                try:
                    dex_cap_value = int(dex_cap)
                except Exception:
                    dex_cap_value = 2
                ac = base_ac + min(dex_mod, dex_cap_value)
            else:
                ac = base_ac + dex_mod
        ac += _safe_int(armor.get("ac_bonus"), 0, minimum=-20, maximum=20)

    for _idx, shield in shield_entries:
        ac += _safe_int(shield.get("ac_bonus"), 0, minimum=-20, maximum=20)

    _item_actions, passives = _derive_item_actions_and_passives(items)
    for passive in passives:
        for effect in list(passive.get("effects") or []):
            if str(effect.get("type") or "").strip().lower() != "ac_bonus":
                continue
            ac += _safe_int(effect.get("value"), 0, minimum=-20, maximum=20)

    return max(1, int(ac))


def _apply_ac_to_char_profiles(session: Session, user: User, ac_value: int, items: list) -> None:
    """Update AC on character profiles owned by *user*.

    Only overwrites a profile's AC when equipment is authoritative:
    - The player has at least one piece of equipped armor or shield, OR
    - The profile's AC was previously set by equipment (``ac_from_equipment``
      is True — handles the case where a player removes all armor), OR
    - The profile has no AC set yet (``profile["ac"]`` is None).

    This prevents equipment recalculation from silently overwriting a manually
    set AC (e.g. the player typed a value in the character sheet form).
    """
    _actions, passives = _derive_item_actions_and_passives(items)
    has_passive_ac = any(
        str(effect.get("type") or "").strip().lower() == "ac_bonus"
        for passive in passives
        for effect in list(passive.get("effects") or [])
    )
    has_armor = bool(
        _iter_equipped_items(items, "armor") or _iter_equipped_items(items, "shield") or has_passive_ac
    )
    profiles = dict(getattr(session, "char_profiles", {}) or {})
    owner_key = normalize_profile_owner_key(getattr(user, "name", "")) or user.id
    mine = list(profiles.get(owner_key, []) or [])
    if not mine and user.id in profiles:
        mine = list(profiles.get(user.id, []) or [])
        if mine:
            profiles[owner_key] = mine
            profiles.pop(user.id, None)
    changed = False
    for profile in mine:
        if not isinstance(profile, dict):
            continue
        prev_from_equip = bool(profile.get("ac_from_equipment"))
        if has_armor or prev_from_equip or profile.get("ac") is None:
            profile["ac"] = ac_value
            profile["ac_from_equipment"] = has_armor
            sheet = dict(profile.get("charSheet") or {})
            if sheet.get("ac") != ac_value:
                sheet["ac"] = ac_value
                profile["charSheet"] = sheet
            changed = True
    if changed:
        profiles[owner_key] = mine
        session.char_profiles = profiles


def _apply_ac_to_owned_tokens(session: Session, user: User, items: list, ac_value: int) -> None:
    """Update AC on tokens owned by *user*.

    Only overwrites a token's AC when equipment is authoritative for that token:
    - The player has at least one piece of equipped armor or shield, OR
    - The token's AC was previously calculated from equipment (``ac_from_equipment``
      is True — handles the case where a player removes all armor), OR
    - The token has no AC set yet (``token.ac is None``).

    This prevents equipment recalculation from silently overwriting a manually
    set AC (e.g. the player typed a value in the quick panel form).
    """
    _actions, passives = _derive_item_actions_and_passives(items)
    has_passive_ac = any(
        str(effect.get("type") or "").strip().lower() == "ac_bonus"
        for passive in passives
        for effect in list(passive.get("effects") or [])
    )
    has_armor = bool(
        _iter_equipped_items(items, "armor") or _iter_equipped_items(items, "shield") or has_passive_ac
    )
    for token in (getattr(session, "tokens", {}) or {}).values():
        if _owner_matches_user(getattr(token, "owner_id", ""), user):
            # Allow equipment to override only when it is (or was) the authority.
            if has_armor or getattr(token, "ac_from_equipment", False) or token.ac is None:
                token.ac = ac_value
                token.ac_from_equipment = has_armor


def _recompute_equipment_effects(session: Session, user: User) -> int:
    items = get_player_inventory_for_user(session, user.id)
    ac_value = _calculate_ac_for_user(session, user, items)
    _apply_ac_to_char_profiles(session, user, ac_value, items)
    _apply_ac_to_owned_tokens(session, user, items, ac_value)
    return ac_value


def _item_charges_current_max(item: dict, fallback_max: int) -> tuple[int, int]:
    """Read charge state, accepting legacy uses_current/uses_max and nested charges.{current,max}."""
    nested = item.get("charges") if isinstance(item.get("charges"), dict) else {}
    raw_max = item.get("charges_max", item.get("uses_max", nested.get("max")))
    raw_current = item.get("charges_current", item.get("uses_current", nested.get("current")))
    charges_max = _safe_int(raw_max, fallback_max, minimum=0, maximum=99)
    charges_current = _safe_int(raw_current, charges_max, minimum=0, maximum=99)
    return charges_current, charges_max


# Recharge types that recover by default on a long rest even without an explicit
# rest-aware time-of-day system (no in-game clock to trigger "dawn" separately).
_LONG_REST_FALLBACK_RECHARGE_TYPES = {"dawn", "daily", "custom"}


def refresh_item_charges_for_rest(session: Session, user: User, rest_type: str) -> list[dict]:
    inventories, owner_key, items = _get_player_inventory_store(session, user)
    changed: list[dict] = []
    changed_any = False
    rest_key = str(rest_type or "").strip().lower()
    for idx, raw_item in enumerate(list(items or [])):
        item = dict(raw_item or {})
        action_payload, _passive = _build_item_runtime_action(item, idx)
        if not action_payload:
            continue
        recharge_type = str(action_payload.get("recharge_type") or "none").strip().lower()
        if recharge_type not in _ITEM_RECHARGE_TYPES or recharge_type == "none":
            continue
        if recharge_type == "short_rest" and rest_key != "short":
            continue
        if recharge_type == "long_rest" and rest_key != "long":
            continue
        if recharge_type in _LONG_REST_FALLBACK_RECHARGE_TYPES and rest_key not in {"long", "dawn"}:
            continue
        fallback_max = _safe_int(action_payload.get("charges_max"), 0, minimum=0, maximum=99)
        current, charges_max = _item_charges_current_max(item, fallback_max)
        if charges_max <= 0:
            continue
        formula = str(action_payload.get("recharge_formula") or "").strip()
        if formula:
            gained = _roll_formula_total(formula)
            next_charges = min(charges_max, current + max(0, gained))
        else:
            next_charges = charges_max
        if next_charges == current:
            continue
        item["charges_max"] = charges_max
        item["charges_current"] = next_charges
        items[idx] = item
        changed_any = True
        changed.append({"item_index": idx, "item_name": str(item.get("name") or "Item"), "charges_current": next_charges, "charges_max": charges_max})
    if changed_any:
        inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in items) if entry]
        session.player_inventories = inventories
    return changed


def _equip_item(session: Session, user: User, item_index: int) -> tuple[bool, str]:
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    if item_index < 0 or item_index >= len(mine):
        return False, "That inventory item is no longer available."
    item = dict(mine[item_index] or {})
    equipment_kind = str(item.get("equipment_kind") or "").strip().lower()
    if equipment_kind not in _EQUIPMENT_KINDS or equipment_kind == "gear":
        return False, "That item cannot be equipped."
    if bool(item.get("equipped")):
        return True, f"{item.get('name') or 'Item'} is already equipped."
    profile = _latest_profile_for_user(session, user)
    if _has_explicit_training_context(profile):
        equip_error = _equipment_training_error(item, _resolve_equipment_training(session, user))
        if equip_error:
            return False, equip_error

    equipped_weapons = [
        (idx, it)
        for idx, it in _iter_equipped_items(mine, "weapon")
        if idx != item_index
    ]
    has_two_handed = any(str(it.get("handedness") or "").strip().lower() == "two_handed" for _, it in equipped_weapons)
    shield_equipped = bool(_iter_equipped_items(mine, "shield"))

    if equipment_kind == "armor":
        if _iter_equipped_items(mine, "armor"):
            return False, "Unequip your current armor first."
        item["equipped"] = True
        item["equip_slot"] = "armor"
    elif equipment_kind == "shield":
        if shield_equipped:
            return False, "You already have a shield equipped."
        if has_two_handed:
            return False, "Cannot equip a shield while wielding a two-handed weapon."
        item["equipped"] = True
        item["equip_slot"] = "shield"
    elif equipment_kind in ("ring", "accessory", "wondrous"):
        # Allow up to 2 rings (finger_1 / finger_2); accessories go to neck or wrist slots with no hard cap.
        ring_slots = [str(it.get("equip_slot") or "").strip().lower() for _, it in _iter_equipped_items(mine, equipment_kind)]
        if equipment_kind == "ring" and len(ring_slots) >= 2:
            return False, "You can only wear 2 rings at a time."
        slot = "finger_1" if (equipment_kind == "ring" and "finger_1" not in ring_slots) else (
            "finger_2" if equipment_kind == "ring" else equipment_kind
        )
        item["equipped"] = True
        item["equip_slot"] = slot
    elif equipment_kind == "weapon":
        handedness = str(item.get("handedness") or "one_handed").strip().lower()
        if handedness == "two_handed":
            if shield_equipped:
                return False, "Cannot equip a two-handed weapon while a shield is equipped."
            if equipped_weapons:
                return False, "Unequip your other weapon first."
            item["equipped"] = True
            item["equip_slot"] = "main_hand"
        else:
            if has_two_handed:
                return False, "Cannot equip another weapon while a two-handed weapon is equipped."
            weapon_slots = {str(it.get("equip_slot") or "").strip().lower() for _, it in equipped_weapons}
            if len(equipped_weapons) >= 2:
                return False, "You can equip at most two weapons."
            if "main_hand" not in weapon_slots:
                item["equipped"] = True
                item["equip_slot"] = "main_hand"
            elif "off_hand" not in weapon_slots:
                item["equipped"] = True
                item["equip_slot"] = "off_hand"
            else:
                return False, "You can equip at most two weapons."

    mine[item_index] = item
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    session.player_inventories = inventories
    return True, f"Equipped {item.get('name') or 'item'}."


def _unequip_item(session: Session, user: User, item_index: int) -> tuple[bool, str]:
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    if item_index < 0 or item_index >= len(mine):
        return False, "That inventory item is no longer available."
    item = dict(mine[item_index] or {})
    if not bool(item.get("equipped")):
        return True, f"{item.get('name') or 'Item'} is already unequipped."
    item["equipped"] = False
    item["equip_slot"] = None
    mine[item_index] = item
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    session.player_inventories = inventories
    return True, f"Unequipped {item.get('name') or 'item'}."


async def handle_inventory_equip_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    if item_index < 0:
        return await _send_inventory_action_result(session, user.id, "Choose an item to equip.")
    ok, msg = _equip_item(session, target_user, item_index)
    if not ok:
        return await _send_inventory_action_result(session, user.id, msg)
    ac_value = _recompute_equipment_effects(session, target_user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"{msg} AC is now {ac_value}.")
    if target_user.id != user.id:
        await _send_inventory_action_result(session, target_user.id, f"{msg} AC is now {ac_value}.")


async def handle_inventory_unequip_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    if item_index < 0:
        return await _send_inventory_action_result(session, user.id, "Choose an item to unequip.")
    ok, msg = _unequip_item(session, target_user, item_index)
    if not ok:
        return await _send_inventory_action_result(session, user.id, msg)
    ac_value = _recompute_equipment_effects(session, target_user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"{msg} AC is now {ac_value}.")
    if target_user.id != user.id:
        await _send_inventory_action_result(session, target_user.id, f"{msg} AC is now {ac_value}.")


async def handle_inventory_attune_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    inventories, owner_key, mine = _get_player_inventory_store(session, target_user)
    if item_index < 0 or item_index >= len(mine):
        return await _send_inventory_action_result(session, user.id, "Choose an item to attune.")
    item = dict(mine[item_index] or {})
    if not _item_requires_attunement(item):
        return await _send_inventory_action_result(session, user.id, f"{item.get('name') or 'That item'} does not require attunement.")
    if bool(item.get("attuned")):
        return await _send_inventory_action_result(session, user.id, f"{item.get('name') or 'That item'} is already attuned.")
    if _attuned_item_count(mine) >= 3:
        return await _send_inventory_action_result(session, user.id, "Attunement limit reached (3/3). Unattune another item first.")
    item["attunement_required"] = True
    item["requires_attunement"] = True
    item["attuned"] = True
    mine[item_index] = item
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    session.player_inventories = inventories
    ac_value = _recompute_equipment_effects(session, target_user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"Attuned {item.get('name') or 'item'}. Attunement {_attuned_item_count(mine)}/3. AC is now {ac_value}.")
    if target_user.id != user.id:
        await _send_inventory_action_result(session, target_user.id, f"Attuned {item.get('name') or 'item'}.")


async def handle_inventory_unattune_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    inventories, owner_key, mine = _get_player_inventory_store(session, target_user)
    if item_index < 0 or item_index >= len(mine):
        return await _send_inventory_action_result(session, user.id, "Choose an item to unattune.")
    item = dict(mine[item_index] or {})
    if not bool(item.get("attuned")):
        return await _send_inventory_action_result(session, user.id, f"{item.get('name') or 'That item'} is not attuned.")
    item["attuned"] = False
    mine[item_index] = item
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    session.player_inventories = inventories
    ac_value = _recompute_equipment_effects(session, target_user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"Unattuned {item.get('name') or 'item'}. Attunement {_attuned_item_count(mine)}/3. AC is now {ac_value}.")
    if target_user.id != user.id:
        await _send_inventory_action_result(session, target_user.id, f"Unattuned {item.get('name') or 'item'}.")


def _roll_formula_total(formula: str) -> int:
    total, _rolls, _modifier = _roll_formula_detail(formula)
    return total


def _roll_formula_detail(formula: str) -> tuple[int, list[int], int]:
    text = str(formula or "").strip().lower().replace(" ", "")
    if not text:
        return 0, [], 0
    match = re.fullmatch(r"(\d+)d(\d+)([+-]\d+)?", text)
    if not match:
        return 0, [], 0
    dice = _safe_int(match.group(1), 1, minimum=1, maximum=50)
    sides = _safe_int(match.group(2), 1, minimum=1, maximum=100)
    modifier = _safe_int(match.group(3) or 0, 0, minimum=-200, maximum=200)
    rolls = [random.randint(1, sides) for _ in range(max(1, dice))]
    return sum(rolls) + modifier, rolls, modifier


def _apply_healing_to_player_hp(session: Session, user: User, amount: int) -> dict:
    healed = max(0, int(amount or 0))
    target_name = str(getattr(user, "name", "") or "character")
    before_hp = None
    after_hp = None
    max_hp = None

    for token in (getattr(session, "tokens", {}) or {}).values():
        if not _owner_matches_user(getattr(token, "owner_id", ""), user):
            continue
        if bool(getattr(token, "staged", False)):
            continue
        current = int(getattr(token, "hp", 0) or 0)
        cap = int(getattr(token, "max_hp", 0) or 0)
        if cap <= 0:
            continue
        before_hp = current
        max_hp = cap
        after_hp = min(cap, current + healed)
        token.hp = after_hp
        target_name = str(getattr(token, "name", "") or target_name)
        break

    profiles = dict(getattr(session, "char_profiles", {}) or {})
    owner_key = normalize_profile_owner_key(getattr(user, "name", "")) or user.id
    rows = list(profiles.get(owner_key) or profiles.get(user.id) or [])
    if rows:
        latest_index = max(range(len(rows)), key=lambda idx: float((rows[idx] or {}).get("updated_at") or 0.0))
        profile = dict(rows[latest_index] or {})
        sheet = dict(profile.get("charSheet") or {})
        hp = dict(sheet.get("hp") or {})
        book = dict(profile.get("charBook") or {})
        current = _safe_int(hp.get("current", sheet.get("currentHp", book.get("currentHp", before_hp if before_hp is not None else 0))), 0, minimum=0, maximum=9999)
        cap = _safe_int(hp.get("max", sheet.get("maxHp", book.get("maxHp", max_hp if max_hp is not None else 0))), 0, minimum=0, maximum=9999)
        if cap > 0:
            before_hp = current if before_hp is None else before_hp
            max_hp = cap if max_hp is None else max_hp
            after_profile_hp = min(cap, current + healed)
            after_hp = after_profile_hp if after_hp is None else after_hp
            hp["current"] = after_profile_hp
            hp["max"] = cap
            sheet["hp"] = hp
            sheet["currentHp"] = after_profile_hp
            sheet["maxHp"] = cap
            book["currentHp"] = after_profile_hp
            book["maxHp"] = cap
            profile["charSheet"] = sheet
            profile["charBook"] = book
            rows[latest_index] = profile
            profiles[owner_key if owner_key in profiles else user.id] = rows
            session.char_profiles = profiles
    return {"target_name": target_name, "before_hp": before_hp, "after_hp": after_hp, "max_hp": max_hp}


async def handle_inventory_use_item_action(payload: dict, session: Session, user: User):
    client_action_id = payload.get("client_action_id")

    async def _denied(reason: str, *, status: str = "denied"):
        await _send_action_ack(session, user, action="inventory_use_item_action",
                                client_action_id=client_action_id, status=status, reason=reason)

    if user.role != "player":
        await _denied("Action denied", status="failed")
        return await _send_inventory_action_result(session, user.id, "Only players can use inventory item actions.")
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    if item_index < 0:
        await _denied("Item not found", status="failed")
        return await _send_inventory_action_result(session, user.id, "Choose an item action first.")
    inventories, owner_key, items = _get_player_inventory_store(session, user)
    if item_index >= len(items):
        await _denied("Item not found", status="failed")
        return await _send_inventory_action_result(session, user.id, "That item is no longer in your inventory.")
    item = dict(items[item_index] or {})
    action_payload, _passive = _build_item_runtime_action(item, item_index)
    if not action_payload:
        await _denied("Action denied")
        return await _send_inventory_action_result(session, user.id, "That item does not grant an active action.")
    if bool(action_payload.get("disabled")):
        await _denied("Action denied")
        return await _send_inventory_action_result(
            session,
            user.id,
            str(action_payload.get("disabled_reason") or "That item cannot be used right now."),
        )
    usage_cost = max(1, _safe_int(payload.get("usage_cost"), action_payload.get("usage_cost") or 1, minimum=1, maximum=20))
    if action_payload.get("charges_current") is not None:
        charges_current = _safe_int(item.get("charges_current"), _safe_int(action_payload.get("charges_current"), 0, minimum=0, maximum=99), minimum=0, maximum=99)
        if charges_current < usage_cost:
            await _denied("Not enough charges")
            return await _send_inventory_action_result(session, user.id, "Not enough charges.")
        item["charges_current"] = charges_current - usage_cost
        if item.get("charges_max") is None and action_payload.get("charges_max") is not None:
            item["charges_max"] = _safe_int(action_payload.get("charges_max"), 0, minimum=0, maximum=99)
    removed = False
    if bool(action_payload.get("consumed_on_use")):
        current_qty = _safe_int(item.get("qty"), 1, minimum=1, maximum=9999)
        next_qty = current_qty - 1
        if next_qty <= 0 and bool(action_payload.get("remove_when_empty", True)):
            items.pop(item_index)
            removed = True
        else:
            item["qty"] = max(1, next_qty)
    if not removed:
        items[item_index] = item
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in items) if entry]
    session.player_inventories = inventories
    _recompute_equipment_effects(session, user)
    result = {
        "type": "item_action_result",
        "item_name": str(action_payload.get("item_name") or item.get("name") or "Item"),
        "action_name": str(action_payload.get("action_name") or "Use Item"),
        "item_id": str(action_payload.get("item_id") or ""),
        "consumed": bool(action_payload.get("consumed_on_use")),
        "removed": removed,
        "usage_cost": usage_cost,
        "remaining_quantity": None if removed else _safe_int(item.get("qty"), 1, minimum=1, maximum=9999),
        "remaining_charges": None if removed else _safe_int(item.get("charges_current"), 0, minimum=0, maximum=99) if item.get("charges_current") is not None else None,
        "healing_formula": str(action_payload.get("healing_formula") or ""),
        "damage_formula": str(action_payload.get("damage_formula") or ""),
        "effect_text": str(action_payload.get("effect_text") or ""),
        "target_type": str(action_payload.get("target_type") or ""),
    }
    if result["healing_formula"]:
        healing_total, rolls, modifier = _roll_formula_detail(result["healing_formula"])
        result["healing_total"] = healing_total
        result["healing_rolls"] = rolls
        result["healing_modifier"] = modifier
        result["healing_target"] = _apply_healing_to_player_hp(session, user, healing_total)
    await _broadcast_inventory_state(session)
    await _send_action_ack(
        session, user, action="inventory_use_item_action", client_action_id=client_action_id,
        status="confirmed", item_id=result["item_id"],
        inventory_revision=int(getattr(session, "inventory_revision", 0) or 0),
    )
    await manager.send_to(session.id, user.id, {"type": "inventory_item_used", "payload": result})
    if result.get("healing_total"):
        target = dict(result.get("healing_target") or {})
        chat_text = (
            f"{result['item_name']} used. Rolled {result['healing_formula']} = {result['healing_total']}. "
            f"Healed {target.get('target_name') or user.name} for {result['healing_total']} HP."
        )
    else:
        chat_text = f"🧰 **{result['action_name']}** — {result['item_name']}."
    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {
            "user": user.name,
            "message": chat_text,
            "channel": "everyone",
            "msg_type": "system",
        },
    })
    await _send_inventory_action_result(
        session,
        user.id,
        f"{result['item_name']} used."
        if not result.get("healing_total")
        else f"{result['item_name']} used for {result['healing_total']} healing.",
    )
    await save_campaign_async(session)


def _build_item_spell_cards(items: list[dict], *, include_unavailable: bool = False) -> list[dict]:
    """Build item-spell cards for items that grant spells.

    By default only items that are actually equipped and (if required) attuned
    yield cards — these are delivered as `item_spell_cards` in the inventory
    state payload and are the player's castable item spells. They are distinct
    from class-prepared spells and must not consume spell slots.

    When ``include_unavailable`` is True (used by the quick-actions snapshot),
    unequipped/unattuned items still yield cards, but they are marked
    ``disabled`` with a human-readable ``disabled_reason`` so the UI can explain
    why the spell cannot be cast.
    """
    from server.item_compendium import get_spell_metadata  # local import to avoid circular
    cards: list[dict] = []
    for idx, item in enumerate(list(items or [])):
        if not isinstance(item, dict):
            continue
        granted_spells = list(item.get("granted_spells") or item.get("grantedSpells") or item.get("itemSpells") or item.get("item_spells") or item.get("spellsGranted") or item.get("spellGrants") or [])
        if not granted_spells:
            continue

        # Item-granted spells are only available when the item is actually
        # equipped and (if it requires attunement) attuned. An item sitting in
        # the bag must not contribute castable spell cards unless the caller has
        # asked to surface them as disabled (e.g. the quick-actions UI).
        if not include_unavailable:
            if not bool(item.get("equipped")):
                continue
            if not _item_is_attuned(item):
                continue

        item_id = str(item.get("id") or item.get("magic_item_id") or f"item_{idx}")
        item_name = str(item.get("name") or "Item")
        charges_current = _safe_int(item.get("charges_current"), -1, minimum=-1, maximum=9999)
        charges_max = _safe_int(item.get("charges_max"), 0, minimum=0, maximum=9999)
        item_spell_dc = _safe_int(item.get("item_spell_save_dc"), 0, minimum=0, maximum=40)
        item_spell_atk = _safe_int(item.get("item_spell_attack_bonus"), 0, minimum=-20, maximum=30)

        for spell_entry in granted_spells:
            if isinstance(spell_entry, str):
                spell_entry = {"id": spell_entry.lower().replace(" ", "-"), "name": spell_entry}
            if not isinstance(spell_entry, dict):
                continue
            spell_id = str(spell_entry.get("spellId") or spell_entry.get("spell_id") or spell_entry.get("id") or "").strip()
            spell_name = str(spell_entry.get("name") or spell_id).strip()
            if not spell_id and not spell_name:
                continue

            charge_cost = max(0, _safe_int(spell_entry.get("charge_cost", spell_entry.get("chargeCost", 1)), 1, minimum=0, maximum=99))
            charge_cost_min = _safe_int(spell_entry.get("charge_cost_min"), 0, minimum=0, maximum=99) or None
            charge_cost_max = _safe_int(spell_entry.get("charge_cost_max"), 0, minimum=0, maximum=99) or None
            cast_level = max(0, _safe_int(spell_entry.get("cast_level", spell_entry.get("castLevel", spell_entry.get("defaultCastLevel", 0))), 0, minimum=0, maximum=9))
            uses_item_dc = bool(spell_entry.get("uses_item_dc", spell_entry.get("usesItemDc", True)))
            uses_item_atk = bool(spell_entry.get("uses_item_attack_bonus", spell_entry.get("usesItemAttackBonus", False)))
            consume_slot = bool(spell_entry.get("consume_spell_slot", False))

            spell_meta = get_spell_metadata(spell_id) or {}
            resolved_name = str(spell_meta.get("displayName") or spell_name)

            missing_spell_data = not bool(spell_meta)
            has_charges = charges_current < 0 or charges_current >= charge_cost
            disabled_reason = ""
            if not bool(item.get("equipped")):
                disabled_reason = "Not equipped."
            elif not _item_is_attuned(item):
                disabled_reason = "Not attuned."
            elif not has_charges:
                disabled_reason = f"No charges (need {charge_cost}, have {max(0, charges_current)})."
            elif missing_spell_data:
                disabled_reason = "Missing spell data."
            disabled = bool(disabled_reason)

            cards.append({
                "source": "item_spell",
                "item_index": idx,
                "item_id": item_id,
                "item_name": item_name,
                "spell_id": spell_id,
                "spell_name": resolved_name or spell_name,
                "charge_cost": charge_cost,
                "charge_cost_min": charge_cost_min,
                "charge_cost_max": charge_cost_max,
                "cast_level": cast_level,
                "uses_item_dc": uses_item_dc,
                "uses_item_attack_bonus": uses_item_atk,
                "consume_spell_slot": consume_slot,
                "item_spell_save_dc": item_spell_dc if uses_item_dc else 0,
                "item_spell_attack_bonus": item_spell_atk if uses_item_atk else 0,
                "charges_current": charges_current if charges_current >= 0 else None,
                "charges_max": charges_max,
                "disabled": disabled,
                "disabled_reason": disabled_reason,
                "level": cast_level or _safe_int(spell_meta.get("level"), 0, minimum=0, maximum=9),
                "school": str(spell_meta.get("school") or ""),
                "range": str(spell_meta.get("range") or ""),
                "casting_time": str(spell_meta.get("castingTime") or "1 Action"),
                "duration": str(spell_meta.get("duration") or ""),
                "components": str(spell_meta.get("components") or ""),
                "damage_type": str(spell_meta.get("damageType") or ""),
                "damage_formula": str(spell_meta.get("damageFormula") or ""),
                "saving_throw": str(spell_meta.get("savingThrow") or ""),
                "attack_type": str(spell_meta.get("attackType") or ""),
                "description": str(spell_entry.get("description") or spell_meta.get("description") or ""),
            })
    return cards


async def handle_inventory_cast_item_spell(payload: dict, session: Session, user: User):
    """Cast a spell from an equipped magic item, spending the configured charge cost."""
    client_action_id = payload.get("client_action_id")

    async def _denied(reason: str, *, status: str = "denied"):
        await _send_action_ack(session, user, action="inventory_cast_item_spell",
                                client_action_id=client_action_id, status=status, reason=reason)

    if user.role != "player":
        await _denied("Action denied", status="failed")
        return await _send_inventory_action_result(session, user.id, "Only players can cast item spells.")

    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    item_id = str(payload.get("item_id") or "").strip()
    spell_id = str(payload.get("spell_id") or "").strip()
    target_id = str(payload.get("target_id") or "").strip()
    charge_cost = max(0, _safe_int(payload.get("charge_cost"), 1, minimum=0, maximum=99))
    cast_level = max(0, _safe_int(payload.get("cast_level"), 0, minimum=0, maximum=9))

    if not spell_id:
        await _denied("Spell not available", status="failed")
        return await _send_inventory_action_result(session, user.id, "No spell specified.")
    if item_index < 0:
        await _denied("Item not found", status="failed")
        return await _send_inventory_action_result(session, user.id, "No item specified.")

    inventories, owner_key, items = _get_player_inventory_store(session, user)
    if item_index >= len(items):
        await _denied("Item not found", status="failed")
        return await _send_inventory_action_result(session, user.id, "That item is no longer in your inventory.")

    item = dict(items[item_index] or {})

    actual_id = str(item.get("id") or item.get("magic_item_id") or "").strip()
    if item_id and actual_id and item_id != actual_id:
        await _denied("Item not found", status="failed")
        return await _send_inventory_action_result(session, user.id, "Item mismatch — please refresh your inventory.")

    if not bool(item.get("equipped")):
        await _denied("Item not equipped")
        return await _send_inventory_action_result(session, user.id, "That item must be equipped to cast its spells.")

    if not _item_is_attuned(item):
        await _denied("Item not equipped")
        return await _send_inventory_action_result(
            session, user.id, f"{item.get('name', 'That item')} requires attunement before casting its spells."
        )

    granted_spells = list(item.get("granted_spells") or item.get("grantedSpells") or item.get("itemSpells") or item.get("item_spells") or item.get("spellsGranted") or item.get("spellGrants") or [])
    spell_entry: dict | None = None
    for gs in granted_spells:
        if isinstance(gs, str):
            if gs.lower().replace(" ", "-") == spell_id.lower():
                gs = {"id": spell_id, "name": gs}
            else:
                continue
        if isinstance(gs, dict):
            gs_id = str(gs.get("spellId") or gs.get("spell_id") or gs.get("id") or "").strip()
            gs_name = str(gs.get("name") or "").lower().replace(" ", "-")
            if gs_id == spell_id or gs_name == spell_id.lower():
                spell_entry = gs
                break

    if spell_entry is None:
        await _denied("Spell not available")
        return await _send_inventory_action_result(
            session, user.id, f"Spell '{spell_id}' is not granted by {item.get('name', 'that item')}."
        )

    item_charge_cost = max(0, _safe_int(spell_entry.get("charge_cost", spell_entry.get("chargeCost", 1)), 1, minimum=0, maximum=99))
    charge_cost_min = _safe_int(spell_entry.get("charge_cost_min"), 0, minimum=0, maximum=99) or None
    charge_cost_max = _safe_int(spell_entry.get("charge_cost_max"), 0, minimum=0, maximum=99) or None
    actual_cost = charge_cost if charge_cost > 0 else item_charge_cost
    if charge_cost_min is not None and charge_cost_max is not None and charge_cost_max > charge_cost_min:
        actual_cost = max(charge_cost_min, min(charge_cost_max, actual_cost))
    charges_current = _safe_int(item.get("charges_current"), -1, minimum=-1, maximum=9999)
    charges_max = _safe_int(item.get("charges_max"), 0, minimum=0, maximum=9999)

    if charges_max > 0 and charges_current >= 0 and charges_current < actual_cost:
        await _denied("Not enough charges")
        return await _send_inventory_action_result(
            session, user.id,
            f"Not enough charges. {item.get('name', 'Item')} has {charges_current}/{charges_max} charges; casting {spell_id} costs {actual_cost}.",
        )

    from server.item_compendium import get_spell_metadata  # local import
    spell_meta = get_spell_metadata(spell_id) or {}
    spell_name = str(spell_entry.get("name") or spell_meta.get("displayName") or spell_id)

    if charges_max > 0 and charges_current >= 0:
        item["charges_current"] = max(0, charges_current - actual_cost)
    items[item_index] = item
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in items) if entry]
    session.player_inventories = inventories

    uses_item_dc = bool(spell_entry.get("uses_item_dc", spell_entry.get("usesItemDc", True)))
    uses_item_atk = bool(spell_entry.get("uses_item_attack_bonus", spell_entry.get("usesItemAttackBonus", False)))
    item_spell_dc = _safe_int(item.get("item_spell_save_dc"), 0, minimum=0, maximum=40)
    item_spell_atk = _safe_int(item.get("item_spell_attack_bonus"), 0, minimum=-20, maximum=30)
    resolved_cast_level = cast_level or _safe_int(spell_entry.get("cast_level", spell_entry.get("castLevel", spell_entry.get("defaultCastLevel", 0))), 0, minimum=0, maximum=9)

    result = {
        "type": "item_spell_cast_result",
        "item_name": str(item.get("name") or "Item"),
        "item_id": actual_id or item_id,
        "item_index": item_index,
        "spell_id": spell_id,
        "spell_name": spell_name,
        "cast_level": resolved_cast_level,
        "charge_cost": actual_cost,
        "remaining_charges": item.get("charges_current"),
        "save_dc": item_spell_dc if uses_item_dc else 0,
        "attack_bonus": item_spell_atk if uses_item_atk else 0,
        "target_id": target_id,
        "damage_formula": str(spell_meta.get("damageFormula") or ""),
        "damage_type": str(spell_meta.get("damageType") or ""),
        "range": str(spell_meta.get("range") or ""),
        "school": str(spell_meta.get("school") or ""),
        "description": str(spell_entry.get("description") or spell_meta.get("description") or ""),
    }

    await _broadcast_inventory_state(session)
    await _send_action_ack(
        session, user, action="inventory_cast_item_spell", client_action_id=client_action_id,
        status="confirmed", item_id=result["item_id"], spell_key=spell_id,
        inventory_revision=int(getattr(session, "inventory_revision", 0) or 0),
    )
    await manager.send_to(session.id, user.id, {"type": "inventory_item_spell_cast", "payload": result})

    level_note = f" (level {resolved_cast_level})" if resolved_cast_level else ""
    charge_note = f" — {actual_cost} charge{'s' if actual_cost != 1 else ''} spent" if actual_cost > 0 else ""
    remaining = item.get("charges_current")
    remaining_note = f", {remaining} remaining" if remaining is not None else ""
    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {
            "user": user.name,
            "message": f"✨ **{spell_name}**{level_note} — cast from *{item.get('name', 'an item')}*{charge_note}{remaining_note}.",
            "channel": "everyone",
            "msg_type": "system",
        },
    })
    await _send_inventory_action_result(
        session, user.id,
        f"Cast {spell_name} from {item.get('name', 'item')}{charge_note}{remaining_note}.",
    )
    await save_campaign_async(session)


async def handle_prop_take_item(payload: dict, session: Session, user: User):
    await _execute_prop_take_item(payload, session, user, bypass_need_greed=False)


async def handle_chest_loot_roll_choice(payload: dict, session: Session, user: User):
    if getattr(user, "role", "") != "player":
        return
    roll_id = str(payload.get("roll_id") or "").strip()[:64]
    choice = str(payload.get("choice") or "pass").strip().lower()
    if not roll_id:
        return
    if choice not in _LOOT_ROLL_NEED_GREED_CHOICES:
        choice = "pass"
    state = _init_loot_roll_state(session).get(roll_id)
    if not isinstance(state, dict):
        return
    responder_ids = list(state.get("responder_ids") or [])
    if user.id not in responder_ids:
        return
    deadline = float(state.get("deadline") or 0)
    if deadline and time.time() > deadline:
        return  # Late vote rejected – auto-pass already ran or will run
    choices = dict(state.get("choices") or {})
    if user.id in choices:
        return
    roll_value = random.randint(1, 20) if choice in {"need", "greed"} else 0
    choices[user.id] = {
        "choice": choice,
        "roll": roll_value,
        "user_name": user.name,
        "chosen_at": time.time(),
    }
    state["choices"] = choices
    _init_loot_roll_state(session)[roll_id] = state
    await manager.broadcast(session.id, {
        "type": "chest_loot_roll_choice",
        "payload": {
            "roll_id": roll_id,
            "user_id": user.id,
            "user_name": user.name,
            "choice": choice,
            "roll": roll_value,
            "remaining": max(0, len(responder_ids) - len(choices)),
        },
    })
    await _finalize_loot_roll_resolution(session, roll_id)


async def handle_prop_buy_item(payload: dict, session: Session, user: User):
    from server.handlers.map_editor import (
        _get_editor_prop, _send_prop_action_error,
        _broadcast_prop_action_log, _send_prop_action_result,
        _broadcast_editor_props_state,
    )
    if user.role not in {"dm", "player"}:
        return
    map_ctx = str(payload.get("map_context") or "world")[:80]
    prop_id = str(payload.get("prop_id") or "").strip()[:48]
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=999)
    request_qty = _safe_int(payload.get("qty"), 1, minimum=1, maximum=999)
    if not prop_id or item_index < 0:
        return await _send_prop_action_error(session, user, "Invalid shop item selection.")
    props_all, items, prop_idx, prop = _get_editor_prop(session, map_ctx, prop_id)
    if prop_idx < 0 or not prop:
        fallback_ctx, fallback_prop = _find_prop_context_and_data(session, prop_id)
        if fallback_ctx and fallback_prop:
            map_ctx = fallback_ctx
            props_all, items, prop_idx, prop = _get_editor_prop(session, map_ctx, prop_id)
    if prop_idx < 0 or not prop:
        return await _send_prop_action_error(session, user, "That shop is no longer available on this map.")
    kind = str(prop.get("kind") or "")
    if kind not in {"merchant", "store", "tavern", "blacksmith", "market_stall", "inn", "shop", "shop_front"}:
        return await _send_prop_action_error(session, user, "Only shop props support buying right now.")
    if user.role != "dm" and bool(prop.get("hidden")):
        return await _send_prop_action_error(session, user, "That shop is not visible to players yet.")
    inventory = list(prop.get("inventory") or [])
    if item_index >= len(inventory):
        return await _send_prop_action_error(session, user, "That stock slot is no longer available.")
    entry = dict(inventory[item_index] or {})
    entry_name = str(entry.get("name") or "").strip()[:80]
    if not entry_name:
        return await _send_prop_action_error(session, user, "That stock slot is empty.")
    current_qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=999)
    infinite = bool(entry.get("infinite"))
    buy_qty = request_qty if infinite else min(current_qty, request_qty)
    raw_price = str(entry.get("price") or "").strip()[:32]
    per_item_units = 0
    if raw_price:
        parsed_units = _parse_gold_to_units(raw_price)
        if parsed_units is None:
            return await _send_prop_action_error(session, user, f"{entry_name} has a shop price I can't charge yet ({raw_price}).")
        per_item_units = parsed_units
    total_cost_units = per_item_units * max(1, buy_qty)
    if total_cost_units > 0:
        ok, current_gold, new_gold = _try_spend_gold(session, user, total_cost_units)
        if not ok:
            return await _send_prop_action_error(session, user, f"Not enough gold. Need {_format_gold_units(total_cost_units)}, have {_format_gold_units(current_gold)}.")
    else:
        new_gold = get_player_gold_for_user(session, user.id)
    if not infinite:
        remaining = current_qty - buy_qty
        if remaining > 0:
            entry["qty"] = remaining
            inventory[item_index] = entry
        else:
            inventory.pop(item_index)
        prop["inventory"] = inventory
        items[prop_idx] = prop
        props_all[map_ctx] = items
        session.editor_props = props_all
    from server.handlers.common import _refresh_map_documents
    _refresh_map_documents(session, map_ctx)
    await _broadcast_editor_props_state(session)
    price = raw_price
    default_shop_name = {"merchant": "Merchant", "store": "Store Stall", "tavern": "Tavern", "blacksmith": "Blacksmith", "market_stall": "Market Stall", "inn": "Inn", "shop": "Shop"}.get(kind, "Shop")
    prop_name = str(prop.get("name") or default_shop_name).strip()[:60] or default_shop_name
    _add_item_to_player_inventory(session, user, entry, buy_qty, source_name=prop_name, price=price)
    _recompute_equipment_effects(session, user)
    total_cost_label = _format_gold_units(total_cost_units) if total_cost_units > 0 else ""
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "buy",
        "item_name": entry_name,
        "qty": buy_qty,
        "price": total_cost_label or price,
        "source_name": prop_name,
        "source_kind": kind,
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    suffix = f" for {total_cost_label}" if total_cost_label else (f" for {price}" if price else "")
    action_text = f"{user.name} bought {buy_qty}× {entry_name}{suffix} from {prop_name}." if buy_qty != 1 else f"{user.name} bought {entry_name}{suffix} from {prop_name}."
    if total_cost_units > 0:
        action_text += f" ({_format_gold_units(new_gold)} remaining)"
    await _broadcast_prop_action_log(session, action_text)
    msg = f"Bought {buy_qty}× {entry_name}{suffix}." if buy_qty != 1 else f"Bought {entry_name}{suffix}."
    if total_cost_units > 0:
        msg += f" {_format_gold_units(new_gold)} remaining."
    await _send_prop_action_result(session, user.id, msg)


async def handle_inventory_add_gold(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    if user.role != "dm":
        await _send_inventory_action_result(session, user.id, "Only the DM can add gold directly.")
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    amount_units = _parse_gold_to_units(payload.get("amount"))
    if amount_units is None or amount_units <= 0:
        return await _send_inventory_action_result(session, user.id, "Enter a gold amount like 100 gp or 5 sp.")
    source_name = str(payload.get("source_name") or ("DM Award" if user.role == "dm" else "Manual Add")).strip()[:60] or ("DM Award" if user.role == "dm" else "Manual Add")
    new_total = _add_gold_to_player(session, target_user, amount_units)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "add",
        "item_name": "Gold",
        "qty": 1,
        "target_name": target_user.name if target_user.id != user.id else "",
        "source_name": source_name,
        "price": _format_gold_units(amount_units),
        "source_kind": "inventory",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    msg = f"Added {_format_gold_units(amount_units)}. {target_user.name} now has {_format_gold_units(new_total)} on hand." if user.role == "dm" and target_user.id != user.id else f"Added {_format_gold_units(amount_units)}. {_format_gold_units(new_total)} on hand."
    await _send_inventory_action_result(session, user.id, msg)
    if target_user.id != user.id:
        await _send_inventory_action_result(session, target_user.id, f"Received {_format_gold_units(amount_units)} from {user.name}. {_format_gold_units(new_total)} on hand.")


async def handle_inventory_remove_gold(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    if user.role != "dm":
        await _send_inventory_action_result(session, user.id, "Only the DM can remove gold directly.")
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    amount_units = _parse_gold_to_units(payload.get("amount"))
    if amount_units is None or amount_units <= 0:
        return await _send_inventory_action_result(session, user.id, "Enter a gold amount like 100 gp or 5 sp.")
    ok, current_gold, new_gold = _try_spend_gold(session, target_user, amount_units)
    if not ok:
        return await _send_inventory_action_result(session, user.id, f"Not enough gold. Have {_format_gold_units(current_gold)}, need {_format_gold_units(amount_units)}.")
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "remove",
        "item_name": "Gold",
        "qty": 1,
        "target_name": target_user.name if target_user.id != user.id else "",
        "source_name": "Manual Remove",
        "price": _format_gold_units(amount_units),
        "source_kind": "inventory",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    msg = f"Removed {_format_gold_units(amount_units)}. {_format_gold_units(new_gold)} remaining." if target_user.id == user.id else f"Removed {_format_gold_units(amount_units)} from {target_user.name}. {_format_gold_units(new_gold)} remaining."
    await _send_inventory_action_result(session, user.id, msg)


async def handle_inventory_add_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    if user.role != "dm":
        await _send_inventory_action_result(session, user.id, "Only the DM can add items directly.")
        return
    entry = _normalize_player_inventory_entry(payload.get("entry") or payload or {})
    if not entry:
        return await _send_inventory_action_result(session, user.id, "Add an item name first.")
    qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
    source_name = str(payload.get("source_name") or entry.get("source") or "Manual Add").strip()[:60] or "Manual Add"
    price = str(entry.get("price") or payload.get("price") or "").strip()[:32]
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    item_name = str(entry.get("name") or "").strip()
    if _is_gold_item_name(item_name):
        added_units = max(0, qty) * 100
        new_total = _add_gold_to_player(session, target_user, added_units)
        _append_party_loot_log(session, {
            "player_name": user.name,
            "player_role": user.role,
            "action": "add",
            "item_name": "Gold",
            "qty": qty,
            "target_name": target_user.name if target_user.id != user.id else "",
            "source_name": source_name,
            "price": _format_gold_units(added_units),
            "source_kind": "inventory",
        })
        await _broadcast_inventory_state(session)
        await save_campaign_async(session)
        msg = f"Added {_format_gold_units(added_units)}. {target_user.name} now has {_format_gold_units(new_total)} on hand." if target_user.id != user.id else f"Added {_format_gold_units(added_units)}. {_format_gold_units(new_total)} on hand."
        await _send_inventory_action_result(session, user.id, msg)
        if target_user.id != user.id:
            await _send_inventory_action_result(session, target_user.id, f"Received {_format_gold_units(added_units)} from {user.name}. {_format_gold_units(new_total)} on hand.")
        return
    _add_item_to_player_inventory(session, target_user, entry, qty, source_name=source_name, price=price)
    _recompute_equipment_effects(session, target_user)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "add",
        "item_name": entry.get("name"),
        "qty": qty,
        "target_name": target_user.name if target_user.id != user.id else "",
        "source_name": source_name,
        "price": price,
        "source_kind": "inventory",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    msg = f"Added {qty}× {entry.get('name')} to {target_user.name}'s inventory." if qty != 1 else f"Added {entry.get('name')} to {target_user.name}'s inventory."
    if target_user.id == user.id:
        msg = f"Added {qty}× {entry.get('name')} to your inventory." if qty != 1 else f"Added {entry.get('name')} to your inventory."
    await _send_inventory_action_result(session, user.id, msg)
    if target_user.id != user.id:
        await _send_inventory_action_result(session, target_user.id, f"Received {qty}× {entry.get('name')} from {user.name}." if qty != 1 else f"Received {entry.get('name')} from {user.name}.")


async def handle_inventory_remove_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    request_qty = _safe_int(payload.get("qty"), 1, minimum=1, maximum=9999)
    if item_index < 0:
        return await _send_inventory_action_result(session, user.id, "Choose an item to remove.")
    entry, removed_qty = _remove_item_from_player_inventory(session, user, item_index, request_qty)
    if not entry or removed_qty <= 0:
        return await _send_inventory_action_result(session, user.id, "That inventory item is no longer available.")
    _recompute_equipment_effects(session, user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    item_name = str(entry.get("name") or "Item").strip() or "Item"
    await _send_inventory_action_result(session, user.id, f"Removed {removed_qty}× {item_name}." if removed_qty != 1 else f"Removed {item_name}.")


async def handle_inventory_transfer_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    request_qty = _safe_int(payload.get("qty"), 1, minimum=1, maximum=9999)
    target_user_id = str(payload.get("target_user_id") or "").strip()[:64]
    if item_index < 0 or not target_user_id:
        return await _send_inventory_action_result(session, user.id, "Choose an item and a recipient first.")
    target_user = (getattr(session, "users", {}) or {}).get(target_user_id)
    if not target_user or getattr(target_user, "role", "viewer") not in {"dm", "player"}:
        return await _send_inventory_action_result(session, user.id, "That recipient is no longer available.")
    if target_user.id == user.id:
        return await _send_inventory_action_result(session, user.id, "Choose another player to transfer to.")
    entry, moved_qty = _remove_item_from_player_inventory(session, user, item_index, request_qty)
    if not entry or moved_qty <= 0:
        return await _send_inventory_action_result(session, user.id, "That inventory item is no longer available.")
    item_name = str(entry.get("name") or "Item").strip() or "Item"
    price = str(entry.get("price") or "").strip()[:32]
    _add_item_to_player_inventory(session, target_user, entry, moved_qty, source_name=f"Gift from {user.name}", price=price)
    _recompute_equipment_effects(session, user)
    _recompute_equipment_effects(session, target_user)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "give",
        "item_name": item_name,
        "qty": moved_qty,
        "target_name": target_user.name,
        "source_name": f"to {target_user.name}",
        "source_kind": "inventory",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"Gave {moved_qty}× {item_name} to {target_user.name}." if moved_qty != 1 else f"Gave {item_name} to {target_user.name}.")
    await _send_inventory_action_result(session, target_user.id, f"Received {moved_qty}× {item_name} from {user.name}." if moved_qty != 1 else f"Received {item_name} from {user.name}.")


async def handle_inventory_send_to_stash(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    request_qty = _safe_int(payload.get("qty"), 1, minimum=1, maximum=9999)
    if item_index < 0:
        return await _send_inventory_action_result(session, user.id, "Choose an item to send to the party stash.")
    entry, moved_qty = _remove_item_from_player_inventory(session, user, item_index, request_qty)
    if not entry or moved_qty <= 0:
        return await _send_inventory_action_result(session, user.id, "That inventory item is no longer available.")
    item_name = str(entry.get("name") or "Item").strip() or "Item"
    _add_item_to_party_stash(session, entry, moved_qty, source_name=f"Stashed by {user.name}")
    _recompute_equipment_effects(session, user)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "stash",
        "item_name": item_name,
        "qty": moved_qty,
        "source_name": "Party Stash",
        "source_kind": "inventory",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"Sent {moved_qty}× {item_name} to the party stash." if moved_qty != 1 else f"Sent {item_name} to the party stash.")


async def handle_stash_claim_item(payload: dict, session: Session, user: User):
    if user.role not in {"dm", "player"}:
        return
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    request_qty = _safe_int(payload.get("qty"), 1, minimum=1, maximum=9999)
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    if item_index < 0:
        return await _send_inventory_action_result(session, user.id, "Choose a stash item first.")
    entry, moved_qty = _remove_item_from_party_stash(session, item_index, request_qty)
    if not entry or moved_qty <= 0:
        return await _send_inventory_action_result(session, user.id, "That stash item is no longer available.")
    item_name = str(entry.get("name") or "Item").strip() or "Item"
    assigned_to_other = target_user.id != user.id
    _add_item_to_player_inventory(session, target_user, entry, moved_qty, source_name="Party Stash")
    _recompute_equipment_effects(session, target_user)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "claim",
        "item_name": item_name,
        "qty": moved_qty,
        "target_name": target_user.name if assigned_to_other else "",
        "source_name": "Party Stash",
        "source_kind": "stash",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    if assigned_to_other:
        await _send_inventory_action_result(session, user.id, f"Assigned {moved_qty}× {item_name} to {target_user.name} from the party stash." if moved_qty != 1 else f"Assigned {item_name} to {target_user.name} from the party stash.")
        await _send_inventory_action_result(session, target_user.id, f"Received {moved_qty}× {item_name} from the party stash." if moved_qty != 1 else f"Received {item_name} from the party stash.")
    else:
        await _send_inventory_action_result(session, user.id, f"Claimed {moved_qty}× {item_name} from the party stash." if moved_qty != 1 else f"Claimed {item_name} from the party stash.")


# ─── Shop system handlers ──────────────────────────────────────────────────────

def _shop_item_total_gp_units(item: dict) -> int:
    """Convert shop item price fields to internal units (1 gp = 100 units)."""
    gp = int(item.get("price_gp") or 0)
    sp = int(item.get("price_sp") or 0)
    cp = int(item.get("price_cp") or 0)
    return gp * 100 + sp * 10 + cp


def _haggle_offer_store(session: Session) -> dict[str, dict]:
    store = getattr(session, "_haggle_offers", None)
    if not isinstance(store, dict):
        store = {}
        session._haggle_offers = store
    return store


def _haggle_offer_key(user_id: str, shop_id: str, item_id: str) -> str:
    return f"{user_id}:{shop_id}:{item_id}"


def _set_haggle_offer(session: Session, user: User, shop_id: str, item_id: str, *, discount_pct: int, base_price_units: int) -> dict:
    expires_at = time.time() + _HAGGLE_OFFER_TTL_SECONDS
    offer = {
        "discount_pct": max(0, int(discount_pct)),
        "base_price_units": max(0, int(base_price_units)),
        "expires_at": float(expires_at),
        "created_at": float(time.time()),
    }
    _haggle_offer_store(session)[_haggle_offer_key(user.id, shop_id, item_id)] = offer
    return offer


def _clear_haggle_offer(session: Session, user_id: str, shop_id: str, item_id: str) -> None:
    _haggle_offer_store(session).pop(_haggle_offer_key(user_id, shop_id, item_id), None)


def _resolve_haggle_offer(session: Session, user: User, shop_id: str, item: dict) -> dict | None:
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return None
    key = _haggle_offer_key(user.id, shop_id, item_id)
    offer = _haggle_offer_store(session).get(key)
    if not isinstance(offer, dict):
        return None
    now = time.time()
    expires_at = float(offer.get("expires_at") or 0)
    if expires_at <= now:
        _haggle_offer_store(session).pop(key, None)
        return None
    base_units_now = _shop_item_total_gp_units(item)
    if int(offer.get("base_price_units") or -1) != base_units_now:
        _haggle_offer_store(session).pop(key, None)
        return None
    discount_pct = max(0, min(90, int(offer.get("discount_pct") or 0)))
    if discount_pct <= 0:
        return None
    return {
        "discount_pct": discount_pct,
        "expires_at": expires_at,
        "base_price_units": base_units_now,
    }


def _resolve_shop_price_for_user(session: Session, user: User, shop_id: str, item: dict, qty: int = 1) -> dict:
    base_per_item_units = _shop_item_total_gp_units(item)
    active_offer = _resolve_haggle_offer(session, user, shop_id, item)
    discount_pct = int((active_offer or {}).get("discount_pct") or 0)
    final_per_item_units = int(base_per_item_units * (1 - discount_pct / 100))
    final_per_item_units = max(0, final_per_item_units)
    quantity = _safe_int(qty, 1, minimum=1, maximum=999)
    return {
        "base_per_item_units": base_per_item_units,
        "final_per_item_units": final_per_item_units,
        "quantity": quantity,
        "base_total_units": base_per_item_units * quantity,
        "final_total_units": final_per_item_units * quantity,
        "haggle": {
            "active": bool(active_offer),
            "discount_pct": discount_pct,
            "expires_at": (active_offer or {}).get("expires_at"),
            "consumes_on_purchase": True,
        },
    }


def _build_shop_price_state(session: Session, user: User, shop: dict) -> dict[str, dict]:
    shop_id = str(shop.get("id") or "").strip()
    pricing: dict[str, dict] = {}
    for item in list(shop.get("inventory") or []):
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        quote = _resolve_shop_price_for_user(session, user, shop_id, item, qty=1)
        pricing[item_id] = {
            "base_price_units": quote["base_per_item_units"],
            "final_price_units": quote["final_per_item_units"],
            "haggle": quote["haggle"],
        }
    return pricing


def _find_player_token_for_user(session: Session, user: User):
    """Return the first non-staged token owned by this user, or None."""
    for token in session.tokens.values():
        if getattr(token, "owner_id", None) == user.id and not getattr(token, "staged", False):
            return token
    return None


def _prop_center(prop: dict) -> tuple[float, float]:
    """Return pixel center of a prop from its editor_props dict entry."""
    x = float(prop.get("x") or 0)
    y = float(prop.get("y") or 0)
    w = float(prop.get("w") or 1) * PX_PER_GRID
    h = float(prop.get("h") or 1) * PX_PER_GRID
    return (x + w / 2, y + h / 2)


def _find_prop_by_id(session: Session, prop_id: str) -> dict | None:
    """Search all map contexts for a prop with this id."""
    props_all = getattr(session, "editor_props", {}) or {}
    for ctx_items in props_all.values():
        if not isinstance(ctx_items, list):
            continue
        for p in ctx_items:
            if isinstance(p, dict) and p.get("id") == prop_id:
                return p
    return None


def _find_prop_context_and_data(session: Session, prop_id: str) -> tuple[str | None, dict | None]:
    """Search all map contexts and return both context key + prop dict."""
    props_all = getattr(session, "editor_props", {}) or {}
    for map_ctx, ctx_items in props_all.items():
        if not isinstance(ctx_items, list):
            continue
        for p in ctx_items:
            if isinstance(p, dict) and p.get("id") == prop_id:
                return str(map_ctx or "world"), p
    return None, None


def _shop_access_store(session: Session) -> dict:
    if not hasattr(session, "_shop_access_tokens") or not isinstance(session._shop_access_tokens, dict):
        session._shop_access_tokens = {}
    return session._shop_access_tokens


def _mark_shop_access(session: Session, user: User, shop: dict) -> None:
    shop_id = str(shop.get("id") or "").strip()
    if not shop_id:
        return
    _shop_access_store(session)[f"{user.id}:{shop_id}"] = {
        "expires_at": time.time() + _SHOP_ACCESS_TTL_SECONDS,
        "prop_id": str(shop.get("prop_id") or "").strip()[:48],
    }


def _has_recent_shop_access(session: Session, user: User, shop_id: str) -> bool:
    key = f"{user.id}:{shop_id}"
    entry = _shop_access_store(session).get(key)
    if not isinstance(entry, dict):
        return False
    if float(entry.get("expires_at") or 0) <= time.time():
        _shop_access_store(session).pop(key, None)
        return False
    return True


def _build_profession_state(campaign_id: str, user_id: str) -> dict:
    from server.db import get_player_professions, list_professions
    ids = list(get_player_professions(campaign_id, user_id) or [])
    catalog = list_professions()
    allowed = {str(item.get("id") or "") for item in catalog}
    ids = [pid for pid in ids if pid in allowed][:_PLAYER_MAX_PROFESSIONS]
    return {
        "catalog": catalog,
        "player_profession_ids": ids,
        "max_professions": _PLAYER_MAX_PROFESSIONS,
        "open_slots": max(0, _PLAYER_MAX_PROFESSIONS - len(ids)),
    }


def _inventory_material_count(session: Session, user: User, material_name: str) -> int:
    target = str(material_name or "").strip().lower()
    if not target:
        return 0
    _, _, mine = _get_player_inventory_store(session, user)
    total = 0
    for entry in mine:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "").strip().lower() != target:
            continue
        total += _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
    return total


def _consume_materials_now(session: Session, user: User, materials: list[dict]) -> tuple[bool, list[dict], str]:
    """Consume required material stacks immediately. Returns success + consumed log + error."""
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    requirements: list[dict] = []
    for raw in list(materials or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()[:80]
        qty = _safe_int(raw.get("qty"), 1, minimum=1, maximum=9999)
        if name:
            requirements.append({"name": name, "qty": qty})
    # Validate availability first.
    for req in requirements:
        have = sum(
            _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
            for entry in mine
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().lower() == req["name"].lower()
        )
        if have < req["qty"]:
            return False, [], f"Missing materials: {req['name']} ({have}/{req['qty']})."

    consumed: list[dict] = []
    for req in requirements:
        remaining = req["qty"]
        for entry in list(mine):
            if remaining <= 0:
                break
            if not isinstance(entry, dict):
                continue
            if str(entry.get("name") or "").strip().lower() != req["name"].lower():
                continue
            stack_qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
            take = min(stack_qty, remaining)
            left = stack_qty - take
            if left > 0:
                entry["qty"] = left
            else:
                mine.remove(entry)
            remaining -= take
            consumed.append({"name": req["name"], "qty": take})
    inventories[owner_key] = [entry for entry in (_normalize_player_inventory_entry(x) for x in mine) if entry]
    session.player_inventories = inventories
    return True, consumed, ""


def _decorate_craft_jobs(rows: list[dict], now_ts: float) -> list[dict]:
    out: list[dict] = []
    for row in list(rows or []):
        item = dict(row or {})
        status = str(item.get("status") or "crafting").strip().lower()
        if status == "collected":
            display = "collected"
        elif now_ts >= float(item.get("ready_at") or 0):
            display = "ready"
        else:
            display = "crafting"
        item["status_display"] = display
        out.append(item)
    return out


def _build_craft_state(session: Session, user: User, shop: dict) -> dict:
    from server.db import list_crafting_recipes, list_craft_jobs
    now_ts = time.time()
    campaign_cfg = _corpse_dm_config(session)
    if not campaign_cfg.get("crafting_enabled", True):
        return {"recipes": [], "jobs": [], "now": now_ts, "campaign_crafting_enabled": False, "locked_reason": "Crafting is disabled by the DM for this campaign."}
    learned = set(_build_profession_state(session.id, user.id).get("player_profession_ids") or [])
    shop_type = str(shop.get("shop_type") or "").strip().lower()
    recipes: list[dict] = []
    for recipe in list_crafting_recipes():
        required_prof = [str(x).strip().lower() for x in list(recipe.get("requires_professions_json") or []) if str(x).strip()]
        station_types = [str(x).strip().lower() for x in list(recipe.get("station_shop_types_json") or []) if str(x).strip()]
        locked_reason = ""
        if required_prof and not set(required_prof).issubset(learned):
            locked_reason = "missing_profession"
        elif station_types and shop_type not in station_types:
            locked_reason = "wrong_station"
        mats_view = []
        for mat in list(recipe.get("requires_materials_json") or []):
            if not isinstance(mat, dict):
                continue
            mat_name = str(mat.get("name") or "").strip()
            need_qty = _safe_int(mat.get("qty"), 1, minimum=1, maximum=9999)
            mats_view.append({
                "name": mat_name,
                "required_qty": need_qty,
                "owned_qty": _inventory_material_count(session, user, mat_name),
            })
        fee_units = max(0, int(recipe.get("fee_units") or 0))
        missing_mats = any(int(m.get("owned_qty") or 0) < int(m.get("required_qty") or 0) for m in mats_view)
        recipes.append({
            **recipe,
            "requires_materials_view": mats_view,
            "can_afford_fee": get_player_gold_for_user(session, user.id) >= fee_units,
            "locked_reason": locked_reason or ("missing_mats" if missing_mats else ""),
        })
    jobs = _decorate_craft_jobs(list_craft_jobs(session.id, user.id, shop_id=str(shop.get("id") or "")), now_ts)
    return {"recipes": recipes, "jobs": jobs, "now": now_ts, "campaign_crafting_enabled": True}


def _player_is_near_prop(session: Session, user: User, prop: dict, player_x: float = 0.0, player_y: float = 0.0) -> bool:
    if user.role == "dm":
        return True
    if not isinstance(prop, dict):
        return False
    px, py = _prop_center(prop)
    token = _find_player_token_for_user(session, user)
    if token:
        tx, ty = _token_center(token)
    else:
        tx, ty = float(player_x or 0), float(player_y or 0)
    dist = ((tx - px) ** 2 + (ty - py) ** 2) ** 0.5
    max_dist = PX_PER_GRID * 2.5  # 2 grid squares + small buffer
    return dist <= max_dist


async def handle_open_shop(payload: dict, session: Session, user: User):
    """Any role. Open a shop linked to a prop_id. Checks player proximity."""
    from server.db import get_shop_by_prop_id
    prop_id = str(payload.get("prop_id") or "").strip()[:48]
    player_x = float(payload.get("player_position_x") or 0)
    player_y = float(payload.get("player_position_y") or 0)
    if not prop_id:
        return await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "No shop specified."}
        })
    # Proximity check: player token must be within 2 grid squares of the prop
    if user.role != "dm":
        prop = _find_prop_by_id(session, prop_id)
        if prop and not _player_is_near_prop(session, user, prop, player_x, player_y):
            return await manager.send_to(session.id, user.id, {
                "type": "shop_data",
                "payload": {"error": "You are too far away from the shop."}
            })
    shop = get_shop_by_prop_id(session.id, prop_id)
    if not shop:
        return await manager.send_to(session.id, user.id, {
            "type": "shop_data",
            "payload": {"error": "No shopkeeper is present."}
        })
    if not shop.get("is_open"):
        return await manager.send_to(session.id, user.id, {
            "type": "shop_data",
            "payload": {"error": "This shop is currently closed."}
        })
    # Include player's gold balance
    player_gold_units = get_player_gold_for_user(session, user.id)
    _mark_shop_access(session, user, shop)
    profession_state = _build_profession_state(session.id, user.id)
    from server.db import resolve_shop_taught_profession_ids
    teachable_ids = resolve_shop_taught_profession_ids(shop)
    await manager.send_to(session.id, user.id, {
        "type": "shop_data",
        "payload": {
            "shop": shop,
            "price_state": _build_shop_price_state(session, user, shop),
            "player_gold_units": player_gold_units,
            "player_gold_label": _format_gold_units(player_gold_units),
            "campaign_economy": _corpse_dm_config(session),
            "profession_state": {
                **profession_state,
                "teachable_profession_ids": teachable_ids,
            },
            "craft_state": _build_craft_state(session, user, shop),
        }
    })


async def handle_dm_configure_shop(payload: dict, session: Session, user: User):
    """DM only. Create or update shop linked to a prop. Broadcasts shop_updated."""
    from server.db import upsert_shop
    if user.role != "dm":
        return
    prop_id = str(payload.get("prop_id") or "").strip()[:48]
    name = str(payload.get("name") or "Shop").strip()[:80] or "Shop"
    shopkeeper_name = str(payload.get("shopkeeper_name") or "Shopkeeper").strip()[:80] or "Shopkeeper"
    shop_type = str(payload.get("shop_type") or "general").strip()[:40]
    if shop_type not in {"general", "blacksmith", "alchemist", "magic", "black_market"}:
        shop_type = "general"
    description = str(payload.get("description") or "").strip()[:500]
    personality = str(payload.get("personality") or "friendly").strip().lower()[:40]
    if personality not in {"friendly", "gruff", "greedy", "shifty", "scholarly"}:
        personality = "friendly"
    dialogue_enabled = bool(payload.get("dialogue_enabled", True))
    voice = str(payload.get("voice") or "grand_narrator").strip()[:80] or "grand_narrator"
    tts_enabled = bool(payload.get("tts_enabled", False))
    greeting_override = str(payload.get("greeting_override") or "").strip()[:220]
    raw_inventory = payload.get("inventory")
    raw_taught = payload.get("taught_profession_ids")
    crafting_enabled = bool(payload.get("crafting_enabled", True))
    buy_categories = payload.get("buy_categories") if isinstance(payload.get("buy_categories"), list) else []
    if not isinstance(raw_inventory, list):
        raw_inventory = []
    if not isinstance(raw_taught, list):
        raw_taught = []
    inventory = []
    for item in raw_inventory:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("item_name") or "").strip()[:80]
        if not item_name:
            continue
        inventory.append({
            "item_name": item_name,
            "item_type": str(item.get("item_type") or "misc").strip()[:40],
            "description": str(item.get("description") or "").strip()[:500],
            "price_gp": max(0, int(item.get("price_gp") or 0)),
            "price_sp": max(0, int(item.get("price_sp") or 0)),
            "price_cp": max(0, int(item.get("price_cp") or 0)),
            "quantity": item.get("quantity"),  # None = unlimited
            "item_data": item.get("item_data") or {},
        })
    # Sell configuration
    raw_buy_rate = payload.get("buy_rate_pct")
    buy_rate_pct = max(5, min(95, int(raw_buy_rate or 50))) if raw_buy_rate is not None else 50
    raw_vendor_cash = payload.get("vendor_cash_units")
    vendor_cash_units = max(0, int(raw_vendor_cash)) if raw_vendor_cash is not None else None
    raw_accepted = payload.get("accepted_item_types")
    _valid_types = {"weapon", "armour", "consumable", "tool", "material", "trinket", "magic", "misc"}
    if isinstance(raw_accepted, list):
        accepted_item_types = [t for t in (str(x).strip().lower()[:40] for x in raw_accepted) if t in _valid_types]
    else:
        accepted_item_types = list(_valid_types)
    shop_sales_enabled = bool(payload.get("shop_sales_enabled", True))
    player_sell_enabled = bool(payload.get("player_sell_enabled", payload.get("selling_enabled", True)))
    buyback_enabled = bool(payload.get("buyback_enabled", False))
    shop = upsert_shop(
        session.id, prop_id, name, shopkeeper_name, shop_type, description, inventory,
        taught_profession_ids=raw_taught,
        crafting_enabled=crafting_enabled,
        shop_sales_enabled=shop_sales_enabled,
        player_sell_enabled=player_sell_enabled,
        buy_categories=buy_categories,
        vendor_cash_units=vendor_cash_units,
        buy_rate_pct=buy_rate_pct,
        accepted_item_types=accepted_item_types,
        buyback_enabled=buyback_enabled,
        personality=personality,
        dialogue_enabled=dialogue_enabled,
        voice=voice,
        tts_enabled=tts_enabled,
        greeting_override=greeting_override,
    )
    if not shop:
        return await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "Failed to save shop configuration."}
        })
    await manager.broadcast(session.id, {
        "type": "shop_updated",
        "payload": {"shop": shop}
    })


async def handle_dm_get_shop_config(payload: dict, session: Session, user: User):
    """DM only. Fetch persisted shop config for a prop without open/proximity checks."""
    from server.db import get_shop_by_prop_id
    if user.role != "dm":
        return
    prop_id = str(payload.get("prop_id") or "").strip()[:48]
    if not prop_id:
        return
    shop = get_shop_by_prop_id(session.id, prop_id)
    await manager.send_to(session.id, user.id, {
        "type": "dm_shop_config",
        "payload": {
            "prop_id": prop_id,
            "shop": shop or None,
        },
    })


async def handle_learn_profession(payload: dict, session: Session, user: User):
    from server.db import (
        get_shop_by_id,
        get_profession_by_id,
        get_player_professions,
        set_player_professions,
        resolve_shop_taught_profession_ids,
    )
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    profession_id = str(payload.get("profession_id") or "").strip().lower()[:40]
    replace_id = str(payload.get("replace_profession_id") or "").strip().lower()[:40]
    if not shop_id or not profession_id:
        return await manager.send_to(session.id, user.id, {
            "type": "profession_learn_result",
            "payload": {"success": False, "message": "Invalid profession request."}
        })

    shop = get_shop_by_id(shop_id)
    if not shop or not shop.get("is_open"):
        return await manager.send_to(session.id, user.id, {
            "type": "profession_learn_result",
            "payload": {"success": False, "message": "That merchant is not available."}
        })
    if user.role != "dm" and not _has_recent_shop_access(session, user, shop_id):
        return await manager.send_to(session.id, user.id, {
            "type": "profession_learn_result",
            "payload": {"success": False, "message": "Open the merchant shop first."}
        })
    if user.role != "dm":
        prop_id = str(shop.get("prop_id") or "").strip()[:48]
        prop = _find_prop_by_id(session, prop_id) if prop_id else None
        if prop and not _player_is_near_prop(session, user, prop):
            return await manager.send_to(session.id, user.id, {
                "type": "profession_learn_result",
                "payload": {"success": False, "message": "Move closer to the merchant to learn that profession."}
            })

    profession = get_profession_by_id(profession_id)
    if not profession:
        return await manager.send_to(session.id, user.id, {
            "type": "profession_learn_result",
            "payload": {"success": False, "message": "That profession does not exist."}
        })
    teachable = set(resolve_shop_taught_profession_ids(shop))
    if profession_id not in teachable:
        return await manager.send_to(session.id, user.id, {
            "type": "profession_learn_result",
            "payload": {"success": False, "message": "This merchant cannot teach that profession."}
        })

    current = list(get_player_professions(session.id, user.id) or [])
    changed = False
    if profession_id in current:
        msg = f"You already know {profession.get('name', 'that profession')}."
    elif len(current) < _PLAYER_MAX_PROFESSIONS:
        current.append(profession_id)
        changed = True
        msg = f"Learned {profession.get('name', 'profession')}."
    else:
        if not replace_id or replace_id not in current:
            return await manager.send_to(session.id, user.id, {
                "type": "profession_learn_result",
                "payload": {"success": False, "message": "Pick a profession to replace (max 2)."}
            })
        current = [profession_id if pid == replace_id else pid for pid in current]
        changed = True
        msg = f"Replaced {replace_id} with {profession.get('name', 'profession')}."

    if changed:
        current = set_player_professions(session.id, user.id, current)
        session.add_log(f"[Economy] {user.name} learned {profession.get('name', 'a profession')} at {shop.get('name', 'Shop')}.", "system", "System")

    state = _build_profession_state(session.id, user.id)
    state["teachable_profession_ids"] = list(teachable)
    await manager.send_to(session.id, user.id, {
        "type": "profession_learn_result",
        "payload": {
            "success": True,
            "message": msg,
            "profession_state": state,
            "craft_state": _build_craft_state(session, user, shop),
        }
    })


async def handle_start_craft_job(payload: dict, session: Session, user: User):
    from server.db import get_shop_by_id, get_crafting_recipe, create_craft_job
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    recipe_id = str(payload.get("recipe_id") or "").strip()[:80]
    if not shop_id or not recipe_id:
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "Invalid crafting request."}
        })
    if not _corpse_dm_config(session).get("crafting_enabled", True):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "Crafting is disabled by the DM for this campaign."}
        })
    shop = get_shop_by_id(shop_id)
    if not shop or not shop.get("is_open"):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "That crafting station is unavailable."}
        })
    if not bool(shop.get("crafting_enabled", True)):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "Crafting is disabled at this shop."}
        })
    if user.role != "dm" and not _has_recent_shop_access(session, user, shop_id):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "Open the merchant shop first."}
        })
    recipe = get_crafting_recipe(recipe_id)
    if not recipe:
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "Recipe not found."}
        })
    station_types = {str(x).strip().lower() for x in list(recipe.get("station_shop_types_json") or []) if str(x).strip()}
    if station_types and str(shop.get("shop_type") or "").strip().lower() not in station_types:
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "This station cannot craft that recipe."}
        })
    learned = set(_build_profession_state(session.id, user.id).get("player_profession_ids") or [])
    required_prof = {str(x).strip().lower() for x in list(recipe.get("requires_professions_json") or []) if str(x).strip()}
    if not required_prof.issubset(learned):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": "You do not meet this recipe's profession requirements."}
        })
    fee_units = max(0, int(recipe.get("fee_units") or 0))
    if fee_units > 0:
        ok, current_gold, _new_gold = _try_spend_gold(session, user, fee_units)
        if not ok:
            return await manager.send_to(session.id, user.id, {
                "type": "craft_job_result",
                "payload": {"success": False, "message": f"Not enough gold. Need {_format_gold_units(fee_units)}, have {_format_gold_units(current_gold)}."}
            })
    consumed_ok, consumed_inputs, err = _consume_materials_now(session, user, list(recipe.get("requires_materials_json") or []))
    if not consumed_ok:
        if fee_units > 0:
            set_player_gold_for_user(session, user.id, get_player_gold_for_user(session, user.id) + fee_units)
        return await manager.send_to(session.id, user.id, {
            "type": "craft_job_result",
            "payload": {"success": False, "message": err or "Missing crafting materials."}
        })
    now_ts = time.time()
    ready_at = now_ts + max(0, int(recipe.get("duration_seconds") or 0))
    result_json = dict(recipe.get("result_item_json") or {})
    created = create_craft_job(
        session.id, user.id, recipe_id, shop_id, now_ts, ready_at, "crafting",
        consumed_inputs, result_json, [{"at": now_ts, "event": "started"}],
    )
    if created:
        session.add_log(f"[Economy] {user.name} started craft: {recipe.get('name', 'item')} at {shop.get('name', 'Shop')}.", "system", "System")
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    craft_state = _build_craft_state(session, user, shop)
    await manager.send_to(session.id, user.id, {
        "type": "craft_job_result",
        "payload": {
            "success": bool(created),
            "message": f"Started crafting {recipe.get('name', 'item')}.",
            "job": created,
            "craft_state": craft_state,
            "player_gold_units": get_player_gold_for_user(session, user.id),
            "player_gold_label": _format_gold_units(get_player_gold_for_user(session, user.id)),
        }
    })


async def handle_collect_craft_job(payload: dict, session: Session, user: User):
    from server.db import get_shop_by_id, get_craft_job, update_craft_job_status
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    job_id = str(payload.get("job_id") or "").strip()[:40]
    if not shop_id or not job_id:
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "Invalid collect request."}
        })
    shop = get_shop_by_id(shop_id)
    if not shop or not shop.get("is_open"):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "That station is unavailable."}
        })
    if user.role != "dm" and not _has_recent_shop_access(session, user, shop_id):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "Open the merchant shop first."}
        })
    job = get_craft_job(job_id)
    if not job or str(job.get("campaign_id") or "") != session.id or str(job.get("user_id") or "") != user.id:
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "Craft job not found."}
        })
    if str(job.get("shop_id") or "") != shop_id:
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "Collect this job at the original station."}
        })
    if str(job.get("status") or "").lower() == "collected":
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "This craft job was already collected."}
        })
    now_ts = time.time()
    if now_ts < float(job.get("ready_at") or 0):
        return await manager.send_to(session.id, user.id, {
            "type": "craft_collect_result",
            "payload": {"success": False, "message": "This item is still crafting."}
        })
    result_entry = dict(job.get("result_json") or {})
    canonical_result = normalize_crafted_result_row(result_entry, recipe_id=str(job.get("recipe_id") or ""))
    result_entry = to_inventory_entry(canonical_result, notes=str(result_entry.get("notes") or ""), source_label="Crafting")
    item_name = str(result_entry.get("name") or "Crafted Item").strip()[:80]
    _add_item_to_player_inventory(session, user, result_entry, 1, source_name="Crafting")
    logs = list(job.get("logs_json") or [])
    logs.append({"at": now_ts, "event": "collected"})
    updated = update_craft_job_status(job_id, "collected", logs=logs)
    if updated:
        session.add_log(f"[Economy] {user.name} collected craft: {item_name}.", "system", "System")
    _recompute_equipment_effects(session, user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    craft_state = _build_craft_state(session, user, shop)
    await manager.send_to(session.id, user.id, {
        "type": "craft_collect_result",
        "payload": {
            "success": bool(updated),
            "message": f"Collected {item_name}.",
            "job": updated or job,
            "craft_state": craft_state,
            "player_gold_units": get_player_gold_for_user(session, user.id),
            "player_gold_label": _format_gold_units(get_player_gold_for_user(session, user.id)),
        }
    })


async def handle_purchase_item(payload: dict, session: Session, user: User):
    """Any player. Buy an item from a shop. Deducts gold, adds to inventory."""
    from server.db import get_shop_by_id, decrement_shop_item, record_shop_transaction
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    item_id = str(payload.get("item_id") or "").strip()[:32]
    qty = _safe_int(payload.get("quantity"), 1, minimum=1, maximum=99)
    if not shop_id or not item_id:
        return await _send_inventory_action_result(session, user.id, "Invalid purchase request.")
    shop = get_shop_by_id(shop_id)
    if not shop:
        return await _send_inventory_action_result(session, user.id, "That shop no longer exists.")
    if not shop.get("shop_sales_enabled", True):
        return await _send_inventory_action_result(session, user.id, "This shop is not selling items right now.")
    item = next((i for i in shop.get("inventory", []) if i["id"] == item_id), None)
    if not item:
        return await _send_inventory_action_result(session, user.id, "That item is no longer available.")
    # Check stock
    current_qty = item.get("quantity")
    if current_qty is not None and current_qty < qty:
        qty = current_qty
    if current_qty is not None and current_qty <= 0:
        return await _send_inventory_action_result(session, user.id, "That item is sold out.")
    # Calculate cost from server-authoritative price state only.
    # Never trust any client-provided discount values.
    quote = _resolve_shop_price_for_user(session, user, shop_id, item, qty=qty)
    total_cost_units = int(quote["final_total_units"])
    had_haggle_discount = bool((quote.get("haggle") or {}).get("active"))
    if total_cost_units > 0:
        ok, current_gold, new_gold = _try_spend_gold(session, user, total_cost_units)
        if not ok:
            return await _send_inventory_action_result(
                session, user.id,
                f"Not enough gold. Need {_format_gold_units(total_cost_units)}, have {_format_gold_units(current_gold)}."
            )
    else:
        new_gold = get_player_gold_for_user(session, user.id)
    # Decrement stock
    if current_qty is not None:
        decrement_shop_item(item_id, qty)
    # Record transaction
    price_paid_gp = (total_cost_units // 100) if total_cost_units > 0 else 0
    record_shop_transaction(shop_id, user.id, item_id, qty, price_paid_gp)
    # Add to player inventory (preserve item_type for sell valuation)
    canonical_shop_item = normalize_shop_item_row(item)
    item_name = str(canonical_shop_item.get("identity", {}).get("name") or item.get("item_name") or "Item").strip()
    price_label = _format_gold_units(total_cost_units) if total_cost_units > 0 else ""
    shop_inventory_entry = to_inventory_entry(
        canonical_shop_item,
        notes=str(item.get("description") or ""),
        source_label=str(shop.get("name", "Shop") or "Shop"),
        price_label=price_label,
    )
    _add_item_to_player_inventory(session, user, shop_inventory_entry, qty, source_name=shop.get("name", "Shop"), price=price_label)
    # Record purchase in session buy-log for anti-resale checks
    _record_buy_in_log(session, user, shop_id, item_name, _shop_item_total_gp_units(item))
    _recompute_equipment_effects(session, user)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "buy",
        "item_name": item_name,
        "qty": qty,
        "price": price_label,
        "source_name": shop.get("name", "Shop"),
        "source_kind": "shop",
    })
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    if had_haggle_discount:
        _clear_haggle_offer(session, user.id, shop_id, item_id)
    session.add_log(f"[Economy] {user.name} bought item: {qty}× {item_name} from {shop.get('name', 'Shop')} for {price_label or '0 gp'}.", "system", "System")
    # Refresh shop data for all session clients
    updated_shop = get_shop_by_id(shop_id)
    player_gold_units = get_player_gold_for_user(session, user.id)
    if updated_shop:
        await manager.broadcast(session.id, {
            "type": "shop_inventory_updated",
            "payload": {
                "shop_id": shop_id,
                "inventory": updated_shop.get("inventory", []),
            }
        })
    # Notify DM
    if session.dm_id:
        await manager.send_to(session.id, session.dm_id, {
            "type": "shop_purchase",
            "payload": {
                "buyer_name": user.name,
                "item_name": item_name,
                "quantity": qty,
                "price": price_label,
                "shop_name": shop.get("name", "Shop"),
            }
        })
    # Confirm to buyer
    suffix = f" for {price_label}" if price_label else ""
    msg = f"Bought {qty}× {item_name}{suffix}." if qty != 1 else f"Bought {item_name}{suffix}."
    if total_cost_units > 0:
        msg += f" {_format_gold_units(new_gold)} remaining."
    await manager.send_to(session.id, user.id, {
        "type": "purchase_result",
        "payload": {
            "success": True,
            "message": msg,
            "price_quote": quote,
            "price_state": _build_shop_price_state(session, user, updated_shop or shop),
            "haggle_consumed": had_haggle_discount,
            "shop_id": shop_id,
            "item_id": item_id,
            "player_gold_units": player_gold_units,
            "player_gold_label": _format_gold_units(player_gold_units),
        }
    })


# Haggle cooldown store: session_id → { shop_id → expiry_timestamp }
_haggle_annoyed: dict[str, dict[str, float]] = {}


async def handle_haggle_item(payload: dict, session: Session, user: User):
    """Any player. Roll d20 + charisma_modifier vs DC 15/20 for a discount."""
    from server.db import get_shop_by_id
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    item_id = str(payload.get("item_id") or "").strip()[:32]
    cha_mod = _safe_int(payload.get("charisma_modifier"), 0, minimum=-5, maximum=10)
    if not shop_id or not item_id:
        return await manager.send_to(session.id, user.id, {
            "type": "haggle_result",
            "payload": {"error": "Invalid haggle request."}
        })
    # Check if shopkeeper is annoyed at this player
    annoyed_key = f"{session.id}:{user.id}"
    session_annoyed = _haggle_annoyed.get(annoyed_key, {})
    if session_annoyed.get(shop_id, 0) > time.time():
        remaining = int(session_annoyed[shop_id] - time.time())
        return await manager.send_to(session.id, user.id, {
            "type": "haggle_result",
            "payload": {
                "success": False,
                "roll": 0,
                "discount_pct": 0,
                "new_price_gp": None,
                "flavor_text": f"The shopkeeper eyes you coldly. \"I told you, the price is the price. Come back in {remaining // 60 + 1} minutes.\"",
                "annoyed": True,
            }
        })
    shop = get_shop_by_id(shop_id)
    if not shop:
        return await manager.send_to(session.id, user.id, {
            "type": "haggle_result", "payload": {"error": "Shop not found."}
        })
    if not shop.get("shop_sales_enabled", True):
        return await manager.send_to(session.id, user.id, {
            "type": "haggle_result", "payload": {"error": "This shop is not selling items right now."}
        })
    item = next((i for i in shop.get("inventory", []) if i["id"] == item_id), None)
    if not item:
        return await manager.send_to(session.id, user.id, {
            "type": "haggle_result", "payload": {"error": "Item not found."}
        })
    roll = random.randint(1, 20)
    total = roll + cha_mod
    per_item_units = _shop_item_total_gp_units(item)
    item_name = str(item.get("item_name") or "Item").strip()
    HAGGLE_FLAVORS = {
        "success_20": [
            f"The {shop.get('shopkeeper_name','shopkeeper')} grins. \"You drive a hard bargain! Fine, 20% off.\"",
            "\"Ah, a true negotiator! You've earned a real discount.\"",
            f"\"Very well,\" the {shop.get('shopkeeper_name','shopkeeper')} sighs dramatically. \"Twenty percent — don't tell my other customers.\"",
        ],
        "success_15": [
            f"The {shop.get('shopkeeper_name','shopkeeper')} rubs their chin. \"I suppose I can knock a bit off...\"",
            "\"You're a persuasive one. Ten percent — that's my best offer.\"",
            "\"Fine, fine. A small discount for a worthy customer.\"",
        ],
        "fail": [
            f"The {shop.get('shopkeeper_name','shopkeeper')} crosses their arms. \"My prices are fair. Take it or leave it.\"",
            "\"Nice try. The price stands.\"",
            f"\"I've heard better bargains from a goblin,\" {shop.get('shopkeeper_name','the shopkeeper')} scoffs.",
            "\"Don't push your luck. The price is the price.\"",
        ],
    }
    offer = None
    if total >= 20:
        discount_pct = 20
        new_units = int(per_item_units * 0.80)
        new_price_gp = new_units / 100
        flavor = random.choice(HAGGLE_FLAVORS["success_20"])
        success = True
    elif total >= 15:
        discount_pct = 10
        new_units = int(per_item_units * 0.90)
        new_price_gp = new_units / 100
        flavor = random.choice(HAGGLE_FLAVORS["success_15"])
        success = True
    else:
        discount_pct = 0
        new_price_gp = per_item_units / 100
        flavor = random.choice(HAGGLE_FLAVORS["fail"])
        success = False
        # Mark shopkeeper as annoyed for 10 minutes
        if annoyed_key not in _haggle_annoyed:
            _haggle_annoyed[annoyed_key] = {}
        _haggle_annoyed[annoyed_key][shop_id] = time.time() + 600
    if success and discount_pct > 0:
        offer = _set_haggle_offer(session, user, shop_id, item_id, discount_pct=discount_pct, base_price_units=per_item_units)
        session.add_log(f"[Economy] {user.name} haggled successfully for {item_name} ({discount_pct}% off).", "system", "System")
    else:
        _clear_haggle_offer(session, user.id, shop_id, item_id)
    quote = _resolve_shop_price_for_user(session, user, shop_id, item, qty=1)
    await manager.send_to(session.id, user.id, {
        "type": "haggle_result",
        "payload": {
            "success": success,
            "roll": roll,
            "modifier": cha_mod,
            "total": total,
            "discount_pct": discount_pct,
            "new_price_gp": new_price_gp,
            "original_price_units": per_item_units,
            "new_price_units": int(per_item_units * (1 - discount_pct / 100)),
            "price_quote": quote,
            "item_name": item_name,
            "item_id": item_id,
            "flavor_text": flavor,
            "annoyed": not success,
            "haggle_expires_at": (offer or {}).get("expires_at"),
            "shop_id": shop_id,
        }
    })


# ─── Sell flow helpers ───────────────────────────────────────────────────────

def _buy_log_store(session: Session) -> dict:
    """Session-level record of player shop purchases: {user_id: [entries]}."""
    if not hasattr(session, "_shop_buy_log") or not isinstance(session._shop_buy_log, dict):
        session._shop_buy_log = {}
    return session._shop_buy_log


def _record_buy_in_log(session: Session, user: User, shop_id: str, item_name: str, base_price_units: int) -> None:
    log = _buy_log_store(session)
    key = user.id
    if key not in log:
        log[key] = []
    log[key].append({
        "item_name": item_name.strip().lower(),
        "shop_id": str(shop_id),
        "base_price_units": int(base_price_units),
        "bought_at": time.time(),
    })
    if len(log[key]) > 200:
        log[key] = log[key][-200:]


def _get_recent_buy(session: Session, user: User, shop_id: str, item_name: str) -> dict | None:
    """Return the most recent buy-log entry for this item from this shop within lockout period."""
    entries = _buy_log_store(session).get(user.id, [])
    now = time.time()
    name_lower = item_name.strip().lower()
    for entry in reversed(entries):
        if (str(entry.get("shop_id") or "") == str(shop_id)
                and str(entry.get("item_name") or "") == name_lower
                and now - float(entry.get("bought_at") or 0) < _RESALE_LOCKOUT_SECONDS):
            return entry
    return None


def _sell_haggle_store(session: Session) -> dict:
    """Session-level sell haggle offer cache."""
    if not hasattr(session, "_sell_haggle_offers") or not isinstance(session._sell_haggle_offers, dict):
        session._sell_haggle_offers = {}
    return session._sell_haggle_offers


def _set_sell_haggle_offer(session: Session, user: User, shop_id: str, item_name: str,
                            bonus_pct: int, base_offer_units: int) -> dict:
    key = f"{user.id}:{shop_id}:{item_name.strip().lower()}"
    offer = {
        "bonus_pct": int(bonus_pct),
        "base_offer_units": int(base_offer_units),
        "expires_at": time.time() + _SELL_HAGGLE_TTL_SECONDS,
    }
    _sell_haggle_store(session)[key] = offer
    return offer


def _resolve_sell_haggle_offer(session: Session, user: User, shop_id: str,
                                item_name: str, base_offer_units: int) -> dict | None:
    key = f"{user.id}:{shop_id}:{item_name.strip().lower()}"
    offer = _sell_haggle_store(session).get(key)
    if not offer:
        return None
    if float(offer.get("expires_at") or 0) <= time.time():
        _sell_haggle_store(session).pop(key, None)
        return None
    if int(offer.get("base_offer_units") or -1) != int(base_offer_units):
        _sell_haggle_store(session).pop(key, None)
        return None
    bonus_pct = max(0, min(30, int(offer.get("bonus_pct") or 0)))
    if bonus_pct <= 0:
        return None
    return {"bonus_pct": bonus_pct, "expires_at": float(offer.get("expires_at") or 0)}


def _clear_sell_haggle_offer(session: Session, user_id: str, shop_id: str, item_name: str) -> None:
    key = f"{user_id}:{shop_id}:{item_name.strip().lower()}"
    _sell_haggle_store(session).pop(key, None)


def _item_base_sell_ref(item_type: str, purchase_ref_units: int | None, entry: dict | None = None) -> int:
    """Return item reference value in copper units for sell offer calculation.

    Preference order:
    1) authoritative recent-purchase price from shop logs
    2) per-item value parsed from the inventory row itself
    3) category fallback defaults
    """
    if purchase_ref_units is not None and purchase_ref_units > 0:
        return int(purchase_ref_units)
    if isinstance(entry, dict):
        explicit_units = entry.get("base_price_units")
        try:
            explicit_units = int(explicit_units) if explicit_units is not None else 0
        except Exception:
            explicit_units = 0
        if explicit_units > 0:
            qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
            return max(1, int(round(explicit_units / max(1, qty))))
        gp_value = entry.get("gp_value")
        try:
            gp_units = int(round(float(gp_value) * 100)) if gp_value is not None else 0
        except Exception:
            gp_units = 0
        if gp_units > 0:
            qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
            return max(1, int(round(gp_units / max(1, qty))))
        parsed_price_units = _parse_gold_to_units(entry.get("price"))
        if parsed_price_units and parsed_price_units > 0:
            qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
            return max(1, int(round(parsed_price_units / max(1, qty))))
    return _CATEGORY_SELL_DEFAULTS.get(str(item_type or "misc").strip().lower(), 50)


def _compute_sell_offer_units(base_ref_units: int, buy_rate_pct: int, bonus_pct: int = 0) -> int:
    """Server-authoritative sell offer. buy_rate_pct applied, then optional haggle bonus."""
    rate = max(5, min(95, int(buy_rate_pct)))
    base_offer = int(base_ref_units * rate / 100)
    if bonus_pct > 0:
        capped_bonus = max(0, min(30, int(bonus_pct)))
        base_offer = int(base_offer * (1 + capped_bonus / 100))
    return max(1, base_offer)


async def handle_get_sell_offers(payload: dict, session: Session, user: User):
    """Player requests sell offer listing for a shop. Returns server-computed offers."""
    from server.db import get_shop_by_id
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    if not shop_id:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_offers", "payload": {"error": "No shop specified."}
        })
    shop = get_shop_by_id(shop_id)
    if not shop or not shop.get("is_open"):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_offers", "payload": {"error": "Shop not available."}
        })
    if not shop.get("player_sell_enabled", shop.get("selling_enabled", True)):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_offers",
            "payload": {"selling_enabled": False, "offers": [], "shop_id": shop_id}
        })
    if user.role != "dm" and not _has_recent_shop_access(session, user, shop_id):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_offers", "payload": {"error": "Open the shop first."}
        })

    accepted_types = set(shop.get("accepted_item_types_json") or list(_DEFAULT_ACCEPTED_TYPES))
    buy_rate_pct = max(5, min(95, int(shop.get("buy_rate_pct") or 50)))
    vendor_cash = shop.get("vendor_cash_units")
    buyback_enabled = bool(shop.get("buyback_enabled", False))

    _, _, mine = _get_player_inventory_store(session, user)
    offers = []
    for entry in mine:
        if not isinstance(entry, dict):
            continue
        item_name = str(entry.get("name") or "").strip()
        if not item_name:
            continue
        qty = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
        item_type = str(entry.get("item_type") or "misc").strip().lower()
        accepted = item_type in accepted_types
        recent_buy = _get_recent_buy(session, user, shop_id, item_name)
        resale_locked = (recent_buy is not None) and not buyback_enabled
        purchase_ref = int(recent_buy.get("base_price_units", 0)) if recent_buy else 0
        base_ref = _item_base_sell_ref(item_type, purchase_ref if purchase_ref > 0 else None, entry)
        base_offer = _compute_sell_offer_units(base_ref, buy_rate_pct)
        haggle_offer = _resolve_sell_haggle_offer(session, user, shop_id, item_name, base_offer)
        haggle_bonus = int((haggle_offer or {}).get("bonus_pct", 0))
        final_offer = _compute_sell_offer_units(base_ref, buy_rate_pct, haggle_bonus)
        offers.append({
            "item_name": item_name,
            "qty": qty,
            "item_type": item_type,
            "accepted": accepted,
            "resale_locked": resale_locked,
            "base_offer_units": base_offer,
            "final_offer_units": final_offer,
            "haggle": {
                "active": bool(haggle_offer),
                "bonus_pct": haggle_bonus,
                "expires_at": (haggle_offer or {}).get("expires_at"),
            },
        })

    await manager.send_to(session.id, user.id, {
        "type": "sell_offers",
        "payload": {
            "shop_id": shop_id,
            "selling_enabled": True,
            "buy_rate_pct": buy_rate_pct,
            "vendor_cash_units": vendor_cash,
            "accepted_item_types": list(accepted_types),
            "offers": offers,
        }
    })


async def handle_sell_item(payload: dict, session: Session, user: User):
    """Player sells an item to the shop. Server computes offer; client value is ignored."""
    from server.db import get_shop_by_id, record_shop_sell_transaction, update_vendor_cash
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    item_name = str(payload.get("item_name") or "").strip()[:80]
    qty = _safe_int(payload.get("quantity"), 1, minimum=1, maximum=99)
    if not shop_id or not item_name:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result", "payload": {"success": False, "message": "Invalid sell request."}
        })

    shop = get_shop_by_id(shop_id)
    if not shop or not shop.get("is_open"):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result", "payload": {"success": False, "message": "That shop is not available."}
        })
    if not shop.get("player_sell_enabled", shop.get("selling_enabled", True)):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result", "payload": {"success": False, "message": "This shop does not buy items."}
        })
    if user.role != "dm" and not _has_recent_shop_access(session, user, shop_id):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result", "payload": {"success": False, "message": "Open the shop first."}
        })

    accepted_types = set(shop.get("accepted_item_types_json") or list(_DEFAULT_ACCEPTED_TYPES))
    buy_rate_pct = max(5, min(95, int(shop.get("buy_rate_pct") or 50)))
    vendor_cash = shop.get("vendor_cash_units")
    buyback_enabled = bool(shop.get("buyback_enabled", False))

    # Find item in player inventory
    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    target_entry = next(
        (e for e in mine if str(e.get("name") or "").strip().lower() == item_name.lower()),
        None
    )
    if not target_entry:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result",
            "payload": {"success": False, "message": f"'{item_name}' not found in your inventory."}
        })

    have_qty = _safe_int(target_entry.get("qty"), 1, minimum=1, maximum=9999)
    if have_qty < qty:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result",
            "payload": {"success": False, "message": f"You only have {have_qty}× {item_name}."}
        })

    item_type = str(target_entry.get("item_type") or "misc").strip().lower()

    # Anti-exploit: category acceptance
    if item_type not in accepted_types:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result",
            "payload": {"success": False, "message": f"This shop does not accept {item_type} items."}
        })

    # Anti-exploit: resale lockout — block selling back to same shop within lockout period
    recent_buy = _get_recent_buy(session, user, shop_id, item_name)
    if recent_buy is not None and not buyback_enabled:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result",
            "payload": {"success": False, "message": "Cannot sell a recently purchased item back to the same shop (buyback not enabled)."}
        })

    # Server-authoritative offer — client-supplied price values are never used
    purchase_ref = int(recent_buy.get("base_price_units", 0)) if recent_buy else 0
    base_ref = _item_base_sell_ref(item_type, purchase_ref if purchase_ref > 0 else None, target_entry)
    base_offer = _compute_sell_offer_units(base_ref, buy_rate_pct)

    # Check for active sell haggle (bonus)
    haggle_offer = _resolve_sell_haggle_offer(session, user, shop_id, item_name, base_offer)
    haggle_bonus = int((haggle_offer or {}).get("bonus_pct", 0))
    per_unit_offer = _compute_sell_offer_units(base_ref, buy_rate_pct, haggle_bonus)
    total_offer = per_unit_offer * qty

    # Anti-exploit: vendor cash cap
    if vendor_cash is not None:
        vendor_cash_int = max(0, int(vendor_cash))
        if total_offer > vendor_cash_int:
            return await manager.send_to(session.id, user.id, {
                "type": "sell_result",
                "payload": {
                    "success": False,
                    "message": (
                        f"The merchant doesn't have enough coin. "
                        f"(Has {_format_gold_units(vendor_cash_int)}, "
                        f"needs {_format_gold_units(total_offer)})"
                    )
                }
            })

    # Remove item from inventory
    fresh_inventories, fresh_key, fresh_mine = _get_player_inventory_store(session, user)
    removed = False
    for entry in list(fresh_mine):
        if str(entry.get("name") or "").strip().lower() == item_name.lower():
            have = _safe_int(entry.get("qty"), 1, minimum=1, maximum=9999)
            new_qty = have - qty
            if new_qty <= 0:
                fresh_mine.remove(entry)
            else:
                entry["qty"] = new_qty
            removed = True
            break
    if not removed:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_result",
            "payload": {"success": False, "message": "Item no longer in inventory."}
        })
    fresh_inventories[fresh_key] = [
        e for e in (_normalize_player_inventory_entry(x) for x in fresh_mine) if e
    ]
    session.player_inventories = fresh_inventories

    # Add gold to player
    new_gold = get_player_gold_for_user(session, user.id) + total_offer
    set_player_gold_for_user(session, user.id, new_gold)

    # Reduce vendor cash if tracked
    if vendor_cash is not None:
        update_vendor_cash(shop_id, int(vendor_cash) - total_offer)

    # Record transaction
    record_shop_sell_transaction(
        shop_id, user.id, item_name, qty, total_offer // 100 if total_offer > 0 else 0
    )

    # Consume haggle offer
    if haggle_offer:
        _clear_sell_haggle_offer(session, user.id, shop_id, item_name)

    price_label = _format_gold_units(total_offer)
    _append_party_loot_log(session, {
        "player_name": user.name,
        "player_role": user.role,
        "action": "sell",
        "item_name": item_name,
        "qty": qty,
        "price": price_label,
        "source_name": shop.get("name", "Shop"),
        "source_kind": "shop",
    })
    _recompute_equipment_effects(session, user)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)

    player_gold_units = get_player_gold_for_user(session, user.id)
    suffix = f" for {price_label}" if total_offer > 0 else ""
    msg = (f"Sold {qty}× {item_name}{suffix}." if qty != 1 else f"Sold {item_name}{suffix}.")
    msg += f" {_format_gold_units(player_gold_units)} remaining."

    if session.dm_id:
        await manager.send_to(session.id, session.dm_id, {
            "type": "shop_sale",
            "payload": {
                "seller_name": user.name,
                "item_name": item_name,
                "quantity": qty,
                "price": price_label,
                "shop_name": shop.get("name", "Shop"),
            }
        })

    await manager.send_to(session.id, user.id, {
        "type": "sell_result",
        "payload": {
            "success": True,
            "message": msg,
            "item_name": item_name,
            "quantity": qty,
            "offer_units": total_offer,
            "haggle_consumed": bool(haggle_offer),
            "player_gold_units": player_gold_units,
            "player_gold_label": _format_gold_units(player_gold_units),
        }
    })


async def handle_haggle_sell_item(payload: dict, session: Session, user: User):
    """Player haggles for a better sell price. Server resolves via d20 + CHA modifier."""
    from server.db import get_shop_by_id
    if user.role not in {"dm", "player"}:
        return
    shop_id = str(payload.get("shop_id") or "").strip()[:32]
    item_name = str(payload.get("item_name") or "").strip()[:80]
    cha_mod = _safe_int(payload.get("charisma_modifier"), 0, minimum=-5, maximum=10)
    if not shop_id or not item_name:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_haggle_result", "payload": {"error": "Invalid haggle request."}
        })

    # Sell haggle uses a separate annoy key (sell:<shop_id>) to avoid cross-polluting buy haggle
    annoyed_key = f"{session.id}:{user.id}"
    session_annoyed = _haggle_annoyed.get(annoyed_key, {})
    sell_annoy_key = f"sell:{shop_id}"
    if session_annoyed.get(sell_annoy_key, 0) > time.time():
        remaining = int(session_annoyed[sell_annoy_key] - time.time())
        return await manager.send_to(session.id, user.id, {
            "type": "sell_haggle_result",
            "payload": {
                "success": False, "roll": 0, "bonus_pct": 0,
                "flavor_text": f"The merchant shakes their head. \"My offer stands. Come back in {remaining // 60 + 1} minutes.\"",
                "annoyed": True,
            }
        })

    shop = get_shop_by_id(shop_id)
    if not shop or not shop.get("is_open") or not shop.get("player_sell_enabled", shop.get("selling_enabled", True)):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_haggle_result", "payload": {"error": "Shop unavailable for selling."}
        })
    if user.role != "dm" and not _has_recent_shop_access(session, user, shop_id):
        return await manager.send_to(session.id, user.id, {
            "type": "sell_haggle_result", "payload": {"error": "Open the shop first."}
        })

    _, _, mine = _get_player_inventory_store(session, user)
    target_entry = next(
        (e for e in mine if str(e.get("name") or "").strip().lower() == item_name.lower()),
        None
    )
    if not target_entry:
        return await manager.send_to(session.id, user.id, {
            "type": "sell_haggle_result",
            "payload": {"error": f"Item '{item_name}' not found in inventory."}
        })

    buy_rate_pct = max(5, min(95, int(shop.get("buy_rate_pct") or 50)))
    item_type = str(target_entry.get("item_type") or "misc").strip().lower()
    recent_buy = _get_recent_buy(session, user, shop_id, item_name)
    purchase_ref = int(recent_buy.get("base_price_units", 0)) if recent_buy else 0
    base_ref = _item_base_sell_ref(item_type, purchase_ref if purchase_ref > 0 else None, target_entry)
    base_offer = _compute_sell_offer_units(base_ref, buy_rate_pct)

    roll = random.randint(1, 20)
    total = roll + cha_mod
    keeper = str(shop.get("shopkeeper_name") or "the merchant")

    SELL_FLAVORS = {
        "success_20": [
            f"\"{keeper} lets out a low whistle. 'Fine — I'll add a bit more. You know your goods.'\"",
            f"\"Very well,\" {keeper} relents. \"Twenty percent more. Don't tell my competitors.\"",
        ],
        "success_15": [
            f"{keeper} squints appraisingly. \"I can round up a little — fair enough.\"",
            "\"You clearly know the value. I'll add ten percent — best I can do.\"",
        ],
        "fail": [
            f"\"That's my price,\" {keeper} says flatly. \"Take it or keep it.\"",
            "\"You overestimate what this fetches around here.\"",
            "\"Nice try. My offer stands.\"",
        ],
    }

    if total >= 20:
        bonus_pct = 20
        flavor = random.choice(SELL_FLAVORS["success_20"])
        success = True
    elif total >= 15:
        bonus_pct = 10
        flavor = random.choice(SELL_FLAVORS["success_15"])
        success = True
    else:
        bonus_pct = 0
        flavor = random.choice(SELL_FLAVORS["fail"])
        success = False
        if annoyed_key not in _haggle_annoyed:
            _haggle_annoyed[annoyed_key] = {}
        _haggle_annoyed[annoyed_key][sell_annoy_key] = time.time() + 600

    offer_meta = None
    if success and bonus_pct > 0:
        offer_meta = _set_sell_haggle_offer(session, user, shop_id, item_name, bonus_pct, base_offer)
    else:
        _clear_sell_haggle_offer(session, user.id, shop_id, item_name)

    final_offer = _compute_sell_offer_units(base_ref, buy_rate_pct, bonus_pct if success else 0)

    await manager.send_to(session.id, user.id, {
        "type": "sell_haggle_result",
        "payload": {
            "success": success,
            "roll": roll,
            "modifier": cha_mod,
            "total": total,
            "bonus_pct": bonus_pct,
            "base_offer_units": base_offer,
            "final_offer_units": final_offer,
            "item_name": item_name,
            "flavor_text": flavor,
            "annoyed": not success,
            "haggle_expires_at": (offer_meta or {}).get("expires_at"),
            "shop_id": shop_id,
        }
    })


# ─── Corpse search / harvest handlers ────────────────────────────────────────

def _corpse_dm_config(session: Session) -> dict:
    base = {
        "search_attempts_per_corpse": 2,
        "harvest_attempts_per_corpse": 1,
        "allow_humanoid_salvage": False,
        "fail_by_5_consequence": False,
        "reward_destination": "player_inventory",
        "crafting_enabled": True,
        "selling_enabled": True,
    }
    cfg = dict(getattr(session, "corpse_dm_config", {}) or {})
    out = {
        "search_attempts_per_corpse": max(1, min(5, int(cfg.get("search_attempts_per_corpse", base["search_attempts_per_corpse"]) or base["search_attempts_per_corpse"]))),
        "harvest_attempts_per_corpse": max(1, min(5, int(cfg.get("harvest_attempts_per_corpse", base["harvest_attempts_per_corpse"]) or base["harvest_attempts_per_corpse"]))),
        "allow_humanoid_salvage": bool(cfg.get("allow_humanoid_salvage", base["allow_humanoid_salvage"])),
        "fail_by_5_consequence": bool(cfg.get("fail_by_5_consequence", base["fail_by_5_consequence"])),
        "reward_destination": str(cfg.get("reward_destination", base["reward_destination"]) or base["reward_destination"]).strip().lower(),
        "crafting_enabled": bool(cfg.get("crafting_enabled", base["crafting_enabled"])),
        "selling_enabled": bool(cfg.get("selling_enabled", base["selling_enabled"])),
    }
    if out["reward_destination"] not in {"player_inventory", "party_treasury"}:
        out["reward_destination"] = "player_inventory"
    session.corpse_dm_config = out
    return out


def _cr_from_text(raw) -> float:
    text = str(raw or "").strip().lower()
    if not text:
        return 0.0
    if "/" in text:
        left, right = text.split("/", 1)
        try:
            num = float(left)
            den = float(right or 1.0)
            if den > 0:
                return max(0.0, num / den)
        except Exception:
            return 0.0
    try:
        return max(0.0, float(text))
    except Exception:
        return 0.0


def _corpse_ref(corpse: dict) -> dict:
    return dict(corpse.get("creature_ref") or {})


def _action_dc(action: str, creature_ref: dict) -> int:
    cr_val = _cr_from_text(creature_ref.get("cr_num", creature_ref.get("cr", 0)))
    if action == "search":
        return min(20, 10 + int((cr_val + 1) // 2))
    return min(22, 12 + int((cr_val + 1) // 2))


def _harvest_table(monster_type: str, cr_num: float) -> list[dict]:
    mtype = str(monster_type or "").strip().lower()
    band = 0 if cr_num <= 4 else 1 if cr_num <= 10 else 2
    tables = {
        "beast": [
            {"name": "Hide Scraps", "qty": [1, 2], "notes": "usable tanning material"},
            {"name": "Bone Shards", "qty": [1, 2], "notes": "crafting material"},
        ],
        "dragon": [
            {"name": "Drake Scale", "qty": [1, 2], "notes": "rare armor component"},
            {"name": "Serrated Fang", "qty": [1, 1], "notes": "weapon crafting"},
        ],
        "undead": [
            {"name": "Necrotic Residue", "qty": [1, 2], "notes": "unstable reagent"},
            {"name": "Grave Dust", "qty": [1, 3], "notes": "ritual component"},
        ],
        "monstrosity": [
            {"name": "Chitin Plate", "qty": [1, 2], "notes": "reinforcement material"},
            {"name": "Predator Tendon", "qty": [1, 2], "notes": "bowstring grade"},
        ],
        "humanoid": [
            {"name": "Stitched Leather", "qty": [1, 1], "notes": "salvaged armor scraps"},
        ],
        "default": [
            {"name": "Monster Parts", "qty": [1, 2], "notes": "generic alchemical parts"},
            {"name": "Trophy Fragment", "qty": [1, 1], "notes": "proof of kill"},
        ],
    }
    base = list(tables.get(mtype) or tables["default"])
    if band >= 1:
        base.append({"name": "Rare Organ", "qty": [1, 1], "notes": "valuable to alchemists"})
    if band >= 2:
        base.append({"name": "Pristine Core", "qty": [1, 1], "notes": "high-grade crafting catalyst"})
    return base


def _search_table(cr_num: float) -> list[dict]:
    table = [
        {"kind": "coins", "gp": [1, max(4, int(4 + cr_num * 1.5))]},
        {"kind": "item", "name": "Trinket", "qty": [1, 1], "notes": "odd keepsake"},
    ]
    if cr_num >= 5:
        table.append({"kind": "item", "name": "Minor Consumable", "qty": [1, 1], "notes": "single-use find"})
    if cr_num >= 11:
        table.append({"kind": "item", "name": "Gem Pouch", "qty": [1, 1], "notes": "small pouch of gems"})
    return table


def _roll_one(table: list[dict], rng) -> dict:
    entry = dict(rng.choice(table))
    if entry.get("kind") == "coins":
        lo, hi = entry.get("gp", [1, 1])
        gp = max(0, int(rng.randint(int(lo), int(hi))))
        return {"name": f"{gp} gp", "qty": 1, "notes": "Coins", "item_type": "currency", "gp_value": gp}
    lo, hi = entry.get("qty", [1, 1])
    qty = max(1, int(rng.randint(int(lo), int(hi))))
    return {"name": str(entry.get("name") or "Loot")[:80], "qty": qty, "notes": str(entry.get("notes") or "")[:160]}


def _corpse_reward_rarity(*, action: str, cr_num: float, reward: dict) -> str:
    gp_value = int(reward.get("gp_value") or 0)
    name = str(reward.get("name") or "").strip().lower()
    if gp_value > 0:
        if cr_num >= 11:
            return "uncommon"
        return "common"
    if "pristine core" in name:
        return "very rare"
    if "rare organ" in name or "venom dose" in name:
        return "rare"
    if "drake scale" in name and cr_num >= 5:
        return "rare"
    if action == "harvest":
        if cr_num >= 11:
            return "rare"
        if cr_num >= 5:
            return "uncommon"
        return "common"
    if cr_num >= 11:
        return "rare"
    if cr_num >= 5:
        return "uncommon"
    return "common"


def _award_corpse_loot(session: Session, user: User, rewards: list[dict], destination: str) -> dict:
    granted = []
    total_gp = 0
    if destination == "party_treasury":
        try:
            from server.rules_db import get_party_treasury, set_party_treasury
            treasury = get_party_treasury(session.id) or {"gold": 0, "silver": 0, "copper": 0}
            for entry in rewards:
                gp = int(entry.get("gp_value") or 0)
                if gp > 0:
                    total_gp += gp
                    treasury["gold"] = int(treasury.get("gold") or 0) + gp
                else:
                    # Non-coin rewards go to party stash as lowest-risk shared destination.
                    stash = get_party_stash_inventory(session)
                    stash.append({
                        "name": str(entry.get("name") or "Loot")[:80],
                        "qty": max(1, int(entry.get("qty") or 1)),
                        "notes": str(entry.get("notes") or "")[:160],
                        "source": "corpse",
                    })
                    _set_party_stash_items(session, stash)
                granted.append(dict(entry))
            set_party_treasury(
                session.id,
                gold=int(treasury.get("gold") or 0),
                silver=int(treasury.get("silver") or 0),
                copper=int(treasury.get("copper") or 0),
            )
        except Exception:
            destination = "player_inventory"
    if destination != "party_treasury":
        for entry in rewards:
            gp = int(entry.get("gp_value") or 0)
            if gp > 0:
                total_gp += gp
                _add_gold_to_player(session, user, gp * 100)
            else:
                _add_item_to_player_inventory(
                    session,
                    user,
                    {"name": entry.get("name"), "notes": entry.get("notes"), "source": "corpse"},
                    int(entry.get("qty") or 1),
                    source_name="corpse",
                )
            granted.append(dict(entry))
    return {"rewards": granted, "gold_gp": total_gp, "destination": destination}


def _resolve_corpse_action(*, action: str, corpse: dict, session: Session, user: User, rng) -> dict:
    cfg = _corpse_dm_config(session)
    ref = _corpse_ref(corpse)
    cr_num = _cr_from_text(ref.get("cr_num", ref.get("cr", 0)))
    dc = _action_dc(action, ref)
    attempts_key = "search_attempts" if action == "search" else "harvest_attempts"
    limit = int(cfg["search_attempts_per_corpse"] if action == "search" else cfg["harvest_attempts_per_corpse"])
    attempts = dict(corpse.get(attempts_key) or {})
    used = max(0, int(attempts.get(user.id) or 0))
    if corpse.get("depleted"):
        return {"ok": False, "reason": "depleted", "message": "This corpse is depleted."}
    if used >= limit:
        return {"ok": False, "reason": "limit", "message": "No attempts remaining for this action."}

    special_poison = bool(action == "harvest" and str(corpse.get("special_harvest") or "").strip().lower() == "poison")
    roll = int(rng.randint(1, 20))
    skill_mod = 0
    if special_poison:
        dc = 20
        skill_mod = _safe_int((corpse.get("skill_modifiers") or {}).get("nature"), 0, minimum=-10, maximum=20)
        if bool((corpse.get("tool_proficiencies") or {}).get("poisoners_kit")):
            skill_mod += 2
    total = int(roll + skill_mod)
    margin = total - dc
    success = margin >= 0
    rewards = []
    consequence = None
    if success:
        bonus_rolls = 2 if margin >= 10 else 1 if margin >= 5 else 0
        rolls = 1 + bonus_rolls
        table = _search_table(cr_num) if action == "search" else _harvest_table(ref.get("monster_type"), cr_num)
        if special_poison:
            rewards = [{"name": "Venom Dose", "qty": 1, "notes": "harvested poison"}]
        else:
            for _ in range(rolls):
                rewards.append(_roll_one(table, rng))
        for entry in rewards:
            entry["rarity"] = _corpse_reward_rarity(action=action, cr_num=cr_num, reward=entry)
    else:
        rewards = []
        if special_poison and cfg.get("fail_by_5_consequence") and margin <= -5:
            consequence = "Poison exposure while harvesting."

    attempts[user.id] = used + 1
    corpse[attempts_key] = attempts
    remaining = max(0, limit - int(attempts[user.id]))
    if (action == "search" and int((corpse.get("search_attempts") or {}).get(user.id, 0)) >= int(cfg["search_attempts_per_corpse"]) and
        int((corpse.get("harvest_attempts") or {}).get(user.id, 0)) >= int(cfg["harvest_attempts_per_corpse"])):
        corpse["depleted"] = True
    awarded = _award_corpse_loot(session, user, rewards, cfg.get("reward_destination", "player_inventory")) if success and rewards else {"rewards": [], "gold_gp": 0, "destination": cfg.get("reward_destination", "player_inventory")}
    return {
        "ok": True,
        "action": action,
        "dc": dc,
        "roll": roll,
        "modifier": skill_mod,
        "total": total,
        "margin": margin,
        "success": success,
        "rewards": awarded["rewards"],
        "gold_gp": awarded["gold_gp"],
        "reward_destination": awarded["destination"],
        "remaining_attempts": remaining,
        "depleted": bool(corpse.get("depleted")),
        "consequence": consequence,
    }


async def handle_corpse_config_update(payload: dict, session: Session, user: User):
    if user.role != "dm":
        return
    cfg = _corpse_dm_config(session)
    if "search_attempts_per_corpse" in payload:
        cfg["search_attempts_per_corpse"] = max(1, min(5, int(payload.get("search_attempts_per_corpse") or cfg["search_attempts_per_corpse"])))
    if "harvest_attempts_per_corpse" in payload:
        cfg["harvest_attempts_per_corpse"] = max(1, min(5, int(payload.get("harvest_attempts_per_corpse") or cfg["harvest_attempts_per_corpse"])))
    if "allow_humanoid_salvage" in payload:
        cfg["allow_humanoid_salvage"] = bool(payload.get("allow_humanoid_salvage"))
    if "fail_by_5_consequence" in payload:
        cfg["fail_by_5_consequence"] = bool(payload.get("fail_by_5_consequence"))
    if "reward_destination" in payload:
        dest = str(payload.get("reward_destination") or "").strip().lower()
        if dest in {"player_inventory", "party_treasury"}:
            cfg["reward_destination"] = dest
    if "crafting_enabled" in payload:
        cfg["crafting_enabled"] = bool(payload.get("crafting_enabled"))
    if "selling_enabled" in payload:
        cfg["selling_enabled"] = bool(payload.get("selling_enabled"))
    session.corpse_dm_config = cfg
    await save_campaign_async(session)
    await manager.broadcast(session.id, {"type": "corpse_config_sync", "payload": cfg})


async def _handle_corpse_action(payload: dict, session: Session, user: User, *, action: str):
    if user.role not in {"dm", "player"}:
        return
    corpse_id = str(payload.get("corpse_id") or payload.get("token_id") or "").strip()
    corpse_states = dict(getattr(session, "corpse_states", {}) or {})
    corpse = corpse_states.get(corpse_id)
    token = (getattr(session, "tokens", {}) or {}).get(corpse_id)
    if not corpse or not token:
        await manager.send_to(session.id, user.id, {"type": "corpse_action_result", "payload": {"ok": False, "message": "Corpse not found."}})
        return
    if int(getattr(token, "hp", 1) or 0) > 0:
        await manager.send_to(session.id, user.id, {"type": "corpse_action_result", "payload": {"ok": False, "message": "Target is not defeated."}})
        return
    cfg = _corpse_dm_config(session)
    creature_ref = _corpse_ref(corpse)
    if action == "harvest" and str(creature_ref.get("monster_type") or "").strip().lower() == "humanoid" and not cfg.get("allow_humanoid_salvage"):
        await manager.send_to(session.id, user.id, {"type": "corpse_action_result", "payload": {"ok": False, "message": "Humanoid salvage is disabled by the DM."}})
        return
    if action == "harvest" and str(corpse.get("special_harvest") or "").strip().lower() == "poison":
        allowed_states = {"dead", "incapacitated"}
        state_tag = str(corpse.get("target_state") or "dead").strip().lower()
        if state_tag not in allowed_states:
            await manager.send_to(session.id, user.id, {"type": "corpse_action_result", "payload": {"ok": False, "message": "Poison harvesting requires dead or incapacitated target."}})
            return
    result = _resolve_corpse_action(action=action, corpse=corpse, session=session, user=user, rng=_random)
    corpse_states[corpse_id] = corpse
    session.corpse_states = corpse_states
    if result.get("ok") and result.get("success"):
        reward_summary = ", ".join([f"{int(entry.get('qty') or 1)}x {entry.get('name')}" for entry in result.get("rewards", [])[:4]]) or "no loot"
        message = f"{user.name} {action}ed {corpse.get('token_name','corpse')}: success ({reward_summary})."
        session.add_log(f"[Economy] {user.name} {action}ed corpse: {corpse.get('token_name','corpse')} ({reward_summary}).", "system", "System")
    elif result.get("ok"):
        message = f"{user.name} {action}ed {corpse.get('token_name','corpse')}: failed."
    else:
        message = str(result.get("message") or f"{action.title()} unavailable.")
    log = session.add_log(message, "system", user.name)
    await save_campaign_async(session)
    await _broadcast_inventory_state(session)
    await manager.broadcast(session.id, {
        "type": "corpse_action_result",
        "payload": {
            **result,
            "corpse_id": corpse_id,
            "corpse_state": corpse,
            "message": message,
            "log": log,
            "actor_user_id": user.id,
        },
    })


async def handle_corpse_search(payload: dict, session: Session, user: User):
    await _handle_corpse_action(payload, session, user, action="search")


async def handle_corpse_harvest(payload: dict, session: Session, user: User):
    await _handle_corpse_action(payload, session, user, action="harvest")


# ─── Loot generation handlers ─────────────────────────────────────────────────

import random as _random

_DUNGEON_LEVEL_TO_CR = {
    1: (0, 4), 2: (0, 4), 3: (0, 4),
    4: (5, 10), 5: (5, 10), 6: (5, 10),
    7: (11, 16), 8: (11, 16), 9: (11, 16),
    10: (17, 30),
}

_RARITY_COLORS = {
    "common": "#aaaaaa",
    "uncommon": "#1eff00",
    "rare": "#0070dd",
    "very rare": "#a335ee",
    "legendary": "#ff8000",
}

_COIN_MULTIPLIERS = {
    # CR range → coin qty multipliers for individual vs hoard
    "individual": {(0,4):1, (5,10):10, (11,16):100, (17,30):1000},
    "hoard":      {(0,4):1, (5,10):1,  (11,16):1,   (17,30):1},
}

_HOARD_CR_BANDS = [(0,4),(5,10),(11,16),(17,30)]
_HOARD_MAGIC_ITEM_CHANCES = {
    (0,4):   [(1,6,"common")],
    (5,10):  [(1,4,"uncommon"),(1,6,"common")],
    (11,16): [(1,4,"rare"),(1,4,"uncommon")],
    (17,30): [(1,4,"very rare"),(1,4,"rare"),(1,4,"uncommon")],
}


def _cr_band(cr: int) -> tuple:
    if cr <= 4:  return (0,4)
    if cr <= 10: return (5,10)
    if cr <= 16: return (11,16)
    return (17,30)


def _roll(dice: int, sides: int, modifier: int = 0) -> int:
    return sum(_random.randint(1, sides) for _ in range(max(1, dice))) + modifier


def _generate_coins(cr_band: tuple, table_type: str) -> dict:
    """Return a dict of coin totals for the given CR band and table type."""
    from server.rules_db import get_conn as _gc
    with _gc() as conn:
        rows = conn.execute(
            "SELECT * FROM treasure_tables WHERE cr_range_min=? AND cr_range_max=? AND table_type=? AND item_type='coin'",
            (cr_band[0], cr_band[1], table_type),
        ).fetchall()
    if not rows:
        return {}
    coins: dict[str, int] = {}
    if table_type == "individual":
        roll = _random.randint(1, 100)
        for row in rows:
            row = dict(row)
            if row["roll_d100_min"] <= roll <= row["roll_d100_max"]:
                # Individual treasure uses a multiplier based on CR band
                mult = {(0,4):1,(5,10):1,(11,16):1,(17,30):1}.get(cr_band, 1)
                qty = _roll(row["quantity_dice"], row.get("quantity_sides") or 6, row["quantity_modifier"]) * mult
                coin_type = row["coin_type"] or "gp"
                coins[coin_type] = coins.get(coin_type, 0) + qty
                break
    else:  # hoard
        for row in rows:
            row = dict(row)
            qty = _roll(row["quantity_dice"], row.get("quantity_sides") or 6, row["quantity_modifier"])
            coin_type = row["coin_type"] or "gp"
            coins[coin_type] = coins.get(coin_type, 0) + qty
    return coins


def _pick_magic_items(cr_band: tuple) -> list[dict]:
    """Roll for random magic items appropriate to CR band."""
    from server.rules_db import get_all_magic_items
    all_items = get_all_magic_items()
    if not all_items:
        return []
    chance_list = _HOARD_MAGIC_ITEM_CHANCES.get(cr_band, [])
    chosen = []
    for (dice, sides, rarity) in chance_list:
        pool = [m for m in all_items if m["rarity"] == rarity]
        if not pool:
            pool = all_items  # fallback
        count = _roll(dice, sides)
        for _ in range(min(count, 4)):  # cap at 4 per rarity tier
            item = _random.choice(pool)
            chosen.append({
                "id": item["id"],
                "name": "Unidentified Magic Item",
                "unidentified_description": item["unidentified_description"],
                "rarity": item["rarity"],
                "rarity_color": _RARITY_COLORS.get(item["rarity"], "#aaaaaa"),
                "item_type": item["item_type"],
                "attunement_required": bool(item["attunement_required"]),
                "is_identified": False,
                "_true_id": item["id"],
            })
    return chosen


async def handle_generate_loot(payload: dict, session: Session, user: User):
    """DM-only. Generate a level-aware loot preview, optionally keeping locked items."""
    if user.role != "dm":
        return await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "DM only."}})

    prop_id = str(payload.get("prop_id") or "").strip()[:64]
    dungeon_level = max(1, min(20, int(payload.get("dungeon_level") or 1)))

    raw_keep = payload.get("keep")
    keep = [it for it in raw_keep if isinstance(it, dict)] if isinstance(raw_keep, list) else None

    raw_theme = payload.get("theme")
    theme = str(raw_theme).strip() or None if raw_theme else None

    preview = generate_loot_preview(dungeon_level, keep=keep, theme=theme)
    preview["prop_id"] = prop_id

    await manager.send_to(session.id, user.id, {
        "type": "loot_generated",
        "payload": preview,
    })


async def handle_distribute_loot(payload: dict, session: Session, user: User):
    """DM-only. Distribute generated loot to players / treasury."""
    if user.role != "dm":
        return await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "DM only."}})

    from server.rules_db import update_party_treasury, get_party_treasury

    prop_id      = str(payload.get("prop_id") or "_unsaved").strip()[:64]
    distribution = list(payload.get("distribution") or [])

    pending = dict(getattr(session, "_pending_loot", {}) or {})
    loot_entry = pending.get(prop_id) or pending.get("_unsaved")
    if not loot_entry:
        return await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "No pending loot for that prop. Generate loot first."}
        })

    # ── distribute coins ────────────────────────────────────────────────────
    coin_recipients: dict[str, dict[str, int]] = {}  # user_id/treasury → {coin_type: amount}
    for d_entry in distribution:
        item_id   = str(d_entry.get("item_id") or "").strip()
        recipient = str(d_entry.get("recipient_user_id") or "treasury").strip()
        if not item_id.startswith("coin_"):
            continue
        coin_type = item_id.replace("coin_", "")
        amount    = int(d_entry.get("amount") or 0)
        bucket    = coin_recipients.setdefault(recipient, {})
        bucket[coin_type] = bucket.get(coin_type, 0) + amount

    for recipient, coin_map in coin_recipients.items():
        gp = coin_map.get("gp", 0)
        sp = coin_map.get("sp", 0)
        cp = coin_map.get("cp", 0)
        ep = coin_map.get("ep", 0)
        pp = coin_map.get("pp", 0)
        # Convert to gp units (100 units = 1 gp)
        total_units = pp*1000 + gp*100 + ep*50 + sp*10 + cp
        if recipient == "treasury":
            update_party_treasury(session.id, gold=gp + pp*10, silver=sp + ep*5, copper=cp)
        else:
            target_user = (getattr(session, "users", {}) or {}).get(recipient)
            if target_user and getattr(target_user, "role", "viewer") in {"dm", "player"}:
                _add_gold_to_player(session, target_user, total_units)
                await manager.send_to(session.id, recipient, {
                    "type": "loot_received",
                    "payload": {
                        "items": [],
                        "coins": coin_map,
                        "source": loot_entry.get("prop_type", "loot"),
                    },
                })

    # ── distribute magic items ───────────────────────────────────────────────
    items_by_recipient: dict[str, list] = {}
    magic_items_map = {mi["id"] if "id" in mi else mi.get("_true_id","?"): mi
                       for mi in loot_entry.get("magic_items", [])}
    for d_entry in distribution:
        item_id   = str(d_entry.get("item_id") or "").strip()
        recipient = str(d_entry.get("recipient_user_id") or "treasury").strip()
        if item_id.startswith("coin_"):
            continue
        # find item in magic_items list (match by _true_id or positional index)
        mi = magic_items_map.get(item_id)
        if not mi:
            # try matching by position index encoded as "magic_N"
            if item_id.startswith("magic_"):
                try:
                    idx = int(item_id.split("_",1)[1])
                    mi_list = loot_entry.get("magic_items", [])
                    mi = mi_list[idx] if 0 <= idx < len(mi_list) else None
                except Exception:
                    pass
        if not mi:
            continue
        if recipient == "treasury":
            continue  # magic items don't go to treasury in this version
        if recipient == "stash":
            items_by_recipient.setdefault(recipient, []).append(mi)
            continue
        items_by_recipient.setdefault(recipient, []).append(mi)

    for recipient, items in items_by_recipient.items():
        if recipient == "stash":
            for mi in items:
                stash_entry = {
                    "id": str(mi.get("_true_id") or mi.get("id") or ""),
                    "name": mi.get("name", "Magic Item"),
                    "qty": 1,
                    "notes": mi.get("description") or mi.get("unidentified_description", ""),
                    "source": "Party Stash",
                    "is_magic": True,
                    "is_identified": False,
                    "magic_item_id": mi.get("_true_id") or mi.get("id", ""),
                    "rarity": mi.get("rarity", "common"),
                    "unidentified_description": mi.get("unidentified_description", ""),
                    "item_type": mi.get("item_type", "wondrous"),
                    "attunement_required": mi.get("attunement_required", False),
                }
                _add_item_to_party_stash(session, stash_entry, 1, source_name="Loot Distribution")
                _append_party_loot_log(session, {
                    "player_name": user.name,
                    "player_role": user.role,
                    "action": "stash",
                    "item_name": stash_entry["name"],
                    "qty": 1,
                    "source_name": "Party Stash",
                    "source_kind": "loot",
                })
            continue
        target_user = (getattr(session, "users", {}) or {}).get(recipient)
        if not target_user or getattr(target_user, "role", "viewer") not in {"dm", "player"}:
            continue
        for mi in items:
            inv_entry = {
                "name": "Unidentified Magic Item",
                "qty": 1,
                "notes": mi.get("unidentified_description", "A mysterious magical item."),
                "price": "",
                "source": "Loot",
                # Extra fields for identification
                "is_magic": True,
                "is_identified": False,
                "magic_item_id": mi.get("_true_id") or mi.get("id", ""),
                "rarity": mi.get("rarity", "common"),
                "unidentified_description": mi.get("unidentified_description", ""),
                "item_type": mi.get("item_type", "wondrous"),
                "attunement_required": mi.get("attunement_required", False),
            }
            _add_item_to_player_inventory(session, target_user, inv_entry, 1, source_name="Loot")
        await manager.send_to(session.id, recipient, {
            "type": "loot_received",
            "payload": {
                "items": [
                    {
                        "name": mi.get("name","Unidentified Magic Item"),
                        "unidentified_description": mi.get("unidentified_description",""),
                        "rarity": mi.get("rarity","common"),
                        "rarity_color": _RARITY_COLORS.get(mi.get("rarity","common"),"#aaaaaa"),
                        "is_identified": False,
                    }
                    for mi in items
                ],
                "coins": {},
                "source": loot_entry.get("prop_type", "loot"),
            },
        })

    # Broadcast treasury update
    treasury = get_party_treasury(session.id)
    await manager.broadcast(session.id, {
        "type": "treasury_sync",
        "payload": treasury,
    })

    # Remove the pending loot entry
    pending.pop(prop_id, None)
    pending.pop("_unsaved", None)
    session._pending_loot = pending

    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await manager.send_to(session.id, user.id, {
        "type": "distribute_loot_result",
        "payload": {"success": True, "message": "Loot distributed."},
    })


# ─── Level-aware loot generation (v2) ─────────────────────────────────────────

# Rarity weight tables keyed by dungeon level band (min, max).
_LEVEL_RARITY_WEIGHTS: dict[tuple, list[tuple]] = {
    (1,  4):  [("Common", 85), ("Uncommon", 14), ("Rare",  1), ("Very Rare",  0), ("Legendary",  0)],
    (5,  10): [("Common", 55), ("Uncommon", 35), ("Rare",  9), ("Very Rare",  1), ("Legendary",  0)],
    (11, 16): [("Common", 20), ("Uncommon", 40), ("Rare", 28), ("Very Rare", 11), ("Legendary",  1)],
    (17, 20): [("Common",  5), ("Uncommon", 20), ("Rare", 35), ("Very Rare", 30), ("Legendary", 10)],
}

# How many items to roll per level band: (min, max)
_LEVEL_ITEM_COUNTS: dict[tuple, tuple] = {
    (1,  4):  (2, 5),
    (5,  10): (3, 6),
    (11, 16): (4, 7),
    (17, 20): (5, 8),
}


def _loot_level_band(level: int) -> tuple[int, int]:
    """Map a dungeon level (1-20) to a rarity weight band."""
    level = max(1, min(20, int(level)))
    if level <= 4:   return (1, 4)
    if level <= 10:  return (5, 10)
    if level <= 16:  return (11, 16)
    return (17, 20)


def _weighted_rarity_pick(level_band: tuple) -> str:
    """Pick a rarity string using weighted random for the given level band."""
    weights = _LEVEL_RARITY_WEIGHTS.get(level_band, _LEVEL_RARITY_WEIGHTS[(1, 4)])
    rarities = [r for r, _ in weights]
    w_values = [w for _, w in weights]
    total = sum(w_values)
    if total <= 0:
        return "Common"
    roll = _random.randint(1, total)
    cumulative = 0
    for rarity, weight in zip(rarities, w_values):
        cumulative += weight
        if roll <= cumulative:
            return rarity
    return "Common"


def _pick_srd_item_by_rarity(rarity: str, theme: str | None = None) -> dict | None:
    """Pick a random SRD item with the given rarity; falls back to Common.

    When ``theme`` is given, prefer items whose category/tags match it; if no
    item in the rarity pool matches the theme, fall back to the full pool
    rather than returning nothing.
    """
    from server.rules_db import get_srd_items_by_rarity
    pool = get_srd_items_by_rarity(rarity)
    if not pool:
        pool = get_srd_items_by_rarity("Common")
    if not pool:
        return None
    theme = str(theme or "").strip().lower()
    if theme:
        themed = [
            it for it in pool
            if theme in str(it.get("category", "")).lower() or theme in str(it.get("tags", "")).lower()
        ]
        if themed:
            pool = themed
    return _random.choice(pool)


# Rough gp value per rarity tier, used when an item has no parseable price
# (e.g. most magic items, whose value comes from rarity rather than a price tag).
_RARITY_GP_FALLBACK: dict[str, float] = {
    "common": 50, "uncommon": 250, "rare": 1500,
    "very rare": 5000, "legendary": 20000, "artifact": 50000,
}

_PRICE_UNIT_TO_GP = {"pp": 10, "gp": 1, "ep": 0.5, "sp": 0.1, "cp": 0.01}


def _parse_price_to_gp(default_price) -> float:
    """Parse a price string like '15 gp' or '2 sp' into a gp amount."""
    match = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", str(default_price or ""))
    if not match:
        return 0.0
    amount = float(match.group(1))
    unit = match.group(2).lower()
    return amount * _PRICE_UNIT_TO_GP.get(unit, 1)


def _item_gp_value(srd_item: dict, rarity: str) -> float:
    """Resolve a gp value for an SRD item, falling back to a rarity-based estimate."""
    gp = _parse_price_to_gp(srd_item.get("default_price"))
    if gp <= 0:
        gp = _RARITY_GP_FALLBACK.get(str(rarity or "common").strip().lower(), 50)
    return round(gp, 2)


def _loot_budget(total_gp: float, dungeon_level: int) -> dict:
    """Classify a treasure haul's value relative to a per-level target."""
    target_gp = round(dungeon_level * 140, 2)
    if target_gp <= 0:
        band = "balanced"
    elif total_gp < target_gp * 0.65:
        band = "light"
    elif total_gp > target_gp * 1.5:
        band = "generous"
    else:
        band = "balanced"
    return {"total_gp": round(total_gp, 2), "target_gp": target_gp, "band": band}


def generate_loot_preview(dungeon_level: int, keep: list | None = None, theme: str | None = None) -> dict:
    """Generate a loot preview for a given dungeon level.

    ``keep`` is an optional list of item dicts the DM has locked from a prior
    roll; they are included verbatim and only the remaining slots are rolled,
    avoiding duplicates of the kept item names. ``theme`` optionally biases
    newly-rolled items toward a category (e.g. "weapon", "potion").

    Returns a dict with keys: dungeon_level, gold, items (list), budget.
    Does NOT modify any session state.
    """
    level = max(1, min(20, int(dungeon_level)))
    band = _loot_level_band(level)
    min_count, max_count = _LEVEL_ITEM_COUNTS.get(band, (2, 5))
    count = _random.randint(min_count, max_count)

    kept_items = [dict(it) for it in (keep or []) if isinstance(it, dict)]
    used_names = {str(it.get("name", "")).strip().lower() for it in kept_items}

    slots = max(0, count - len(kept_items))
    generated = []
    attempts = 0
    max_attempts = max(slots * 25, 25)
    while len(generated) < slots and attempts < max_attempts:
        attempts += 1
        rarity = _weighted_rarity_pick(band)
        srd_item = _pick_srd_item_by_rarity(rarity, theme=theme)
        if not srd_item:
            continue
        name_key = str(srd_item["name"]).strip().lower()
        if name_key in used_names:
            continue
        used_names.add(name_key)
        generated.append({
            "id": srd_item["id"],
            "name": srd_item["name"],
            "rarity": rarity,
            "category": srd_item.get("category", "Gear"),
            "qty": 1,
            "gp": _item_gp_value(srd_item, rarity),
        })

    items = kept_items + generated
    gold = _random.randint(level * 2, level * 10)
    total_gp = gold + sum(float(it.get("gp") or 0) for it in items)

    return {
        "dungeon_level": level,
        "gold": gold,
        "items": items,
        "budget": _loot_budget(total_gp, level),
    }


def apply_loot_to_chest(session, prop_id: str, loot_data: dict) -> bool:
    """Apply generated loot items and gold to a chest prop's inventory.

    ``session.editor_props`` is keyed by map context, so we must locate the
    target prop within each context list instead of treating the prop id as a
    top-level key.

    Gold is added to the prop's ``gp`` currency field when present; otherwise
    a coin inventory row is appended as a fallback.

    Returns True if the prop was found and updated, False otherwise.
    """
    props_all = dict(getattr(session, "editor_props", {}) or {})
    legacy_prop = props_all.get(prop_id)
    if isinstance(legacy_prop, dict) and str(legacy_prop.get("kind") or "").strip().lower() == "chest":
        prop = dict(legacy_prop)
        inventory = list(prop.get("inventory") or [])
        for item in loot_data.get("items") or []:
            inventory.append({
                "name": str(item.get("name", "Item"))[:80],
                "qty": max(1, int(item.get("qty", 1) or 1)),
                "notes": str(item.get("rarity", ""))[:80],
                "price": "",
            })
        gold = max(0, int(loot_data.get("gold") or 0))
        if gold > 0:
            if "gp" in prop:
                prop["gp"] = int(prop.get("gp") or 0) + gold
            else:
                inventory.append({
                    "name": f"{gold} gp",
                    "qty": 1,
                    "notes": "Coins",
                    "price": f"{gold} gp",
                })
        prop["inventory"] = inventory
        props_all[prop_id] = prop
        session.editor_props = props_all
        return True
    for map_ctx, ctx_items in list(props_all.items()):
        if not isinstance(ctx_items, list):
            continue
        items = list(ctx_items)
        for idx, raw_prop in enumerate(items):
            if not isinstance(raw_prop, dict) or str(raw_prop.get("id") or "") != prop_id:
                continue
            prop = dict(raw_prop)

            inventory = list(prop.get("inventory") or [])
            for item in loot_data.get("items") or []:
                inventory.append({
                    "name": str(item.get("name", "Item"))[:80],
                    "qty": max(1, int(item.get("qty", 1) or 1)),
                    "notes": str(item.get("rarity", ""))[:80],
                    "price": "",
                })

            gold = max(0, int(loot_data.get("gold") or 0))
            if gold > 0:
                if "gp" in prop:
                    prop["gp"] = int(prop.get("gp") or 0) + gold
                else:
                    inventory.append({
                        "name": f"{gold} gp",
                        "qty": 1,
                        "notes": "Coins",
                        "price": f"{gold} gp",
                    })

            prop["inventory"] = inventory
            items[idx] = prop
            props_all[map_ctx] = items
            session.editor_props = props_all
            try:
                from server.handlers.common import _refresh_map_documents
                _refresh_map_documents(session, map_ctx)
            except Exception:
                pass
            return True
    return False


async def handle_identify_item(payload: dict, session: Session, user: User):
    """Player or DM. Attempt to identify a magic item in the player's inventory."""
    if user.role not in {"dm", "player"}:
        return

    from server.rules_db import get_magic_item

    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    method     = str(payload.get("method") or "rest").strip().lower()
    int_mod    = _safe_int(payload.get("int_modifier"), 0, minimum=-5, maximum=10)

    if item_index < 0:
        return await _send_inventory_action_result(session, user.id, "Choose an item to identify.")

    inventories, owner_key, items = _get_player_inventory_store(session, user)
    if item_index >= len(items):
        return await _send_inventory_action_result(session, user.id, "That item is no longer in your inventory.")

    item = items[item_index]
    if not item.get("is_magic") or item.get("is_identified"):
        return await _send_inventory_action_result(session, user.id, "That item doesn't need identification.")

    magic_item_id = str(item.get("magic_item_id") or "").strip()
    true_item = get_magic_item(magic_item_id) if magic_item_id else None

    # ── Check identification method ─────────────────────────────────────────
    if method == "spell":
        # Client must have already verified the spell slot — we trust it here
        identified = True
    elif method == "rest":
        roll = _random.randint(1, 20) + int_mod
        if roll < 15:
            return await manager.send_to(session.id, user.id, {
                "type": "identify_result",
                "payload": {
                    "success": False,
                    "roll": roll,
                    "message": f"The item resists your examination. (rolled {roll}, need 15)",
                },
            })
        identified = True
    elif method == "attunement":
        identified = True
    else:
        identified = True

    # ── Mark item as identified in inventory ────────────────────────────────
    if identified and true_item:
        items[item_index] = dict(item)
        items[item_index]["is_identified"] = True
        items[item_index]["name"] = true_item["name"]
        items[item_index]["notes"] = true_item["description"]
        items[item_index]["effect"] = true_item.get("effect", "")
        inventories[owner_key] = items
        session.player_inventories = inventories

        await _broadcast_inventory_state(session)
        await save_campaign_async(session)
        await manager.send_to(session.id, user.id, {
            "type": "item_identified",
            "payload": {
                "success": True,
                "item_index": item_index,
                "item": {
                    "name": true_item["name"],
                    "description": true_item["description"],
                    "effect": true_item.get("effect", ""),
                    "rarity": true_item["rarity"],
                    "rarity_color": _RARITY_COLORS.get(true_item["rarity"], "#aaaaaa"),
                    "attunement_required": bool(true_item.get("attunement_required")),
                    "item_type": true_item.get("item_type", "wondrous"),
                },
            },
        })
    else:
        await _send_inventory_action_result(session, user.id, "Could not identify that item (unknown magic item ID).")


async def handle_treasury_update(payload: dict, session: Session, user: User):
    """DM-only. Set party treasury values directly."""
    if user.role != "dm":
        return await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "DM only."}})

    from server.rules_db import set_party_treasury, get_party_treasury

    gold   = _safe_int(payload.get("gold"),   0, minimum=0, maximum=9999999)
    silver = _safe_int(payload.get("silver"),  0, minimum=0, maximum=9999999)
    copper = _safe_int(payload.get("copper"),  0, minimum=0, maximum=9999999)

    treasury = set_party_treasury(session.id, gold=gold, silver=silver, copper=copper)
    await manager.broadcast(session.id, {
        "type": "treasury_sync",
        "payload": treasury,
    })


async def handle_treasury_split(payload: dict, session: Session, user: User):
    """DM-only. Split gold evenly among active players."""
    if user.role != "dm":
        return await manager.send_to(session.id, user.id, {"type": "error", "payload": {"message": "DM only."}})

    from server.rules_db import get_party_treasury, set_party_treasury

    treasury = get_party_treasury(session.id)
    players  = [u for u in (getattr(session, "users", {}) or {}).values()
                if getattr(u, "role", "viewer") == "player" and getattr(u, "connected", False)]
    if not players:
        return await manager.send_to(session.id, user.id, {
            "type": "error", "payload": {"message": "No active players to split with."}
        })

    total_gold = int(treasury.get("gold", 0))
    share, remainder = divmod(total_gold, len(players))

    for player in players:
        _add_gold_to_player(session, player, share * 100)  # 100 units per gp
        await manager.send_to(session.id, player.id, {
            "type": "loot_received",
            "payload": {
                "items": [],
                "coins": {"gp": share},
                "source": "Treasury Split",
            },
        })

    # Keep only the remainder in treasury
    set_party_treasury(session.id, gold=remainder, silver=int(treasury.get("silver",0)), copper=int(treasury.get("copper",0)))

    treasury = get_party_treasury(session.id)
    await manager.broadcast(session.id, {"type": "treasury_sync", "payload": treasury})
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await manager.send_to(session.id, user.id, {
        "type": "treasury_split_result",
        "payload": {"success": True, "message": f"Split {total_gold} gp among {len(players)} player(s). {share} gp each."},
    })


async def handle_treasury_get(payload: dict, session: Session, user: User):
    """Any DM or player — fetch current party treasury."""
    if user.role not in {"dm", "player"}:
        return
    from server.rules_db import get_party_treasury
    treasury = get_party_treasury(session.id)
    await manager.send_to(session.id, user.id, {"type": "treasury_sync", "payload": treasury})


# ═══════════════════════════════════════════════════════════════════
# ENCUMBRANCE HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def handle_encumbrance_settings_update(payload: dict, session: Session, user: User):
    """DM only — update campaign-wide encumbrance house-rule settings."""
    if user.role != "dm":
        return
    current = dict(getattr(session, "encumbrance_settings", {}) or {})
    allowed_bool = ("use_encumbrance", "size_restrictions", "allow_dm_override",
                    "bag_destruction_events", "extradim_conflict_block")
    for key in allowed_bool:
        if key in payload:
            current[key] = bool(payload[key])
    if "variant" in payload:
        v = str(payload["variant"] or "variant").strip().lower()
        current["variant"] = v if v in {"basic", "variant"} else "variant"
    session.encumbrance_settings = current
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, "Encumbrance settings saved.")


async def handle_inventory_update_item_weight(payload: dict, session: Session, user: User):
    """DM or player — set a manual weight override on an inventory item."""
    if user.role not in {"dm", "player"}:
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    try:
        new_weight = max(0.0, float(payload.get("weight_lbs") or 0.0))
    except Exception:
        return await _send_inventory_action_result(session, user.id, "Invalid weight value.")
    inventories, owner_key, mine = _get_player_inventory_store(session, target_user)
    if item_index < 0 or item_index >= len(mine):
        return await _send_inventory_action_result(session, user.id, "Item not found.")
    mine[item_index]["weight_lbs"] = new_weight
    _set_player_inventory_items(session, target_user, mine)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"Weight updated to {new_weight} lbs.")


async def handle_enc_set_player_str(payload: dict, session: Session, user: User):
    """DM sets a player's STR score / size for carry-capacity without a char import."""
    if user.role != "dm":
        return
    target_user_id = str(payload.get("target_user_id") or "").strip()
    if not target_user_id:
        return await _send_inventory_action_result(session, user.id, "Missing target_user_id.")
    try:
        strength = max(1, min(30, int(payload.get("strength") or 10)))
    except Exception:
        return await _send_inventory_action_result(session, user.id, "Invalid strength value.")
    size = str(payload.get("size") or "medium").strip().lower() or "medium"
    if not hasattr(session, "enc_str_overrides") or session.enc_str_overrides is None:
        session.enc_str_overrides = {}
    session.enc_str_overrides[target_user_id] = {"strength": strength, "size": size}
    _update_encumbrance_cache(session, target_user_id)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(session, user.id, f"Set STR {strength} ({size}) for player.")


async def handle_bag_add_item(payload: dict, session: Session, user: User):
    """
    Move a carried inventory item into an extradimensional container.
    payload: { bag_index, item_index, qty }
    """
    if user.role not in {"dm", "player"}:
        return
    bag_index  = _safe_int(payload.get("bag_index"),  -1, minimum=-1, maximum=9999)
    item_index = _safe_int(payload.get("item_index"), -1, minimum=-1, maximum=9999)
    move_qty   = _safe_int(payload.get("qty"), 1, minimum=1, maximum=9999)

    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    if bag_index < 0 or bag_index >= len(mine):
        return await _send_inventory_action_result(session, user.id, "Bag not found.")
    bag = mine[bag_index]
    if not is_inventory_container(bag):
        return await _send_inventory_action_result(session, user.id, "That item is not a container.")
    if bag.get("is_devouring"):
        return await _send_inventory_action_result(session, user.id, "The Bag of Devouring consumes that item forever! Action blocked.")
    if item_index < 0 or item_index >= len(mine) or item_index == bag_index:
        return await _send_inventory_action_result(session, user.id, "Item not found.")

    item = dict(mine[item_index])
    # Extradimensional conflict check
    enc_settings = dict(getattr(session, "encumbrance_settings", {}) or {})
    if item.get("extradimensional") and enc_settings.get("extradim_conflict_block", True):
        bag_name = str(bag.get("name") or "Container")
        item_name = str(item.get("name") or "item")
        await manager.broadcast(session.id, {
            "type": "chat_message",
            "payload": {
                "user_name": "System",
                "role": "system",
                "message": f"⚠️ CATASTROPHIC RIFT: Placing {item_name} inside {bag_name} creates an extradimensional rift — both containers and ALL contents are destroyed!",
            }
        })
        return await _send_inventory_action_result(
            session, user.id,
            f"Placing an extradimensional item inside another creates a rift. Action blocked. (DM can disable this in Encumbrance Settings.)"
        )

    # Capacity check
    contents = list(bag.get("bag_contents") or [])
    from server.encumbrance import get_bag_contents_weight, get_item_weight
    current_fill = get_bag_contents_weight(bag)
    item_weight = get_item_weight(item)
    cap = float(bag.get("capacity_lbs") or 500.0)
    if current_fill + item_weight * move_qty > cap:
        return await _send_inventory_action_result(
            session, user.id,
            f"The {bag.get('name','bag')} is too full ({current_fill:.1f}/{cap:.0f} lbs). Cannot add {move_qty}× {item.get('name','item')}."
        )

    # Reduce from main inventory
    current_qty = _safe_int(item.get("qty"), 1, minimum=1, maximum=9999)
    move_qty = min(current_qty, move_qty)
    remaining = current_qty - move_qty
    if remaining > 0:
        mine[item_index]["qty"] = remaining
    else:
        mine.pop(item_index)
        if item_index < bag_index:
            bag_index -= 1

    # Re-read bag after potential pop
    bag = mine[bag_index]

    # Add to bag contents
    item_to_store = dict(item)
    item_to_store["qty"] = move_qty
    # Merge with existing entry in bag if same name/notes
    bag_contents = list(bag.get("bag_contents") or [])
    merged = False
    for ci, ci_item in enumerate(bag_contents):
        if str(ci_item.get("name") or "").lower() == str(item_to_store.get("name") or "").lower() and \
           str(ci_item.get("notes") or "") == str(item_to_store.get("notes") or ""):
            bag_contents[ci]["qty"] = _safe_int(ci_item.get("qty"), 1, minimum=1, maximum=9999) + move_qty
            merged = True
            break
    if not merged:
        bag_contents.append(item_to_store)
    mine[bag_index]["bag_contents"] = bag_contents

    _set_player_inventory_items(session, user, mine)
    _update_encumbrance_cache(session, user.id)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(
        session, user.id,
        f"Moved {move_qty}× {item_to_store.get('name','item')} into {bag.get('name','bag')}."
    )
    if _looks_like_creature_inventory_entry(item_to_store):
        await _send_inventory_action_result(
            session, user.id,
            "⚠️ Breathing warning: living creatures in containers can run out of air quickly."
        )


async def handle_bag_remove_item(payload: dict, session: Session, user: User):
    """
    Move an item from inside an extradimensional container back to main inventory.
    payload: { bag_index, content_index, qty }
    """
    if user.role not in {"dm", "player"}:
        return
    bag_index     = _safe_int(payload.get("bag_index"),     -1, minimum=-1, maximum=9999)
    content_index = _safe_int(payload.get("content_index"), -1, minimum=-1, maximum=9999)
    move_qty      = _safe_int(payload.get("qty"), 1, minimum=1, maximum=9999)

    inventories, owner_key, mine = _get_player_inventory_store(session, user)
    if bag_index < 0 or bag_index >= len(mine):
        return await _send_inventory_action_result(session, user.id, "Bag not found.")
    bag = mine[bag_index]
    if not is_inventory_container(bag):
        return await _send_inventory_action_result(session, user.id, "That item is not a container.")
    bag_contents = list(bag.get("bag_contents") or [])
    if content_index < 0 or content_index >= len(bag_contents):
        return await _send_inventory_action_result(session, user.id, "Content item not found.")

    content_item = dict(bag_contents[content_index])
    current_qty = _safe_int(content_item.get("qty"), 1, minimum=1, maximum=9999)
    move_qty = min(current_qty, move_qty)
    remaining = current_qty - move_qty
    if remaining > 0:
        bag_contents[content_index]["qty"] = remaining
    else:
        bag_contents.pop(content_index)
    mine[bag_index]["bag_contents"] = bag_contents

    # Add back to main inventory
    item_to_return = dict(content_item)
    item_to_return["qty"] = move_qty
    _set_player_inventory_items(session, user, mine)
    _add_item_to_player_inventory(session, user, item_to_return, move_qty)

    _update_encumbrance_cache(session, user.id)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
    await _send_inventory_action_result(
        session, user.id,
        f"Removed {move_qty}× {content_item.get('name','item')} from {bag.get('name','bag')}."
    )


async def handle_bag_destroy(payload: dict, session: Session, user: User):
    """
    DM only — destroy an extradimensional container, losing all contents.
    payload: { target_user_id, bag_index }
    """
    if user.role != "dm":
        return
    target_user = _inventory_target_user(session, user, payload.get("target_user_id"))
    bag_index = _safe_int(payload.get("bag_index"), -1, minimum=-1, maximum=9999)

    inventories, owner_key, mine = _get_player_inventory_store(session, target_user)
    if bag_index < 0 or bag_index >= len(mine):
        return await _send_inventory_action_result(session, user.id, "Bag not found.")
    bag = mine[bag_index]
    if not is_inventory_container(bag):
        return await _send_inventory_action_result(session, user.id, "That item is not a container.")

    bag_name = str(bag.get("name") or "Bag of Holding")
    mine.pop(bag_index)
    _set_player_inventory_items(session, target_user, mine)

    await manager.broadcast(session.id, {
        "type": "chat_message",
        "payload": {
            "user_name": "System",
            "role": "system",
            "message": f"💀 The {bag_name} tears open — its contents are lost to the Astral Plane!",
        }
    })
    _update_encumbrance_cache(session, target_user.id)
    await _broadcast_inventory_state(session)
    await save_campaign_async(session)
