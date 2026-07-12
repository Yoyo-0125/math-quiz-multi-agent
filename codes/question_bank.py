import argparse
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path


QUESTION_BANK_DIRNAME = "question_bank"
MODE_LABELS = {
    "style": "风格类似",
    "knowledge": "考点类似",
    "difficulty": "难度类似",
    "mixed": "综合类似",
}


def read_json_text(text, default):
    try:
        return json.loads(text)
    except Exception:
        return default


def normalize_text(text):
    return re.sub(r"\s+", "", str(text or "").lower())


def text_tokens(text):
    raw = str(text or "").lower()
    tokens = re.findall(r"[a-zA-Z]+|\d+|[\u4e00-\u9fff]", raw)
    if tokens:
        return set(tokens)
    compact = normalize_text(raw)
    return {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}


def jaccard(left, right):
    left = set(left or [])
    right = set(right or [])
    if not left and not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def bank_root(result_root):
    return Path(result_root) / QUESTION_BANK_DIRNAME


def load_taxonomy_map(taxonomy):
    mapping = {}
    for item in taxonomy or []:
        tag_id = item.get("id") or item.get("tag_id")
        if tag_id:
            mapping[tag_id] = item
    return mapping


def tag_dimension(tag, taxonomy_map):
    if isinstance(tag, dict):
        return tag.get("dimension") or tag.get("type") or ""
    meta = taxonomy_map.get(str(tag), {})
    return meta.get("dimension") or meta.get("type") or ""


def tag_name(tag, taxonomy_map):
    if isinstance(tag, dict):
        return tag.get("name") or tag.get("label") or tag.get("id") or ""
    meta = taxonomy_map.get(str(tag), {})
    return meta.get("name") or meta.get("label") or str(tag)


def tag_id(tag):
    if isinstance(tag, dict):
        return tag.get("id") or tag.get("tag_id") or tag.get("name") or ""
    return str(tag)


def enrich_question(record, taxonomy_map):
    tags = record.get("tags") or []
    normalized_tags = []
    if isinstance(tags, dict):
        for dimension, values in tags.items():
            for value in values or []:
                if isinstance(value, dict):
                    tagged = dict(value)
                    tagged["dimension"] = tagged.get("dimension") or dimension
                    normalized_tags.append(tagged)
                else:
                    normalized_tags.append({"id": value, "name": str(value), "dimension": dimension})
    else:
        normalized_tags = tags
    tag_names = []
    tag_ids = []
    by_dimension = {}
    for tag in normalized_tags:
        current_id = tag_id(tag)
        if current_id:
            tag_ids.append(current_id)
        name = tag_name(tag, taxonomy_map)
        if name:
            tag_names.append(name)
        dimension = tag_dimension(tag, taxonomy_map) or "other"
        by_dimension.setdefault(dimension, []).append(name or current_id)

    style_terms = set(by_dimension.get("question_type", []))
    style_terms.update(by_dimension.get("solution_idea", []))
    if record.get("question_type_field"):
        style_terms.add(str(record.get("question_type_field")))

    knowledge_terms = set(by_dimension.get("knowledge_point", []))
    difficulty_terms = {
        str(record.get("difficulty") or "未标注"),
        str(record.get("grade_band") or "未标注"),
    }
    searchable = "\n".join(
        str(record.get(key, ""))
        for key in ["stem", "prompt", "answer", "question_type_field", "difficulty", "grade_band"]
    )
    searchable += "\n" + "\n".join(tag_names)

    enriched = dict(record)
    enriched["tag_ids"] = sorted(set(tag_ids))
    enriched["tag_names"] = sorted(set(tag_names))
    enriched["tag_by_dimension"] = {
        key: sorted(set(value)) for key, value in by_dimension.items()
    }
    enriched["similarity_features"] = {
        "style": sorted(style_terms),
        "knowledge": sorted(knowledge_terms),
        "difficulty": sorted(difficulty_terms),
        "text_tokens": sorted(text_tokens(searchable)),
    }
    return enriched


def iter_zip_jsonl(zip_file, name):
    with zip_file.open(name) as file:
        for raw_line in file:
            line = raw_line.decode("utf-8-sig").strip()
            if line:
                yield json.loads(line)


def import_export_zip(zip_path, result_root="result"):
    zip_path = Path(zip_path)
    target = bank_root(result_root)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        banks = json.loads(archive.read("banks.json").decode("utf-8-sig"))
        taxonomy = json.loads(archive.read("taxonomy.json").decode("utf-8-sig"))
        taxonomy_map = load_taxonomy_map(taxonomy)
        questions = [
            enrich_question(record, taxonomy_map)
            for record in iter_zip_jsonl(archive, "questions.jsonl")
        ]

    tag_counter = Counter()
    dimension_counter = Counter()
    for question in questions:
        tag_counter.update(question.get("tag_names", []))
        for dimension, names in question.get("tag_by_dimension", {}).items():
            dimension_counter[dimension] += len(names)

    (target / "banks.json").write_text(
        json.dumps(banks, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target / "taxonomy.json").write_text(
        json.dumps(taxonomy, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (target / "questions.jsonl").open("w", encoding="utf-8", newline="\n") as file:
        for question in questions:
            file.write(json.dumps(question, ensure_ascii=False) + "\n")

    index = {
        "source_zip": str(zip_path),
        "imported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bank_count": len(banks),
        "question_count": len(questions),
        "top_tags": tag_counter.most_common(40),
        "dimension_counts": dict(dimension_counter),
    }
    (target / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return index


def load_question_bank(result_root="result"):
    target = bank_root(result_root)
    questions_path = target / "questions.jsonl"
    if not questions_path.exists():
        return []
    questions = []
    with questions_path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            line = line.strip()
            if line:
                questions.append(json.loads(line))
    return questions


def infer_query_features(query_text, questions):
    query_tokens = text_tokens(query_text)
    known = {
        "style": set(),
        "knowledge": set(),
        "difficulty": set(),
        "text_tokens": sorted(query_tokens),
    }
    compact_query = normalize_text(query_text)
    for question in questions:
        features = question.get("similarity_features", {})
        for key in ["style", "knowledge", "difficulty"]:
            for term in features.get(key, []):
                if term and normalize_text(term) in compact_query:
                    known[key].add(term)
    known["style"] = sorted(known["style"])
    known["knowledge"] = sorted(known["knowledge"])
    known["difficulty"] = sorted(known["difficulty"])
    return known


def similarity_score(query_features, question, mode):
    features = question.get("similarity_features", {})
    text_score = jaccard(query_features.get("text_tokens"), features.get("text_tokens"))
    style_score = jaccard(query_features.get("style"), features.get("style"))
    knowledge_score = jaccard(query_features.get("knowledge"), features.get("knowledge"))
    difficulty_score = jaccard(query_features.get("difficulty"), features.get("difficulty"))
    if mode == "style":
        return 0.55 * style_score + 0.35 * text_score + 0.10 * knowledge_score
    if mode == "knowledge":
        return 0.60 * knowledge_score + 0.30 * text_score + 0.10 * style_score
    if mode == "difficulty":
        return 0.55 * difficulty_score + 0.25 * style_score + 0.20 * text_score
    return 0.30 * style_score + 0.35 * knowledge_score + 0.20 * difficulty_score + 0.15 * text_score


def similar_questions(query_text, mode="mixed", limit=8, result_root="result"):
    questions = load_question_bank(result_root)
    if not questions:
        return []
    query_features = infer_query_features(query_text, questions)
    ranked = []
    for question in questions:
        score = similarity_score(query_features, question, mode)
        ranked.append((score, question))
    ranked.sort(key=lambda item: (item[0], normalize_text(item[1].get("stem", ""))), reverse=True)
    return [
        {
            "score": round(score, 4),
            "mode": mode,
            "item_id": question.get("item_id") or question.get("marwyn_id") or "",
            "bank_id": question.get("bank_id", ""),
            "grade_band": question.get("grade_band", ""),
            "difficulty": question.get("difficulty", ""),
            "question_type": question.get("question_type_field", ""),
            "stem": question.get("stem", ""),
            "prompt": question.get("prompt", ""),
            "answer": question.get("answer", ""),
            "tags": question.get("tag_names", [])[:12],
        }
        for score, question in ranked[: max(1, int(limit))]
    ]


def render_similar_markdown(results_by_mode):
    if not results_by_mode:
        return "暂无题库相似题结果。\n"
    lines = ["# 题库中类似题", ""]
    for mode, results in results_by_mode.items():
        lines.extend([f"## {MODE_LABELS.get(mode, mode)}", ""])
        if not results:
            lines.extend(["暂无匹配。", ""])
            continue
        for index, item in enumerate(results, start=1):
            tags = "、".join(item.get("tags", [])[:6]) or "未标注"
            stem = (item.get("stem") or item.get("prompt") or "").strip()
            answer = str(item.get("answer") or "").strip()
            lines.append(
                f"{index}. "
                f"score={item.get('score')} 难度={item.get('difficulty') or '未标注'} "
                f"类型={item.get('question_type') or '未标注'}"
            )
            lines.append(f"   标签：{tags}")
            lines.append(f"   题目：{stem[:420]}")
            if answer:
                lines.append(f"   答案：{answer[:260]}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_similar_outputs(input_path, output_dir="outputs", result_root="result", limit=8):
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    query_text = input_path.read_text(encoding="utf-8-sig") if input_path.exists() else ""
    modes = ["style", "knowledge", "difficulty"]
    results = {
        mode: similar_questions(query_text, mode=mode, limit=limit, result_root=result_root)
        for mode in modes
    }
    (output_dir / "question_bank_similar.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown = render_similar_markdown(results)
    (output_dir / "question_bank_similar.md").write_text(markdown, encoding="utf-8")
    return results


def main():
    parser = argparse.ArgumentParser(description="Import and search the local math question bank.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--zip", required=True, dest="zip_path")
    import_parser.add_argument("--result-root", default="result")

    similar_parser = subparsers.add_parser("similar")
    similar_parser.add_argument("--input", default="outputs/generated_questions_final.md")
    similar_parser.add_argument("--output-dir", default="outputs")
    similar_parser.add_argument("--result-root", default="result")
    similar_parser.add_argument("--limit", type=int, default=8)

    args = parser.parse_args()
    if args.command == "import":
        summary = import_export_zip(args.zip_path, result_root=args.result_root)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "similar":
        results = write_similar_outputs(
            args.input,
            output_dir=args.output_dir,
            result_root=args.result_root,
            limit=args.limit,
        )
        print(json.dumps({key: len(value) for key, value in results.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

