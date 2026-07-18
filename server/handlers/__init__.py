"""
server/handlers/__init__.py — Message dispatch and re-exports.
"""
import logging
import time as _time

from server.session import Session, User
from server.handlers.common import manager

# ---------------------------------------------------------------------------
# Server-side rate limit for the chat_bridge role. The bridge process has its
# own limiter, but the trust boundary is here: a buggy or compromised bridge
# must not be able to flood the game server.
# ---------------------------------------------------------------------------
_BRIDGE_RATE_WINDOW_SEC = 10.0
_BRIDGE_RATE_MAX_MSGS = 60
_bridge_rate_state: dict = {}  # (session_id, user_id) -> [timestamps]


def _bridge_rate_limited(session_id: str, user_id: str) -> bool:
    now = _time.monotonic()
    key = (session_id, user_id)
    stamps = [t for t in _bridge_rate_state.get(key, []) if now - t < _BRIDGE_RATE_WINDOW_SEC]
    limited = len(stamps) >= _BRIDGE_RATE_MAX_MSGS
    if not limited:
        stamps.append(now)
    _bridge_rate_state[key] = stamps
    # Opportunistic cleanup so stale sessions don't accumulate
    if len(_bridge_rate_state) > 256:
        for stale_key in [k for k, v in _bridge_rate_state.items()
                          if not v or now - v[-1] > _BRIDGE_RATE_WINDOW_SEC * 6]:
            _bridge_rate_state.pop(stale_key, None)
    return limited

# Sub-module handler imports
from server.handlers.tokens import (
    handle_token_move,
    handle_token_create,
    handle_token_placed,
    handle_token_send_to_staging,
    handle_token_delete,
    handle_token_hp_update,
    handle_token_edit,
    handle_toggle_hidden,
    handle_token_condition,
    handle_char_hp_update,
    handle_mark_target,
    handle_token_emote,
)
from server.handlers.combat import (
    handle_combat_update,
    handle_combat_next,
    handle_combat_prev,
    handle_combat_clear,
    handle_combat_dash,
    handle_combat_toggle_difficult_terrain,
    handle_combat_toggle_disengage,
    handle_combat_reset_movement,
    handle_combat_move_preview,
    handle_combat_move_commit,
    handle_combat_end_turn,
    handle_combat_death_save,
    handle_combat_roll_initiative,
    handle_combat_add_token,
    handle_combat_remove_combatant,
    handle_combat_select_target,
    handle_combat_attack_request,
    handle_combat_attack_override,
    handle_combat_fog_sync_request,
    handle_combat_state_request,
    handle_combat_action_economy_use,
)
from server.handlers.map_editor import (
    handle_editor_layer_save,
    handle_editor_layer_clear,
    handle_editor_walls_save,
    handle_editor_walls_clear,
    handle_editor_props_save,
    handle_editor_props_clear,
    handle_editor_paths_save,
    handle_editor_paths_clear,
    handle_editor_labels_save,
    handle_editor_labels_clear,
    handle_editor_markers_save,
    handle_editor_markers_clear,
    handle_map_settings_save,
    handle_door_toggle,
    handle_door_lock_set,
    handle_fog_toggle,
    handle_fog_paint,
    handle_spell_marker_relay,
    handle_ruler_broadcast,
    handle_ping_map,
    handle_poi_create,
    handle_poi_update,
    handle_poi_delete,
    handle_bring_all_to_map,
    handle_local_map_nav,
    handle_weather_set,
    handle_map_set_url,
    handle_interactable_action,
)
from server.handlers.inventory import (
    handle_prop_take_item,
    handle_chest_loot_roll_choice,
    handle_prop_buy_item,
    handle_inventory_add_gold,
    handle_inventory_remove_gold,
    handle_inventory_add_item,
    handle_inventory_remove_item,
    handle_inventory_transfer_item,
    handle_inventory_send_to_stash,
    handle_stash_claim_item,
    handle_inventory_equip_item,
    handle_inventory_unequip_item,
    handle_inventory_attune_item,
    handle_inventory_unattune_item,
    handle_inventory_use_item_action,
    handle_inventory_cast_item_spell,
    handle_open_shop,
    handle_dm_configure_shop,
    handle_dm_get_shop_config,
    handle_learn_profession,
    handle_start_craft_job,
    handle_collect_craft_job,
    handle_purchase_item,
    handle_haggle_item,
    handle_get_sell_offers,
    handle_sell_item,
    handle_haggle_sell_item,
    handle_generate_loot,
    handle_party_loot_log_fetch,
    handle_distribute_loot,
    handle_identify_item,
    handle_treasury_update,
    handle_treasury_split,
    handle_treasury_get,
    handle_encumbrance_settings_update,
    handle_inventory_update_item_weight,
    handle_enc_set_player_str,
    handle_bag_add_item,
    handle_bag_remove_item,
    handle_bag_destroy,
    handle_corpse_search,
    handle_corpse_harvest,
    handle_corpse_config_update,
)
from server.handlers.content import (
    handle_journal_upsert,
    handle_journal_delete,
    handle_session_quest_upsert,
    handle_session_quest_accept,
    handle_session_quest_objective_event,
    handle_session_quest_progress_override,
    handle_session_quest_turn_in,
    handle_quest_template_library_list,
    handle_quest_template_import,
    handle_prep_pack_library_list,
    handle_prep_pack_import,
    handle_item_library_upsert,
    handle_item_library_delete,
    handle_srd_items_request,
    handle_char_profile_upsert,
    handle_char_profile_select,
    handle_char_profile_delete,
    handle_char_profile_fetch,
    handle_dice_roll,
    handle_dice_special_fx,
    handle_grant_permission,
    handle_assistant_dm_permissions_set,
    handle_chat_message,
    handle_request_state,
    handle_party_memory_add,
    handle_party_memory_delete,
    handle_setting_update,
    handle_send_handout,
    handle_handout_delete,
    handle_discovery_trigger,
    handle_discovery_reveal,
    handle_discovery_acknowledge,
    handle_discovery_save,
    handle_discovery_unsave,
    handle_private_story_hook_upsert,
    handle_private_story_hook_delete,
    handle_private_story_hook_resolve,
    handle_encounter_template_upsert,
    handle_encounter_template_delete,
    handle_encounter_spawn_group,
    handle_create_custom_spell,
    handle_grant_spell_to_player,
    handle_get_spell_library,
    handle_revoke_spell,
    handle_viewer_emote,
    handle_poll_create,
    handle_poll_vote,
    handle_poll_close,
    handle_dm_notes_save,
    handle_split_party_assign,
    handle_split_party_set_context,
)
from server.handlers.hazards import (
    handle_hazard_zone_create,
    handle_hazard_zone_update,
    handle_hazard_zone_delete,
    handle_hazard_zone_apply,
)
from server.handlers.viewer_powers import (
    handle_viewer_power_create,
    handle_viewer_power_grant,
    handle_viewer_power_grant_preset,
    handle_viewer_power_revoke,
    handle_viewer_power_use,
    handle_viewer_power_pending_decision,
    handle_viewer_cursor_update,
    handle_viewer_presence_toggle,
)
from server.handlers.sound import (
    handle_sound_set_ambient,
    handle_sound_play_sfx,
    handle_sound_stop_all,
)
from server.handlers.narration import (
    handle_narration_speak,
    handle_narration_stop,
)
from server.handlers.tts_relay import (
    handle_tts_narration,
    handle_tts_narration_stop,
    handle_player_audio_ready,
)
from server.handlers.ai_dm import (
    handle_ai_npc_speak,
    handle_ai_rules_oracle,
    handle_ai_describe_scene,
)
from server.handlers.camp_rest import (
    handle_camp_rest_start,
    handle_camp_rest_end,
    handle_camp_rest_activity_select,
    handle_camp_rest_update_activities,
    handle_camp_rest_clear_activity,
    handle_camp_rest_take_rest,
    handle_camp_rest_spend_hit_die,
    handle_character_self_rest,
)
from server.handlers.conversation import (
    handle_conversation_enter,
    handle_conversation_exit,
    handle_conversation_set_tone,
    handle_conversation_social_action,
    handle_conversation_queue_join,
    handle_conversation_queue_leave,
    handle_conversation_queue_advance,
    handle_conversation_reaction_set,
)
from server.handlers.summons import (
    handle_summon_runtime_request,
    handle_summon_runtime_dismiss,
    handle_summon_runtime_admin,
    handle_summon_action_use,
)
from server.handlers.pets import (
    handle_pet_acquire,
    handle_pet_release,
)
from server.handlers.codex import (
    handle_codex_upsert,
    handle_codex_delete,
    handle_codex_link_set,
    handle_codex_link_query,
)
from server.handlers.chat_bridge import (
    handle_chat_participant_join,
    handle_chat_participant_leave,
    handle_chat_participant_target,
    handle_chat_bridge_loot_grant,
    handle_chat_participant_inventory,
    handle_chat_bridge_poll_vote,
    handle_chat_participant_name_set,
    handle_chat_participant_me,
    handle_chat_participant_resolve,
    handle_dm_chat_bridge_kill_switch,
    handle_dm_chat_friendly_fire,
    handle_dm_grant_chat_participant_item,
    handle_chat_participant_arena_stats_update,
    handle_arena_display_event,
    handle_ticker_event,
    handle_chat_participant_arena_load,
    handle_chat_participant_arena_sync,
    handle_chat_participant_arena_leaderboard,
    handle_chat_participant_quest_start,
    handle_chat_participant_quest_resolve,
    handle_chat_bridge_active_quests,
    handle_chat_participant_arena_death,
    handle_chat_participant_graveyard,
    handle_dm_chat_participants_get,
    handle_dm_chat_participant_update,
    handle_dm_chat_participant_reset,
    handle_dm_chat_persistence_mode,
    handle_chat_bridge_rewards_get,
    handle_dm_chat_rewards_get,
    handle_dm_chat_rewards_update,
    handle_chat_bridge_power_info,
)
from server.handlers.token_placement_secure import handle_token_placed_secure
from server.handlers.ws_permissions import is_ws_message_allowed_for_role
from server.handlers.inventory_runtime_bridge import install_inventory_runtime_bridge

install_inventory_runtime_bridge()

logger = logging.getLogger(__name__)


async def handle_message(raw: dict, session: Session, user: User):
    """Route incoming WS messages to appropriate handler."""
    msg_type = raw.get("type", "")
    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    dispatch = {
        "token_move":       handle_token_move,
        "token_create":     handle_token_create,
        "token_placed":     handle_token_placed_secure,
        "token_send_to_staging": handle_token_send_to_staging,
        "token_delete":     handle_token_delete,
        "token_hp_update":  handle_token_hp_update,
        "fog_paint":        handle_fog_paint,
        "toggle_hidden":    handle_toggle_hidden,
        "spell_marker_add":    handle_spell_marker_relay,
        "spell_marker_remove": handle_spell_marker_relay,
        "spell_marker_clear":  handle_spell_marker_relay,
        "fog_toggle":       handle_fog_toggle,
        "token_edit":       handle_token_edit,
        "dice_roll":        handle_dice_roll,
        "dice_special_fx":  handle_dice_special_fx,
        "ruler_broadcast":  handle_ruler_broadcast,
        "ping_map":         handle_ping_map,
        "grant_permission": handle_grant_permission,
        "assistant_dm_permissions_set": handle_assistant_dm_permissions_set,
        "chat_message":     handle_chat_message,
        "request_state":    handle_request_state,
        "poi_create":       handle_poi_create,
        "poi_update":       handle_poi_update,
        "poi_delete":       handle_poi_delete,
        "char_hp_update":   handle_char_hp_update,
        "local_map_enter":  handle_local_map_nav,
        "local_map_exit":   handle_local_map_nav,
        "bring_all_to_map":    handle_bring_all_to_map,
        "token_hidden_changed": handle_toggle_hidden,
        "token_condition":      handle_token_condition,
        "mark_target":          handle_mark_target,
        "token_emote":          handle_token_emote,
        "combat_update":        handle_combat_update,
        "combat_next":          handle_combat_next,
        "combat_prev":          handle_combat_prev,
        "combat_clear":         handle_combat_clear,
        "combat_dash":          handle_combat_dash,
        "combat_toggle_difficult_terrain": handle_combat_toggle_difficult_terrain,
        "combat_toggle_disengage": handle_combat_toggle_disengage,
        "combat_reset_movement": handle_combat_reset_movement,
        "combat_move_preview": handle_combat_move_preview,
        "combat_move_commit": handle_combat_move_commit,
        "combat_end_turn":      handle_combat_end_turn,
        "combat_death_save":    handle_combat_death_save,
        "combat_roll_initiative": handle_combat_roll_initiative,
        "combat_add_token": handle_combat_add_token,
        "combat_remove_combatant": handle_combat_remove_combatant,
        "combat_select_target": handle_combat_select_target,
        "combat_attack_request": handle_combat_attack_request,
        "combat_attack_override": handle_combat_attack_override,
        "combat_fog_sync_request": handle_combat_fog_sync_request,
        "combat_state_request": handle_combat_state_request,
        "combat_action_economy_use": handle_combat_action_economy_use,
        "summon_runtime_request": handle_summon_runtime_request,
        "summon_runtime_dismiss": handle_summon_runtime_dismiss,
        "summon_runtime_admin": handle_summon_runtime_admin,
        "summon_action_use": handle_summon_action_use,
        "pet_acquire": handle_pet_acquire,
        "pet_release": handle_pet_release,
        "journal_upsert":         handle_journal_upsert,
        "journal_delete":         handle_journal_delete,
        "session_quest_upsert":   handle_session_quest_upsert,
        "session_quest_accept":   handle_session_quest_accept,
        "session_quest_objective_event": handle_session_quest_objective_event,
        "session_quest_progress_override": handle_session_quest_progress_override,
        "session_quest_turn_in": handle_session_quest_turn_in,
        "quest_template_library_list": handle_quest_template_library_list,
        "quest_template_import": handle_quest_template_import,
        "prep_pack_library_list": handle_prep_pack_library_list,
        "prep_pack_import": handle_prep_pack_import,
        "party_memory_add":       handle_party_memory_add,
        "party_memory_delete":    handle_party_memory_delete,
        "item_library_upsert":    handle_item_library_upsert,
        "item_library_delete":    handle_item_library_delete,
        "srd_items_request":      handle_srd_items_request,
        "char_profile_upsert":    handle_char_profile_upsert,
        "char_profile_select":    handle_char_profile_select,
        "char_profile_delete":    handle_char_profile_delete,
        "char_profile_fetch":     handle_char_profile_fetch,
        "editor_layer_save":      handle_editor_layer_save,
        "editor_layer_clear":     handle_editor_layer_clear,
        "editor_walls_save":      handle_editor_walls_save,
        "editor_walls_clear":     handle_editor_walls_clear,
        "editor_props_save":      handle_editor_props_save,
        "editor_props_clear":     handle_editor_props_clear,
        "editor_paths_save":      handle_editor_paths_save,
        "editor_paths_clear":     handle_editor_paths_clear,
        "editor_labels_save":     handle_editor_labels_save,
        "editor_labels_clear":    handle_editor_labels_clear,
        "editor_markers_save":    handle_editor_markers_save,
        "editor_markers_clear":   handle_editor_markers_clear,
        "map_settings_save":     handle_map_settings_save,
        "door_toggle":           handle_door_toggle,
        "door_lock_set":         handle_door_lock_set,
        "weather_set":           handle_weather_set,
        "map_set_url":           handle_map_set_url,
        "interactable_action":  handle_interactable_action,
        "prop_take_item":         handle_prop_take_item,
        "chest_loot_roll_choice": handle_chest_loot_roll_choice,
        "prop_buy_item":          handle_prop_buy_item,
        "inventory_add_item":     handle_inventory_add_item,
        "inventory_add_gold":     handle_inventory_add_gold,
        "inventory_remove_gold":  handle_inventory_remove_gold,
        "inventory_remove_item":  handle_inventory_remove_item,
        "inventory_transfer_item": handle_inventory_transfer_item,
        "inventory_send_to_stash": handle_inventory_send_to_stash,
        "stash_claim_item":        handle_stash_claim_item,
        "inventory_equip_item":    handle_inventory_equip_item,
        "inventory_unequip_item":  handle_inventory_unequip_item,
        "inventory_attune_item":   handle_inventory_attune_item,
        "inventory_unattune_item": handle_inventory_unattune_item,
        "inventory_use_item_action": handle_inventory_use_item_action,
        "inventory_cast_item_spell": handle_inventory_cast_item_spell,
        "open_shop":              handle_open_shop,
        "dm_configure_shop":      handle_dm_configure_shop,
        "dm_get_shop_config":     handle_dm_get_shop_config,
        "learn_profession":       handle_learn_profession,
        "start_craft_job":        handle_start_craft_job,
        "collect_craft_job":      handle_collect_craft_job,
        "purchase_item":          handle_purchase_item,
        "haggle_item":            handle_haggle_item,
        "get_sell_offers":        handle_get_sell_offers,
        "sell_item":              handle_sell_item,
        "haggle_sell_item":       handle_haggle_sell_item,
        "generate_loot":          handle_generate_loot,
        "party_loot_log_fetch":   handle_party_loot_log_fetch,
        "distribute_loot":        handle_distribute_loot,
        "identify_item":          handle_identify_item,
        "treasury_update":        handle_treasury_update,
        "treasury_split":         handle_treasury_split,
        "treasury_get":           handle_treasury_get,
        "encumbrance_settings_update":   handle_encumbrance_settings_update,
        "inventory_update_item_weight":  handle_inventory_update_item_weight,
        "enc_set_player_str":            handle_enc_set_player_str,
        "bag_add_item":                  handle_bag_add_item,
        "bag_remove_item":               handle_bag_remove_item,
        "bag_destroy":                   handle_bag_destroy,
        "corpse_search":                 handle_corpse_search,
        "corpse_harvest":                handle_corpse_harvest,
        "corpse_config_update":          handle_corpse_config_update,
        "viewer_power_create": handle_viewer_power_create,
        "viewer_power_grant": handle_viewer_power_grant,
        "viewer_power_grant_preset": handle_viewer_power_grant_preset,
        "viewer_power_revoke": handle_viewer_power_revoke,
        "viewer_power_use": handle_viewer_power_use,
        "viewer_power_pending_decision": handle_viewer_power_pending_decision,
        "viewer_emote": handle_viewer_emote,
        "viewer_cursor_update": handle_viewer_cursor_update,
        "viewer_presence_toggle": handle_viewer_presence_toggle,
        "poll_create": handle_poll_create,
        "poll_vote": handle_poll_vote,
        "poll_close": handle_poll_close,
        "dm_notes_save": handle_dm_notes_save,
        "split_party_assign": handle_split_party_assign,
        "split_party_set_context": handle_split_party_set_context,
        "hazard_zone_create": handle_hazard_zone_create,
        "hazard_zone_update": handle_hazard_zone_update,
        "hazard_zone_delete": handle_hazard_zone_delete,
        "hazard_zone_apply": handle_hazard_zone_apply,
        "setting_update": handle_setting_update,
        "send_handout": handle_send_handout,
        "handout_delete": handle_handout_delete,
        "discovery_trigger": handle_discovery_trigger,
        "discovery_reveal": handle_discovery_reveal,
        "discovery_acknowledge": handle_discovery_acknowledge,
        "discovery_save": handle_discovery_save,
        "discovery_unsave": handle_discovery_unsave,
        "private_story_hook_upsert": handle_private_story_hook_upsert,
        "private_story_hook_delete": handle_private_story_hook_delete,
        "private_story_hook_resolve": handle_private_story_hook_resolve,
        "encounter_template_upsert": handle_encounter_template_upsert,
        "encounter_template_delete": handle_encounter_template_delete,
        "encounter_spawn_group": handle_encounter_spawn_group,
        "create_custom_spell": handle_create_custom_spell,
        "grant_spell_to_player": handle_grant_spell_to_player,
        "get_spell_library": handle_get_spell_library,
        "revoke_spell": handle_revoke_spell,
        "sound_set_ambient": handle_sound_set_ambient,
        "sound_play_sfx": handle_sound_play_sfx,
        "sound_stop_all": handle_sound_stop_all,
        "narration_speak": handle_narration_speak,
        "narration_stop":  handle_narration_stop,
        # ── GPU TTS relay messages ─────────────────────────────────────────
        "tts_narration":      handle_tts_narration,
        "tts_narration_stop": handle_tts_narration_stop,
        "player_audio_ready": handle_player_audio_ready,
        # ── AI Dungeon Master ──────────────────────────────────────────────
        "ai_npc_speak":      handle_ai_npc_speak,
        "ai_rules_oracle":   handle_ai_rules_oracle,
        "ai_describe_scene": handle_ai_describe_scene,
        # ── Camp / Rest mode ───────────────────────────────────────────────
        "camp_rest_start":              handle_camp_rest_start,
        "camp_rest_end":                handle_camp_rest_end,
        "camp_rest_activity_select":    handle_camp_rest_activity_select,
        "camp_rest_update_activities":  handle_camp_rest_update_activities,
        "camp_rest_clear_activity":     handle_camp_rest_clear_activity,
        "camp_rest_take_rest":          handle_camp_rest_take_rest,
        "camp_rest_spend_hit_die":      handle_camp_rest_spend_hit_die,
        "character_rest":               handle_character_self_rest,
        # ── NPC Conversation Mode ──────────────────────────────────────────
        "conversation_enter":           handle_conversation_enter,
        "conversation_exit":            handle_conversation_exit,
        "conversation_set_tone":        handle_conversation_set_tone,
        "conversation_social_action":   handle_conversation_social_action,
        "conversation_queue_join":      handle_conversation_queue_join,
        "conversation_queue_leave":     handle_conversation_queue_leave,
        "conversation_queue_advance":   handle_conversation_queue_advance,
        "conversation_reaction_set":    handle_conversation_reaction_set,
        # ── Codex ──────────────────────────────────────────────────────────
        "codex_upsert":      handle_codex_upsert,
        "codex_delete":      handle_codex_delete,
        "codex_link_set":    handle_codex_link_set,
        "codex_link_query":  handle_codex_link_query,
        # ── Twitch Chat Bridge (chat_bridge role) ───────────────────────────
        "chat_participant_join":      handle_chat_participant_join,
        "chat_participant_leave":     handle_chat_participant_leave,
        "chat_participant_target":    handle_chat_participant_target,
        "chat_bridge_loot_grant":     handle_chat_bridge_loot_grant,
        "chat_participant_inventory": handle_chat_participant_inventory,
        "chat_bridge_poll_vote":      handle_chat_bridge_poll_vote,
        "chat_participant_name_set":  handle_chat_participant_name_set,
        "chat_participant_me":        handle_chat_participant_me,
        "chat_participant_resolve":   handle_chat_participant_resolve,
        # ── Chat Bridge admin (dm role only) ───────────────────────────────
        "dm_chat_bridge_kill_switch":        handle_dm_chat_bridge_kill_switch,
        "dm_chat_friendly_fire":             handle_dm_chat_friendly_fire,
        "dm_grant_chat_participant_item":    handle_dm_grant_chat_participant_item,
        "dm_chat_participants_get":          handle_dm_chat_participants_get,
        "dm_chat_participant_update":        handle_dm_chat_participant_update,
        "dm_chat_participant_reset":         handle_dm_chat_participant_reset,
        "dm_chat_persistence_mode":          handle_dm_chat_persistence_mode,
        "dm_chat_rewards_get":               handle_dm_chat_rewards_get,
        "dm_chat_rewards_update":            handle_dm_chat_rewards_update,
        # ── Twitch reward tables (chat_bridge role, read-only) ─────────────
        "chat_bridge_rewards_get":           handle_chat_bridge_rewards_get,
        # ── Power description lookup for !powers (chat_bridge role) ────────
        "chat_bridge_power_info":            handle_chat_bridge_power_info,
        # ── Arena stats sync (chat_bridge role) ────────────────────────────
        "chat_participant_arena_stats_update": handle_chat_participant_arena_stats_update,
        # ── Arena character persistence + leaderboard (chat_bridge role) ────
        "chat_participant_arena_load":        handle_chat_participant_arena_load,
        "chat_participant_arena_sync":        handle_chat_participant_arena_sync,
        "chat_participant_arena_leaderboard": handle_chat_participant_arena_leaderboard,
        # ── Chat quests + permadeath (chat_bridge role, server-side timers) ─
        "chat_participant_quest_start":   handle_chat_participant_quest_start,
        "chat_participant_quest_resolve": handle_chat_participant_quest_resolve,
        "chat_bridge_active_quests":      handle_chat_bridge_active_quests,
        "chat_participant_arena_death":   handle_chat_participant_arena_death,
        "chat_participant_graveyard":     handle_chat_participant_graveyard,
        # ── Arena display event relay (chat_bridge role, overlay broadcast) ─
        "arena_display_event": handle_arena_display_event,
        # ── Overlay event ticker relay (chat_bridge role, overlay broadcast) ─
        "ticker_event": handle_ticker_event,
    }

    decision = is_ws_message_allowed_for_role(msg_type, getattr(user, "role", None))
    if not decision.allowed:
        logger.warning(
            "WebSocket message blocked by role policy",
            extra={
                "msg_type": msg_type,
                "reason": decision.reason,
                "session_id": getattr(session, "id", None),
                "user_id": getattr(user, "id", None),
                "user_role": getattr(user, "role", None),
            },
        )
        if decision.error_message:
            await manager.send_to(session.id, user.id, {
                "type": "error",
                "payload": {"message": decision.error_message},
            })
        return

    if getattr(user, "role", "") == "chat_bridge" and _bridge_rate_limited(
        getattr(session, "id", ""), getattr(user, "id", "")
    ):
        logger.warning(
            "Chat bridge message dropped by server-side rate limit",
            extra={"msg_type": msg_type, "session_id": getattr(session, "id", None)},
        )
        return

    handler = dispatch.get(msg_type)
    if handler:
        payload['_msg_type'] = msg_type
        await handler(payload, session, user)
    else:
        logger.warning(
            "Unknown WebSocket message type received",
            extra={
                "msg_type": msg_type,
                "session_id": getattr(session, "id", None),
                "user_id": getattr(user, "id", None),
                "user_role": getattr(user, "role", None),
            },
        )
        await manager.send_to(session.id, user.id, {
            "type": "error",
            "payload": {"message": "Something went wrong. Please refresh and try again."}
        })
