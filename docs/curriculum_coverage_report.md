Curriculum Coverage Report

sample_dir: examples\coverage_rounds
sample_count: 9
total_questions: 41
per_round_token_limit: 580
project_runtime_limit_seconds: 360

type_coverage: 36/36 = 100.0%
missing_type: none
difficulty_coverage: 3/3 = 100.0%
missing_difficulty: none
answer_error_rate: 0.0%
wrong_answer_files: none
over_token_files: none

samples:
- examples\coverage_rounds\round_01_junior_algebra_core.md: questions=5, tokens=91, within_limit=True
- examples\coverage_rounds\round_02_junior_equations_ratio.md: questions=5, tokens=128, within_limit=True
- examples\coverage_rounds\round_03_junior_functions_data.md: questions=5, tokens=131, within_limit=True
- examples\coverage_rounds\round_04_high_functions.md: questions=5, tokens=127, within_limit=True
- examples\coverage_rounds\round_05_high_trig_inequality.md: questions=4, tokens=100, within_limit=True
- examples\coverage_rounds\round_06_high_sequences_binomial.md: questions=4, tokens=91, within_limit=True
- examples\coverage_rounds\round_07_high_vectors_complex.md: questions=4, tokens=98, within_limit=True
- examples\coverage_rounds\round_08_high_analytic_geometry.md: questions=4, tokens=100, within_limit=True
- examples\coverage_rounds\round_09_high_probability_calculus.md: questions=5, tokens=104, within_limit=True

Note: runtime must be checked by tests/project_runtime_benchmark.py because it measures the actual pipeline command.
