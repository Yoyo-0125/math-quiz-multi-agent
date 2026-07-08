import argparse
from pathlib import Path

from common import read_json_file, read_text_file, write_text_file


def read_if_exists(path):
    file_path = Path(path)
    if not file_path.exists():
        return ""
    return read_text_file(file_path)


def render_preview(output_dir="outputs", preview_path=None):
    output_path = Path(output_dir)
    if preview_path is None:
        preview_path = output_path / "vscode_preview.md"
    else:
        preview_path = Path(preview_path)

    decomposer_path = output_path / "decomposer_final.json"
    reviewer_path = output_path / "reviewer_final.json"
    generator_path = output_path / "generator_final.json"
    qc_path = output_path / "qc_final.json"

    questions = read_if_exists(output_path / "generated_questions_final.md")
    answer_key = read_if_exists(output_path / "answer_key_final.md")

    sections = ["# Math Quiz 可视化预览", ""]
    sections.append("> 在 VSCode 中打开本文件后，使用 Markdown Preview 查看数学公式。")
    sections.append("")

    if decomposer_path.exists():
        decomposer = read_json_file(decomposer_path)
        sections.extend(
            [
                "## 考点分析",
                "",
                f"- 主题：{decomposer.get('topic', '')}",
                f"- 年级：{decomposer.get('grade_level', '')}",
                f"- 题型：{', '.join(decomposer.get('question_types', []))}",
                f"- LaTeX：{decomposer.get('latex_usage', '')}",
                "",
                "### 出题约束",
                "",
            ]
        )
        for item in decomposer.get("generation_constraints", []):
            sections.append(f"- {item}")
        sections.append("")

    if questions:
        sections.extend(["## 新题", "", questions.strip(), ""])

    if answer_key:
        sections.extend(["## 答案", "", answer_key.strip(), ""])

    if reviewer_path.exists():
        reviewer = read_json_file(reviewer_path)
        sections.extend(
            [
                "## Reviewer",
                "",
                f"- 是否通过：{reviewer.get('is_valid')}",
                f"- 分数：{reviewer.get('score')}",
                f"- 总结：{reviewer.get('review_summary', '')}",
                "",
            ]
        )

    if qc_path.exists():
        qc = read_json_file(qc_path)
        sections.extend(
            [
                "## QC",
                "",
                f"- 是否通过：{qc.get('is_passed')}",
                f"- 分数：{qc.get('score')}",
                f"- 总结：{qc.get('qc_summary', '')}",
                "",
            ]
        )

    if generator_path.exists():
        generator = read_json_file(generator_path)
        sections.extend(["## 生成说明", ""])
        for item in generator.get("generation_notes", []):
            sections.append(f"- {item}")
        sections.append("")

    write_text_file(preview_path, "\n".join(sections).rstrip() + "\n")
    return str(preview_path)


def main():
    parser = argparse.ArgumentParser(description="Render a VSCode-friendly Markdown preview.")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--preview-path", default=None)
    args = parser.parse_args()

    path = render_preview(args.output_dir, args.preview_path)
    print(f"VSCode preview written: {path}")


if __name__ == "__main__":
    main()

