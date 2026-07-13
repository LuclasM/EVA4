"""
loops/task_runner.py — 递归任务分解与执行

LLM 决定每个节点是否继续分解，无深度限制。
_run_node 递归调用自身：分解 → 对每个子任务递归 → 合并结果。
每个节点状态变化后立即持久化整棵树到 DB 和 MemoryStore。
"""

import datetime
import json
import re
import uuid

from loops.agent_loop import run_agent
from loops._upgrade_eval import UpgradeEvaluator
from memory.task_memory import TaskMemory, _tree_had_failure
from utils.display import info, dim, ok, err
import i18n as T

# Feedback loop tuning (see TaskRunner._needs_feedback / _maybe_collect_feedback)
_MAX_FEEDBACK_ROUNDS = 4   # cap on back-and-forth clarifying questions
_MAX_FEEDBACK_REDOS  = 2   # cap on recursive redo attempts triggered by feedback


def _node(goal: str) -> dict:
    return {
        "id":       uuid.uuid4().hex[:8],
        "goal":     goal,
        "status":   "pending",
        "result":   "",
        "subtasks": [],
        "atomic":   False,
    }


class TaskRunner:

    def __init__(self, llm, schemas, fns,
                 task_memory: TaskMemory, mem_store, session_id: str,
                 progress_callback=None, supplement_queue=None):
        self.llm               = llm
        self.schemas           = schemas
        self.fns               = fns
        self.task_memory       = task_memory
        self.mem_store         = mem_store
        self.session_id        = session_id
        self.progress_callback = progress_callback
        self.supplement_queue  = supplement_queue
        # P0-4: 升级触发机制 - 跟踪 root 任务完成情况
        self._upgrade_evaluator = UpgradeEvaluator(self.llm, self.task_memory, self.mem_store)

    # ── 入口 ─────────────────────────────────────────────

    def run(self, goal: str, _redo_depth: int = 0, on_result=None) -> str:
        display_goal = _strip_adapter_prefix(goal)   # clean goal for DB / display
        self.llm.set_goal(display_goal)              # classify without adapter noise
        root         = _node(goal)                   # full goal (with adapter context) for LLM
        record_id   = uuid.uuid4().hex[:12]
        started     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mem_id      = [None]   # list 让递归内层可以修改
        aar_mem_ids: list[str] = []   # 本次任务过程中写入的 AAR 经验记忆，供反馈附加

        self._persist(record_id, root, "running", "", [], started, display_goal)

        try:
            history_ctx = self.task_memory.build_context(goal)
            self._run_node(root, root, record_id, started, history_ctx, mem_id, aar_mem_ids, depth=0)
        except KeyboardInterrupt:
            self._mark_interrupted(root)
            self._persist(record_id, root, "active", T.sentinel_user_interrupted(), [], started, display_goal)
            self._cleanup_mem(mem_id)
            raise

        final = root.get("result", T.sentinel_not_completed())
        if on_result:
            on_result(final)   # show the result to the user before asking for feedback
        summary, artifacts = self._post_process(goal, final)
        feedback_decision = self._maybe_collect_feedback(display_goal, summary, final, root, started, aar_mem_ids)
        self._persist(record_id, root, "active", summary, artifacts, started, display_goal)

        self._cleanup_mem(mem_id)

        archived = self.task_memory.archive_old()
        if archived:
            print(dim(T.archived_note(archived)))
        if self.task_memory.maybe_compress(self.llm):
            print(dim(T.compressed_note()))

        # P0-4: 任务完成后评估是否需要系统升级
        self._upgrade_evaluator.evaluate_after_task(goal, final)

        if (feedback_decision and feedback_decision.get("action") == "redo"
                and feedback_decision.get("new_goal") and _redo_depth < _MAX_FEEDBACK_REDOS):
            print(dim(T.feedback_redo_note()))
            return self.run(feedback_decision["new_goal"], _redo_depth=_redo_depth + 1, on_result=on_result)

        return final

    # ── 递归核心 ─────────────────────────────────────────

    def _run_node(self, node: dict, root: dict, record_id: str, started: str,
                  history_ctx: str, mem_id: list, aar_mem_ids: list, depth: int,
                  ancestor_goals: list[str] | None = None,
                  prior_results: list[dict] | None = None) -> None:
        indent   = "  " * depth
        ancestors = ancestor_goals or []
        prior     = prior_results or []

        subtask_goals = self._decompose(node["goal"], history_ctx, root, ancestors)

        if subtask_goals:
            node["subtasks"] = [_node(st) for st in subtask_goals]
            self._print_decompose(node["goal"], subtask_goals, depth)
            self._save(record_id, root, started)
            mem_id[0] = self._write_mem(root, mem_id[0])

            child_ancestors = ancestors + [node["goal"]]
            child_prior: list[dict] = []          # 本层已完成的兄弟结果
            for child in node["subtasks"]:
                self._run_node(child, root, record_id, started, history_ctx,
                               mem_id, aar_mem_ids, depth + 1, child_ancestors, child_prior)
                # 子节点完成后，把完整结果加入兄弟列表供下一个子节点使用
                if child["status"] in ("done", "failed"):
                    child_prior.append({"goal": child["goal"], "result": child["result"]})

            node["status"] = "running"
            result = self._synthesize(node)
            node["result"] = result
            node["status"] = "done"
            print(f"{indent}{ok('◉')} {T.merge_line(node['goal'][:60])}")

        else:
            node["atomic"] = True
            node["status"] = "running"
            self._save(record_id, root, started)
            mem_id[0] = self._write_mem(root, mem_id[0])

            result = self._execute(node, history_ctx, root, ancestors, prior)
            node["result"] = result
            node["status"] = "failed" if _is_failed(result) else "done"
            icon = ok("✓") if node["status"] == "done" else err("✗")
            print(f"{indent}  {icon} {node['goal'][:60]}")

            # P0-3: 原子任务完成后自动执行 AAR
            aar_id = self._auto_aar(node, ancestors)
            if aar_id:
                aar_mem_ids.append(aar_id)
        # 每个节点完成后持久化
        self._save(record_id, root, started)
        mem_id[0] = self._write_mem(root, mem_id[0])

    # ── 分解决策 ─────────────────────────────────────────

    def _decompose(self, goal: str, history_ctx: str, root: dict,
                   ancestors: list[str]) -> list[str] | None:
        # 祖先路径中已有相同目标 → 强制原子，防止无限递归
        if any(_goals_similar(goal, a) for a in ancestors):
            return None

        tree_ctx     = self._tree_str(root)
        ancestor_str = " › ".join(ancestors[-3:]) if ancestors else ""
        history_block  = f"Work history:\n{history_ctx[:600]}\n\n---\n\n" if history_ctx else ""
        task_path_block = f"Task path: {ancestor_str} › {goal}\n\n" if ancestor_str else ""
        prompt = (
            f"{history_block}"
            f"Current task tree:\n{tree_ctx}\n\n---\n\n"
            f"{task_path_block}"
            f"Pending task: {goal}\n\n"
            "Does this task need to be decomposed into multiple independent subtasks?\n"
            "- Can be done via tool calls or direct reasoning → {\"atomic\": true}\n"
            "- Needs multiple independent steps → {\"subtasks\": [\"step1\", \"step2\", ...]}\n\n"
            "Note: subtasks must not duplicate the current task or any ancestor task's goal.\n"
            "Return JSON only, no other text."
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                if data.get("atomic"):
                    return None
                sts = data.get("subtasks", [])
                if isinstance(sts, list) and len(sts) >= 2:
                    # 过滤掉与祖先相似的子任务
                    filtered = [
                        str(s).strip() for s in sts
                        if str(s).strip()
                        and not any(_goals_similar(str(s), a) for a in ancestors + [goal])
                    ]
                    if len(filtered) >= 2:
                        return filtered
        except Exception:
            pass
        return None

    # ── 原子执行 ─────────────────────────────────────────

    def _execute(self, node: dict, history_ctx: str,
                 root: dict, ancestors: list[str],
                 prior_results: list[dict] | None = None) -> str:
        prior = prior_results or []

        # 完整任务树（含各节点完整结果，供 LLM 了解全局）
        tree_str         = self._tree_str_full(root)
        path             = " › ".join(ancestors + [node["goal"]]) if ancestors else node["goal"]
        immediate_parent = ancestors[-1] if ancestors else ""

        # 已完成的前置步骤结果（完整，不截断）
        prior_section = ""
        if prior:
            lines = ["=== Completed prior step results (use directly, do not redo) ==="]
            for i, pr in enumerate(prior, 1):
                lines.append(f"\n[Step {i}] {pr['goal']}")
                lines.append(pr["result"])
            prior_section = "\n".join(lines) + "\n\n"

        full_ctx = (
            f"=== Current task tree ===\n{tree_str}\n\n"
            f"=== Current execution point ===\n{path}\n\n"
            + prior_section +
            "[Execution rules] This is an atomic subtask produced by the task planner.\n"
            "1. The 'completed prior step results' above may already contain the actual facts "
            "you need for this task — data, figures, dates, names, case details, etc., not just "
            "technical artifacts like paths/cookies/IDs. Extract and reuse them directly. Only "
            "use tools to fetch information that is genuinely NOT already covered above.\n"
            "2. Do not redo steps already completed above (e.g. re-querying a system that was "
            "already queried, re-deriving a fact already stated).\n"
            "3. Use tools to complete this task. Do not decompose it further.\n"
        )
        # 重新拉取最新上下文（任务执行中可能写入了新记忆）
        fresh_ctx = self.task_memory.build_context(node["goal"])
        if fresh_ctx:
            full_ctx += f"\n{fresh_ctx}"

        task = {
            "id":     uuid.uuid4().hex[:12],
            "goal":   node["goal"],
            "status": "active",
            "log":    "",
            "result": "",
        }
        # Link this node to its execution transcript (SESSION_DIR/messages/{exec_id}.json),
        # so a failed node can be traced back to the full tool-call log after the fact.
        node["exec_id"] = task["id"]

        try:
            result = run_agent(
                node["goal"], task, self.llm, self.schemas, self.fns,
                task_context=full_ctx,
                parent_goal=immediate_parent,
                progress_callback=self.progress_callback,
                supplement_queue=self.supplement_queue,
            )
            task["result"] = result
            task["status"] = "done"
        except Exception as e:
            task["status"] = "failed"
            result = T.sentinel_exec_error(e)
            print(err(T.tool_error_line(e)))

        return result



    def _auto_aar(self, node: dict, ancestors: list[str]) -> str | None:
        """P0-3: 原子任务完成后自动执行 After Action Review。返回写入的记忆 id（若有）。"""
        log = node.get("result", "")
        if not log or len(log) < 50:
            return None  # 结果太短，跳过 AAR

        goal = node["goal"]
        status = node["status"]
        status_text = "success" if status == "done" else "failure"

        prompt = (
            f"You just completed a task: {goal}\n"
            f"Result status: {status_text}\n"
            f"Result summary: {log[:1500]}\n\n"
            "Please perform an After Action Review:\n"
            "1. What went well? (extract reusable methods)\n"
            "2. What problems came up? (record lessons learned)\n"
            "3. Anything worth remembering?\n\n"
            "If there is an experience worth recording, return JSON:\n"
            '{"experience": "...", "type": "experience or workflow", "tags": ["tag1"], "importance": 5}\n'
            "If not, return: {\"experience\": null}\n"
            "Return JSON only, no other text."
        )
        try:
            resp = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                exp = data.get("experience")
                if exp and isinstance(exp, str) and len(exp) > 10:
                    mid = self.mem_store.write(
                        content=exp,
                        type=data.get("type", "experience"),
                        tags=data.get("tags", [goal[:20]]),
                        importance=min(10, max(1, data.get("importance", 5))),
                        source="first_hand",       # own direct execution, not external material
                        credibility=9,
                    )
                    brain_icon = ok("\U0001f9e0")
                    print(f"{'  ' * (len(ancestors) + 1)}{brain_icon} {T.aar_saved(mid)}")
                    return mid
        except Exception:
            pass  # AAR 失败不应影响主任务
        return None

    def _synthesize(self, node: dict) -> str:
        lines = [
            f"Subtask {i+1}: {st['goal']}\nResult: {st['result']}"
            for i, st in enumerate(node["subtasks"])
        ]
        prompt = (
            f"You completed the task: {node['goal']}\n\n"
            "Subtask results:\n" + "─" * 40 + "\n"
            + "\n\n".join(lines)
            + "\n" + "─" * 40 + "\n\n"
            "Synthesize the results above into a complete answer to the original task. Be concise and accurate."
        )
        try:
            return self.llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        except Exception:
            return "\n".join(f"{st['goal']}: {st['result'][:200]}" for st in node["subtasks"])

    # ── 持久化 ───────────────────────────────────────────

    def _persist(self, record_id: str, root: dict, tier: str,
                 summary: str, artifacts: list, created_at: str,
                 display_goal: str | None = None) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.task_memory.save({
            "id":           record_id,
            "session_id":   self.session_id,
            "goal":         display_goal or root["goal"],
            "summary":      summary,
            "artifacts":    artifacts,
            "tree":         root,
            "importance":   7,
            "tier":         tier,
            "created_at":   created_at,
            "completed_at": now,
        })

    def _save(self, record_id: str, root: dict, started: str) -> None:
        self._persist(record_id, root, "running", "", [], started)

    def _write_mem(self, root: dict, existing_id: str | None) -> str:
        content = f"{T.current_task_tree_label()}\n{self._tree_str_full(root)}"
        if existing_id:
            try:
                if self.mem_store.update(existing_id, content=content):
                    return existing_id
            except Exception:
                pass
        return self.mem_store.write(
            content=content,
            type="task_state",
            tags=["task_state", root["goal"][:20]],
            importance=8,
        )

    # ── 完成摘要 ─────────────────────────────────────────

    def _post_process(self, goal: str, result: str) -> tuple[str, list]:
        prompt = (
            f"Task: {goal}\n\nResult:\n{result[:2000]}\n\n"
            "Generate:\n1. A one-sentence summary (max ~80 words)\n"
            "2. A list of artifacts (file paths, URLs, key findings; empty array if none)\n\n"
            'Return JSON only: {"summary": "...", "artifacts": [{"type": "file", "path": "...", "desc": "..."}]}'
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get("summary", result[:80]), data.get("artifacts", [])
        except Exception:
            pass
        return result[:80], []

    def _maybe_collect_feedback(self, goal: str, summary: str, final: str,
                                root: dict, started: str, aar_mem_ids: list[str]) -> dict | None:
        """Ask the user for feedback on the completed task, only when it's warranted
        (first time doing this kind of task, mid-task errors/retries, long-running,
        large/multi-step, or an open-ended/subjective result). Routine, quick, clean
        results are skipped — not every task needs a check-in.

        Only runs interactively — in non-interactive channels (API/adapters) ask_user
        raises _NeedUserInput, which we treat as "no feedback loop available here" and skip.

        On negative feedback, keeps asking short clarifying questions (capped at
        _MAX_FEEDBACK_ROUNDS) until the user gives a clear instruction to end or to
        redo the task with a corrected approach. Always ends by folding the exchange
        (transcript + distilled lesson) into this task's AAR memories — or a standalone
        record if it produced none — so it feeds future learning.

        Returns a decision dict {"action": "redo"|"end", ...} or None if no feedback
        was collected at all.
        """
        from tools.user_input import ask_user, _NeedUserInput
        if not self._needs_feedback(goal, final, root, started):
            return None
        try:
            answer = ask_user(T.feedback_question(summary))
        except _NeedUserInput:
            return None
        if not answer or answer == T.ask_user_no_answer():
            return None

        transcript = [{"q": T.feedback_question(summary), "a": answer}]
        decision   = self._interpret_feedback(goal, summary, transcript)

        rounds = 0
        while decision.get("action") == "continue" and rounds < _MAX_FEEDBACK_ROUNDS:
            next_q = decision.get("question") or T.feedback_followup_default()
            try:
                reply = ask_user(next_q)
            except _NeedUserInput:
                break
            transcript.append({"q": next_q, "a": reply})
            decision = self._interpret_feedback(goal, summary, transcript)
            rounds  += 1

        self._save_feedback_memory(goal, summary, transcript, decision, aar_mem_ids)
        print(dim(T.feedback_saved()))
        return decision

    def _needs_feedback(self, goal: str, final: str, root: dict, started: str) -> bool:
        """Ask the LLM whether this task's result warrants a feedback check-in.
        We compute the objective signals (they're cheap) but the decision itself is
        always the LLM's call, not a hardcoded gate.
        """
        elapsed = (
            datetime.datetime.now() - datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
        ).total_seconds()
        had_failure = _tree_had_failure(root)
        node_count  = self._count_nodes(root)
        try:
            first_time = not self.task_memory.get_relevant(goal, limit=1)
        except Exception:
            first_time = False

        prompt = (
            f"Task: {goal}\nResult: {final[:600]}\n\n"
            f"Signals: elapsed={elapsed:.0f}s, subtask_count={node_count}, "
            f"had_failure_or_retry={had_failure}, first_time_similar_task={first_time}\n\n"
            "Decide whether to ask the user for feedback on this result. Ask if ANY of: "
            "this is the first time doing this kind of task, errors/retries happened mid-task, "
            "it took a long time, it's a large/multi-step task, or the result is open-ended/"
            "subjective — no single objectively correct answer, so the user's judgment matters "
            "(writing, recommendations, creative work, ambiguous requests). Skip for simple, "
            "routine, quick tasks with a clean, unambiguous, verifiable result.\n"
            'Return JSON only: {"ask_feedback": true/false}'
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=50)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                return bool(json.loads(match.group()).get("ask_feedback", False))
        except Exception:
            pass
        return False

    def _interpret_feedback(self, goal: str, summary: str, transcript: list[dict]) -> dict:
        """One LLM call that both classifies sentiment and decides the next move,
        mirroring the JSON-decision pattern used by _decompose/_post_process/_auto_aar.
        """
        convo = "\n".join(f"- Q: {t['q']}\n  A: {t['a']}" for t in transcript)
        prompt = (
            f"Task: {goal}\nResult summary: {summary}\n\nFeedback conversation so far:\n{convo}\n\n"
            "Decide what to do next:\n"
            '- User is satisfied / confirms the result is good: '
            '{"sentiment": "positive", "action": "end"}\n'
            '- User is unsatisfied, AND has given a concrete new approach to try, AND has '
            'explicitly confirmed they want the task redone (both conditions required — do not '
            'infer consent to redo just because a correction was mentioned): '
            '{"sentiment": "negative", "action": "redo", '
            '"new_goal": "<original task re-stated, incorporating the corrected approach>"}\n'
            '- User is unsatisfied and either explicitly wants to stop without redoing, or '
            'doesn\'t know the right answer and declines to redo: '
            '{"sentiment": "negative", "action": "end"}\n'
            '- Not yet resolved — ask ONE short concrete question. If you don\'t yet know what '
            'was wrong or what to do instead, ask that. If you know what\'s wrong but don\'t yet '
            'have both a concrete new approach and explicit confirmation to redo, ask directly: '
            '"want me to redo it with X approach?": '
            '{"sentiment": "negative", "action": "continue", "question": "..."}\n\n'
            "Return JSON only, no other text."
        )
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=400)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                data = json.loads(match.group())
                if data.get("action") in ("redo", "end", "continue"):
                    return data
        except Exception:
            pass
        return {"sentiment": "negative", "action": "end"}

    def _save_feedback_memory(self, goal: str, summary: str, transcript: list[dict],
                              decision: dict, aar_mem_ids: list[str]) -> None:
        """Fold feedback into the AAR experience memories this task actually wrote,
        instead of filing it as a disconnected record. That way a future retrieval
        of "what we tried" also carries "and here's what the user said about it" —
        a correction stored off to the side is easy for search to surface the
        original (now-outdated) lesson without ever showing the fix alongside it.

        Falls back to a standalone feedback memory only when this task produced no
        AAR memory to attach to (e.g. the result was too short/trivial for _auto_aar
        to extract anything).
        """
        convo     = "\n".join(f"Q: {t['q']}\nA: {t['a']}" for t in transcript)
        sentiment = decision.get("sentiment", "negative")

        prompt = (
            f"Task: {goal}\nSummary: {summary}\nSentiment: {sentiment}\nConversation:\n{convo}\n\n"
            "Extract a concise, reusable lesson from this feedback exchange for future similar tasks "
            "(what to do differently, what the user actually wants, pitfalls to avoid).\n"
            'If there is a lesson worth recording, return: {"experience": "..."}\n'
            'Otherwise return: {"experience": null}\n'
            "Return JSON only."
        )
        experience = None
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=400)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                experience = json.loads(match.group()).get("experience")
        except Exception:
            pass

        if aar_mem_ids:
            self._apply_targeted_feedback(goal, summary, convo, sentiment, experience, aar_mem_ids)
            return

        content = f"Task: {goal}\nSummary: {summary}\nSentiment: {sentiment}\nConversation:\n{convo}"
        if experience:
            content += f"\nExtracted lesson: {experience}"
        self.mem_store.write(
            content=content,
            type="feedback",
            tags=["user_feedback", sentiment, goal[:20]],
            importance=8 if sentiment == "negative" else 6,
            source="user_instruction",   # directly stated by the user
            credibility=9,
        )

    def _apply_targeted_feedback(self, goal: str, summary: str, convo: str, sentiment: str,
                                 experience: str | None, aar_mem_ids: list[str]) -> None:
        """Feedback about an overall multi-step task doesn't indict every step that
        ran — a specific step's approach can be confirmed as correct even while the
        final result needs work (e.g. data collection was fine, the write-up wasn't).
        Blindly tagging every AAR memory from this task as "negative" would misrepresent
        the steps that were actually fine, and blindly tagging them all "positive" would
        hide a real correction. Ask the LLM to judge each step's memory against the
        feedback individually instead of applying one verdict to all of them.
        """
        records = {}
        for mid in aar_mem_ids:
            r = self.mem_store.get(mid)
            if r:
                records[mid] = r
        if not records:
            return

        steps_block = "\n\n".join(
            f"[id={mid}] Step: {r['content'][:400]}" for mid, r in records.items()
        )
        prompt = (
            f"Task: {goal}\nOverall result: {summary}\nOverall sentiment: {sentiment}\n"
            f"Feedback conversation:\n{convo}\n\n"
            f"This task ran the following steps, each already recorded as its own experience "
            f"memory:\n\n{steps_block}\n\n"
            "The feedback is about the OVERALL result, but may not apply equally to every "
            "step — a specific step's approach can be correct even if the final result needs "
            "work, or vice versa. For EACH step id above, decide:\n"
            '- "confirmed": the feedback shows this specific step\'s approach was fine\n'
            '- "corrected": the feedback specifically points out a problem with this step, '
            "or gives a correction that applies to it\n"
            '- "unrelated": the feedback doesn\'t say anything about this specific step\n\n'
            'Return JSON only: {"steps": [{"id": "...", "verdict": "confirmed|corrected|unrelated", '
            '"note": "short explanation, or the correction if corrected — omit if unrelated"}]}'
        )
        verdicts = None
        try:
            resp    = self.llm.chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=600)
            cleaned = re.sub(r'```[a-z]*\n?', '', resp).strip()
            match   = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if match:
                verdicts = json.loads(match.group()).get("steps")
        except Exception:
            pass

        if not isinstance(verdicts, list) or not verdicts:
            # Couldn't tell which steps the feedback applies to — fall back to
            # applying the overall sentiment to all of them rather than silently
            # dropping the feedback.
            verdicts = [
                {"id": mid, "verdict": "corrected" if sentiment == "negative" else "confirmed",
                 "note": experience or ""}
                for mid in records
            ]

        by_id = {v.get("id"): v for v in verdicts if isinstance(v, dict)}
        for mid, record in records.items():
            v = by_id.get(mid)
            if not v or v.get("verdict") == "unrelated":
                continue
            verdict = v.get("verdict", "corrected")
            note    = v.get("note") or experience or ""
            tag     = "confirmed" if verdict == "confirmed" else "corrected"
            label   = "Confirmed correct" if verdict == "confirmed" else "Correction"
            feedback_block = (f"\n\n---\n[User feedback — {tag}]\n{label}: {note}" if note
                              else f"\n\n---\n[User feedback — {tag}]")
            tags = list(record.get("tags") or [])
            for t in ("user_feedback", tag):
                if t not in tags:
                    tags.append(t)
            importance_floor = 9 if verdict == "corrected" else 7
            self.mem_store.update(
                mid,
                content=record["content"] + feedback_block,
                tags=tags,
                importance=max(record.get("importance", 5), importance_floor),
                credibility=10,
            )

    def _count_nodes(self, node: dict) -> int:
        return 1 + sum(self._count_nodes(st) for st in node.get("subtasks", []))

    # ── 树显示 ───────────────────────────────────────────

    def _tree_str(self, root: dict) -> str:
        """终端显示用（结果截断到60字）。"""
        lines = []
        self._fmt_node(root, lines, 0)
        return "\n".join(lines)

    def _tree_str_full(self, root: dict) -> str:
        """注入 LLM context 用（结果完整，不截断）。"""
        lines = []
        self._fmt_node_full(root, lines, 0)
        return "\n".join(lines)

    def _fmt_node(self, node: dict, lines: list, depth: int) -> None:
        icon   = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}.get(node["status"], "?")
        indent = "  " * depth
        result = f" → {node['result'][:60]}" if node.get("result") else ""
        lines.append(f"{indent}[{icon}] {node['goal']}{result}")
        for st in node.get("subtasks", []):
            self._fmt_node(st, lines, depth + 1)

    def _fmt_node_full(self, node: dict, lines: list, depth: int) -> None:
        """完整结果版本，供 LLM context 使用。"""
        icon   = {"pending": "○", "running": "▶", "done": "✓", "failed": "✗"}.get(node["status"], "?")
        indent = "  " * depth
        lines.append(f"{indent}[{icon}] {node['goal']}")
        if node.get("result"):
            for ln in node["result"].splitlines():
                lines.append(f"{indent}    {ln}")
        for st in node.get("subtasks", []):
            self._fmt_node_full(st, lines, depth + 1)

    def _cleanup_mem(self, mem_id: list) -> None:
        if mem_id[0]:
            try:
                self.mem_store.delete(mem_id[0])
            except Exception:
                pass
            mem_id[0] = None

    def _mark_interrupted(self, node: dict) -> None:
        if node["status"] not in ("done", "failed"):
            node["status"] = "failed"
            if not node.get("result"):
                node["result"] = T.sentinel_user_interrupted()
        for st in node.get("subtasks", []):
            self._mark_interrupted(st)

    def _print_decompose(self, goal: str, subtasks: list[str], depth: int) -> None:
        indent = "  " * depth
        print(f"\n{indent}{info('◈')} {T.decompose_line(goal)}")
        for i, st in enumerate(subtasks, 1):
            print(f"{indent}  {dim(str(i) + '.')} {st}")


# ── 模块级辅助 ────────────────────────────────────────────

def _is_failed(result: str) -> bool:
    return any(result.startswith(p) for p in T.failed_prefixes())


def _strip_adapter_prefix(goal: str) -> str:
    """Remove messaging-adapter context prefixes injected before the actual task goal."""
    return re.sub(r'^\[[^\]]{0,200}\]\s*', '', goal).strip()


def _goals_similar(a: str, b: str) -> bool:
    """判断两个目标是否高度相似（防止子任务包含与祖先相同的目标）。"""
    a, b = a.strip().lower(), b.strip().lower()
    if a == b:
        return True
    # 一方是另一方的子串且长度接近
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter and shorter in longer and len(shorter) / len(longer) > 0.7:
        return True
    return False
