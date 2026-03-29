from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.ncbi import NCBIClient, PubMedArticle
from app.services.openalex import OpenAlexClient
from app.services.openai_compat import OpenAICompatClient


class CitationPipelineError(RuntimeError):
    pass


MAX_ARTICLES_PER_SENTENCE = 3
QUERY_META_TERMS = {
    "and",
    "or",
    "not",
    "title",
    "abstract",
    "publication",
    "type",
    "review",
    "reviews",
    "scoping",
    "perspective",
    "perspectives",
    "comment",
    "comments",
    "mesh",
    "majr",
    "tiab",
    "pt",
    "all",
    "field",
    "fields",
}


@dataclass(slots=True)
class SentenceChunk:
    sentence_id: int
    text: str
    start: int
    end: int


@dataclass(slots=True)
class SearchFilters:
    recent_years: int | None = None
    impact_factor_min: float | None = None
    impact_factor_max: float | None = None

    def has_impact_factor_filter(self) -> bool:
        return self.impact_factor_min is not None or self.impact_factor_max is not None

    def has_any_filter(self) -> bool:
        return self.recent_years is not None or self.has_impact_factor_filter()

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent_years": self.recent_years,
            "impact_factor_min": self.impact_factor_min,
            "impact_factor_max": self.impact_factor_max,
        }


@dataclass(slots=True)
class AttemptPlan:
    phase: str
    label: str
    query_guidance: str
    search_filters: SearchFilters
    query_round_index: int = 0
    query_round_total: int = 0


class CitationPipeline:
    def __init__(
        self,
        llm: OpenAICompatClient,
        ncbi: NCBIClient,
        openalex: OpenAlexClient | None = None,
    ) -> None:
        self.llm = llm
        self.ncbi = ncbi
        self.openalex = openalex

    def run(
        self,
        text: str,
        max_targets: int = 4,
        max_attempts: int = 10,
        results_per_query: int = 6,
        search_filters: SearchFilters | None = None,
        existing_references: list[dict[str, Any]] | None = None,
        existing_placements: list[dict[str, Any]] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        source_text = (text or "").strip()
        if not source_text:
            raise CitationPipelineError("Text is required.")
        if len(source_text) > 20000:
            raise CitationPipelineError("Text is too long. Please keep it within 20,000 characters.")
        active_filters = search_filters or SearchFilters()
        if active_filters.has_impact_factor_filter() and self.openalex is None:
            raise CitationPipelineError("使用 IF 区间过滤前，请先在 auth.json 配置 OPENALEX_APIkey。")

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

        normalized_references = _normalize_existing_references(existing_references or [])
        valid_markers = {int(reference["marker"]) for reference in normalized_references}
        normalized_placements = _normalize_existing_placements(
            existing_placements or [],
            sentences=sentences,
            valid_markers=valid_markers,
        )
        cited_sentence_ids = {int(placement["sentence_id"]) for placement in normalized_placements}
        selectable_sentences = [sentence for sentence in sentences if sentence.sentence_id not in cited_sentence_ids]
        marker_map = {
            str(reference.get("article", {}).get("pmid", "")).strip(): int(reference["marker"])
            for reference in normalized_references
            if str(reference.get("article", {}).get("pmid", "")).strip()
        }
        references: list[dict[str, Any]] = list(normalized_references)
        placements: list[dict[str, Any]] = list(normalized_placements)
        unresolved_targets: list[dict[str, Any]] = []
        existing_reference_count = len(references)
        existing_placement_count = len(placements)

        _emit_progress(
            progress_callback,
            stage="select_targets",
            progress_percent=14,
            message=f"已识别 {len(sentences)} 个句子。",
            detail="正在选择最需要插入文献的位置。",
        )
        planned_targets = sorted(
            self._select_targets(selectable_sentences, max_targets),
            key=lambda item: item["sentence_id"],
        )
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
                search_filters=active_filters,
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

            resolved_articles: list[PubMedArticle] = resolution["articles"]
            marker_entries: list[dict[str, Any]] = []
            for article in resolved_articles:
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
                marker_entries.append({"marker": marker, "article": article.to_brief_dict()})

            markers = [item["marker"] for item in marker_entries]
            placements.append(
                {
                    "marker": markers[0],
                    "markers": markers,
                    "sentence_id": sentence.sentence_id,
                    "sentence_text": sentence.text,
                    "claim_summary": target.get("claim_summary", ""),
                    "reason": target.get("reason", ""),
                    "final_query": resolution["final_query"],
                    "article": marker_entries[0]["article"],
                    "articles": [item["article"] for item in marker_entries],
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
            "search_filters": active_filters.to_dict(),
            "source_text": source_text,
            "continued_from_existing": bool(existing_reference_count or existing_placement_count),
            "existing_reference_count": existing_reference_count,
            "existing_placement_count": existing_placement_count,
            "new_reference_count": max(0, len(references) - existing_reference_count),
            "new_placement_count": max(0, len(placements) - existing_placement_count),
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
        search_filters: SearchFilters,
        target_index: int,
        total_targets: int,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        current_query = (initial_query or fallback_query(sentence.text)).strip()
        attempts: list[dict[str, Any]] = []
        attempt_plans = _build_attempt_plans(max_attempts=max_attempts, base_filters=search_filters)

        for attempt_index, current_plan in enumerate(attempt_plans, start=1):
            next_plan = attempt_plans[attempt_index] if attempt_index < len(attempt_plans) else None
            min_publication_year = _minimum_publication_year(current_plan.search_filters.recent_years)
            raw_results_limit = _raw_results_limit(results_per_query, current_plan.search_filters)
            _emit_progress(
                progress_callback,
                stage="search_attempt",
                progress_percent=_target_progress(target_index - 1, total_targets, attempt_index - 1, max_attempts),
                message=f"第 {target_index}/{max(total_targets, 1)} 句，第 {attempt_index}/{max_attempts} 轮检索。",
                detail=f"{current_plan.label} · {current_query}",
            )
            raw_results = self.ncbi.search_pubmed(
                current_query,
                retmax=raw_results_limit,
                min_publication_year=min_publication_year,
            )
            filtered_results = self._filter_results(raw_results, current_plan.search_filters)
            results = filtered_results[:results_per_query]
            evaluation = self._evaluate_results(
                sentence=sentence,
                claim_summary=claim_summary,
                reason=reason,
                current_query=current_query,
                current_plan=current_plan,
                next_plan=next_plan,
                attempt_index=attempt_index,
                attempts=attempts,
                results=results,
            )
            decision = str(evaluation.get("decision", "")).strip().lower()
            selected_pmids = _normalize_selected_pmids(evaluation, results)
            improved_query = str(evaluation.get("improved_query", "")).strip()
            evaluation_reason = str(evaluation.get("reason", "")).strip()
            confidence = evaluation.get("confidence", 0)

            attempts.append(
                {
                    "attempt": attempt_index,
                    "query": current_query,
                    "result_count": len(results),
                    "raw_result_count": len(raw_results),
                    "filtered_out_count": max(0, len(raw_results) - len(results)),
                    "decision": decision or "retry",
                    "strategy": current_plan.phase,
                    "strategy_label": current_plan.label,
                    "applied_search_filters": current_plan.search_filters.to_dict(),
                    "chosen_pmid": selected_pmids[0] if selected_pmids else "",
                    "chosen_pmids": selected_pmids,
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
                chosen_articles = [article for article in results if article.pmid in selected_pmids]
                if chosen_articles:
                    return {
                        "accepted": True,
                        "articles": chosen_articles,
                        "attempts": attempts,
                        "final_query": current_query,
                    }

            if next_plan is None:
                continue
            next_query = build_retry_query(
                current_query=current_query,
                improved_query=improved_query,
                sentence_text=sentence.text,
                attempt_index=attempt_index,
                next_plan=next_plan,
            )
            _emit_progress(
                progress_callback,
                stage="refine_query",
                progress_percent=_target_progress(target_index - 1, total_targets, attempt_index, max_attempts),
                message=f"第 {target_index}/{max(total_targets, 1)} 句继续改写检索词。",
                detail=f"{next_plan.label} · {next_query}",
            )
            current_query = next_query.strip()

        return {"accepted": False, "attempts": attempts, "final_query": current_query}

    def _filter_results(
        self,
        results: list[PubMedArticle],
        search_filters: SearchFilters,
    ) -> list[PubMedArticle]:
        filtered = list(results)

        min_publication_year = _minimum_publication_year(search_filters.recent_years)
        if min_publication_year is not None:
            filtered = [
                article
                for article in filtered
                if (_parse_year_value(article.year) or 0) >= min_publication_year
            ]

        if not search_filters.has_impact_factor_filter():
            return filtered

        matched: list[PubMedArticle] = []
        for article in filtered:
            impact_factor = self._resolve_impact_factor(article)
            if impact_factor is None:
                continue
            if search_filters.impact_factor_min is not None and impact_factor < search_filters.impact_factor_min:
                continue
            if search_filters.impact_factor_max is not None and impact_factor > search_filters.impact_factor_max:
                continue
            matched.append(article)
        return matched

    def _resolve_impact_factor(self, article: PubMedArticle) -> float | None:
        if article.impact_factor is not None:
            return article.impact_factor
        if self.openalex is None or not article.issn:
            return None

        metrics = self.openalex.get_source_metrics(article.issn)
        if metrics is None or metrics.impact_factor is None:
            return None

        article.impact_factor = metrics.impact_factor
        article.impact_factor_source = "OpenAlex 2yr_mean_citedness"
        if metrics.display_name and not article.journal:
            article.journal = metrics.display_name
        return article.impact_factor

    def _evaluate_results(
        self,
        sentence: SentenceChunk,
        claim_summary: str,
        reason: str,
        current_query: str,
        current_plan: AttemptPlan,
        next_plan: AttemptPlan | None,
        attempt_index: int,
        attempts: list[dict[str, Any]],
        results: list[PubMedArticle],
    ) -> dict[str, Any]:
        previous_attempts = "\n".join(
            f"- attempt {item['attempt']}: query={item['query']}; decision={item['decision']}; "
            f"chosen_pmids={','.join(item.get('chosen_pmids', []))}; reason={item['reason']}"
            for item in attempts[-3:]
        )
        result_text = format_search_results(results)

        system_prompt = (
            "You evaluate whether PubMed search results directly support a target biomedical sentence. "
            "Return strict JSON only with this schema: "
            '{"decision":"accept|retry","chosen_pmids":["..."],"confidence":0.0,"reason":"...",'
            '"improved_query":"..."}. '
            f"Accept only when one or more listed results clearly support the sentence. Choose 1 to {MAX_ARTICLES_PER_SENTENCE} PMIDs only. "
            "Prefer the smallest sufficient set. Multiple PMIDs are allowed when they are all directly relevant. "
            "If results are weak, off-topic, too broad, or missing, set decision to retry and give a better "
            "English PubMed-style query. chosen_pmids must be an empty array when decision is retry. "
            "When you provide improved_query for a retry, follow the requested next-round strategy and do not make "
            "the next query narrower than instructed."
        )
        user_prompt = (
            f"Sentence: {sentence.text}\n"
            f"Claim summary: {claim_summary}\n"
            f"Why this sentence needs citation: {reason}\n"
            f"Current query: {current_query}\n"
            f"Current round strategy: {current_plan.label}\n"
            f"Current round filters: {_describe_search_filters(current_plan.search_filters)}\n"
            f"Next round strategy if retry: {next_plan.label if next_plan else 'No next round; this is the final attempt.'}\n"
            f"Next round filters if retry: {_describe_search_filters(next_plan.search_filters) if next_plan else 'No next round.'}\n"
            f"Next round query guidance: {next_plan.query_guidance if next_plan else 'No next round.'}\n"
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
        impact_factor_line = (
            f"Impact Factor: {article.impact_factor:.3f} ({article.impact_factor_source})\n"
            if article.impact_factor is not None
            else ""
        )
        lines.append(
            f"{index}. PMID={article.pmid}\n"
            f"Title: {article.title}\n"
            f"Journal: {article.journal}\n"
            f"Year: {article.year}\n"
            f"{impact_factor_line}"
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


def build_retry_query(
    *,
    current_query: str,
    improved_query: str,
    sentence_text: str,
    attempt_index: int,
    next_plan: AttemptPlan,
) -> str:
    candidate_query = _normalize_query_text(improved_query or current_query or fallback_query(sentence_text))
    if next_plan.phase == "strict":
        if candidate_query.strip().lower() != _normalize_query_text(current_query).lower():
            return candidate_query
        return _build_strict_retry_query(current_query=current_query, sentence_text=sentence_text, attempt_index=attempt_index)

    relaxed_query = _build_relaxed_query(candidate_query, sentence_text, next_plan)
    if relaxed_query.strip().lower() == _normalize_query_text(current_query).lower():
        relaxed_query = _build_relaxed_query(sentence_text, sentence_text, next_plan)
    if not relaxed_query:
        return _normalize_query_text(mutate_query(current_query, sentence_text, attempt_index))
    return relaxed_query


def _normalize_selected_pmids(response: dict[str, Any], results: list[PubMedArticle]) -> list[str]:
    valid_pmids = {article.pmid for article in results}
    ordered_result_pmids = [article.pmid for article in results]

    raw_pmids = response.get("chosen_pmids")
    values: list[str] = []
    if isinstance(raw_pmids, list):
        values.extend(str(item or "").strip() for item in raw_pmids)
    else:
        single = str(response.get("chosen_pmid", "")).strip()
        if single:
            values.append(single)

    seen: set[str] = set()
    selected = []
    for pmid in ordered_result_pmids:
        if pmid not in valid_pmids:
            continue
        if pmid not in values:
            continue
        if pmid in seen:
            continue
        seen.add(pmid)
        selected.append(pmid)
        if len(selected) >= MAX_ARTICLES_PER_SENTENCE:
            break
    return selected


def _build_attempt_plans(max_attempts: int, base_filters: SearchFilters) -> list[AttemptPlan]:
    strict_rounds = min(3, max_attempts)
    remaining_rounds = max(0, max_attempts - strict_rounds)

    phase_sequence = ["strict"] * strict_rounds
    if remaining_rounds > 0:
        tail_phases = ["topic_only"]
        if remaining_rounds >= 2:
            tail_phases.insert(0, "relax_query")
        if base_filters.has_impact_factor_filter() and remaining_rounds > len(tail_phases):
            tail_phases.insert(-1, "relax_if")
        if base_filters.recent_years is not None and remaining_rounds > len(tail_phases):
            tail_phases.insert(-1, "relax_year")

        extra_query_rounds = max(0, remaining_rounds - len(tail_phases))
        phase_sequence.extend(["relax_query"] * extra_query_rounds)
        phase_sequence.extend(tail_phases)

    total_query_rounds = phase_sequence.count("relax_query")
    query_round_index = 0
    plans: list[AttemptPlan] = []
    for phase in phase_sequence:
        if phase == "relax_query":
            query_round_index += 1
        plans.append(
            _build_attempt_plan(
                phase=phase,
                base_filters=base_filters,
                query_round_index=query_round_index,
                query_round_total=total_query_rounds,
            )
        )
    return plans


def _build_attempt_plan(
    *,
    phase: str,
    base_filters: SearchFilters,
    query_round_index: int,
    query_round_total: int,
) -> AttemptPlan:
    if phase == "strict":
        return AttemptPlan(
            phase=phase,
            label="严格条件",
            query_guidance=(
                "Keep the query specific. Preserve the core disease/exposure/outcome concepts and do not "
                "broaden the search yet."
            ),
            search_filters=SearchFilters(
                recent_years=base_filters.recent_years,
                impact_factor_min=base_filters.impact_factor_min,
                impact_factor_max=base_filters.impact_factor_max,
            ),
        )
    if phase == "relax_query":
        return AttemptPlan(
            phase=phase,
            label=f"放宽检索词 {query_round_index}/{max(query_round_total, 1)}",
            query_guidance=(
                "Broaden the search modestly. Remove secondary modifiers, publication-type limits, and redundant "
                "synonyms, but keep the same biomedical topic."
            ),
            search_filters=SearchFilters(
                recent_years=base_filters.recent_years,
                impact_factor_min=base_filters.impact_factor_min,
                impact_factor_max=base_filters.impact_factor_max,
            ),
            query_round_index=query_round_index,
            query_round_total=query_round_total,
        )
    if phase == "relax_if":
        return AttemptPlan(
            phase=phase,
            label="放宽 IF",
            query_guidance=(
                "Broaden the query further. The next round may ignore impact-factor limits, so keep only the "
                "main biomedical concepts."
            ),
            search_filters=SearchFilters(recent_years=base_filters.recent_years),
        )
    if phase == "relax_year":
        return AttemptPlan(
            phase=phase,
            label="放宽年份",
            query_guidance=(
                "Broaden the query again. The next round may ignore both impact-factor and year limits, so keep "
                "only the central topic terms."
            ),
            search_filters=SearchFilters(),
        )
    return AttemptPlan(
        phase="topic_only",
        label="主题兜底",
        query_guidance=(
            "Final fallback. Keep only the core topic terms that best represent the sentence and ignore IF/year "
            "constraints."
        ),
        search_filters=SearchFilters(),
    )


def _describe_search_filters(search_filters: SearchFilters) -> str:
    parts: list[str] = []
    if search_filters.recent_years is not None:
        parts.append(f"publication year within the last {search_filters.recent_years} years")
    if search_filters.has_impact_factor_filter():
        lower = (
            f">= {search_filters.impact_factor_min:.3f}"
            if search_filters.impact_factor_min is not None
            else "no lower bound"
        )
        upper = (
            f"<= {search_filters.impact_factor_max:.3f}"
            if search_filters.impact_factor_max is not None
            else "no upper bound"
        )
        parts.append(f"impact factor {lower}, {upper}")
    return "; ".join(parts) if parts else "no IF or year filters"


def _build_relaxed_query(query_text: str, sentence_text: str, attempt_plan: AttemptPlan) -> str:
    primary_terms = _extract_search_terms(query_text)
    secondary_terms = _extract_search_terms(sentence_text)
    if attempt_plan.phase == "topic_only":
        merged_terms = _merge_terms(secondary_terms, primary_terms)
    else:
        merged_terms = _merge_terms(primary_terms, secondary_terms)

    if not merged_terms:
        return _normalize_query_text(fallback_query(sentence_text))

    limit = _query_term_limit(attempt_plan)
    query = " ".join(merged_terms[:limit])
    current_normalized = _normalize_query_text(query_text).lower()
    if current_normalized and _normalize_query_text(query).lower() == current_normalized and len(merged_terms) > 3:
        query = " ".join(merged_terms[: max(3, limit - 1)])
    return _normalize_query_text(query)


def _build_strict_retry_query(*, current_query: str, sentence_text: str, attempt_index: int) -> str:
    query_terms = _extract_search_terms(current_query)
    if query_terms:
        limit = max(6, 9 - min(2, max(0, attempt_index - 1)))
        return _normalize_query_text(" ".join(query_terms[:limit]))
    return _normalize_query_text(mutate_query(current_query, sentence_text, attempt_index))


def _query_term_limit(attempt_plan: AttemptPlan) -> int:
    if attempt_plan.phase == "relax_query":
        return max(4, 8 - min(4, attempt_plan.query_round_index))
    if attempt_plan.phase == "relax_if":
        return 5
    return 4


def _extract_search_terms(text: str) -> list[str]:
    normalized = re.sub(r"\[[^\]]+\]", " ", str(text or ""))
    normalized = normalized.replace("*", " ")
    normalized = re.sub(r"[^\w\s-]", " ", normalized, flags=re.UNICODE)
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in normalized.split():
        term = raw_term.strip("-_").lower()
        if len(term) < 3 or term.isdigit():
            continue
        if term in QUERY_META_TERMS:
            continue
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def _merge_terms(primary_terms: list[str], secondary_terms: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for term in primary_terms + secondary_terms:
        normalized = str(term or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def _normalize_query_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_existing_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    used_markers: set[int] = set()
    used_pmids: set[str] = set()

    for item in sorted(references, key=lambda value: int(value.get("marker", 0) or 0)):
        if not isinstance(item, dict):
            continue
        try:
            marker = int(item.get("marker"))
        except Exception:  # noqa: BLE001
            continue
        if marker <= 0 or marker in used_markers:
            continue
        article = item.get("article", {})
        if not isinstance(article, dict):
            continue
        pmid = str(article.get("pmid", "")).strip()
        if not pmid or pmid in used_pmids:
            continue
        used_markers.add(marker)
        used_pmids.add(pmid)
        normalized.append(
            {
                "marker": marker,
                "article": article,
                "reference_line": str(item.get("reference_line", "")).strip() or _fallback_reference_line(marker, article),
            }
        )
    return normalized


def _normalize_existing_placements(
    placements: list[dict[str, Any]],
    *,
    sentences: list[SentenceChunk],
    valid_markers: set[int],
) -> list[dict[str, Any]]:
    sentence_map = {sentence.sentence_id: sentence for sentence in sentences}
    normalized: list[dict[str, Any]] = []
    used_sentence_ids: set[int] = set()

    for item in placements:
        if not isinstance(item, dict):
            continue
        try:
            sentence_id = int(item.get("sentence_id"))
        except Exception:  # noqa: BLE001
            continue
        if sentence_id in used_sentence_ids:
            continue
        sentence = sentence_map.get(sentence_id)
        if sentence is None:
            continue
        sentence_text = str(item.get("sentence_text", "")).strip()
        if sentence_text and sentence_text != sentence.text:
            continue

        markers = item.get("markers")
        if not isinstance(markers, list):
            markers = [item.get("marker")]
        normalized_markers: list[int] = []
        seen_markers: set[int] = set()
        for marker_value in markers:
            try:
                marker = int(marker_value)
            except Exception:  # noqa: BLE001
                continue
            if marker <= 0 or marker not in valid_markers or marker in seen_markers:
                continue
            seen_markers.add(marker)
            normalized_markers.append(marker)
        if not normalized_markers:
            continue

        articles = item.get("articles")
        if not isinstance(articles, list) or not articles:
            article = item.get("article")
            articles = [article] if isinstance(article, dict) else []
        normalized_articles = [article for article in articles if isinstance(article, dict)]
        if not normalized_articles:
            normalized_articles = [{} for _ in normalized_markers]

        normalized.append(
            {
                "marker": normalized_markers[0],
                "markers": normalized_markers,
                "sentence_id": sentence_id,
                "sentence_text": sentence.text,
                "claim_summary": str(item.get("claim_summary", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "final_query": str(item.get("final_query", "")).strip(),
                "article": normalized_articles[0],
                "articles": normalized_articles,
                "attempts": item.get("attempts", []) if isinstance(item.get("attempts"), list) else [],
            }
        )
        used_sentence_ids.add(sentence_id)
    return normalized


def _fallback_reference_line(marker: int, article: dict[str, Any]) -> str:
    title = str(article.get("title", "")).strip()
    pmid = str(article.get("pmid", "")).strip()
    if title:
        return f"[{marker}] {title}."
    if pmid:
        return f"[{marker}] PMID:{pmid}."
    return f"[{marker}]"


def _minimum_publication_year(recent_years: int | None) -> int | None:
    if recent_years is None or recent_years <= 0:
        return None
    current_year = datetime.now().year
    return max(1900, current_year - recent_years + 1)


def _parse_year_value(value: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    if not match:
        return None
    return int(match.group(0))


def _raw_results_limit(results_per_query: int, search_filters: SearchFilters) -> int:
    if not search_filters.has_any_filter():
        return results_per_query
    multiplier = 4 if search_filters.has_impact_factor_filter() else 2
    return max(results_per_query, min(60, results_per_query * multiplier))


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
        markers = placement.get("markers") or [placement["marker"]]
        marker_text = "[" + ", ".join(str(marker) for marker in markers) + "]"
        insertions.append((insert_at, marker_text))

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
