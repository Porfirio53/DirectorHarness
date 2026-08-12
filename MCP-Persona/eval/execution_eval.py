"""
Execution-based Evaluation Module

Evaluates agent performance by checking execution correctness against gt checkpoints:
- personalized_search: Compare agent's final_answer with GT_value
- operate: Evaluate tool calling correctness

Uses LLM-as-a-judge with different prompts for each checkpoint type.
"""

import json
import os
import sys
import hashlib
import argparse
import logging
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


def compute_instruction_hash(instruction: str) -> str:
    """Compute MD5 hash of instruction for use as dictionary key."""
    if not instruction:
        return ""
    hash_obj = hashlib.md5(instruction.encode('utf-8'))
    return hash_obj.hexdigest()


class ExecutionDataLoader:
    """
    Load and prepare evaluation data for execution-based evaluation.

    Processes gt field which contains checkpoints with different types:
    - personalized_search: Evaluate final_answer against GT_value
    - operate: Evaluate tool calling correctness
    """

    def __init__(
        self,
        data_path: str,
        results_path: str,
        eval_output_path: str,
    ):
        """
        Initialize data loader.

        Args:
            data_path: Path to data_final_gt.json
            results_path: Path to results.jsonl
            eval_output_path: Path to existing results_exec_eval.jsonl
        """
        self.data_path = data_path
        self.results_path = results_path
        self.eval_output_path = eval_output_path
        self.existing_evals: Dict[str, Dict] = {}
        self.tasks_data: Dict[str, Dict] = {}
        self.agent_results: Dict[str, Dict] = {}

    def load_existing_evals(self) -> None:
        """Load existing evaluations for resume functionality."""
        if not os.path.exists(self.eval_output_path):
            return

        logger.success(f"Found existing evaluation file: {self.eval_output_path}")
        logger.success("Loading existing evaluations for resume...")

        with open(self.eval_output_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    query = entry.get("query", {})
                    if isinstance(query, dict):
                        instruction = query.get("instruction", "")
                    else:
                        instruction = ""

                    instruction_hash = compute_instruction_hash(instruction)
                    if instruction_hash and "exec_eval" in entry:
                        self.existing_evals[instruction_hash] = entry["exec_eval"]
                except Exception as e:
                    logger.warning(f"Failed to parse existing eval line: {e}")

        logger.success(f"Loaded {len(self.existing_evals)} existing evaluations")

    def load_tasks_data(self) -> None:
        """Load original task data from data_final_gt.json."""
        logger.success(f"Loading tasks data from: {self.data_path}")
        with open(self.data_path, "r", encoding="utf-8") as f:
            all_tasks = json.load(f)

        for task in all_tasks:
            instruction = task.get("instruction", "")
            instruction_hash = compute_instruction_hash(instruction)

            if instruction_hash:
                task_id = task.get("id")
                task["_instruction_hash"] = instruction_hash
                task["_task_id"] = task_id
                self.tasks_data[instruction_hash] = task

        logger.success(f"Loaded {len(self.tasks_data)} tasks")

    def load_agent_results(self) -> None:
        """Load agent execution results from results.jsonl."""
        logger.success(f"Loading agent results from: {self.results_path}")
        with open(self.results_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                try:
                    result = json.loads(line)

                    query = result.get("query", {})
                    if isinstance(query, dict):
                        instruction = query.get("instruction", "")
                    else:
                        instruction = ""

                    instruction_hash = compute_instruction_hash(instruction)

                    if instruction_hash:
                        task_id = result.get("task_id")
                        result["_instruction_hash"] = instruction_hash
                        result["_task_id"] = task_id
                        self.agent_results[instruction_hash] = result

                except json.JSONDecodeError as e:
                    logger.error(f"Line {line_num}: Failed to parse JSON: {e}")
                    continue

        logger.success(f"Loaded {len(self.agent_results)} agent results")

    def format_tool_calls_for_operate(self, tool_calls: List[Dict]) -> str:
        """
        Format tool calls for operate evaluation.

        Only includes agent's input parameters, excludes tool outputs to avoid
        exceeding context length limits.

        Args:
            tool_calls: List of tool call records

        Returns:
            Formatted string representation
        """
        if not tool_calls:
            return "No tool calls were made."

        max_calls = 20
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
            descriptions.append(description)

        return "\n".join(descriptions) + truncated_note

    def prepare_evaluation_data(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Prepare data pool for evaluation pipeline.

        Returns:
            Tuple of (data_pool entries, list of checkpoint types)
        """
        data_pool = []
        checkpoint_types = []
        skipped_count = 0

        for instruction_hash, task_data in self.tasks_data.items():
            if instruction_hash in self.existing_evals:
                skipped_count += 1
                continue

            agent_result = self.agent_results.get(instruction_hash)
            if not agent_result:
                original_task_id = task_data.get("_task_id", "unknown")
                logger.warning(
                    f"Task {original_task_id} (hash: {instruction_hash[:8]}...): "
                    f"No agent result found, skipping"
                )
                continue

            instruction = task_data.get("instruction", "")
            original_task_id = task_data.get("_task_id", "unknown")

            gt_checkpoints = task_data.get("gt", [])
            if not gt_checkpoints:
                logger.warning(f"Task {original_task_id}: No gt checkpoints found, skipping")
                continue

            final_answer = agent_result.get("final_answer", "")
            tool_calls = agent_result.get("tool_calls", [])

            logger.success(
                f"Task {original_task_id}: {len(gt_checkpoints)} execution checkpoints, "
                f"{len(tool_calls)} tool calls"
            )

            for checkpoint_idx, checkpoint in enumerate(gt_checkpoints):
                checkpoint_type = checkpoint.get("checkpoint_type", "")
                if not checkpoint_type:
                    logger.warning(f"Task {original_task_id}, checkpoint {checkpoint_idx}: "
                                   f"No checkpoint_type found, skipping")
                    continue

                checkpoint_types.append(checkpoint_type)

                if checkpoint_type == "personalized_search":
                    data_pool_entry = self._prepare_search_entry(
                        instruction_hash, original_task_id, checkpoint_idx,
                        instruction, checkpoint, final_answer, tool_calls, task_data, agent_result
                    )
                    if data_pool_entry:
                        data_pool.append(data_pool_entry)
                elif checkpoint_type == "operate":
                    data_pool_entry = self._prepare_operate_entry(
                        instruction_hash, original_task_id, checkpoint_idx,
                        instruction, checkpoint, tool_calls, task_data, agent_result
                    )
                    if data_pool_entry:
                        data_pool.append(data_pool_entry)
                else:
                    logger.warning(
                        f"Task {original_task_id}, checkpoint {checkpoint_idx}: "
                        f"Unknown checkpoint_type '{checkpoint_type}', skipping"
                    )
                    continue

        logger.success(f"Prepared {len(data_pool)} evaluation entries")
        if skipped_count > 0:
            logger.success(f"Skipped {skipped_count} already evaluated tasks")

        return data_pool, checkpoint_types

    def _prepare_search_entry(
        self,
        instruction_hash: str,
        task_id: str,
        checkpoint_idx: int,
        instruction: str,
        checkpoint: Dict,
        final_answer: str,
        tool_calls: List[Dict],
        task_data: Dict,
        agent_result: Dict,
    ) -> Dict[str, Any]:
        """Prepare data pool entry for personalized_search checkpoint."""
        gt_value = checkpoint.get("GT_value", "")
        tool_calls_str = self.format_tool_calls_for_operate(tool_calls)

        return {
            "_id": f"{instruction_hash}_{checkpoint_idx}",
            "_instruction_hash": instruction_hash,
            "_task_id": task_id,
            "_checkpoint_idx": checkpoint_idx,
            "_checkpoint_type": "personalized_search",
            "_final_answer": final_answer,
            "_GT_value": gt_value,
            "_instruction": instruction,
            "_checkpoints_count": 1,
            "_original_task_data": task_data,
            "_agent_result": agent_result,
            "system_prompt_kwargs": {},
            "user_prompt_kwargs": {
                "instruction": instruction,
                "tool_calls": tool_calls_str,
                "final_answer": final_answer,
                "GT_value": gt_value,
            },
        }

    def _load_sandbox_context(
        self,
        sandbox_path: str,
        server_name: str,
        context_id: str,
    ) -> Dict[str, Any]:
        """
        Load context data from sandbox file.

        Args:
            sandbox_path: Path to sandbox directory
            server_name: Name of the server (e.g., "lark_mcp")
            context_id: ID of the context to load

        Returns:
            Context data dictionary, or empty dict if not found
        """
        if server_name == "lark_mcp":
            context_file_path = os.path.join(sandbox_path, "lark_mcp", "lark_mcp.json")
        elif server_name == "notion":
            context_file_path = os.path.join(sandbox_path, "notion/blocks.jsonl")
        elif server_name == "obsidian":
            context_file_path = os.path.join(sandbox_path, "obsidian/clinic.jsonl")
        elif server_name == "slack":
            context_file_path = os.path.join(sandbox_path, "slack/context.json")
        elif server_name == "universal_email":
            context_file_path = os.path.join(sandbox_path, "universal_email/context.json")
        elif server_name == "xiaohongshu":
            context_file_path = os.path.join(sandbox_path, "xiaohongshu/context.json")
        elif server_name == "wecome":
            context_file_path = os.path.join(sandbox_path, "wecome/context.json")
        else:
            context_file_path = None
            print(f"Error! Invalid server name: {context_file_path}")

        if not os.path.exists(context_file_path):
            logger.warning(
                f"Context file not found: {context_file_path}"
            )
            return {}

        try:
            with open(context_file_path, "r", encoding="utf-8") as f:
                context_data = json.load(f)

            if context_id in context_data:
                return context_data[context_id]
            else:
                logger.warning(
                    f"Context ID '{context_id}' not found in {context_file_path}"
                )
                return {}
        except Exception as e:
            logger.error(f"Failed to load context from {context_file_path}: {e}")
            return {}

    def _prepare_operate_entry(
        self,
        instruction_hash: str,
        task_id: str,
        checkpoint_idx: int,
        instruction: str,
        checkpoint: Dict,
        tool_calls: List[Dict],
        task_data: Dict,
        agent_result: Dict,
    ) -> Optional[Dict[str, Any]]:
        """
        Prepare data pool entry for operate checkpoint.

        Returns None if context loading fails, which will skip this checkpoint.
        """
        expected_tool = checkpoint.get("tool", "")
        operate_type = checkpoint.get("operate_type", "")
        context_list = checkpoint.get("context", [])

        tool_calls_str = self.format_tool_calls_for_operate(tool_calls)

        # Load actual context data from sandbox
        sandbox_path = agent_result.get("sandbox_path", "")
        context_data_list = []
        context_load_errors = []

        if sandbox_path and context_list:
            for context_item in context_list:
                server_name = context_item.get("server_name", "")
                context_id = context_item.get("context_id", "")

                if server_name and context_id:
                    actual_context = self._load_sandbox_context(
                        sandbox_path, server_name, context_id
                    )
                    if actual_context:
                        context_data_list.append({
                            "server_name": server_name,
                            "context_id": context_id,
                            "data": actual_context
                        })
                    else:
                        context_load_errors.append(
                            f"Failed to load context: server={server_name}, id={context_id}"
                        )

        # Skip this checkpoint if context loading failed completely
        if context_list and not context_data_list:
            logger.warning(
                f"Task {task_id}, checkpoint {checkpoint_idx}: "
                f"Context loading failed for all {len(context_list)} contexts, skipping. "
                f"Errors: {'; '.join(context_load_errors)}"
            )
            return None

        context_str = json.dumps(context_data_list, ensure_ascii=False, indent=2)

        return {
            "_id": f"{instruction_hash}_{checkpoint_idx}",
            "_instruction_hash": instruction_hash,
            "_task_id": task_id,
            "_checkpoint_idx": checkpoint_idx,
            "_checkpoint_type": "operate",
            "_tool_calls_str": tool_calls_str,
            "_expected_tool": expected_tool,
            "_operate_type": operate_type,
            "_context": context_str,
            "_instruction": instruction,
            "_checkpoints_count": 1,
            "_original_task_data": task_data,
            "_agent_result": agent_result,
            "system_prompt_kwargs": {},
            "user_prompt_kwargs": {
                "instruction": instruction,
                "tool_calls": tool_calls_str,
                "tool": expected_tool,
                "operate_type": operate_type,
                "context": context_str,
            },
        }

    def load(self) -> Tuple[List[Dict[str, Any]], Dict[str, Dict], List[str]]:
        """
        Load all data and prepare for evaluation.

        Returns:
            Tuple of (data_pool entries, existing_evals, checkpoint_types)
        """
        self.load_existing_evals()
        self.load_tasks_data()
        self.load_agent_results()
        data_pool, checkpoint_types = self.prepare_evaluation_data()
        return data_pool, self.existing_evals, checkpoint_types


def extract_tag_content(tag: str, text: str, strip_whitespace: bool = True) -> List[str]:
    """Extract content between XML-like tags."""
    import re
    pattern = rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>"
    matches = re.findall(pattern, text, re.DOTALL)

    if strip_whitespace:
        return [match.strip() for match in matches]
    return matches


def extract_exec_score(text: str) -> Dict[str, Any]:
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


def reconstruct_exec_results(
    eval_results: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Reconstruct per-task results from individual checkpoint evaluations.

    Args:
        eval_results: Evaluation results from pipeline

    Returns:
        Dict mapping instruction_hash to evaluation result
    """
    task_results: Dict[str, Dict[str, Any]] = {}

    for entry_with_result in eval_results:
        input_data = entry_with_result.get("input", {})
        instruction_hash = input_data.get("_instruction_hash")
        task_id = input_data.get("_task_id")
        checkpoint_type = input_data.get("_checkpoint_type")
        checkpoint_idx = input_data.get("_checkpoint_idx")
        raw_response = entry_with_result.get("raw_response", "")

        if not instruction_hash:
            continue

        if instruction_hash not in task_results:
            task_results[instruction_hash] = {
                "instruction_hash": instruction_hash,
                "task_id": task_id,
                "checkpoint_scores": [],
                "evaluation_start_time": datetime.now().isoformat(),
            }

        extracted = entry_with_result.get("extracted", {})
        score = extracted.get("score", 0.0)

        score_result = {
            "checkpoint_type": checkpoint_type,
            "checkpoint_idx": checkpoint_idx,
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


def save_exec_evaluation_results(
    data_path: str,
    results_path: str,
    eval_output_path: str,
    task_evals: Dict[str, Dict[str, Any]],
    existing_evals: Dict[str, Dict[str, Any]],
):
    """
    Save evaluation results in results_exec_eval.jsonl format.

    Args:
        data_path: Path to data_final_gt.json
        results_path: Path to results.jsonl
        eval_output_path: Path to output results_exec_eval.jsonl
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

    # Merge with existing and new evaluations
    all_evals = {**existing_evals, **task_evals}

    # Write results_exec_eval.jsonl
    with open(eval_output_path, "w", encoding="utf-8") as f:
        for task in all_tasks:
            instruction = task.get("instruction", "")
            instruction_hash = compute_instruction_hash(instruction)

            if not instruction_hash:
                continue

            if instruction_hash in all_evals:
                if instruction_hash in agent_results:
                    entry = agent_results[instruction_hash].copy()
                else:
                    entry = {
                        "task_id": task.get("id"),
                        "query": {"instruction": instruction}
                    }

                entry["exec_eval"] = all_evals[instruction_hash]
                entry["exec_score"] = all_evals[instruction_hash].get("final_score", 0.0)

                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    logger.success(f"Saved {len(all_evals)} evaluated tasks to {eval_output_path}")


def generate_score_distribution_chart(
    eval_output_path: str,
    output_pdf_path: str,
    checkpoint_types: List[str],
):
    """
    Generate score distribution chart and save as PDF.

    Args:
        eval_output_path: Path to results_exec_eval.jsonl
        output_pdf_path: Path to output PDF
        checkpoint_types: List of checkpoint types
    """
    scores = []
    with open(eval_output_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            score = entry.get("exec_score")
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
    ax1.set_title('Execution Score Distribution', fontsize=14, fontweight='bold')
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


def load_task_type_mapping(dataset_path: str) -> Dict[str, str]:
    """
    Load task type mapping from dataset file.

    Classifies tasks as 'single_server' or 'multi_server' based on query_type field.
    - If query_type contains "single" → single_server
    - Otherwise → multi_server

    Uses instruction hash as key to match with evaluation results.

    Args:
        dataset_path: Path to dataset JSON file

    Returns:
        Dict mapping instruction_hash to task_type ('single_server' or 'multi_server')
    """
    if not os.path.exists(dataset_path):
        logger.warning(f"Dataset file not found: {dataset_path}")
        return {}

    from hashlib import md5

    task_type_map = {}

    try:
        with open(dataset_path, "r", encoding="utf-8") as f:
            dataset = json.load(f)

        for item in dataset:
            instruction = item.get("instruction", "")
            if not instruction:
                continue

            # Compute instruction hash as key (same as used in results)
            instruction_hash = md5(instruction.encode('utf-8')).hexdigest()

            query_type = item.get("query_type", "").lower()

            # Classify based on query_type field
            if "single" in query_type:
                task_type_map[instruction_hash] = "single_server"
            else:
                task_type_map[instruction_hash] = "multi_server"

        logger.success(
            f"Loaded task types: {len(task_type_map)} tasks "
            f"({sum(1 for t in task_type_map.values() if t == 'single_server')} single, "
            f"{sum(1 for t in task_type_map.values() if t == 'multi_server')} multi)"
        )

    except Exception as e:
        logger.error(f"Error loading task types: {e}")

    return task_type_map


def print_detailed_results(
    eval_output_path: str,
    checkpoint_types: List[str],
    dataset_path: str = None,
):
    """
    Print detailed evaluation results to console.

    Args:
        eval_output_path: Path to results_exec_eval.jsonl
        checkpoint_types: List of checkpoint types
        dataset_path: Path to dataset JSON file for task type mapping (optional)
    """
    logger.success("=" * 80)
    logger.success("EXECUTION EVALUATION RESULTS")
    logger.success("=" * 80)

    scores = []
    task_details = []
    checkpoint_type_scores = {"personalized_search": [], "operate": []}

    # Load task type mapping if dataset path is provided
    task_type_map = {}
    if dataset_path:
        task_type_map = load_task_type_mapping(dataset_path)

    with open(eval_output_path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            task_id = entry.get("task_id")
            score = entry.get("exec_score")
            exec_eval = entry.get("exec_eval", {})
            query = entry.get("query", {})
            instruction = query.get("instruction", "") if isinstance(query, dict) else ""

            if score is not None:
                scores.append(score)
                checkpoint_scores = exec_eval.get("checkpoint_scores", [])

                # Group by checkpoint type
                for cp in checkpoint_scores:
                    cp_type = cp.get("checkpoint_type", "unknown")
                    if cp_type in checkpoint_type_scores:
                        checkpoint_type_scores[cp_type].append(cp.get("score", 0))

                task_details.append({
                    "task_id": task_id,
                    "instruction": instruction,
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

    # Checkpoint type statistics
    logger.success(f"\nCheckpoint Type Statistics:")
    for cp_type, cp_scores in checkpoint_type_scores.items():
        if cp_scores:
            avg = sum(cp_scores) / len(cp_scores)
            logger.success(f"  {cp_type}: {len(cp_scores)} checkpoints, avg score = {avg:.3f}")

    # Single/Multi Server statistics (if dataset path is provided)
    if task_type_map:
        logger.success(f"\nSingle/Multi Server Statistics:")

        single_scores = []
        multi_scores = []

        for detail in task_details:
            instruction = detail.get("instruction", "")
            score = detail["score"]

            # Compute instruction hash for matching
            if instruction:
                from hashlib import md5
                instruction_hash = md5(instruction.encode('utf-8')).hexdigest()
                task_type = task_type_map.get(instruction_hash)

                if task_type == "single_server":
                    single_scores.append(score)
                elif task_type == "multi_server":
                    multi_scores.append(score)

        if single_scores:
            single_avg = sum(single_scores) / len(single_scores)
            logger.success(f"  Single Server: {len(single_scores)} tasks, avg score = {single_avg:.3f}")
        else:
            logger.warning(f"  Single Server: No tasks found")

        if multi_scores:
            multi_avg = sum(multi_scores) / len(multi_scores)
            logger.success(f"  Multi Server:  {len(multi_scores)} tasks, avg score = {multi_avg:.3f}")
        else:
            logger.warning(f"  Multi Server: No tasks found")

        # Combined statistics
        if single_scores and multi_scores:
            logger.success(f"\n  Comparison:")
            logger.success(f"    Single avg: {single_avg:.3f} vs Multi avg: {multi_avg:.3f}")
            logger.success(f"    Difference: {multi_avg - single_avg:+.3f}")

    # Per-task details
    logger.success(f"\nPer-Task Details:")
    for detail in sorted(task_details, key=lambda x: x["score"], reverse=True):
        task_id = detail["task_id"]
        score = detail["score"]
        checkpoints = detail["checkpoints"]
        logger.success(f"  Task {task_id}: Score = {score:.3f} ({len(checkpoints)} checkpoints)")

    logger.success("=" * 80)


def get_prompt_paths(checkpoint_type: str) -> Tuple[str, str, str]:
    """
    Get prompt file paths based on checkpoint type.

    Args:
        checkpoint_type: Type of checkpoint ("personalized_search" or "operate")

    Returns:
        Tuple of (sys_prompt_path, user_prompt_path, config_path)
    """
    config_path = "/data/JohnDoe/MCP-Persona/v3/config.yaml"

    if checkpoint_type == "personalized_search":
        sys_prompt_path = "/data/JohnDoe/MCP-Persona/v3/prompt/new/eval_prompt/exec_eval_search_sys.md"
        user_prompt_path = "/data/JohnDoe/MCP-Persona/v3/prompt/new/eval_prompt/exec_eval_search_user.md"
    elif checkpoint_type == "operate":
        sys_prompt_path = "/data/JohnDoe/MCP-Persona/v3/prompt/new/eval_prompt/exec_eval_operate_sys.md"
        user_prompt_path = "/data/JohnDoe/MCP-Persona/v3/prompt/new/eval_prompt/exec_eval_operate_user.md"
    else:
        raise ValueError(f"Unknown checkpoint_type: {checkpoint_type}")

    return sys_prompt_path, user_prompt_path, config_path


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
    eval_output_path = os.path.join(results_dir, "results_exec_eval.jsonl")
    pdf_output_path = os.path.join(results_dir, f"exec_score_distribution.pdf")

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

    # Load data using ExecutionDataLoader
    loader = ExecutionDataLoader(
        data_path=data_path,
        results_path=results_path,
        eval_output_path=eval_output_path,
    )

    data_pool, existing_evals, checkpoint_types = loader.load()

    if not data_pool:
        logger.success("No new tasks to evaluate (all tasks already evaluated)")
        if os.path.exists(eval_output_path):
            generate_score_distribution_chart(eval_output_path, pdf_output_path, checkpoint_types)
            print_detailed_results(eval_output_path, checkpoint_types)
        return

    # Group data pool by checkpoint type
    search_pool = [e for e in data_pool if e.get("_checkpoint_type") == "personalized_search"]
    operate_pool = [e for e in data_pool if e.get("_checkpoint_type") == "operate"]

    logger.success(f"Data pool: {len(search_pool)} search checkpoints, {len(operate_pool)} operate checkpoints")

    all_eval_results = []

    # Evaluate personalized_search checkpoints
    if search_pool:
        logger.success("\nEvaluating personalized_search checkpoints...")
        sys_prompt_path, user_prompt_path, config_path = get_prompt_paths("personalized_search")

        pipeline = DataGenerationPipeline(
            config_path=config_path,
            experiment_name=f"exec_eval_search_{model_name}",
            system_prompt_path=sys_prompt_path,
            user_prompt_path=user_prompt_path,
            need_time_stamp=False,
        )

        try:
            search_results = pipeline.run(
                data_pool=search_pool,
                concurrency_limit=config.get("concurrency_limit", 5),
                extract_function=extract_exec_score,
            )
            all_eval_results.extend(search_results)
            logger.success(f"Completed {len(search_results)} search checkpoint evaluations")
        except Exception as e:
            logger.error(f"Search evaluation pipeline failed: {e}")
            raise

    # Evaluate operate checkpoints
    if operate_pool:
        logger.success("\nEvaluating operate checkpoints...")
        sys_prompt_path, user_prompt_path, config_path = get_prompt_paths("operate")

        pipeline = DataGenerationPipeline(
            config_path=config_path,
            experiment_name=f"exec_eval_operate_{model_name}",
            system_prompt_path=sys_prompt_path,
            user_prompt_path=user_prompt_path,
            need_time_stamp=False,
        )

        try:
            operate_results = pipeline.run(
                data_pool=operate_pool,
                concurrency_limit=config.get("concurrency_limit", 5),
                extract_function=extract_exec_score,
            )
            all_eval_results.extend(operate_results)
            logger.success(f"Completed {len(operate_results)} operate checkpoint evaluations")
        except Exception as e:
            logger.error(f"Operate evaluation pipeline failed: {e}")
            raise

    logger.success(f"Pipeline completed, evaluated {len(all_eval_results)} checkpoints")

    # Reconstruct task-level results
    task_evals = reconstruct_exec_results(all_eval_results)

    # Save evaluation results
    save_exec_evaluation_results(
        data_path=data_path,
        results_path=results_path,
        eval_output_path=eval_output_path,
        task_evals=task_evals,
        existing_evals=existing_evals,
    )

    # Generate score distribution chart
    generate_score_distribution_chart(eval_output_path, pdf_output_path, checkpoint_types)

    # Print detailed results (with dataset path for single/multi server statistics)
    print_detailed_results(eval_output_path, checkpoint_types, dataset_path=data_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Execution-based evaluation with checkpoint type-specific prompts"
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
        help="Path to data JSON file with gt checkpoints",
    )

    args = parser.parse_args()
    main(vars(args))
