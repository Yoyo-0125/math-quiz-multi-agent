Model Selection Notes

Current strategy:

Decomposer: deepseek-v4-flash
Reviewer: deepseek-v4-flash
Generator: deepseek-v4-pro
QC: deepseek-v4-pro

Thinking:

Generator: enabled
QC: enabled

Reasoning effort:

Generator: high
QC: high

Why:

1. Decomposer and Reviewer mainly do structure analysis and review. Flash is kept for cost and speed.
2. Generator and QC directly affect answer correctness. They are the highest-risk steps for parameterized inequalities, so they use Pro.
3. Pro is not treated as a simple replacement for thinking. Complex math still benefits from thinking.
4. The intended reliability stack is: Pro + thinking + local math audit.

Official docs snapshot:

DeepSeek API docs list both deepseek-v4-flash and deepseek-v4-pro.
Both support non-thinking and thinking modes.
Both list 1M context length and 384K max output.

Pricing shown by the official docs:

deepseek-v4-flash:
- cache-hit input: $0.0028 / 1M tokens
- cache-miss input: $0.14 / 1M tokens
- output: $0.28 / 1M tokens
- concurrency limit: 2500

deepseek-v4-pro:
- cache-hit input: $0.003625 / 1M tokens
- cache-miss input: $0.435 / 1M tokens
- output: $0.87 / 1M tokens
- concurrency limit: 500

Interpretation:

Pro is materially more expensive, especially for cache-miss input and output.
For this project, Pro should be reserved for answer-producing and answer-checking steps, not every agent.

Risks:

1. Pro is expected to cost more than Flash.
2. Thinking increases latency and token use.
3. Pro still cannot replace local deterministic checks for root ordering, denominator exclusions, and sign-changing leading coefficients.

Rollback:

If cost or speed becomes a problem, change config/pipeline_options.json:

generator: deepseek-v4-pro
qc: deepseek-v4-pro

back to:

generator: deepseek-v4-flash
qc: deepseek-v4-flash

Keep math_checks enabled either way.
