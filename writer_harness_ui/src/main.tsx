import { StrictMode, FormEvent, KeyboardEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import directorIcon from "./icons/openharness.png";
import writerIcon from "./icons/writer.svg";
import agentIcon from "./icons/agent.svg";
import chatIcon from "./icons/chat.svg";
import compareIcon from "./icons/compare.svg";
import hideNavigationIcon from "./icons/hide_navi.svg";
import navigationIcon from "./icons/navi.svg";
import settingIcon from "./icons/setting.svg";
import { FieldBadge } from "./components/FieldBadge";
import { FloatingTooltip } from "./components/FloatingTooltip";
import { HarnessOverviewPage } from "./components/HarnessOverviewPage";
import { JsonSection } from "./components/JsonSection";
import { MarkdownPreview } from "./components/MarkdownPreview";
import { MultiTurnPage } from "./components/MultiTurnPage";
import { SettingsPage, type RuntimeSettings } from "./components/SettingsPage";
import type { ChatTurn, HarnessStrategy, KeyValueItem, ToolTrace, ViewMode, WriterExecution, WriterHarnessResponse, WriterResult } from "./types";

const defaultQuery = "请检查 OpenHarness 当前内置的工具实现文件，并总结哪些属于只读工具，哪些可能产生状态变更。";
const storageKey = "writer-harness-ui-state";

type AppPage = "workspace" | "overview" | "settings" | "multiturn";

interface PersistedAppState {
  query: string;
  turns: ChatTurn[];
  viewMode: ViewMode;
  autoExecute: boolean;
  ohRealRun: boolean;
  sidebarExpanded: boolean;
  currentPage: AppPage;
  runtimeSettings: RuntimeSettings;
}

const defaultRuntimeSettings: RuntimeSettings = {
  writerModel: "",
  writerBaseUrl: "",
  writerApiKey: "",
  actorModel: "",
  actorBaseUrl: "",
  actorApiKey: "",
  actorApiFormat: "",
  ohBin: "",
  openharnessSrc: "",
  directorLogPath: "",
};

function getStrategyLabel(strategy: HarnessStrategy) {
  return strategy === "vanilla" ? "OpenHarness【vanilla】" : "编导 Harness";
}

function getStrategyShortLabel(strategy: HarnessStrategy) {
  return strategy === "vanilla" ? "vanilla" : "Writer Harness";
}

function getStrategyIcon(strategy: HarnessStrategy) {
  return strategy === "vanilla" ? "◉" : "✦";
}

function getConfigSwitchOptions(kind: "oh" | "execute") {
  if (kind === "oh") {
    return { left: "Dry Run", right: "真实 OH" };
  }
  return { left: "仅生成剧本", right: "正式执行" };
}

function ConfigSwitch({ kind, active, onToggle }: { kind: "oh" | "execute"; active: boolean; onToggle: () => void }) {
  const options = getConfigSwitchOptions(kind);
  return (
    <label className="switch-label">
      <button type="button" aria-pressed={active} className={`switch-button ${active ? "switch-button--active" : ""}`} onClick={onToggle}>
        <span className="switch-button__option switch-button__option--left">{options.left}</span>
        <span className="switch-button__option switch-button__option--right">{options.right}</span>
        <span className="switch-button__thumb" />
      </button>
    </label>
  );
}

function getTone(result?: WriterResult): "default" | "success" | "warning" | "danger" {
  if (!result) return "default";
  if (result.ok === false || result.error) return "danger";
  const score = result.judge_overall_score ?? result.judge_completeness_evaluation?.overall_score;
  if (typeof score === "number") {
    if (score > 85) return "success";
    if (score >= 70) return "warning";
    return "danger";
  }
  if (result.ok) return "success";
  return "default";
}

function getActionLabel(action?: string) {
  if (action === "execute") return "可执行";
  if (action === "direct_execute") return "直接执行";
  if (action === "cautious_execute") return "谨慎执行";
  if (action === "re_generate_scripts") return "重新生成剧本";
  if (action === "blocked") return "阻断执行";
  if (action === "unknown" || !action) return "未判断";
  return action;
}

function getSufficiencyLabel(value?: string) {
  if (value === "sufficient") return "充分";
  if (value === "partially_sufficient") return "部分充分";
  if (value === "insufficient") return "不充分";
  return value || "-";
}

function toDisplayValue(value: unknown): string | string[] {
  if (Array.isArray(value)) return value.map((item) => String(item));
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function buildMetricItems(result?: WriterResult): KeyValueItem[] {
  const evaluation = result?.judge_completeness_evaluation;
  const judgment = result?.online_completeness_judgment;
  const execution = result?.execution;
  const mode = result?.mode;
  if (mode === "vanilla") {
    return [
      { label: "执行状态", value: toDisplayValue(execution?.status), tone: execution?.status === "done" ? "success" : execution?.status === "failed" ? "danger" : "default" },
      { label: "执行策略", value: "直接依据用户输入执行 OpenHarness", tone: "default" },
      { label: "下一步", value: getActionLabel(result?.next_action), tone: getTone(result) },
      { label: "决策依据", value: toDisplayValue(execution?.decision_rationale), tone: "default", span: "full" },
    ];
  }
  return [
    { label: "总体评分", value: toDisplayValue(result?.judge_overall_score ?? evaluation?.overall_score), tone: getTone(result) },
    { label: "规划评分", value: toDisplayValue(evaluation?.planning_score), tone: "default" },
    { label: "结构评分", value: toDisplayValue(evaluation?.structure_score), tone: "default" },
    { label: "风险评分", value: toDisplayValue(evaluation?.risk_score), tone: "default" },
    { label: "澄清评分", value: toDisplayValue(evaluation?.clarification_score), tone: "default" },
    { label: "充分性", value: getSufficiencyLabel(evaluation?.overall_sufficiency), tone: "default" },
    { label: "完整性", value: judgment?.is_complete ? "完整" : judgment ? "需补全" : "未判断", tone: judgment?.is_complete ? "success" : judgment ? "warning" : "default" },
    { label: "下一步", value: getActionLabel(result?.judge_next_action || evaluation?.next_action || result?.next_action), tone: getTone(result) },
    { label: "执行状态", value: toDisplayValue(execution?.status), tone: execution?.status === "done" ? "success" : execution?.status === "failed" ? "danger" : "default" },
    { label: "决策依据", value: toDisplayValue(execution?.decision_rationale), tone: "default", span: "full" },
  ];
}

function buildJudgmentItems(result?: WriterResult): KeyValueItem[] {
  const judgment = result?.online_completeness_judgment;
  return [
    { label: "完整性层级", value: toDisplayValue(judgment?.completeness_level), tone: "default" },
    { label: "命中分节", value: toDisplayValue(judgment?.matched_sections), tone: "success" },
    { label: "缺失分节", value: toDisplayValue(judgment?.missing_sections), tone: (judgment?.missing_sections?.length || 0) ? "warning" : "success" },
    { label: "缺失检查项", value: toDisplayValue(judgment?.missing_checks), tone: (judgment?.missing_checks?.length || 0) ? "warning" : "success" },
    { label: "判断依据", value: toDisplayValue(judgment?.rationale), tone: "default", span: "full" },
  ];
}

function buildCapabilityItems(result?: WriterResult): KeyValueItem[] {
  const capabilityMatch = result?.capability_match;
  return [
    { label: "匹配置信度", value: toDisplayValue(capabilityMatch?.tool_match_confidence), tone: "default" },
    { label: "检索能力", value: toDisplayValue(capabilityMatch?.required_capabilities), tone: "success" },
    { label: "现有工具 / 组合", value: toDisplayValue(capabilityMatch?.available_tools), tone: "success" },
    { label: "待补齐能力", value: toDisplayValue(capabilityMatch?.missing_tools), tone: (capabilityMatch?.missing_tools?.length || 0) ? "warning" : "success" },
    { label: "检索说明", value: toDisplayValue(capabilityMatch?.tool_match_rationale), tone: "default", span: "full" },
  ];
}

function supportsStructuredScript(strategy: HarnessStrategy) {
  return strategy === "writer_harness";
}

function getScript(strategy: HarnessStrategy, result?: WriterResult) {
  if (!supportsStructuredScript(strategy)) return null;
  return result?.final_script_report || result?.script_report || null;
}

function getPrimaryAnswer(result?: WriterResult) {
  return result?.execution?.execution_output || "";
}

function getErrorContent(result?: WriterResult) {
  return result?.error || result?.stderr || result?.execution?.stderr || result?.debug?.stderrFull || "";
}

function getExecutionAnswer(execution?: WriterExecution) {
  return execution?.execution_output || "";
}

function shouldShowJudgment(strategy: HarnessStrategy, result?: WriterResult) {
  if (strategy !== "writer_harness") return false;
  return Boolean(result?.online_completeness_judgment || result?.judge_completeness_evaluation || (result?.regeneration_suggestions?.length || 0) > 0);
}

function shouldShowMetrics(strategy: HarnessStrategy, result?: WriterResult) {
  if (strategy !== "writer_harness") return false;
  return Boolean(result?.judge_completeness_evaluation);
}

function shouldShowCapabilityMatch(strategy: HarnessStrategy, result?: WriterResult) {
  return strategy === "writer_harness" && Boolean(result?.capability_match);
}

function shouldShowReply(answer: string, executionAnswer: string) {
  if (executionAnswer) return true;
  return Boolean(answer);
}

function getReplyContent(answer: string, executionAnswer: string) {
  return executionAnswer || answer;
}

function getChatBubbleContent(result?: WriterResult) {
  const executionAnswer = getExecutionAnswer(result?.execution);
  const replyAnswer = getPrimaryAnswer(result);
  return executionAnswer || replyAnswer;
}

function getVisibleChatStrategies(viewMode: ViewMode): HarnessStrategy[] {
  if (viewMode === "vanilla") return ["vanilla"];
  if (viewMode === "writer_harness") return ["writer_harness"];
  return ["vanilla", "writer_harness"];
}

function getReplyEmptyMessage(strategy: HarnessStrategy) {
  if (strategy === "vanilla") {
    return "当前还没有返回 execution_output。执行完成后，回复内容会直接展示 OpenHarness 的最终执行结果。";
  }
  return "当前还没有返回 execution_output。执行完成后，回复内容会直接展示最终执行结果。";
}

function getMetricsTitle(strategy: HarnessStrategy) {
  return strategy === "writer_harness" ? "剧本评价指标" : "执行信息";
}

function getScriptTitle(strategy: HarnessStrategy) {
  return strategy === "writer_harness" ? "剧本展示" : "内容展示";
}

function getReplySummary(strategy: HarnessStrategy) {
  return strategy === "vanilla" ? "回复给用户的内容严格取 execution_output，不使用 final_scripts 或 actor_harness_output。" : "回复给用户的内容严格取 execution_output，剧本仅保留在剧本展示区域。";
}

function getStrategyDescription(strategy: HarnessStrategy) {
  return strategy === "vanilla"
    ? "原 OpenHarness 回复策略，直接依据用户输入执行并把执行输出返回给用户。"
    : "先生成结构化剧本并完成门控，最终把真实执行输出返回给用户。";
}

function getStrategyCardTone(strategy: HarnessStrategy) {
  return strategy === "vanilla" ? "vanilla" : "writer_harness";
}

function getStrategyHeaderIcon(strategy: HarnessStrategy) {
  return strategy === "vanilla" ? directorIcon : writerIcon;
}

function inferProgressLabel(result?: WriterResult) {
  if (!result) return "正在准备任务";
  if (result.execution?.status === "done") return "已完成执行与结果整理";
  if (result.execution?.status === "failed") return "执行失败，正在整理报错信息";
  if (result.execution?.status === "skipped") return "剧本已完成，当前未进入执行阶段";
  if (result.execution?.status === "running") {
    const sequence = result.execution?.tool_trace?.tool_sequence || [];
    const latest = sequence[sequence.length - 1];
    if (latest?.name) return `正在执行剧本【${latest.name}】`;
    return "正在执行剧本";
  }
  if (result.final_script_report || result.script_report) return "正在思考并整理执行输出";
  if (result.mode === "writer_harness") return "正在生成剧本";
  return "正在思考与规划执行步骤";
}

function getRunningProgressLabel(turn?: ChatTurn) {
  if (!turn || turn.status !== "running") return "";
  if (turn.progressLabel) return turn.progressLabel;
  const vanilla = inferProgressLabel(turn.response?.vanilla);
  const writerHarness = inferProgressLabel(turn.response?.writer_harness);
  if (turn.response?.writer_harness) return `编导 Harness：${writerHarness}`;
  if (turn.response?.vanilla) return `OpenHarness：${vanilla}`;
  return "正在生成剧本";
}

function sanitizeTurns(turns: ChatTurn[]): ChatTurn[] {
  return turns.map((turn) => (
    turn.status === "running"
      ? { ...turn, status: "failed", error: turn.error || "页面刷新后，前端已恢复上次记录，但该次运行中的任务状态无法继续自动追踪，请重新发起或手动核验后台执行结果。", progressLabel: "页面已刷新，运行中任务待确认" }
      : turn
  ));
}

function loadPersistedState(): PersistedAppState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PersistedAppState>;
    return {
      query: typeof parsed.query === "string" ? parsed.query : defaultQuery,
      turns: Array.isArray(parsed.turns) ? sanitizeTurns(parsed.turns as ChatTurn[]) : [],
      viewMode: parsed.viewMode === "vanilla" || parsed.viewMode === "writer_harness" ? parsed.viewMode : "both",
      autoExecute: parsed.autoExecute !== false,
      ohRealRun: parsed.ohRealRun !== false,
      sidebarExpanded: Boolean(parsed.sidebarExpanded),
      currentPage: parsed.currentPage === "overview" || parsed.currentPage === "settings" || parsed.currentPage === "multiturn" ? parsed.currentPage : "workspace",
      runtimeSettings: {
        writerModel: typeof parsed.runtimeSettings?.writerModel === "string" ? parsed.runtimeSettings.writerModel : "",
        writerBaseUrl: typeof parsed.runtimeSettings?.writerBaseUrl === "string" ? parsed.runtimeSettings.writerBaseUrl : "",
        writerApiKey: typeof parsed.runtimeSettings?.writerApiKey === "string" ? parsed.runtimeSettings.writerApiKey : "",
        actorModel: typeof parsed.runtimeSettings?.actorModel === "string" ? parsed.runtimeSettings.actorModel : "",
        actorBaseUrl: typeof parsed.runtimeSettings?.actorBaseUrl === "string" ? parsed.runtimeSettings.actorBaseUrl : "",
        actorApiKey: typeof parsed.runtimeSettings?.actorApiKey === "string" ? parsed.runtimeSettings.actorApiKey : "",
        actorApiFormat: typeof parsed.runtimeSettings?.actorApiFormat === "string" ? parsed.runtimeSettings.actorApiFormat : "",
        ohBin: typeof parsed.runtimeSettings?.ohBin === "string" ? parsed.runtimeSettings.ohBin : "",
        openharnessSrc: typeof parsed.runtimeSettings?.openharnessSrc === "string" ? parsed.runtimeSettings.openharnessSrc : "",
        directorLogPath: typeof parsed.runtimeSettings?.directorLogPath === "string" ? parsed.runtimeSettings.directorLogPath : "",
      },
    };
  } catch {
    return null;
  }
}

interface ToolCallItem {
  name: string;
  output?: unknown;
  isError?: boolean | null;
}

function ToolCallChip({ call, index }: { call: ToolCallItem; index: number }) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const [tooltipVisible, setTooltipVisible] = useState(false);
  return (
    <>
      <span
        ref={anchorRef}
        className={`tool-call-order__item ${call.isError ? "tool-call-order__item--error" : ""}`}
        tabIndex={0}
        onMouseEnter={() => setTooltipVisible(true)}
        onMouseLeave={() => setTooltipVisible(false)}
        onFocus={() => setTooltipVisible(true)}
        onBlur={() => setTooltipVisible(false)}
      >
        {index + 1}. {call.name}
      </span>
      <FloatingTooltip anchorRef={anchorRef} visible={tooltipVisible} className="tool-call-order__tooltip">
        {getToolCallOutput(call)}
      </FloatingTooltip>
    </>
  );
}

function buildToolCalls(trace?: ToolTrace): ToolCallItem[] {
  const calls: ToolCallItem[] = [];
  const pendingCalls = new Map<string, ToolCallItem[]>();
  for (const event of trace?.tool_events || []) {
    const name = event.tool_name || "unknown";
    if (event.type === "tool_started") {
      const call = { name };
      calls.push(call);
      const pending = pendingCalls.get(name) || [];
      pending.push(call);
      pendingCalls.set(name, pending);
    }
    if (event.type === "tool_completed") {
      const pending = pendingCalls.get(name);
      const call = pending?.shift();
      if (call) {
        call.output = event.output;
        call.isError = event.is_error;
      } else {
        calls.push({ name, output: event.output, isError: event.is_error });
      }
    }
  }
  if (calls.length) return calls;
  return (trace?.tool_sequence || []).filter((item) => item.type === "start").map((item) => ({ name: item.name || "unknown" }));
}

function getToolCallOutput(call: ToolCallItem) {
  if (call.output === undefined || call.output === null) return "该调用尚未返回输出内容";
  return typeof call.output === "string" ? call.output : JSON.stringify(call.output, null, 2);
}

function buildToolSequenceItems(trace?: ToolTrace): KeyValueItem[] {
  const calls = buildToolCalls(trace);
  const askUserQuestionStatus = trace?.used_ask_user_question ? "已调用" : "未调用";
  return [
    { label: "工具调用次数", value: toDisplayValue(calls.length), tone: calls.length ? "success" : "default" },
    { label: "是否调用澄清工具", value: askUserQuestionStatus, tone: trace?.used_ask_user_question ? "warning" : "default" },
  ];
}

function shouldShowToolSequence(result?: WriterResult) {
  return Boolean(result?.execution?.tool_trace?.tool_sequence?.length || result?.execution?.tool_trace?.tool_events?.length);
}

function shouldShowScript(strategy: HarnessStrategy, result?: WriterResult) {
  return strategy === "writer_harness" && Boolean(getScript(strategy, result));
}

function shouldShowJudgmentPlaceholder(strategy: HarnessStrategy) {
  return strategy === "writer_harness";
}

function StrategyPanel({ strategy, result, loading }: { strategy: HarnessStrategy; result?: WriterResult; loading: boolean }) {
  const answer = getPrimaryAnswer(result);
  const script = getScript(strategy, result);
  const errorContent = getErrorContent(result);
  const executionAnswer = getExecutionAnswer(result?.execution);
  const replyContent = getReplyContent(answer, executionAnswer);
  const showScriptPanel = shouldShowScript(strategy, result);
  const showJudgmentPanel = shouldShowJudgment(strategy, result);
  const showMetricsPanel = shouldShowMetrics(strategy, result);
  const showCapabilityMatchPanel = shouldShowCapabilityMatch(strategy, result);
  const showToolSequencePanel = shouldShowToolSequence(result);
  const toolCalls = buildToolCalls(result?.execution?.tool_trace);
  const actionLabel = loading ? "思考中……" : result ? getActionLabel(result.judge_next_action || result.next_action) : "待运行";
  return (
    <section className={`strategy-panel strategy-panel--${getStrategyCardTone(strategy)}`}>
      <div className="strategy-panel__header">
        <div className="strategy-panel__title-group">
          <img src={getStrategyHeaderIcon(strategy)} alt="" aria-hidden="true" className={`strategy-panel__title-icon strategy-panel__title-icon--${strategy}`} />
          <div>
          <h2>{getStrategyLabel(strategy)}</h2>
          <p>{getStrategyDescription(strategy)}</p>
          </div>
        </div>
        <div className="strategy-panel__badge-row">
          <FieldBadge label={getStrategyShortLabel(strategy)} tone={strategy === "vanilla" ? "default" : "success"} />
          <FieldBadge label={actionLabel} tone={loading ? "warning" : getTone(result)} />
        </div>
      </div>
      <div className="strategy-panel__body">
        <div className="content-block">
          <div className="content-toolbar">
            <FieldBadge label="回复内容" tone="default" />
            <span className="inline-note">{getReplySummary(strategy)}</span>
          </div>
          {loading && !replyContent ? <div className="thinking-box">思考中……正在等待 {getStrategyShortLabel(strategy)} 生成回复</div> : shouldShowReply(answer, executionAnswer) ? <MarkdownPreview content={replyContent} compact /> : <div className="empty-box">{getReplyEmptyMessage(strategy)}</div>}
        </div>
        {showMetricsPanel ? (
          <details className="details-panel">
            <summary>{getMetricsTitle(strategy)}</summary>
            <JsonSection title="剧本评价指标" content={result?.judge_completeness_evaluation || null} items={buildMetricItems(result)} tone={getTone(result)} columns={2} />
          </details>
        ) : null}
        {showScriptPanel ? (
          <details className="details-panel" open>
            <summary>{getScriptTitle(strategy)}</summary>
            {script ? <JsonSection title="final_scripts" content={script} tone="default" /> : <div className="empty-box">当前结果尚未生成结构化剧本</div>}
          </details>
        ) : null}
        {showCapabilityMatchPanel ? (
          <details className="details-panel" open>
            <summary>工具检索与能力覆盖</summary>
            <JsonSection title="capability_match" content={result?.capability_match || null} items={buildCapabilityItems(result)} tone="default" />
          </details>
        ) : null}
        {showJudgmentPanel ? (
          <details className="details-panel">
            <summary>完整性判断与补全建议</summary>
            <JsonSection title="online_completeness_judgment" content={result?.online_completeness_judgment || null} items={buildJudgmentItems(result)} tone={getTone(result)} />
            {(result?.regeneration_suggestions?.length || 0) > 0 ? (
              <div className="suggestion-list">
                {result?.regeneration_suggestions?.map((item) => <span key={item}>{item}</span>)}
              </div>
            ) : null}
          </details>
        ) : shouldShowJudgmentPlaceholder(strategy) && !showScriptPanel && !showMetricsPanel ? null : null}
        <details className="details-panel" open={showToolSequencePanel}>
          <summary>工具调用</summary>
          {showToolSequencePanel ? (
            <>
              <JsonSection title="工具调用概览" content={null} items={buildToolSequenceItems(result?.execution?.tool_trace)} tone="default" />
              <div className="tool-call-order" aria-label="工具调用顺序">
                <span className="tool-call-order__label">调用顺序</span>
                <ol>
                  {toolCalls.map((call, index) => <li key={`${call.name}-${index}`}><ToolCallChip call={call} index={index} /></li>)}
                </ol>
              </div>
            </>
          ) : (
            <div className="empty-box">当前执行结果未返回工具调用轨迹</div>
          )}
        </details>
        <details className="details-panel">
          <summary>报错内容{errorContent ? "（有）" : "（无）"}</summary>
          {errorContent ? <div className="error-box"><MarkdownPreview content={errorContent} compact /></div> : <div className="empty-box">当前策略未返回报错内容</div>}
        </details>
      </div>
    </section>
  );
}

function ChatLog({ turns, activeTurn, viewMode }: { turns: ChatTurn[]; activeTurn?: ChatTurn; viewMode: ViewMode }) {
  if (!turns.length) {
    return <div className="chat-empty">输入问题后，这里会展示用户与 harness 的对话历史，包含两种回答。</div>;
  }
  return (
    <div className="chat-log">
      {turns.map((turn) => (
        <div key={turn.id} className="chat-turn">
          <div className="dialog-message dialog-message--user">
            <div className="dialog-message__bubble">
              <span className="dialog-message__role">USER · {turn.createdAt}</span>
              <MarkdownPreview content={turn.query} compact />
            </div>
          </div>
          <div className="dialog-message dialog-message--assistant">
            <div className="dialog-message__bubble dialog-message__bubble--assistant-large">
              <div className={`assistant-compare-row assistant-compare-row--${getVisibleChatStrategies(viewMode).length}`}>
            {getVisibleChatStrategies(viewMode).map((strategy) => {
              const result = turn.response?.[strategy];
              const isRunning = turn.status === "running";
              const bubbleContent = getChatBubbleContent(result);
              return (
                <div key={strategy} className={`assistant-result-block assistant-result-block--${getStrategyCardTone(strategy)}`}>
                    <div className="dialog-message__meta-row">
                      <span className={`strategy-tag strategy-tag--${strategy}`}>
                        <span className="strategy-tag__icon" aria-hidden="true">{getStrategyIcon(strategy)}</span>
                        <span>{getStrategyLabel(strategy)}</span>
                      </span>
                      <FieldBadge label={isRunning && !result ? "思考中……" : result ? getActionLabel(result.judge_next_action || result.next_action) : "待运行"} tone={isRunning && !result ? "warning" : getTone(result)} />
                    </div>
                    <MarkdownPreview content={isRunning && !result ? getRunningProgressLabel(turn) || "思考中……" : bubbleContent || turn.error || "暂无回复"} compact />
                </div>
              );
            })}
              </div>
            </div>
          </div>
        </div>
      ))}
      {activeTurn?.status === "running" ? <div className="running-indicator"><span />{getRunningProgressLabel(activeTurn) || "思考中……两种策略正在生成内容"}</div> : null}
    </div>
  );
}

function getVisibleStrategies(viewMode: ViewMode): HarnessStrategy[] {
  if (viewMode === "vanilla") return ["vanilla"];
  if (viewMode === "writer_harness") return ["writer_harness"];
  return ["vanilla", "writer_harness"];
}

const navigationItems: Array<{ id: AppPage; icon: string; label: string }> = [
  { id: "overview", icon: agentIcon, label: "方法说明" },
  { id: "workspace", icon: compareIcon, label: "编导展示" },
  { id: "multiturn", icon: chatIcon, label: "多轮对话" },
  { id: "settings", icon: settingIcon, label: "运行设置" },
];

function App() {
  const persistedState = useMemo(() => loadPersistedState(), []);
  const [query, setQuery] = useState(persistedState?.query || defaultQuery);
  const [turns, setTurns] = useState<ChatTurn[]>(persistedState?.turns || []);
  const [loading, setLoading] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>(persistedState?.viewMode || "both");
  const [autoExecute, setAutoExecute] = useState(persistedState?.autoExecute ?? true);
  const [ohRealRun, setOhRealRun] = useState(persistedState?.ohRealRun ?? true);
  const [sidebarExpanded, setSidebarExpanded] = useState(persistedState?.sidebarExpanded ?? false);
  const [currentPage, setCurrentPage] = useState<AppPage>(persistedState?.currentPage || "workspace");
  const [runtimeSettings, setRuntimeSettings] = useState<RuntimeSettings>(persistedState?.runtimeSettings || defaultRuntimeSettings);
  const [isTopNavLayout, setIsTopNavLayout] = useState(() => window.matchMedia("(max-width: 1100px)").matches);
  const navRef = useRef<HTMLElement>(null);
  const navItemRefs = useRef<Partial<Record<AppPage, HTMLButtonElement>>>({});
  const [navSlider, setNavSlider] = useState({ top: 0, left: 0, width: 0, height: 0, visible: false });
  const latestTurn = turns[turns.length - 1];
  const visibleStrategies = useMemo(() => getVisibleStrategies(viewMode), [viewMode]);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(max-width: 1100px)");
    const updateLayoutMode = () => setIsTopNavLayout(mediaQuery.matches);
    updateLayoutMode();
    mediaQuery.addEventListener("change", updateLayoutMode);
    return () => mediaQuery.removeEventListener("change", updateLayoutMode);
  }, []);

  useLayoutEffect(() => {
    const updateNavSlider = () => {
      const nav = navRef.current;
      const activeItem = navItemRefs.current[currentPage];
      if (!nav || !activeItem) return;
      setNavSlider({
        top: activeItem.offsetTop,
        left: activeItem.offsetLeft,
        width: activeItem.offsetWidth,
        height: activeItem.offsetHeight,
        visible: true,
      });
    };
    updateNavSlider();
    const observer = new ResizeObserver(updateNavSlider);
    if (navRef.current) observer.observe(navRef.current);
    window.addEventListener("resize", updateNavSlider);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", updateNavSlider);
    };
  }, [currentPage, sidebarExpanded, isTopNavLayout]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const payload: PersistedAppState = {
      query,
      turns,
      viewMode,
      autoExecute,
      ohRealRun,
      sidebarExpanded,
      currentPage,
      runtimeSettings,
    };
    window.localStorage.setItem(storageKey, JSON.stringify(payload));
  }, [query, turns, viewMode, autoExecute, ohRealRun, sidebarExpanded, currentPage, runtimeSettings]);

  useEffect(() => {
    const hasRunningTurn = turns.some((turn) => turn.status === "running");
    if (!hasRunningTurn) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "当前仍有执行中的任务，刷新页面会中断前端跟踪状态，是否继续？";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [turns]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed || loading) return;
    const id = `${Date.now()}`;
    const createdAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const nextTurn: ChatTurn = { id, query: trimmed, createdAt, status: "running", progressLabel: "正在生成剧本" };
    setTurns((prev) => [...prev, nextTurn]);
    setLoading(true);
    try {
      const response = await fetch("/api/writer-harness", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: trimmed, autoExecute, ohRealRun, executeOutputFormat: "stream-json", runtimeSettings }),
      });
      const data = await response.json() as WriterHarnessResponse;
      if (!response.ok) throw new Error(data.error || "writer harness request failed");
      setTurns((prev) => prev.map((turn) => turn.id === id ? { ...turn, status: data.ok === false ? "failed" : "done", response: data, error: data.error, progressLabel: inferProgressLabel(data.writer_harness) } : turn));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setTurns((prev) => prev.map((turn) => turn.id === id ? { ...turn, status: "failed", error: message } : turn));
    } finally {
      setLoading(false);
    }
  }

  async function handleStop() {
    await fetch("/api/writer-harness-stop", { method: "POST" });
    setLoading(false);
    setTurns((prev) => prev.map((turn) => turn.status === "running" ? { ...turn, status: "failed", error: "用户已终止当前任务" } : turn));
  }

  function handleClearHistory() {
    if (loading) return;
    setTurns([]);
  }

  function handleQueryKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.altKey)) {
      event.preventDefault();
      const target = event.currentTarget;
      const start = target.selectionStart;
      const end = target.selectionEnd;
      const nextValue = `${query.slice(0, start)}\n${query.slice(end)}`;
      setQuery(nextValue);
      requestAnimationFrame(() => {
        target.selectionStart = start + 1;
        target.selectionEnd = start + 1;
      });
      return;
    }
    if (event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault();
      void handleSubmit(event);
    }
  }

  return (
    <main className={`app-shell app-shell--with-sidebar ${sidebarExpanded ? "app-shell--sidebar-expanded" : "app-shell--sidebar-collapsed"}`}>
      <aside ref={navRef} className={`side-nav side-nav--${currentPage} ${sidebarExpanded ? "side-nav--expanded" : "side-nav--collapsed"}`} aria-label="页面导航">
        {!isTopNavLayout ? <button type="button" className="side-nav__toggle" aria-label={sidebarExpanded ? "收起导航" : "展开导航"} aria-pressed={sidebarExpanded} onClick={() => setSidebarExpanded((value) => !value)}>
          <img className="side-nav__toggle-icon" src={sidebarExpanded ? hideNavigationIcon : navigationIcon} alt="" aria-hidden="true" />
        </button> : null}
        <div className="side-nav__slider" aria-hidden="true" style={{ transform: `translate(${navSlider.left}px, ${navSlider.top}px)`, width: navSlider.width, height: navSlider.height, opacity: navSlider.visible ? 1 : 0 }} />
        {navigationItems.map((item) => (
          <button ref={(element) => { navItemRefs.current[item.id] = element || undefined; }} key={item.id} type="button" className={`side-nav__item side-nav__item--${item.id} ${currentPage === item.id ? "side-nav__item--active" : ""}`} aria-current={currentPage === item.id ? "page" : undefined} onClick={() => setCurrentPage(item.id)}>
            <img className="side-nav__icon" src={item.icon} alt="" aria-hidden="true" />
            <span className={`side-nav__label ${isTopNavLayout || sidebarExpanded ? "side-nav__label--visible" : "side-nav__label--hidden"}`}>{item.label}</span>
          </button>
        ))}
      </aside>
      <div className="app-content">
        <div className={currentPage === "workspace" ? "page-section" : "page-section page-section--hidden"} aria-hidden={currentPage !== "workspace"}>
            <section className="hero-card">
              <div>
                <span className="eyebrow">Writer Harness Online UI</span>
                <h1>编导 Harness 展示</h1>
                <p>输入问题后，并行调用原 OpenHarness与编导 Harness，对比回复内容。</p>
                {turns.some((turn) => turn.error?.includes("页面刷新后")) ? <span className="hero-inline-tip">已恢复上次执行记录；若刷新前有运行中任务，请重新发起或核验后台结果。</span> : null}
              </div>
            </section>
            <section className="composer-card chat-shell">
              <div className="chat-header">
                <div>
                  <h2>对话框</h2>
                </div>
                <div className="chat-header__controls">
                  <div className="view-switcher" data-mode={viewMode}>
                    <button className={viewMode === "both" ? "active" : ""} onClick={() => setViewMode("both")}>对比</button>
                    <button className={viewMode === "vanilla" ? "active" : ""} onClick={() => setViewMode("vanilla")}>原OpenHarness</button>
                    <button className={viewMode === "writer_harness" ? "active" : ""} onClick={() => setViewMode("writer_harness")}>编导Harness</button>
                  </div>
                  <button type="button" className="history-clear-button" onClick={handleClearHistory} disabled={loading || turns.length === 0}>清除历史</button>
                </div>
              </div>
              <ChatLog turns={turns} activeTurn={latestTurn} viewMode={viewMode} />
              <form className="composer-row" onSubmit={handleSubmit}>
                <div className="search-icon">⌕</div>
                <div className="composer-row__input-shell">
                  <textarea value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={handleQueryKeyDown} placeholder="请输入要交给 harness 的问题；Enter 发送，Shift / Ctrl / Alt + Enter 换行" disabled={loading} rows={3} />
                  <div className="composer-row__actions composer-row__actions--floating">
                    <button type="submit" className="composer-action-button" disabled={loading || !query.trim()} aria-label={loading ? "生成中" : "发送"}>
                      <span className="composer-action-button__icon">➤</span>
                      <span className="composer-action-button__text">{loading ? "生成中" : "发送"}</span>
                    </button>
                    <button type="button" className="composer-action-button composer-action-button--stop" disabled={!loading} onClick={handleStop} aria-label="停止">
                      <span className="composer-action-button__icon">■</span>
                      <span className="composer-action-button__text">停止</span>
                    </button>
                  </div>
                </div>
              </form>
              <div className="config-row">
                <span>运行配置</span>
                <ConfigSwitch kind="oh" active={ohRealRun} onToggle={() => setOhRealRun((value) => !value)} />
                <ConfigSwitch kind="execute" active={autoExecute} onToggle={() => setAutoExecute((value) => !value)} />
              </div>
            </section>
            <section className={`strategy-grid strategy-grid--${visibleStrategies.length}`}>
              {visibleStrategies.map((strategy) => (
                <StrategyPanel key={strategy} strategy={strategy} result={latestTurn?.response?.[strategy]} loading={loading && latestTurn?.status === "running"} />
              ))}
            </section>
        </div>
        <div className={currentPage === "overview" ? "page-section" : "page-section page-section--hidden"} aria-hidden={currentPage !== "overview"}>
          <HarnessOverviewPage />
        </div>
        <div className={currentPage === "multiturn" ? "page-section" : "page-section page-section--hidden"} aria-hidden={currentPage !== "multiturn"}>
          <MultiTurnPage runtimeSettings={runtimeSettings} />
        </div>
        <div className={currentPage === "settings" ? "page-section" : "page-section page-section--hidden"} aria-hidden={currentPage !== "settings"}>
          <SettingsPage settings={runtimeSettings} onChange={(key, value) => setRuntimeSettings((current) => ({ ...current, [key]: value }))} />
        </div>
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
