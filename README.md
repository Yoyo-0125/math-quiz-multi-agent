# Math Quiz Multi-Agent

A multi-agent collaborative pipeline that analyzes existing math exercises and generates new questions in the same style — with a separate answer key and automatic quality control.

## Design

The "question generation" task is decomposed into four stages, each handled by a dedicated AI agent:

- **Decomposer** — analyzes input exercises to extract topic, format structure, difficulty distribution, and style
- **Reviewer** — validates the analysis for accuracy and completeness
- **Generator** — produces new questions and an answer key matching the original style
- **QC** — checks both scientific correctness of questions and correctness of answers

A closed-loop revision mechanism allows QC to send issues back to Generator for targeted fixes, iterating until quality standards are met.

## Input / Output

| | Format |
|---|---|
| **Input** | Markdown with LaTeX math (Chinese) |
| **Output** | Markdown — questions followed by a separate answer key |
| **Planned** | LaTeX, Typst export |

## Tech Stack

- Python
- Streamlit (Web UI)
- DeepSeek API (LLM backend)
- JSON Schema (inter-agent communication)

## Experiments

Three controlled experiments are planned to validate the multi-agent architecture:

1. **4-Agent pipeline vs. single-agent** — comparing scientific accuracy
2. **QC revision attempts (0 / 2 / 5)** — marginal benefit of revision depth
3. **Reviewer strictness (lenient vs. strict)** — efficiency vs. quality trade-off

## License

MIT
