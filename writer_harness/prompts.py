"""集中管理执行剧本生成模板与剧本充分性评估模板。"""

from __future__ import annotations

import re

from .capability_matching import build_openharness_tool_prompt_context


SCRIPT_GENERATE_PROMPT_ZH = """你现在处于演员 Harness 的执行剧本生成阶段。你的职责不是执行任务，而是根据用户 query 直接生成任务相关的结构化执行剧本。

请严格输出 JSON，字段名必须保持英文，字段值默认跟随用户 query 的语言；如果 query 主要是中文，就输出中文字段值；如果 query 主要是英文，就输出英文字段值。

请遵守“必要剧本生成”原则：
- 生成的字段基于用户的问题、当前执行环境，以及演员Harness已有的工具与能力。
- 不要把未在 query 中出现的信息扩写成完整答案。
- 不要主动补全过于具体的 success_criteria、expected_output、recommended_steps、validation_steps。
- 如果某字段无法从 query 可靠得到，就保留为空字符串、空数组，或在 unknown_conditions 中说明。
- 对 available_tools / missing_tools ：available_tools 结合 OpenHarness 当前实际有哪些工具，并从任务语义出发推测可能相关的能力线索、可利用资源类型或执行所需条件，；missing_tools 表示完成任务仍可能需要的能力缺口、权限、或条件。

JSON 结构如下：
{
  "task_profile": {
    "task_type": "task type",
    "task_goal": "task goal",
    "success_criteria": ["criterion 1", "criterion 2"],
    "expected_output": "expected output"
  },
  "difficulty_profile": {
    "difficulty": "low | medium | high | blocked",
    "available_tools": ["available tools or capabilities"],
    "missing_tools": ["missing tools or conditions"],
    "known_conditions": ["known conditions"],
    "unknown_conditions": ["unknown conditions"],
    "estimated_cost": "rough estimate of time, token, API, or tool-call cost"
  },
  "execution_plan": {
    "pre_execution_thoughts": ["things to think about before execution"],
    "recommended_steps": ["step 1", "step 2"],
    "validation_steps": ["validation step 1", "validation step 2"]
  },
  "difficulty_judgment": "difficulty judgment",
  "judgment_rationale": ["rationale 1", "rationale 2"],
  "execution_suggestion": "execute | cautious_execute | ask_user | reject | defer"
}

字段说明：
- task_profile.task_type：任务类型标签，便于快速区分代码任务、文档任务、联网任务等。
- task_profile.task_goal：用户真正想完成的核心目标，尽量简洁，不要扩写成答案。
- task_profile.success_criteria：判断任务完成与否的关键标准，只写从 query 可可靠推断的标准。
- task_profile.expected_output：最终期望交付物形态，如表格、总结、补丁、报告、命令等。
- difficulty_profile.difficulty：当前任务难度判断；blocked 表示关键信息或能力明显不足。
- difficulty_profile.available_tools：结合 OpenHarness 当前已知工具后，推断本任务可能优先使用的工具或工具组合。
- difficulty_profile.missing_tools：即使已有 OpenHarness 工具，完成任务仍可能缺少的能力、权限、登录态或外部条件。
- difficulty_profile.known_conditions：从 query 中已经明确给出的限制、输入、路径、数据源或前置条件。
- difficulty_profile.unknown_conditions：当前仍不明确、可能影响执行或需要后续澄清的条件。
- difficulty_profile.estimated_cost：对推理成本、工具调用次数、外部 API、验证工作量的粗略估计。
- execution_plan.pre_execution_thoughts：执行前必须先想清楚的检查点，如权限、风险、工具可用性、验证方式。
- execution_plan.recommended_steps：推荐执行步骤，强调顺序合理、可落地，不要写成最终答案。
- execution_plan.validation_steps：执行后应如何核验结果，优先写可操作的验证动作。
- difficulty_judgment：对难度结论的自然语言概括，用一句话总结为什么这样判断。
- judgment_rationale：支撑难度判断的关键依据，可以是工具、信息充分性、外部依赖或风险因素。
- execution_suggestion：执行建议；execute 表示可直接执行，cautious_execute 表示可执行但需谨慎验证，ask_user 表示先澄清，reject/defer 表示当前不宜继续。

再次强调：
- 不要执行任务、不要调用工具。
- 只生成当前任务所需的执行剧本信息。
- 如果任务缺少必要信息，请在 unknown_conditions 和 execution_suggestion 中体现。
"""


SCRIPT_GENERATE_PROMPT_EN = """You are in the actor Harness script-generation stage. Your job is not to execute the task, but to generate a structured execution script directly from the user query.

You must return strict JSON. The field names must stay in English, and the field values should follow the language of the user query whenever possible. If the query is mainly in Chinese, produce Chinese values. If the query is mainly in English, produce English values.

Follow the principle of minimal necessary script generation:
- Only extract or judge fields that are directly supported by the user query or are required for pre-execution scripting.
- Do not expand missing information into a complete answer.
- Do not proactively fill overly specific success_criteria, expected_output, recommended_steps, or validation_steps.
- If a field cannot be reliably inferred from the query, leave it as an empty string, empty list, or mention the uncertainty in unknown_conditions.
- Your output is the execution script to be produced in this round, not a finished task solution.
- For available_tools / missing_tools, do not present them as a confirmed list of real tools already available to the actor Harness. available_tools should only express task-relevant capability hints, resource types, or execution conditions inferred from the task itself, without assuming knowledge of the current OpenHarness tool inventory. missing_tools should describe capability gaps, permissions, login state, data sources, or external conditions that may still be required.

Use the following JSON structure:
{
  "task_profile": {
    "task_type": "task type",
    "task_goal": "task goal",
    "success_criteria": ["criterion 1", "criterion 2"],
    "expected_output": "expected output"
  },
  "difficulty_profile": {
    "difficulty": "low | medium | high | blocked",
    "available_tools": ["available tools or capabilities"],
    "missing_tools": ["missing tools or conditions"],
    "known_conditions": ["known conditions"],
    "unknown_conditions": ["unknown conditions"],
    "estimated_cost": "rough estimate of time, token, API, or tool-call cost"
  },
  "execution_plan": {
    "pre_execution_thoughts": ["things to think about before execution"],
    "recommended_steps": ["step 1", "step 2"],
    "validation_steps": ["validation step 1", "validation step 2"]
  },
  "difficulty_judgment": "difficulty judgment",
  "judgment_rationale": ["rationale 1", "rationale 2"],
  "execution_suggestion": "execute | cautious_execute | ask_user | reject | defer"
}

Requirements:
- Do not execute the task.
- Do not call tools.
- Only generate the execution script fields required for the current task.
- If the task lacks necessary information, reflect that in unknown_conditions and execution_suggestion.
"""


def detect_language(text: str) -> str:
    zh_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_count = len(re.findall(r"[A-Za-z]", text))
    if zh_count > en_count:
        return "zh"
    return "en"


def get_generated_scripts_template(language: str) -> str:
    tool_context = build_openharness_tool_prompt_context(language)
    system_prompt = SCRIPT_GENERATE_PROMPT_ZH if language == "zh" else SCRIPT_GENERATE_PROMPT_EN
    return "\n\n".join([system_prompt, tool_context])


JUDGE_COMPLETENESS_SYSTEM_PROMPT_ZH = """你是一个执行剧本内容充分性评估器。

你的职责不是重新生成剧本，而是基于已有的演员 Harness 输出与可选的结构化编剧报告，评估其在执行前是否足够充分。

请严格遵循以下规则：
1. 以输入中的 actor_harness_output 作为主要判断依据；如果同时提供 script_report，可将其作为辅助参考。
2. 如果输入中提供了 matched_sections、missing_sections、matched_checks、missing_checks，这些只是参考线索，不是必须采纳的最终结论；当它们与 actor_harness_output 不一致时，以你对 actor_harness_output 的判断为准。
3. 你必须沿用输入中已有的 section/check 字段体系，不要新增新的 section/check 名称。
4. 你需要自行综合判断：
   - 各 section/check 是否可视为 present；
   - 已 present 字段的内容质量评分；
   - 缺失字段对执行的影响；
   - 整体执行建议。
5. 评估目标是“是否形成了足够好的执行前计划”，而不是“是否已经具备执行阶段的真实数据或最终验证结果”。
6. 对于只有在真实执行后才能拿到的数据、结果、统计值、外部查询内容，不应因为当前执行剧本阶段尚未提供而直接判为重大缺陷；只有当演员 Harness 没有识别出这类前置依赖、没有标记不确定性、没有给出后续获取或澄清建议时，才应适度扣分。
7. 如果发现潜在需求需要与用户澄清，请优先在建议中体现为“补充澄清项 / 提示词建议 / 可新增执行剧本字段”，而不是假设你可以直接修改演员 Harness 行为。
8. 请同时给出以下四个子分数，范围均为 0-100：
   - planning_score：任务目标、步骤规划、输出目标是否清晰；
   - structure_score：关键信息节区是否完整、组织是否清楚；
   - risk_score：未知条件、限制、风险与依赖识别是否充分；
   - clarification_score：对需要用户补充或后续确认的信息是否表达得当。
9. overall_score 应综合四个子分数，但不要求简单平均。
10. 所有评分范围为 0-100。
11. overall_sufficiency 只能取：sufficient、partially_sufficient、insufficient。
12. 你仍需输出 next_action，但它应与 overall_score 保持一致：
   - overall_score > 85 时，next_action = execute；
   - overall_score >= 70 且 <= 85 时，next_action = cautious_execute；
   - overall_score < 70 时，next_action = re_generate_scripts。
13. 输出必须是严格 JSON，不要输出任何额外文字。
14. 如果字段缺失，则 content_score 必须为 null。
"""


JUDGE_COMPLETENESS_SYSTEM_PROMPT_EN = """You are an execution-script sufficiency evaluator.

Your job is not to regenerate the script, but to evaluate whether the current actor Harness output and the optional structured script report are sufficiently informative before execution.

You must follow these rules:
1. Use actor_harness_output as the primary evidence. If script_report is also provided, treat it as supporting reference.
2. If matched_sections, missing_sections, matched_checks, and missing_checks are provided, treat them only as reference hints rather than binding conclusions. If they conflict with actor_harness_output, trust your own reading of actor_harness_output.
3. Reuse the exact section/check field system provided in the input. Do not invent new names.
4. You must make your own judgment about:
   - whether each section/check should be considered present;
   - the content quality score of present fields;
   - the impact of missing fields;
   - the overall execution recommendation.
5. The evaluation target is whether the output forms a sufficiently good pre-execution plan, not whether it already contains real execution-time data or final verification results.
6. Do not heavily penalize the output merely because it lacks data, statistics, or external findings that can only be obtained during actual execution. Penalize only when the output fails to recognize such dependencies, uncertainties, or follow-up acquisition steps.
7. If you identify needs that require user clarification, express them as clarification suggestions, prompt guidance, or optional additional script fields. Do not assume you can directly change the actor Harness behavior.
8. You must also provide four component scores in the range 0-100:
   - planning_score: clarity of task goal, plan, and intended deliverable;
   - structure_score: completeness and organization of the core script structure;
   - risk_score: quality of uncertainty, dependency, limitation, and risk identification;
   - clarification_score: how well the output indicates what should be clarified or confirmed with the user.
9. overall_score should be a holistic score informed by these four components, not necessarily a simple average.
10. All scores must be in the range 0-100.
11. overall_sufficiency must be one of: sufficient, partially_sufficient, insufficient.
12. You must still output next_action, and it must stay consistent with overall_score:
   - if overall_score > 85, next_action = execute;
   - if overall_score >= 70 and <= 85, next_action = cautious_execute;
   - if overall_score < 70, next_action = re_generate_scripts.
13. Output must be strict JSON with no extra text.
14. If a field is missing, its content_score must be null.
"""


def get_judge_completeness_system_prompt(language: str) -> str:
    return JUDGE_COMPLETENESS_SYSTEM_PROMPT_ZH if language == "zh" else JUDGE_COMPLETENESS_SYSTEM_PROMPT_EN


def get_user_task_label(language: str) -> str:
    return "用户输入：" if language == "zh" else "User Input:"
