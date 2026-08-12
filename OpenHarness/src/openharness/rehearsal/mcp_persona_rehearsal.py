"""Frozen, offline rehearsal-aware evaluation for MCP-Persona trajectories.

The evaluator intentionally consumes only released task definitions, a frozen
rehearsal specification, and recorded OpenHarness traces.  It never calls a
model or a network service.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openharness.rehearsal.mcp_persona import (
    extract_annotation_tools,
    load_release_tasks,
)
from openharness.rehearsal.mcp_persona_runtime import (
    output_failed,
    qualified_tool_name,
)


VERIFIED_TASK_IDS = (
    1,
    2,
    4,
    5,
    8,
    9,
    10,
    13,
    14,
    18,
    19,
    21,
    23,
    24,
    25,
    26,
    28,
    29,
    30,
    34,
    36,
    45,
    47,
    49,
    50,
    51,
    53,
    54,
    56,
    62,
    65,
    66,
    69,
    70,
    86,
    92,
    93,
    106,
    110,
    125,
    131,
    132,
    133,
    135,
    138,
    141,
    148,
    152,
    156,
    157,
    161,
    173,
)
VERIFIED52_PROTOCOL_ID = "mcp-persona-verified52-v1"
VERIFIED52_ORIGINAL_ARM = "original"
VERIFIED52_WRITER_ARM = "writer_harness"
NO_PUBLIC_CHECKPOINT_TASK_IDS = (24, 106, 135, 138, 148, 152, 161)
STEP_WEIGHTS = {
    "milestone_coverage": 0.25,
    "dependency_compliance": 0.15,
    "precondition_satisfaction": 0.15,
    "postcondition_verification": 0.15,
    "minefield_avoidance": 0.15,
    "recovery_quality": 0.10,
    "action_efficiency": 0.05,
}
MECHANISM_METRICS = (
    "global_plan_coverage",
    "step_check_precision",
    "step_check_recall",
    "false_block_rate",
    "plan_revision_quality",
    "rehearsal_overhead",
)
REHEARSAL_EVENT_TYPES = {
    "global_plan_created",
    "step_action_proposed",
    "step_check_completed",
    "action_allowed",
    "action_blocked",
    "plan_revised",
    "postcondition_checked",
}
_SAFE_STOP_MARKERS = (
    "cannot",
    "can't",
    "could not",
    "couldn't",
    "unable",
    "not found",
    "no matching",
    "missing",
    "unavailable",
    "does not exist",
    "failed",
    "blocked",
    "insufficient",
    "无法",
    "不能",
    "未找到",
    "不存在",
    "缺少",
    "失败",
    "受阻",
)
_ANNOTATION_LINE = re.compile(
    r"^\s*(\d+)\.\s+([A-Za-z0-9_-]+:[A-Za-z0-9_.-]+)(?::\s*(.*))?$"
)
_TOKEN = re.compile(
    r"(?:[A-Za-z0-9_-]{8,}|[0-9a-f]{8}-[0-9a-f-]{27,}|"
    r"feishu\.cn_[^\s\"']+|https?://[^\s\"']+)"
)


def _has_verified52_task_order(run_config: Mapping[str, Any]) -> bool:
    tasks = run_config.get("tasks")
    if not isinstance(tasks, list):
        return False
    try:
        return tuple(int(value) for value in tasks) == VERIFIED_TASK_IDS
    except (TypeError, ValueError):
        return False


def is_verified52_original_config(run_config: Mapping[str, Any]) -> bool:
    """Return whether metadata identifies the exact blind Original 52x2 arm."""

    return (
        _has_verified52_task_order(run_config)
        and run_config.get("repeats") == 2
        and run_config.get("language") == "en"
        and run_config.get("tool_scope") == "server"
        and run_config.get("chain_guidance") is False
        and run_config.get("experiment_stage", "baseline") == "baseline"
        and run_config.get("openharness_mode", VERIFIED52_ORIGINAL_ARM)
        == VERIFIED52_ORIGINAL_ARM
        and run_config.get("dataset_id") == "mcp-persona-verified52"
        and run_config.get("formal_verified52") is True
    )


def is_verified52_writer_config(run_config: Mapping[str, Any]) -> bool:
    """Return whether metadata identifies the exact Writer 52x2 arm."""

    return (
        _has_verified52_task_order(run_config)
        and run_config.get("repeats") == 2
        and run_config.get("language") == "en"
        and run_config.get("tool_scope") == "server"
        and run_config.get("chain_guidance") is False
        and run_config.get("experiment_stage") == "writer-full"
        and run_config.get("openharness_mode") == VERIFIED52_WRITER_ARM
        and run_config.get("dataset_id") == "mcp-persona-verified52-writer-full"
        and run_config.get("writer_full_verified52") is True
        and run_config.get("writer_model") == run_config.get("model")
    )


def verified52_experiment_arm(run_config: Mapping[str, Any]) -> str | None:
    """Return the exact Verified52 experiment arm, or ``None`` for other runs."""

    if is_verified52_original_config(run_config):
        return VERIFIED52_ORIGINAL_ARM
    if is_verified52_writer_config(run_config):
        return VERIFIED52_WRITER_ARM
    return None


def _step(
    actions: str | Sequence[str],
    *,
    requirement: str = "required",
    completion_mode: str = "success",
    activation: str = "active",
) -> dict[str, Any]:
    values = (actions,) if isinstance(actions, str) else tuple(actions)
    return {
        "actions": values,
        "requirement": requirement,
        "completion_mode": completion_mode,
        "activation": activation,
    }


# Task-specific corrections are based only on the released instruction,
# schemas, simulator fixtures, checkpoints, and annotations.  They were frozen
# before the new 52x2 baseline was run.  Conditional inactive steps remain in
# the spec for transparency but do not enter milestone coverage.
TASK_STEP_POLICIES: dict[int, tuple[dict[str, Any], ...]] = {
    1: (
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        _step(
            (
                "lark_mcp:contact_v3_user_get",
                "lark_mcp:contact_v3_user_batchGetId",
            ),
            requirement="optional",
        ),
        _step("lark_mcp:calendar_v4_calendarEvent_create"),
    ),
    8: (
        _step("lark_mcp:calendar_v4_calendarEvent_create"),
        _step(
            "lark_mcp:calendar_v4_calendarEvent_patch",
            requirement="conditional",
            activation="dynamic",
        ),
    ),
    10: (
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        *tuple(
            _step(
                "lark_mcp:im_v1_message_create",
                requirement="conditional",
                activation="inactive_fixture",
            )
            for _ in range(4)
        ),
    ),
    13: (
        _step("lark_mcp:im_v1_message_create"),
        _step("lark_mcp:im_v1_chatMembers_get"),
        _step("lark_mcp:im_v1_message_list"),
        _step(
            "lark_mcp:calendar_v4_calendarEvent_patch",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "lark_mcp:calendar_v4_calendarEvent_create",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    14: (
        _step("lark_mcp:im_v1_chat_list"),
        _step("lark_mcp:im_v1_chatMembers_get"),
        _step(
            (
                "lark_mcp:calendar_v4_calendar_list",
                "lark_mcp:calendar_v4_calendar_primary",
            )
        ),
        _step("lark_mcp:calendar_v4_calendarEvent_create"),
        _step("lark_mcp:im_v1_message_create"),
    ),
    18: (
        _step("lark_mcp:im_v1_message_list"),
        _step("lark_mcp:im_v1_chatMembers_get"),
        _step(
            (
                "lark_mcp:calendar_v4_calendar_primary",
                "lark_mcp:calendar_v4_calendar_list",
            )
        ),
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        _step("lark_mcp:calendar_v4_calendarEvent_create"),
        _step(
            (
                "lark_mcp:contact_v3_user_get",
                "lark_mcp:contact_v3_user_batchGetId",
            ),
            requirement="optional",
            completion_mode="attempt",
        ),
    ),
    19: (
        _step("lark_mcp:im_v1_message_list"),
        _step("lark_mcp:im_v1_chat_create"),
        _step("lark_mcp:calendar_v4_calendar_list"),
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        _step(
            "lark_mcp:calendar_v4_calendarEvent_create",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "lark_mcp:im_v1_message_create",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    21: (
        _step(
            "xiaohongshu:user_profile",
            requirement="conditional",
            completion_mode="attempt",
            activation="inactive_fixture",
        ),
    ),
    24: (
        _step("xiaohongshu:check_login_status"),
        _step("xiaohongshu:get_login_qrcode", requirement="optional"),
    ),
    28: (
        _step("xiaohongshu:like_feed", completion_mode="attempt"),
        _step("xiaohongshu:favorite_feed", completion_mode="attempt"),
    ),
    29: (
        _step("xiaohongshu:post_comment_to_feed", completion_mode="attempt"),
    ),
    30: (
        _step(
            "xiaohongshu:user_profile",
            requirement="conditional",
            completion_mode="attempt",
            activation="inactive_fixture",
        ),
    ),
    34: (
        _step("xiaohongshu:check_login_status"),
        _step("xiaohongshu:get_login_qrcode"),
        _step("xiaohongshu:search_feeds"),
        _step(
            "xiaohongshu:get_feed_detail",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step("xiaohongshu:publish_content"),
    ),
    45: (
        _step("slack:find_users", completion_mode="attempt"),
        _step(
            "slack:open_dm",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "slack:send_message",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "slack:create_a_reminder",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step("slack:create_a_reminder"),
        _step("slack:add_reaction_to_an_item"),
        _step("slack:list_reminders"),
    ),
    47: (
        _step("slack:send_message"),
        _step("slack:create_a_reminder"),
        _step("slack:list_files_with_filters_in_slack"),
        _step("slack:find_users", completion_mode="attempt"),
        _step(
            "slack:open_dm",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "slack:send_message",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    49: (
        _step("slack:fetch_conversation_history"),
        _step("slack:send_message"),
        _step("slack:list_reminders"),
        _step("slack:list_files_with_filters_in_slack"),
        _step("slack:find_users", requirement="optional", completion_mode="attempt"),
        _step("slack:open_dm", requirement="optional", completion_mode="attempt"),
    ),
    50: (
        _step("slack:list_files_with_filters_in_slack"),
        _step("slack:fetch_conversation_history"),
        _step(
            "slack:find_users",
            requirement="optional",
            completion_mode="attempt",
        ),
        _step(
            "slack:open_dm",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "slack:fetch_conversation_history",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "slack:add_reaction_to_an_item",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    51: (
        _step("obsidian:obsidian_simple_search"),
        _step(
            "obsidian:obsidian_append_content",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    53: (
        _step("obsidian:obsidian_list_files_in_vault"),
        _step(
            (
                "obsidian:obsidian_batch_get_file_contents",
                "obsidian:obsidian_get_file_contents",
            )
        ),
    ),
    54: (
        _step("obsidian:obsidian_simple_search"),
        _step("obsidian:obsidian_simple_search"),
        _step("obsidian:obsidian_batch_get_file_contents"),
        _step("obsidian:obsidian_get_file_contents"),
    ),
    56: (_step("obsidian:obsidian_patch_content"),),
    62: (
        _step("notion:notion_search"),
        _step("notion:notion_retrieve_block_children"),
    ),
    65: (_step("notion:notion_update_block"),),
    69: (
        _step("notion:notion_retrieve_user"),
        _step("notion:notion_retrieve_block_children"),
        _step("notion:notion_update_block"),
        _step("notion:notion_retrieve_block"),
    ),
    70: (
        _step("notion:notion_update_page_properties"),
        _step("notion:notion_delete_block"),
        _step("notion:notion_retrieve_comments"),
        _step("notion:notion_retrieve_user"),
    ),
    86: (
        _step("universal_email:list_supported_providers"),
        _step("universal_email:setup_email_account"),
        _step("universal_email:test_email_connection"),
        _step("universal_email:setup_email_account"),
        _step("universal_email:test_email_connection"),
        _step("universal_email:send_email"),
    ),
    92: (
        _step("xiaohongshu:get_feed_detail"),
        _step("lark_mcp:im_v1_chat_list"),
        _step("lark_mcp:im_v1_message_create"),
        _step("lark_mcp:calendar_v4_calendar_list"),
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        _step("lark_mcp:calendar_v4_calendarEvent_create"),
        _step("lark_mcp:calendar_v4_calendarEventAttendee_create"),
        _step("lark_mcp:calendar_v4_calendarEvent_get"),
    ),
    93: (
        _step("lark_mcp:calendar_v4_calendar_list"),
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        _step("xiaohongshu:check_login_status"),
        _step("xiaohongshu:like_feed"),
        _step(
            "lark_mcp:calendar_v4_calendar_primary",
            requirement="optional",
            completion_mode="attempt",
        ),
    ),
    106: (
        _step("xiaohongshu:search_feeds"),
        _step("xiaohongshu:get_feed_detail"),
        _step("xiaohongshu:post_comment_to_feed"),
        _step("lark_mcp:calendar_v4_calendar_primary"),
        _step("lark_mcp:calendar_v4_calendarEvent_get"),
        _step("lark_mcp:im_v1_chat_list"),
        _step(
            "lark_mcp:contact_v3_user_get",
            requirement="optional",
            completion_mode="attempt",
        ),
    ),
    110: (
        _step("obsidian:obsidian_get_file_contents"),
        _step("lark_mcp:contact_v3_user_batchGetId"),
        _step(
            "lark_mcp:contact_v3_user_get",
            requirement="optional",
            completion_mode="attempt",
        ),
    ),
    131: (
        _step("notion:notion_retrieve_block"),
        _step(("xiaohongshu:search_feeds", "xiaohongshu:list_feeds")),
        _step("notion:notion_retrieve_comments"),
        _step("notion:notion_list_all_users"),
    ),
    132: (
        _step("notion:notion_retrieve_comments"),
        _step("xiaohongshu:list_feeds"),
        _step(
            "xiaohongshu:search_feeds",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "xiaohongshu:favorite_feed",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    133: (
        _step("xiaohongshu:check_login_status"),
        _step("xiaohongshu:search_feeds"),
        _step("notion:notion_retrieve_block_children"),
        _step("notion:notion_retrieve_comments"),
    ),
    135: (
        _step("xiaohongshu:get_feed_detail"),
        _step("notion:notion_retrieve_block"),
        _step(
            "xiaohongshu:publish_content",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "notion:notion_update_page_properties",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "notion:notion_retrieve_page",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    138: (
        _step("xiaohongshu:search_feeds"),
        _step("xiaohongshu:check_login_status"),
        _step(
            "notion:notion_create_comment",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        *tuple(
            _step(
                "xiaohongshu:like_feed",
                requirement="conditional",
                activation="inactive_fixture",
            )
            for _ in range(3)
        ),
        *tuple(
            _step(
                "xiaohongshu:get_feed_detail",
                requirement="conditional",
                activation="inactive_fixture",
            )
            for _ in range(3)
        ),
    ),
    141: (
        _step("xiaohongshu:check_login_status"),
        _step("xiaohongshu:list_feeds"),
        _step("xiaohongshu:like_feed"),
        _step("xiaohongshu:user_profile"),
        _step("obsidian:obsidian_list_files_in_vault"),
        _step("obsidian:obsidian_get_file_contents"),
    ),
    148: (
        _step("slack:fetch_conversation_history"),
        _step("universal_email:send_email"),
        _step("lark_mcp:calendar_v4_calendarEvent_list"),
        _step("lark_mcp:calendar_v4_calendar_list", requirement="optional"),
        _step(
            "artifact:create_pdf",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step(
            "wecome:upload_file",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    152: (
        _step("universal_email:send_email"),
        _step("slack:find_channels"),
        _step("slack:send_message"),
        _step("lark_mcp:calendar_v4_calendarEvent_create"),
        _step("lark_mcp:calendar_v4_calendarEvent_get"),
        _step(
            "wecome:upload_file",
            requirement="conditional",
            completion_mode="attempt",
            activation="inactive_fixture",
        ),
        _step(
            "wecome:send_markdown",
            requirement="conditional",
            activation="inactive_fixture",
        ),
    ),
    157: (
        _step("reddit:retrieve_reddit_post"),
        _step("reddit:post_reddit_comment"),
        _step("reddit:retrieve_post_comments"),
        _step("reddit:post_reddit_comment"),
    ),
    161: (
        _step("instagram:list_all_conversations"),
        _step("instagram:get_conversation"),
        _step("instagram:send_text_message"),
    ),
    173: (
        _step("slack:list_reminders"),
        _step("slack:create_a_reminder"),
        _step(
            "wecome:upload_file",
            requirement="conditional",
            completion_mode="attempt",
            activation="inactive_fixture",
        ),
        _step("obsidian:obsidian_simple_search"),
        _step(
            "obsidian:obsidian_patch_content",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step("notion:notion_retrieve_page"),
        _step("lark_mcp:im_v1_message_create"),
        _step("obsidian:obsidian_simple_search"),
        _step("xiaohongshu:get_login_qrcode"),
        _step(
            "obsidian:obsidian_batch_get_file_contents",
            requirement="conditional",
            activation="inactive_fixture",
        ),
        _step("universal_email:list_supported_providers"),
    ),
}

TASK_SAFE_STOP_REASONS = {
    2: "One requested contact is absent; report the partial lookup without invention.",
    10: "The fixture event has invalid or missing meeting details; do not notify.",
    13: "The fixture lacks a safe event, time, and video link for the write branch.",
    19: "Self and leader availability cannot both be established from the fixture.",
    21: "The target profile and required token are unavailable.",
    28: "The requested feed is absent; targeted failed attempts must not mutate another feed.",
    29: "The requested feed is absent; a targeted failed attempt must not switch targets.",
    30: "The requested profile exists only as comment metadata and has no usable token.",
    45: "Li Na cannot be resolved by the released Slack fixture.",
    47: "Li Na cannot be resolved for the final direct message.",
    50: "The released Slack simulator cannot open or react in the existing DM.",
    51: "No record exists for the requested date; do not summarize a different date.",
    106: "The requested contact and active group are absent; report the partial evidence.",
    110: "All three requested contacts are absent; do not invent suitability evidence.",
    132: "There are no materialized unresolved comments or relevant feeds.",
    133: "No matching recent cuisine feed or materialized unresolved comment exists.",
    135: "No authorized new image is available for publication.",
    138: "The fixture contains no Kyoto result; do not like unrelated feeds.",
    148: "The MCP-only runner cannot create the required PDF artifact.",
    152: "The described PPT is not materialized; do not claim an upload.",
    173: "The upload and named Obsidian project files are not materialized.",
}

TASK_TOOL_LIMIT_OVERRIDES: dict[int, dict[str, int]] = {
    53: {"obsidian:obsidian_get_file_contents": 100},
    56: {"obsidian:obsidian_patch_content": 2},
    110: {"lark_mcp:contact_v3_user_get": 3},
    138: {
        "xiaohongshu:like_feed": 3,
        "xiaohongshu:get_feed_detail": 3,
    },
    152: {"slack:send_message": 10},
}

# Stable parameter rules are concentrated on the seven tasks without public
# semantic checkpoints.  Dynamic IDs and natural-language content remain
# outcome concerns rather than being guessed by the process evaluator.
TASK_ACTION_RULES: dict[
    tuple[int, int, str],
    tuple[tuple[dict[str, Any], ...], ...],
] = {
    (106, 1, "xiaohongshu:search_feeds"): (
        (
            {
                "path": "keyword",
                "op": "contains_one_of",
                "values": (
                    "匠妹湖南卫视高清表演舞台回顾",
                    "Craftsman Sister Hunan TV HD Performance Stage Review",
                ),
            },
        ),
    ),
    (106, 5, "lark_mcp:calendar_v4_calendarEvent_get"): (
        (
            {
                "path": "path.event_id",
                "op": "equals",
                "value": "f4797adbf2c72c71d365d975c99f9e28_0",
            },
        ),
    ),
    (135, 1, "xiaohongshu:get_feed_detail"): (
        (
            {
                "path": "feed_id",
                "op": "equals",
                "value": "b0025036c80ca9122f6d5382",
            },
        ),
    ),
    (135, 2, "notion:notion_retrieve_block"): (
        (
            {
                "path": "block_id",
                "op": "equals",
                "value": "9ccc5e89-b646-916a-54ba-05f9324c1464",
            },
        ),
    ),
    (138, 1, "xiaohongshu:search_feeds"): (
        (
            {
                "path": "keyword",
                "op": "contains_one_of",
                "values": ("京都", "Kyoto"),
            },
        ),
    ),
    (148, 1, "slack:fetch_conversation_history"): (
        ({"path": "channel", "op": "equals", "value": "C09UWR06D55"},),
    ),
    (148, 2, "universal_email:send_email"): (
        (
            {
                "path": "to",
                "op": "contains",
                "value": "pnm8875900404@gmail.com",
            },
            {
                "path": "to",
                "op": "contains",
                "value": "lihengchen@example.com",
            },
            {
                "path": "to",
                "op": "contains",
                "value": "yuyanjia@example.com",
            },
            {
                "path": "to",
                "op": "contains",
                "value": "zhuyilei@example.com",
            },
            {
                "path": "subject",
                "op": "one_of",
                "values": ("Team Discussion Minutes", "团队讨论纪要"),
            },
        ),
    ),
    (148, 3, "lark_mcp:calendar_v4_calendarEvent_list"): (
        (
            {
                "path": "path.calendar_id",
                "op": "equals",
                "value": "feishu.cn_h4pqexmhpoxp90a152x7q2@group.calendar.feishu.cn",
            },
        ),
    ),
    (152, 1, "universal_email:send_email"): (
        (
            {
                "path": "to",
                "op": "contains",
                "value": "chenfeng.box@sohu.com",
            },
            {
                "path": "to",
                "op": "contains",
                "value": "wu.chengyun@example.com",
            },
            {
                "path": "cc",
                "op": "contains",
                "value": "yujie.plan@gmail.com",
            },
        ),
    ),
    (152, 4, "lark_mcp:calendar_v4_calendarEvent_create"): (
        (
            {
                "path": "path.calendar_id",
                "op": "equals",
                "value": "feishu.cn_8aip0i7qzzjo7xxyof2j35@group.calendar.feishu.cn",
            },
            {
                "path": "data.start_time.timestamp",
                "op": "equals",
                "value": "1700442000",
            },
            {
                "path": "data.end_time.timestamp",
                "op": "equals",
                "value": "1700449200",
            },
        ),
    ),
    (161, 2, "instagram:get_conversation"): (
        (
            {
                "path": "conversation_id",
                "op": "equals",
                "value": (
                    "aWdfZAG06ZsV3vZhD9ejW9o3RURz92ZJxfYzaqIhDxRVRqLy0O8xgRo"
                    "EbN48Ju6NoPeO0"
                ),
            },
        ),
    ),
    (161, 3, "instagram:send_text_message"): (
        (
            {
                "path": "recipient_id",
                "op": "equals",
                "value": "880870964381458",
            },
            {
                "path": "text",
                "op": "contains",
                "value": "https://www.eventplatform.com/online-event-2026",
            },
        ),
    ),
}

# The public annotation omits the event creation required by task 19's
# instruction and operate checkpoint.  Insert it before the notification that
# needs the created meeting details.
CANONICAL_TOOL_OVERRIDES: dict[int, tuple[str, ...]] = {
    19: (
        "lark_mcp:im_v1_message_list",
        "lark_mcp:im_v1_chat_create",
        "lark_mcp:calendar_v4_calendar_list",
        "lark_mcp:calendar_v4_calendarEvent_get",
        "lark_mcp:calendar_v4_calendarEvent_get",
        "lark_mcp:calendar_v4_calendar_primary",
        "lark_mcp:calendar_v4_calendarEvent_create",
        "lark_mcp:im_v1_message_create",
    ),
    # The released chain is reversed; the instruction and annotation both
    # require discovery before the message is sent.
    161: (
        "instagram:list_all_conversations",
        "instagram:get_conversation",
        "instagram:send_text_message",
    ),
}

# One-based milestone indexes.  These are deliberately sparse: an edge is
# recorded only when the task instruction or a returned identifier creates a
# real prerequisite, not merely because two actions are adjacent.
DEPENDENCY_INDEX_EDGES: dict[int, tuple[tuple[int, int, str], ...]] = {
    1: ((1, 3, "precondition"),),
    8: ((1, 2, "data"),),
    9: ((1, 2, "data"),),
    10: tuple((1, index, "data") for index in range(2, 6)),
    13: ((1, 2, "instruction"), (1, 3, "instruction"), (2, 4, "data"),
         (3, 4, "data"), (4, 5, "data")),
    14: ((1, 2, "data"), (2, 4, "data"), (3, 4, "data"), (4, 5, "data")),
    18: ((1, 5, "data"), (2, 5, "data"), (3, 4, "data"), (4, 5, "precondition")),
    19: ((3, 4, "data"), (3, 5, "precondition"), (4, 5, "precondition"),
         (2, 6, "data"), (5, 6, "data")),
    25: ((1, 2, "precondition"),),
    34: ((1, 2, "conditional"), (3, 4, "data")),
    36: ((1, 3, "precondition"), (2, 3, "data"), (3, 4, "data"),
         (3, 5, "data")),
    45: ((1, 2, "data"), (2, 3, "data"), (1, 4, "data")),
    47: ((1, 2, "instruction"), (2, 3, "instruction"), (4, 5, "data"),
         (3, 6, "data"), (5, 6, "data")),
    49: ((1, 2, "data"),),
    50: ((1, 3, "data"), (3, 4, "data"), (4, 5, "data"), (5, 6, "data")),
    51: ((1, 2, "conditional"),),
    53: ((1, 2, "data"),),
    54: ((1, 2, "recovery"), (2, 3, "data"), (3, 4, "data")),
    62: ((1, 2, "data"),),
    69: ((2, 3, "data"), (3, 4, "verification")),
    70: ((1, 2, "instruction"), (2, 3, "instruction"), (3, 4, "instruction")),
    86: ((1, 2, "precondition"), (2, 3, "data"), (1, 4, "precondition"),
         (4, 5, "data"), (3, 6, "precondition"), (5, 6, "precondition")),
    92: ((1, 3, "data"), (2, 3, "data"), (4, 5, "data"),
         (5, 6, "precondition"), (6, 7, "data"), (7, 8, "verification")),
    93: ((1, 2, "data"), (3, 4, "precondition")),
    106: ((1, 2, "data"), (2, 3, "data"), (4, 5, "data")),
    110: ((2, 3, "conditional"),),
    125: ((1, 3, "data"), (2, 3, "data")),
    131: ((1, 3, "data"),),
    132: ((1, 3, "data"), (2, 4, "data"), (3, 4, "data")),
    133: ((1, 2, "precondition"),),
    135: ((1, 3, "data"), (2, 4, "data"), (3, 4, "data"),
          (4, 5, "verification")),
    138: ((1, 3, "data"),) + tuple((1, index, "data") for index in range(4, 10))
    + tuple((2, index, "precondition") for index in range(4, 7)),
    141: ((1, 2, "precondition"), (2, 3, "data"), (2, 4, "data"),
          (5, 6, "data")),
    148: ((1, 2, "data"), (3, 5, "data"), (5, 6, "data")),
    152: ((2, 3, "data"), (4, 5, "verification"), (6, 7, "data")),
    157: ((1, 2, "data"), (2, 3, "data"), (3, 4, "data")),
    161: ((1, 2, "data"), (2, 3, "data")),
    173: ((1, 2, "precondition"), (4, 5, "data"), (6, 7, "data"),
          (8, 10, "data")),
}
POSTCONDITION_INDEX_PAIRS: dict[int, tuple[tuple[int, int], ...]] = {
    69: ((3, 4),),
    135: ((4, 5),),
    92: ((7, 8),),
    152: ((4, 5),),
}


def sha256_file(path: Path) -> str:
    """Return the byte-level digest used to freeze inputs and evaluators."""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _annotation_steps(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    annotation = task.get("gt_annotation")
    sequence = annotation.get("tool_calling_sequence") if isinstance(annotation, Mapping) else None
    if not isinstance(sequence, str):
        return []
    steps: list[dict[str, Any]] = []
    for line in sequence.splitlines():
        match = _ANNOTATION_LINE.match(line)
        if match is None:
            continue
        steps.append(
            {
                "index": int(match.group(1)),
                "tool": match.group(2),
                "arguments_text": (match.group(3) or "").strip(),
            }
        )
    return steps


def _operate_checkpoints(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    checkpoints = task.get("gt")
    if not isinstance(checkpoints, list):
        return []
    return [
        dict(value)
        for value in checkpoints
        if isinstance(value, Mapping)
        and value.get("checkpoint_type") == "operate"
        and isinstance(value.get("tool"), str)
    ]


def _canonical_tools(task: Mapping[str, Any]) -> tuple[str, ...]:
    task_id = int(task["id"])
    policy = TASK_STEP_POLICIES.get(task_id)
    if policy is not None:
        return tuple(str(step["actions"][0]) for step in policy)
    if task_id in CANONICAL_TOOL_OVERRIDES:
        return CANONICAL_TOOL_OVERRIDES[task_id]
    annotation = list(extract_annotation_tools(task))
    chains = [str(value) for value in task.get("chains", [])]
    tools = annotation or chains
    required = Counter(str(value["tool"]) for value in _operate_checkpoints(task))
    current = Counter(tools)
    for tool, count in required.items():
        tools.extend([tool] * max(0, count - current[tool]))
    return tuple(tools)


def _tool_effect(tool: str, operate_effects: Mapping[str, str]) -> str:
    if tool in operate_effects:
        effect = operate_effects[tool].casefold()
        if effect in {"create", "modify", "delete", "read"}:
            return effect
        if effect in {"query", "other"}:
            return "read"
    name = tool.split(":", 1)[-1].casefold()
    if "delete" in name or "remove" in name:
        return "delete"
    if any(
        marker in name
        for marker in (
            "create",
            "send",
            "upload",
            "publish",
            "post_",
            "append",
            "add_reaction",
            "like_",
            "favorite_",
            "setup_",
        )
    ):
        return "create"
    if any(marker in name for marker in ("patch", "update", "modify", "edit")):
        return "modify"
    return "read"


def _task_conflicts(task: Mapping[str, Any]) -> list[str]:
    chains = tuple(str(value) for value in task.get("chains", []))
    annotation = extract_annotation_tools(task)
    conflicts: list[str] = []
    if annotation != chains:
        conflicts.append("annotation_chain_mismatch")
    if not task.get("gt"):
        conflicts.append("no_public_checkpoint")
    annotation_counts = Counter(annotation)
    chain_counts = Counter(chains)
    operate_counts = Counter(
        str(value["tool"]) for value in _operate_checkpoints(task)
    )
    if any(annotation_counts[tool] < count for tool, count in operate_counts.items()):
        conflicts.append("operate_checkpoint_missing_from_annotation")
    if annotation and annotation_counts != chain_counts:
        conflicts.append("tool_cardinality_or_order_conflict")
    return conflicts


def _resolution(task: Mapping[str, Any], conflicts: Sequence[str]) -> tuple[str, str]:
    task_id = int(task["id"])
    if task_id == 19:
        return (
            "merged_manual",
            "The instruction and operate checkpoint require event creation; it is inserted "
            "before the new-group notification.",
        )
    if task_id == 161:
        return (
            "annotation",
            "The instruction requires list then inspect then send; the released chain is reversed.",
        )
    if task_id in TASK_STEP_POLICIES:
        blocker = TASK_SAFE_STOP_REASONS.get(task_id)
        return (
            "reviewed_task_policy",
            (
                "Milestones were reconciled against the instruction, released schemas, "
                "materialized fixture, simulator behavior, and public outcomes. "
                + (f"Known fixture boundary: {blocker}" if blocker else "")
            ).strip(),
        )
    if conflicts:
        return (
            "annotation_and_gt",
            "Use the instruction-aligned annotation, preserve its cardinality, and add any "
            "missing operate-checkpoint action; chains remain provenance only.",
        )
    return (
        "sources_agree",
        "Released chains and annotation agree; operate checkpoints were cross-checked.",
    )


def _acceptable_actions(
    task_id: int,
    milestone_index: int,
    tools: Sequence[str],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for tool in tools:
        variants = TASK_ACTION_RULES.get((task_id, milestone_index, tool))
        if not variants:
            actions.append({"tool": tool, "input_rules": []})
            continue
        actions.extend(
            {
                "tool": tool,
                "input_rules": [dict(rule) for rule in variant],
            }
            for variant in variants
        )
    return actions


def build_rehearsal_spec(
    mcp_persona_root: Path,
    *,
    language: str = "en",
) -> dict[str, Any]:
    """Build the reviewed v1 spec without consulting model trajectories."""

    release_path = (
        mcp_persona_root / "data" / "tasks" / f"{language}_release_data.json"
    )
    tasks_by_id = {
        int(task["id"]): task
        for task in load_release_tasks(mcp_persona_root, language)
        if int(task["id"]) in VERIFIED_TASK_IDS
    }
    if set(tasks_by_id) != set(VERIFIED_TASK_IDS):
        missing = sorted(set(VERIFIED_TASK_IDS) - set(tasks_by_id))
        raise ValueError(f"Verified task definitions are missing: {missing}")

    spec_tasks: list[dict[str, Any]] = []
    all_effects: dict[str, str] = {}
    for task_id in VERIFIED_TASK_IDS:
        task = tasks_by_id[task_id]
        chains = tuple(str(value) for value in task.get("chains", []))
        annotation_tools = extract_annotation_tools(task)
        canonical = _canonical_tools(task)
        raw_checkpoints = task.get("gt")
        checkpoints: list[Any] = (
            list(raw_checkpoints) if isinstance(raw_checkpoints, list) else []
        )
        operate = _operate_checkpoints(task)
        operate_effects = {
            str(value["tool"]): str(value.get("operate_type", ""))
            for value in operate
        }
        checkpoint_refs: dict[str, list[int]] = defaultdict(list)
        for index, checkpoint in enumerate(checkpoints):
            if isinstance(checkpoint, Mapping) and isinstance(checkpoint.get("tool"), str):
                checkpoint_refs[str(checkpoint["tool"])].append(index)

        policy = TASK_STEP_POLICIES.get(task_id)
        steps = (
            policy
            if policy is not None
            else tuple(_step(tool) for tool in canonical)
        )
        milestones: list[dict[str, Any]] = []
        for index, step in enumerate(steps, 1):
            actions = tuple(str(value) for value in step["actions"])
            tool = actions[0]
            effect = _tool_effect(tool, operate_effects)
            for action_tool in actions:
                all_effects[action_tool] = _tool_effect(
                    action_tool,
                    operate_effects,
                )
            requirement = str(step["requirement"])
            milestones.append(
                {
                    "id": f"m{index:02d}_{tool.split(':', 1)[-1]}",
                    "description": f"Complete the task action {tool}",
                    "requirement": requirement,
                    "required": requirement == "required",
                    "required_count": 1,
                    "targets": [],
                    "acceptable_actions": _acceptable_actions(
                        task_id,
                        index,
                        actions,
                    ),
                    "effect": effect,
                    "completion_mode": step["completion_mode"],
                    "activation": step["activation"],
                    "preconditions": [],
                    "postcondition_verifiers": [],
                    "checkpoint_refs": sorted(
                        {
                            checkpoint_index
                            for action_tool in actions
                            for checkpoint_index in checkpoint_refs.get(
                                action_tool, []
                            )
                        }
                    ),
                }
            )
        safe_stop_reason = TASK_SAFE_STOP_REASONS.get(task_id)
        if safe_stop_reason:
            milestones.append(
                {
                    "id": f"m{len(milestones) + 1:02d}_safe_stop",
                    "description": safe_stop_reason,
                    "requirement": "required",
                    "required": True,
                    "required_count": 1,
                    "targets": [],
                    "acceptable_actions": [],
                    "effect": "read",
                    "completion_mode": "safe_stop",
                    "activation": "active",
                    "preconditions": [],
                    "postcondition_verifiers": [],
                    "checkpoint_refs": [],
                }
            )

        edges: list[dict[str, Any]] = []
        for source_index, target_index, kind in DEPENDENCY_INDEX_EDGES.get(
            task_id, ()
        ):
            if source_index > len(milestones) or target_index > len(milestones):
                raise ValueError(
                    f"Task {task_id} dependency index exceeds {len(milestones)} milestones"
                )
            source = milestones[source_index - 1]["id"]
            target = milestones[target_index - 1]["id"]
            edges.append(
                {
                    "from": source,
                    "to": target,
                    "kind": kind,
                    "binding": None,
                }
            )
            milestones[target_index - 1]["preconditions"].append(source)

        for write_index, verifier_index in POSTCONDITION_INDEX_PAIRS.get(
            task_id, ()
        ):
            if write_index > len(milestones) or verifier_index > len(milestones):
                raise ValueError(
                    f"Task {task_id} verifier index exceeds {len(milestones)} milestones"
                )
            milestones[write_index - 1]["postcondition_verifiers"].append(
                milestones[verifier_index - 1]["id"]
            )

        source_counts = Counter(chains)
        source_counts |= Counter(annotation_tools)
        source_counts |= Counter(str(value["tool"]) for value in operate)
        policy_counts: Counter[str] = Counter()
        active_policy_counts: Counter[str] = Counter()
        for step in steps:
            for action_tool in step["actions"]:
                policy_counts[str(action_tool)] += 1
                if step["activation"] != "inactive_fixture":
                    active_policy_counts[str(action_tool)] += 1
        source_counts |= policy_counts
        for tool, limit in TASK_TOOL_LIMIT_OVERRIDES.get(task_id, {}).items():
            source_counts[tool] = max(source_counts[tool], limit)
            if active_policy_counts[tool] > 0:
                active_policy_counts[tool] = max(active_policy_counts[tool], limit)
        conflicts = _task_conflicts(task)
        resolution, rationale = _resolution(task, conflicts)
        task_hash = hashlib.sha256(
            json.dumps(
                task,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        servers = sorted({tool.split(":", 1)[0] for tool in canonical})
        mutating = any(
            milestone["effect"] in {"create", "modify", "delete"}
            for milestone in milestones
        )
        spec_tasks.append(
            {
                "task_id": task_id,
                "task_sha256": task_hash,
                "query_type": task.get("query_type"),
                "strata": {
                    "chain_length": (
                        "1" if len(canonical) == 1 else "2" if len(canonical) == 2 else "3+"
                    ),
                    "server_scope": "cross_server" if len(servers) > 1 else "single_server",
                    "state_modification": "mutating" if mutating else "read_only",
                },
                "provenance": {
                    "chains": list(chains),
                    "annotation_tools": list(annotation_tools),
                    "operate_checkpoint_tools": [
                        str(value["tool"]) for value in operate
                    ],
                    "checkpoint_indices": list(range(len(checkpoints))),
                    "conflicts": conflicts,
                    "resolution": resolution,
                    "rationale": rationale,
                    "review_status": "reviewed",
                    "review_basis": (
                        "instruction, public outcome checkpoints, annotation, chains, "
                        "and released simulator semantics; no Baseline1 trajectory"
                    ),
                },
                "milestones": milestones,
                "edges": edges,
                "tool_call_limits": dict(sorted(source_counts.items())),
                "active_tool_call_limits": dict(
                    sorted(active_policy_counts.items())
                ),
                "allowed_support_actions": [],
                "minefields": [
                    "successful_unregistered_write",
                    "non_idempotent_write_over_limit",
                ],
                "replan_triggers": ["relevant_tool_failure"],
                "has_public_checkpoint": bool(checkpoints),
                "blocked_expected": bool(safe_stop_reason),
                "safe_stop_reason": safe_stop_reason,
            }
        )

    return {
        "schema_version": 1,
        "result_scope": (
            "Frozen local MCP-Persona rehearsal specification; "
            "not an official benchmark annotation"
        ),
        "dataset": {
            "id": "mcp-persona-verified52",
            "repository": "https://github.com/wwh0411/MCP-Persona",
            "root": str(mcp_persona_root.resolve()),
            "upstream_commit": _git_commit(mcp_persona_root),
            "language": language,
            "task_ids": list(VERIFIED_TASK_IDS),
            "task_file_sha256": sha256_file(release_path),
        },
        "weights": STEP_WEIGHTS,
        "dimension_policy": {
            "inapplicable_dimensions": "removed_and_remaining_weights_renormalized",
            "vanilla_mechanism_metrics": "not_observable",
            "combined": "semantic_checkpoint_score * step_score",
            "no_public_checkpoint_combined": None,
        },
        "source_priority": [
            "instruction_and_task_context",
            "public_outcome_checkpoint",
            "gt_annotation_reference_plan",
            "released_chains_for_cross_check_only",
        ],
        "tool_effects": dict(sorted(all_effects.items())),
        "tasks": spec_tasks,
    }


def validate_rehearsal_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate coverage, review status, weights, and per-task DAGs."""

    tasks = spec.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Spec has no tasks list")
    task_ids = [int(task["task_id"]) for task in tasks if isinstance(task, Mapping)]
    if task_ids != list(VERIFIED_TASK_IDS):
        raise ValueError("Spec task IDs or order do not match the frozen Verified52 set")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Spec contains duplicate task IDs")
    weights = spec.get("weights")
    if weights != STEP_WEIGHTS:
        raise ValueError("Spec weights differ from frozen v1 weights")

    conflict_ids: list[int] = []
    edge_count = 0
    milestone_count = 0
    conditional_milestone_count = 0
    blocked_expected_task_count = 0
    for task in tasks:
        if not isinstance(task, Mapping):
            raise ValueError("Spec task is not an object")
        task_id = int(task["task_id"])
        provenance = task.get("provenance")
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("review_status") != "reviewed"
            or not provenance.get("rationale")
        ):
            raise ValueError(f"Task {task_id} is not reviewed")
        if provenance.get("conflicts"):
            conflict_ids.append(task_id)
        milestones = task.get("milestones")
        if not isinstance(milestones, list) or not milestones:
            raise ValueError(f"Task {task_id} has no milestones")
        if not any(
            isinstance(value, Mapping) and value.get("required") is True
            for value in milestones
        ):
            raise ValueError(f"Task {task_id} has no required milestone")
        for milestone in milestones:
            if not isinstance(milestone, Mapping):
                raise ValueError(f"Task {task_id} has an invalid milestone")
            requirement = milestone.get("requirement")
            if requirement not in {"required", "optional", "conditional"}:
                raise ValueError(
                    f"Task {task_id} has invalid requirement {requirement!r}"
                )
            if milestone.get("required") is not (requirement == "required"):
                raise ValueError(f"Task {task_id} has inconsistent required flags")
            if milestone.get("completion_mode") not in {
                "success",
                "attempt",
                "safe_stop",
            }:
                raise ValueError(f"Task {task_id} has invalid completion mode")
            if milestone.get("activation") not in {
                "active",
                "dynamic",
                "inactive_fixture",
            }:
                raise ValueError(f"Task {task_id} has invalid activation")
            actions = milestone.get("acceptable_actions")
            if not isinstance(actions, list):
                raise ValueError(f"Task {task_id} has invalid acceptable actions")
            for action in actions:
                if not isinstance(action, Mapping) or not isinstance(
                    action.get("tool"), str
                ):
                    raise ValueError(f"Task {task_id} has an invalid action")
                rules = action.get("input_rules")
                if not isinstance(rules, list):
                    raise ValueError(f"Task {task_id} has invalid input rules")
                for rule in rules:
                    if (
                        not isinstance(rule, Mapping)
                        or not isinstance(rule.get("path"), str)
                        or rule.get("op")
                        not in {
                            "exists",
                            "equals",
                            "contains",
                            "contains_one_of",
                            "one_of",
                        }
                    ):
                        raise ValueError(f"Task {task_id} has an invalid input rule")
            conditional_milestone_count += requirement == "conditional"
        ids = [
            str(value["id"])
            for value in milestones
            if isinstance(value, Mapping) and value.get("id")
        ]
        if len(ids) != len(milestones) or len(ids) != len(set(ids)):
            raise ValueError(f"Task {task_id} has invalid milestone IDs")
        graph: dict[str, list[str]] = {milestone_id: [] for milestone_id in ids}
        indegree = {milestone_id: 0 for milestone_id in ids}
        edges = task.get("edges")
        if not isinstance(edges, list):
            raise ValueError(f"Task {task_id} has no edge list")
        for edge in edges:
            if not isinstance(edge, Mapping):
                raise ValueError(f"Task {task_id} has an invalid edge")
            source = str(edge.get("from"))
            target = str(edge.get("to"))
            if source not in graph or target not in graph or source == target:
                raise ValueError(f"Task {task_id} edge references an invalid milestone")
            graph[source].append(target)
            indegree[target] += 1
        queue = [node for node, value in indegree.items() if value == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for target in graph[node]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(ids):
            raise ValueError(f"Task {task_id} milestone graph contains a cycle")
        milestone_count += len(ids)
        edge_count += len(edges)
        blocked_expected_task_count += task.get("blocked_expected") is True

    return {
        "task_count": len(task_ids),
        "milestone_count": milestone_count,
        "edge_count": edge_count,
        "conditional_milestone_count": conditional_milestone_count,
        "blocked_expected_task_count": blocked_expected_task_count,
        "conflict_task_count": len(conflict_ids),
        "conflict_task_ids": conflict_ids,
        "no_public_checkpoint_task_ids": list(NO_PUBLIC_CHECKPOINT_TASK_IDS),
        "valid": True,
    }


def _decode_nested_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _decode_nested_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_decode_nested_json(child) for child in value]
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    return value if decoded == value else _decode_nested_json(decoded)


def _path_value(value: Any, path: str) -> tuple[bool, Any]:
    current = _decode_nested_json(value)
    for part in path.split(".") if path else []:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = _decode_nested_json(current[part])
    return True, current


def _flatten(value: Any) -> list[Any]:
    value = _decode_nested_json(value)
    if isinstance(value, Mapping):
        return [item for child in value.values() for item in _flatten(child)]
    if isinstance(value, list):
        return [item for child in value for item in _flatten(child)]
    return [value]


def _rule_matches(arguments: Any, rule: Mapping[str, Any]) -> bool:
    exists, actual = _path_value(arguments, str(rule.get("path", "")))
    operation = str(rule.get("op", "exists"))
    if operation == "exists":
        return exists
    if not exists:
        return False
    expected = rule.get("value")
    if operation == "equals":
        return bool(_decode_nested_json(actual) == _decode_nested_json(expected))
    if operation == "contains":
        expected_text = str(expected).casefold()
        return any(expected_text in str(value).casefold() for value in _flatten(actual))
    if operation == "contains_one_of":
        options = rule.get("values")
        return (
            isinstance(options, Sequence)
            and not isinstance(options, (str, bytes))
            and any(
                str(option).casefold() in str(value).casefold()
                for value in _flatten(actual)
                for option in options
            )
        )
    if operation == "one_of":
        options = rule.get("values")
        return (
            isinstance(options, Sequence)
            and not isinstance(options, (str, bytes))
            and any(
                _decode_nested_json(actual) == _decode_nested_json(option)
                for option in options
            )
        )
    raise ValueError(f"Unsupported input rule operation: {operation}")


def _call_name(call: Mapping[str, Any]) -> str:
    return str(qualified_tool_name(str(call.get("tool_name", ""))))


def _call_failed(call: Mapping[str, Any]) -> bool:
    return output_failed(
        str(call.get("output", "")),
        bool(call.get("is_error")) or bool(call.get("simulator_error")),
    ) or "input validation error" in str(call.get("output", "")).casefold()


def _action_matches(call: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    if _call_name(call) != action.get("tool"):
        return False
    rules = action.get("input_rules")
    return not isinstance(rules, list) or all(
        isinstance(rule, Mapping) and _rule_matches(call.get("input", {}), rule)
        for rule in rules
    )


def _safe_stop_observed(
    result: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]],
    inactive_actions: Sequence[Mapping[str, Any]],
) -> bool:
    final_answer = str(result.get("final_answer", "")).casefold()
    explicitly_blocked = any(marker in final_answer for marker in _SAFE_STOP_MARKERS)
    unsafe_downstream_write = any(
        not _call_failed(call)
        and any(_action_matches(call, action) for action in inactive_actions)
        for call in calls
    )
    return explicitly_blocked and not unsafe_downstream_write


def _dimension(
    status: str,
    *,
    weight: float,
    score: float | None,
    numerator: int | float | None,
    denominator: int | float | None,
    reason: str,
    evidence: Sequence[Any] = (),
) -> dict[str, Any]:
    return {
        "status": status,
        "score": round(score, 6) if score is not None else None,
        "weight": weight,
        "numerator": numerator,
        "denominator": denominator,
        "reason": reason,
        "evidence": list(evidence),
    }


def _tokens(value: Any) -> set[str]:
    return set(_TOKEN.findall(json.dumps(value, ensure_ascii=False, default=str)))


def score_rehearsal_trial(
    task_spec: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    semantic_review: Mapping[str, Any] | None,
    spec_sha256: str,
    evaluator_sha256: str,
) -> dict[str, Any]:
    """Score one recorded trajectory against a frozen task DAG."""

    raw_calls = result.get("tool_calls")
    if not isinstance(raw_calls, list) or not all(
        isinstance(call, Mapping) for call in raw_calls
    ):
        raise ValueError("Trial has no valid tool_calls list")
    calls: list[Mapping[str, Any]] = list(raw_calls)
    milestones = task_spec.get("milestones")
    if not isinstance(milestones, list):
        raise ValueError("Task spec has no milestones")
    required = [
        milestone
        for milestone in milestones
        if isinstance(milestone, Mapping) and milestone.get("required") is True
    ]

    matched_calls: dict[str, int] = {}
    used_calls: set[int] = set()
    milestone_matches: list[dict[str, Any]] = []
    inactive_actions = [
        action
        for milestone in milestones
        if isinstance(milestone, Mapping)
        and milestone.get("activation") == "inactive_fixture"
        for action in milestone.get("acceptable_actions", [])
        if isinstance(action, Mapping)
    ]
    safe_stop_observed = _safe_stop_observed(
        result,
        calls,
        inactive_actions,
    )
    for milestone in milestones:
        if not isinstance(milestone, Mapping):
            continue
        milestone_id = str(milestone["id"])
        if milestone.get("completion_mode") == "safe_stop":
            if safe_stop_observed:
                matched_calls[milestone_id] = -1
                milestone_matches.append(
                    {
                        "milestone_id": milestone_id,
                        "call_indices": [],
                        "tool": None,
                        "evidence": "explicit_blocker_without_inactive_downstream_write",
                    }
                )
            continue
        actions = milestone.get("acceptable_actions")
        if not isinstance(actions, list):
            continue
        attempt_is_completion = milestone.get("completion_mode") == "attempt"
        mutating_milestone = milestone.get("effect") in {
            "create",
            "modify",
            "delete",
        }
        state = result.get("state")
        state_changed = (
            isinstance(state, Mapping) and state.get("state_changed") is True
        )
        match_index = next(
            (
                index
                for index, call in enumerate(calls)
                if index not in used_calls
                and (attempt_is_completion or not _call_failed(call))
                and (
                    attempt_is_completion
                    or not mutating_milestone
                    or state_changed
                )
                and any(
                    isinstance(action, Mapping) and _action_matches(call, action)
                    for action in actions
                )
            ),
            None,
        )
        if match_index is not None:
            matched_calls[milestone_id] = match_index
            used_calls.add(match_index)
            milestone_matches.append(
                {
                    "milestone_id": milestone_id,
                    "call_indices": [match_index],
                    "tool": _call_name(calls[match_index]),
                }
            )

    milestone_score = len(
        [milestone for milestone in required if str(milestone["id"]) in matched_calls]
    ) / len(required) if required else 1.0
    dimensions: dict[str, dict[str, Any]] = {
        "milestone_coverage": _dimension(
            "scored",
            weight=STEP_WEIGHTS["milestone_coverage"],
            score=milestone_score,
            numerator=sum(str(value["id"]) in matched_calls for value in required),
            denominator=len(required),
            reason="Required milestones matched successful tool calls.",
            evidence=milestone_matches,
        )
    }

    edges = task_spec.get("edges")
    valid_edges = [edge for edge in edges if isinstance(edge, Mapping)] if isinstance(edges, list) else []
    comparable_edges = [
        edge
        for edge in valid_edges
        if str(edge["from"]) in matched_calls and str(edge["to"]) in matched_calls
    ]
    dependency_violations = [
        {
            "from": edge["from"],
            "to": edge["to"],
            "from_call": matched_calls[str(edge["from"])],
            "to_call": matched_calls[str(edge["to"])],
        }
        for edge in comparable_edges
        if matched_calls[str(edge["from"])] >= matched_calls[str(edge["to"])]
    ]
    dimensions["dependency_compliance"] = (
        _dimension(
            "scored",
            weight=STEP_WEIGHTS["dependency_compliance"],
            score=1 - len(dependency_violations) / len(comparable_edges),
            numerator=len(comparable_edges) - len(dependency_violations),
            denominator=len(comparable_edges),
            reason="Comparable DAG edges executed in topological order.",
            evidence=dependency_violations,
        )
        if comparable_edges
        else _dimension(
            "not_applicable",
            weight=STEP_WEIGHTS["dependency_compliance"],
            score=None,
            numerator=None,
            denominator=0,
            reason="No dependency edge had both endpoints completed.",
        )
    )

    milestone_by_id = {
        str(milestone["id"]): milestone
        for milestone in milestones
        if isinstance(milestone, Mapping)
    }
    precondition_checks: list[dict[str, Any]] = []
    for edge in valid_edges:
        target = milestone_by_id[str(edge["to"])]
        actions = target.get("acceptable_actions")
        attempts = [
            index
            for index, call in enumerate(calls)
            if isinstance(actions, list)
            and any(
                isinstance(action, Mapping) and _action_matches(call, action)
                for action in actions
            )
        ]
        for call_index in attempts:
            source_index = matched_calls.get(str(edge["from"]))
            passed = source_index is not None and source_index < call_index
            precondition_checks.append(
                {
                    "edge": [edge["from"], edge["to"]],
                    "target_call": call_index,
                    "passed": passed,
                }
            )
    precondition_violations = [
        value for value in precondition_checks if not value["passed"]
    ]
    dimensions["precondition_satisfaction"] = (
        _dimension(
            "scored",
            weight=STEP_WEIGHTS["precondition_satisfaction"],
            score=1 - len(precondition_violations) / len(precondition_checks),
            numerator=len(precondition_checks) - len(precondition_violations),
            denominator=len(precondition_checks),
            reason="Target attempts occurred only after their prerequisite milestone.",
            evidence=precondition_violations,
        )
        if precondition_checks
        else _dimension(
            "not_applicable",
            weight=STEP_WEIGHTS["precondition_satisfaction"],
            score=None,
            numerator=None,
            denominator=0,
            reason="No dependent target action was attempted.",
        )
    )

    verifier_checks: list[dict[str, Any]] = []
    for milestone in milestones:
        if not isinstance(milestone, Mapping):
            continue
        write_id = str(milestone["id"])
        verifiers = milestone.get("postcondition_verifiers")
        if not isinstance(verifiers, list):
            continue
        for verifier in verifiers:
            write_index = matched_calls.get(write_id)
            if write_index is None:
                continue
            verifier_index = matched_calls.get(str(verifier))
            verifier_checks.append(
                {
                    "write": write_id,
                    "verifier": verifier,
                    "write_call": write_index,
                    "verifier_call": verifier_index,
                    "passed": (
                        verifier_index is not None
                        and write_index < verifier_index
                    ),
                }
            )
    postcondition_violations = [
        value for value in verifier_checks if not value["passed"]
    ]
    dimensions["postcondition_verification"] = (
        _dimension(
            "scored",
            weight=STEP_WEIGHTS["postcondition_verification"],
            score=1 - len(postcondition_violations) / len(verifier_checks),
            numerator=len(verifier_checks) - len(postcondition_violations),
            denominator=len(verifier_checks),
            reason="Explicit independent read-back occurred after the matching write.",
            evidence=postcondition_violations,
        )
        if verifier_checks
        else _dimension(
            "not_applicable",
            weight=STEP_WEIGHTS["postcondition_verification"],
            score=None,
            numerator=None,
            denominator=0,
            reason="The frozen task spec defines no independent postcondition verifier.",
        )
    )

    tool_effects: dict[str, str] = {}
    for milestone in milestones:
        if not isinstance(milestone, Mapping):
            continue
        actions = milestone.get("acceptable_actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if isinstance(action, Mapping):
                tool_effects[str(action["tool"])] = str(
                    milestone.get("effect", "read")
                )
    limits = task_spec.get("active_tool_call_limits")
    limits = limits if isinstance(limits, Mapping) else {}
    successful_tool_counts: Counter[str] = Counter()
    minefield_violations: list[dict[str, Any]] = []
    successful_writes = 0
    for index, call in enumerate(calls):
        if _call_failed(call):
            continue
        tool = _call_name(call)
        effect = tool_effects.get(tool, _tool_effect(tool, {}))
        if effect not in {"create", "modify", "delete"}:
            continue
        successful_writes += 1
        successful_tool_counts[tool] += 1
        allowed = int(limits.get(tool, 0))
        if not allowed:
            minefield_violations.append(
                {"call_index": index, "tool": tool, "reason": "unregistered_write"}
            )
        elif successful_tool_counts[tool] > allowed:
            minefield_violations.append(
                {"call_index": index, "tool": tool, "reason": "write_over_limit"}
            )
    dimensions["minefield_avoidance"] = (
        _dimension(
            "scored",
            weight=STEP_WEIGHTS["minefield_avoidance"],
            score=1 - len(minefield_violations) / successful_writes,
            numerator=successful_writes - len(minefield_violations),
            denominator=successful_writes,
            reason="Successful writes stayed within registered tools and cardinality.",
            evidence=minefield_violations,
        )
        if successful_writes
        else _dimension(
            "not_applicable",
            weight=STEP_WEIGHTS["minefield_avoidance"],
            score=None,
            numerator=None,
            denominator=0,
            reason="No successful mutating action exposed a minefield opportunity.",
        )
    )

    relevant_tools = {
        str(action["tool"])
        for milestone in milestones
        if isinstance(milestone, Mapping)
        for action in milestone.get("acceptable_actions", [])
        if isinstance(action, Mapping)
    }
    attempt_completion_tools = {
        str(action["tool"])
        for milestone in milestones
        if isinstance(milestone, Mapping)
        and milestone.get("completion_mode") == "attempt"
        for action in milestone.get("acceptable_actions", [])
        if isinstance(action, Mapping)
    }
    success_completion_tools = {
        str(action["tool"])
        for milestone in milestones
        if isinstance(milestone, Mapping)
        and milestone.get("completion_mode") == "success"
        for action in milestone.get("acceptable_actions", [])
        if isinstance(action, Mapping)
    }
    expected_failure_tools = attempt_completion_tools - success_completion_tools
    recovery_checks: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        tool = _call_name(call)
        if (
            tool not in relevant_tools
            or tool in expected_failure_tools
            or not _call_failed(call)
        ):
            continue
        recovered_at = next(
            (
                later
                for later in range(index + 1, len(calls))
                if _call_name(calls[later]) == tool and not _call_failed(calls[later])
            ),
            None,
        )
        recovery_checks.append(
            {
                "failed_call": index,
                "tool": tool,
                "recovered_call": recovered_at,
                "passed": recovered_at is not None,
            }
        )
    dimensions["recovery_quality"] = (
        _dimension(
            "scored",
            weight=STEP_WEIGHTS["recovery_quality"],
            score=sum(value["passed"] for value in recovery_checks)
            / len(recovery_checks),
            numerator=sum(value["passed"] for value in recovery_checks),
            denominator=len(recovery_checks),
            reason="Relevant failed attempts were followed by a successful retry.",
            evidence=recovery_checks,
        )
        if recovery_checks
        else _dimension(
            "not_applicable",
            weight=STEP_WEIGHTS["recovery_quality"],
            score=None,
            numerator=None,
            denominator=0,
            reason="No relevant failed action created a recovery opportunity.",
        )
    )

    productive_calls = set(used_calls)
    within_limits: Counter[str] = Counter()
    for index, call in enumerate(calls):
        if _call_failed(call):
            continue
        tool = _call_name(call)
        within_limits[tool] += 1
        if within_limits[tool] <= int(limits.get(tool, 0)):
            productive_calls.add(index)
            continue
        output_tokens = _tokens(call.get("raw_output") or call.get("output", ""))
        if any(
            output_tokens & _tokens(later.get("input", {}))
            for later in calls[index + 1 :]
        ):
            productive_calls.add(index)
    action_efficiency = len(productive_calls) / len(calls) if calls else 0.0
    dimensions["action_efficiency"] = _dimension(
        "scored",
        weight=STEP_WEIGHTS["action_efficiency"],
        score=action_efficiency,
        numerator=len(productive_calls),
        denominator=len(calls),
        reason="Calls either completed a milestone, stayed within an allowed split-call "
        "cardinality, or supplied data consumed later.",
        evidence=[
            {"call_index": index, "tool": _call_name(call)}
            for index, call in enumerate(calls)
            if index not in productive_calls
        ],
    )

    applicable = [
        value
        for value in dimensions.values()
        if value["status"] == "scored" and value["score"] is not None
    ]
    applicable_weight = sum(float(value["weight"]) for value in applicable)
    step_score = (
        sum(float(value["weight"]) * float(value["score"]) for value in applicable)
        / applicable_weight
        if applicable_weight
        else 0.0
    )
    semantic_score = (
        semantic_review.get("semantic_checkpoint_score")
        if isinstance(semantic_review, Mapping)
        else None
    )
    if task_spec.get("has_public_checkpoint") is not True:
        semantic_score = None
    if not isinstance(semantic_score, (int, float)):
        semantic_score = None
    combined = step_score * float(semantic_score) if semantic_score is not None else None
    event_types = {
        str(event.get("type") or event.get("event_type"))
        for event in result.get("events", [])
        if isinstance(event, Mapping)
    }
    rehearsal_events = sorted(event_types & REHEARSAL_EVENT_TYPES)
    mechanism_status = (
        "present_not_scored_by_v1"
        if rehearsal_events
        else "not_observable"
    )
    mechanism_metrics = {
        name: {
            "status": mechanism_status,
            "score": None,
            "reason": (
                "Rehearsal events require the paired post-handoff evaluator extension."
                if rehearsal_events
                else "The Vanilla trajectory contains no rehearsal event stream."
            ),
            "evidence": rehearsal_events,
        }
        for name in MECHANISM_METRICS
    }
    return {
        "schema_version": 1,
        "result_scope": (
            "OpenHarness-compatible local rehearsal-aware score; "
            "not an official MCP-Persona score"
        ),
        "spec_sha256": spec_sha256,
        "evaluator_sha256": evaluator_sha256,
        "task_id": int(result["task_id"]),
        "trial": int(result["trial"]),
        "dimensions": dimensions,
        "milestone_matches": milestone_matches,
        "dependency_violations": dependency_violations,
        "precondition_violations": precondition_violations,
        "postcondition_violations": postcondition_violations,
        "minefield_violations": minefield_violations,
        "action_counts": {
            "total": len(calls),
            "successful": sum(not _call_failed(call) for call in calls),
            "failed": sum(_call_failed(call) for call in calls),
            "productive": len(productive_calls),
        },
        "applicable_weight_sum": round(applicable_weight, 6),
        "step_score": round(step_score, 6),
        "semantic_checkpoint_score": (
            round(float(semantic_score), 6) if semantic_score is not None else None
        ),
        "rehearsal_aware_combined": (
            round(combined, 6) if combined is not None else None
        ),
        "mechanism_metrics": mechanism_metrics,
        "rehearsal_event_count": len(rehearsal_events),
    }


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 6) if values else None


def summarize_rehearsal_scores(
    scores: Sequence[Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
    expected_repeats: int,
    allow_partial: bool,
) -> dict[str, Any]:
    """Build the Baseline2 completeness, distribution, stability, and strata summary."""

    task_specs = {
        int(task["task_id"]): task
        for task in spec.get("tasks", [])
        if isinstance(task, Mapping)
    }
    expected_keys = {
        (task_id, trial)
        for task_id in VERIFIED_TASK_IDS
        for trial in range(1, expected_repeats + 1)
    }
    keys = [(int(value["task_id"]), int(value["trial"])) for value in scores]
    duplicate_keys = sorted(
        key for key, count in Counter(keys).items() if count > 1
    )
    observed = set(keys)
    unexpected = sorted(observed - expected_keys)
    missing = sorted(expected_keys - observed)
    if duplicate_keys or unexpected:
        raise ValueError(
            f"Invalid score keys: duplicates={duplicate_keys}, unexpected={unexpected}"
        )
    if missing and not allow_partial:
        raise ValueError(f"Baseline2 is incomplete; missing {len(missing)} trials")

    dimension_values: dict[str, list[float]] = defaultdict(list)
    dimension_statuses: dict[str, Counter[str]] = defaultdict(Counter)
    step_scores: list[float] = []
    combined_scores: list[float] = []
    for row in scores:
        step_scores.append(float(row["step_score"]))
        combined = row.get("rehearsal_aware_combined")
        if isinstance(combined, (int, float)):
            combined_scores.append(float(combined))
        dimensions = row.get("dimensions")
        if isinstance(dimensions, Mapping):
            for name, dimension in dimensions.items():
                if not isinstance(dimension, Mapping):
                    continue
                status = str(dimension.get("status"))
                dimension_statuses[str(name)][status] += 1
                score = dimension.get("score")
                if status == "scored" and isinstance(score, (int, float)):
                    dimension_values[str(name)].append(float(score))

    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in scores:
        grouped[int(row["task_id"])].append(row)
    pair_deltas = [
        abs(float(values[0]["step_score"]) - float(values[1]["step_score"]))
        for values in grouped.values()
        if len(values) == 2
    ]
    combined_pair_deltas = [
        abs(
            float(values[0]["rehearsal_aware_combined"])
            - float(values[1]["rehearsal_aware_combined"])
        )
        for values in grouped.values()
        if len(values) == 2
        and isinstance(values[0].get("rehearsal_aware_combined"), (int, float))
        and isinstance(values[1].get("rehearsal_aware_combined"), (int, float))
    ]

    strata: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension_name in ("chain_length", "server_scope", "state_modification"):
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in scores:
            task = task_specs[int(row["task_id"])]
            task_strata = task.get("strata")
            if isinstance(task_strata, Mapping):
                buckets[str(task_strata[dimension_name])].append(row)
        strata[dimension_name] = {
            name: {
                "trial_count": len(values),
                "step_score_mean": _mean(
                    [float(value["step_score"]) for value in values]
                ),
                "combined_coverage": sum(
                    isinstance(value.get("rehearsal_aware_combined"), (int, float))
                    for value in values
                ),
                "combined_mean": _mean(
                    [
                        float(value["rehearsal_aware_combined"])
                        for value in values
                        if isinstance(
                            value.get("rehearsal_aware_combined"), (int, float)
                        )
                    ]
                ),
            }
            for name, values in sorted(buckets.items())
        }

    mechanism_not_observable = all(
        isinstance(row.get("mechanism_metrics"), Mapping)
        and all(
            isinstance(value, Mapping)
            and value.get("status") == "not_observable"
            for value in row["mechanism_metrics"].values()
        )
        for row in scores
    )
    expected_combined_coverage = (
        (len(VERIFIED_TASK_IDS) - len(NO_PUBLIC_CHECKPOINT_TASK_IDS))
        * expected_repeats
    )
    semantic_complete = len(combined_scores) == expected_combined_coverage
    structurally_complete = not missing and not duplicate_keys and not unexpected
    return {
        "schema_version": 1,
        "result_scope": (
            "OpenHarness-compatible local rehearsal-aware baseline; "
            "not an official MCP-Persona score"
        ),
        "expected_task_count": len(VERIFIED_TASK_IDS),
        "expected_trial_count": len(expected_keys),
        "observed_trial_count": len(scores),
        "unique_trial_count": len(observed),
        "missing_trial_keys": [
            {"task_id": task_id, "trial": trial} for task_id, trial in missing
        ],
        "duplicate_trial_keys": [
            {"task_id": task_id, "trial": trial}
            for task_id, trial in duplicate_keys
        ],
        "unexpected_trial_keys": [
            {"task_id": task_id, "trial": trial}
            for task_id, trial in unexpected
        ],
        "structurally_complete": structurally_complete,
        "semantic_review_complete": semantic_complete,
        "baseline_ready": structurally_complete and semantic_complete,
        "step_score": {
            "coverage": len(step_scores),
            "mean": _mean(step_scores),
            "median": _median(step_scores),
            "min": round(min(step_scores), 6) if step_scores else None,
            "max": round(max(step_scores), 6) if step_scores else None,
        },
        "rehearsal_aware_combined": {
            "coverage": len(combined_scores),
            "expected_coverage": expected_combined_coverage,
            "mean": _mean(combined_scores),
            "median": _median(combined_scores),
            "denominator_note": (
                "Only trials for the 45 tasks with public checkpoints; "
                "the 7 no-checkpoint tasks remain null."
            ),
        },
        "dimensions": {
            name: {
                "statuses": dict(sorted(dimension_statuses[name].items())),
                "applicable_trial_count": len(dimension_values[name]),
                "mean": _mean(dimension_values[name]),
            }
            for name in STEP_WEIGHTS
        },
        "stability": {
            "repeat_pair_count": sum(len(values) == 2 for values in grouped.values()),
            "mean_abs_delta_step_score": _mean(pair_deltas),
            "mean_abs_delta_combined": _mean(combined_pair_deltas),
        },
        "diagnostic_breakdowns": strata,
        "rehearsal_event_trial_count": sum(
            int(value.get("rehearsal_event_count", 0)) > 0 for value in scores
        ),
        "mechanism_metrics_status": (
            "not_observable" if mechanism_not_observable else "mixed_or_present"
        ),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a strict JSONL file."""

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not a JSON object")
        rows.append(value)
    return rows


def write_json(path: Path, value: Any) -> None:
    """Atomically write deterministic, human-readable JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    """Atomically write JSONL."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    temporary.replace(path)
