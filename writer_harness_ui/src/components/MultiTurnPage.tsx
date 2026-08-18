import { FormEvent, KeyboardEvent, useEffect, useMemo, useState } from "react";
import { useRef } from "react";
import { FloatingTooltip } from "./FloatingTooltip";
import { MarkdownPreview } from "./MarkdownPreview";
import writerIcon from "../icons/writer.svg";
import directorIcon from "../icons/director.svg";
import type { RuntimeSettings } from "./SettingsPage";
import type { CapabilityMatch, ToolTrace, WriterHarnessResponse, WriterResult } from "../types";

type MultiTurnMode = "vanilla" | "writer_harness";

interface MultiTurnRecord {
  id: string;
  query: string;
  answer: string;
  createdAt: string;
  status: "running" | "done" | "failed";
  mode?: MultiTurnMode;
  response?: WriterHarnessResponse;
  error?: string;
}

interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: number;
  turns: MultiTurnRecord[];
}

interface PersistedMultiTurnState {
  conversations: Conversation[];
  activeConversationId: string;
  writerHarnessEnabled: boolean;
  directorHarnessEnabled: boolean;
}

const storageKey = "writer-harness-ui-multiturn-state";

function createId(prefix: string) {
  return `${prefix}-${typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function createConversation(): Conversation {
  return { id: createId("session"), title: "", createdAt: new Date().toLocaleString("zh-CN", { hour12: false }), updatedAt: Date.now(), turns: [] };
}

function getConversationLabel(conversation?: Conversation) {
  const title = conversation?.title.trim();
  if (title) return title;
  const firstQuery = conversation?.turns[0]?.query.trim();
  return firstQuery ? `${firstQuery.slice(0, 10)}${firstQuery.length > 10 ? "…" : ""}` : "未命名会话";
}

function loadState(): PersistedMultiTurnState {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey) || "{}") as Partial<PersistedMultiTurnState>;
    const conversations = Array.isArray(parsed.conversations) && parsed.conversations.length ? parsed.conversations : [createConversation()];
    const activeConversationId = conversations.some((conversation) => conversation.id === parsed.activeConversationId) ? parsed.activeConversationId! : conversations[0].id;
    return { conversations, activeConversationId, writerHarnessEnabled: parsed.writerHarnessEnabled !== false, directorHarnessEnabled: parsed.directorHarnessEnabled === true };
  } catch {
    const conversation = createConversation();
    return { conversations: [conversation], activeConversationId: conversation.id, writerHarnessEnabled: true, directorHarnessEnabled: false };
  }
}

function getAnswer(result?: WriterResult) {
  return result?.execution?.execution_output || result?.execution?.stdout || result?.actor_harness_output || result?.error || "当前未返回可展示的执行结果。";
}

function getScript(result?: WriterResult) {
  return result?.final_script_report || result?.script_report || null;
}

function getToolCounts(trace?: ToolTrace) {
  const counts = new Map<string, number>();
  const names = (trace?.tool_events || []).filter((event) => event.type === "tool_started").map((event) => event.tool_name || "unknown");
  const sequence = names.length ? names : (trace?.tool_sequence || []).filter((item) => item.type === "start").map((item) => item.name || "unknown");
  sequence.forEach((name) => counts.set(name, (counts.get(name) || 0) + 1));
  return [...counts.entries()];
}

function getDirectorStatusLabel(status?: string) {
  if (status === "passed" || status === "repaired") return "通过";
  if (status === "failed" || status === "deny") return "失败";
  if (status === "skipped") return "跳过";
  return status || "未标记";
}

function getScoreTone(score?: number | null) {
  if (typeof score !== "number") return "default";
  if (score >= 85) return "success";
  if (score >= 70) return "warning";
  return "danger";
}

function getActionLabel(action?: string | null) {
  if (action === "execute") return "可执行";
  if (action === "direct_execute") return "直接执行";
  if (action === "cautious_execute") return "谨慎执行";
  if (action === "re_generate_scripts") return "重新生成剧本";
  if (action === "blocked") return "阻断执行";
  return action || "未判断";
}

function getPlanActionLabel(action?: string) {
  if (action === "new_plan") return "生成新计划";
  if (action === "refine_existing_plan") return "微调已有计划";
  if (action === "reuse_existing_plan") return "复用已有计划";
  if (action === "split_existing_plan") return "拆分已有计划";
  if (action === "continue_existing_plan") return "接续已有计划";
  return action || "未判断";
}

function formatDuration(value?: number | null) {
  return typeof value === "number" ? `${value.toFixed(value < 1 ? 2 : 1)}s` : "-";
}

function ScriptValue({ label, value, tone = "default" }: { label: string; value: unknown; tone?: string }) {
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) return null;
  const content = Array.isArray(value) ? value.map(String).join("、") : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  return <div className={`multiturn-script__item multiturn-script__item--${tone}`}><span>{label}</span><div>{content}</div></div>;
}

function ScriptTooltipSection({ title, items }: { title: string; items: Array<[string, unknown]> }) {
  const availableItems = items.filter(([, value]) => value !== null && value !== undefined && value !== "" && (!Array.isArray(value) || value.length));
  if (!availableItems.length) return null;
  return <section className="multiturn-script-tooltip__section"><h4>{title}</h4><div>{availableItems.map(([label, value]) => <ScriptValue key={label} label={label} value={value} />)}</div></section>;
}

function displayValue(value: unknown) {
  if (Array.isArray(value)) return value.map(String).join("、");
  return value === null || value === undefined ? "" : String(value);
}

function MissingToolRequirements({ value }: { value: unknown }) {
  if (!Array.isArray(value) || !value.length) return null;
  const requirements = value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null);
  if (!requirements.length) return null;
  return <section className="multiturn-script-tooltip__section multiturn-script-tooltip__section--requirements"><h4>缺失功能与实现策略</h4><div className="multiturn-missing-requirements">{requirements.map((requirement, requirementIndex) => {
    const strategies = Array.isArray(requirement.resolution_strategies) ? requirement.resolution_strategies.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null) : [];
    return <article className="multiturn-missing-requirement" key={`${displayValue(requirement.missing_tool || requirement.capability)}-${requirementIndex}`}>
      <header><strong>{displayValue(requirement.missing_tool || requirement.capability || "未命名缺失功能")}</strong>{requirement.description ? <span>{displayValue(requirement.description)}</span> : null}</header>
      {requirement.required_for_steps ? <div className="multiturn-missing-requirement__field"><b>动作拆解</b><p>{displayValue(requirement.required_for_steps)}</p></div> : null}
      {strategies.length ? <div className="multiturn-missing-requirement__strategies">{strategies.map((strategy, strategyIndex) => <div className="multiturn-resolution-strategy" key={`${displayValue(strategy.strategy_type)}-${strategyIndex}`}><strong>{displayValue(strategy.strategy_type || "候选策略")}</strong>{strategy.description ? <p>{displayValue(strategy.description)}</p> : null}{strategy.tool_chain ? <p><b>工具链：</b>{displayValue(strategy.tool_chain)}</p> : null}{strategy.preconditions ? <p><b>前置条件：</b>{displayValue(strategy.preconditions)}</p> : null}{strategy.validation ? <p><b>验证：</b>{displayValue(strategy.validation)}</p> : null}{strategy.risk ? <p><b>风险：</b>{displayValue(strategy.risk)}</p> : null}</div>)}</div> : <div className="multiturn-missing-requirement__empty">尚未生成可执行的补全策略</div>}
      {requirement.selection_rule ? <div className="multiturn-missing-requirement__field"><b>选择规则</b><p>{displayValue(requirement.selection_rule)}</p></div> : null}
      {requirement.unresolved_action ? <div className="multiturn-missing-requirement__field multiturn-missing-requirement__field--warning"><b>不可用时</b><p>{displayValue(requirement.unresolved_action)}</p></div> : null}
    </article>;
  })}</div></section>;
}

function CapabilityMatchSection({ capabilityMatch }: { capabilityMatch?: CapabilityMatch | null }) {
  if (!capabilityMatch) return null;
  const details = capabilityMatch.tool_match_details || [];
  const availableTools = capabilityMatch.available_tools || [];
  const missingTools = capabilityMatch.missing_tools || [];
  const requiredCapabilities = capabilityMatch.required_capabilities || [];
  if (!details.length && !availableTools.length && !missingTools.length && !requiredCapabilities.length) return null;
  return <section className="multiturn-script-tooltip__section multiturn-script-tooltip__section--capabilities"><h4>工具检索与能力覆盖</h4><div className="multiturn-capability-match">
    <div className="multiturn-capability-match__meta"><span>置信度：{capabilityMatch.tool_match_confidence || "未标记"}</span>{capabilityMatch.tool_match_rationale ? <p>{capabilityMatch.tool_match_rationale}</p> : null}</div>
    {requiredCapabilities.length ? <ScriptValue label="检索到的能力" value={requiredCapabilities} /> : null}
    {availableTools.length ? <ScriptValue label="可直接使用或组合的工具" value={availableTools} tone="success" /> : null}
    {missingTools.length ? <ScriptValue label="待补齐的能力" value={missingTools} tone="warning" /> : null}
    {details.length ? <div className="multiturn-capability-match__details">{details.map((detail, index) => <article key={`${detail.required_capability || "capability"}-${index}`}><header><strong>{detail.required_capability || "未命名能力"}</strong><em className={`multiturn-capability-match__coverage multiturn-capability-match__coverage--${detail.coverage || "unknown"}`}>{detail.coverage === "high" ? "已覆盖" : detail.coverage === "missing" ? "待补齐" : detail.coverage || "未标记"}</em></header>{detail.matched_tools?.length ? <p><b>候选工具：</b>{detail.matched_tools.join("、")}</p> : null}{detail.reason ? <p><b>检索依据：</b>{detail.reason}</p> : null}</article>)}</div> : null}
  </div></section>;
}

function ScriptTooltip({ result, response }: { result?: WriterResult; response?: WriterHarnessResponse }) {
  const script = getScript(result) || {};
  const taskProfile = typeof script.task_profile === "object" && script.task_profile !== null ? script.task_profile as Record<string, unknown> : {};
  const difficultyProfile = typeof script.difficulty_profile === "object" && script.difficulty_profile !== null ? script.difficulty_profile as Record<string, unknown> : {};
  const executionPlan = typeof script.execution_plan === "object" && script.execution_plan !== null ? script.execution_plan as Record<string, unknown> : {};
  const evaluation = result?.judge_completeness_evaluation;
  return <div className="multiturn-script-tooltip"><ScriptTooltipSection title="多轮上下文" items={[["已注入历史轮数", response?.history_turn_count], ["历史上下文", response?.history_context_enabled ? "已启用" : "未启用"], ["计划处理", getPlanActionLabel(response?.plan_decision?.action)], ["计划处理依据", response?.plan_decision?.reason], ["计划引用方式", response?.plan_decision?.plan_reference_usage], ["本轮业务目标", response?.plan_decision?.effective_task_goal], ["实际执行输入", response?.effective_query]]} /><ScriptTooltipSection title="任务基本信息" items={[["任务类型", taskProfile.task_type], ["任务目标", taskProfile.task_goal || script.task_goal || script.objective || script.goal], ["预期输出", taskProfile.expected_output || script.expected_output], ["成功标准", taskProfile.success_criteria || script.success_criteria]]} /><CapabilityMatchSection capabilityMatch={result?.capability_match} /><ScriptTooltipSection title="任务难度信息" items={[["难度判断", difficultyProfile.difficulty || script.difficulty_judgment], ["可用能力", difficultyProfile.available_tools], ["缺失功能 / 专用工具", difficultyProfile.missing_tools], ["已知条件", difficultyProfile.known_conditions || script.known_conditions], ["风险与未知项", difficultyProfile.unknown_conditions || script.unknown_conditions || script.unknown_or_risk || script.risks]]} /><MissingToolRequirements value={difficultyProfile.missing_tool_requirements} /><ScriptTooltipSection title="执行计划" items={[["执行前思考", executionPlan.pre_execution_thoughts], ["推荐步骤", executionPlan.recommended_steps || script.recommended_steps || script.steps], ["验证步骤", executionPlan.validation_steps], ["执行建议", script.execution_suggestion || result?.judge_next_action || result?.next_action], ["执行分档", result?.execution?.score_band], ["执行决策依据", result?.execution?.decision_rationale]]} /><ScriptTooltipSection title="评分" items={[["综合", result?.judge_overall_score ?? evaluation?.overall_score], ["规划", evaluation?.planning_score], ["结构", evaluation?.structure_score], ["风险", evaluation?.risk_score], ["澄清", evaluation?.clarification_score], ["充分性", evaluation?.overall_sufficiency]]} /></div>;
}

function getOverallScore(result?: WriterResult) {
  const score = result?.judge_overall_score ?? result?.judge_completeness_evaluation?.overall_score;
  return typeof score === "number" ? score : null;
}

function TurnArtifacts({ mode, result, activeDirectorEventId, onDirectorEventToggle }: { mode?: MultiTurnMode; result?: WriterResult; activeDirectorEventId: string | null; onDirectorEventToggle: (eventId: string) => void }) {
  const trace = result?.execution?.tool_trace;
  const toolCounts = getToolCounts(trace);
  const directorEvents = trace?.director_events || [];
  if (!toolCounts.length && !directorEvents.length) return null;
  return (
    <div className="multiturn-artifacts">
      {toolCounts.length ? <details><summary>工具调用统计（{toolCounts.reduce((total, [, count]) => total + count, 0)}）</summary><div className="multiturn-artifacts__tools">{toolCounts.map(([name, count]) => <span key={name}>{name} × {count}</span>)}</div></details> : null}
      {directorEvents.length ? <details className="multiturn-director-events"><summary>Director 检查事件（{directorEvents.length}）</summary><div className="multiturn-director-events__list">{directorEvents.map((event, index) => { const eventId = `${event.tool_use_id || event.event || "event"}-${index}`; return <details className="multiturn-director-event" key={eventId} open={activeDirectorEventId === eventId}><summary onClick={(clickEvent) => { clickEvent.preventDefault(); onDirectorEventToggle(eventId); }}><span className="multiturn-director-event__index">{index + 1}</span><strong>{event.event || "director_event"}</strong><span>{event.requested_tool_name || event.tool_name || "未关联工具"}</span><em className={`multiturn-director-event__status multiturn-director-event__status--${event.status || "unknown"}`}>{getDirectorStatusLabel(event.status)}</em></summary><div className="multiturn-director-event__detail"><ScriptValue label="实际工具" value={event.tool_name} /><ScriptValue label="请求工具" value={event.requested_tool_name} /><ScriptValue label="说明" value={event.detail} /><ScriptValue label="会话 ID" value={event.session_id} /><ScriptValue label="调用 ID" value={event.tool_use_id} /><ScriptValue label="附加数据" value={event.data} /></div></details>; })}</div></details> : null}
    </div>
  );
}

export function MultiTurnPage({ runtimeSettings }: { runtimeSettings: RuntimeSettings }) {
  const initial = useMemo(loadState, []);
  const [conversations, setConversations] = useState<Conversation[]>(initial.conversations);
  const [activeConversationId, setActiveConversationId] = useState(initial.activeConversationId);
  const [writerHarnessEnabled, setWriterHarnessEnabled] = useState(initial.writerHarnessEnabled);
  const [directorHarnessEnabled, setDirectorHarnessEnabled] = useState(initial.directorHarnessEnabled);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [writerModeTooltipVisible, setWriterModeTooltipVisible] = useState(false);
  const [directorModeTooltipVisible, setDirectorModeTooltipVisible] = useState(false);
  const [deleteTooltipConversationId, setDeleteTooltipConversationId] = useState<string | null>(null);
  const [scriptTooltipTurnId, setScriptTooltipTurnId] = useState<string | null>(null);
  const [activeDirectorEventId, setActiveDirectorEventId] = useState<string | null>(null);
  const writerModeButtonRef = useRef<HTMLButtonElement>(null);
  const directorModeButtonRef = useRef<HTMLButtonElement>(null);
  const deleteButtonRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const scriptLabelRefs = useRef<Record<string, HTMLSpanElement | null>>({});
  const activeConversation = conversations.find((conversation) => conversation.id === activeConversationId) || conversations[0];

  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify({ conversations, activeConversationId, writerHarnessEnabled, directorHarnessEnabled }));
  }, [conversations, activeConversationId, writerHarnessEnabled, directorHarnessEnabled]);

  useEffect(() => {
    const closeDirectorEvent = (event: MouseEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest(".multiturn-director-event")) setActiveDirectorEventId(null);
    };
    window.addEventListener("mousedown", closeDirectorEvent);
    return () => window.removeEventListener("mousedown", closeDirectorEvent);
  }, []);

  useEffect(() => {
    const closeScriptTooltip = (event: MouseEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest(".multiturn-script-trigger, .multiturn-script-tooltip-layer")) setScriptTooltipTurnId(null);
    };
    window.addEventListener("mousedown", closeScriptTooltip);
    return () => window.removeEventListener("mousedown", closeScriptTooltip);
  }, []);

  function updateConversation(id: string, updater: (conversation: Conversation) => Conversation) {
    setConversations((current) => current.map((conversation) => conversation.id === id ? updater(conversation) : conversation));
  }

  function handleNewConversation() {
    if (loading) return;
    const conversation = createConversation();
    setConversations((current) => [conversation, ...current]);
    setActiveConversationId(conversation.id);
    setQuery("");
  }

  function handleConversationTitleChange(title: string) {
    if (!activeConversation) return;
    updateConversation(activeConversation.id, (conversation) => ({ ...conversation, title, updatedAt: Date.now() }));
  }

  async function handleClearActiveConversationHistory() {
    if (loading || !activeConversation || !activeConversation.turns.length) return;
    try {
      const response = await fetch("/api/writer-harness-multiturn-reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sessionId: activeConversation.id }) });
      if (!response.ok) throw new Error("后端历史清空失败");
      updateConversation(activeConversation.id, (conversation) => ({ ...conversation, updatedAt: Date.now(), turns: [] }));
      setScriptTooltipTurnId(null);
      setActiveDirectorEventId(null);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }

  async function handleDeleteConversation(conversationId: string) {
    if (loading) return;
    try {
      const response = await fetch("/api/writer-harness-multiturn-reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sessionId: conversationId }) });
      if (!response.ok) throw new Error("后端会话删除失败");
      setConversations((current) => {
        const remaining = current.filter((conversation) => conversation.id !== conversationId);
        const next = remaining.length ? remaining : [createConversation()];
        if (activeConversationId === conversationId) setActiveConversationId(next.slice().sort((a, b) => b.updatedAt - a.updatedAt)[0].id);
        return next;
      });
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }

  async function handleDeleteTurn(turnId: string) {
    if (loading || !activeConversation) return;
    const deletedTurn = activeConversation.turns.find((turn) => turn.id === turnId);
    updateConversation(activeConversation.id, (conversation) => ({ ...conversation, updatedAt: Date.now(), turns: conversation.turns.filter((turn) => turn.id !== turnId) }));
    try {
      const response = await fetch("/api/writer-harness-multiturn-delete-turn", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sessionId: activeConversation.id, turnId }) });
      const result = await response.json() as { deleted?: boolean; error?: string };
      if (!response.ok || result.deleted !== true) throw new Error(result.error || "后端历史轮次删除失败");
      setScriptTooltipTurnId((current) => current === turnId ? null : current);
      setActiveDirectorEventId(null);
    } catch (error) {
      if (deletedTurn) updateConversation(activeConversation.id, (conversation) => ({ ...conversation, turns: [...conversation.turns, deletedTurn].sort((a, b) => a.id.localeCompare(b.id)) }));
    }
  }

  async function handleCopy(text: string) {
    await navigator.clipboard?.writeText(text);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || loading || !activeConversation) return;
    const mode: MultiTurnMode = writerHarnessEnabled ? "writer_harness" : "vanilla";
    const turn: MultiTurnRecord = { id: createId("turn"), query: trimmed, answer: "", createdAt: new Date().toLocaleTimeString("zh-CN", { hour12: false }), status: "running", mode };
    updateConversation(activeConversation.id, (conversation) => ({ ...conversation, updatedAt: Date.now(), turns: [...conversation.turns, turn] }));
    setQuery("");
    setLoading(true);
    try {
      const response = await fetch("/api/writer-harness-multiturn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: trimmed, sessionId: activeConversation.id, turnId: turn.id, mode, runtimeSettings, directorHarnessEnabled, executeOutputFormat: "stream-json" }),
      });
      const data = await response.json() as WriterHarnessResponse;
      if (!response.ok) throw new Error(data.error || "多轮执行请求失败");
      const result = mode === "writer_harness" ? data.writer_harness : data.vanilla;
      updateConversation(activeConversation.id, (conversation) => ({ ...conversation, updatedAt: Date.now(), turns: conversation.turns.map((item) => item.id === turn.id ? { ...item, status: data.ok === false ? "failed" : "done", answer: getAnswer(result), response: data, error: data.error } : item) }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      updateConversation(activeConversation.id, (conversation) => ({ ...conversation, turns: conversation.turns.map((item) => item.id === turn.id ? { ...item, status: "failed", answer: message, error: message } : item) }));
    } finally {
      setLoading(false);
    }
  }

  async function handleStop() {
    await fetch("/api/writer-harness-stop", { method: "POST" });
    setLoading(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault();
      void handleSubmit(event);
    }
  }

  return (
    <section className="multiturn-page">
      <aside className="conversation-sidebar">
        <button type="button" className="conversation-new-button" onClick={handleNewConversation} disabled={loading}>＋ 新建会话</button>
        <div className="conversation-sidebar__title">历史会话</div>
        <div className="conversation-list">
          {conversations.slice().sort((a, b) => b.updatedAt - a.updatedAt).map((conversation) => (
            <div key={conversation.id} className={`conversation-item-row ${conversation.id === activeConversationId ? "conversation-item-row--active" : ""}`}>
              <button type="button" className={`conversation-item ${conversation.id === activeConversationId ? "conversation-item--active" : ""}`} onClick={() => setActiveConversationId(conversation.id)}>
                <strong>{getConversationLabel(conversation)}</strong><span>{conversation.turns.length} 轮 · {conversation.createdAt}</span>
              </button>
              <button ref={(element) => { deleteButtonRefs.current[conversation.id] = element; }} type="button" className="conversation-item-delete" aria-label={`删除会话 ${getConversationLabel(conversation)}`} onMouseEnter={() => setDeleteTooltipConversationId(conversation.id)} onMouseLeave={() => setDeleteTooltipConversationId(null)} onFocus={() => setDeleteTooltipConversationId(conversation.id)} onBlur={() => setDeleteTooltipConversationId(null)} onClick={() => void handleDeleteConversation(conversation.id)} disabled={loading}>×</button>
              <FloatingTooltip anchorRef={{ current: deleteButtonRefs.current[conversation.id] }} visible={deleteTooltipConversationId === conversation.id} className="conversation-delete-tooltip glass-bubble">删除该会话及其历史记录</FloatingTooltip>
            </div>
          ))}
        </div>
      </aside>
      <div className="multiturn-chat">
        <section className="multiturn-chat-shell">
          <header className="multiturn-chat-shell__header">
            <label className="conversation-title-editor"><input aria-label="会话名称" value={activeConversation?.title || ""} onChange={(event) => handleConversationTitleChange(event.target.value)} placeholder={getConversationLabel(activeConversation)} disabled={loading} maxLength={40} /></label>
            <button type="button" className="conversation-history-clear-button" onClick={() => void handleClearActiveConversationHistory()} disabled={loading || !activeConversation?.turns.length}>清空当前对话</button>
          </header>
          <div className="multiturn-log">
            {!activeConversation?.turns.length ? <div className="chat-empty">开始提问后，此处会保留该会话的多轮上下文与执行回复。</div> : activeConversation.turns.map((turn) => (
              <article className="multiturn-turn" key={turn.id}>
                <div className="dialog-message dialog-message--user"><div className="dialog-message__bubble"><span className="dialog-message__role">你 · {turn.createdAt}</span><MarkdownPreview content={turn.query} compact /></div></div>
                <div className={`multiturn-message multiturn-message--assistant multiturn-message--${turn.status}`}>
                  <div className="multiturn-message__toolbar"><div>{turn.mode === "writer_harness" ? <><span ref={(element) => { scriptLabelRefs.current[turn.id] = element; }} className="multiturn-script-trigger" tabIndex={0} onClick={() => setScriptTooltipTurnId((current) => current === turn.id ? null : turn.id)}>✦ Writer Harness <em className={`multiturn-script-trigger__score multiturn-script-trigger__score--${getScoreTone(getOverallScore(turn.response?.writer_harness))}`}>工具检索 · 剧本 · 决策 {getOverallScore(turn.response?.writer_harness) ?? "-"}</em></span><FloatingTooltip anchorRef={{ current: scriptLabelRefs.current[turn.id] }} visible={scriptTooltipTurnId === turn.id} className="multiturn-script-tooltip-layer glass-bubble" offset={10}><ScriptTooltip result={turn.response?.writer_harness} response={turn.response} /></FloatingTooltip><span className="multiturn-plan-summary"><b>{getPlanActionLabel(turn.response?.plan_decision?.action)}</b></span></> : <span>◉ OpenHarness · 直接执行</span>}</div><div><span className="multiturn-duration-summary">{turn.mode === "writer_harness" ? <>Writer {formatDuration(turn.response?.timing?.writer_harness_seconds)} · 执行 {formatDuration(turn.response?.timing?.execution_seconds)}{typeof turn.response?.timing?.director_seconds === "number" ? ` · Director ${formatDuration(turn.response?.timing?.director_seconds)}` : ""}</> : <>执行 {formatDuration(turn.response?.timing?.total_seconds)}</>}</span><button type="button" onClick={() => void handleCopy(`${turn.query}\n\n${turn.answer}`)}>复制</button><button type="button" onClick={() => void handleDeleteTurn(turn.id)} disabled={loading}>删除</button></div></div>
                  <MarkdownPreview content={turn.status === "running" ? "正在处理本轮请求……" : turn.answer} compact />
                  {turn.status !== "running" && turn.response ? <TurnArtifacts mode={turn.mode} result={turn.mode === "writer_harness" ? turn.response.writer_harness : turn.response.vanilla} activeDirectorEventId={activeDirectorEventId} onDirectorEventToggle={(eventId) => setActiveDirectorEventId((current) => current === eventId ? null : eventId)} /> : null}
                </div>
              </article>
            ))}
          </div>
          <form className="multiturn-composer" onSubmit={handleSubmit}>
            <textarea value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={handleKeyDown} placeholder="输入消息；Enter 发送，Shift + Enter 换行" disabled={loading} rows={3} />
            <div className="multiturn-composer__actions">
              <span className="writer-mode-toggle-wrapper" onMouseEnter={() => setWriterModeTooltipVisible(true)} onMouseLeave={() => setWriterModeTooltipVisible(false)}><button ref={writerModeButtonRef} type="button" className={`writer-mode-toggle ${writerHarnessEnabled ? "writer-mode-toggle--active" : ""}`} aria-pressed={writerHarnessEnabled} onFocus={() => setWriterModeTooltipVisible(true)} onBlur={() => setWriterModeTooltipVisible(false)} onClick={() => { setWriterHarnessEnabled((value) => !value); setWriterModeTooltipVisible(false); }}><span className="writer-mode-toggle__check" aria-hidden="true">✓</span><img className="writer-mode-toggle__icon" src={writerIcon} alt="" aria-hidden="true" />Writer Harness</button><FloatingTooltip anchorRef={writerModeButtonRef} visible={writerModeTooltipVisible} className="writer-mode-tooltip glass-bubble">若不选择，则遵循原版 harness 执行链路</FloatingTooltip></span>
              <span className="writer-mode-toggle-wrapper" onMouseEnter={() => setDirectorModeTooltipVisible(true)} onMouseLeave={() => setDirectorModeTooltipVisible(false)}><button ref={directorModeButtonRef} type="button" className={`writer-mode-toggle writer-mode-toggle--director ${directorHarnessEnabled ? "writer-mode-toggle--director-active" : ""}`} aria-pressed={directorHarnessEnabled} onFocus={() => setDirectorModeTooltipVisible(true)} onBlur={() => setDirectorModeTooltipVisible(false)} onClick={() => { setDirectorHarnessEnabled((value) => !value); setDirectorModeTooltipVisible(false); }}><span className="writer-mode-toggle__check" aria-hidden="true">✓</span><img className="writer-mode-toggle__icon" src={directorIcon} alt="" aria-hidden="true" />Director Harness</button><FloatingTooltip anchorRef={directorModeButtonRef} visible={directorModeTooltipVisible} className="writer-mode-tooltip glass-bubble">对真实工具调用执行预检、MCP 替代与事件审计</FloatingTooltip></span>
              <button type="button" className="composer-action-button composer-action-button--stop" disabled={!loading} onClick={() => void handleStop()}><span className="composer-action-button__icon">■</span><span className="composer-action-button__text">停止</span></button>
              <button type="submit" className="composer-action-button" disabled={loading || !query.trim()}><span className="composer-action-button__icon">➤</span><span className="composer-action-button__text">发送</span></button>
            </div>
          </form>
        </section>
      </div>
    </section>
  );
}
