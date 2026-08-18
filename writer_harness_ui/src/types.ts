export type HarnessStrategy = "vanilla" | "writer_harness";

export type ViewMode = "both" | "vanilla" | "writer_harness";

export interface JudgeFieldScore {
  present?: boolean;
  content_score?: number | null;
  reason?: string;
}

export interface WriterJudgment {
  is_complete?: boolean;
  completeness_level?: string;
  matched_sections?: string[];
  missing_sections?: string[];
  matched_checks?: string[];
  missing_checks?: string[];
  rationale?: string;
  next_action?: string;
}

export interface JudgeEvaluation {
  overall_score?: number | null;
  planning_score?: number | null;
  structure_score?: number | null;
  risk_score?: number | null;
  clarification_score?: number | null;
  overall_sufficiency?: string;
  next_action?: string;
  section_scores?: Record<string, JudgeFieldScore>;
  check_scores?: Record<string, JudgeFieldScore>;
  strengths?: string[];
  weaknesses?: string[];
  rationale?: string;
}

export interface ToolTrace {
  tool_events?: Array<{
    type?: string;
    tool_name?: string;
    tool_input?: unknown;
    output?: unknown;
    is_error?: boolean | null;
  }>;
  tool_sequence?: Array<{
    name?: string;
    type?: string;
  }>;
  director_events?: Array<{
    event?: string;
    tool_name?: string;
    requested_tool_name?: string;
    status?: string;
    detail?: string;
    session_id?: string;
    tool_use_id?: string;
    timestamp?: number;
    data?: Record<string, unknown>;
  }>;
  assistant_text?: string;
  used_ask_user_question?: boolean;
}

export interface WriterExecution {
  executed?: boolean;
  status?: string;
  score_band?: string | null;
  decision_rationale?: string | null;
  prompt?: string | null;
  execution_output?: string;
  stdout?: string;
  stderr?: string;
  return_code?: number | null;
  result_ok?: boolean | null;
  final_prompt?: string;
  tool_trace?: ToolTrace;
  execution_seconds?: number;
  prompt_inputs?: Record<string, unknown>;
  plan_action?: "new_plan" | "refine_existing_plan" | "reuse_existing_plan" | "split_existing_plan" | "continue_existing_plan" | string;
  plan_decision_reason?: string;
  plan_reference_usage?: string;
}

export interface CapabilityMatchDetail {
  required_capability?: string;
  matched_tools?: string[];
  coverage?: string;
  reason?: string;
}

export interface CapabilityMatch {
  required_capabilities?: string[];
  available_tools?: string[];
  missing_tools?: string[];
  missing_tool_requirements?: Array<Record<string, unknown>>;
  tool_match_details?: CapabilityMatchDetail[];
  tool_match_confidence?: string;
  tool_match_rationale?: string;
}

export interface WriterResult {
  mode?: HarnessStrategy | string;
  ok?: boolean;
  return_code?: number | null;
  stderr?: string;
  stdout?: string;
  error?: string;
  final_prompt?: string;
  actor_harness_output?: string;
  script_report?: Record<string, unknown> | null;
  final_script_report?: Record<string, unknown> | null;
  actor_harness_report?: Record<string, unknown> | null;
  online_completeness_judgment?: WriterJudgment;
  judge_completeness_evaluation?: JudgeEvaluation;
  judge_overall_score?: number | null;
  judge_next_action?: string | null;
  next_action?: string;
  regeneration_suggestions?: string[];
  capability_match?: CapabilityMatch | null;
  execution?: WriterExecution;
  timing?: {
    total_seconds?: number;
    plan_decision_seconds?: number;
    writer_harness_seconds?: number;
    execution_seconds?: number;
    director_seconds?: number | null;
    multiturn_context_injected?: boolean;
  };
  debug?: {
    mode?: string;
    autoExecute?: boolean;
    ohRealRun?: boolean;
    missingEnv?: string[];
    command?: string[];
    cwd?: string;
    stderrPreview?: string;
    stderrFull?: string;
    stdoutPreview?: string;
    stdoutFull?: string;
  };
}

export interface WriterHarnessResponse {
  ok?: boolean;
  query?: string;
  effective_query?: string;
  session_id?: string;
  turn_id?: string;
  history_turn_count?: number;
  history_context_enabled?: boolean;
  plan_decision?: {
    action?: "new_plan" | "refine_existing_plan" | "reuse_existing_plan" | "split_existing_plan" | "continue_existing_plan" | string;
    turn_intent?: "execute_task" | "user_plan_generation" | string;
    effective_task_goal?: string;
    reason?: string;
    plan_reference_usage?: string;
    prior_turn_id?: string | null;
  };
  session?: {
    session_id?: string;
    turn_count?: number;
    session_dir?: string;
    reset_supported?: boolean;
  };
  timing?: {
    total_seconds?: number;
    plan_decision_seconds?: number;
    writer_harness_seconds?: number;
    execution_seconds?: number;
    director_seconds?: number | null;
    multiturn_context_injected?: boolean;
  };
  vanilla?: WriterResult;
  writer_harness?: WriterResult;
  error?: string;
  debug?: Record<string, unknown>;
}

export interface ChatTurn {
  id: string;
  query: string;
  createdAt: string;
  status: "running" | "done" | "failed";
  response?: WriterHarnessResponse;
  error?: string;
  progressLabel?: string;
}

export interface KeyValueItem {
  label: string;
  value: string | string[];
  tone?: "default" | "success" | "warning" | "danger";
  span?: "full";
}
