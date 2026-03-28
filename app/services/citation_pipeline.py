from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.services.ncbi import NCBIClient, PubMedArticle
from app.services.openai_compat import OpenAICompatClient


class CitationPipelineError(RuntimeError):
    pass


@dataclass(slots=True)
class SentenceChunk:
    sentence_id: int
    text: str
    start: int
    end: int


class CitationPipeline:
    def __init__(self, llm: OpenAICompatClient, ncbi: NCBIClient) -> None:
        self.llm = llm
        self.ncbi = ncbi

    def run(
        self,
        text: str,
        max_targets: int = 4,
        max_attempts: int = 10,
        results_per_query: int = 6,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        source_text = (text or "").strip()
        if not source_text:
            raise CitationPipelineError("Text is required.")
        if len(source_text) > 20000:
            raise CitationPipelineError("Text is too long. Please keep it within 20,000 characters.")

        _emit_progress(
            progress_callback,
            stage="split_sentences",
            progress_percent=6,
            message="拆分文本。",
            detail="正在识别句子边界。",
        )
        sentences = split_sentences(source_text)
        if not sentences:
            raise CitationPipelineError("Could not identify sentences from the provided text.")

        _emit_progress(
            progress_callback,
            stage="select_targets",
            progress_percent=14,
            message=f"已识别 {len(sentences)} 个句子。",
            detail="正在选择最需要插入文献的位置。",
        )
        planned_targets = sorted(
            self._select_targets(sentences, max_targets),
            key=lambda item: item["sentence_id"],
        )
        placements: list[dict[str, Any]] = []
        unresolved_targets: list[dict[str, Any]] = []
        marker_map: dict[str, int] = {}
        references: list[dict[str, Any]] = []
        total_targets = len(planned_targets)

        _emit_progress(
            progress_callback,
            stage="targets_selected",
            progress_percent=22,
            message=f"已选中 {total_targets} 处候选句子。",
            detail="开始逐句检索 PubMed。",
        )

        for target_index, target in enumerate(planned_targets, start=1):
            sentence = next((item for item in sentences if item.sentence_id == target["sentence_id"]), None)
            if sentence is None:
                continue

            _emit_progress(
                progress_callback,
                stage="resolve_target",
                progress_percent=_target_progress(target_index - 1, total_targets, 0, max_attempts),
                message=f"处理第 {target_index}/{max(total_targets, 1)} 处候选句子。",
                detail=sentence.text,
            )
            resolution = self._resolve_target(
                sentence=sentence,
                claim_summary=target.get("claim_summary", ""),
                reason=target.get("reason", ""),
                initial_query=target.get("initial_query", ""),
                max_attempts=max_attempts,
                results_per_query=results_per_query,
                target_index=target_index,
                total_targets=total_targets,
                progress_callback=progress_callback,
            )

            if not resolution["accepted"]:
                unresolved_targets.append(
                    {
                        "sentence_id": sentence.sentence_id,
                        "sentence_text": sentence.text,
                        "claim_summary": target.get("claim_summary", ""),
                        "reason": target.get("reason", ""),
                        "attempts": resolution["attempts"],
                    }
                )
                continue

            article: PubMedArticle = resolution["article"]
            marker = marker_map.get(article.pmid)
            if marker is None:
                marker = len(marker_map) + 1
                marker_map[article.pmid] = marker
                references.append(
                    {
                        "marker": marker,
                        "article": article.to_brief_dict(),
                        "reference_line": article.to_reference_line(marker),
                    }
                )

            placements.append(
                {
                    "marker": marker,
                    "sentence_id": sentence.sentence_id,
                    "sentence_text": sentence.text,
                    "claim_summary": target.get("claim_summary", ""),
                    "reason": target.get("reason", ""),
                    "final_query": resolution["final_query"],
                    "article": article.to_brief_dict(),
                    "attempts": resolution["attempts"],
                }
                )

        _emit_progress(
            progress_callback,
            stage="finalize",
            progress_percent=95,
            message="整理编号与参考文献。",
            detail="正在生成正文编号与参考文献列表。",
        )
        annotated_text = insert_markers(source_text, sentences, placements)
        reference_block = ""
        if references:
            lines = ["参考文献"]
            lines.extend(reference["reference_line"] for reference in references)
            reference_block = "\n".join(lines)

        return {
            "annotated_text": annotated_text,
            "reference_block": reference_block,
            "references": references,
            "placements": placements,
            "unresolved_targets": unresolved_targets,
            "sentence_count": len(sentences),
            "selected_target_count": len(planned_targets),
        }

    def _select_targets(self, sentences: list[SentenceChunk], max_targets: int) -> list[dict[str, Any]]:
        sentence_lines = [
            f"{sentence.sentence_id}. {sentence.text}" for sentence in sentences[:120] if sentence.text.strip()
        ]
        system_prompt = (
            "You identify which sentences in a biomedical or life-science text most need PubMed references. "
            "Return strict JSON only. The output schema is "
            '{"targets":[{"sentence_id":1,"claim_summary":"...","reason":"...","initial_query":"..."}]}. '
            f"Choose at most {max_targets} sentences. "
            "Use only valid sentence_id values from the provided list. "
            "Prefer factual biomedical claims, mechanisms, treatments, epidemiology, biomarkers, outcomes, or "
            "other statements that clearly benefit from PubMed evidence. "
            "Skip generic introductions, opinions, and sentences that do not need citation. "
            "initial_query must be an English PubMed-style query, concise but specific."
        )
        user_prompt = (
            "Select the best citation targets from the numbered sentences below.\n\n"
            + "\n".join(sentence_lines)
            + "\n\nReturn JSON only."
        )

        response = self.llm.complete_json(system_prompt, user_prompt)
        targets = response.get("targets", [])
        if not isinstance(targets, list):
            raise CitationPipelineError("Model did not return a valid targets list.")

        valid_ids = {sentence.sentence_id for sentence in sentences}
        seen_ids: set[int] = set()
        normalized: list[dict[str, Any]] = []
        for item in targets:
            if not isinstance(item, dict):
                continue
            try:
                sentence_id = int(item.get("sentence_id"))
            except Exception:  # noqa: BLE001
                continue
            if sentence_id not in valid_ids or sentence_id in seen_ids:
                continue
            seen_ids.add(sentence_id)
            normalized.append(
                {
                    "sentence_id": sentence_id,
                    "claim_summary": str(item.get("claim_summary", "")).strip(),
                    "reason": str(item.get("reason", "")).strip(),
                    "initial_query": str(item.get("initial_query", "")).strip(),
                }
            )
            if len(normalized) >= max_targets:
                break
        return normalized

    def _resolve_target(
        self,
        sentence: SentenceChunk,
        claim_summary: str,
        reason: str,
        initial_query: str,
        max_attempts: int,
        results_per_query: int,
        target_index: int,
        total_targets: int,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        current_query = (initial_query or fallback_query(sentence.text)).strip()
        attempts: list[dict[str, Any]] = []

        for attempt_index in range(1, max_attempts + 1):
            _emit_progress(
                progress_callback,
                stage="search_attempt",
                progress_percent=_target_progress(target_index - 1, total_targets, attempt_index - 1, max_attempts),
                message=f"第 {target_index}/{max(total_targets, 1)} 句，第 {attempt_index}/{max_attempts} 轮检索。",
                detail=current_query,
            )
            results = self.ncbi.search_pubmed(current_query, retmax=results_per_query)
            evaluation = self._evaluate_results(
                sentence=sentence,
                claim_summary=claim_summary,
                reason=reason,
                current_query=current_query,
                attempt_index=attempt_index,
                attempts=attempts,
                results=results,
            )
            decision = str(evaluation.get("decision", "")).strip().lower()
            chosen_pmid = str(evaluation.get("chosen_pmid", "")).strip()
            improved_query = str(evaluation.get("improved_query", "")).strip()
            evaluation_reason = str(evaluation.get("reason", "")).strip()
            confidence = evaluation.get("confidence", 0)

            attempts.append(
                {
                    "attempt": attempt_index,
                    "query": current_query,
                    "result_count": len(results),
                    "decision": decision or "retry",
                    "chosen_pmid": chosen_pmid,
                    "confidence": confidence,
                    "reason": evaluation_reason,
                    "top_results": [article.to_brief_dict() for article in results[:3]],
                }
            )

            if decision == "accept":
                _emit_progress(
                    progress_callback,
                    stage="target_accepted",
                    progress_percent=_target_progress(target_index, total_targets, 0, max_attempts),
                    message=f"第 {target_index}/{max(total_targets, 1)} 句已命中文献。",
                    detail=current_query,
                )
                chosen = next((article for article in results if article.pmid == chosen_pmid), None)
                if chosen is not None:
                    return {
                        "accepted": True,
                        "article": chosen,
                        "attempts": attempts,
                        "final_query": current_query,
                    }

            next_query = improved_query or fallback_query(sentence.text)
            if next_query.strip().lower() == current_query.strip().lower():
                next_query = mutate_query(current_query, sentence.text, attempt_index)
            _emit_progress(
                progress_callback,
                stage="refine_query",
                progress_percent=_target_progress(target_index - 1, total_targets, attempt_index, max_attempts),
                message=f"第 {target_index}/{max(total_targets, 1)} 句继续改写检索词。",
                detail=next_query,
            )
            current_query = next_query.strip()

        return {"accepted": False, "attempts": attempts, "final_query": current_query}

    def _evaluate_results(
        self,
        sentence: SentenceChunk,
        claim_summary: str,
        reason: str,
        current_query: str,
        attempt_index: int,
        attempts: list[dict[str, Any]],
        results: list[PubMedArticle],
    ) -> dict[str, Any]:
        previous_attempts = "\n".join(
            f"- attempt {item['attempt']}: query={item['query']}; decision={item['decision']}; "
            f"chosen_pmid={item['chosen_pmid']}; reason={item['reason']}"
            for item in attempts[-3:]
        )
        result_text = format_search_results(results)

        system_prompt = (
            "You evaluate whether PubMed search results directly support a target biomedical sentence. "
            "Return strict JSON only with this schema: "
            '{"decision":"accept|retry","chosen_pmid":"...","confidence":0.0,"reason":"...",'
            '"improved_query":"..."}. '
            "Accept only when one listed result clearly supports the sentence. "
            "If results are weak, off-topic, too broad, or missing, set decision to retry and give a better "
            "English PubMed-style query. chosen_pmid must be empty when decision is retry."
        )
        user_prompt = (
            f"Sentence: {sentence.text}\n"
            f"Claim summary: {claim_summary}\n"
            f"Why this sentence needs citation: {reason}\n"
            f"Current query: {current_query}\n"
            f"Attempt: {attempt_index}\n"
            f"Previous attempts:\n{previous_attempts or '- none'}\n\n"
            f"PubMed results:\n{result_text}\n\n"
            "Return JSON only."
        )

        response = self.llm.complete_json(system_prompt, user_prompt)
        if "decision" not in response:
            response["decision"] = "retry"
        return response


def split_sentences(text: str) -> list[SentenceChunk]:
    sentences: list[SentenceChunk] = []
    start = 0
    sentence_id = 1
    length = len(text)

    while start < length:
        while start < length and text[start].isspace():
            start += 1
        if start >= length:
            break

        end = start
        while end < length:
            char = text[end]
            if char in "\n":
                end += 1
                break
            if char in "。！？!?；;":
                end += 1
                break
            if char == "." and _looks_like_sentence_end(text, end):
                end += 1
                break
            end += 1

        segment = text[start:end]
        stripped = segment.strip()
        if stripped:
            leading = len(segment) - len(segment.lstrip())
            trailing = len(segment) - len(segment.rstrip())
            chunk_start = start + leading
            chunk_end = end - trailing
            sentences.append(
                SentenceChunk(
                    sentence_id=sentence_id,
                    text=text[chunk_start:chunk_end],
                    start=chunk_start,
                    end=chunk_end,
                )
            )
            sentence_id += 1
        start = end

    return sentences


def _looks_like_sentence_end(text: str, index: int) -> bool:
    previous_char = text[index - 1] if index > 0 else ""
    next_char = text[index + 1] if index + 1 < len(text) else ""
    if previous_char.isdigit() and next_char.isdigit():
        return False
    if next_char and not next_char.isspace() and next_char not in '"\')]}':
        return False
    return True


def format_search_results(results: list[PubMedArticle]) -> str:
    if not results:
        return "No results."
    lines: list[str] = []
    for index, article in enumerate(results, start=1):
        authors = ", ".join(article.authors[:4]) if article.authors else "Unknown authors"
        abstract = article.abstract[:900].replace("\n", " ").strip()
        lines.append(
            f"{index}. PMID={article.pmid}\n"
            f"Title: {article.title}\n"
            f"Journal: {article.journal}\n"
            f"Year: {article.year}\n"
            f"Authors: {authors}\n"
            f"Abstract: {abstract or 'No abstract available.'}\n"
        )
    return "\n".join(lines)


def fallback_query(text: str) -> str:
    normalized = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
    tokens = [token for token in normalized.split() if len(token) >= 3]
    if not tokens:
        return "biomedical evidence"
    return " ".join(tokens[:10])


def mutate_query(current_query: str, sentence_text: str, attempt_index: int) -> str:
    stripped = re.sub(r"\b(AND|OR|NOT)\b", " ", current_query, flags=re.IGNORECASE)
    stripped = re.sub(r"[^\w\s-]", " ", stripped, flags=re.UNICODE)
    terms = [term for term in stripped.split() if len(term) >= 3]
    if attempt_index % 2 == 0 and terms:
        return " ".join(terms[: max(3, min(7, len(terms)))])

    sentence_query = fallback_query(sentence_text)
    if sentence_query.lower() != current_query.lower():
        return sentence_query
    if terms:
        return " ".join(reversed(terms[:6]))
    return current_query


def insert_markers(text: str, sentences: list[SentenceChunk], placements: list[dict[str, Any]]) -> str:
    if not placements:
        return text

    sentence_map = {sentence.sentence_id: sentence for sentence in sentences}
    insertions: list[tuple[int, str]] = []
    used_sentence_ids: set[int] = set()

    for placement in placements:
        sentence_id = placement["sentence_id"]
        if sentence_id in used_sentence_ids:
            continue
        used_sentence_ids.add(sentence_id)
        sentence = sentence_map.get(sentence_id)
        if sentence is None:
            continue
        insert_at = _marker_insert_position(text, sentence.start, sentence.end)
        insertions.append((insert_at, f"[{placement['marker']}]"))

    rendered = text
    for position, marker in sorted(insertions, key=lambda item: item[0], reverse=True):
        rendered = rendered[:position] + marker + rendered[position:]
    return rendered


def _marker_insert_position(text: str, start: int, end: int) -> int:
    insert_at = end
    while insert_at > start and text[insert_at - 1].isspace():
        insert_at -= 1

    if insert_at > start and text[insert_at - 1] in "。！？!?.,;；:：":
        return insert_at - 1
    return insert_at


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    progress_percent: int,
    message: str,
    detail: str,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        {
            "stage": stage,
            "progress_percent": max(0, min(100, int(progress_percent))),
            "message": message,
            "detail": detail,
        }
    )


def _target_progress(
    completed_targets: int,
    total_targets: int,
    completed_attempts: int,
    max_attempts: int,
) -> int:
    if total_targets <= 0:
        return 30
    base = 22
    span = 68
    per_target = 1 / total_targets
    per_attempt = (completed_attempts / max(max_attempts, 1)) * per_target
    ratio = min(1.0, completed_targets * per_target + per_attempt)
    return int(base + span * ratio)
