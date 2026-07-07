"""Central WebSocket message role policy.

This is a server-side belt-and-braces gate before handler dispatch. Individual
handlers must still validate ownership/scope, but this policy prevents whole
classes of DM/editor/admin messages from being accepted from players/viewers.
"""
from __future__ import annotations

from dataclasses import dataclass

PUBLIC_MESSAGE_TYPES = frozenset({"ping", "pong"})

VIEWER_ALLOWED_MESSAGE_TYPES = frozenset({
    "ping",
    "pong",
    "viewer_power_use",
    "viewer_cursor_update",
    "viewer_emote",
    "poll_vote",
    "codex_link_query",
})

DM_ADMIN_MESSAGE_TYPES = frozenset({
    "token_delete",
    "fog_paint",
    "fog_toggle",
    "editor_layer_save",
    "editor_layer_clear",
    "editor_walls_save",
    "editor_walls_clear",
    "editor_props_save",
    "editor_props_clear",
    "editor_paths_save",
    "editor_paths_clear",
    "editor_labels_save",
    "editor_labels_clear",
    "editor_markers_save",
    "editor_markers_clear",
    "map_settings_save",
    "map_set_url",
    "door_toggle",
    "door_lock_set",
    "weather_set",
    "bring_all_to_map",
    "local_map_enter",
    "local_map_exit",
    "grant_permission",
    "assistant_dm_permissions_set",
    "session_quest_upsert",
    "session_quest_objective_event",
    "session_quest_progress_override",
    "session_quest_turn_in",
    "quest_template_import",
    "prep_pack_import",
    "item_library_upsert",
    "item_library_delete",
    "send_handout",
    "handout_delete",
    "discovery_trigger",
    "discovery_reveal",
    "private_story_hook_upsert",
    "private_story_hook_delete",
    "private_story_hook_resolve",
    "encounter_template_upsert",
    "encounter_template_delete",
    "encounter_spawn_group",
    "create_custom_spell",
    "grant_spell_to_player",
    "revoke_spell",
    "dm_notes_save",
    "split_party_assign",
    "split_party_set_context",
    "combat_update",
    "combat_clear",
    "combat_add_token",
    "combat_remove_combatant",
    "dm_configure_shop",
    "dm_get_shop_config",
    "generate_loot",
    "distribute_loot",
    "identify_item",
    "treasury_update",
    "treasury_split",
    "encumbrance_settings_update",
    "inventory_update_item_weight",
    "enc_set_player_str",
    "bag_destroy",
    "corpse_config_update",
    "viewer_power_create",
    "viewer_power_grant",
    "viewer_power_grant_preset",
    "viewer_power_revoke",
    "viewer_power_pending_decision",
    "viewer_presence_toggle",
    # Chat bridge admin (DM only)
    "dm_chat_bridge_kill_switch",
    "dm_grant_chat_participant_item",
    "hazard_zone_create",
    "hazard_zone_update",
    "hazard_zone_delete",
    "hazard_zone_apply",
    "setting_update",
    "sound_set_ambient",
    "sound_stop_all",
    "ai_npc_speak",
    "ai_describe_scene",
    "camp_rest_start",
    "camp_rest_end",
    "camp_rest_update_activities",
    "camp_rest_clear_activity",
    "camp_rest_take_rest",
    "conversation_queue_advance",
})

ASSISTANT_DM_DENIED_MESSAGE_TYPES = frozenset({
    "assistant_dm_permissions_set",
    "grant_permission",
    "token_create",
    "token_delete",
    "token_placed",
    "token_send_to_staging",
    "fog_paint",
    "fog_toggle",
    "editor_layer_save",
    "editor_layer_clear",
    "editor_walls_save",
    "editor_walls_clear",
    "editor_props_save",
    "editor_props_clear",
    "editor_paths_save",
    "editor_paths_clear",
    "editor_labels_save",
    "editor_labels_clear",
    "editor_markers_save",
    "editor_markers_clear",
    "map_settings_save",
    "map_set_url",
    "door_toggle",
    "door_lock_set",
    "weather_set",
    "bring_all_to_map",
    "local_map_enter",
    "local_map_exit",
})

CHAT_BRIDGE_ALLOWED_MESSAGE_TYPES = frozenset({
    # Chat-participant lifecycle
    "chat_participant_join",
    "chat_participant_leave",
    # Chat-participant actions (item use / targeting)
    "chat_participant_target",
    # Loot grant triggered by Twitch events (sub/bits/raid)
    "chat_bridge_loot_grant",
    # Inventory query for !inventory command
    "chat_participant_inventory",
    # Poll voting from chat (!vote <N>)
    "chat_bridge_poll_vote",
    # Character name set via !name
    "chat_participant_name_set",
    # Character summary query via !me
    "chat_participant_me",
    # Arena stats sync (duel end — never touches campaign state)
    "chat_participant_arena_stats_update",
    # Arena display event relay (broadcast to overlays, no state change)
    "arena_display_event",
})

PLAYER_KNOWN_GAMEPLAY_MESSAGE_TYPES = frozenset({
    "request_state",
    "chat_message",
    "dice_roll",
    "dice_special_fx",
    "ruler_broadcast",
    "ping_map",
    "token_move",
    "token_create",
    "token_placed",
    "token_send_to_staging",
    "token_edit",
    "token_hp_update",
    "token_condition",
    "mark_target",
    "token_emote",
    "combat_next",
    "combat_prev",
    "combat_dash",
    "combat_toggle_difficult_terrain",
    "combat_toggle_disengage",
    "combat_reset_movement",
    "combat_move_preview",
    "combat_move_commit",
    "combat_end_turn",
    "combat_death_save",
    "combat_roll_initiative",
    "combat_select_target",
    "combat_attack_request",
    "combat_attack_override",
    "combat_fog_sync_request",
    "combat_state_request",
    "combat_action_economy_use",
    "summon_runtime_request",
    "summon_runtime_dismiss",
    "summon_action_use",
    "pet_acquire",
    "pet_release",
    "journal_upsert",
    "journal_delete",
    "session_quest_accept",
    "party_memory_add",
    "party_memory_delete",
    "srd_items_request",
    "char_profile_upsert",
    "char_profile_select",
    "char_profile_delete",
    "poi_create",
    "poi_update",
    "poi_delete",
    "interactable_action",
    "prop_take_item",
    "chest_loot_roll_choice",
    "prop_buy_item",
    "inventory_add_gold",
    "inventory_remove_gold",
    "inventory_add_item",
    "inventory_remove_item",
    "inventory_transfer_item",
    "inventory_send_to_stash",
    "stash_claim_item",
    "inventory_equip_item",
    "inventory_unequip_item",
    "inventory_use_item_action",
    "inventory_cast_item_spell",
    "open_shop",
    "purchase_item",
    "haggle_item",
    "get_sell_offers",
    "sell_item",
    "haggle_sell_item",
    "treasury_get",
    "bag_add_item",
    "bag_remove_item",
    "corpse_search",
    "corpse_harvest",
    "viewer_emote",
    "poll_vote",
    "sound_play_sfx",
    "narration_speak",
    "narration_stop",
    "tts_narration",
    "tts_narration_stop",
    "player_audio_ready",
    "ai_rules_oracle",
    "camp_rest_activity_select",
    "camp_rest_spend_hit_die",
    "character_rest",
    "conversation_enter",
    "conversation_exit",
    "conversation_set_tone",
    "conversation_social_action",
    "conversation_queue_join",
    "conversation_queue_leave",
    "conversation_reaction_set",
    "codex_upsert",
    "codex_delete",
    "codex_link_set",
    "codex_link_query",
})


@dataclass(frozen=True)
class WsPermissionDecision:
    allowed: bool
    reason: str = "allowed"
    error_message: str | None = None


def normalize_role(role: str | None) -> str:
    return str(role or "viewer").strip().lower() or "viewer"


def is_ws_message_allowed_for_role(msg_type: str, role: str | None) -> WsPermissionDecision:
    msg_type = str(msg_type or "").strip()
    role = normalize_role(role)
    if not msg_type:
        return WsPermissionDecision(False, "missing_type", "Invalid WebSocket message.")
    if msg_type in PUBLIC_MESSAGE_TYPES:
        return WsPermissionDecision(True)
    if role == "dm":
        return WsPermissionDecision(True)
    if role == "viewer":
        if msg_type in VIEWER_ALLOWED_MESSAGE_TYPES:
            return WsPermissionDecision(True)
        return WsPermissionDecision(False, "viewer_forbidden", None)
    if role == "assistant_dm":
        if msg_type in ASSISTANT_DM_DENIED_MESSAGE_TYPES:
            return WsPermissionDecision(False, "assistant_dm_forbidden", "Assistant DM scope does not allow that action.")
        return WsPermissionDecision(True)
    if role == "player":
        if msg_type in DM_ADMIN_MESSAGE_TYPES:
            return WsPermissionDecision(False, "player_forbidden", "You don't have permission to do that.")
        if msg_type in PLAYER_KNOWN_GAMEPLAY_MESSAGE_TYPES:
            return WsPermissionDecision(True)
        return WsPermissionDecision(False, "player_unknown_type", "You don't have permission to do that.")
    if role == "overlay":
        if msg_type in PUBLIC_MESSAGE_TYPES:
            return WsPermissionDecision(True)
        return WsPermissionDecision(False, "overlay_readonly", None)
    if role == "chat_bridge":
        if msg_type in CHAT_BRIDGE_ALLOWED_MESSAGE_TYPES:
            return WsPermissionDecision(True)
        return WsPermissionDecision(False, "chat_bridge_forbidden", "Chat bridge does not have permission for that action.")
    return WsPermissionDecision(False, "unknown_role", "You don't have permission to do that.")
