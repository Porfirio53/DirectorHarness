"""
Checkpoint-based Operation Evaluation Module

Evaluates agent performance by scoring each checkpoint with a 3-tier system:
- 1.0: Fully completed
- 0.5: Partially completed
- 0.0: Not completed

This module uses DataGenerationPipeline for batch evaluation.
"""

import json
import os
import sys
import re
import argparse
import logging
import hashlib
from typing import Dict, List, Any, Tuple, Optional
from datetime import datetime
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')


sys.path.insert(0, "/data/JohnDoe/MCP-Persona/agentoolkit")
sys.path.insert(0, "/data/JohnDoe/MCP-Persona")

from agentoolkit.data_generation import DataGenerationPipeline


SUCCESS_LEVEL_NUM = 25
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")


def success(self, message, *args, **kws):
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        self._log(SUCCESS_LEVEL_NUM, message, args, **kws)


logging.Logger.success = success
logging.basicConfig(
    level=SUCCESS_LEVEL_NUM, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# Context window limit (128k tokens, approximately 400k characters)
MAX_CONTEXT_CHARS = 400000
# Reserve space for prompts and other metadata
RESERVED_CHARS = 80000
# Max chars available for tool outputs
MAX_TOOL_OUTPUT_CHARS = MAX_CONTEXT_CHARS - RESERVED_CHARS


def compute_instruction_hash(instruction: str) -> str:
    """
    Compute MD5 hash of instruction for use as dictionary key.

    Args:
        instruction: Instruction text to hash

    Returns:
        Hexadecimal MD5 hash string
    """
    if not instruction:
        return ""

    # Encode to bytes and compute MD5 hash
    hash_obj = hashlib.md5(instruction.encode('utf-8'))
    return hash_obj.hexdigest()


class DynamicContextManager:
    """
    Dynamically manage tool outputs to fit within context window.

    Strategy:
    1. Only include agent outputs and tool inputs by default
    2. Extract tool names from checkpoint description
    3. Find matching tool calls and include their outputs with truncation
    4. Ensure total length stays within MAX_TOOL_OUTPUT_CHARS
    """

    def __init__(self, max_chars: int = MAX_TOOL_OUTPUT_CHARS):
        """
        Initialize context manager.

        Args:
            max_chars: Maximum characters allowed for tool outputs
        """
        self.max_chars = max_chars

    def extract_tool_names_from_checkpoint(
        self, checkpoint: str
    ) -> List[str]:
        """
        Extract tool names mentioned in checkpoint description.

        Prioritizes extracting tool names from backtick-wrapped text,
        which is the most reliable way to identify tools.

        Args:
            checkpoint: Checkpoint description text

        Returns:
            List of unique tool names found
        """
        tool_names = set()

        # Extract content from backticks (most accurate)
        # Tool names are typically wrapped in `backticks` in checkpoints
        backtick_pattern = r'`([^`]+)`'
        backtick_matches = re.findall(backtick_pattern, checkpoint)


        for match in backtick_matches:
            # Extract full tool names like "lark_mcp:calendar_v4_calendarEvent_list"
            parts = match.split(':')
            if len(parts) > 1:
                # Likely a full tool name (e.g., "lark_mcp:tool_name" or "baidu-map:search")
                tool_name = parts[0]  # Get the prefix (e.g., "lark_mcp", "baidu-map")
                if tool_name:
                    tool_names.add(tool_name)

                # Also add the full tool name (most precise)
                tool_names.add(match)

        return list(tool_names)

    def truncate_text(self, text: str, max_length: int) -> str:
        """
        Truncate text to max length, preserving structure.

        Args:
            text: Text to truncate
            max_length: Maximum length

        Returns:
            Truncated text with indicator
        """
        if len(text) <= max_length:
            return text

        # Truncate and add indicator
        return text[:max_length] + "... (truncated)"

    def find_matching_tool_calls(
        self,
        tool_calls: List[Dict],
        tool_names: List[str]
    ) -> List[Dict]:
        """
        Find tool calls that match the extracted tool names.

        Args:
            tool_calls: List of tool call records
            tool_names: List of tool names to match

        Returns:
            List of matching tool calls
        """
        if not tool_names:
            return []

        matching_calls = []
        for call in tool_calls:
            tool_name = call.get("tool_name", "")
            # Check if tool name matches any of the extracted names
            if any(name.lower() in tool_name.lower() or tool_name.lower() in name.lower()
                  for name in tool_names):
                matching_calls.append(call)

        return matching_calls

    def calculate_text_length(self, text: str) -> int:
        """
        Calculate approximate token length from character count.

        Args:
            text: Text to measure

        Returns:
            Estimated length in characters
        """
        return len(text)

    def format_agent_outputs_only(
        self, tool_calls: List[Dict], max_calls: int = 50
    ) -> str:
        """
        Format tool calls showing only agent outputs (no tool outputs).

        Args:
            tool_calls: List of tool call records
            max_calls: Maximum number of calls to display

        Returns:
            Formatted string without tool outputs
        """
        if not tool_calls:
            return "No tool calls were made."

        original_count = len(tool_calls)
        if original_count > max_calls:
            tool_calls = tool_calls[:max_calls]
            truncated_note = f"\n... ({original_count} total, showing first {max_calls})"
        else:
            truncated_note = ""

        descriptions = []
        for i, call in enumerate(tool_calls, 1):
            tool_name = call.get("tool_name", "unknown")
            tool_input = call.get("input", {})

            description = f"Tool Call {i}: {tool_name}\n"
            description += f"Input: {json.dumps(tool_input, ensure_ascii=False)}\n"
            description += f"Output: <tool_output_not_shown>\n"
            descriptions.append(description)

        return "\n".join(descriptions) + truncated_note

    def format_with_selected_tool_outputs(
        self,
        tool_calls: List[Dict],
        checkpoint: str,
        chars_per_tool: int = 3000
    ) -> Tuple[str, int]:
        """
        Format tool calls with selective output loading based on checkpoint.

        Args:
            tool_calls: List of tool call records
            checkpoint: Checkpoint description
            chars_per_tool: Max chars per tool output

        Returns:
            Tuple of (formatted string, actual_length)
        """
        if not tool_calls:
            return "No tool calls were made.", 0

        # Extract tool names from checkpoint
        tool_names = self.extract_tool_names_from_checkpoint(checkpoint)

        # If no tools found, show all without outputs
        if not tool_names:
            formatted = self.format_agent_outputs_only(tool_calls)
            return formatted, self.calculate_text_length(formatted)

        # Find matching tool calls
        matching_calls = self.find_matching_tool_calls(tool_calls, tool_names)

        # Calculate remaining budget after agent outputs
        all_calls_without_outputs = self.format_agent_outputs_only(tool_calls)
        base_length = self.calculate_text_length(all_calls_without_outputs)
        remaining_budget = self.max_chars - base_length

        if remaining_budget <= 0:
            return all_calls_without_outputs, base_length

        # Format with outputs for matching tools only
        formatted_parts = []
        current_length = 0
        max_calls = 50

        for i, call in enumerate(tool_calls[:max_calls], 1):
            tool_name = call.get("tool_name", "unknown")
            tool_input = call.get("input", {})

            description = f"Tool Call {i}: {tool_name}\n"
            description += f"Input: {json.dumps(tool_input, ensure_ascii=False)}\n"

            # Include output only if this tool matches checkpoint
            if call in matching_calls:
                tool_output = call.get("output", "")

                # Calculate fair budget per matching tool
                if matching_calls:
                    budget_per_tool = remaining_budget // len(matching_calls)
                else:
                    budget_per_tool = chars_per_tool

                truncated_output = self.truncate_text(
                    str(tool_output),
                    min(budget_per_tool, chars_per_tool)
                )
                description += f"Output: {truncated_output}\n"
            else:
                description += f"Output: <output_not_relevant_to_checkpoint>\n"

            formatted_parts.append(description)
            current_length += self.calculate_text_length(description)

        formatted = "\n".join(formatted_parts)

        if len(tool_calls) > max_calls:
            formatted += f"\n... ({len(tool_calls)} total, showing first {max_calls})"

        return formatted, min(current_length, self.max_chars)

    def format_tool_calls(
        self, tool_calls: List[Dict], checkpoint: str
    ) -> str:
        """
        Main method to format tool calls with dynamic output loading.

        Args:
            tool_calls: List of tool call records
            checkpoint: Checkpoint description

        Returns:
            Formatted string with selective output loading
        """
        formatted, length = self.format_with_selected_tool_outputs(
            tool_calls, checkpoint
        )

        logger.info(
            f"Formatted tool outputs: {length} chars "
            f"({length/self.max_chars*100:.1f}% of budget)"
        )

        return formatted


class EvaluationDataLoader:
    """
    Load and prepare evaluation data from two sources:
    1. data_final_gt.json: Original task data with gt_annotations
    2. results.jsonl: Agent execution results with tool_calls
    """

    def __init__(
        self,
        data_path: str,
        results_path: str,
        eval_output_path: str,
        use_dynamic_context: bool = True,
    ):
        """
        Initialize data loader.

        Args:
            data_path: Path to data_final_gt.json (original tasks with gt_annotations)
            results_path: Path to results.jsonl (agent outputs with tool_calls)
            eval_output_path: Path to existing results_eval.jsonl (for resume)
            use_dynamic_context: Whether to use dynamic context loading (default True)
        """
        self.data_path = data_path
        self.results_path = results_path
        self.eval_output_path = eval_output_path
        self.existing_evals: Dict[str, Dict] = {}
        self.tasks_data: Dict[str, Dict] = {}
        self.agent_results: Dict[str, Dict] = {}
        self.use_dynamic_context = use_dynamic_context

        # Initialize context manager if dynamic loading is enabled
        if self.use_dynamic_context:
            self.context_manager = DynamicContextManager()
        else:
            self.context_manager = None

    def load_existing_evals(self) -> None:
        """Load existing evaluations for resume functionality."""
        if os.path.exists(self.eval_output_path):
            logger.success(f"Found existing evaluation file: {self.eval_output_path}")
            logger.success("Loading existing evaluations for resume...")
            with open(self.eval_output_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        # Extract instruction from query to compute hash
                        query = entry.get("query", {})
                        if isinstance(query, dict):
                            instruction = query.get("instruction", "")
                        else:
                            instruction = ""

                        # Use instruction hash as key for consistency
                        instruction_hash = compute_instruction_hash(instruction)
                        if instruction_hash and "eval" in entry:
                            self.existing_evals[instruction_hash] = entry["eval"]
                    except Exception as e:
                        logger.warning(f"Failed to parse existing eval line: {e}")
            logger.success(f"Loaded {len(self.existing_evals)} existing evaluations")

    def load_tasks_data(self) -> None:
        """
        Load original task data from data_final_gt.json.

        Uses instruction hash as the key for deduplication and matching.
        """
        logger.success(f"Loading tasks data from: {self.data_path}")
        with open(self.data_path, "r", encoding="utf-8") as f:
            all_tasks = json.load(f)

        for task in all_tasks:
            # Use instruction hash as key for matching with agent results
            instruction = task.get("instruction", "")
            instruction_hash = compute_instruction_hash(instruction)

            if instruction_hash:
                # Store both hash and task_id for reference
                task_id = task.get("id")
                task["_instruction_hash"] = instruction_hash
                task["_task_id"] = task_id

                self.tasks_data[instruction_hash] = task

        logger.success(f"Loaded {len(self.tasks_data)} tasks")

    def load_agent_results(self) -> None:
        """
        Load agent execution results from results.jsonl.

        Uses instruction hash as the key to match with tasks_data.
        """
        logger.success(f"Loading agent results from: {self.results_path}")
        with open(self.results_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                try:
                    result = json.loads(line)

                    # Extract instruction from query
                    query = result.get("query", {})
                    if isinstance(query, dict):
                        instruction = query.get("instruction", "")
                    else:
                        instruction = ""

                    # Use instruction hash as key for matching
                    instruction_hash = compute_instruction_hash(instruction)

                    if instruction_hash:
                        # Store both hash and task_id for reference
                        task_id = result.get("task_id")
                        result["_instruction_hash"] = instruction_hash
                        result["_task_id"] = task_id

                        self.agent_results[instruction_hash] = result

                except json.JSONDecodeError as e:
                    logger.error(f"Line {line_num}: Failed to parse JSON: {e}")
                    continue

        logger.success(f"Loaded {len(self.agent_results)} agent results")

    def format_tool_calls(self, tool_calls: List[Dict]) -> str:
        """
        Format tool calls for LLM analysis.

        Limits output size to prevent token overflow:
        - Max 2000 chars per tool output
        - Max 20 tool calls displayed
        - Total max ~50000 chars

        Args:
            tool_calls: List of tool call records

        Returns:
            Formatted string representation
        """
        if not tool_calls:
            return "No tool calls were made."

        # Limit number of tool calls to display
        max_calls = 20
        original_count = len(tool_calls)
        if original_count > max_calls:
            tool_calls = tool_calls[:max_calls]
            truncated_note = f"\n... ({original_count} total, showing first {max_calls})"
        else:
            truncated_note = ""

        # Per-tool output limit
        max_output_chars = 2000

        descriptions = []
        for i, call in enumerate(tool_calls, 1):
            tool_name = call.get("tool_name", "unknown")
            tool_input = call.get("input", {})
            tool_output = call.get("output", "")

            # Truncate output if too long
            if isinstance(tool_output, str) and len(tool_output) > max_output_chars:
                tool_output = tool_output[:max_output_chars] + "... (truncated)"

            description = f"Tool Call {i}: {tool_name}\n"
            description += f"Input: {json.dumps(tool_input, ensure_ascii=False)}\n"
            description += f"Output: {tool_output}\n"
            descriptions.append(description)

        return "\n".join(descriptions) + truncated_note

    def prepare_evaluation_data(self) -> List[Dict[str, Any]]:
        """
        Prepare data pool for evaluation pipeline.

        Returns:
            List of data pool entries
        """
        data_pool = []
        skipped_count = 0

        for instruction_hash, task_data in self.tasks_data.items():
            # Skip if already evaluated
            if instruction_hash in self.existing_evals:
                skipped_count += 1
                continue

            # Get agent result for this task (using instruction hash)
            agent_result = self.agent_results.get(instruction_hash)
            if not agent_result:
                original_task_id = task_data.get("_task_id", "unknown")
                logger.warning(
                    f"Task {original_task_id} (hash: {instruction_hash[:8]}...): "
                    f"No agent result found, skipping"
                )
                continue

            # Extract task information
            instruction = task_data.get("instruction", "")
            original_task_id = task_data.get("_task_id", "unknown")
            gt_annotation = task_data.get("gt_annotation", {})
            tool_calling_sequence = gt_annotation.get("tool_calling_sequence", "")

            # Truncate tool_calling_sequence if too long (max 3000 chars)
            max_seq_chars = 3000
            if len(tool_calling_sequence) > max_seq_chars:
                tool_calling_sequence = tool_calling_sequence[:max_seq_chars] + "... (truncated)"

            # Get checkpoints from summary
            summary = gt_annotation.get("summary", [])
            if isinstance(summary, str):
                checkpoints = [summary]
            else:
                checkpoints = summary

            if not checkpoints:
                logger.warning(
                    f"Task {original_task_id}: No checkpoints found, skipping"
                )
                continue

            # Get tool calls from agent result
            tool_calls = agent_result.get("tool_calls", [])

            # Log task info
            logger.success(
                f"Task {original_task_id}: {len(checkpoints)} checkpoints, "
                f"{len(tool_calls)} tool calls"
            )

            # Create separate data pool entry for each checkpoint
            for checkpoint_idx, checkpoint in enumerate(checkpoints):
                # Format tool calls with dynamic context loading
                if self.use_dynamic_context and self.context_manager:
                    tool_calls_str = self.context_manager.format_tool_calls(
                        tool_calls, checkpoint
                    )
                else:
                    tool_calls_str = self.format_tool_calls(tool_calls)

                data_pool_entry = {
                    "_id": f"{instruction_hash}_{checkpoint_idx}",
                    "_instruction_hash": instruction_hash,
                    "_task_id": original_task_id,
                    "_checkpoint_idx": checkpoint_idx,
                    "_checkpoint": checkpoint,
                    "_instruction": instruction,
                    "_tool_calls": tool_calls_str,
                    "_tool_calling_sequence": tool_calling_sequence,
                    "_checkpoints_count": len(checkpoints),
                    "_original_task_data": task_data,
                    "_agent_result": agent_result,
                    "system_prompt_kwargs": {},
                    "user_prompt_kwargs": {
                        "instruction": instruction,
                        "tool_calls": tool_calls_str,
                        "checkpoint": checkpoint,
                        "tool_calling_sequence": tool_calling_sequence,
                    },
                }
                data_pool.append(data_pool_entry)

        logger.success(f"Prepared {len(data_pool)} evaluation entries")
        if skipped_count > 0:
            logger.success(f"Skipped {skipped_count} already evaluated tasks")

        return data_pool

    def load(self) -> Tuple[List[Dict[str, Any]], Dict[str, Dict]]:
        """
        Load all data and prepare for evaluation.

        Returns:
            Tuple of (data_pool entries, existing_evals)
        """
        self.load_existing_evals()
        self.load_tasks_data()
        self.load_agent_results()
        data_pool = self.prepare_evaluation_data()
        return data_pool, self.existing_evals


def extract_tag_content(tag: str, text: str, strip_whitespace: bool = True) -> List[str]:
    """
    Extract content between XML-like tags.

    Args:
        tag: Tag name without angle brackets
        text: Text to extract from
        strip_whitespace: Whether to strip leading/trailing whitespace

    Returns:
        List of extracted content, empty list if no matches
    """
    pattern = rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>"
    matches = re.findall(pattern, text, re.DOTALL)

    if strip_whitespace:
        return [match.strip() for match in matches]
    return matches


def extract_eval_result(text: str) -> Dict[str, Any]:
    """
    Extract evaluation score from LLM output.

    Args:
        text: LLM output text

    Returns:
        Dictionary with extracted score
    """
    scores = extract_tag_content("score", text)

    if not scores:
        logger.warning("Failed to extract score from LLM output")
        return {"score": 0}

    score_str = scores[0].strip()
    try:
        score = float(score_str)
        # Normalize score to 1.0, 0.5, or 0.0
        if score >= 1:
            score = 1.0
        elif score >= 0.5:
            score = 0.5
        else:
            score = 0.0
        return {"score": score}
    except ValueError:
        logger.warning(f"Invalid score value: {score_str}")
        return {"score": 0}


def reconstruct_task_results(
    eval_results: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Reconstruct per-task results from individual checkpoint evaluations.

    Args:
        eval_results: Evaluation results from pipeline

    Returns:
        Dict mapping instruction_hash to evaluation result
    """
    # Group results by instruction_hash
    task_results: Dict[str, Dict[str, Any]] = {}

    for entry_with_result in eval_results:
        input_data = entry_with_result.get("input", {})
        instruction_hash = input_data.get("_instruction_hash")
        task_id = input_data.get("_task_id")
        checkpoint = input_data.get("_checkpoint")
        checkpoints_count = input_data.get("_checkpoints_count")
        raw_response = entry_with_result.get("raw_response", "")

        if not instruction_hash:
            continue

        # Initialize task result if not exists
        if instruction_hash not in task_results:
            task_results[instruction_hash] = {
                "instruction_hash": instruction_hash,
                "task_id": task_id,
                "checkpoints_count": checkpoints_count,
                "checkpoint_scores": [],
                "evaluation_start_time": datetime.now().isoformat(),
            }

        # Extract score from result
        extracted = entry_with_result.get("extracted", {})
        score = extracted.get("score", 0.0)

        score_result = {
            "checkpoint": checkpoint,
            "score": score,
            "raw_response": raw_response,
        }

        task_results[instruction_hash]["checkpoint_scores"].append(score_result)

    # Calculate final scores for each task
    for instruction_hash, task_result in task_results.items():
        checkpoint_scores = task_result["checkpoint_scores"]
        total_score = sum(cs["score"] for cs in checkpoint_scores)
        final_score = (
            total_score / len(checkpoint_scores) if checkpoint_scores else 0.0
        )

        task_result["final_score"] = final_score
        task_result["evaluation_end_time"] = datetime.now().isoformat()

    return task_results


def save_evaluation_results(
    data_path: str,
    results_path: str,
    eval_output_path: str,
    task_evals: Dict[str, Dict[str, Any]],
    existing_evals: Dict[str, Dict[str, Any]],
):
    """
    Save evaluation results in results_eval.jsonl format.

    Format: same as results.jsonl but with additional 'eval' and 'score' fields.

    Args:
        data_path: Path to data_final_gt.json
        results_path: Path to results.jsonl
        eval_output_path: Path to output results_eval.jsonl
        task_evals: New task evaluations (keyed by instruction_hash)
        existing_evals: Existing evaluations (keyed by instruction_hash)
    """
    # Load all agent results with instruction_hash as key
    agent_results = {}
    with open(results_path, "r", encoding="utf-8") as f:
        for line in f:
            result = json.loads(line)
            query = result.get("query", {})
            if isinstance(query, dict):
                instruction = query.get("instruction", "")
            else:
                instruction = ""

            instruction_hash = compute_instruction_hash(instruction)
            if instruction_hash:
                agent_results[instruction_hash] = result

    # Load all task data
    with open(data_path, "r", encoding="utf-8") as f:
        all_tasks = json.load(f)

    # Merge with existing and new evaluations (both keyed by instruction_hash)
    all_evals = {**existing_evals, **task_evals}

    # Write results_eval.jsonl
    with open(eval_output_path, "w", encoding="utf-8") as f:
        for task in all_tasks:
            instruction = task.get("instruction", "")
            instruction_hash = compute_instruction_hash(instruction)

            if not instruction_hash:
                continue

            # Write entry if evaluation exists (using instruction_hash)
            if instruction_hash in all_evals:
                # Get agent result if exists
                if instruction_hash in agent_results:
                    entry = agent_results[instruction_hash].copy()
                else:
                    # Create minimal entry if no agent result
                    entry = {
                        "task_id": task.get("id"),
                        "query": {"instruction": instruction}
                    }

                # Add eval and score fields using instruction hash
                entry["eval"] = all_evals[instruction_hash]
                entry["score"] = all_evals[instruction_hash].get("final_score", 0.0)

                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.success(f"Saved {len(all_evals)} evaluated tasks to {eval_output_path}")


def generate_score_distribution_chart(
    eval_output_path: str,
    output_pdf_path: str,
):
    """
    Generate score distribution chart and save as PDF.

    Args:
        eval_output_path: Path to results_eval.jsonl
        output_pdf_path: Path to output PDF
    """
    scores = []
    with open(eval_output_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            score = entry.get("score")
            if score is not None:
                scores.append(score)

    if not scores:
        logger.warning("No scores found for distribution chart")
        return

    # Create figure with two subplots
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    # Score distribution histogram
    ax1.hist(scores, bins=20, edgecolor='black', alpha=0.7, color='steelblue')
    ax1.set_xlabel('Score', fontsize=12)
    ax1.set_ylabel('Frequency', fontsize=12)
    ax1.set_title('Score Distribution', fontsize=14, fontweight='bold')
    ax1.grid(True, alpha=0.3)

    # Score range bar chart
    ranges = {
        '0.0': sum(1 for s in scores if s == 0.0),
        '0.1-0.4': sum(1 for s in scores if 0.1 <= s < 0.4),
        '0.5': sum(1 for s in scores if s == 0.5),
        '0.6-0.9': sum(1 for s in scores if 0.6 <= s < 1.0),
        '1.0': sum(1 for s in scores if s == 1.0),
    }
    ax2.bar(ranges.keys(), ranges.values(), color='coral', edgecolor='black', alpha=0.7)
    ax2.set_xlabel('Score Range', fontsize=12)
    ax2.set_ylabel('Count', fontsize=12)
    ax2.set_title('Score Range Distribution', fontsize=14, fontweight='bold')
    ax2.grid(True, alpha=0.3, axis='y')

    plt.tight_layout()
    plt.savefig(output_pdf_path, format='pdf', dpi=300)
    plt.close()

    logger.success(f"Score distribution chart saved to {output_pdf_path}")


def print_detailed_results(eval_output_path: str):
    """
    Print detailed evaluation results to console.

    Args:
        eval_output_path: Path to results_eval.jsonl
    """
    logger.success("=" * 80)
    logger.success("DETAILED EVALUATION RESULTS")
    logger.success("=" * 80)

    scores = []
    task_details = []

    with open(eval_output_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            task_id = entry.get("task_id")
            score = entry.get("score")
            eval_data = entry.get("eval", {})

            if score is not None:
                scores.append(score)
                checkpoint_scores = eval_data.get("checkpoint_scores", [])
                task_details.append({
                    "task_id": task_id,
                    "score": score,
                    "checkpoints": checkpoint_scores,
                })

    if not scores:
        logger.warning("No evaluation results found")
        return

    # Overall statistics
    logger.success(f"\nOverall Statistics:")
    logger.success(f"  Total Tasks: {len(scores)}")
    logger.success(f"  Average Score: {sum(scores) / len(scores):.3f}")

    # Score distribution
    score_ranges = {
        "Excellent (0.8-1.0]": len([s for s in scores if 0.8 < s <= 1.0]),
        "Good (0.6-0.8]": len([s for s in scores if 0.6 < s <= 0.8]),
        "Fair (0.4-0.6]": len([s for s in scores if 0.4 < s <= 0.6]),
        "Poor (0.2-0.4]": len([s for s in scores if 0.2 < s <= 0.4]),
        "Very Poor [0.0-0.2]": len([s for s in scores if 0.0 <= s <= 0.2]),
    }

    logger.success(f"\nScore Distribution:")
    for range_name, count in score_ranges.items():
        percentage = (count / len(scores)) * 100
        logger.success(f"  {range_name}: {count} tasks ({percentage:.1f}%)")

    # Checkpoint statistics
    all_checkpoint_scores = []
    for detail in task_details:
        for cp in detail["checkpoints"]:
            all_checkpoint_scores.append(cp.get("score", 0))

    total_checkpoints = len(all_checkpoint_scores)
    full_completed = sum(1 for s in all_checkpoint_scores if s == 1.0)
    partial_completed = sum(1 for s in all_checkpoint_scores if s == 0.5)
    not_completed = sum(1 for s in all_checkpoint_scores if s == 0.0)

    logger.success(f"\nCheckpoint Statistics:")
    logger.success(f"  Total Checkpoints: {total_checkpoints}")
    logger.success(
        f"  Fully Completed (1.0): {full_completed} "
        f"({full_completed/total_checkpoints*100:.1f}%)"
    )
    logger.success(
        f"  Partially Completed (0.5): {partial_completed} "
        f"({partial_completed/total_checkpoints*100:.1f}%)"
    )
    logger.success(
        f"  Not Completed (0.0): {not_completed} "
        f"({not_completed/total_checkpoints*100:.1f}%)"
    )
    logger.success(
        f"  Completion Rate: {full_completed/total_checkpoints:.2%}"
    )

    # Per-task details
    logger.success(f"\nPer-Task Details:")
    for detail in sorted(task_details, key=lambda x: x["score"], reverse=True):
        task_id = detail["task_id"]
        score = detail["score"]
        checkpoints = detail["checkpoints"]
        logger.success(f"  Task {task_id}: Score = {score:.3f} ({len(checkpoints)} checkpoints)")

    logger.success("=" * 80)


def main(config: Dict[str, Any]):
    """
    Main evaluation pipeline using DataGenerationPipeline.

    Args:
        config: Configuration dictionary
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    logger.success("=" * 20 + " CONFIG " + "=" * 20)
    for k, v in config.items():
        if k == "api_key":
            v = v[:10] + "..." if v else ""
        logger.success(f"{k}: {v}")
    logger.success("=" * 40)

    experiment_name = config["experiment_name"]
    model_name = config["model_name"]
    data_path = config.get("data_path")

    if not data_path:
        logger.error("--data_path is required")
        return

    # Paths
    results_dir = f"/data/JohnDoe/MCP-Persona/result/{experiment_name}/{model_name}"
    results_path = os.path.join(results_dir, "results.jsonl")
    eval_output_path = os.path.join(results_dir, "results_eval.jsonl")
    pdf_output_path = os.path.join(results_dir, f"score_distribution.pdf")

    logger.success(f"\nData source: {data_path}")
    logger.success(f"Agent results: {results_path}")
    logger.success(f"Eval output: {eval_output_path}")

    # Check if paths exist
    if not os.path.exists(data_path):
        logger.error(f"Data file not found: {data_path}")
        return

    if not os.path.exists(results_path):
        logger.error(f"Results file not found: {results_path}")
        return

    # Load data using EvaluationDataLoader
    loader = EvaluationDataLoader(
        data_path=data_path,
        results_path=results_path,
        eval_output_path=eval_output_path,
    )

    data_pool, existing_evals = loader.load()

    if not data_pool:
        logger.success("No new tasks to evaluate (all tasks already evaluated)")
        # Still generate charts and print results for existing data
        if os.path.exists(eval_output_path):
            generate_score_distribution_chart(eval_output_path, pdf_output_path)
            print_detailed_results(eval_output_path)
        return

    # Initialize pipeline
    pipeline = DataGenerationPipeline(
        config_path="/data/JohnDoe/MCP-Persona/v3/config.yaml",
        experiment_name=f"evaluation_{model_name}",
        system_prompt_path="/data/JohnDoe/MCP-Persona/v3/prompt/new/eval_prompt/eval_zh_sys.md",
        user_prompt_path="/data/JohnDoe/MCP-Persona/v3/prompt/new/eval_prompt/eval_zh_user.md",
        need_time_stamp=False,
    )

    try:
        logger.success("Starting evaluation pipeline...")

        # Run evaluation
        eval_results = pipeline.run(
            data_pool=data_pool,
            concurrency_limit=config.get("concurrency_limit", 5),
            extract_function=extract_eval_result,
        )

        logger.success(f"Pipeline completed, evaluated {len(eval_results)} checkpoints")

        # Reconstruct task-level results
        task_evals = reconstruct_task_results(eval_results)

        # Save evaluation results
        save_evaluation_results(
            data_path=data_path,
            results_path=results_path,
            eval_output_path=eval_output_path,
            task_evals=task_evals,
            existing_evals=existing_evals,
        )

        # Generate score distribution chart
        generate_score_distribution_chart(eval_output_path, pdf_output_path)

        # Print detailed results
        print_detailed_results(eval_output_path)

    except Exception as e:
        logger.error(f"Pipeline execution failed: {e}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Checkpoint-based evaluation with 3-tier scoring using DataGenerationPipeline"
    )

    parser.add_argument("--experiment_name", required=True, help="Experiment name")
    parser.add_argument("--model_name", required=True, help="Model name (e.g., gpt-5)")
    parser.add_argument("--api_key", required=True, help="OpenAI API key")
    parser.add_argument("--base_url", required=True, help="API base URL")
    parser.add_argument(
        "--judge_model",
        default="gpt-4o",
        help="Model for LLM judging (default: gpt-4o)",
    )
    parser.add_argument(
        "--concurrency_limit",
        type=int,
        default=5,
        help="Concurrent evaluation requests (default: 5)",
    )
    parser.add_argument(
        "--data_path",
        required=True,
        help="Path to data JSON file with gt_annotations (e.g., data_final_gt.json)",
    )

    args = parser.parse_args()
    main(vars(args))
